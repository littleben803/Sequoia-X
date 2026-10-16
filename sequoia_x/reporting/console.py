"""终端展示层：数据层只发送结构化事件，不依赖 Rich。"""

from pathlib import Path

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
from sequoia_x.data.research import (
    AdjustmentFactorProgress,
    AdjustmentFactorRefreshReport,
    ResearchSnapshot,
    ResearchSnapshotProgress,
)
from sequoia_x.strategy.base import StrategyResult


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


class AdjustmentFactorConsole:
    """V3 前复权因子刷新终端视图。"""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or get_console()
        self._progress: Progress | None = None
        self._task_id: int | None = None

    def show_header(self, as_of_date: str, db_path: str, provider: str) -> None:
        self.console.print()
        self.console.print(
            Panel.fit(
                "[bold green]🌲 Sequoia-X V3 前复权因子刷新[/bold green]\n"
                f"证据日：[cyan]{as_of_date}[/cyan]  "
                f"Provider：[cyan]{provider}[/cyan]\n"
                f"数据库：[cyan]{db_path}[/cyan]  口径：[cyan]QFQ[/cyan]",
                border_style="green",
            )
        )
        self.console.rule("[bold cyan]📋 阶段 1/2 · 准备研究日 A 股列表[/bold cyan]")

    def update(self, event: AdjustmentFactorProgress) -> None:
        if event.phase == "PREPARING":
            self.console.print(f"[green]✅ {event.message}[/green]")
            self._start(event.requested_instruments)
            return
        if event.phase == "INTERRUPTED":
            self.stop()
            self.console.print(f"[bold yellow]⏸️ {event.message}[/bold yellow]")
            return
        if event.phase == "COMPLETE":
            if self._progress is not None and self._task_id is not None:
                self._progress.update(
                    self._task_id,
                    completed=event.processed_instruments,
                    description="前复权因子刷新完成",
                    refresh=True,
                )
            return
        if self._progress is None:
            self._start(event.requested_instruments)
        assert self._progress is not None and self._task_id is not None
        identity = event.current_instrument_id or "-"
        if event.current_name:
            identity = f"{event.current_name}（{identity}）"
        self._progress.update(
            self._task_id,
            total=event.requested_instruments,
            completed=event.processed_instruments,
            description=(
                f"{identity} · {event.message} "
                f"[green]✅ {event.successful_instruments}[/green] "
                f"[yellow]↪️ 不支持 {event.unsupported_instruments}[/yellow] "
                f"[red]❌ {event.failed_instruments}[/red] "
                f"[cyan]💾 {event.factor_rows_written:,}[/cyan]"
            ),
            refresh=True,
        )

    def _start(self, total: int) -> None:
        if self._progress is not None:
            return
        self.console.rule("[bold cyan]🔄 阶段 2/2 · 逐证券请求、校验并保存 QFQ 因子[/bold cyan]")
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
        self._task_id = self._progress.add_task("正在准备因子任务", total=total)

    def stop(self) -> None:
        if self._progress:
            self._progress.stop()
        self._progress = None
        self._task_id = None

    def show_interrupted(self) -> None:
        self.stop()
        self.console.print(
            Panel.fit(
                "[bold yellow]⏸️ 前复权因子刷新已安全停止[/bold yellow]\n"
                "已完成的证券因子保留；重新运行会幂等更新。",
                border_style="yellow",
            )
        )

    def show_summary(self, report: AdjustmentFactorRefreshReport) -> None:
        self.stop()
        table = Table(title="📊 QFQ 因子刷新 Summary", border_style="cyan", show_header=False)
        table.add_column("项目", style="bold")
        table.add_column("结果", justify="right")
        table.add_row("证据日", report.as_of_date)
        table.add_row("请求证券", f"{report.requested_instruments:,}")
        table.add_row("成功", f"[green]{report.successful_instruments:,}[/green]")
        table.add_row("当前源不支持", f"[yellow]{report.unsupported_instruments:,}[/yellow]")
        table.add_row("失败", f"[red]{report.failed_instruments:,}[/red]")
        table.add_row("写入/更新因子", f"{report.factor_rows_written:,}")
        table.add_row("耗时", f"{report.elapsed_seconds:.1f}s")
        status = (
            "[green]✅ 完成[/green]"
            if report.failed_instruments == 0
            else "[yellow]⚠️ 完成但存在失败[/yellow]"
        )
        table.add_row("状态", status)
        self.console.print(table)
        failed_details = [
            item for item in report.failures if "UNSUPPORTED_FACTOR_SOURCE" not in item
        ]
        for error in failed_details[-10:]:
            self.console.print(f"[red]❌ {error}[/red]")


class ResearchScanConsole:
    """V3 本地选股扫描终端视图。"""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or get_console()

    def show_header(
        self,
        *,
        requested_date: str | None,
        db_path: str,
        strategies: tuple[str, ...],
        output_path: str,
        history_bars: int,
    ) -> None:
        date_text = requested_date or "自动选择最新完整交易日"
        self.console.print()
        self.console.print(
            Panel.fit(
                "[bold green]🌲 Sequoia-X V3 本地选股扫描[/bold green]\n"
                f"研究日：[cyan]{date_text}[/cyan]  口径：[cyan]QFQ[/cyan]\n"
                f"策略：[cyan]{', '.join(strategies)}[/cyan]  "
                f"历史窗口：[cyan]{history_bars}[/cyan]\n"
                f"数据库：[cyan]{db_path}[/cyan]\n"
                f"报告：[cyan]{output_path}[/cyan]",
                border_style="green",
            )
        )
        self.console.rule("[bold cyan]📋 阶段 1/4 · 解析扫描配置[/bold cyan]")
        self.console.print(
            f"[green]✅ 已启用 {len(strategies)} 个策略；扫描只读取本地数据[/green]"
        )
        self.console.rule("[bold cyan]🧪 阶段 2/4 · 构建可信研究快照[/bold cyan]")

    def update_snapshot(self, event: ResearchSnapshotProgress) -> None:
        marker = "✅" if event.phase in {"DATE_RESOLVED", "READY"} else "🔄"
        color = "green" if event.phase in {"DATE_RESOLVED", "READY"} else "cyan"
        detail = ""
        if event.phase == "READY":
            detail = (
                f" · 请求 {event.requested_instruments:,}"
                f" · 可用 {event.eligible_instruments:,}"
                f" · 排除 {event.excluded_instruments:,}"
                f" · 覆盖率 {event.coverage:.2%}"
            )
        self.console.print(f"[{color}]{marker} {event.message}{detail}[/{color}]")

    def show_strategy_phase(self, total: int) -> None:
        self.console.rule(f"[bold cyan]🎯 阶段 3/4 · 依次执行 {total} 个选股策略[/bold cyan]")

    def show_strategy_start(self, index: int, total: int, strategy_key: str) -> None:
        self.console.print(
            f"[cyan]🔄 策略 {index}/{total} · {strategy_key} · 正在计算[/cyan]"
        )

    def show_strategy_result(self, result: StrategyResult) -> None:
        error_text = (
            f" · [yellow]计算异常 {len(result.calculation_errors):,}[/yellow]"
            if result.calculation_errors
            else ""
        )
        self.console.print(
            f"[green]✅ {result.strategy_key} 完成[/green]"
            f" · 输入 {result.input_instruments:,}"
            f" · 有效 {result.eligible_instruments:,}"
            f" · 入选 [bold cyan]{len(result.selected_instruments):,}[/bold cyan]"
            f"{error_text}"
        )
        if result.risk_note:
            self.console.print(f"[yellow]⚠️ {result.strategy_key}：{result.risk_note}[/yellow]")

    def show_report_phase(self, output_path: str) -> None:
        self.console.rule("[bold cyan]💾 阶段 4/4 · 写入本地研究报告[/bold cyan]")
        self.console.print(f"[cyan]🔄 正在原子写入 {output_path}[/cyan]")

    def show_failure(self, message: str) -> None:
        self.console.print(f"[bold red]❌ 扫描停止：{message}[/bold red]")

    def show_summary(
        self,
        *,
        snapshot: ResearchSnapshot,
        results: tuple[StrategyResult, ...],
        report_path: Path,
        elapsed_seconds: float,
    ) -> None:
        unique_selected = set().union(
            *(result.selected_instruments for result in results)
        )
        table = Table(title="📊 Research Scan Summary", border_style="cyan", show_header=False)
        table.add_column("项目", style="bold")
        table.add_column("结果", justify="right")
        table.add_row("数据日期", snapshot.as_of_date)
        table.add_row("价格口径", snapshot.price_adjustment)
        table.add_row("请求证券", f"{snapshot.requested_instruments:,}")
        table.add_row("有效证券", f"[green]{snapshot.eligible_instruments:,}[/green]")
        table.add_row("数据排除", f"[yellow]{len(snapshot.exclusions):,}[/yellow]")
        table.add_row("覆盖率", f"{snapshot.coverage:.2%}")
        for result in results:
            table.add_row(
                f"策略 {result.strategy_key}",
                f"[cyan]入选 {len(result.selected_instruments):,}[/cyan]",
            )
        table.add_row("去重候选", f"[bold cyan]{len(unique_selected):,}[/bold cyan]")
        table.add_row("报告路径", str(report_path))
        table.add_row("耗时", f"{elapsed_seconds:.1f}s")
        table.add_row("状态", "[green]✅ 完成[/green]")
        self.console.print("[green]✅ 研究报告已安全写入[/green]")
        self.console.print(table)
        self.console.print("[dim]仅用于个人研究，不构成投资建议或收益保证。[/dim]")
