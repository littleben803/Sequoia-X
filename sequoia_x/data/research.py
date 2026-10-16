"""V3 研究数据层：统一数据日、质量闸门和前复权快照。"""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Protocol

from sequoia_x.data.database import MarketDataRepository
from sequoia_x.data.models import Adjustment, AdjustmentFactorRecord, V3AuditReport


class ResearchDataError(RuntimeError):
    """研究快照无法在可信边界内生成。"""


@dataclass(frozen=True)
class ResearchBar:
    instrument_id: str
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    previous_close: float | None
    volume: float
    amount: float
    adjustment_factor: float
    price_adjustment: str = "QFQ"


@dataclass(frozen=True)
class ResearchExclusion:
    instrument_id: str
    reason: str


@dataclass(frozen=True)
class ResearchSnapshot:
    as_of_date: str
    bars: Mapping[str, tuple[ResearchBar, ...]]
    exclusions: tuple[ResearchExclusion, ...]
    requested_instruments: int
    eligible_instruments: int
    coverage: float
    audit: V3AuditReport
    price_adjustment: str = "QFQ"


@dataclass(frozen=True)
class ResearchSnapshotAudit:
    ready: bool
    as_of_date: str | None
    requested_instruments: int
    eligible_instruments: int
    coverage: float
    minimum_coverage: float
    exclusion_counts: Mapping[str, int]
    base_audit: V3AuditReport
    problems: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["exclusion_counts"] = dict(self.exclusion_counts)
        data["base_audit"]["problems"] = list(self.base_audit.problems)
        data["problems"] = list(self.problems)
        return data


@dataclass(frozen=True)
class ResearchSnapshotProgress:
    phase: str
    message: str
    as_of_date: str | None = None
    requested_instruments: int = 0
    eligible_instruments: int = 0
    excluded_instruments: int = 0
    coverage: float = 0.0


ResearchSnapshotProgressCallback = Callable[[ResearchSnapshotProgress], None]


@dataclass(frozen=True)
class AdjustmentFactorRefreshReport:
    as_of_date: str
    requested_instruments: int
    successful_instruments: int
    failed_instruments: int
    unsupported_instruments: int
    factor_rows_written: int
    elapsed_seconds: float
    failures: tuple[str, ...]


@dataclass(frozen=True)
class AdjustmentFactorProgress:
    phase: str
    as_of_date: str
    current_instrument_id: str | None
    current_name: str | None
    processed_instruments: int
    requested_instruments: int
    successful_instruments: int
    failed_instruments: int
    unsupported_instruments: int
    factor_rows_written: int
    message: str


AdjustmentFactorProgressCallback = Callable[[AdjustmentFactorProgress], None]


class AdjustmentFactorProvider(Protocol):
    provider_name: str

    def fetch_adjustment_factors(
        self, code: str, kind: str = "qfq"
    ) -> tuple[dict, ...]: ...


class ResearchRepository:
    """只读研究查询；不参与 Provider 调度或写入。"""

    def __init__(self, market_data: MarketDataRepository) -> None:
        self.market_data = market_data

    def listed_a_share_ids(self, as_of_date: str) -> tuple[str, ...]:
        with self.market_data.connect() as conn:
            rows = conn.execute(
                """
                SELECT instrument_id
                FROM instruments
                WHERE security_type='STOCK'
                  AND exchange<>'BSE'
                  AND COALESCE(
                        security_subtype,
                        CASE
                            WHEN exchange='SSE'
                             AND CAST(json_extract(metadata_json, '$.STOCK_TYPE') AS TEXT)='8'
                            THEN 'CDR'
                        END,
                        ''
                      )<>'CDR'
                  AND COALESCE(code_valid_from, list_date, first_seen_date)<=?
                  AND (COALESCE(code_valid_to, delist_date) IS NULL
                       OR COALESCE(code_valid_to, delist_date)>=?)
                ORDER BY instrument_id
                """,
                (as_of_date, as_of_date),
            ).fetchall()
        return tuple(row[0] for row in rows)

    def load_a_share_lifecycles(self) -> tuple[tuple[str, str, str | None], ...]:
        """一次读取回测所需 A 股身份及有效期，不绑定当前日期。"""
        with self.market_data.connect() as conn:
            rows = conn.execute(
                """
                SELECT instrument_id,
                       COALESCE(code_valid_from, list_date, first_seen_date),
                       COALESCE(code_valid_to, delist_date)
                FROM instruments
                WHERE security_type='STOCK'
                  AND exchange<>'BSE'
                  AND COALESCE(
                        security_subtype,
                        CASE
                            WHEN exchange='SSE'
                             AND CAST(json_extract(metadata_json, '$.STOCK_TYPE') AS TEXT)='8'
                            THEN 'CDR'
                        END,
                        ''
                      )<>'CDR'
                ORDER BY instrument_id
                """
            ).fetchall()
        return tuple((row[0], row[1], row[2]) for row in rows)

    def listed_a_shares(
        self, as_of_date: str
    ) -> tuple[tuple[str, str, str, str, str | None], ...]:
        with self.market_data.connect() as conn:
            rows = conn.execute(
                """
                SELECT instrument_id, exchange, security_code, name,
                       COALESCE(
                           security_subtype,
                           CASE
                               WHEN exchange='SSE'
                                AND CAST(json_extract(metadata_json, '$.STOCK_TYPE') AS TEXT)='8'
                               THEN 'CDR'
                           END
                       ) AS research_subtype
                FROM instruments
                WHERE security_type='STOCK'
                  AND COALESCE(code_valid_from, list_date, first_seen_date)<=?
                  AND (COALESCE(code_valid_to, delist_date) IS NULL
                       OR COALESCE(code_valid_to, delist_date)>=?)
                ORDER BY instrument_id
                """,
                (as_of_date, as_of_date),
            ).fetchall()
        return tuple((row[0], row[1], row[2], row[3], row[4]) for row in rows)

    def load_recent_raw_bars(
        self,
        as_of_date: str,
        history_bars: int,
        instrument_ids: tuple[str, ...],
    ) -> dict[str, tuple[tuple, ...]]:
        if history_bars < 1:
            raise ValueError("history_bars 必须大于 0")
        if not instrument_ids:
            return {}
        grouped: dict[str, list[tuple]] = {}
        with self.market_data.connect() as conn:
            for start in range(0, len(instrument_ids), 500):
                chunk = instrument_ids[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    WITH ranked AS (
                        SELECT d.research_instrument_id, d.trade_date, d.open, d.high,
                               d.low, d.close, d.previous_close, d.volume, d.amount,
                               ROW_NUMBER() OVER (
                                   PARTITION BY d.research_instrument_id
                                   ORDER BY d.trade_date DESC
                               ) AS row_number
                        FROM daily_bars_research AS d
                        WHERE d.trade_date<=?
                          AND d.research_instrument_id IN ({placeholders})
                    )
                    SELECT research_instrument_id, trade_date, open, high, low, close,
                           previous_close, volume, amount
                    FROM ranked
                    WHERE row_number<=?
                    ORDER BY research_instrument_id, trade_date
                    """,
                    (as_of_date, *chunk, history_bars),
                ).fetchall()
                for row in rows:
                    grouped.setdefault(row[0], []).append(tuple(row[1:]))
        return {key: tuple(value) for key, value in grouped.items()}

    def load_backtest_raw_bars(
        self,
        end_date: str,
        bars_per_instrument: int,
        instrument_ids: tuple[str, ...],
    ) -> dict[str, tuple[tuple, ...]]:
        """批量读取截止结束日每只证券最近 N 根研究行情。"""
        return self.load_recent_raw_bars(end_date, bars_per_instrument, instrument_ids)

    def load_qfq_factors(self, as_of_date: str) -> dict[str, tuple[tuple[str, float], ...]]:
        with self.market_data.connect() as conn:
            rows = conn.execute(
                """
                WITH preferred AS (
                    SELECT f.instrument_id, f.effective_date, f.factor,
                           ROW_NUMBER() OVER (
                               PARTITION BY f.instrument_id, f.effective_date
                               ORDER BY f.fetched_at DESC, f.provider
                           ) AS provider_rank
                    FROM adjustment_factors AS f
                    JOIN ingestion_batches AS b
                      ON b.batch_id=f.source_batch_id
                    WHERE f.kind='QFQ' AND f.effective_date<=?
                      AND b.status='COMPLETE' AND b.trade_date>=?
                )
                SELECT instrument_id, effective_date, factor
                FROM preferred
                WHERE provider_rank=1
                ORDER BY instrument_id, effective_date
                """,
                (as_of_date, as_of_date),
            ).fetchall()
        grouped: dict[str, list[tuple[str, float]]] = {}
        for instrument_id, effective_date, factor in rows:
            grouped.setdefault(instrument_id, []).append((effective_date, factor))
        return {key: tuple(value) for key, value in grouped.items()}

    def load_qfq_factor_evidence(
        self, end_date: str
    ) -> dict[str, tuple[tuple[str, float, str, str, str], ...]]:
        """批量读取因子及证据日期，供历史日按原规则选择 Provider。"""
        with self.market_data.connect() as conn:
            rows = conn.execute(
                """
                SELECT f.instrument_id, f.effective_date, f.factor, b.trade_date,
                       f.fetched_at, f.provider
                FROM adjustment_factors AS f
                JOIN ingestion_batches AS b ON b.batch_id=f.source_batch_id
                WHERE f.kind='QFQ' AND f.effective_date<=? AND b.status='COMPLETE'
                ORDER BY f.instrument_id, f.effective_date, f.fetched_at DESC, f.provider
                """,
                (end_date,),
            ).fetchall()
        grouped: dict[str, list[tuple[str, float, str, str, str]]] = {}
        for instrument_id, effective_date, factor, evidence_date, fetched_at, provider in rows:
            if evidence_date is None:
                continue
            grouped.setdefault(instrument_id, []).append(
                (effective_date, factor, evidence_date, fetched_at, provider)
            )
        return {key: tuple(value) for key, value in grouped.items()}


class AdjustmentFactorService:
    """按证券刷新沪深 A 股前复权因子，每只证券独立批次。"""

    def __init__(
        self,
        repository: MarketDataRepository,
        provider: AdjustmentFactorProvider,
    ) -> None:
        self.market_data = repository
        self.research = ResearchRepository(repository)
        self.provider = provider

    def refresh_qfq(
        self,
        as_of_date: str,
        progress_callback: AdjustmentFactorProgressCallback | None = None,
    ) -> AdjustmentFactorRefreshReport:
        started = time.monotonic()
        instruments = self.research.listed_a_shares(as_of_date)
        successful = failed = unsupported = rows_written = 0
        failures: list[str] = []
        total = len(instruments)
        self._emit(
            progress_callback,
            phase="PREPARING",
            as_of_date=as_of_date,
            total=total,
            successful=successful,
            failed=failed,
            unsupported=unsupported,
            rows_written=rows_written,
            message=f"已载入 {total:,} 只研究日有效 A 股",
        )
        for index, (instrument_id, exchange, code, name, subtype) in enumerate(instruments, 1):
            if exchange == "BSE" or subtype == "CDR":
                unsupported += 1
                failures.append(f"{instrument_id}: UNSUPPORTED_FACTOR_SOURCE")
                reason = "中国存托凭证" if subtype == "CDR" else "北交所"
                self._emit(
                    progress_callback,
                    phase="REFRESHING",
                    as_of_date=as_of_date,
                    total=total,
                    processed=index,
                    successful=successful,
                    failed=failed,
                    unsupported=unsupported,
                    rows_written=rows_written,
                    instrument_id=instrument_id,
                    name=name,
                    message=f"当前因子源不支持{reason}，已明确跳过",
                )
                continue
            batch_id = self.market_data.start_batch(
                self.provider.provider_name,
                "ADJUSTMENT_FACTOR",
                as_of_date,
            )
            self._emit(
                progress_callback,
                phase="REFRESHING",
                as_of_date=as_of_date,
                total=total,
                processed=index - 1,
                successful=successful,
                failed=failed,
                unsupported=unsupported,
                rows_written=rows_written,
                instrument_id=instrument_id,
                name=name,
                message="正在请求并校验完整 QFQ 因子序列",
            )
            try:
                prefix = "sh" if exchange == "SSE" else "sz"
                source_rows = self.provider.fetch_adjustment_factors(f"{prefix}{code}", "qfq")
                if not source_rows:
                    raise ResearchDataError("EMPTY_QFQ_FACTORS")
                records = tuple(
                    AdjustmentFactorRecord(
                        instrument_id=instrument_id,
                        effective_date=str(row["date"]),
                        factor=float(row["factor"]),
                        kind=Adjustment.QFQ,
                        provider=self.provider.provider_name,
                        raw_payload=dict(row),
                    )
                    for row in source_rows
                )
                inserted, updated = self.market_data.save_adjustment_factors(batch_id, records)
                rows_written += inserted + updated
                successful += 1
                message = f"成功保存 {inserted + updated:,} 条因子"
            except KeyboardInterrupt:
                self.market_data.interrupt_batch(batch_id, "复权因子刷新被用户中断")
                self._emit(
                    progress_callback,
                    phase="INTERRUPTED",
                    as_of_date=as_of_date,
                    total=total,
                    processed=index - 1,
                    successful=successful,
                    failed=failed,
                    unsupported=unsupported,
                    rows_written=rows_written,
                    instrument_id=instrument_id,
                    name=name,
                    message="已安全中断，当前批次已记录",
                )
                raise
            except Exception as error:
                self.market_data.fail_batch(batch_id, error)
                failed += 1
                failures.append(f"{instrument_id}: {error}")
                message = f"刷新失败：{error}"
            self._emit(
                progress_callback,
                phase="REFRESHING",
                as_of_date=as_of_date,
                total=total,
                processed=index,
                successful=successful,
                failed=failed,
                unsupported=unsupported,
                rows_written=rows_written,
                instrument_id=instrument_id,
                name=name,
                message=message,
            )
        report = AdjustmentFactorRefreshReport(
            as_of_date=as_of_date,
            requested_instruments=total,
            successful_instruments=successful,
            failed_instruments=failed,
            unsupported_instruments=unsupported,
            factor_rows_written=rows_written,
            elapsed_seconds=time.monotonic() - started,
            failures=tuple(failures),
        )
        self._emit(
            progress_callback,
            phase="COMPLETE",
            as_of_date=as_of_date,
            total=total,
            processed=total,
            successful=successful,
            failed=failed,
            unsupported=unsupported,
            rows_written=rows_written,
            message="前复权因子刷新完成",
        )
        return report

    @staticmethod
    def _emit(
        callback: AdjustmentFactorProgressCallback | None,
        *,
        phase: str,
        as_of_date: str,
        total: int,
        successful: int,
        failed: int,
        unsupported: int,
        rows_written: int,
        message: str,
        processed: int = 0,
        instrument_id: str | None = None,
        name: str | None = None,
    ) -> None:
        if callback:
            callback(
                AdjustmentFactorProgress(
                    phase=phase,
                    as_of_date=as_of_date,
                    current_instrument_id=instrument_id,
                    current_name=name,
                    processed_instruments=processed,
                    requested_instruments=total,
                    successful_instruments=successful,
                    failed_instruments=failed,
                    unsupported_instruments=unsupported,
                    factor_rows_written=rows_written,
                    message=message,
                )
            )


class ResearchDataService:
    """生成可重复、统一截面日的 A 股前复权研究快照。"""

    def __init__(self, repository: MarketDataRepository) -> None:
        self.market_data = repository
        self.research = ResearchRepository(repository)

    def build_snapshot(
        self,
        *,
        history_bars: int,
        as_of_date: str | None = None,
        minimum_coverage: float = 0.95,
        progress_callback: ResearchSnapshotProgressCallback | None = None,
        base_audit: V3AuditReport | None = None,
    ) -> ResearchSnapshot:
        if not 0 < minimum_coverage <= 1:
            raise ValueError("minimum_coverage 必须在 (0, 1] 范围内")
        self._emit_snapshot_progress(
            progress_callback,
            phase="AUDITING",
            message="正在审计基础数据完整性",
            as_of_date=as_of_date,
        )
        audit = base_audit or self.market_data.audit()
        target_date = self._require_target_date(audit, as_of_date)
        self._emit_snapshot_progress(
            progress_callback,
            phase="DATE_RESOLVED",
            message=f"统一研究日已确定为 {target_date}",
            as_of_date=target_date,
        )
        snapshot = self._assemble_snapshot(
            audit,
            target_date,
            history_bars,
            progress_callback=progress_callback,
        )
        if snapshot.coverage < minimum_coverage:
            raise ResearchDataError(
                f"研究快照覆盖率 {snapshot.coverage:.2%} "
                f"低于闸值 {minimum_coverage:.2%}"
            )
        self._emit_snapshot_progress(
            progress_callback,
            phase="READY",
            message=f"质量闸门通过（最低覆盖率 {minimum_coverage:.2%}）",
            as_of_date=target_date,
            requested=snapshot.requested_instruments,
            eligible=snapshot.eligible_instruments,
            excluded=len(snapshot.exclusions),
            coverage=snapshot.coverage,
        )
        return snapshot

    def audit_snapshot(
        self,
        *,
        history_bars: int,
        as_of_date: str | None = None,
        minimum_coverage: float = 0.95,
        base_audit: V3AuditReport | None = None,
    ) -> ResearchSnapshotAudit:
        if not 0 < minimum_coverage <= 1:
            raise ValueError("minimum_coverage 必须在 (0, 1] 范围内")
        audit = base_audit or self.market_data.audit()
        try:
            target_date = self._require_target_date(audit, as_of_date)
        except ResearchDataError as error:
            return ResearchSnapshotAudit(
                ready=False,
                as_of_date=as_of_date or audit.latest_complete_date,
                requested_instruments=0,
                eligible_instruments=0,
                coverage=0.0,
                minimum_coverage=minimum_coverage,
                exclusion_counts={},
                base_audit=audit,
                problems=(str(error),),
            )
        snapshot = self._assemble_snapshot(audit, target_date, history_bars)
        problems = []
        if snapshot.coverage < minimum_coverage:
            problems.append(
                f"研究快照覆盖率 {snapshot.coverage:.2%} "
                f"低于闸值 {minimum_coverage:.2%}"
            )
        return ResearchSnapshotAudit(
            ready=not problems,
            as_of_date=target_date,
            requested_instruments=snapshot.requested_instruments,
            eligible_instruments=snapshot.eligible_instruments,
            coverage=snapshot.coverage,
            minimum_coverage=minimum_coverage,
            exclusion_counts=dict(Counter(item.reason for item in snapshot.exclusions)),
            base_audit=audit,
            problems=tuple(problems),
        )

    def _require_target_date(
        self, audit: V3AuditReport, as_of_date: str | None
    ) -> str:
        if not audit.ready_for_a_share_research:
            raise ResearchDataError("数据质量闸门未通过：" + "；".join(audit.problems))
        latest = audit.latest_complete_date
        if latest is None:
            raise ResearchDataError("没有可用的完整交易日")
        target_date = as_of_date or latest
        if target_date > latest:
            raise ResearchDataError(
                f"请求研究日 {target_date} 晚于最新完整交易日 {latest}"
            )
        if not self.market_data.has_complete_snapshot(target_date):
            raise ResearchDataError(f"研究日 {target_date} 没有完整全市场批次")
        return target_date

    def _assemble_snapshot(
        self,
        audit: V3AuditReport,
        target_date: str,
        history_bars: int,
        progress_callback: ResearchSnapshotProgressCallback | None = None,
    ) -> ResearchSnapshot:
        self._emit_snapshot_progress(
            progress_callback,
            phase="LOADING_UNIVERSE",
            message="正在加载研究日有效 A 股列表",
            as_of_date=target_date,
        )
        universe = self.research.listed_a_share_ids(target_date)
        self._emit_snapshot_progress(
            progress_callback,
            phase="LOADING_FACTORS",
            message=f"已加载 {len(universe):,} 只证券，正在匹配 QFQ 因子",
            as_of_date=target_date,
            requested=len(universe),
        )
        factors = self.research.load_qfq_factors(target_date)
        factor_covered_ids = tuple(
            instrument_id for instrument_id in universe if instrument_id in factors
        )
        self._emit_snapshot_progress(
            progress_callback,
            phase="LOADING_BARS",
            message=(
                f"{len(factor_covered_ids):,} 只证券具备因子，"
                f"正在加载最近 {history_bars} 个交易日行情"
            ),
            as_of_date=target_date,
            requested=len(universe),
            eligible=len(factor_covered_ids),
            excluded=len(universe) - len(factor_covered_ids),
        )
        raw_bars = self.research.load_recent_raw_bars(
            target_date,
            history_bars,
            factor_covered_ids,
        )
        self._emit_snapshot_progress(
            progress_callback,
            phase="ADJUSTING",
            message="正在校验统一数据日并生成前复权研究快照",
            as_of_date=target_date,
            requested=len(universe),
        )
        adjusted: dict[str, tuple[ResearchBar, ...]] = {}
        exclusions: list[ResearchExclusion] = []

        for instrument_id in universe:
            factor_rows = factors.get(instrument_id, ())
            if not factor_rows:
                exclusions.append(ResearchExclusion(instrument_id, "MISSING_QFQ_FACTORS"))
                continue
            bars = raw_bars.get(instrument_id, ())
            if not bars or bars[-1][0] != target_date:
                exclusions.append(ResearchExclusion(instrument_id, "NO_BAR_ON_RESEARCH_DATE"))
                continue
            try:
                adjusted[instrument_id] = self.apply_qfq(instrument_id, bars, factor_rows)
            except ResearchDataError as error:
                exclusions.append(ResearchExclusion(instrument_id, str(error)))

        requested = len(universe)
        eligible = len(adjusted)
        coverage = eligible / requested if requested else 0.0
        return ResearchSnapshot(
            as_of_date=target_date,
            bars=MappingProxyType(adjusted),
            exclusions=tuple(exclusions),
            requested_instruments=requested,
            eligible_instruments=eligible,
            coverage=coverage,
            audit=audit,
        )

    @staticmethod
    def _emit_snapshot_progress(
        callback: ResearchSnapshotProgressCallback | None,
        *,
        phase: str,
        message: str,
        as_of_date: str | None,
        requested: int = 0,
        eligible: int = 0,
        excluded: int = 0,
        coverage: float = 0.0,
    ) -> None:
        if callback:
            callback(
                ResearchSnapshotProgress(
                    phase=phase,
                    message=message,
                    as_of_date=as_of_date,
                    requested_instruments=requested,
                    eligible_instruments=eligible,
                    excluded_instruments=excluded,
                    coverage=coverage,
                )
            )

    @staticmethod
    def apply_qfq(
        instrument_id: str,
        bars: tuple[tuple, ...],
        factors: tuple[tuple[str, float], ...],
    ) -> tuple[ResearchBar, ...]:
        output: list[ResearchBar] = []
        factor_index = 0
        current_factor: float | None = None
        for bar in bars:
            trade_date = bar[0]
            while factor_index < len(factors) and factors[factor_index][0] <= trade_date:
                current_factor = factors[factor_index][1]
                factor_index += 1
            if current_factor is None:
                raise ResearchDataError("QFQ_FACTOR_DOES_NOT_COVER_HISTORY")
            if not math.isfinite(current_factor) or current_factor <= 0:
                raise ResearchDataError("INVALID_QFQ_FACTOR")
            prices = tuple(float(value) / current_factor for value in bar[1:5])
            previous_close = (
                float(bar[5]) / current_factor if bar[5] is not None else None
            )
            if not all(math.isfinite(value) and value > 0 for value in prices):
                raise ResearchDataError("INVALID_ADJUSTED_PRICE")
            output.append(
                ResearchBar(
                    instrument_id=instrument_id,
                    trade_date=trade_date,
                    open=prices[0],
                    high=prices[1],
                    low=prices[2],
                    close=prices[3],
                    previous_close=previous_close,
                    volume=float(bar[6]),
                    amount=float(bar[7]),
                    adjustment_factor=current_factor,
                )
            )
        return tuple(output)
