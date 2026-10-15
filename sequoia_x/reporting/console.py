"""终端展示层：数据层只发送结构化事件，不依赖 Rich。"""

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from sequoia_x.core.logger import get_console
from sequoia_x.data.engine import BackfillPlan, BackfillProgress, BackfillReport, DataAuditReport
from sequoia_x.data.models import (
    IngestionProgress,
    IngestionReport,
    QualityRepairReport,
    V3AuditReport,
)


class BackfillConsole:
    """V2 兼容视图；旧策略尚未迁移时保留。"""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or get_console()
        self._progress: Progress | None = None
        self._task_id: int | None = None
        self._phase: str | None = None

    def show_header(self, start_date: str, db_path: str) -> None:
        self.console.print()
        self.console.print(
            Panel.fit(
                "[bold green]🌲 Sequoia-X 全市场历史数据回填[/bold green]\n"
                f"历史起点：[cyan]{start_date}[/cyan]  数据库：[cyan]{db_path}[/cyan]",
                border_style="green",
            )
        )

    def show_phase(self, step: int, total: int, title: str, detail: str) -> None:
        self.console.rule(f"[bold cyan]📋 阶段 {step}/{total} · {title}[/bold cyan]")
        self.console.print(f"[dim]{detail}[/dim]")

    def show_success(self, message: str) -> None:
        self.console.print(f"[bold green]✅ {message}[/bold green]")

    def show_warning(self, message: str) -> None:
        self.console.print(f"[bold yellow]⚠️ {message}[/bold yellow]")

    def show_error(self, message: str) -> None:
        self.console.print(f"[bold red]❌ {message}[/bold red]")

    def start_progress(self, total: int) -> None:
        self._progress = Progress(
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            TextColumn("{task.completed:,.0f}/{task.total:,.0f}"),
            TimeElapsedColumn(),
            console=self.console,
            expand=True,
        )
        self._progress.start()
        self._task_id = self._progress.add_task("准备拉取", total=total)

    def show_plan(self, plan: BackfillPlan) -> None:
        table = Table(title="🧭 回填执行计划", border_style="cyan")
        table.add_column("优先级")
        table.add_column("队列")
        table.add_column("数量", justify="right")
        table.add_row("1", "补齐完全缺失股票", f"{len(plan.missing_tasks):,}")
        table.add_row("2", "修复陈旧数据和中间缺口", f"{len(plan.stale_tasks):,}")
        table.add_row("3", "更新普通增量", f"{len(plan.incremental_tasks):,}")
        table.add_row("—", "已经覆盖目标交易日", f"{len(plan.current_symbols):,}")
        self.console.print(table)

    def start_queue(self, index: int, title: str, detail: str, total: int) -> None:
        self.stop_progress()
        self.console.rule(f"[bold magenta]📥 回填队列 {index}/3 · {title}[/bold magenta]")
        self.console.print(f"[dim]{detail}[/dim]")
        self.start_progress(total)

    def update(self, event: BackfillProgress) -> None:
        if self._progress is None or self._task_id is None:
            return
        display = (
            f"{event.current_name}（{event.current_symbol}）"
            if event.current_name
            else event.current_symbol or "-"
        )
        self._progress.update(
            self._task_id,
            completed=event.queue_processed
            if event.queue_processed is not None
            else event.processed_symbols,
            description=(
                f"{display} 成功 {event.successful_symbols} "
                f"跳过 {event.skipped_symbols} 失败 {event.failed_symbols}"
            ),
            refresh=True,
        )

    def stop_progress(self) -> None:
        if self._progress:
            self._progress.stop()
        self._progress = None
        self._task_id = None

    def show_summary(self, report: BackfillReport, audit: DataAuditReport | None = None) -> None:
        self.stop_progress()
        table = Table(title="📊 回填 Summary", border_style="cyan", show_header=False)
        table.add_column("项目", style="bold")
        table.add_column("结果", justify="right")
        table.add_row("计划处理", f"{report.requested_symbols:,}")
        table.add_row("实际处理", f"{report.processed_symbols:,}")
        table.add_row("成功", f"[green]{report.successful_symbols:,}[/green]")
        table.add_row("失败", f"[red]{report.failed_symbols:,}[/red]")
        table.add_row("写入行数", f"{report.rows_written:,}")
        table.add_row("处理进度", f"{report.progress:.2%}")
        if audit:
            table.add_row("全市场最新日覆盖", f"{audit.latest_symbols:,}/{audit.total_symbols:,}")
        if report.succeeded:
            table.add_row("状态", "[green]✅ 完成[/green]")
        elif report.completed:
            table.add_row("状态", "[yellow]⚠️ 完成但存在失败[/yellow]")
        else:
            table.add_row("状态", "[red]❌ 提前终止[/red]")
        if report.aborted_reason:
            table.add_row("终止原因", f"[red]{report.aborted_reason}[/red]")
        self.console.print(table)


class DataConsole:
    """V3 多资产数据任务终端视图。"""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or get_console()
        self._progress: Progress | None = None
        self._task_id: int | None = None

    def show_header(self, start_date: str, end_date: str, db_path: str) -> None:
        self.console.print()
        self.console.print(
            Panel.fit(
                "[bold green]🌲 Sequoia-X V3 全市场原始数据回填[/bold green]\n"
                f"日期范围：[cyan]{start_date}[/cyan] → [cyan]{end_date}[/cyan]\n"
                f"数据库：[cyan]{db_path}[/cyan]  口径：[cyan]原始不复权[/cyan]",
                border_style="green",
            )
        )
        self.console.rule("[bold cyan]📅 阶段 1/3 · 刷新交易日历与证券主数据[/bold cyan]")
        self.console.print("[dim]盘后包内股票、基金、债券、可转债等证券全部保留。[/dim]")

    def start(self, total: int = 1) -> None:
        self.console.rule("[bold cyan]📦 阶段 2/3 · 按交易日下载并写入全市场盘后数据[/bold cyan]")
        self._progress = Progress(
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            TextColumn("{task.completed:,.0f}/{task.total:,.0f}"),
            TimeElapsedColumn(),
            console=self.console,
            expand=True,
        )
        self._progress.start()
        self._task_id = self._progress.add_task("正在准备交易日任务", total=total)
        self._phase = "DAILY_SNAPSHOT"

    def update(self, event: IngestionProgress) -> None:
        if event.phase == "REFERENCE_DATA":
            suffix = f" · 已等待 {event.step_elapsed_seconds:.0f}s" if event.is_heartbeat else ""
            self.console.print(f"[cyan]🔄 {event.message}{suffix}[/cyan]")
            return
        if event.phase == "INTERRUPTED":
            self.stop()
            self.console.print(f"[bold yellow]⏸️ {event.message}[/bold yellow]")
            return
        if self._progress is None:
            self.start(event.total_dates)
        assert self._progress is not None and self._task_id is not None
        type_labels = {
            "STOCK": "股票",
            "ETF": "ETF",
            "LOF": "LOF",
            "FUND": "基金",
            "REIT": "REIT",
            "BOND": "债券",
            "CONVERTIBLE_BOND": "可转债",
            "EXCHANGEABLE_BOND": "可交换债",
            "ABS": "ABS",
            "INDEX": "指数",
            "REPO": "回购",
            "OTHER": "其他证券",
            "UNKNOWN": "待分类证券",
        }
        instrument_detail = ""
        if event.current_instrument_code:
            display_name = event.current_instrument_name or "未知名称"
            display_type = type_labels.get(
                event.current_instrument_type or "", event.current_instrument_type or "证券"
            )
            instrument_detail = (
                f" · {display_name}（{event.current_instrument_code}）· {display_type}"
            )
            if event.step_total:
                instrument_detail += f" {event.step_processed:,}/{event.step_total:,}"
        self._progress.update(
            self._task_id,
            total=event.total_dates,
            completed=event.processed_dates,
            description=(
                f"{event.current_date or '-'} · {event.step}{instrument_detail} "
                f"[green]✅ 成功 {event.successful_dates}[/green] "
                f"[yellow]🟡 降级 {event.degraded_dates}[/yellow] "
                f"[cyan]↪️ 跳过 {event.skipped_dates}[/cyan] "
                f"[yellow]⏳ 待发布 {event.pending_dates}[/yellow] "
                f"[red]❌ 失败 {event.failed_dates}[/red]"
                + (
                    f" [yellow]⏱ {event.step_elapsed_seconds:.0f}s，仍在运行[/yellow]"
                    if event.is_heartbeat
                    else ""
                )
            ),
            refresh=True,
        )

    def stop(self) -> None:
        if self._progress:
            self._progress.stop()
        self._progress = None
        self._task_id = None
        self._phase = None

    def show_interrupted(self) -> None:
        self.stop()
        self.console.print()
        self.console.print(
            Panel.fit(
                "[bold yellow]⏸️ 回填已安全停止[/bold yellow]\n"
                "当前批次已记录为中断；再次运行同一命令会跳过完整日期并自动续跑。",
                border_style="yellow",
            )
        )

    def show_incremental_quality(self, report: QualityRepairReport) -> None:
        self.console.print(
            "[green]✅ 增量质量处理完成[/green] · "
            f"证券分类修正 {report.instruments_reclassified:,} · "
            f"生命周期关闭 {report.delisted_or_inactive:,} · "
            f"状态重建 {report.statuses_rebuilt:,}"
        )

    def show_summary(self, report: IngestionReport, audit: V3AuditReport) -> None:
        self.stop()
        self.console.rule("[bold cyan]🔎 阶段 3/3 · 数据审计[/bold cyan]")
        table = Table(title="📊 V3 数据回填 Summary", border_style="cyan", show_header=False)
        table.add_column("项目", style="bold")
        table.add_column("结果", justify="right")
        table.add_row("计划交易日", f"{report.requested_dates:,}")
        table.add_row("完整成功", f"[green]{report.successful_dates:,}[/green]")
        table.add_row("降级部分成功", f"[yellow]{report.degraded_dates:,}[/yellow]")
        table.add_row("已存在跳过", f"{report.skipped_dates:,}")
        table.add_row("等待盘后包发布", f"[yellow]{report.pending_dates:,}[/yellow]")
        table.add_row("失败", f"[red]{report.failed_dates:,}[/red]")
        table.add_row("自动回收僵尸批次", f"{report.recovered_stale_batches:,}")
        table.add_row("写入/更新原始记录", f"{report.rows_written:,}")
        table.add_row("证券主数据", f"{audit.instruments:,}")
        table.add_row("原始行情总量", f"{audit.raw_bars:,}")
        if report.security_type_counts:
            labels = {
                "STOCK": "股票",
                "ETF": "ETF",
                "LOF": "LOF",
                "FUND": "基金",
                "REIT": "REIT",
                "BOND": "债券",
                "CONVERTIBLE_BOND": "可转债",
                "EXCHANGEABLE_BOND": "可交换债",
                "ABS": "ABS",
                "INDEX": "指数",
                "REPO": "回购",
                "OTHER": "其他",
                "UNKNOWN": "待分类",
            }
            details = "  ".join(
                f"{labels.get(kind, kind)} {count:,}"
                for kind, count in sorted(report.security_type_counts.items())
            )
            table.add_row("盘后包证券分类", details)
        table.add_row("无法解释的 A 股缺口", f"{audit.unexplained_a_share_gaps:,}")
        table.add_row("非法行情", f"{audit.invalid_rows:,}")
        ready = (
            "[green]✅ 是[/green]" if audit.ready_for_a_share_research else "[yellow]⚠️ 否[/yellow]"
        )
        table.add_row("A 股研究就绪", ready)
        if report.failed_trade_dates:
            table.add_row(
                "失败交易日", "[red]" + ", ".join(report.failed_trade_dates[:20]) + "[/red]"
            )
        if report.pending_trade_dates:
            table.add_row(
                "等待发布日期",
                "[yellow]" + ", ".join(report.pending_trade_dates[:20]) + "[/yellow]",
            )
        self.console.print(table)
        for error in report.errors[-10:]:
            self.console.print(f"[yellow]⚠️ {error}[/yellow]")
