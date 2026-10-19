"""V3 日频信号回测内核。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from sequoia_x.backtest.models import (
    AnnualReturn,
    BacktestConfig,
    BacktestProgress,
    BacktestResult,
    BenchmarkResult,
    ForwardOutcome,
    SignalRecord,
)
from sequoia_x.backtest.portfolio import PortfolioSimulator
from sequoia_x.data.research import ResearchDataError, ResearchSnapshot
from sequoia_x.strategy import create_strategy
from sequoia_x.strategy.base import BaseStrategy


class BacktestSnapshotSource(Protocol):
    def complete_trading_dates(self, start_date: str) -> tuple[str, ...]: ...

    def prepare_snapshots(self, dates: tuple[str, ...], history_bars: int) -> None: ...

    def build_snapshot(self, as_of_date: str, history_bars: int) -> ResearchSnapshot: ...

    def benchmark_closes(
        self, instrument_id: str, dates: tuple[str, ...]
    ) -> tuple[tuple[str, float], ...]: ...


class BacktestEngine:
    """逐日运行现有纯策略，并评价收盘信号的前瞻收益。"""

    def __init__(
        self,
        data: BacktestSnapshotSource,
        *,
        strategy_factory: Callable[[str], BaseStrategy] = create_strategy,
    ) -> None:
        self.data = data
        self.strategy_factory = strategy_factory

    def run(
        self,
        config: BacktestConfig,
        *,
        progress_callback: Callable[[BacktestProgress], None] | None = None,
    ) -> BacktestResult:
        self._emit(
            progress_callback,
            phase="PLANNING",
            message="正在解析交易日和回测尾部窗口",
            step=1,
        )
        timeline = self.data.complete_trading_dates(config.start_date)
        signal_dates = tuple(day for day in timeline if day <= config.end_date)
        if not signal_dates:
            raise ResearchDataError("回测区间内没有完整全市场交易日")

        maximum_horizon = max(
            max(config.horizons),
            config.holding_period + 1 + config.exit_retry_days,
        )
        last_signal_index = timeline.index(signal_dates[-1])
        required_end_index = last_signal_index + maximum_horizon
        if required_end_index >= len(timeline):
            raise ResearchDataError(
                f"结束日之后不足 {maximum_horizon} 个完整交易日，无法评价全部前瞻周期"
            )

        required_dates = timeline[: required_end_index + 1]
        self._emit(
            progress_callback,
            phase="PREPARING",
            message="正在审计并批量载入历史行情、生命周期和复权因子",
            step=2,
        )
        self.data.prepare_snapshots(required_dates, config.history_bars)

        def snapshot_for(day: str) -> ResearchSnapshot:
            return self.data.build_snapshot(day, config.history_bars)

        signals: list[SignalRecord] = []
        errors: list[str] = []
        coverages: list[tuple[str, float]] = []
        strategies = tuple(self.strategy_factory(key) for key in config.strategy_keys)
        for date_index, signal_date in enumerate(signal_dates, 1):
            snapshot = snapshot_for(signal_date)
            if snapshot.as_of_date != signal_date:
                raise ResearchDataError(
                    f"历史快照日期漂移：请求 {signal_date}，实际 {snapshot.as_of_date}"
                )
            coverages.append((signal_date, snapshot.coverage))
            for strategy in strategies:
                result = strategy.run(snapshot)
                candidate_scores = dict(result.candidate_scores)
                errors.extend(
                    f"{signal_date}:{result.strategy_key}:{message}"
                    for message in result.calculation_errors
                )
                for instrument_id in result.selected_instruments:
                    bars = snapshot.bars.get(instrument_id)
                    if not bars or bars[-1].trade_date != signal_date:
                        raise ResearchDataError(
                            f"信号证券 {instrument_id} 缺少研究日 {signal_date} 行情"
                        )
                    signals.append(
                        SignalRecord(
                            strategy_key=result.strategy_key,
                            as_of_date=signal_date,
                            instrument_id=instrument_id,
                            signal_close=bars[-1].close,
                            rule_basis=result.rule_basis,
                            risk_note=result.risk_note,
                            snapshot_coverage=snapshot.coverage,
                            candidate_score=candidate_scores.get(instrument_id),
                        )
                    )
            self._emit(
                progress_callback,
                phase="SIGNALS",
                message=f"逐日运行策略：{signal_date} · 累计信号 {len(signals):,}",
                step=3,
                completed=date_index,
                total=len(signal_dates),
                signals=len(signals),
            )

        date_indexes = {day: index for index, day in enumerate(required_dates)}
        outcomes: list[ForwardOutcome] = []
        pending_outcomes: dict[str, list[tuple[SignalRecord, int]]] = {}
        for signal in signals:
            signal_index = date_indexes[signal.as_of_date]
            for horizon in config.horizons:
                evaluation_date = required_dates[signal_index + horizon]
                pending_outcomes.setdefault(evaluation_date, []).append((signal, horizon))
        evaluation_dates = sorted(pending_outcomes)
        for date_index, evaluation_date in enumerate(evaluation_dates, 1):
            future_snapshot = snapshot_for(evaluation_date)
            for signal, horizon in pending_outcomes[evaluation_date]:
                future_bars = future_snapshot.bars.get(signal.instrument_id)
                if not future_bars or future_bars[-1].trade_date != evaluation_date:
                    outcomes.append(
                        ForwardOutcome(
                            strategy_key=signal.strategy_key,
                            signal_date=signal.as_of_date,
                            instrument_id=signal.instrument_id,
                            horizon=horizon,
                            evaluation_date=evaluation_date,
                            signal_close=signal.signal_close,
                            evaluation_close=None,
                            return_rate=None,
                            exclusion_reason="NO_FORWARD_BAR",
                        )
                    )
                    continue
                evaluation_close = future_bars[-1].close
                outcomes.append(
                    ForwardOutcome(
                        strategy_key=signal.strategy_key,
                        signal_date=signal.as_of_date,
                        instrument_id=signal.instrument_id,
                        horizon=horizon,
                        evaluation_date=evaluation_date,
                        signal_close=signal.signal_close,
                        evaluation_close=evaluation_close,
                        return_rate=evaluation_close / signal.signal_close - 1,
                    )
                )
            self._emit(
                progress_callback,
                phase="OUTCOMES",
                message=f"评价前瞻收益：{evaluation_date} · 累计结果 {len(outcomes):,}",
                step=4,
                completed=date_index,
                total=len(evaluation_dates),
                signals=len(signals),
                outcomes=len(outcomes),
            )

        portfolios = PortfolioSimulator().run(
            config=config,
            dates=required_dates,
            signal_dates=signal_dates,
            signals=tuple(signals),
            snapshot_source=snapshot_for,
            progress_callback=lambda current, total, day: self._emit(
                progress_callback,
                phase="PORTFOLIO",
                message=f"模拟账户成交与每日权益：{day}",
                step=5,
                completed=current,
                total=total,
                signals=len(signals),
                outcomes=len(outcomes),
            ),
        )
        benchmarks = tuple(
            self._benchmark_result(instrument_id, required_dates)
            for instrument_id in config.benchmark_ids
        )

        return BacktestResult(
            config=config,
            signal_dates=signal_dates,
            signals=tuple(signals),
            outcomes=tuple(outcomes),
            snapshot_coverages=tuple(coverages),
            calculation_errors=tuple(errors),
            portfolios=portfolios,
            benchmarks=benchmarks,
        )

    def _benchmark_result(self, instrument_id: str, dates: tuple[str, ...]) -> BenchmarkResult:
        closes = self.data.benchmark_closes(instrument_id, dates)
        actual_dates = tuple(day for day, _close in closes)
        if actual_dates != dates:
            missing = next((day for day in dates if day not in set(actual_dates)), dates[0])
            raise ResearchDataError(f"基准 {instrument_id} 缺少完整交易日 {missing} 行情")
        values = [close for _day, close in closes]
        if not values or values[0] <= 0:
            raise ResearchDataError(f"基准 {instrument_id} 没有有效行情")
        total_return = values[-1] / values[0] - 1
        periods = len(values) - 1
        annualized_return = (
            (1 + total_return) ** (252 / periods) - 1 if periods and 1 + total_return > 0 else None
        )
        peak = 0.0
        max_drawdown = 0.0
        for value in values:
            peak = max(peak, value)
            if peak > 0:
                max_drawdown = max(max_drawdown, 1 - value / peak)
        grouped: dict[int, list[float]] = {}
        grouped_dates: dict[int, list[str]] = {}
        for day, close in closes:
            year = int(day[:4])
            grouped.setdefault(year, []).append(close)
            grouped_dates.setdefault(year, []).append(day)
        annual_returns_list = []
        opening_value = values[0]
        for year in sorted(grouped):
            annual_returns_list.append(
                AnnualReturn(
                    year=year,
                    return_rate=grouped[year][-1] / opening_value - 1,
                    is_partial=not (
                        grouped_dates[year][0][5:7] == "01" and grouped_dates[year][-1][5:7] == "12"
                    ),
                )
            )
            opening_value = grouped[year][-1]
        annual_returns = tuple(annual_returns_list)
        return BenchmarkResult(
            instrument_id=instrument_id,
            name={
                "SSE:000300": "沪深300",
                "SSE:000985": "中证全指",
            }.get(instrument_id, instrument_id),
            start_date=closes[0][0],
            end_date=closes[-1][0],
            total_return=total_return,
            annualized_return=annualized_return,
            max_drawdown=max_drawdown,
            annual_returns=annual_returns,
        )

    @staticmethod
    def _emit(
        callback: Callable[[BacktestProgress], None] | None,
        *,
        phase: str,
        message: str,
        step: int,
        completed: int = 0,
        total: int = 1,
        signals: int = 0,
        outcomes: int = 0,
    ) -> None:
        if callback:
            callback(
                BacktestProgress(
                    phase=phase,
                    message=message,
                    step=step,
                    completed=completed,
                    total=total,
                    signals=signals,
                    outcomes=outcomes,
                )
            )
