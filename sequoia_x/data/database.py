"""V3 SQLite Repository：保存原始数据、来源证据和批次状态。"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sequoia_x.data.classification import classify_security
from sequoia_x.data.models import (
    Adjustment,
    AdjustmentFactorRecord,
    BatchStatus,
    DailySnapshotResult,
    DailyStatus,
    Exchange,
    InstrumentRecord,
    QualityRepairReport,
    SecurityType,
    TradeCalendarDay,
    TradingStatusRecord,
    V3AuditReport,
)

SCHEMA_VERSION = 5
WriteProgressCallback = Callable[[str, int, int, str], None]


class MarketDataRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS instruments (
                    instrument_id TEXT PRIMARY KEY,
                    exchange TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    security_type TEXT NOT NULL,
                    security_subtype TEXT,
                    currency TEXT NOT NULL,
                    list_date TEXT,
                    delist_date TEXT,
                    listing_status TEXT NOT NULL,
                    underlying_instrument_id TEXT,
                    first_seen_date TEXT NOT NULL,
                    last_seen_date TEXT NOT NULL,
                    primary_source TEXT NOT NULL,
                    code_valid_from TEXT,
                    code_valid_to TEXT,
                    lifecycle_source TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(exchange, security_code)
                );

                CREATE TABLE IF NOT EXISTS instrument_source_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
                    provider TEXT NOT NULL,
                    as_of_date TEXT NOT NULL,
                    source_identifier TEXT,
                    raw_payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    UNIQUE(instrument_id, provider, as_of_date)
                );

                CREATE TABLE IF NOT EXISTS ingestion_batches (
                    batch_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    trade_date TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    degraded INTEGER NOT NULL DEFAULT 0,
                    requested_count INTEGER NOT NULL DEFAULT 0,
                    received_count INTEGER NOT NULL DEFAULT 0,
                    valid_count INTEGER NOT NULL DEFAULT 0,
                    invalid_count INTEGER NOT NULL DEFAULT 0,
                    inserted_count INTEGER NOT NULL DEFAULT 0,
                    updated_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    error_message TEXT,
                    source_url TEXT,
                    checksum TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS daily_bars_raw (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
                    trade_date TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    previous_close REAL,
                    volume REAL NOT NULL,
                    amount REAL NOT NULL,
                    provider TEXT NOT NULL,
                    source_batch_id TEXT REFERENCES ingestion_batches(batch_id),
                    adjustment TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    raw_payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    UNIQUE(instrument_id, trade_date, provider, adjustment)
                );

                CREATE INDEX IF NOT EXISTS idx_raw_bar_date
                ON daily_bars_raw(trade_date);
                CREATE INDEX IF NOT EXISTS idx_raw_bar_instrument_date
                ON daily_bars_raw(instrument_id, trade_date);

                CREATE TABLE IF NOT EXISTS daily_bars_canonical (
                    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
                    trade_date TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    previous_close REAL,
                    volume REAL NOT NULL,
                    amount REAL NOT NULL,
                    selected_provider TEXT NOT NULL,
                    source_raw_bar_id INTEGER NOT NULL REFERENCES daily_bars_raw(id),
                    quality_status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(instrument_id, trade_date)
                );

                CREATE TABLE IF NOT EXISTS adjustment_factors (
                    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
                    effective_date TEXT NOT NULL,
                    factor REAL NOT NULL,
                    kind TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    source_batch_id TEXT NOT NULL REFERENCES ingestion_batches(batch_id),
                    raw_payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY(instrument_id, effective_date, kind, provider)
                );

                CREATE INDEX IF NOT EXISTS idx_adjustment_factor_lookup
                ON adjustment_factors(instrument_id, kind, effective_date);

                CREATE TABLE IF NOT EXISTS trade_calendar (
                    market TEXT NOT NULL,
                    calendar_date TEXT NOT NULL,
                    is_trading_day INTEGER NOT NULL,
                    session_status TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY(market, calendar_date, provider)
                );

                CREATE TABLE IF NOT EXISTS instrument_daily_status (
                    instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
                    trade_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    status_source TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    PRIMARY KEY(instrument_id, trade_date, status_source)
                );

                CREATE TABLE IF NOT EXISTS instrument_code_aliases (
                    alias_instrument_id TEXT PRIMARY KEY REFERENCES instruments(instrument_id),
                    canonical_instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
                    effective_from TEXT NOT NULL,
                    effective_to TEXT,
                    reason TEXT NOT NULL,
                    evidence_json TEXT NOT NULL
                );
                """
            )
            instrument_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(instruments)").fetchall()
            }
            for column, declaration in (
                ("code_valid_from", "TEXT"),
                ("code_valid_to", "TEXT"),
                ("lifecycle_source", "TEXT"),
            ):
                if column not in instrument_columns:
                    conn.execute(f"ALTER TABLE instruments ADD COLUMN {column} {declaration}")
            conn.execute("DROP VIEW IF EXISTS daily_bars_research")
            conn.execute(
                """
                CREATE VIEW daily_bars_research AS
                SELECT COALESCE(a.canonical_instrument_id, d.instrument_id)
                           AS research_instrument_id,
                       d.instrument_id AS source_instrument_id,
                       d.trade_date, d.open, d.high, d.low, d.close,
                       d.previous_close, d.volume, d.amount,
                       d.selected_provider, d.source_raw_bar_id,
                       d.quality_status, d.updated_at
                FROM daily_bars_canonical AS d
                LEFT JOIN instrument_code_aliases AS a
                  ON a.alias_instrument_id=d.instrument_id
                 AND d.trade_date>=a.effective_from
                 AND (a.effective_to IS NULL OR d.trade_date<=a.effective_to)
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def set_metadata(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES(?, ?)",
                (key, value),
            )

    def start_batch(self, provider: str, capability: str, trade_date: str | None) -> str:
        batch_id = str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO ingestion_batches
                    (batch_id, provider, capability, trade_date, started_at, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (batch_id, provider, capability, trade_date, self._now(), BatchStatus.RUNNING),
            )
        return batch_id

    def fail_batch(self, batch_id: str, error: Exception) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE ingestion_batches SET finished_at=?, status=?, error_code=?, error_message=?
                WHERE batch_id=?
                """,
                (self._now(), BatchStatus.FAILED, type(error).__name__, str(error), batch_id),
            )

    def skip_unpublished_batch(self, batch_id: str, reason: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE ingestion_batches SET finished_at=?, status=?,
                    error_code='NOT_PUBLISHED_YET', error_message=?
                WHERE batch_id=? AND status='RUNNING'
                """,
                (self._now(), BatchStatus.SKIPPED, reason, batch_id),
            )

    def interrupt_batch(self, batch_id: str, reason: str = "任务被用户中断") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE ingestion_batches SET finished_at=?, status=?,
                    error_code='INTERRUPTED', error_message=?
                WHERE batch_id=? AND status='RUNNING'
                """,
                (self._now(), BatchStatus.FAILED, reason, batch_id),
            )

    def recover_stale_batches(self, older_than_minutes: int = 60) -> int:
        """回收异常退出后遗留的 RUNNING 批次，使对应交易日可以安全续跑。"""
        cutoff = (datetime.now(UTC) - timedelta(minutes=older_than_minutes)).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE ingestion_batches SET finished_at=?, status=?,
                    error_code='INTERRUPTED_STALE',
                    error_message='上一次进程异常退出，启动续跑时自动回收'
                WHERE status='RUNNING' AND started_at < ?
                """,
                (self._now(), BatchStatus.FAILED, cutoff),
            )
        return cursor.rowcount

    def has_complete_snapshot(self, trade_date: str) -> bool:
        with self.connect() as conn:
            return (
                conn.execute(
                    """
                    SELECT 1 FROM ingestion_batches
                    WHERE capability='DAILY_SNAPSHOT' AND trade_date=? AND status='COMPLETE'
                    LIMIT 1
                    """,
                    (trade_date,),
                ).fetchone()
                is not None
            )

    def latest_complete_snapshot_date(self) -> str | None:
        """返回最新完整全市场盘后批次日期，不执行全库质量审计。"""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT MAX(trade_date) FROM ingestion_batches
                WHERE capability='DAILY_SNAPSHOT' AND status='COMPLETE'
                """
            ).fetchone()
        return row[0] if row else None

    def save_calendar(self, days: tuple[TradeCalendarDay, ...]) -> None:
        now = self._now()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO trade_calendar
                    (market, calendar_date, is_trading_day, session_status, provider, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(market, calendar_date, provider) DO UPDATE SET
                    is_trading_day=excluded.is_trading_day,
                    session_status=excluded.session_status,
                    fetched_at=excluded.fetched_at
                """,
                (
                    (
                        day.market,
                        day.calendar_date,
                        int(day.is_trading_day),
                        "OPEN" if day.is_trading_day else "CLOSED",
                        day.provider,
                        now,
                    )
                    for day in days
                ),
            )

    def trading_dates(self, start_date: str, end_date: str) -> tuple[str, ...]:
        with self.connect() as conn:
            return tuple(
                row[0]
                for row in conn.execute(
                    """
                    SELECT DISTINCT calendar_date FROM trade_calendar
                    WHERE calendar_date BETWEEN ? AND ? AND is_trading_day=1
                    ORDER BY calendar_date
                    """,
                    (start_date, end_date),
                )
            )

    def upsert_instruments(
        self,
        records: tuple[InstrumentRecord, ...],
        as_of_date: str,
        progress_callback: WriteProgressCallback | None = None,
    ) -> int:
        now = self._now()
        with self.connect() as conn:
            total = len(records)
            for index, record in enumerate(records, 1):
                conn.execute(
                    """
                    INSERT INTO instruments
                        (instrument_id, exchange, security_code, name, security_type,
                         security_subtype, currency, list_date, delist_date, listing_status,
                         underlying_instrument_id, first_seen_date, last_seen_date,
                         primary_source, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(instrument_id) DO UPDATE SET
                        name=excluded.name,
                        security_type=CASE
                            WHEN excluded.security_type='UNKNOWN' THEN instruments.security_type
                            ELSE excluded.security_type END,
                        security_subtype=COALESCE(
                            excluded.security_subtype,
                            instruments.security_subtype
                        ),
                        list_date=COALESCE(excluded.list_date, instruments.list_date),
                        delist_date=COALESCE(excluded.delist_date, instruments.delist_date),
                        listing_status=excluded.listing_status,
                        last_seen_date=excluded.last_seen_date,
                        primary_source=excluded.primary_source,
                        metadata_json=excluded.metadata_json
                    """,
                    (
                        record.instrument_id,
                        record.exchange,
                        record.security_code,
                        record.name,
                        record.security_type,
                        record.security_subtype,
                        record.currency,
                        record.list_date,
                        record.delist_date,
                        record.listing_status,
                        record.underlying_instrument_id,
                        as_of_date,
                        as_of_date,
                        record.source,
                        json.dumps(record.raw_payload, ensure_ascii=False),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO instrument_source_records
                        (instrument_id, provider, as_of_date, source_identifier,
                         raw_payload_json, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(instrument_id, provider, as_of_date) DO UPDATE SET
                        source_identifier=excluded.source_identifier,
                        raw_payload_json=excluded.raw_payload_json,
                        fetched_at=excluded.fetched_at
                    """,
                    (
                        record.instrument_id,
                        record.source,
                        as_of_date,
                        record.source_identifier,
                        json.dumps(record.raw_payload, ensure_ascii=False),
                        now,
                    ),
                )
                if progress_callback and (index == 1 or index % 250 == 0 or index == total):
                    progress_callback("INSTRUMENTS", index, total, record.instrument_id)
        return len(records)

    def save_snapshot(
        self,
        batch_id: str,
        snapshot: DailySnapshotResult,
        status: BatchStatus = BatchStatus.COMPLETE,
        degraded_reason: str | None = None,
        progress_callback: WriteProgressCallback | None = None,
    ) -> tuple[int, int]:
        """一个交易日内的证券、原始行情、canonical 与批次状态在同一事务提交。"""
        now = self._now()
        self.upsert_instruments(
            snapshot.instruments,
            snapshot.trade_date,
            progress_callback=progress_callback,
        )
        inserted = updated = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            total_bars = len(snapshot.bars)
            for index, bar in enumerate(snapshot.bars, 1):
                existing = conn.execute(
                    """
                    SELECT id FROM daily_bars_raw
                    WHERE instrument_id=? AND trade_date=? AND provider=? AND adjustment=?
                    """,
                    (bar.instrument_id, bar.trade_date, bar.provider, bar.adjustment),
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO daily_bars_raw
                        (instrument_id, trade_date, open, high, low, close, previous_close,
                         volume, amount, provider, source_batch_id, adjustment, currency,
                         raw_payload_json, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(instrument_id, trade_date, provider, adjustment) DO UPDATE SET
                        open=excluded.open, high=excluded.high, low=excluded.low,
                        close=excluded.close, previous_close=excluded.previous_close,
                        volume=excluded.volume, amount=excluded.amount,
                        source_batch_id=excluded.source_batch_id,
                        raw_payload_json=excluded.raw_payload_json,
                        fetched_at=excluded.fetched_at
                    """,
                    (
                        bar.instrument_id,
                        bar.trade_date,
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.previous_close,
                        bar.volume,
                        bar.amount,
                        bar.provider,
                        batch_id,
                        bar.adjustment,
                        bar.currency,
                        json.dumps(bar.raw_payload, ensure_ascii=False),
                        now,
                    ),
                )
                inserted += existing is None
                updated += existing is not None
                if progress_callback and (index == 1 or index % 250 == 0 or index == total_bars):
                    progress_callback("DAILY_BARS", index, total_bars, bar.instrument_id)
            conn.execute(
                """
                INSERT INTO daily_bars_canonical
                    (instrument_id, trade_date, open, high, low, close, previous_close,
                     volume, amount, selected_provider, source_raw_bar_id,
                     quality_status, updated_at)
                SELECT instrument_id, trade_date, open, high, low, close, previous_close,
                       volume, amount, provider, id, 'PRIMARY_RAW', ?
                FROM daily_bars_raw
                WHERE source_batch_id=?
                  AND open>0 AND high>0 AND low>0 AND close>0
                  AND volume>=0 AND amount>=0
                  AND high>=open AND high>=close AND high>=low
                  AND low<=open AND low<=close AND low<=high
                ON CONFLICT(instrument_id, trade_date) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, previous_close=excluded.previous_close,
                    volume=excluded.volume, amount=excluded.amount,
                    selected_provider=excluded.selected_provider,
                    source_raw_bar_id=excluded.source_raw_bar_id,
                    quality_status=excluded.quality_status,
                    updated_at=excluded.updated_at
                """,
                (now, batch_id),
            )
            conn.execute(
                """
                UPDATE ingestion_batches SET
                    finished_at=?, status=?, requested_count=?, received_count=?,
                    valid_count=?, inserted_count=?, updated_count=?, source_url=?,
                    checksum=?, metadata_json=?
                WHERE batch_id=?
                """,
                (
                    now,
                    status,
                    len(snapshot.bars),
                    len(snapshot.bars),
                    len(snapshot.bars),
                    inserted,
                    updated,
                    snapshot.source_url,
                    snapshot.checksum,
                    json.dumps(snapshot.metadata, ensure_ascii=False),
                    batch_id,
                ),
            )
            if degraded_reason:
                conn.execute(
                    """
                    UPDATE ingestion_batches
                    SET degraded=1, error_code='PRIMARY_FAILED', error_message=?
                    WHERE batch_id=?
                    """,
                    (degraded_reason, batch_id),
                )
        instrument_types = {
            instrument.instrument_id: instrument.security_type
            for instrument in snapshot.instruments
        }
        no_trade_records = tuple(
            TradingStatusRecord(
                instrument_id=bar.instrument_id,
                trade_date=bar.trade_date,
                status=DailyStatus.NO_TRADE_OBSERVED,
                provider=snapshot.provider,
                evidence={
                    "reason": "盘后包包含证券记录，但开高低、成交量和成交额均为零",
                    "close": bar.close,
                    "source_batch_id": batch_id,
                },
            )
            for bar in snapshot.bars
            if instrument_types.get(bar.instrument_id) == SecurityType.STOCK
            and bar.open == 0
            and bar.high == 0
            and bar.low == 0
            and bar.volume == 0
            and bar.amount == 0
        )
        if no_trade_records:
            self.save_daily_status(no_trade_records)
        return inserted, updated

    def save_adjustment_factors(
        self,
        batch_id: str,
        records: tuple[AdjustmentFactorRecord, ...],
    ) -> tuple[int, int]:
        """幂等保存单个证券的复权因子，并在同一事务中完成批次。"""
        if not records:
            raise ValueError("复权因子为空，不能冒充成功批次")
        instrument_ids = {record.instrument_id for record in records}
        kinds = {record.kind for record in records}
        providers = {record.provider for record in records}
        if len(instrument_ids) != 1 or len(kinds) != 1 or len(providers) != 1:
            raise ValueError("单个复权因子批次只能包含一个证券、口径和 Provider")
        now = self._now()
        inserted = updated = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute(
                "SELECT status FROM ingestion_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if batch is None or batch[0] != BatchStatus.RUNNING:
                raise ValueError("复权因子批次不存在或已结束")
            for record in records:
                if record.kind not in (Adjustment.QFQ, Adjustment.HFQ):
                    raise ValueError("复权因子口径只能是 QFQ 或 HFQ")
                try:
                    date.fromisoformat(record.effective_date)
                except ValueError as error:
                    raise ValueError("复权因子生效日必须是 ISO 日期") from error
                if not math.isfinite(record.factor) or record.factor <= 0:
                    raise ValueError("复权因子必须是大于 0 的有限数")
                existing = conn.execute(
                    """
                    SELECT 1 FROM adjustment_factors
                    WHERE instrument_id=? AND effective_date=? AND kind=? AND provider=?
                    """,
                    (
                        record.instrument_id,
                        record.effective_date,
                        record.kind,
                        record.provider,
                    ),
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO adjustment_factors
                        (instrument_id, effective_date, factor, kind, provider,
                         source_batch_id, raw_payload_json, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(instrument_id, effective_date, kind, provider) DO UPDATE SET
                        factor=excluded.factor,
                        source_batch_id=excluded.source_batch_id,
                        raw_payload_json=excluded.raw_payload_json,
                        fetched_at=excluded.fetched_at
                    """,
                    (
                        record.instrument_id,
                        record.effective_date,
                        record.factor,
                        record.kind,
                        record.provider,
                        batch_id,
                        json.dumps(record.raw_payload, ensure_ascii=False),
                        now,
                    ),
                )
                inserted += existing is None
                updated += existing is not None
            conn.execute(
                """
                UPDATE ingestion_batches SET finished_at=?, status=?, requested_count=?,
                    received_count=?, valid_count=?, inserted_count=?, updated_count=?,
                    metadata_json=?
                WHERE batch_id=?
                """,
                (
                    now,
                    BatchStatus.COMPLETE,
                    len(records),
                    len(records),
                    len(records),
                    inserted,
                    updated,
                    json.dumps(
                        {
                            "instrument_id": next(iter(instrument_ids)),
                            "kind": next(iter(kinds)),
                        },
                        ensure_ascii=False,
                    ),
                    batch_id,
                ),
            )
        return inserted, updated

    def save_daily_status(self, records: tuple[TradingStatusRecord, ...]) -> None:
        now = self._now()
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO instrument_daily_status
                    (instrument_id, trade_date, status, status_source, evidence_json, checked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(instrument_id, trade_date, status_source) DO UPDATE SET
                    status=excluded.status,
                    evidence_json=excluded.evidence_json,
                    checked_at=excluded.checked_at
                """,
                (
                    (
                        record.instrument_id,
                        record.trade_date,
                        record.status,
                        record.provider,
                        json.dumps(record.evidence, ensure_ascii=False),
                        now,
                    )
                    for record in records
                ),
            )

    def listed_a_share_missing_on(self, trade_date: str) -> tuple[InstrumentRecord, ...]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT exchange, security_code, name, security_type, security_subtype,
                       currency, list_date, delist_date, listing_status, primary_source
                FROM instruments AS i
                WHERE security_type='STOCK'
                  AND exchange IN ('SSE', 'SZSE', 'BSE')
                  AND COALESCE(code_valid_from, list_date, first_seen_date) <= ?
                  AND (COALESCE(code_valid_to, delist_date) IS NULL
                       OR COALESCE(code_valid_to, delist_date) >= ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM daily_bars_canonical AS d
                      WHERE d.instrument_id=i.instrument_id AND d.trade_date=?
                  )
                ORDER BY exchange, security_code
                """,
                (trade_date, trade_date, trade_date),
            ).fetchall()
        return tuple(
            InstrumentRecord(
                exchange=Exchange(row[0]),
                security_code=row[1],
                name=row[2],
                security_type=SecurityType(row[3]),
                security_subtype=row[4],
                currency=row[5],
                list_date=row[6],
                delist_date=row[7],
                listing_status=row[8],
                source=row[9],
            )
            for row in rows
        )

    @staticmethod
    def _normalized_security_name(name: str) -> str:
        normalized = name.upper().replace(" ", "")
        for marker in ("*ST", "ST", "XD", "DR"):
            normalized = normalized.replace(marker, "")
        return normalized.removesuffix("退")

    def rebuild_data_quality(
        self, status_start_date: str | None = None, status_end_date: str | None = None
    ) -> QualityRepairReport:
        """重建生命周期；可只重建指定日期范围内的每日质量状态。"""
        if (status_start_date is None) != (status_end_date is None):
            raise ValueError("质量状态起止日期必须同时提供")
        if status_start_date and status_start_date > status_end_date:
            raise ValueError("质量状态起始日期不能晚于结束日期")
        with self.connect() as conn:
            latest = conn.execute(
                """
                SELECT MAX(trade_date) FROM ingestion_batches
                WHERE capability='DAILY_SNAPSHOT' AND status='COMPLETE'
                """
            ).fetchone()[0]
            if not latest:
                raise RuntimeError("没有完整盘后批次，不能重建数据质量")
            rows = conn.execute(
                """
                SELECT instrument_id, exchange, security_code, name, security_type,
                       list_date, primary_source
                FROM instruments
                """
            ).fetchall()
            bounds = {
                row[0]: (row[1], row[2])
                for row in conn.execute(
                    """
                    SELECT instrument_id, MIN(trade_date), MAX(trade_date)
                    FROM (
                        SELECT instrument_id, trade_date
                        FROM daily_bars_canonical
                        UNION ALL
                        SELECT instrument_id, trade_date
                        FROM daily_bars_raw
                        WHERE adjustment='RAW'
                    )
                    GROUP BY instrument_id
                    """
                )
            }

            classified: dict[str, SecurityType] = {}
            details: dict[str, sqlite3.Row | tuple] = {}
            reclassified = 0
            for row in rows:
                instrument_id, exchange, code, name, old_type, _, _ = row
                security_type = classify_security(Exchange(exchange), code, name)
                classified[instrument_id] = security_type
                details[instrument_id] = row
                reclassified += security_type.value != old_type

            current_bse_by_name = {
                self._normalized_security_name(row[3]): row[0]
                for row in rows
                if row[1] == Exchange.BSE.value
                and row[2].startswith("920")
                and row[6].endswith("-official")
                and classified[row[0]] == SecurityType.STOCK
            }
            aliases: list[tuple[str, str, str, str | None, str, str]] = []
            alias_targets: set[str] = set()
            for row in rows:
                instrument_id, exchange, code, name, _, _, _ = row
                if (
                    exchange == Exchange.BSE.value
                    and code.startswith(("43", "83", "87"))
                    and classified[instrument_id] == SecurityType.STOCK
                ):
                    canonical = current_bse_by_name.get(self._normalized_security_name(name))
                    if canonical:
                        aliases.append(
                            (
                                instrument_id,
                                canonical,
                                bounds.get(instrument_id, (None, None))[0] or "2024-01-01",
                                "2025-09-30",
                                "BSE_CODE_MIGRATION_920",
                                json.dumps(
                                    {"old_name": name, "new_name": details[canonical][3]},
                                    ensure_ascii=False,
                                ),
                            )
                        )
                        alias_targets.add(canonical)

            lifecycle_updates = []
            currently_listed = inactive = 0
            alias_ids = {item[0] for item in aliases}
            for row in rows:
                instrument_id, exchange, code, _, _, list_date, source = row
                security_type = classified[instrument_id]
                first_bar, last_bar = bounds.get(instrument_id, (None, None))
                status = "OBSERVED"
                valid_from = list_date or first_bar
                valid_to = None
                lifecycle_source = "OBSERVED_HISTORY"
                if security_type == SecurityType.STOCK:
                    if instrument_id in alias_ids:
                        status = "CODE_MIGRATED"
                        valid_to = "2025-09-30"
                        lifecycle_source = "BSE_CODE_MIGRATION_920"
                        inactive += 1
                    elif source.endswith("-official"):
                        status = "LISTED"
                        if instrument_id in alias_targets:
                            valid_from = "2025-10-09"
                            lifecycle_source = "BSE_CODE_MIGRATION_920"
                        else:
                            lifecycle_source = source
                        currently_listed += 1
                    else:
                        status = "DELISTED"
                        valid_to = last_bar
                        lifecycle_source = "INFERRED_NOT_IN_CURRENT_OFFICIAL_DIRECTORY"
                        inactive += 1
                lifecycle_updates.append(
                    (
                        security_type,
                        status,
                        valid_from,
                        valid_to,
                        lifecycle_source,
                        instrument_id,
                    )
                )

            conn.executemany(
                """
                UPDATE instruments SET security_type=?, listing_status=?,
                    code_valid_from=?, code_valid_to=?, lifecycle_source=?
                WHERE instrument_id=?
                """,
                lifecycle_updates,
            )
            conn.execute("DELETE FROM instrument_code_aliases")
            conn.executemany(
                """
                INSERT INTO instrument_code_aliases
                    (alias_instrument_id, canonical_instrument_id, effective_from,
                     effective_to, reason, evidence_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                aliases,
            )
            delete_sql = """
                DELETE FROM instrument_daily_status
                WHERE status IN ('MISSING_UNEXPLAINED', 'SOURCE_UNAVAILABLE',
                                 'NO_TRADE_OBSERVED', 'NOT_YET_LISTED', 'DELISTED')
            """
            delete_params: tuple[str, ...] = ()
            if status_start_date and status_end_date:
                delete_sql += " AND trade_date BETWEEN ? AND ?"
                delete_params = (status_start_date, status_end_date)
            conn.execute(delete_sql, delete_params)
            now = self._now()
            cursor = conn.execute(
                """
                INSERT INTO instrument_daily_status
                    (instrument_id, trade_date, status, status_source,
                     evidence_json, checked_at)
                SELECT i.instrument_id, t.calendar_date,
                       CASE WHEN EXISTS (
                           SELECT 1 FROM instrument_daily_status old_status
                           WHERE old_status.instrument_id=i.instrument_id
                             AND old_status.trade_date=t.calendar_date
                             AND old_status.status='TRADED'
                       ) THEN 'MISSING_UNEXPLAINED'
                       ELSE 'NO_TRADE_OBSERVED' END,
                       'quality-rebuild-v4',
                       CASE WHEN EXISTS (
                           SELECT 1 FROM daily_bars_raw r
                           WHERE r.instrument_id=i.instrument_id
                             AND r.trade_date=t.calendar_date
                       ) THEN '{"evidence":"complete_package_zero_trade_record"}'
                       ELSE '{"evidence":"absent_from_complete_daily_package"}' END,
                       ?
                FROM instruments i
                JOIN (
                    SELECT DISTINCT calendar_date FROM trade_calendar
                    WHERE is_trading_day=1
                      AND (? IS NULL OR calendar_date>=?)
                      AND (? IS NULL OR calendar_date<=?)
                ) t
                  ON t.calendar_date>=COALESCE(
                        i.code_valid_from, i.list_date, i.first_seen_date
                     )
                 AND (COALESCE(i.code_valid_to, i.delist_date) IS NULL
                      OR t.calendar_date<=COALESCE(i.code_valid_to, i.delist_date))
                WHERE i.security_type='STOCK'
                  AND EXISTS (
                      SELECT 1 FROM ingestion_batches b
                      WHERE b.trade_date=t.calendar_date
                        AND b.capability='DAILY_SNAPSHOT' AND b.status='COMPLETE'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM daily_bars_canonical d
                      WHERE d.instrument_id=i.instrument_id
                        AND d.trade_date=t.calendar_date
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM instrument_daily_status confirmed
                      WHERE confirmed.instrument_id=i.instrument_id
                        AND confirmed.trade_date=t.calendar_date
                        AND confirmed.status='SUSPENDED_CONFIRMED'
                  )
                """,
                (
                    now,
                    status_start_date,
                    status_start_date,
                    status_end_date,
                    status_end_date,
                ),
            )
        audit = self.audit()
        return QualityRepairReport(
            instruments_reclassified=reclassified,
            currently_listed=currently_listed,
            delisted_or_inactive=inactive,
            code_migrations=len(aliases),
            statuses_rebuilt=cursor.rowcount,
            unexplained_gaps_after_repair=audit.unexplained_a_share_gaps,
        )

    def type_counts(self, trade_date: str) -> dict[str, int]:
        with self.connect() as conn:
            return dict(
                conn.execute(
                    """
                    SELECT i.security_type, COUNT(*)
                    FROM daily_bars_canonical AS d
                    JOIN instruments AS i USING(instrument_id)
                    WHERE d.trade_date=? GROUP BY i.security_type
                    """,
                    (trade_date,),
                ).fetchall()
            )

    def audit(self) -> V3AuditReport:
        with self.connect() as conn:
            directory_status_row = conn.execute(
                "SELECT value FROM schema_metadata WHERE key='instrument_directory_status'"
            ).fetchone()
            directory_status = directory_status_row[0] if directory_status_row else "MISSING"
            instruments = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
            raw_bars = conn.execute("SELECT COUNT(*) FROM daily_bars_raw").fetchone()[0]
            batches = conn.execute("SELECT COUNT(*) FROM ingestion_batches").fetchone()[0]
            counts = Counter(
                dict(
                    conn.execute(
                        "SELECT status, COUNT(*) FROM ingestion_batches GROUP BY status"
                    ).fetchall()
                )
            )
            latest = conn.execute(
                """
                SELECT MAX(trade_date) FROM ingestion_batches
                WHERE capability='DAILY_SNAPSHOT' AND status='COMPLETE'
                """
            ).fetchone()[0]
            unexplained = conn.execute(
                """
                SELECT COUNT(*)
                FROM instruments i
                JOIN (
                    SELECT DISTINCT calendar_date FROM trade_calendar
                    WHERE is_trading_day=1
                ) t
                  ON t.calendar_date>=COALESCE(
                        i.code_valid_from, i.list_date, i.first_seen_date
                     )
                 AND (COALESCE(i.code_valid_to, i.delist_date) IS NULL
                      OR t.calendar_date<=COALESCE(i.code_valid_to, i.delist_date))
                WHERE i.security_type='STOCK'
                  AND EXISTS (
                      SELECT 1 FROM ingestion_batches b
                      WHERE b.trade_date=t.calendar_date
                        AND b.capability='DAILY_SNAPSHOT' AND b.status='COMPLETE'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM daily_bars_canonical d
                      WHERE d.instrument_id=i.instrument_id
                        AND d.trade_date=t.calendar_date
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM instrument_daily_status s
                      WHERE s.instrument_id=i.instrument_id
                        AND s.trade_date=t.calendar_date
                        AND s.status IN ('SUSPENDED_CONFIRMED', 'NO_TRADE_OBSERVED')
                  )
                """
            ).fetchone()[0]
            invalid = conn.execute(
                """
                SELECT COUNT(*)
                FROM daily_bars_raw AS d
                JOIN instruments AS i USING(instrument_id)
                WHERE i.security_type='STOCK'
                  AND (d.open<=0 OR d.high<=0 OR d.low<=0 OR d.close<=0
                       OR d.volume<0 OR d.amount<0
                       OR d.high<d.open OR d.high<d.close OR d.high<d.low
                       OR d.low>d.open OR d.low>d.close OR d.low>d.high)
                  AND NOT (
                      d.open=0 AND d.high=0 AND d.low=0 AND d.close>0
                      AND d.volume=0 AND d.amount=0
                  )
                """
            ).fetchone()[0]
        problems: list[str] = []
        if not latest:
            problems.append("没有完整的全市场盘后批次")
        if directory_status != "COMPLETE":
            problems.append("统一证券目录未成功刷新，无法完整识别停牌或缺失证券")
        if invalid:
            problems.append(f"存在 {invalid} 行非法 A 股原始行情")
        if unexplained:
            problems.append(f"存在 {unexplained} 个尚未解释的 A 股缺口")
        return V3AuditReport(
            ready_for_a_share_research=not problems,
            latest_complete_date=latest,
            instruments=instruments,
            raw_bars=raw_bars,
            batches=batches,
            complete_batches=counts[BatchStatus.COMPLETE],
            degraded_batches=counts[BatchStatus.DEGRADED_PARTIAL],
            failed_batches=counts[BatchStatus.FAILED],
            unexplained_a_share_gaps=unexplained,
            invalid_rows=invalid,
            problems=tuple(problems),
        )
