"""Sequoia-X V3 命令行入口：当前阶段专注可信市场数据底座。"""

from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import time
from datetime import date, timedelta

from sequoia_x.backtest.data import BacktestDataService
from sequoia_x.backtest.engine import BacktestEngine
from sequoia_x.backtest.models import BacktestConfig
from sequoia_x.backtest.reporting import write_backtest_report
from sequoia_x.core.config import Settings, get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.database import MarketDataRepository
from sequoia_x.data.research import (
    AdjustmentFactorService,
    ResearchDataError,
    ResearchDataService,
)
from sequoia_x.data.router import ProviderRouter
from sequoia_x.data.service import DataApplicationService
from sequoia_x.reporting.console import (
    AdjustmentFactorConsole,
    DataConsole,
    ResearchScanConsole,
)
from sequoia_x.reporting.local import write_research_report
from sequoia_x.strategy import STRATEGY_TYPES, create_strategy

logger = get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sequoia-X V3 市场数据与研究工具")
    legacy = parser.add_mutually_exclusive_group()
    legacy.add_argument("--backfill", action="store_true", help="兼容命令：等价于 data backfill")
    legacy.add_argument("--audit", action="store_true", help="兼容命令：等价于 data audit")
    legacy.add_argument(
        "--scan-only",
        action="store_true",
        help="兼容命令：使用 V3 研究快照执行默认策略扫描",
    )

    commands = parser.add_subparsers(dest="command")
    data_parser = commands.add_parser("data", help="V3 数据获取、审计与批次查询")
    data_commands = data_parser.add_subparsers(dest="data_command")

    backfill = data_commands.add_parser("backfill", help="按交易日回填全市场原始盘后数据")
    backfill.add_argument("--start", help="起始日期 YYYY-MM-DD，默认读取 START_DATE")
    backfill.add_argument("--end", help="结束日期 YYYY-MM-DD，默认今天")

    sync = data_commands.add_parser("sync", help="增量补齐最近缺失交易日")
    sync.add_argument("--days", type=int, default=14, help="向前检查的自然日数，默认 14")

    data_commands.add_parser("audit", help="只读审计 V3 数据库")
    data_commands.add_parser("repair-quality", help="重建证券生命周期、代码别名和缺口状态")
    data_commands.add_parser("instruments", help="查看证券类型统计")
    data_commands.add_parser("batches", help="查看最近抓取批次")

    research_parser = commands.add_parser("research", help="V3 研究数据与选股扫描")
    research_commands = research_parser.add_subparsers(dest="research_command")
    factors = research_commands.add_parser("factors", help="显式刷新沪深 A 股前复权因子")
    factors.add_argument("--date", help="因子证据日 YYYY-MM-DD，默认最新完整交易日")
    research_audit = research_commands.add_parser("audit", help="只读审计研究快照")
    research_audit.add_argument("--date", help="研究日 YYYY-MM-DD")
    scan = research_commands.add_parser("scan", help="运行 V3 本地选股扫描")
    scan.add_argument("--date", help="研究日 YYYY-MM-DD")
    scan.add_argument(
        "--strategy",
        action="append",
        choices=sorted(STRATEGY_TYPES),
        help="显式选择策略，可重复；默认读取 ENABLED_STRATEGIES",
    )
    scan.add_argument("--output", default="reports/latest.json", help="本地 JSON 报告路径")

    backtest_parser = commands.add_parser("backtest", help="V3 本地历史信号回测")
    backtest_commands = backtest_parser.add_subparsers(dest="backtest_command")
    backtest_audit = backtest_commands.add_parser("audit", help="只读审计回测数据区间")
    backtest_audit.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    backtest_audit.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    backtest_audit.add_argument("--history-bars", type=int, default=121, help="历史窗口")
    backtest_run = backtest_commands.add_parser("run", help="运行逐日信号与前瞻收益评价")
    backtest_run.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    backtest_run.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    backtest_run.add_argument(
        "--strategy",
        action="append",
        choices=sorted(STRATEGY_TYPES),
        help="显式选择策略，可重复；默认读取 ENABLED_STRATEGIES",
    )
    backtest_run.add_argument(
        "--horizon",
        action="append",
        type=int,
        help="前瞻交易日数，可重复；默认 1/5/10/20",
    )
    backtest_run.add_argument("--history-bars", type=int, default=121, help="历史窗口")
    backtest_run.add_argument("--initial-cash", type=float, default=1_000_000, help="初始资金")
    backtest_run.add_argument("--holding-period", type=int, default=5, help="固定持有交易日")
    backtest_run.add_argument(
        "--exit-retry-days", type=int, default=5, help="卖出受阻后的尾部重试交易日"
    )
    backtest_run.add_argument("--max-positions", type=int, default=10, help="最大同时持仓数")
    backtest_run.add_argument(
        "--commission-rate", type=float, default=0.0003, help="双边佣金率"
    )
    backtest_run.add_argument(
        "--minimum-commission", type=float, default=5.0, help="单笔最低佣金"
    )
    backtest_run.add_argument("--slippage-bps", type=float, default=0.0, help="单边滑点基点")
    backtest_run.add_argument(
        "--output",
        default="reports/backtest-latest.json",
        help="本地 JSON 报告路径",
    )
    return parser.parse_args()


def _components(settings: Settings) -> tuple[MarketDataRepository, DataApplicationService]:
    repository = MarketDataRepository(settings.db_path)
    return repository, DataApplicationService(repository, ProviderRouter())


def _run_backfill(
    settings: Settings,
    repository: MarketDataRepository,
    service: DataApplicationService,
    start_date: str,
    end_date: str,
    incremental_quality: bool = False,
) -> int:
    date.fromisoformat(start_date)
    date.fromisoformat(end_date)
    view = DataConsole()
    view.show_header(start_date, end_date, settings.db_path)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def handle_sigterm(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        report = service.backfill(start_date, end_date, progress_callback=view.update)
    except KeyboardInterrupt:
        view.show_interrupted()
        logger.warning("回填已中断；重新运行原命令即可从未完成交易日续跑")
        return 130
    finally:
        view.stop()
        signal.signal(signal.SIGTERM, previous_sigterm)
    if incremental_quality:
        quality_report = repository.rebuild_data_quality(start_date, end_date)
        view.show_incremental_quality(quality_report)
    audit = repository.audit()
    view.show_summary(report, audit)
    if report.failed_dates:
        return 1
    if report.degraded_dates:
        logger.warning("任务包含降级部分数据，不代表全品种盘后数据完整")
    return 0


def _print_instruments(repository: MarketDataRepository) -> None:
    with repository.connect() as conn:
        rows = conn.execute(
            """
            SELECT exchange, security_type, COUNT(*) AS count
            FROM instruments GROUP BY exchange, security_type
            ORDER BY exchange, security_type
            """
        ).fetchall()
    print(
        json.dumps(
            [{"exchange": row[0], "security_type": row[1], "count": row[2]} for row in rows],
            ensure_ascii=False,
            indent=2,
        )
    )


def _print_batches(repository: MarketDataRepository) -> None:
    with repository.connect() as conn:
        rows = conn.execute(
            """
            SELECT batch_id, provider, capability, trade_date, status, degraded,
                   received_count, inserted_count, updated_count, error_message
            FROM ingestion_batches ORDER BY started_at DESC LIMIT 50
            """
        ).fetchall()
    keys = [
        "batch_id",
        "provider",
        "capability",
        "trade_date",
        "status",
        "degraded",
        "received_count",
        "inserted_count",
        "updated_count",
        "error_message",
    ]
    print(
        json.dumps(
            [dict(zip(keys, row, strict=True)) for row in rows], ensure_ascii=False, indent=2
        )
    )


def _strategy_keys(settings: Settings, requested: list[str] | None) -> tuple[str, ...]:
    raw_keys = requested or [
        key.strip().lower() for key in settings.enabled_strategies.split(",") if key.strip()
    ]
    keys = tuple(dict.fromkeys(raw_keys))
    unknown = sorted(set(keys) - set(STRATEGY_TYPES))
    if unknown:
        raise ValueError(f"未知或尚未迁移的策略：{', '.join(unknown)}")
    if not keys:
        raise ValueError("至少需要启用一个已迁移策略")
    return keys


def _run_research_audit(
    settings: Settings,
    repository: MarketDataRepository,
    as_of_date: str | None,
) -> int:
    if as_of_date:
        date.fromisoformat(as_of_date)
    report = ResearchDataService(repository).audit_snapshot(
        history_bars=121,
        as_of_date=as_of_date,
        minimum_coverage=settings.minimum_latest_coverage,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ready else 1


def _run_factor_refresh(
    repository: MarketDataRepository,
    router: ProviderRouter,
    as_of_date: str | None,
) -> int:
    target_date = as_of_date or repository.latest_complete_snapshot_date()
    if target_date is None:
        raise ResearchDataError("没有完整交易日，无法确定复权因子证据日")
    date.fromisoformat(target_date)
    if not repository.has_complete_snapshot(target_date):
        raise ResearchDataError(f"因子证据日 {target_date} 没有完整全市场批次")
    view = AdjustmentFactorConsole()
    view.show_header(target_date, repository.db_path, router.primary.provider_name)
    try:
        report = AdjustmentFactorService(repository, router.primary).refresh_qfq(
            target_date,
            progress_callback=view.update,
        )
    except KeyboardInterrupt:
        view.show_interrupted()
        return 130
    view.show_summary(report)
    return 0 if report.failed_instruments == 0 else 1


def _run_research_scan(
    settings: Settings,
    repository: MarketDataRepository,
    *,
    as_of_date: str | None,
    requested_strategies: list[str] | None,
    output_path: str,
) -> int:
    started = time.monotonic()
    if as_of_date:
        date.fromisoformat(as_of_date)
    keys = _strategy_keys(settings, requested_strategies)
    history_bars = 121
    view = ResearchScanConsole()
    view.show_header(
        requested_date=as_of_date,
        db_path=repository.db_path,
        strategies=keys,
        output_path=output_path,
        history_bars=history_bars,
    )
    try:
        snapshot = ResearchDataService(repository).build_snapshot(
            history_bars=history_bars,
            as_of_date=as_of_date,
            minimum_coverage=settings.minimum_latest_coverage,
            progress_callback=view.update_snapshot,
        )
        view.show_strategy_phase(len(keys))
        collected_results = []
        for index, key in enumerate(keys, 1):
            view.show_strategy_start(index, len(keys), key)
            result = create_strategy(key).run(snapshot)
            collected_results.append(result)
            view.show_strategy_result(result)
        results = tuple(collected_results)
        view.show_report_phase(output_path)
        report_path = write_research_report(
            snapshot=snapshot,
            results=results,
            output_path=output_path,
        )
    except Exception as error:
        view.show_failure(str(error))
        raise
    view.show_summary(
        snapshot=snapshot,
        results=results,
        report_path=report_path,
        elapsed_seconds=time.monotonic() - started,
    )
    return 0


def _run_backtest_audit(
    settings: Settings,
    repository: MarketDataRepository,
    *,
    start_date: str,
    end_date: str,
    history_bars: int,
) -> int:
    date.fromisoformat(start_date)
    date.fromisoformat(end_date)
    if end_date < start_date:
        raise ValueError("结束日期不能早于开始日期")
    if history_bars < 121:
        raise ValueError("历史窗口不能少于 121 个交易日")
    report = BacktestDataService(
        repository,
        minimum_coverage=settings.minimum_latest_coverage,
    ).audit_range(
        start_date=start_date,
        end_date=end_date,
        history_bars=history_bars,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 1


def _run_backtest(
    settings: Settings,
    repository: MarketDataRepository,
    *,
    start_date: str,
    end_date: str,
    requested_strategies: list[str] | None,
    horizons: list[int] | None,
    history_bars: int,
    initial_cash: float,
    holding_period: int,
    exit_retry_days: int,
    max_positions: int,
    commission_rate: float,
    minimum_commission: float,
    slippage_bps: float,
    output_path: str,
) -> int:
    keys = _strategy_keys(settings, requested_strategies)
    config = BacktestConfig(
        start_date=start_date,
        end_date=end_date,
        strategy_keys=keys,
        horizons=tuple(horizons or (1, 5, 10, 20)),
        history_bars=history_bars,
        minimum_coverage=settings.minimum_latest_coverage,
        initial_cash=initial_cash,
        holding_period=holding_period,
        exit_retry_days=exit_retry_days,
        max_positions=max_positions,
        commission_rate=commission_rate,
        minimum_commission=minimum_commission,
        slippage_bps=slippage_bps,
    )
    data = BacktestDataService(
        repository,
        minimum_coverage=config.minimum_coverage,
    )
    result = BacktestEngine(data).run(config)
    report_path = write_backtest_report(
        result=result,
        database_path=repository.db_path,
        output_path=output_path,
    )
    print(
        json.dumps(
            {
                "status": "COMPLETE",
                "start_date": config.start_date,
                "end_date": config.end_date,
                "signal_dates": len(result.signal_dates),
                "signals": len(result.signals),
                "outcomes": len(result.outcomes),
                "calculation_errors": len(result.calculation_errors),
                "portfolios": {
                    item.strategy_key: {
                        "final_equity": item.final_equity,
                        "total_return": item.total_return,
                        "max_drawdown": item.max_drawdown,
                        "filled_buys": item.filled_buys,
                        "filled_sells": item.filled_sells,
                        "open_positions": len(item.open_positions),
                    }
                    for item in result.portfolios
                },
                "report_path": str(report_path),
                "risk_boundary": "历史日线模拟，不代表实盘可成交结果或未来收益。",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> None:
    args = _parse_args()
    try:
        settings = get_settings()
        repository, service = _components(settings)
        today = date.today()
        latest_default_end = today - timedelta(days=1)
        if args.backfill:
            raise SystemExit(
                _run_backfill(
                    settings,
                    repository,
                    service,
                    settings.start_date,
                    latest_default_end.isoformat(),
                )
            )
        if args.audit:
            report = repository.audit()
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
            raise SystemExit(0 if report.ready_for_a_share_research else 1)
        if args.scan_only:
            raise SystemExit(
                _run_research_scan(
                    settings,
                    repository,
                    as_of_date=None,
                    requested_strategies=None,
                    output_path="reports/latest.json",
                )
            )
        if args.command == "research":
            if args.research_command == "factors":
                raise SystemExit(_run_factor_refresh(repository, service.router, args.date))
            if args.research_command == "audit":
                raise SystemExit(_run_research_audit(settings, repository, args.date))
            if args.research_command == "scan":
                raise SystemExit(
                    _run_research_scan(
                        settings,
                        repository,
                        as_of_date=args.date,
                        requested_strategies=args.strategy,
                        output_path=args.output,
                    )
                )
            logger.info("`python main.py research --help` 查看研究命令")
            return
        if args.command == "backtest":
            if args.backtest_command == "audit":
                raise SystemExit(
                    _run_backtest_audit(
                        settings,
                        repository,
                        start_date=args.start,
                        end_date=args.end,
                        history_bars=args.history_bars,
                    )
                )
            if args.backtest_command == "run":
                raise SystemExit(
                    _run_backtest(
                        settings,
                        repository,
                        start_date=args.start,
                        end_date=args.end,
                        requested_strategies=args.strategy,
                        horizons=args.horizon,
                        history_bars=args.history_bars,
                        initial_cash=args.initial_cash,
                        holding_period=args.holding_period,
                        exit_retry_days=args.exit_retry_days,
                        max_positions=args.max_positions,
                        commission_rate=args.commission_rate,
                        minimum_commission=args.minimum_commission,
                        slippage_bps=args.slippage_bps,
                        output_path=args.output,
                    )
                )
            logger.info("`python main.py backtest --help` 查看回测命令")
            return
        if args.command != "data":
            logger.info("`python main.py --help` 查看数据与研究命令")
            return
        if args.data_command == "backfill":
            start = args.start or settings.start_date
            end = args.end or latest_default_end.isoformat()
            raise SystemExit(_run_backfill(settings, repository, service, start, end))
        if args.data_command == "sync":
            if args.days < 1:
                raise ValueError("--days 必须大于 0")
            start = (today - timedelta(days=args.days)).isoformat()
            raise SystemExit(
                _run_backfill(
                    settings,
                    repository,
                    service,
                    start,
                    latest_default_end.isoformat(),
                    incremental_quality=True,
                )
            )
        if args.data_command == "audit":
            print(json.dumps(repository.audit().to_dict(), ensure_ascii=False, indent=2))
            return
        if args.data_command == "repair-quality":
            report = repository.rebuild_data_quality()
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
            raise SystemExit(0 if report.unexplained_gaps_after_repair == 0 else 1)
        if args.data_command == "instruments":
            _print_instruments(repository)
            return
        if args.data_command == "batches":
            _print_batches(repository)
            return
        logger.info("运行 `python main.py data --help` 查看数据命令")
    except (ValueError, sqlite3.DatabaseError) as exc:
        logger.error("请求或本地数据库错误：%s", exc)
        raise SystemExit(2) from exc
    except ResearchDataError as exc:
        logger.error("研究质量闸门未通过：%s", exc)
        raise SystemExit(1) from exc
    except SystemExit:
        raise
    except Exception:
        logger.exception("数据流程失败")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
