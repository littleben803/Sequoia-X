"""Baostock 降级 Provider，仅覆盖其明确支持的沪深 A 股能力。"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from multiprocessing import get_context
from queue import Empty

import baostock as bs
from baostock.common import context as bs_context

from sequoia_x.data.errors import CapabilityNotSupportedError, DataProviderError
from sequoia_x.data.models import (
    Adjustment,
    DailyBar,
    DailyStatus,
    Exchange,
    InstrumentRecord,
    SecurityType,
    TradingStatusRecord,
)
from sequoia_x.data.providers.base import ProviderCapability


class BaostockProvider:
    provider_name = "baostock"
    capabilities = frozenset(
        {
            ProviderCapability.DAILY_BARS,
            ProviderCapability.INSTRUMENT_DIRECTORY,
            ProviderCapability.TRADING_STATUS,
        }
    )

    @contextmanager
    def _session(self) -> Iterator[None]:
        with redirect_stdout(StringIO()):
            login = bs.login()
        if login.error_code != "0":
            raise DataProviderError(f"Baostock 建立匿名行情会话失败：{login.error_msg}")
        default_socket = getattr(bs_context, "default_socket", None)
        if default_socket is not None:
            default_socket.settimeout(15)
        try:
            yield
        finally:
            with redirect_stdout(StringIO()):
                bs.logout()

    @staticmethod
    def _code(instrument: InstrumentRecord) -> str:
        if instrument.exchange == Exchange.SSE:
            return f"sh.{instrument.security_code}"
        if instrument.exchange == Exchange.SZSE:
            return f"sz.{instrument.security_code}"
        raise CapabilityNotSupportedError(
            f"Baostock 不支持 {instrument.exchange.value}:{instrument.security_code}"
        )

    def fetch_instruments(self, as_of_date: str) -> tuple[InstrumentRecord, ...]:
        """在隔离进程中读取目录，防止 Baostock 内部吞掉超时后永久阻塞。"""
        context = get_context("spawn")
        queue = context.Queue()
        process = context.Process(target=_fetch_instruments_worker, args=(queue,))
        process.start()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(5)
            raise DataProviderError("Baostock 股票清单超过 20 秒未响应，已终止隔离会话")
        try:
            ok, payload = queue.get_nowait()
        except Empty as exc:
            raise DataProviderError("Baostock 股票清单进程异常退出且没有返回结果") from exc
        if not ok:
            raise DataProviderError(f"Baostock 股票清单失败：{payload}")
        return tuple(payload)

    def _fetch_instruments_direct(self) -> tuple[InstrumentRecord, ...]:
        records: list[InstrumentRecord] = []
        with self._session(), redirect_stdout(StringIO()):
            result = bs.query_stock_basic()
            if result.error_code != "0":
                raise DataProviderError(f"Baostock 股票清单失败：{result.error_msg}")
            while result.next():
                row = dict(zip(result.fields, result.get_row_data(), strict=False))
                raw_code = row.get("code", "")
                if not raw_code.startswith(("sh.", "sz.")) or row.get("type") != "1":
                    continue
                exchange = Exchange.SSE if raw_code.startswith("sh.") else Exchange.SZSE
                records.append(
                    InstrumentRecord(
                        exchange=exchange,
                        security_code=raw_code.split(".", 1)[1],
                        name=row.get("code_name", ""),
                        security_type=SecurityType.STOCK,
                        list_date=row.get("ipoDate") or None,
                        delist_date=row.get("outDate") or None,
                        listing_status="LISTED" if row.get("status") == "1" else "INACTIVE",
                        source=self.provider_name,
                        source_identifier=raw_code,
                        raw_payload=row,
                    )
                )
        return tuple(records)

    def fetch_daily_bars(
        self, instrument: InstrumentRecord, start_date: str, end_date: str
    ) -> tuple[DailyBar, ...]:
        code = self._code(instrument)
        fields = "date,open,high,low,close,volume,amount,tradestatus"
        bars: list[DailyBar] = []
        with self._session(), redirect_stdout(StringIO()):
            result = bs.query_history_k_data_plus(
                code,
                fields,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="3",
            )
            if result.error_code != "0":
                raise DataProviderError(f"Baostock {code} 日线失败：{result.error_msg}")
            while result.next():
                row = dict(zip(result.fields, result.get_row_data(), strict=False))
                if row.get("tradestatus") != "1" or not row.get("close"):
                    continue
                try:
                    bars.append(
                        DailyBar(
                            instrument_id=instrument.instrument_id,
                            trade_date=row["date"],
                            open=float(row["open"]),
                            high=float(row["high"]),
                            low=float(row["low"]),
                            close=float(row["close"]),
                            previous_close=None,
                            volume=float(row["volume"]),
                            amount=float(row["amount"]),
                            provider=self.provider_name,
                            adjustment=Adjustment.RAW,
                            raw_payload=row,
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise DataProviderError(f"Baostock {code} 返回非法日线：{row}") from exc
        return tuple(bars)

    def fetch_trading_status(
        self, instruments: Sequence[InstrumentRecord], trade_date: str
    ) -> tuple[TradingStatusRecord, ...]:
        context = get_context("spawn")
        queue = context.Queue()
        process = context.Process(
            target=_fetch_trading_status_worker,
            args=(queue, tuple(instruments), trade_date),
        )
        process.start()
        process.join(30)
        if process.is_alive():
            process.terminate()
            process.join(5)
            raise DataProviderError("Baostock 停牌确认超过 30 秒未响应，已终止隔离会话")
        try:
            ok, payload = queue.get_nowait()
        except Empty as exc:
            raise DataProviderError("Baostock 停牌确认进程异常退出且没有返回结果") from exc
        if not ok:
            raise DataProviderError(f"Baostock 停牌确认失败：{payload}")
        return tuple(payload)

    def _fetch_trading_status_direct(
        self, instruments: Sequence[InstrumentRecord], trade_date: str
    ) -> tuple[TradingStatusRecord, ...]:
        records: list[TradingStatusRecord] = []
        with self._session():
            for instrument in instruments:
                try:
                    code = self._code(instrument)
                except CapabilityNotSupportedError:
                    continue
                with redirect_stdout(StringIO()):
                    result = bs.query_history_k_data_plus(
                        code,
                        "date,tradestatus,open,close,volume",
                        start_date=trade_date,
                        end_date=trade_date,
                        frequency="d",
                        adjustflag="3",
                    )
                    if result.error_code != "0":
                        records.append(
                            TradingStatusRecord(
                                instrument_id=instrument.instrument_id,
                                trade_date=trade_date,
                                status=DailyStatus.SOURCE_UNAVAILABLE,
                                provider=self.provider_name,
                                evidence={"error": result.error_msg},
                            )
                        )
                        continue
                    rows = []
                    while result.next():
                        rows.append(dict(zip(result.fields, result.get_row_data(), strict=False)))
                if rows and rows[0].get("tradestatus") == "0":
                    status = DailyStatus.SUSPENDED_CONFIRMED
                elif rows and rows[0].get("tradestatus") == "1":
                    status = DailyStatus.TRADED
                else:
                    status = DailyStatus.MISSING_UNEXPLAINED
                records.append(
                    TradingStatusRecord(
                        instrument_id=instrument.instrument_id,
                        trade_date=trade_date,
                        status=status,
                        provider=self.provider_name,
                        evidence=rows[0] if rows else {"rows": 0},
                    )
                )
        return tuple(records)


def _fetch_instruments_worker(queue) -> None:
    try:
        queue.put((True, BaostockProvider()._fetch_instruments_direct()))
    except BaseException as exc:
        queue.put((False, f"{type(exc).__name__}: {exc}"))


def _fetch_trading_status_worker(queue, instruments, trade_date) -> None:
    try:
        queue.put(
            (
                True,
                BaostockProvider()._fetch_trading_status_direct(instruments, trade_date),
            )
        )
    except BaseException as exc:
        queue.put((False, f"{type(exc).__name__}: {exc}"))
