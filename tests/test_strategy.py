"""V3 策略纯计算契约和现有公式回归测试。"""

import inspect
from datetime import date, timedelta

from sequoia_x.data.models import V3AuditReport
from sequoia_x.data.research import ResearchBar, ResearchSnapshot
from sequoia_x.strategy import (
    DEFAULT_STRATEGY_KEYS,
    EXPERIMENTAL_STRATEGY_KEYS,
    STRATEGY_TYPES,
    create_strategy,
)
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy


def _bars(
    closes: list[float],
    *,
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
    amounts: list[float] | None = None,
    instrument_id: str = "SSE:600001",
) -> tuple[ResearchBar, ...]:
    count = len(closes)
    opens = opens or closes
    highs = highs or [value * 1.01 for value in closes]
    lows = lows or [value * 0.99 for value in closes]
    volumes = volumes or [100.0] * count
    amounts = amounts or [1_000_000.0] * count
    start = date(2026, 1, 1)
    return tuple(
        ResearchBar(
            instrument_id=instrument_id,
            trade_date=(start + timedelta(days=index)).isoformat(),
            open=opens[index],
            high=highs[index],
            low=lows[index],
            close=close,
            previous_close=closes[index - 1] if index else None,
            volume=volumes[index],
            amount=amounts[index],
            adjustment_factor=1.0,
        )
        for index, close in enumerate(closes)
    )


def _snapshot(bars: dict[str, tuple[ResearchBar, ...]]) -> ResearchSnapshot:
    audit = V3AuditReport(
        ready_for_a_share_research=True,
        latest_complete_date="2026-10-13",
        instruments=len(bars),
        raw_bars=sum(len(value) for value in bars.values()),
        batches=1,
        complete_batches=1,
        degraded_batches=0,
        failed_batches=0,
        unexplained_a_share_gaps=0,
        invalid_rows=0,
        problems=(),
    )
    return ResearchSnapshot(
        as_of_date="2026-10-13",
        bars=bars,
        exclusions=(),
        requested_instruments=len(bars),
        eligible_instruments=len(bars),
        coverage=1.0,
        audit=audit,
    )


def test_ma_volume_preserves_golden_cross_and_volume_formula() -> None:
    closes = [10.0] * 16 + [8.0] * 4 + [20.0]
    volumes = [100.0] * 20 + [1_000.0]
    snapshot = _snapshot({"SSE:600001": _bars(closes, volumes=volumes)})

    result = MaVolumeStrategy().run(snapshot)

    assert result.selected_instruments == ("SSE:600001",)
    assert result.as_of_date == snapshot.as_of_date


def test_turtle_preserves_breakout_liquidity_and_candle_formula() -> None:
    closes = [10.0] * 20 + [12.0]
    snapshot = _snapshot(
        {
            "SSE:600001": _bars(
                closes,
                opens=[10.0] * 20 + [11.0],
                highs=[10.5] * 20 + [12.2],
                lows=[9.5] * 20 + [10.8],
                amounts=[1_000_000.0] * 20 + [200_000_000.0],
            )
        }
    )

    assert TurtleTradeStrategy().run(snapshot).selected_instruments == ("SSE:600001",)


def test_high_tight_flag_preserves_momentum_consolidation_and_shrink_formula() -> None:
    closes = [15.0] * 30 + [18.5] * 10
    snapshot = _snapshot(
        {
            "SSE:600001": _bars(
                closes,
                highs=[20.0] + [19.0] * 39,
                lows=[10.0] + [18.0] * 39,
                volumes=[100.0] * 39 + [10.0],
            )
        }
    )

    assert HighTightFlagStrategy().run(snapshot).selected_instruments == ("SSE:600001",)


def test_rps_uses_same_snapshot_cross_section_and_120_day_formula() -> None:
    weak_id = "SSE:600001"
    strong_id = "SZSE:000001"
    weak = _bars([10.0] * 121, instrument_id=weak_id)
    strong_closes = [10.0] * 120 + [20.0]
    strong = _bars(
        strong_closes,
        highs=[10.1] * 120 + [20.0],
        instrument_id=strong_id,
    )
    snapshot = _snapshot({weak_id: weak, strong_id: strong})

    result = RpsBreakoutStrategy().run(snapshot)

    assert result.selected_instruments == (strong_id,)


def test_shakeout_is_migrated_but_remains_marked_unvalidated() -> None:
    snapshot = _snapshot(
        {
            "SSE:600001": _bars(
                [10.0, 11.0, 11.2],
                opens=[10.0, 10.5, 11.5],
                highs=[10.2, 11.2, 11.6],
                lows=[9.8, 10.4, 11.0],
                volumes=[100.0, 100.0, 250.0],
            )
        }
    )

    result = LimitUpShakeoutStrategy().run(snapshot)

    assert result.selected_instruments == ("SSE:600001",)
    assert result.risk_note is not None
    assert "默认禁用" in result.risk_note


def test_uptrend_limit_down_is_migrated_but_remains_marked_unvalidated() -> None:
    closes = [float(value) for value in range(1, 61)] + [54.0]
    volumes = [100.0] * 60 + [10_000.0]
    snapshot = _snapshot({"SSE:600001": _bars(closes, volumes=volumes)})

    result = UptrendLimitDownStrategy().run(snapshot)

    assert result.selected_instruments == ("SSE:600001",)
    assert result.risk_note is not None
    assert "默认禁用" in result.risk_note


def test_all_migrated_strategies_are_pure_and_do_not_mutate_snapshot() -> None:
    snapshot = _snapshot({"SSE:600001": _bars([10.0] * 121)})
    original = snapshot.bars["SSE:600001"]
    strategies = (
        MaVolumeStrategy(),
        TurtleTradeStrategy(),
        HighTightFlagStrategy(),
        RpsBreakoutStrategy(),
        LimitUpShakeoutStrategy(),
        UptrendLimitDownStrategy(),
    )

    results = tuple(strategy.run(snapshot) for strategy in strategies)

    assert snapshot.bars["SSE:600001"] == original
    assert all(result.input_instruments == 1 for result in results)
    assert all(result.eligible_instruments == 1 for result in results)
    assert all(result.calculation_errors == () for result in results)


def test_registry_keeps_high_risk_strategies_out_of_defaults() -> None:
    assert DEFAULT_STRATEGY_KEYS == {"ma_volume", "turtle", "flag", "rps"}
    assert EXPERIMENTAL_STRATEGY_KEYS == {"shakeout", "limit_down"}
    assert set(STRATEGY_TYPES) == DEFAULT_STRATEGY_KEYS | EXPERIMENTAL_STRATEGY_KEYS
    assert create_strategy("shakeout").risk_note is not None


def test_migrated_strategies_have_no_database_or_network_dependencies() -> None:
    forbidden = ("self.engine", "sqlite3", "baostock", "akshare", "requests")
    for strategy_type in STRATEGY_TYPES.values():
        source = inspect.getsource(strategy_type)
        assert all(token not in source for token in forbidden)
