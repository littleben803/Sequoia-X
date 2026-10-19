"""V3 数据应用服务：按交易日调度、降级、落库并发送结构化事件。"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from datetime import date
from threading import Event, Thread
from typing import TypeVar, cast

from sequoia_x.data.database import MarketDataRepository
from sequoia_x.data.errors import DataNotAvailableError
from sequoia_x.data.models import (
    BenchmarkProgress,
    BenchmarkSyncReport,
    DailyStatus,
    IngestionProgress,
    IngestionReport,
    TradingStatusRecord,
)
from sequoia_x.data.router import ProviderRouter

ProgressCallback = Callable[[IngestionProgress], None]
BenchmarkProgressCallback = Callable[[BenchmarkProgress], None]
T = TypeVar("T")


class DataApplicationService:
    def __init__(self, repository: MarketDataRepository, router: ProviderRouter) -> None:
        self.repository = repository
        self.router = router

    def refresh_reference_data(
        self, start_date: str, end_date: str
    ) -> tuple[int, int, bool, str | None]:
        calendar = self.router.primary.fetch_trade_calendar(start_date, end_date)
        self.repository.save_calendar(calendar)
        try:
            instruments, degraded, warning = self.router.fetch_instruments(end_date)
        except Exception as error:
            self.repository.set_metadata("instrument_directory_status", "FAILED")
            return len(calendar), 0, True, f"统一证券目录两套来源均失败：{error}"
        count = self.repository.upsert_instruments(instruments, end_date)
        self.repository.set_metadata("instrument_directory_status", "COMPLETE")
        return len(calendar), count, degraded, warning

    def sync_benchmark(
        self,
        instrument_id: str,
        start_date: str,
        end_date: str,
        progress_callback: BenchmarkProgressCallback | None = None,
        required_dates: tuple[str, ...] = (),
    ) -> BenchmarkSyncReport:
        """显式联网补齐单个基准；回测本身仍保持纯本地只读。"""
        started = time.monotonic()
        if progress_callback:
            progress_callback(BenchmarkProgress("FETCHING", "正在下载并校验基准日线", 1))
        batch_id = self.repository.start_batch("tencent", "BENCHMARK_BARS", end_date)
        try:
            result = self.router.primary.fetch_benchmark_bars(instrument_id, start_date, end_date)
            returned_dates = {bar.trade_date for bar in result.bars}
            still_missing = tuple(day for day in required_dates if day not in returned_dates)
            if still_missing:
                raise DataNotAvailableError(
                    f"基准 {instrument_id} 来源缺少 {len(still_missing)} 个完整交易日，"
                    f"首个缺口 {still_missing[0]}"
                )
            if progress_callback:
                progress_callback(
                    BenchmarkProgress("WRITING", f"正在原子保存 {len(result.bars):,} 条基准行情", 2)
                )
            inserted, updated = self.repository.save_benchmark_bars(batch_id, result)
        except KeyboardInterrupt:
            self.repository.interrupt_batch(batch_id)
            raise
        except Exception as error:
            self.repository.fail_batch(batch_id, error)
            raise
        if progress_callback:
            progress_callback(BenchmarkProgress("COMPLETE", "基准行情同步完成", 3))
        return BenchmarkSyncReport(
            instrument_id=result.instrument.instrument_id,
            name=result.instrument.name,
            start_date=result.start_date,
            end_date=result.end_date,
            fetched_rows=len(result.bars),
            inserted_rows=inserted,
            updated_rows=updated,
            elapsed_seconds=time.monotonic() - started,
        )

    def sync_supplemental_data(
        self,
        start_date: str,
        end_date: str,
        progress_callback: BenchmarkProgressCallback | None = None,
    ) -> tuple[BenchmarkSyncReport, ...]:
        """统一增量补齐数据底座附加能力；新增能力只接入这里。"""
        reports = []
        for instrument_id in ("SSE:000985",):
            missing = self.repository.missing_complete_bar_dates(
                instrument_id, start_date, end_date
            )
            if not missing:
                continue
            reports.append(
                self.sync_benchmark(
                    instrument_id,
                    missing[0],
                    end_date,
                    progress_callback,
                    required_dates=missing,
                )
            )
        return tuple(reports)

    def backfill(
        self,
        start_date: str,
        end_date: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> IngestionReport:
        started = time.monotonic()
        end_date = end_date or date.today().isoformat()
        recovered_stale_batches = self.repository.recover_stale_batches()
        reference_started = time.monotonic()
        self._emit(
            progress_callback,
            phase="REFERENCE_DATA",
            step="刷新交易日历与证券主数据",
            message="正在连接交易所官方数据源",
            elapsed=0,
        )
        reference_result = self._run_with_heartbeat(
            lambda: self.refresh_reference_data(start_date, end_date),
            lambda elapsed: self._emit(
                progress_callback,
                phase="REFERENCE_DATA",
                step="刷新交易日历与证券主数据",
                message="仍在等待数据源响应",
                elapsed=elapsed,
                is_heartbeat=True,
            ),
        )
        _, initial_instruments, reference_degraded, reference_warning = reference_result
        trade_dates = self.repository.trading_dates(start_date, end_date)
        successful = degraded = skipped = pending = failed = rows_written = instruments_upserted = 0
        failed_dates: list[str] = []
        pending_dates: list[str] = []
        errors: list[str] = []
        security_type_counts: Counter[str] = Counter()
        if reference_warning:
            errors.append(reference_warning)
        if reference_degraded:
            errors.append("证券目录使用了 Baostock 降级结果")
        self._emit(
            progress_callback,
            phase="REFERENCE_DATA",
            step="刷新交易日历与证券主数据",
            message=f"参考数据准备完成，共 {len(trade_dates):,} 个交易日",
            elapsed=time.monotonic() - reference_started,
        )

        for index, trade_date in enumerate(trade_dates, 1):
            if self.repository.has_complete_snapshot(trade_date):
                skipped += 1
                self._emit(
                    progress_callback,
                    phase="DAILY_SNAPSHOT",
                    step="检查本地批次",
                    trade_date=trade_date,
                    processed=index,
                    total=len(trade_dates),
                    successful=successful,
                    degraded=degraded,
                    skipped=skipped,
                    failed=failed,
                    pending=pending,
                    rows_written=rows_written,
                    message="已有完整批次，跳过下载",
                )
                continue
            batch_id = self.repository.start_batch(
                self.router.primary.provider_name, "DAILY_SNAPSHOT", trade_date
            )
            step_started = time.monotonic()
            try:
                self._emit(
                    progress_callback,
                    phase="DAILY_SNAPSHOT",
                    step="下载并解析全市场盘后包",
                    trade_date=trade_date,
                    processed=index - 1,
                    total=len(trade_dates),
                    successful=successful,
                    degraded=degraded,
                    skipped=skipped,
                    failed=failed,
                    pending=pending,
                    rows_written=rows_written,
                    message="正在请求、校验、解压并解析盘后包",
                )
                snapshot = self._run_with_heartbeat(
                    lambda: self.router.primary.fetch_daily_snapshot(trade_date),
                    lambda elapsed: self._emit(
                        progress_callback,
                        phase="DAILY_SNAPSHOT",
                        step="下载并解析全市场盘后包",
                        trade_date=trade_date,
                        processed=index - 1,
                        total=len(trade_dates),
                        successful=successful,
                        degraded=degraded,
                        skipped=skipped,
                        failed=failed,
                        pending=pending,
                        rows_written=rows_written,
                        message="仍在下载或解析，任务保持运行",
                        elapsed=elapsed,
                        is_heartbeat=True,
                    ),
                )
                self._emit(
                    progress_callback,
                    phase="DAILY_SNAPSHOT",
                    step="保存当天市场数据",
                    trade_date=trade_date,
                    processed=index - 1,
                    total=len(trade_dates),
                    successful=successful,
                    degraded=degraded,
                    skipped=skipped,
                    failed=failed,
                    pending=pending,
                    rows_written=rows_written,
                    type_counts=snapshot.metadata.get("security_type_counts", {}),
                    message=f"已解析 {len(snapshot.bars):,} 条市场数据，准备保存",
                )
                instruments = {item.instrument_id: item for item in snapshot.instruments}
                write_state: dict[str, str | int | None] = {
                    "name": None,
                    "code": None,
                    "type": None,
                    "processed": 0,
                    "total": 0,
                }

                def report_write_progress(
                    write_stage: str,
                    step_processed: int,
                    step_total: int,
                    instrument_id: str,
                ) -> None:
                    instrument = instruments.get(instrument_id)
                    stage_message = (
                        "正在保存证券名称和类型"
                        if write_stage == "INSTRUMENTS"
                        else "正在保存当天行情"
                    )
                    write_state.update(
                        name=instrument.name if instrument else None,
                        code=(
                            instrument.security_code if instrument else instrument_id.split(":")[-1]
                        ),
                        type=instrument.security_type.value if instrument else None,
                        processed=step_processed,
                        total=step_total,
                    )
                    self._emit(
                        progress_callback,
                        phase="DAILY_SNAPSHOT",
                        step="保存当天市场数据",
                        trade_date=trade_date,
                        processed=index - 1,
                        total=len(trade_dates),
                        successful=successful,
                        degraded=degraded,
                        skipped=skipped,
                        failed=failed,
                        pending=pending,
                        rows_written=rows_written,
                        message=stage_message,
                        current_instrument_name=instrument.name if instrument else None,
                        current_instrument_code=(
                            instrument.security_code if instrument else instrument_id.split(":")[-1]
                        ),
                        current_instrument_type=(
                            instrument.security_type.value if instrument else None
                        ),
                        step_processed=step_processed,
                        step_total=step_total,
                    )

                inserted, updated = self._run_with_heartbeat(
                    lambda: self.repository.save_snapshot(
                        batch_id,
                        snapshot,
                        progress_callback=report_write_progress,
                    ),
                    lambda elapsed: self._emit(
                        progress_callback,
                        phase="DAILY_SNAPSHOT",
                        step="保存当天市场数据",
                        trade_date=trade_date,
                        processed=index - 1,
                        total=len(trade_dates),
                        successful=successful,
                        degraded=degraded,
                        skipped=skipped,
                        failed=failed,
                        pending=pending,
                        rows_written=rows_written,
                        message="当天市场数据仍在保存，请勿关闭终端",
                        elapsed=elapsed,
                        is_heartbeat=True,
                        current_instrument_name=cast(str | None, write_state["name"]),
                        current_instrument_code=cast(str | None, write_state["code"]),
                        current_instrument_type=cast(str | None, write_state["type"]),
                        step_processed=cast(int, write_state["processed"]),
                        step_total=cast(int, write_state["total"]),
                    ),
                )
                rows_written += inserted + updated
                instruments_upserted += len(snapshot.instruments)
                successful += 1
                self._emit(
                    progress_callback,
                    phase="DAILY_SNAPSHOT",
                    step="确认无成交 A 股状态",
                    trade_date=trade_date,
                    processed=index - 1,
                    total=len(trade_dates),
                    successful=successful,
                    degraded=degraded,
                    skipped=skipped,
                    failed=failed,
                    pending=pending,
                    rows_written=rows_written,
                    message="正在区分停牌、未上市和来源不可用",
                )
                self._run_with_heartbeat(
                    lambda: self._confirm_missing_a_shares(trade_date),
                    lambda elapsed: self._emit(
                        progress_callback,
                        phase="DAILY_SNAPSHOT",
                        step="确认无成交 A 股状态",
                        trade_date=trade_date,
                        processed=index - 1,
                        total=len(trade_dates),
                        successful=successful,
                        degraded=degraded,
                        skipped=skipped,
                        failed=failed,
                        pending=pending,
                        rows_written=rows_written,
                        message="备源状态确认仍在运行",
                        elapsed=elapsed,
                        is_heartbeat=True,
                    ),
                )
                message = "主数据源完整写入"
                type_counts = snapshot.metadata.get("security_type_counts", {})
                security_type_counts.update(type_counts)
            except DataNotAvailableError as primary_error:
                if trade_date >= date.today().isoformat():
                    self.repository.skip_unpublished_batch(batch_id, str(primary_error))
                    pending += 1
                    pending_dates.append(trade_date)
                    message = "盘后包尚未发布，已标记为等待发布"
                    type_counts = {}
                else:
                    self.repository.fail_batch(batch_id, primary_error)
                    failed += 1
                    failed_dates.append(trade_date)
                    errors.append(f"{trade_date} 历史盘后包缺失：{primary_error}")
                    message = "历史盘后包缺失，已记录失败并继续"
                    type_counts = {}
            except Exception as primary_error:
                self.repository.fail_batch(batch_id, primary_error)
                failed += 1
                failed_dates.append(trade_date)
                errors.append(f"{trade_date} 全市场盘后包失败：{primary_error}")
                message = "盘后包失败，已快速记录并继续；未启动逐股降级"
                type_counts = {}
            except KeyboardInterrupt:
                self.repository.interrupt_batch(batch_id)
                self._emit(
                    progress_callback,
                    phase="INTERRUPTED",
                    step="安全停止",
                    trade_date=trade_date,
                    processed=index - 1,
                    total=len(trade_dates),
                    successful=successful,
                    degraded=degraded,
                    skipped=skipped,
                    failed=failed,
                    pending=pending,
                    rows_written=rows_written,
                    message="当前批次已标记中断，下次运行会自动续跑",
                    elapsed=time.monotonic() - step_started,
                )
                raise
            self._emit(
                progress_callback,
                phase="DAILY_SNAPSHOT",
                step="完成交易日处理",
                trade_date=trade_date,
                processed=index,
                total=len(trade_dates),
                successful=successful,
                degraded=degraded,
                skipped=skipped,
                failed=failed,
                pending=pending,
                rows_written=rows_written,
                type_counts=type_counts,
                message=message,
                elapsed=time.monotonic() - step_started,
            )
        return IngestionReport(
            requested_dates=len(trade_dates),
            processed_dates=len(trade_dates),
            successful_dates=successful,
            degraded_dates=degraded,
            skipped_dates=skipped,
            failed_dates=failed,
            rows_written=rows_written,
            instruments_upserted=max(instruments_upserted, initial_instruments),
            elapsed_seconds=time.monotonic() - started,
            pending_dates=pending,
            recovered_stale_batches=recovered_stale_batches,
            security_type_counts=dict(security_type_counts),
            failed_trade_dates=tuple(failed_dates),
            pending_trade_dates=tuple(pending_dates),
            errors=tuple(errors),
        )

    def _confirm_missing_a_shares(self, trade_date: str) -> None:
        missing = self.repository.listed_a_share_missing_on(trade_date)
        if not missing:
            return
        supported = tuple(item for item in missing if item.exchange.value in {"SSE", "SZSE"})
        records = []
        if supported:
            try:
                records = list(self.router.confirm_trading_status(supported, trade_date))
            except Exception as error:
                records = [
                    TradingStatusRecord(
                        instrument_id=item.instrument_id,
                        trade_date=trade_date,
                        status=DailyStatus.SOURCE_UNAVAILABLE,
                        provider=self.router.fallback.provider_name,
                        evidence={"error": str(error)},
                    )
                    for item in supported
                ]
        known = {record.instrument_id for record in records}
        records.extend(
            TradingStatusRecord(
                instrument_id=item.instrument_id,
                trade_date=trade_date,
                status=DailyStatus.MISSING_UNEXPLAINED,
                provider="a-stock-data",
                evidence={"reason": "盘后包无记录且当前没有该市场的权威停牌备源"},
            )
            for item in missing
            if item.instrument_id not in known
        )
        self.repository.save_daily_status(tuple(records))

    @staticmethod
    def _emit(
        callback: ProgressCallback | None,
        *,
        phase: str,
        step: str,
        trade_date: str | None = None,
        processed: int = 0,
        total: int = 0,
        successful: int = 0,
        degraded: int = 0,
        skipped: int = 0,
        failed: int = 0,
        pending: int = 0,
        rows_written: int = 0,
        type_counts: dict[str, int] | None = None,
        message: str = "",
        elapsed: float = 0,
        is_heartbeat: bool = False,
        current_instrument_name: str | None = None,
        current_instrument_code: str | None = None,
        current_instrument_type: str | None = None,
        step_processed: int = 0,
        step_total: int = 0,
    ) -> None:
        if callback:
            callback(
                IngestionProgress(
                    phase=phase,
                    step=step,
                    current_date=trade_date,
                    processed_dates=processed,
                    total_dates=total,
                    successful_dates=successful,
                    degraded_dates=degraded,
                    skipped_dates=skipped,
                    failed_dates=failed,
                    rows_written=rows_written,
                    pending_dates=pending,
                    type_counts=type_counts or {},
                    message=message,
                    step_elapsed_seconds=elapsed,
                    is_heartbeat=is_heartbeat,
                    current_instrument_name=current_instrument_name,
                    current_instrument_code=current_instrument_code,
                    current_instrument_type=current_instrument_type,
                    step_processed=step_processed,
                    step_total=step_total,
                )
            )

    @staticmethod
    def _run_with_heartbeat(
        operation: Callable[[], T], heartbeat: Callable[[float], None], interval: float = 15
    ) -> T:
        stopped = Event()
        started = time.monotonic()

        def pulse() -> None:
            while not stopped.wait(interval):
                heartbeat(time.monotonic() - started)

        thread = Thread(target=pulse, name="data-progress-heartbeat", daemon=True)
        thread.start()
        try:
            return operation()
        finally:
            stopped.set()
            thread.join(timeout=1)
