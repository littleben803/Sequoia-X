"""终端回填视图测试。"""

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from sequoia_x.data.engine import BackfillProgress, BackfillReport
from sequoia_x.data.models import IngestionProgress
from sequoia_x.data.research import (
    AdjustmentFactorProgress,
    AdjustmentFactorRefreshReport,
    ResearchSnapshotProgress,
)
from sequoia_x.reporting.console import (
    AdjustmentFactorConsole,
    BackfillConsole,
    DataConsole,
    ResearchScanConsole,
)
from sequoia_x.strategy.base import StrategyResult


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


def test_factor_progress_displays_current_security_counts_and_summary() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=180)
    view = AdjustmentFactorConsole(console)
    view.show_header("2026-10-14", "data/test.db", "a-stock-data")
    view.update(
        AdjustmentFactorProgress(
            phase="PREPARING",
            as_of_date="2026-10-14",
            current_instrument_id=None,
            current_name=None,
            processed_instruments=0,
            requested_instruments=5574,
            successful_instruments=0,
            failed_instruments=0,
            unsupported_instruments=0,
            factor_rows_written=0,
            message="已载入 5,574 只研究日有效 A 股",
        )
    )
    view.update(
        AdjustmentFactorProgress(
            phase="REFRESHING",
            as_of_date="2026-10-14",
            current_instrument_id="SSE:600519",
            current_name="贵州茅台",
            processed_instruments=1,
            requested_instruments=5574,
            successful_instruments=1,
            failed_instruments=0,
            unsupported_instruments=0,
            factor_rows_written=42,
            message="成功保存 42 条因子",
        )
    )
    view.show_summary(
        AdjustmentFactorRefreshReport(
            as_of_date="2026-10-14",
            requested_instruments=5574,
            successful_instruments=5200,
            failed_instruments=1,
            unsupported_instruments=373,
            factor_rows_written=80_000,
            elapsed_seconds=123.4,
            failures=("SSE:600002: 源站失败",),
        )
    )

    rendered = output.getvalue()
    assert "前复权因子刷新" in rendered
    assert "贵州茅台（SSE:600519）" in rendered
    assert "成功" in rendered and "5,200" in rendered
    assert "当前源不支持" in rendered and "373" in rendered
    assert "123.4s" in rendered
    assert "SSE:600002: 源站失败" in rendered


def test_research_scan_displays_phases_strategy_results_and_summary() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=180)
    view = ResearchScanConsole(console)
    view.show_header(
        requested_date=None,
        db_path="data/test.db",
        strategies=("ma_volume", "rps"),
        output_path="reports/latest.json",
        history_bars=121,
    )
    view.update_snapshot(
        ResearchSnapshotProgress(
            phase="READY",
            message="质量闸门通过（最低覆盖率 95.00%）",
            as_of_date="2026-10-14",
            requested_instruments=5000,
            eligible_instruments=4900,
            excluded_instruments=100,
            coverage=0.98,
        )
    )
    result = StrategyResult(
        strategy_key="ma_volume",
        as_of_date="2026-10-14",
        selected_instruments=("SSE:600001",),
        rule_basis=("测试规则",),
        input_instruments=5000,
        eligible_instruments=4900,
        data_exclusions=100,
    )
    view.show_strategy_phase(1)
    view.show_strategy_start(1, 1, "ma_volume")
    view.show_strategy_result(result)
    view.show_report_phase("reports/latest.json")
    view.show_summary(
        snapshot=SimpleNamespace(
            as_of_date="2026-10-14",
            price_adjustment="QFQ",
            requested_instruments=5000,
            eligible_instruments=4900,
            exclusions=tuple(range(100)),
            coverage=0.98,
        ),
        results=(result,),
        report_path=Path("reports/latest.json"),
        elapsed_seconds=1.2,
    )

    rendered = output.getvalue()
    assert "阶段 1/4" in rendered
    assert "阶段 2/4" in rendered
    assert "阶段 3/4" in rendered
    assert "阶段 4/4" in rendered
    assert "质量闸门通过" in rendered
    assert "ma_volume 完成" in rendered
    assert "Research Scan Summary" in rendered
    assert "去重候选" in rendered
    assert "reports/latest.json" in rendered
