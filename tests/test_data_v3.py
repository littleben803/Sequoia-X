"""V3 Provider 契约、原始行情存储与审计测试。"""

import sqlite3
from pathlib import Path

from sequoia_x.data.database import MarketDataRepository
from sequoia_x.data.models import (
    Adjustment,
    DailyBar,
    DailySnapshotResult,
    DailyStatus,
    Exchange,
    InstrumentRecord,
    SecurityType,
    TradeCalendarDay,
    TradingStatusRecord,
)


def _instrument(code: str, kind: SecurityType) -> InstrumentRecord:
    return InstrumentRecord(
        exchange=Exchange.SSE,
        security_code=code,
        name=f"测试{code}",
        security_type=kind,
        source="test-provider",
        raw_payload={"original": True},
    )


def _bar(instrument: InstrumentRecord, provider: str = "test-provider") -> DailyBar:
    return DailyBar(
        instrument_id=instrument.instrument_id,
        trade_date="2026-10-13",
        open=10,
        high=11,
        low=9,
        close=10.5,
        previous_close=9.8,
        volume=1_000,
        amount=10_500,
        provider=provider,
        adjustment=Adjustment.RAW,
        raw_payload={"untouched": "payload"},
    )


def test_snapshot_keeps_all_security_types_and_raw_payload(tmp_path: Path) -> None:
    repository = MarketDataRepository(str(tmp_path / "v3.db"))
    stock = _instrument("600001", SecurityType.STOCK)
    etf = _instrument("510300", SecurityType.ETF)
    bond = _instrument("110001", SecurityType.BOND)
    snapshot = DailySnapshotResult(
        trade_date="2026-10-13",
        provider="test-provider",
        source_url="https://example.test/package.zip",
        bars=tuple(_bar(item) for item in (stock, etf, bond)),
        instruments=(stock, etf, bond),
        checksum="abc",
    )
    batch_id = repository.start_batch("test-provider", "DAILY_SNAPSHOT", "2026-10-13")

    repository.save_snapshot(batch_id, snapshot)

    with repository.connect() as conn:
        kinds = dict(
            conn.execute(
                "SELECT security_type, COUNT(*) FROM instruments GROUP BY security_type"
            ).fetchall()
        )
        payloads = [
            row[0]
            for row in conn.execute(
                "SELECT raw_payload_json FROM daily_bars_raw ORDER BY instrument_id"
            )
        ]
    assert kinds == {"BOND": 1, "ETF": 1, "STOCK": 1}
    assert len(payloads) == 3
    assert all('"untouched": "payload"' in payload for payload in payloads)


def test_snapshot_reports_throttled_instrument_and_bar_write_progress(tmp_path: Path) -> None:
    repository = MarketDataRepository(str(tmp_path / "v3.db"))
    stock = _instrument("600001", SecurityType.STOCK)
    etf = _instrument("510300", SecurityType.ETF)
    snapshot = DailySnapshotResult(
        trade_date="2026-10-13",
        provider="test-provider",
        source_url="https://example.test/package.zip",
        bars=(_bar(stock), _bar(etf)),
        instruments=(stock, etf),
        checksum="abc",
    )
    batch_id = repository.start_batch("test-provider", "DAILY_SNAPSHOT", "2026-10-13")
    progress = []

    repository.save_snapshot(
        batch_id, snapshot, progress_callback=lambda *args: progress.append(args)
    )

    assert ("INSTRUMENTS", 1, 2, stock.instrument_id) in progress
    assert ("INSTRUMENTS", 2, 2, etf.instrument_id) in progress
    assert ("DAILY_BARS", 1, 2, stock.instrument_id) in progress
    assert ("DAILY_BARS", 2, 2, etf.instrument_id) in progress


def test_same_bar_from_different_providers_is_not_overwritten(tmp_path: Path) -> None:
    repository = MarketDataRepository(str(tmp_path / "v3.db"))
    stock = _instrument("600001", SecurityType.STOCK)
    for provider in ("primary", "fallback"):
        snapshot = DailySnapshotResult(
            trade_date="2026-10-13",
            provider=provider,
            source_url=f"https://example.test/{provider}",
            bars=(_bar(stock, provider),),
            instruments=(stock,),
            checksum=provider,
        )
        batch = repository.start_batch(provider, "DAILY_SNAPSHOT", "2026-10-13")
        repository.save_snapshot(batch, snapshot)

    with repository.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_bars_raw").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM daily_bars_canonical").fetchone()[0] == 1


def test_calendar_persists_closed_days_but_plans_only_open_days(tmp_path: Path) -> None:
    repository = MarketDataRepository(str(tmp_path / "v3.db"))
    repository.save_calendar(
        (
            TradeCalendarDay("2026-10-11", False, provider="szse"),
            TradeCalendarDay("2026-10-12", True, provider="szse"),
        )
    )

    assert repository.trading_dates("2026-10-11", "2026-10-12") == ("2026-10-12",)


def test_v3_schema_does_not_create_v2_stock_daily_table(tmp_path: Path) -> None:
    path = tmp_path / "v3.db"
    MarketDataRepository(str(path))
    with sqlite3.connect(path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "daily_bars_raw" in tables
    assert "stock_daily" not in tables


def test_zero_trade_stock_is_raw_evidence_not_canonical_or_invalid(tmp_path: Path) -> None:
    repository = MarketDataRepository(str(tmp_path / "v3.db"))
    stock = _instrument("600001", SecurityType.STOCK)
    bar = DailyBar(
        instrument_id=stock.instrument_id,
        trade_date="2026-10-13",
        open=0,
        high=0,
        low=0,
        close=10.5,
        previous_close=10.5,
        volume=0,
        amount=0,
        provider="test-provider",
        raw_payload={"zero_trade": True},
    )
    snapshot = DailySnapshotResult(
        trade_date="2026-10-13",
        provider="test-provider",
        source_url="https://example.test/package.zip",
        bars=(bar,),
        instruments=(stock,),
        checksum="abc",
    )
    batch = repository.start_batch("test-provider", "DAILY_SNAPSHOT", "2026-10-13")

    repository.save_snapshot(batch, snapshot)

    with repository.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM daily_bars_raw").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM daily_bars_canonical").fetchone()[0] == 0
        assert (
            conn.execute("SELECT status FROM instrument_daily_status").fetchone()[0]
            == "NO_TRADE_OBSERVED"
        )
    assert repository.audit().invalid_rows == 0


def test_confirmed_suspension_resolves_no_trade_gap(tmp_path: Path) -> None:
    repository = MarketDataRepository(str(tmp_path / "v3.db"))
    stock = _instrument("600001", SecurityType.STOCK)
    repository.upsert_instruments((stock,), "2026-10-13")
    repository.save_daily_status(
        (
            TradingStatusRecord(
                stock.instrument_id,
                "2026-10-13",
                DailyStatus.NO_TRADE_OBSERVED,
                "primary",
            ),
            TradingStatusRecord(
                stock.instrument_id,
                "2026-10-13",
                DailyStatus.SUSPENDED_CONFIRMED,
                "fallback",
            ),
        )
    )

    assert repository.audit().unexplained_a_share_gaps == 0


def test_quality_repair_maps_bse_code_migration_and_closes_lifecycle_gaps(
    tmp_path: Path,
) -> None:
    repository = MarketDataRepository(str(tmp_path / "v3.db"))
    old_bse = InstrumentRecord(
        Exchange.BSE,
        "430017",
        "星昊医药",
        SecurityType.STOCK,
        source="a-stock-data",
    )
    new_bse = InstrumentRecord(
        Exchange.BSE,
        "920017",
        "星昊医药",
        SecurityType.STOCK,
        list_date="2023-05-31",
        listing_status="LISTED",
        source="bse-official",
    )
    delisted = InstrumentRecord(
        Exchange.SSE,
        "600290",
        "退市测试",
        SecurityType.STOCK,
        source="a-stock-data",
    )
    bse_bond = InstrumentRecord(
        Exchange.BSE,
        "821001",
        "24京资01",
        SecurityType.STOCK,
        source="a-stock-data",
    )
    repository.save_calendar(
        tuple(
            TradeCalendarDay(day, True, provider="test")
            for day in ("2025-09-30", "2025-10-09", "2025-10-10")
        )
    )
    for trade_date, instruments in (
        ("2025-09-30", (old_bse, delisted, bse_bond)),
        ("2025-10-09", (new_bse,)),
        ("2025-10-10", ()),
    ):
        bars = tuple(
            DailyBar(
                instrument_id=item.instrument_id,
                trade_date=trade_date,
                open=10,
                high=11,
                low=9,
                close=10,
                previous_close=10,
                volume=100,
                amount=1_000,
                provider="a-stock-data",
            )
            for item in instruments
        )
        snapshot = DailySnapshotResult(
            trade_date,
            "a-stock-data",
            "test://package",
            bars,
            instruments,
            "checksum",
        )
        batch = repository.start_batch("a-stock-data", "DAILY_SNAPSHOT", trade_date)
        repository.save_snapshot(batch, snapshot)
    repository.upsert_instruments((new_bse,), "2025-10-10")

    report = repository.rebuild_data_quality()

    assert report.code_migrations == 1
    assert report.unexplained_gaps_after_repair == 0
    with repository.connect() as conn:
        alias = conn.execute(
            "SELECT canonical_instrument_id FROM instrument_code_aliases "
            "WHERE alias_instrument_id='BSE:430017'"
        ).fetchone()[0]
        research_ids = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT research_instrument_id FROM daily_bars_research "
                "WHERE source_instrument_id IN ('BSE:430017','BSE:920017')"
            )
        }
        old_status, old_end = conn.execute(
            "SELECT listing_status, code_valid_to FROM instruments WHERE instrument_id='BSE:430017'"
        ).fetchone()
        delisted_status, delisted_end = conn.execute(
            "SELECT listing_status, code_valid_to FROM instruments WHERE instrument_id='SSE:600290'"
        ).fetchone()
        bond_type = conn.execute(
            "SELECT security_type FROM instruments WHERE instrument_id='BSE:821001'"
        ).fetchone()[0]
    assert alias == "BSE:920017"
    assert research_ids == {"BSE:920017"}
    assert (old_status, old_end) == ("CODE_MIGRATED", "2025-09-30")
    assert (delisted_status, delisted_end) == ("DELISTED", "2025-09-30")
    assert bond_type == "BOND"

    repository.save_daily_status(
        (
            TradingStatusRecord(
                delisted.instrument_id,
                "2025-09-30",
                DailyStatus.MISSING_UNEXPLAINED,
                "range-isolation-test",
            ),
        )
    )
    repository.rebuild_data_quality("2025-10-09", "2025-10-10")
    with repository.connect() as conn:
        preserved = conn.execute(
            "SELECT COUNT(*) FROM instrument_daily_status "
            "WHERE trade_date='2025-09-30' AND status_source='range-isolation-test'"
        ).fetchone()[0]
    assert preserved == 1
