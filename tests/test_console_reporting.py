"""终端回填视图测试。"""

from io import StringIO

from rich.console import Console

from sequoia_x.data.engine import BackfillProgress, BackfillReport
from sequoia_x.data.models import IngestionProgress
from sequoia_x.reporting.console import BackfillConsole, DataConsole


def test_backfill_summary_distinguishes_aborted_run() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)
    view = BackfillConsole(console)
    report = BackfillReport(
        requested_symbols=100,
        processed_symbols=25,
        successful_symbols=20,
        up_to_date_symbols=4,
        empty_symbols=0,
        failed_symbols=1,
        rows_written=1234,
        elapsed_seconds=65,
        completed=False,
        aborted_reason="数据服务重连失败",
        failed_codes=("600000",),
    )

    view.show_summary(report)
    rendered = output.getvalue()

    assert "回填 Summary" in rendered
    assert "提前终止" in rendered
    assert "25.00%" in rendered
    assert "数据服务重连失败" in rendered


def test_progress_displays_stock_name_and_code() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=140)
    view = BackfillConsole(console)
    view.start_progress(10)
    view.update(
        BackfillProgress(
            current_symbol="688615",
            current_name="合合信息",
            processed_symbols=1,
            requested_symbols=10,
            successful_symbols=1,
            up_to_date_symbols=0,
            empty_symbols=0,
            failed_symbols=0,
            rows_written=500,
        )
    )
    view.stop_progress()

    assert "合合信息（688615）" in output.getvalue()


def test_v3_progress_displays_current_step_and_heartbeat() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=180)
    view = DataConsole(console)
    view.update(
        IngestionProgress(
            phase="DAILY_SNAPSHOT",
            step="下载并解析全市场盘后包",
            current_date="2026-09-03",
            processed_dates=646,
            total_dates=671,
            successful_dates=645,
            degraded_dates=0,
            skipped_dates=1,
            failed_dates=1,
            rows_written=29_386_950,
            message="仍在下载或解析，任务保持运行",
            step_elapsed_seconds=30,
            is_heartbeat=True,
        )
    )
    view.stop()

    rendered = output.getvalue()
    assert "下载并解析全市场盘后包" in rendered
    assert "仍在运行" in rendered
    assert "✅ 成功 645" in rendered
    assert "🟡 降级 0" in rendered
    assert "↪️ 跳过 1" in rendered
    assert "❌ 失败 1" in rendered
    assert "💾" not in rendered


def test_v3_write_progress_displays_friendly_instrument_details() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=180)
    view = DataConsole(console)
    view.update(
        IngestionProgress(
            phase="DAILY_SNAPSHOT",
            step="保存当天市场数据",
            current_date="2026-09-15",
            processed_dates=10,
            total_dates=20,
            successful_dates=10,
            degraded_dates=0,
            skipped_dates=0,
            failed_dates=0,
            rows_written=402_226,
            current_instrument_name="沪深300ETF",
            current_instrument_code="510300",
            current_instrument_type="ETF",
            step_processed=12_500,
            step_total=52_000,
        )
    )
    view.stop()

    rendered = output.getvalue()
    assert "保存当天市场数据" in rendered
    assert "沪深300ETF（510300）· ETF" in rendered
    assert "12,500/52,000" in rendered
    assert "402,226" not in rendered
