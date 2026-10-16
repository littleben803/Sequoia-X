"""AStockDataProvider：从 a-stock-data 归档实现提取的核心市场数据能力。"""

from __future__ import annotations

import calendar
import hashlib
import io
import json
import math
import re
import struct
import warnings
import zipfile
import zlib
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from typing import Any

import pandas as pd
import requests

from sequoia_x.data.classification import (
    classify_security,
    exchange_from_tdx,
    sse_security_subtype,
)
from sequoia_x.data.errors import DataIntegrityError, DataNotAvailableError, DataProviderError
from sequoia_x.data.models import (
    Adjustment,
    DailyBar,
    DailySnapshotResult,
    Exchange,
    InstrumentRecord,
    SecurityType,
    TradeCalendarDay,
)
from sequoia_x.data.providers.base import ProviderCapability

TDX_PACKAGE_URL = "https://www.tdx.com.cn/products/data/data/g4day/{ymd}.zip"
TDX_BJ_FIRST_DAY = "20220506"
TDX_MIN_PRICED = {"sh": 10_000, "sz": 3_000, "bj": 50}
SZSE_CALENDAR_URL = "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList"
EASTMONEY_LIST_URL = "https://82.push2.eastmoney.com/api/qt/clist/get"


def _iso_day(value: str) -> str:
    value = str(value).strip()
    fmt = "%Y%m%d" if re.fullmatch(r"\d{8}", value) else "%Y-%m-%d"
    return datetime.strptime(value, fmt).date().isoformat()


def _optional_iso_day(value: Any) -> str | None:
    if value is None or str(value).strip().lower() in {"", "nan", "nat", "none", "-"}:
        return None
    try:
        return pd.to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        return None


def _json_scalar(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _parse_bse_directory(text: str) -> tuple[int, list]:
    start = text.find("[")
    if start < 0:
        raise DataIntegrityError("北交所股票目录没有 JSON 数组")
    try:
        payload = json.loads(text[start : text.rfind("]") + 1])[0]
        return int(payload["totalPages"]), payload["content"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, IndexError) as exc:
        raise DataIntegrityError("北交所股票目录返回结构改变") from exc


class AStockDataProvider:
    """全市场盘后数据主 Provider，不在此层做策略或数据库操作。"""

    provider_name = "a-stock-data"
    capabilities = frozenset(
        {
            ProviderCapability.DAILY_SNAPSHOT,
            ProviderCapability.INSTRUMENT_DIRECTORY,
            ProviderCapability.TRADE_CALENDAR,
            ProviderCapability.ADJUSTMENT_FACTOR,
        }
    )

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "Sequoia-X/3.0 (+local research tool)"})

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        timeout = kwargs.pop("timeout", (10, 90))
        try:
            response = self.session.get(url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            if response is not None and response.status_code == 404:
                raise DataNotAvailableError(f"源站没有发布资源：{url}") from exc
            raise DataProviderError(f"请求数据源失败：{url}：{exc}") from exc

    def _post(self, url: str, **kwargs: Any) -> requests.Response:
        timeout = kwargs.pop("timeout", (10, 40))
        try:
            response = self.session.post(url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            raise DataProviderError(f"请求数据源失败：{url}：{exc}") from exc

    def fetch_daily_snapshot(self, trade_date: str) -> DailySnapshotResult:
        day = _iso_day(trade_date)
        ymd = day.replace("-", "")
        url = TDX_PACKAGE_URL.format(ymd=ymd)
        response = self._get(url)
        content = response.content
        if not content.startswith(b"PK"):
            raise DataIntegrityError("通达信盘后包不是 ZIP 文件，可能返回了错误页")
        try:
            rows = self._parse_package(content, ymd)
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            raise DataIntegrityError(f"通达信盘后包无法解压：{exc}") from exc

        instruments: dict[str, InstrumentRecord] = {}
        bars: list[DailyBar] = []
        counts: Counter[str] = Counter()
        for row in rows:
            exchange = exchange_from_tdx(row["market"])
            security_type = classify_security(exchange, row["code"], row["name"])
            instrument = InstrumentRecord(
                exchange=exchange,
                security_code=row["code"],
                name=row["name"],
                security_type=security_type,
                source=self.provider_name,
                source_identifier=f"{row['market']}{row['code']}",
                raw_payload={"market": row["market"], "code": row["code"], "name": row["name"]},
            )
            instruments[instrument.instrument_id] = instrument
            counts[security_type.value] += 1
            bars.append(
                DailyBar(
                    instrument_id=instrument.instrument_id,
                    trade_date=day,
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    previous_close=row["prev_close"],
                    volume=row["volume"],
                    amount=row["amount"],
                    provider=self.provider_name,
                    adjustment=Adjustment.RAW,
                    raw_payload=row,
                )
            )
        return DailySnapshotResult(
            trade_date=day,
            provider=self.provider_name,
            source_url=url,
            bars=tuple(bars),
            instruments=tuple(instruments.values()),
            checksum=hashlib.sha256(content).hexdigest(),
            metadata={"security_type_counts": dict(counts), "bytes": len(content)},
        )

    @staticmethod
    def _parse_package(content: bytes, ymd: str) -> list[dict[str, Any]]:
        archive = zipfile.ZipFile(io.BytesIO(content))
        names = set(archive.namelist())
        rows: list[dict[str, Any]] = []
        for market in ("sh", "sz", "bj"):
            cod_name, md1_name = f"{market}{ymd[2:]}.cod", f"{market}{ymd[2:]}.md1"
            if market == "bj" and ymd < TDX_BJ_FIRST_DAY and not {cod_name, md1_name} & names:
                continue
            if cod_name not in names or md1_name not in names:
                raise DataIntegrityError(f"盘后包缺少 {cod_name}/{md1_name}")
            cod, md1 = archive.read(cod_name), archive.read(md1_name)
            if len(cod) % 150 or len(md1) % 512 or len(cod) // 150 != len(md1) // 512:
                raise DataIntegrityError(f"{market} 代码表和行情块长度异常")
            before, codes, sequences = len(rows), set(), set()
            for offset in range(0, len(cod), 150):
                record = cod[offset : offset + 150]
                code = record[0:6].rstrip(b"\x00 ").decode("ascii", "replace")
                sequence = struct.unpack("<H", record[32:34])[0]
                if not re.fullmatch(r"\d{6}", code):
                    raise DataIntegrityError(f"{market} 出现非法代码 {code!r}")
                if code in codes or sequence in sequences:
                    raise DataIntegrityError(f"{market} 出现重复代码或行情块序号")
                codes.add(code)
                sequences.add(sequence)
                block = md1[sequence * 512 : (sequence + 1) * 512]
                if len(block) != 512:
                    raise DataIntegrityError(f"{market}{code} 行情块越界")
                prev_close = struct.unpack("<d", block[4:12])[0]
                open_, high, low, close = struct.unpack("<4d", block[12:44])
                amount = struct.unpack("<d", block[72:80])[0]
                numeric = (prev_close, open_, high, low, close, amount)
                if not all(math.isfinite(value) for value in numeric):
                    raise DataIntegrityError(f"{market}{code} 出现非有限数值")
                if close <= 0:
                    continue
                volume = struct.unpack("<Q", block[56:64])[0]
                raw_name = record[40:72].split(b"\x00")[0]
                try:
                    name = raw_name.decode("gbk").strip()
                except UnicodeDecodeError as exc:
                    raise DataIntegrityError(f"{market}{code} 名称不是 GBK") from exc
                if not name:
                    raise DataIntegrityError(f"{market}{code} 有价格但没有名称")
                rows.append(
                    {
                        "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
                        "market": market,
                        "code": code,
                        "name": name,
                        "prev_close": round(prev_close, 4),
                        "open": round(open_, 4),
                        "high": round(high, 4),
                        "low": round(low, 4),
                        "close": round(close, 4),
                        "volume": volume,
                        "amount": round(amount, 2),
                    }
                )
            if len(rows) - before < TDX_MIN_PRICED[market]:
                raise DataIntegrityError(
                    f"{market} 只有 {len(rows) - before} 条有价记录，低于完整性下限"
                )
        return rows

    def fetch_trade_calendar(self, start_date: str, end_date: str) -> tuple[TradeCalendarDay, ...]:
        start, end = (
            date.fromisoformat(_iso_day(start_date)),
            date.fromisoformat(_iso_day(end_date)),
        )
        if start > end:
            raise ValueError("start_date 不能晚于 end_date")
        result: dict[str, TradeCalendarDay] = {}
        cursor = start.replace(day=1)
        while cursor <= end:
            last = calendar.monthrange(cursor.year, cursor.month)[1]
            expected = {
                date(cursor.year, cursor.month, day).isoformat() for day in range(1, last + 1)
            }
            response = self._get(
                SZSE_CALENDAR_URL,
                params={"month": f"{cursor.year}-{cursor.month}"},
                headers={"Referer": "https://www.szse.cn/"},
                timeout=(10, 40),
            )
            try:
                payload = response.json()
            except requests.JSONDecodeError as exc:
                raise DataIntegrityError("深交所交易日历不是 JSON") from exc
            data = payload.get("data")
            if not isinstance(data, list) or not data:
                raise DataIntegrityError(f"深交所未返回 {cursor:%Y-%m} 的完整日历")
            actual: set[str] = set()
            for item in data:
                if str(item.get("jybz")) not in {"0", "1"} or not item.get("jyrq"):
                    raise DataIntegrityError("深交所交易日历字段异常")
                day = _iso_day(item["jyrq"])
                actual.add(day)
                result[day] = TradeCalendarDay(
                    calendar_date=day,
                    is_trading_day=str(item["jybz"]) == "1",
                    provider=self.provider_name,
                )
            if actual != expected:
                raise DataIntegrityError(f"{cursor:%Y-%m} 日历月份错位或日期不完整")
            cursor = cursor.replace(day=last) + timedelta(days=1)
        return tuple(
            result[day] for day in sorted(result) if start.isoformat() <= day <= end.isoformat()
        )

    def fetch_instruments(self, as_of_date: str) -> tuple[InstrumentRecord, ...]:
        """三交易所官方 A 股目录；官方源失败时降级到东财聚合目录。"""
        _iso_day(as_of_date)
        try:
            return self._fetch_official_a_share_instruments()
        except Exception as official_error:
            records = self._fetch_eastmoney_a_share_instruments()
            return tuple(
                InstrumentRecord(
                    exchange=record.exchange,
                    security_code=record.security_code,
                    name=record.name,
                    security_type=record.security_type,
                    security_subtype=record.security_subtype,
                    currency=record.currency,
                    list_date=record.list_date,
                    delist_date=record.delist_date,
                    listing_status=record.listing_status,
                    source=record.source,
                    source_identifier=record.source_identifier,
                    raw_payload={
                        **record.raw_payload,
                        "official_directory_error": str(official_error),
                    },
                )
                for record in records
            )

    def _fetch_official_a_share_instruments(self) -> tuple[InstrumentRecord, ...]:
        records: dict[str, InstrumentRecord] = {}
        sse_url = "https://query.sse.com.cn/sseQuery/commonQuery.do"
        for stock_type in ("1", "8"):
            response = self._get(
                sse_url,
                params={
                    "STOCK_TYPE": stock_type,
                    "REG_PROVINCE": "",
                    "CSRC_CODE": "",
                    "STOCK_CODE": "",
                    "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
                    "COMPANY_STATUS": "2,4,5,7,8",
                    "type": "inParams",
                    "isPagination": "true",
                    "pageHelp.cacheSize": "1",
                    "pageHelp.beginPage": "1",
                    "pageHelp.pageSize": "10000",
                    "pageHelp.pageNo": "1",
                    "pageHelp.endPage": "1",
                },
                headers={"Referer": "https://www.sse.com.cn/assortment/stock/list/share/"},
                timeout=(10, 40),
            )
            try:
                rows = response.json()["result"]
            except (ValueError, KeyError, TypeError) as exc:
                raise DataIntegrityError("上交所股票目录返回结构改变") from exc
            for row in rows:
                code = str(row.get("A_STOCK_CODE", ""))
                name = str(row.get("SEC_NAME_CN", "")).strip()
                if not re.fullmatch(r"\d{6}", code) or not name:
                    continue
                item = InstrumentRecord(
                    exchange=Exchange.SSE,
                    security_code=code,
                    name=name,
                    security_type=SecurityType.STOCK,
                    security_subtype=sse_security_subtype(stock_type),
                    list_date=_optional_iso_day(row.get("LIST_DATE")),
                    listing_status="LISTED",
                    source="sse-official",
                    source_identifier=code,
                    raw_payload=dict(row),
                )
                records[item.instrument_id] = item

        szse_url = "https://www.szse.cn/api/report/ShowReport"
        response = self._get(
            szse_url,
            params={"SHOWTYPE": "xlsx", "CATALOGID": "1110", "TABKEY": "tab1"},
            headers={"Referer": "https://www.szse.cn/market/product/stock/list/index.html"},
            timeout=(10, 40),
        )
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
                frame = pd.read_excel(BytesIO(response.content), dtype=str)
        except (ValueError, OSError) as exc:
            raise DataIntegrityError("深交所股票目录不是可解析的 XLSX") from exc
        required = {"A股代码", "A股简称", "A股上市日期"}
        if not required <= set(frame.columns):
            raise DataIntegrityError(
                f"深交所股票目录缺少字段：{sorted(required - set(frame.columns))}"
            )
        for row in frame.to_dict("records"):
            code = str(row.get("A股代码", "")).split(".", 1)[0].zfill(6)
            name = str(row.get("A股简称", "")).strip()
            if not re.fullmatch(r"\d{6}", code) or not name or name.lower() == "nan":
                continue
            item = InstrumentRecord(
                exchange=Exchange.SZSE,
                security_code=code,
                name=name,
                security_type=SecurityType.STOCK,
                list_date=_optional_iso_day(row.get("A股上市日期")),
                listing_status="LISTED",
                source="szse-official",
                source_identifier=code,
                raw_payload={key: _json_scalar(value) for key, value in row.items()},
            )
            records[item.instrument_id] = item

        bse_url = "https://www.bse.cn/nqxxController/nqxxCnzq.do"
        payload = {
            "page": "0",
            "typejb": "T",
            "xxfcbj[]": "2",
            "xxzqdm": "",
            "sortfield": "xxzqdm",
            "sorttype": "asc",
        }
        pages, all_rows = _parse_bse_directory(self._post(bse_url, data=payload).text)
        for page in range(1, pages):
            payload["page"] = str(page)
            _, page_rows = _parse_bse_directory(self._post(bse_url, data=payload).text)
            all_rows.extend(page_rows)
        for row in all_rows:
            if isinstance(row, dict):
                code = str(row.get("xxzqdm", "")).strip()
                name = str(row.get("xxzqjc", "")).strip()
                list_date = _optional_iso_day(row.get("fxssrq") or row.get("xxgprq"))
                raw_payload = dict(row)
            elif isinstance(row, list) and len(row) > 40:
                code, name = str(row[38]).strip(), str(row[40]).strip()
                list_date = _optional_iso_day(row[0])
                raw_payload = {"row": row}
            else:
                raise DataIntegrityError("北交所股票目录行结构改变")
            if not re.fullmatch(r"\d{6}", code) or not name:
                continue
            item = InstrumentRecord(
                exchange=Exchange.BSE,
                security_code=code,
                name=name,
                security_type=SecurityType.STOCK,
                list_date=list_date,
                listing_status="LISTED",
                source="bse-official",
                source_identifier=code,
                raw_payload=raw_payload,
            )
            records[item.instrument_id] = item
        if len(records) < 4_000:
            raise DataIntegrityError(f"三交易所官方目录合计仅 {len(records)} 只 A 股")
        return tuple(records.values())

    def _fetch_eastmoney_a_share_instruments(self) -> tuple[InstrumentRecord, ...]:
        params = {
            "pn": 1,
            "pz": 10_000,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f12",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": "f12,f13,f14",
        }
        response = self._get(EASTMONEY_LIST_URL, params=params, timeout=(10, 40))
        try:
            rows = response.json()["data"]["diff"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DataIntegrityError("统一 A 股目录返回结构改变") from exc
        if not isinstance(rows, list) or len(rows) < 4_000:
            raise DataIntegrityError(
                f"统一 A 股目录仅返回 {len(rows) if isinstance(rows, list) else 0} 条"
            )
        instruments: dict[str, InstrumentRecord] = {}
        for row in rows:
            code, name = str(row.get("f12", "")), str(row.get("f14", "")).strip()
            if not re.fullmatch(r"\d{6}", code) or not name:
                raise DataIntegrityError("统一 A 股目录存在非法代码或空名称")
            market = int(row.get("f13", -1))
            exchange = (
                Exchange.SSE
                if market == 1
                else Exchange.BSE
                if code.startswith(("4", "8", "92"))
                else Exchange.SZSE
            )
            instrument = InstrumentRecord(
                exchange=exchange,
                security_code=code,
                name=name,
                security_type=SecurityType.STOCK,
                listing_status="LISTED",
                source="eastmoney-directory",
                source_identifier=str(market),
                raw_payload=dict(row),
            )
            instruments[instrument.instrument_id] = instrument
        return tuple(instruments.values())

    def fetch_adjustment_factors(self, code: str, kind: str = "qfq") -> tuple[dict[str, Any], ...]:
        if kind not in {"qfq", "hfq"}:
            raise ValueError("kind 只能是 qfq 或 hfq")
        match = re.fullmatch(r"(?:(sh|sz|bj))?(\d{6})", code.lower())
        if not match:
            raise ValueError("代码必须是六位数字，可带 sh/sz/bj 前缀")
        prefix, digits = match.groups()
        prefix = prefix or ("sh" if digits.startswith(("5", "6", "9")) else "sz")
        url = f"https://finance.sina.com.cn/realstock/company/{prefix}{digits}/{kind}.js"
        text = self._get(url, headers={"Referer": "https://finance.sina.com.cn/"}).text
        brace = text.find("{")
        if brace < 0:
            raise DataIntegrityError("新浪复权因子响应没有 JSON")
        try:
            data, _ = json.JSONDecoder().raw_decode(text[brace:])
            return tuple(
                {"date": item["d"], "factor": float(item["f"])} for item in data.get("data", [])
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise DataIntegrityError(f"新浪复权因子解析失败：{exc}") from exc


SOURCE_ARCHIVE_VERSION = "v3.10.2"
SOURCE_ARCHIVE_FETCHED_AT = datetime.now(UTC).isoformat()
