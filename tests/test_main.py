"""V3 主程序入口测试。"""

import sys
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

import main as main_module
from sequoia_x.data.models import IngestionReport, V3AuditReport
from sequoia_x.data.research import ResearchDataError


@given(error_msg=st.text(min_size=1, max_size=100))
@h_settings(max_examples=30, deadline=None)
def test_main_exits_nonzero_on_exception(error_msg: str) -> None:
    with (
        patch.object(sys, "argv", ["main.py"]),
        patch.object(main_module, "get_settings", side_effect=RuntimeError(error_msg)),
    ):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main()
        assert exc_info.value.code != 0


def test_backfill_failed_date_exits_nonzero() -> None:
    report = IngestionReport(
        requested_dates=2,
        processed_dates=2,
        successful_dates=1,
        degraded_dates=0,
        skipped_dates=0,
        failed_dates=1,
        rows_written=100,
        instruments_upserted=50,
        elapsed_seconds=1,
        failed_trade_dates=("2026-10-12",),
    )
    audit = V3AuditReport(
        ready_for_a_share_research=False,
        latest_complete_date="2026-10-11",
        instruments=50,
        raw_bars=100,
        batches=2,
        complete_batches=1,
        degraded_batches=0,
        failed_batches=1,
        unexplained_a_share_gaps=0,
        invalid_rows=0,
        problems=("存在失败批次",),
    )
    repository = Mock()
    repository.audit.return_value = audit
    service = Mock()
    service.backfill.return_value = report
    settings = SimpleNamespace(
        db_path="data/test-v3.db",
        start_date="2026-10-11",
    )

    with (
        patch.object(sys, "argv", ["main.py", "--backfill"]),
        patch.object(main_module, "get_settings", return_value=settings),
        patch.object(main_module, "_components", return_value=(repository, service)),
        pytest.raises(SystemExit) as exc_info,
    ):
        main_module.main()

    assert exc_info.value.code == 1


def test_backfill_keyboard_interrupt_exits_130() -> None:
    repository = Mock()
    service = Mock()
    service.backfill.side_effect = KeyboardInterrupt
    settings = SimpleNamespace(db_path="data/test-v3.db", start_date="2026-10-11")

    with (
        patch.object(sys, "argv", ["main.py", "--backfill"]),
        patch.object(main_module, "get_settings", return_value=settings),
        patch.object(main_module, "_components", return_value=(repository, service)),
        pytest.raises(SystemExit) as exc_info,
    ):
        main_module.main()

    assert exc_info.value.code == 130


def test_default_backfill_stops_at_yesterday() -> None:
    settings = SimpleNamespace(db_path="data/test-v3.db", start_date="2024-01-01")
    repository = Mock()
    service = Mock()

    with (
        patch.object(sys, "argv", ["main.py", "--backfill"]),
        patch.object(main_module, "get_settings", return_value=settings),
        patch.object(main_module, "_components", return_value=(repository, service)),
        patch.object(main_module, "_run_backfill", return_value=0) as run_backfill,
        pytest.raises(SystemExit) as exc_info,
    ):
        main_module.main()

    assert exc_info.value.code == 0
    assert run_backfill.call_args.args[-1] == (date.today() - timedelta(days=1)).isoformat()


def test_daily_sync_enables_incremental_quality_processing() -> None:
    settings = SimpleNamespace(db_path="data/test-v3.db", start_date="2024-01-01")
    repository = Mock()
    service = Mock()

    with (
        patch.object(sys, "argv", ["main.py", "data", "sync", "--days", "7"]),
        patch.object(main_module, "get_settings", return_value=settings),
        patch.object(main_module, "_components", return_value=(repository, service)),
        patch.object(main_module, "_run_backfill", return_value=0) as run_backfill,
        pytest.raises(SystemExit) as exc_info,
    ):
        main_module.main()

    assert exc_info.value.code == 0
    assert run_backfill.call_args.kwargs == {"incremental_quality": True}
    assert run_backfill.call_args.args[-2:] == (
        (date.today() - timedelta(days=7)).isoformat(),
        (date.today() - timedelta(days=1)).isoformat(),
    )


def test_scan_only_routes_to_v3_research_scan() -> None:
    settings = SimpleNamespace(db_path="data/test-v3.db", start_date="2024-01-01")
    repository = Mock()
    service = Mock()

    with (
        patch.object(sys, "argv", ["main.py", "--scan-only"]),
        patch.object(main_module, "get_settings", return_value=settings),
        patch.object(main_module, "_components", return_value=(repository, service)),
        patch.object(main_module, "_run_research_scan", return_value=0) as run_scan,
        pytest.raises(SystemExit) as exc_info,
    ):
        main_module.main()

    assert exc_info.value.code == 0
    run_scan.assert_called_once_with(
        settings,
        repository,
        as_of_date=None,
        requested_strategies=None,
        output_path="reports/latest.json",
    )


def test_research_scan_dispatches_explicit_strategy_and_output() -> None:
    settings = SimpleNamespace(db_path="data/test-v3.db", start_date="2024-01-01")
    repository = Mock()
    service = Mock()

    with (
        patch.object(
            sys,
            "argv",
            [
                "main.py",
                "research",
                "scan",
                "--date",
                "2026-10-13",
                "--strategy",
                "rps",
                "--output",
                "reports/test.json",
            ],
        ),
        patch.object(main_module, "get_settings", return_value=settings),
        patch.object(main_module, "_components", return_value=(repository, service)),
        patch.object(main_module, "_run_research_scan", return_value=0) as run_scan,
        pytest.raises(SystemExit) as exc_info,
    ):
        main_module.main()

    assert exc_info.value.code == 0
    run_scan.assert_called_once_with(
        settings,
        repository,
        as_of_date="2026-10-13",
        requested_strategies=["rps"],
        output_path="reports/test.json",
    )


def test_research_quality_gate_failure_exits_one() -> None:
    settings = SimpleNamespace(db_path="data/test-v3.db", start_date="2024-01-01")
    repository = Mock()
    service = Mock()

    with (
        patch.object(sys, "argv", ["main.py", "--scan-only"]),
        patch.object(main_module, "get_settings", return_value=settings),
        patch.object(main_module, "_components", return_value=(repository, service)),
        patch.object(
            main_module,
            "_run_research_scan",
            side_effect=ResearchDataError("复权因子覆盖不足"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        main_module.main()

    assert exc_info.value.code == 1


def test_default_strategy_selection_keeps_experimental_strategies_disabled() -> None:
    settings = SimpleNamespace(enabled_strategies="ma_volume,turtle,flag,rps")

    assert main_module._strategy_keys(settings, None) == (
        "ma_volume",
        "turtle",
        "flag",
        "rps",
    )


def test_failed_snapshot_does_not_overwrite_last_successful_report() -> None:
    settings = SimpleNamespace(
        enabled_strategies="ma_volume,turtle,flag,rps",
        minimum_latest_coverage=0.95,
    )
    repository = Mock()

    with (
        patch.object(main_module, "ResearchDataService") as service_type,
        patch.object(main_module, "write_research_report") as write_report,
        pytest.raises(ResearchDataError, match="覆盖不足"),
    ):
        service_type.return_value.build_snapshot.side_effect = ResearchDataError("覆盖不足")
        main_module._run_research_scan(
            settings,
            repository,
            as_of_date=None,
            requested_strategies=None,
            output_path="reports/latest.json",
        )

    write_report.assert_not_called()
