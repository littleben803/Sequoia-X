"""V3 研究数据层：复权证据、统一数据日和质量闸门。"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from sequoia_x.data.classification import sse_security_subtype
from sequoia_x.data.database import MarketDataRepository
from sequoia_x.data.models import (
    Adjustment,
    AdjustmentFactorRecord,
    DailyBar,
    DailySnapshotResult,
    DailyStatus,
    Exchange,
    InstrumentRecord,
    SecurityType,
    TradeCalendarDay,
    TradingStatusRecord,
)
from sequoia_x.data.research import (
    AdjustmentFactorService,
    ResearchDataError,
    ResearchDataService,
)


def _stock(code: str) -> InstrumentRecord:
    return InstrumentRecord(
        exchange=Exchange.SSE,
        security_code=code,
        name=f"测试{code}",
        security_type=SecurityType.STOCK,
        source="test",
    )


def _prepare_repository(
    tmp_path: Path, stocks: tuple[InstrumentRecord, ...]
) -> MarketDataRepository:
    repository = MarketDataRepository(str(tmp_path / "research.db"))
    repository.set_metadata("instrument_directory_status", "COMPLETE")
    repository.save_calendar((TradeCalendarDay("2026-10-13", True, provider="test"),))
    snapshot = DailySnapshotResult(
        trade_date="2026-10-13",
        provider="test",
        source_url="https://example.test/daily.zip",
        bars=tuple(
            DailyBar(
                instrument_id=stock.instrument_id,
                trade_date="2026-10-13",
                open=20,
                high=22,
                low=18,
                close=21,
                previous_close=19,
                volume=1_000,
                amount=20_000,
                provider="test",
            )
            for stock in stocks
        ),
        instruments=stocks,
        checksum="checksum",
    )
    batch = repository.start_batch("test", "DAILY_SNAPSHOT", "2026-10-13")
    repository.save_snapshot(batch, snapshot)
    return repository


def _save_qfq(repository: MarketDataRepository, stock: InstrumentRecord, factor: float) -> None:
    batch = repository.start_batch("sina", "ADJUSTMENT_FACTOR", "2026-10-13")
    repository.save_adjustment_factors(
        batch,
        (
            AdjustmentFactorRecord(
                instrument_id=stock.instrument_id,
                effective_date="1900-01-01",
                factor=factor,
                kind=Adjustment.QFQ,
                provider="sina",
                raw_payload={"d": "1900-01-01", "f": factor},
            ),
        ),
    )


def test_adjustment_factor_batch_is_auditable_and_idempotent(tmp_path: Path) -> None:
    stock = _stock("600001")
    repository = _prepare_repository(tmp_path, (stock,))

    _save_qfq(repository, stock, 2.0)
    _save_qfq(repository, stock, 2.5)

    with repository.connect() as conn:
        factor, payload, source_batch = conn.execute(
            """
            SELECT factor, raw_payload_json, source_batch_id
            FROM adjustment_factors WHERE instrument_id=?
            """,
            (stock.instrument_id,),
        ).fetchone()
        batch_status = conn.execute(
            "SELECT status FROM ingestion_batches WHERE batch_id=?", (source_batch,)
        ).fetchone()[0]
    assert factor == 2.5
    assert '"f": 2.5' in payload
    assert batch_status == "COMPLETE"


def test_invalid_adjustment_factor_is_rejected_atomically(tmp_path: Path) -> None:
    stock = _stock("600001")
    repository = _prepare_repository(tmp_path, (stock,))
    batch = repository.start_batch("sina", "ADJUSTMENT_FACTOR", "2026-10-13")

    with pytest.raises(ValueError, match="有限数"):
        repository.save_adjustment_factors(
            batch,
            (
                AdjustmentFactorRecord(
                    stock.instrument_id,
                    "1900-01-01",
                    float("nan"),
                    Adjustment.QFQ,
                    "sina",
                ),
            ),
        )

    with repository.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM adjustment_factors").fetchone()[0] == 0


def test_research_snapshot_uses_one_date_and_qfq_prices(tmp_path: Path) -> None:
    stock = _stock("600001")
    repository = _prepare_repository(tmp_path, (stock,))
    _save_qfq(repository, stock, 2.0)

    snapshot = ResearchDataService(repository).build_snapshot(history_bars=120)

    assert snapshot.as_of_date == "2026-10-13"
    assert snapshot.coverage == 1.0
    bar = snapshot.bars[stock.instrument_id][0]
    assert (bar.open, bar.high, bar.low, bar.close) == (10.0, 11.0, 9.0, 10.5)
    assert bar.previous_close == 9.5
    assert bar.volume == 1_000
    assert bar.amount == 20_000
    assert bar.price_adjustment == "QFQ"


def test_missing_factor_is_excluded_and_never_treated_as_raw_price(tmp_path: Path) -> None:
    covered = _stock("600001")
    missing = _stock("600002")
    repository = _prepare_repository(tmp_path, (covered, missing))
    _save_qfq(repository, covered, 2.0)

    snapshot = ResearchDataService(repository).build_snapshot(
        history_bars=20, minimum_coverage=0.5
    )

    assert tuple(snapshot.bars) == (covered.instrument_id,)
    assert len(snapshot.exclusions) == 1
    assert snapshot.exclusions[0].instrument_id == missing.instrument_id
    assert snapshot.exclusions[0].reason == "MISSING_QFQ_FACTORS"


def test_snapshot_fails_when_coverage_is_below_gate(tmp_path: Path) -> None:
    covered = _stock("600001")
    missing = _stock("600002")
    repository = _prepare_repository(tmp_path, (covered, missing))
    _save_qfq(repository, covered, 2.0)

    with pytest.raises(ResearchDataError, match="覆盖率"):
        ResearchDataService(repository).build_snapshot(
            history_bars=20, minimum_coverage=0.95
        )


def test_factor_evidence_older_than_research_date_is_not_accepted(tmp_path: Path) -> None:
    stock = _stock("600001")
    repository = _prepare_repository(tmp_path, (stock,))
    batch = repository.start_batch("sina", "ADJUSTMENT_FACTOR", "2026-10-12")
    repository.save_adjustment_factors(
        batch,
        (
            AdjustmentFactorRecord(
                stock.instrument_id,
                "1900-01-01",
                2.0,
                Adjustment.QFQ,
                "sina",
            ),
        ),
    )

    with pytest.raises(ResearchDataError, match="覆盖率"):
        ResearchDataService(repository).build_snapshot(history_bars=20)


def test_no_bar_on_research_date_is_excluded_even_when_gap_is_explained(tmp_path: Path) -> None:
    traded = _stock("600001")
    suspended = _stock("600002")
    repository = _prepare_repository(tmp_path, (traded,))
    repository.upsert_instruments((suspended,), "2026-10-13")
    repository.save_daily_status(
        (
            TradingStatusRecord(
                suspended.instrument_id,
                "2026-10-13",
                DailyStatus.SUSPENDED_CONFIRMED,
                "test",
            ),
        )
    )
    _save_qfq(repository, traded, 2.0)
    _save_qfq(repository, suspended, 2.0)

    snapshot = ResearchDataService(repository).build_snapshot(
        history_bars=20, minimum_coverage=0.5
    )

    assert suspended.instrument_id not in snapshot.bars
    assert snapshot.exclusions[-1].reason == "NO_BAR_ON_RESEARCH_DATE"


def test_base_data_audit_blocks_research_snapshot(tmp_path: Path) -> None:
    repository = MarketDataRepository(str(tmp_path / "empty.db"))

    with pytest.raises(ResearchDataError, match="质量闸门未通过"):
        ResearchDataService(repository).build_snapshot(history_bars=20)


def test_research_audit_reports_exclusions_without_throwing(tmp_path: Path) -> None:
    covered = _stock("600001")
    missing = _stock("600002")
    repository = _prepare_repository(tmp_path, (covered, missing))
    _save_qfq(repository, covered, 2.0)

    report = ResearchDataService(repository).audit_snapshot(
        history_bars=20, minimum_coverage=0.95
    )

    assert report.ready is False
    assert report.as_of_date == "2026-10-13"
    assert report.coverage == 0.5
    assert report.exclusion_counts == {"MISSING_QFQ_FACTORS": 1}
    assert "覆盖率" in report.problems[0]


def test_factor_refresh_uses_exchange_prefix_and_records_failures(tmp_path: Path) -> None:
    sh_stock = _stock("600001")
    failed_stock = _stock("600002")
    repository = _prepare_repository(tmp_path, (sh_stock, failed_stock))
    provider = Mock()
    provider.provider_name = "sina"
    provider.fetch_adjustment_factors.side_effect = (
        ({"date": "1900-01-01", "factor": 2.0},),
        RuntimeError("源站失败"),
    )

    events = []
    report = AdjustmentFactorService(repository, provider).refresh_qfq(
        "2026-10-13", progress_callback=events.append
    )

    assert report.successful_instruments == 1
    assert report.failed_instruments == 1
    assert report.factor_rows_written == 1
    assert report.failures == ("SSE:600002: 源站失败",)
    assert provider.fetch_adjustment_factors.call_args_list[0].args == ("sh600001", "qfq")
    assert events[0].phase == "PREPARING"
    assert events[0].requested_instruments == 2
    assert events[-1].phase == "COMPLETE"
    assert events[-1].processed_instruments == 2
    assert any(event.current_name == "测试600001" for event in events)


def test_sse_stock_type_8_is_classified_as_cdr() -> None:
    assert sse_security_subtype("8") == "CDR"
    assert sse_security_subtype("1") is None


def test_factor_refresh_skips_cdr_and_excludes_it_from_research_universe(
    tmp_path: Path,
) -> None:
    ordinary = _stock("688981")
    cdr = InstrumentRecord(
        exchange=Exchange.SSE,
        security_code="689009",
        name="九号公司",
        security_type=SecurityType.STOCK,
        source="sse-official",
        raw_payload={"STOCK_TYPE": "8"},
    )
    bse = InstrumentRecord(
        exchange=Exchange.BSE,
        security_code="920001",
        name="北交测试",
        security_type=SecurityType.STOCK,
        source="bse-official",
    )
    repository = _prepare_repository(tmp_path, (ordinary, cdr, bse))
    provider = Mock(provider_name="sina")
    provider.fetch_adjustment_factors.return_value = (
        {"date": "1900-01-01", "factor": 1.0},
    )

    report = AdjustmentFactorService(repository, provider).refresh_qfq("2026-10-13")

    assert report.successful_instruments == 1
    assert report.failed_instruments == 0
    assert report.unsupported_instruments == 2
    assert report.failures == (
        "BSE:920001: UNSUPPORTED_FACTOR_SOURCE",
        "SSE:689009: UNSUPPORTED_FACTOR_SOURCE",
    )
    provider.fetch_adjustment_factors.assert_called_once_with("sh688981", "qfq")
    assert ResearchDataService(repository).research.listed_a_share_ids("2026-10-13") == (
        "SSE:688981",
    )


def test_factor_refresh_interrupt_marks_current_batch_failed(tmp_path: Path) -> None:
    stock = _stock("600001")
    repository = _prepare_repository(tmp_path, (stock,))
    provider = Mock(provider_name="sina")
    provider.fetch_adjustment_factors.side_effect = KeyboardInterrupt
    events = []

    with pytest.raises(KeyboardInterrupt):
        AdjustmentFactorService(repository, provider).refresh_qfq(
            "2026-10-13", progress_callback=events.append
        )

    with repository.connect() as conn:
        status, error_code = conn.execute(
            """
            SELECT status, error_code FROM ingestion_batches
            WHERE capability='ADJUSTMENT_FACTOR' ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
    assert status == "FAILED"
    assert error_code == "INTERRUPTED"
    assert events[-1].phase == "INTERRUPTED"
