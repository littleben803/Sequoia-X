"""Provider 能力契约。"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Protocol

from sequoia_x.data.models import (
    DailyBar,
    DailySnapshotResult,
    InstrumentRecord,
    TradeCalendarDay,
    TradingStatusRecord,
)


class ProviderCapability(StrEnum):
    DAILY_SNAPSHOT = "DAILY_SNAPSHOT"
    DAILY_BARS = "DAILY_BARS"
    INSTRUMENT_DIRECTORY = "INSTRUMENT_DIRECTORY"
    TRADE_CALENDAR = "TRADE_CALENDAR"
    TRADING_STATUS = "TRADING_STATUS"
    ADJUSTMENT_FACTOR = "ADJUSTMENT_FACTOR"


class MarketDataProvider(Protocol):
    provider_name: str
    capabilities: frozenset[ProviderCapability]

    def fetch_daily_snapshot(self, trade_date: str) -> DailySnapshotResult: ...

    def fetch_daily_bars(
        self, instrument: InstrumentRecord, start_date: str, end_date: str
    ) -> tuple[DailyBar, ...]: ...

    def fetch_instruments(self, as_of_date: str) -> tuple[InstrumentRecord, ...]: ...

    def fetch_trade_calendar(
        self, start_date: str, end_date: str
    ) -> tuple[TradeCalendarDay, ...]: ...

    def fetch_trading_status(
        self, instruments: Sequence[InstrumentRecord], trade_date: str
    ) -> tuple[TradingStatusRecord, ...]: ...

    def fetch_adjustment_factors(
        self, code: str, kind: str = "qfq"
    ) -> tuple[dict[str, Any], ...]: ...
