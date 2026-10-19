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


def _ma_volume_closes() -> list[float]:
    warmup = [7.0 + index * (2.9 / 59) for index in range(60)]
    signal_window = [
        10.0,
        9.9219,
        9.9563,
        10.0368,
        10.1094,
        10.0917,
        10.0934,
        10.1161,
        10.2208,
        10.2477,
        10.2354,
        10.2527,
        10.1285,
        10.01,
        10.0911,
        10.2576,
        10.3068,
        10.2945,
        10.2133,
        10.2344,
        10.4029,
        10.5081,
        10.5416,
        10.6766,
        10.6122,
        10.6378,
        10.8035,
        10.8503,
        10.8587,
        10.8053,
        10.8424,
        11.0128,
        10.8715,
        10.9858,
        11.1134,
        11.2644,
        11.3682,
        11.4963,
        11.5258,
        11.57,
        11.5675,
        11.4366,
        11.5864,
        11.6339,
        11.5524,
        11.5772,
        11.5951,
        11.5685,
        11.5382,
        11.5746,
        11.6406,
        11.6683,
        11.6419,
        11.4654,
        11.361,
        11.2396,
        11.2568,
        11.3675,
        11.4579,
        11.5486,
        12.2192,
    ]
    return warmup + signal_window


def _ma_volume_bars(
    *,
    instrument_id: str,
    current_volume: float = 200.0,
    average_amount: float = 100_000_000.0,
) -> tuple[ResearchBar, ...]:
    closes = _ma_volume_closes()
    opens = closes.copy()
    opens[-1] = closes[-2]
    return _bars(
        closes,
        opens=opens,
        volumes=[100.0] * 120 + [current_volume],
        amounts=[average_amount] * 121,
        instrument_id=instrument_id,
    )


def test_ma_volume_requires_trend_liquidity_and_independent_volume_baseline() -> None:
    qualified = "SZSE:000001"
    low_liquidity = "SSE:600001"
    snapshot = _snapshot(
        {
            qualified: _ma_volume_bars(instrument_id=qualified),
            low_liquidity: _ma_volume_bars(
                instrument_id=low_liquidity,
                average_amount=49_999_999.0,
            ),
        }
    )

    result = MaVolumeStrategy().run(snapshot)

    assert result.selected_instruments == (qualified,)
    assert result.as_of_date == snapshot.as_of_date
    assert 0 <= dict(result.candidate_scores)[qualified] <= 100


def test_ma_volume_ranks_stronger_candidate_before_instrument_code() -> None:
    weaker = "SSE:600001"
    stronger = "SZSE:000001"
    snapshot = _snapshot(
        {
            weaker: _ma_volume_bars(instrument_id=weaker, current_volume=180.0),
            stronger: _ma_volume_bars(instrument_id=stronger, current_volume=300.0),
        }
    )

    result = MaVolumeStrategy().run(snapshot)

    assert result.selected_instruments == (stronger, weaker)
    assert result.candidate_scores[0][1] > result.candidate_scores[1][1]


def test_ma_volume_rejects_single_candle_abnormality() -> None:
    instrument_id = "SSE:600001"
    normal = list(_ma_volume_bars(instrument_id=instrument_id))
    current = normal[-1]
    normal[-1] = ResearchBar(
        instrument_id=current.instrument_id,
        trade_date=current.trade_date,
        open=current.open,
        high=current.high * 1.08,
        low=current.low * 0.96,
        close=current.close,
        previous_close=current.previous_close,
        volume=current.volume,
        amount=current.amount,
        adjustment_factor=current.adjustment_factor,
    )

    result = MaVolumeStrategy().run(_snapshot({instrument_id: tuple(normal)}))

    assert result.selected_instruments == ()


def test_ma_volume_requires_ma60_to_be_rising() -> None:
    instrument_id = "SSE:600001"
    bars = list(_ma_volume_bars(instrument_id=instrument_id))
    for index in range(51, 61):
        bar = bars[index]
        price = 18.0
        bars[index] = ResearchBar(
            instrument_id=bar.instrument_id,
            trade_date=bar.trade_date,
            open=price,
            high=price * 1.01,
            low=price * 0.99,
            close=price,
            previous_close=bars[index - 1].close if index else None,
            volume=bar.volume,
            amount=bar.amount,
            adjustment_factor=bar.adjustment_factor,
        )

    result = MaVolumeStrategy().run(_snapshot({instrument_id: tuple(bars)}))

    assert result.selected_instruments == ()


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
