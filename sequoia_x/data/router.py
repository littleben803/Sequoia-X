"""Provider 能力路由与明确降级。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from sequoia_x.data.errors import AllProvidersFailedError
from sequoia_x.data.models import DailyBar, InstrumentRecord, TradingStatusRecord
from sequoia_x.data.providers.a_stock_data import AStockDataProvider
from sequoia_x.data.providers.baostock import BaostockProvider


class ProviderRouter:
    def __init__(
        self,
        primary: AStockDataProvider | None = None,
        fallback: BaostockProvider | None = None,
    ) -> None:
        self.primary = primary or AStockDataProvider()
        self.fallback = fallback or BaostockProvider()

    def fetch_instruments(
        self, as_of_date: str
    ) -> tuple[tuple[InstrumentRecord, ...], bool, str | None]:
        try:
            primary_records = self.primary.fetch_instruments(as_of_date)
        except Exception as primary_error:
            try:
                records = self.fallback.fetch_instruments(as_of_date)
                return records, True, str(primary_error)
            except Exception as fallback_error:
                raise AllProvidersFailedError(
                    "INSTRUMENT_DIRECTORY",
                    [
                        (self.primary.provider_name, primary_error),
                        (self.fallback.provider_name, fallback_error),
                    ],
                ) from fallback_error
        if primary_records and all(item.source.endswith("-official") for item in primary_records):
            return primary_records, False, None
        try:
            fallback_records = {
                item.instrument_id: item for item in self.fallback.fetch_instruments(as_of_date)
            }
        except Exception as fallback_error:
            return primary_records, False, f"Baostock 主数据增强失败：{fallback_error}"
        enriched = []
        for item in primary_records:
            extra = fallback_records.get(item.instrument_id)
            if extra is None:
                enriched.append(item)
                continue
            enriched.append(
                replace(
                    item,
                    list_date=extra.list_date,
                    delist_date=extra.delist_date,
                    raw_payload={**item.raw_payload, "baostock": extra.raw_payload},
                )
            )
        return tuple(enriched), False, None

    def fetch_bars_with_fallback(
        self,
        instrument: InstrumentRecord,
        start_date: str,
        end_date: str,
    ) -> tuple[tuple[DailyBar, ...], str]:
        """当前用于盘后包中缺失的沪深 A 股；主快照仍由 primary 单独负责。"""
        errors: list[tuple[str, Exception]] = []
        try:
            # 腾讯区间 K 线将在后续独立能力接入；当前直接使用明确可审计的 Baostock 降级。
            bars = self.fallback.fetch_daily_bars(instrument, start_date, end_date)
            return bars, self.fallback.provider_name
        except Exception as error:
            errors.append((self.fallback.provider_name, error))
        raise AllProvidersFailedError("DAILY_BARS", errors)

    def confirm_trading_status(
        self, instruments: Sequence[InstrumentRecord], trade_date: str
    ) -> tuple[TradingStatusRecord, ...]:
        return self.fallback.fetch_trading_status(instruments, trade_date)
