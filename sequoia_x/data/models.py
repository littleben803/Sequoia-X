"""V3 数据层领域模型。Provider、存储和 UI 只通过这些结构交换数据。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Exchange(StrEnum):
    SSE = "SSE"
    SZSE = "SZSE"
    BSE = "BSE"


class SecurityType(StrEnum):
    STOCK = "STOCK"
    FUND = "FUND"
    ETF = "ETF"
    LOF = "LOF"
    REIT = "REIT"
    BOND = "BOND"
    CONVERTIBLE_BOND = "CONVERTIBLE_BOND"
    EXCHANGEABLE_BOND = "EXCHANGEABLE_BOND"
    ABS = "ABS"
    INDEX = "INDEX"
    REPO = "REPO"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class Adjustment(StrEnum):
    RAW = "RAW"
    QFQ = "QFQ"
    HFQ = "HFQ"


class BatchStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    DEGRADED_PARTIAL = "DEGRADED_PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class DailyStatus(StrEnum):
    TRADED = "TRADED"
    SUSPENDED_CONFIRMED = "SUSPENDED_CONFIRMED"
    NO_TRADE_OBSERVED = "NO_TRADE_OBSERVED"
    NOT_YET_LISTED = "NOT_YET_LISTED"
    DELISTED = "DELISTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    MISSING_UNEXPLAINED = "MISSING_UNEXPLAINED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


@dataclass(frozen=True)
class InstrumentRecord:
    exchange: Exchange
    security_code: str
    name: str
    security_type: SecurityType = SecurityType.UNKNOWN
    security_subtype: str | None = None
    currency: str = "CNY"
    list_date: str | None = None
    delist_date: str | None = None
    listing_status: str = "OBSERVED"
    underlying_instrument_id: str | None = None
    source: str = ""
    source_identifier: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def instrument_id(self) -> str:
        return f"{self.exchange.value}:{self.security_code}"


@dataclass(frozen=True)
class DailyBar:
    instrument_id: str
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    previous_close: float | None
    volume: float
    amount: float
    provider: str
    adjustment: Adjustment = Adjustment.RAW
    currency: str = "CNY"
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdjustmentFactorRecord:
    """可审计的复权因子记录。

    QFQ 因子是除数：前复权价 = 原始价 / factor。
    """

    instrument_id: str
    effective_date: str
    factor: float
    kind: Adjustment
    provider: str
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DailySnapshotResult:
    trade_date: str
    provider: str
    source_url: str
    bars: tuple[DailyBar, ...]
    instruments: tuple[InstrumentRecord, ...]
    checksum: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BenchmarkBarsResult:
    instrument: InstrumentRecord
    start_date: str
    end_date: str
    provider: str
    source_url: str
    bars: tuple[DailyBar, ...]
    checksum: str


@dataclass(frozen=True)
class BenchmarkSyncReport:
    instrument_id: str
    name: str
    start_date: str
    end_date: str
    fetched_rows: int
    inserted_rows: int
    updated_rows: int
    elapsed_seconds: float


@dataclass(frozen=True)
class BenchmarkProgress:
    phase: str
    message: str
    step: int
    total_steps: int = 3


@dataclass(frozen=True)
class TradeCalendarDay:
    calendar_date: str
    is_trading_day: bool
    market: str = "CN"
    provider: str = ""


@dataclass(frozen=True)
class TradingStatusRecord:
    instrument_id: str
    trade_date: str
    status: DailyStatus
    provider: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestionProgress:
    phase: str
    step: str
    current_date: str | None
    processed_dates: int
    total_dates: int
    successful_dates: int
    degraded_dates: int
    skipped_dates: int
    failed_dates: int
    rows_written: int
    pending_dates: int = 0
    type_counts: dict[str, int] = field(default_factory=dict)
    message: str = ""
    step_elapsed_seconds: float = 0
    is_heartbeat: bool = False
    current_instrument_name: str | None = None
    current_instrument_code: str | None = None
    current_instrument_type: str | None = None
    step_processed: int = 0
    step_total: int = 0


@dataclass(frozen=True)
class IngestionReport:
    requested_dates: int
    processed_dates: int
    successful_dates: int
    degraded_dates: int
    skipped_dates: int
    failed_dates: int
    rows_written: int
    instruments_upserted: int
    elapsed_seconds: float
    pending_dates: int = 0
    recovered_stale_batches: int = 0
    security_type_counts: dict[str, int] = field(default_factory=dict)
    failed_trade_dates: tuple[str, ...] = ()
    pending_trade_dates: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.failed_dates == 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"succeeded": self.succeeded}


@dataclass(frozen=True)
class V3AuditReport:
    ready_for_a_share_research: bool
    latest_complete_date: str | None
    instruments: int
    raw_bars: int
    batches: int
    complete_batches: int
    degraded_batches: int
    failed_batches: int
    unexplained_a_share_gaps: int
    invalid_rows: int
    problems: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["problems"] = list(self.problems)
        return data


@dataclass(frozen=True)
class QualityRepairReport:
    instruments_reclassified: int
    currently_listed: int
    delisted_or_inactive: int
    code_migrations: int
    statuses_rebuilt: int
    unexplained_gaps_after_repair: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
