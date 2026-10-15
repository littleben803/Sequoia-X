"""V3 回填编排、中断恢复和快速失败策略测试。"""

from datetime import date
from unittest.mock import Mock

import pytest

from sequoia_x.data.database import MarketDataRepository
from sequoia_x.data.errors import DataNotAvailableError
from sequoia_x.data.service import DataApplicationService


def _service_fixture() -> tuple[DataApplicationService, Mock, Mock]:
    repository = Mock()
    repository.recover_stale_batches.return_value = 1
    repository.trading_dates.return_value = ("2026-10-13",)
    repository.has_complete_snapshot.return_value = False
    repository.start_batch.return_value = "batch-1"
    router = Mock()
    router.primary.provider_name = "a-stock-data"
    service = DataApplicationService(repository, router)
    service.refresh_reference_data = Mock(return_value=(1, 0, False, None))
    return service, repository, router


def test_snapshot_failure_is_recorded_without_full_market_baostock_fallback() -> None:
    service, repository, router = _service_fixture()
    router.primary.fetch_daily_snapshot.side_effect = RuntimeError("盘后包不可用")
    events = []

    report = service.backfill("2026-10-13", "2026-10-13", events.append)

    repository.fail_batch.assert_called_once()
    router.fallback.fetch_daily_bars.assert_not_called()
    assert report.failed_dates == 1
    assert report.recovered_stale_batches == 1
    assert report.failed_trade_dates == ("2026-10-13",)
    assert events[-1].message == "盘后包失败，已快速记录并继续；未启动逐股降级"


def test_keyboard_interrupt_marks_current_batch_and_can_resume() -> None:
    service, repository, router = _service_fixture()
    router.primary.fetch_daily_snapshot.side_effect = KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        service.backfill("2026-10-13", "2026-10-13")

    repository.interrupt_batch.assert_called_once_with("batch-1")
    repository.fail_batch.assert_not_called()


def test_unpublished_current_day_is_pending_not_failed() -> None:
    service, repository, router = _service_fixture()
    today = date.today().isoformat()
    repository.trading_dates.return_value = (today,)
    router.primary.fetch_daily_snapshot.side_effect = DataNotAvailableError("源站尚未发布")

    report = service.backfill(today, today)

    repository.skip_unpublished_batch.assert_called_once_with("batch-1", "源站尚未发布")
    repository.fail_batch.assert_not_called()
    assert report.pending_dates == 1
    assert report.pending_trade_dates == (today,)
    assert report.failed_dates == 0


def test_missing_historical_package_remains_a_real_failure() -> None:
    service, repository, router = _service_fixture()
    router.primary.fetch_daily_snapshot.side_effect = DataNotAvailableError("历史资源不存在")

    report = service.backfill("2026-10-13", "2026-10-13")

    repository.fail_batch.assert_called_once()
    repository.skip_unpublished_batch.assert_not_called()
    assert report.failed_dates == 1
    assert report.pending_dates == 0


def test_stale_running_batch_is_recovered(tmp_path) -> None:
    repository = MarketDataRepository(str(tmp_path / "v3.db"))
    batch_id = repository.start_batch("a-stock-data", "DAILY_SNAPSHOT", "2026-09-03")
    with repository.connect() as conn:
        conn.execute(
            "UPDATE ingestion_batches SET started_at='2020-01-01T00:00:00+00:00' WHERE batch_id=?",
            (batch_id,),
        )

    assert repository.recover_stale_batches() == 1

    with repository.connect() as conn:
        status, code = conn.execute(
            "SELECT status, error_code FROM ingestion_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
    assert status == "FAILED"
    assert code == "INTERRUPTED_STALE"
