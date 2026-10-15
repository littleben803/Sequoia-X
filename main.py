"""Sequoia-X V3 命令行入口：当前阶段专注可信市场数据底座。"""

from __future__ import annotations

import argparse
import json
import signal
import sqlite3
from datetime import date, timedelta

from sequoia_x.core.config import Settings, get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.database import MarketDataRepository
from sequoia_x.data.router import ProviderRouter
from sequoia_x.data.service import DataApplicationService
from sequoia_x.reporting.console import DataConsole

logger = get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sequoia-X V3 市场数据与研究工具")
    legacy = parser.add_mutually_exclusive_group()
    legacy.add_argument("--backfill", action="store_true", help="兼容命令：等价于 data backfill")
    legacy.add_argument("--audit", action="store_true", help="兼容命令：等价于 data audit")
    legacy.add_argument(
        "--scan-only",
        action="store_true",
        help="策略层尚未接入 V3；本参数当前只执行数据审计",
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
        if args.audit or args.scan_only:
            report = repository.audit()
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
            if args.scan_only:
                logger.warning("策略层尚未接入 V3，本次只完成数据审计，没有输出候选股")
            raise SystemExit(0 if report.ready_for_a_share_research else 1)
        if args.command != "data":
            logger.info("V3 当前阶段只开放数据命令；运行 `python main.py data --help` 查看用法")
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
    except SystemExit:
        raise
    except Exception:
        logger.exception("数据流程失败")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
