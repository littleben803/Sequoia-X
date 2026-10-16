"""V3 日频信号回测内核。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from sequoia_x.backtest.models import (
    BacktestConfig,
    BacktestResult,
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

    def run(self, config: BacktestConfig) -> BacktestResult:
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
        self.data.prepare_snapshots(required_dates, config.history_bars)
        def snapshot_for(day: str) -> ResearchSnapshot:
            return self.data.build_snapshot(day, config.history_bars)

        signals: list[SignalRecord] = []
        errors: list[str] = []
        coverages: list[tuple[str, float]] = []
        strategies = tuple(self.strategy_factory(key) for key in config.strategy_keys)
        for signal_date in signal_dates:
            snapshot = snapshot_for(signal_date)
            if snapshot.as_of_date != signal_date:
                raise ResearchDataError(
                    f"历史快照日期漂移：请求 {signal_date}，实际 {snapshot.as_of_date}"
                )
            coverages.append((signal_date, snapshot.coverage))
            for strategy in strategies:
                result = strategy.run(snapshot)
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
                        )
                    )

        date_indexes = {day: index for index, day in enumerate(required_dates)}
        outcomes: list[ForwardOutcome] = []
        pending_outcomes: dict[str, list[tuple[SignalRecord, int]]] = {}
        for signal in signals:
            signal_index = date_indexes[signal.as_of_date]
            for horizon in config.horizons:
                evaluation_date = required_dates[signal_index + horizon]
                pending_outcomes.setdefault(evaluation_date, []).append((signal, horizon))
        for evaluation_date in sorted(pending_outcomes):
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

        portfolios = PortfolioSimulator().run(
            config=config,
            dates=required_dates,
            signal_dates=signal_dates,
            signals=tuple(signals),
            snapshot_source=snapshot_for,
        )

        return BacktestResult(
            config=config,
            signal_dates=signal_dates,
            signals=tuple(signals),
            outcomes=tuple(outcomes),
            snapshot_coverages=tuple(coverages),
            calculation_errors=tuple(errors),
            portfolios=portfolios,
        )
