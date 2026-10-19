"""V3 回测信号内核测试。"""

from dataclasses import replace
from datetime import date, timedelta
from types import MappingProxyType

import pytest

from sequoia_x.backtest.engine import BacktestEngine
from sequoia_x.backtest.models import (
    BacktestConfig,
    EquityPoint,
    OrderRecord,
    SignalRecord,
)
from sequoia_x.backtest.portfolio import PortfolioSimulator
from sequoia_x.data.models import V3AuditReport
from sequoia_x.data.research import ResearchBar, ResearchSnapshot
from sequoia_x.strategy.base import BaseStrategy


def _audit(latest: str) -> V3AuditReport:
    return V3AuditReport(
        ready_for_a_share_research=True,
        latest_complete_date=latest,
        instruments=1,
        raw_bars=30,
        batches=30,
        complete_batches=30,
        degraded_batches=0,
        failed_batches=0,
        unexplained_a_share_gaps=0,
        invalid_rows=0,
        problems=(),
    )


def _snapshot(day: str, close: float) -> ResearchSnapshot:
    bar = ResearchBar(
        instrument_id="SSE:600001",
        trade_date=day,
        open=close,
        high=close,
        low=close,
        close=close,
        previous_close=None,
        volume=100.0,
        amount=1_000_000.0,
        adjustment_factor=1.0,
    )
    return ResearchSnapshot(
        as_of_date=day,
        bars=MappingProxyType({"SSE:600001": (bar,)}),
        exclusions=(),
        requested_instruments=1,
        eligible_instruments=1,
        coverage=1.0,
        audit=_audit(day),
    )


class _FakeDataService:
    def __init__(self, snapshots: dict[str, ResearchSnapshot]) -> None:
        self.snapshots = snapshots
        self.requested_dates: list[str] = []

    def complete_trading_dates(self, start_date: str) -> tuple[str, ...]:
        return tuple(day for day in sorted(self.snapshots) if day >= start_date)

    def prepare_snapshots(self, dates: tuple[str, ...], history_bars: int) -> None:
        assert dates
        assert history_bars >= 121

    def build_snapshot(self, as_of_date: str, history_bars: int) -> ResearchSnapshot:
        self.requested_dates.append(as_of_date)
        snapshot = self.snapshots[as_of_date]
        assert all(bar.trade_date <= as_of_date for bars in snapshot.bars.values() for bar in bars)
        return snapshot

    def benchmark_closes(
        self, instrument_id: str, dates: tuple[str, ...]
    ) -> tuple[tuple[str, float], ...]:
        assert instrument_id in {"SSE:000300", "SSE:000985"}
        base = 100.0 if instrument_id == "SSE:000300" else 200.0
        return tuple((day, base + index) for index, day in enumerate(dates))


class _SelectEveryDayStrategy(BaseStrategy):
    strategy_key = "test"
    rule_basis = ("测试信号",)

    def run(self, snapshot: ResearchSnapshot):
        return self._result(snapshot, list(snapshot.bars))


def test_backtest_uses_only_signal_date_snapshot_and_computes_forward_returns() -> None:
    snapshots = {
        "2026-01-05": _snapshot("2026-01-05", 10.0),
        "2026-01-06": _snapshot("2026-01-06", 11.0),
        "2026-01-07": _snapshot("2026-01-07", 12.0),
    }
    data = _FakeDataService(snapshots)
    config = BacktestConfig(
        start_date="2026-01-05",
        end_date="2026-01-05",
        strategy_keys=("test",),
        horizons=(1, 2),
        history_bars=121,
        holding_period=1,
        exit_retry_days=0,
    )

    events = []
    result = BacktestEngine(
        data,
        strategy_factory=lambda _key: _SelectEveryDayStrategy(),
    ).run(config, progress_callback=events.append)

    assert [item.as_of_date for item in result.signals] == ["2026-01-05"]
    assert result.signals[0].signal_close == 10.0
    assert [item.return_rate for item in result.outcomes] == pytest.approx([0.1, 0.2])
    assert result.outcomes[0].evaluation_date == "2026-01-06"
    assert sorted(set(data.requested_dates)) == [
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
    ]
    assert {event.phase for event in events} == {
        "PLANNING",
        "PREPARING",
        "SIGNALS",
        "OUTCOMES",
        "PORTFOLIO",
    }
    assert events[-1].phase == "PORTFOLIO"
    assert events[-1].completed == events[-1].total


def test_backtest_records_missing_forward_bar_instead_of_fabricating_return() -> None:
    signal_snapshot = _snapshot("2026-01-05", 10.0)
    empty_future = replace(
        _snapshot("2026-01-06", 11.0),
        bars=MappingProxyType({}),
        eligible_instruments=0,
        coverage=0.0,
    )
    data = _FakeDataService(
        {
            "2026-01-05": signal_snapshot,
            "2026-01-06": empty_future,
            "2026-01-07": _snapshot("2026-01-07", 12.0),
        }
    )
    config = BacktestConfig(
        start_date="2026-01-05",
        end_date="2026-01-05",
        strategy_keys=("test",),
        horizons=(1,),
        holding_period=1,
        exit_retry_days=0,
    )

    result = BacktestEngine(
        data,
        strategy_factory=lambda _key: _SelectEveryDayStrategy(),
    ).run(config)

    assert result.outcomes[0].return_rate is None
    assert result.outcomes[0].exclusion_reason == "NO_FORWARD_BAR"


def test_backtest_rejects_invalid_range_and_horizons() -> None:
    with pytest.raises(ValueError, match="结束日期"):
        BacktestConfig(
            start_date="2026-01-06",
            end_date="2026-01-05",
            strategy_keys=("rps",),
        )
    with pytest.raises(ValueError, match="持有期"):
        BacktestConfig(
            start_date="2026-01-05",
            end_date="2026-01-06",
            strategy_keys=("rps",),
            horizons=(0,),
        )


def test_backtest_signal_dates_are_limited_to_requested_range() -> None:
    start = date(2026, 1, 5)
    snapshots = {
        (start + timedelta(days=index)).isoformat(): _snapshot(
            (start + timedelta(days=index)).isoformat(), 10.0 + index
        )
        for index in range(5)
    }
    data = _FakeDataService(snapshots)
    config = BacktestConfig(
        start_date="2026-01-06",
        end_date="2026-01-07",
        strategy_keys=("test",),
        horizons=(1,),
        holding_period=1,
        exit_retry_days=0,
    )

    result = BacktestEngine(
        data,
        strategy_factory=lambda _key: _SelectEveryDayStrategy(),
    ).run(config)

    assert result.signal_dates == ("2026-01-06", "2026-01-07")
    assert {item.as_of_date for item in result.signals} == {"2026-01-06", "2026-01-07"}


def test_portfolio_enters_next_open_and_exits_after_fixed_holding_period() -> None:
    data = _FakeDataService(
        {
            "2026-01-05": _snapshot("2026-01-05", 10.0),
            "2026-01-06": _snapshot("2026-01-06", 11.0),
            "2026-01-07": _snapshot("2026-01-07", 12.0),
        }
    )
    config = BacktestConfig(
        start_date="2026-01-05",
        end_date="2026-01-05",
        strategy_keys=("test",),
        horizons=(1,),
        holding_period=1,
        exit_retry_days=0,
        max_positions=1,
        initial_cash=100_000.0,
    )

    result = BacktestEngine(
        data,
        strategy_factory=lambda _key: _SelectEveryDayStrategy(),
    ).run(config)

    portfolio = result.portfolios[0]
    buy, sell = (item for item in portfolio.orders if item.status == "FILLED")
    assert (buy.side, buy.trade_date, buy.quantity, buy.price) == (
        "BUY",
        "2026-01-06",
        9_000,
        11.0,
    )
    assert (sell.side, sell.trade_date, sell.quantity, sell.price) == (
        "SELL",
        "2026-01-07",
        9_000,
        12.0,
    )
    assert sell.stamp_duty == pytest.approx(54.0)
    assert portfolio.final_equity == pytest.approx(108_883.9)
    assert portfolio.open_positions == ()
    assert portfolio.closed_trades[0].net_profit == pytest.approx(8_883.9)
    assert portfolio.trade_win_rate == 1.0
    assert portfolio.average_win == pytest.approx(8_883.9)
    assert portfolio.average_loss is None
    assert portfolio.profit_loss_ratio is None
    assert portfolio.interval_turnover is not None
    assert portfolio.annualized_turnover is not None
    assert result.benchmarks[0].instrument_id == "SSE:000300"
    assert result.benchmarks[0].total_return == pytest.approx(0.02)
    assert result.benchmarks[1].instrument_id == "SSE:000985"
    assert result.benchmarks[1].total_return == pytest.approx(0.01)


def test_one_price_up_lock_rejects_buy_without_fabricating_fill() -> None:
    entry = _snapshot("2026-01-06", 11.0)
    locked_bar = replace(entry.bars["SSE:600001"][0], previous_close=10.0)
    entry = replace(
        entry,
        bars=MappingProxyType({"SSE:600001": (locked_bar,)}),
    )
    data = _FakeDataService(
        {
            "2026-01-05": _snapshot("2026-01-05", 10.0),
            "2026-01-06": entry,
            "2026-01-07": _snapshot("2026-01-07", 12.0),
        }
    )
    config = BacktestConfig(
        start_date="2026-01-05",
        end_date="2026-01-05",
        strategy_keys=("test",),
        horizons=(1,),
        holding_period=1,
        exit_retry_days=0,
    )

    result = BacktestEngine(
        data,
        strategy_factory=lambda _key: _SelectEveryDayStrategy(),
    ).run(config)

    portfolio = result.portfolios[0]
    assert portfolio.filled_buys == 0
    assert portfolio.rejected_orders == 1
    assert portfolio.orders[0].reason == "ONE_PRICE_UP_LOCK"


def test_stamp_duty_uses_official_2023_reduction_effective_date() -> None:
    assert PortfolioSimulator.stamp_duty_rate("2023-08-27") == 0.001
    assert PortfolioSimulator.stamp_duty_rate("2023-08-28") == 0.0005


def test_portfolio_metrics_use_closed_net_trades_and_downside_returns() -> None:
    orders = [
        OrderRecord(
            "test",
            "2025-01-02",
            "2025-01-01",
            "A",
            "BUY",
            "FILLED",
            None,
            100,
            10.0,
            1_000.0,
            0.0,
            0.0,
        ),
        OrderRecord(
            "test",
            "2025-01-03",
            "2025-01-01",
            "A",
            "SELL",
            "FILLED",
            None,
            100,
            11.0,
            1_100.0,
            0.0,
            0.0,
        ),
        OrderRecord(
            "test",
            "2025-01-06",
            "2025-01-05",
            "B",
            "BUY",
            "FILLED",
            None,
            100,
            10.0,
            1_000.0,
            0.0,
            0.0,
        ),
        OrderRecord(
            "test",
            "2025-01-07",
            "2025-01-05",
            "B",
            "SELL",
            "FILLED",
            None,
            100,
            9.0,
            900.0,
            0.0,
            0.0,
        ),
    ]
    curve = [
        EquityPoint("2025-01-02", 1_000.0, 0.0, 1_000.0, 0),
        EquityPoint("2025-01-03", 1_100.0, 0.0, 1_100.0, 0),
        EquityPoint("2025-01-07", 1_000.0, 0.0, 1_000.0, 0),
    ]
    config = BacktestConfig(
        start_date="2025-01-02",
        end_date="2025-01-07",
        strategy_keys=("test",),
        initial_cash=1_000.0,
        commission_rate=0.0,
        minimum_commission=0.0,
    )

    result = PortfolioSimulator()._result("test", config, 1_000.0, {}, orders, curve)

    assert result.trade_win_rate == 0.5
    assert result.average_win == pytest.approx(100.0)
    assert result.average_loss == pytest.approx(-100.0)
    assert result.profit_loss_ratio == pytest.approx(1.0)
    assert result.sortino_ratio is not None
    assert result.interval_turnover == pytest.approx(4_000 / (3_100 / 3))
    assert result.annual_returns[0].return_rate == pytest.approx(0.0)
    assert result.annual_returns[0].is_partial is True


def test_portfolio_prefers_higher_candidate_score_when_capacity_is_limited() -> None:
    stronger = "SZSE:000001"
    snapshots = {}
    for day, close in (
        ("2026-01-05", 10.0),
        ("2026-01-06", 11.0),
        ("2026-01-07", 12.0),
    ):
        base = _snapshot(day, close)
        weaker_bar = base.bars["SSE:600001"][0]
        stronger_bar = replace(weaker_bar, instrument_id=stronger)
        snapshots[day] = replace(
            base,
            bars=MappingProxyType({"SSE:600001": (weaker_bar,), stronger: (stronger_bar,)}),
            requested_instruments=2,
            eligible_instruments=2,
        )
    signals = tuple(
        SignalRecord(
            strategy_key="test",
            as_of_date="2026-01-05",
            instrument_id=instrument_id,
            signal_close=10.0,
            rule_basis=("测试评分",),
            risk_note=None,
            snapshot_coverage=1.0,
            candidate_score=score,
        )
        for instrument_id, score in (("SSE:600001", 10.0), (stronger, 90.0))
    )
    config = BacktestConfig(
        start_date="2026-01-05",
        end_date="2026-01-05",
        strategy_keys=("test",),
        horizons=(1,),
        holding_period=1,
        exit_retry_days=0,
        max_positions=1,
    )

    result = PortfolioSimulator().run(
        config=config,
        dates=tuple(snapshots),
        signal_dates=("2026-01-05",),
        signals=signals,
        snapshot_source=snapshots.__getitem__,
    )[0]

    filled_buy = next(order for order in result.orders if order.side == "BUY")
    assert filled_buy.instrument_id == stronger


def test_one_price_down_lock_defers_sell_then_retries_next_day() -> None:
    locked = _snapshot("2026-01-07", 12.0)
    locked_bar = replace(locked.bars["SSE:600001"][0], previous_close=13.0)
    locked = replace(
        locked,
        bars=MappingProxyType({"SSE:600001": (locked_bar,)}),
    )
    retry = _snapshot("2026-01-08", 11.0)
    retry_bar = replace(
        retry.bars["SSE:600001"][0],
        high=11.2,
        low=10.8,
        previous_close=12.0,
    )
    retry = replace(
        retry,
        bars=MappingProxyType({"SSE:600001": (retry_bar,)}),
    )
    data = _FakeDataService(
        {
            "2026-01-05": _snapshot("2026-01-05", 10.0),
            "2026-01-06": _snapshot("2026-01-06", 11.0),
            "2026-01-07": locked,
            "2026-01-08": retry,
        }
    )
    config = BacktestConfig(
        start_date="2026-01-05",
        end_date="2026-01-05",
        strategy_keys=("test",),
        horizons=(1,),
        holding_period=1,
        exit_retry_days=1,
        max_positions=1,
    )

    result = BacktestEngine(
        data,
        strategy_factory=lambda _key: _SelectEveryDayStrategy(),
    ).run(config)

    portfolio = result.portfolios[0]
    sells = [item for item in portfolio.orders if item.side == "SELL"]
    assert [(item.trade_date, item.status, item.reason) for item in sells] == [
        ("2026-01-07", "DEFERRED", "ONE_PRICE_DOWN_LOCK"),
        ("2026-01-08", "FILLED", None),
    ]
    assert portfolio.open_positions == ()
