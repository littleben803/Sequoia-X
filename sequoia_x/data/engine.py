"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
import time
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass
from datetime import date
from io import StringIO
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


class DataQualityError(RuntimeError):
    """数据未通过策略运行前的质量闸门。"""


@dataclass(frozen=True)
class SyncReport:
    """单次增量同步的可观测结果。"""

    requested_symbols: int
    received_symbols: int
    failed_symbols: int
    rows_written: int
    invalid_rows_dropped: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MarketSymbol:
    """当前市场中的股票基础信息。"""

    symbol: str
    name: str
    ipo_date: str | None = None

    @property
    def display_name(self) -> str:
        return f"{self.name}（{self.symbol}）" if self.name else self.symbol


@dataclass(frozen=True)
class BackfillTask:
    """一只股票需要补齐的连续日期区间。"""

    symbol: str
    name: str
    queue: str
    missing_ranges: tuple[tuple[str, str], ...]
    last_local_date: str | None
    gap_count: int

    @property
    def display_name(self) -> str:
        return f"{self.name}（{self.symbol}）" if self.name else self.symbol


@dataclass(frozen=True)
class BackfillPlan:
    """按缺失、陈旧/缺口、普通增量分类后的执行计划。"""

    target_trade_date: str
    trading_dates: tuple[str, ...]
    missing_tasks: tuple[BackfillTask, ...]
    stale_tasks: tuple[BackfillTask, ...]
    incremental_tasks: tuple[BackfillTask, ...]
    current_symbols: tuple[MarketSymbol, ...]

    @property
    def tasks(self) -> tuple[BackfillTask, ...]:
        return self.missing_tasks + self.stale_tasks + self.incremental_tasks

    @property
    def requested_symbols(self) -> int:
        return len(self.tasks)


@dataclass(frozen=True)
class BackfillProgress:
    """单只股票处理完成后发出的结构化进度事件。"""

    current_symbol: str | None
    processed_symbols: int
    requested_symbols: int
    successful_symbols: int
    up_to_date_symbols: int
    empty_symbols: int
    failed_symbols: int
    rows_written: int
    activity: str = "completed"
    current_name: str = ""
    queue: str = ""
    repaired_gap_dates: int = 0
    suspended_dates: int = 0
    queue_processed: int | None = None

    @property
    def skipped_symbols(self) -> int:
        return self.up_to_date_symbols + self.empty_symbols


@dataclass(frozen=True)
class BackfillReport:
    """一次全市场回填的结构化结果，可供终端或未来 Web UI 使用。"""

    requested_symbols: int
    processed_symbols: int
    successful_symbols: int
    up_to_date_symbols: int
    empty_symbols: int
    failed_symbols: int
    rows_written: int
    elapsed_seconds: float
    completed: bool
    aborted_reason: str | None = None
    failed_codes: tuple[str, ...] = ()
    repaired_gap_dates: int = 0
    suspended_dates: int = 0

    @property
    def skipped_symbols(self) -> int:
        return self.up_to_date_symbols + self.empty_symbols

    @property
    def progress(self) -> float:
        return self.processed_symbols / self.requested_symbols if self.requested_symbols else 0.0

    @property
    def succeeded(self) -> bool:
        return self.completed and self.failed_symbols == 0 and self.aborted_reason is None

    @property
    def elapsed_display(self) -> str:
        seconds = max(int(self.elapsed_seconds), 0)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["skipped_symbols"] = self.skipped_symbols
        data["progress"] = self.progress
        data["succeeded"] = self.succeeded
        return data


@dataclass(frozen=True)
class DataAuditReport:
    """本地行情库质量快照。"""

    ready: bool
    latest_date: str | None
    total_symbols: int
    stored_symbols: int
    latest_symbols: int
    stale_symbols: int
    latest_coverage: float
    invalid_rows: int
    future_rows: int
    data_age_days: int | None
    problems: tuple[str, ...]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["problems"] = list(self.problems)
        return data

    def require_ready(self) -> None:
        if not self.ready:
            raise DataQualityError("；".join(self.problems))


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""

_CREATE_UNIVERSE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS market_universe (
    symbol          TEXT PRIMARY KEY,
    last_seen_date  TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    ipo_date        TEXT
);
"""

_CREATE_TRADE_CALENDAR_SQL = """
CREATE TABLE IF NOT EXISTS trade_calendar (
    date             TEXT PRIMARY KEY,
    is_trading_day   INTEGER NOT NULL,
    last_refreshed   TEXT NOT NULL
);
"""

_CREATE_NO_DATA_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS symbol_no_data (
    symbol        TEXT NOT NULL,
    date          TEXT NOT NULL,
    reason        TEXT NOT NULL,
    last_checked  TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);
"""


def _bs_fetch_batch(tasks: list) -> list:
    """多进程 worker：独立 login，批量拉取 baostock 数据。"""
    import baostock as bs

    results = []
    received_symbols: set[str] = set()
    failed_symbols: list[str] = []
    with redirect_stdout(StringIO()):
        login_result = bs.login()
        if login_result.error_code != "0":
            return [results, received_symbols, [task[0] for task in tasks]]

        try:
            for symbol, bs_code, start, end in tasks:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,volume,amount",
                    start_date=start,
                    end_date=end,
                    frequency="d",
                    adjustflag="1",  # 后复权
                )
                if rs.error_code != "0":
                    failed_symbols.append(symbol)
                    continue
                while rs.next():
                    results.append([symbol] + rs.get_row_data())
                    received_symbols.add(symbol)
        finally:
            bs.logout()
    return [results, received_symbols, failed_symbols]


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.execute(_CREATE_UNIVERSE_TABLE_SQL)
            universe_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(market_universe)").fetchall()
            }
            if "name" not in universe_columns:
                conn.execute("ALTER TABLE market_universe ADD COLUMN name TEXT NOT NULL DEFAULT ''")
            if "ipo_date" not in universe_columns:
                conn.execute("ALTER TABLE market_universe ADD COLUMN ipo_date TEXT")
            conn.execute(_CREATE_TRADE_CALENDAR_SQL)
            conn.execute(_CREATE_NO_DATA_TABLE_SQL)
            conn.commit()
        logger.info("✅ 本地数据库就绪：%s", self.db_path)

    def replace_market_universe(self, symbols: list[str] | list[MarketSymbol]) -> None:
        """原子替换最近一次从数据源取得的全市场股票清单。"""
        snapshot_date = date.today().isoformat()
        records: dict[str, MarketSymbol] = {}
        for item in symbols:
            record = item if isinstance(item, MarketSymbol) else MarketSymbol(item, "")
            records[record.symbol] = record
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM market_universe")
            conn.executemany(
                """
                INSERT INTO market_universe (symbol, last_seen_date, name, ipo_date)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (record.symbol, snapshot_date, record.name, record.ipo_date)
                    for record in sorted(records.values(), key=lambda value: value.symbol)
                ),
            )
            conn.commit()

    def replace_trade_calendar(self, trading_dates: list[str]) -> None:
        """保存给定范围内的有效交易日。"""
        refreshed_at = date.today().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM trade_calendar")
            conn.executemany(
                """
                INSERT INTO trade_calendar (date, is_trading_day, last_refreshed)
                VALUES (?, 1, ?)
                """,
                ((day, refreshed_at) for day in sorted(set(trading_dates))),
            )
            conn.commit()

    @staticmethod
    def _group_missing_dates(
        missing_dates: list[str], trading_dates: tuple[str, ...]
    ) -> tuple[tuple[str, str], ...]:
        if not missing_dates:
            return ()
        positions = {day: index for index, day in enumerate(trading_dates)}
        ranges: list[tuple[str, str]] = []
        start = previous = missing_dates[0]
        for current in missing_dates[1:]:
            if positions[current] == positions[previous] + 1:
                previous = current
                continue
            ranges.append((start, previous))
            start = previous = current
        ranges.append((start, previous))
        return tuple(ranges)

    def build_backfill_plan(self) -> BackfillPlan:
        """使用市场清单、交易日历和本地覆盖状态生成优先级队列。"""
        with sqlite3.connect(self.db_path) as conn:
            universe = [
                MarketSymbol(symbol=row[0], name=row[1] or "", ipo_date=row[2])
                for row in conn.execute(
                    "SELECT symbol, name, ipo_date FROM market_universe ORDER BY symbol"
                ).fetchall()
            ]
            trading_dates = tuple(
                row[0]
                for row in conn.execute(
                    """
                    SELECT date FROM trade_calendar
                    WHERE is_trading_day = 1
                    ORDER BY date
                    """
                ).fetchall()
            )
            if not universe:
                raise DataQualityError("缺少全市场股票清单，无法生成回填计划")
            if not trading_dates:
                raise DataQualityError("缺少交易日历，无法生成回填计划")
            first_day, target_day = trading_dates[0], trading_dates[-1]
            existing_by_symbol: dict[str, set[str]] = {}
            for symbol, day in conn.execute(
                """
                SELECT symbol, date FROM stock_daily
                WHERE date BETWEEN ? AND ?
                """,
                (first_day, target_day),
            ):
                existing_by_symbol.setdefault(symbol, set()).add(day)
            no_data_by_symbol: dict[str, set[str]] = {}
            for symbol, day in conn.execute(
                """
                SELECT symbol, date FROM symbol_no_data
                WHERE date BETWEEN ? AND ?
                """,
                (first_day, target_day),
            ):
                no_data_by_symbol.setdefault(symbol, set()).add(day)

        previous_trade_day = trading_dates[-2] if len(trading_dates) > 1 else target_day
        missing_tasks: list[BackfillTask] = []
        stale_tasks: list[BackfillTask] = []
        incremental_tasks: list[BackfillTask] = []
        current_symbols: list[MarketSymbol] = []

        for stock in universe:
            expected_start = max(self.start_date, stock.ipo_date or self.start_date)
            expected_dates = tuple(day for day in trading_dates if day >= expected_start)
            existing_dates = existing_by_symbol.get(stock.symbol, set())
            known_no_data = no_data_by_symbol.get(stock.symbol, set())
            missing_dates = [
                day
                for day in expected_dates
                if day not in existing_dates and day not in known_no_data
            ]
            if not missing_dates:
                current_symbols.append(stock)
                continue

            last_local_date = max(existing_dates) if existing_dates else None
            ranges = self._group_missing_dates(missing_dates, trading_dates)
            if not existing_dates:
                missing_tasks.append(
                    BackfillTask(
                        stock.symbol,
                        stock.name,
                        "missing",
                        ranges,
                        None,
                        len(missing_dates),
                    )
                )
                continue

            internal_gaps = [day for day in missing_dates if day <= last_local_date]
            task = BackfillTask(
                stock.symbol,
                stock.name,
                "stale" if internal_gaps or last_local_date < previous_trade_day else "incremental",
                ranges,
                last_local_date,
                len(internal_gaps),
            )
            if task.queue == "stale":
                stale_tasks.append(task)
            else:
                incremental_tasks.append(task)

        stale_tasks.sort(
            key=lambda task: (task.last_local_date or "", -task.gap_count, task.symbol)
        )
        return BackfillPlan(
            target_trade_date=target_day,
            trading_dates=trading_dates,
            missing_tasks=tuple(sorted(missing_tasks, key=lambda task: task.symbol)),
            stale_tasks=tuple(stale_tasks),
            incremental_tasks=tuple(sorted(incremental_tasks, key=lambda task: task.symbol)),
            current_symbols=tuple(sorted(current_symbols, key=lambda stock: stock.symbol)),
        )

    def _get_last_date(self, symbol: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    # ── 数据同步 ──

    def sync_today_bulk(self) -> SyncReport:
        """多进程并行通过 baostock 拉取增量数据（后复权），写入 SQLite。"""
        from datetime import date, timedelta
        from multiprocessing import Pool

        today_str = date.today().strftime("%Y-%m-%d")

        tasks = []
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return SyncReport(0, 0, 0, 0)

        for symbol, last_date in rows:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return SyncReport(0, 0, 0, 0)

        logger.info(f"需要更新 {len(tasks)} 只股票，启动多进程并行拉取...")

        n_workers = min(8, len(tasks))
        chunks = [tasks[i::n_workers] for i in range(n_workers)]

        with Pool(n_workers) as pool:
            batch_results = pool.map(_bs_fetch_batch, chunks)

        all_rows = []
        received_symbols: set[str] = set()
        explicit_failures: set[str] = set()
        for batch_rows, batch_symbols, batch_failures in batch_results:
            all_rows.extend(batch_rows)
            received_symbols.update(batch_symbols)
            explicit_failures.update(batch_failures)

        if not all_rows:
            logger.info("无新数据（可能非交易日）")
            return SyncReport(
                requested_symbols=len(tasks),
                received_symbols=0,
                failed_symbols=len(explicit_failures),
                rows_written=0,
            )

        df = pd.DataFrame(
            all_rows,
            columns=["symbol", "date", "open", "high", "low", "close", "volume", "turnover"],
        )
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        original_count = len(df)
        valid_ohlcv = (
            df[["open", "high", "low", "close", "volume", "turnover"]].notna().all(axis=1)
            & (df[["open", "high", "low", "close"]] > 0).all(axis=1)
            & (df["volume"] > 0)
            & (df["turnover"] >= 0)
            & (df["high"] >= df[["open", "close", "low"]].max(axis=1))
            & (df["low"] <= df[["open", "close", "high"]].min(axis=1))
        )
        df = df[valid_ohlcv].copy()
        invalid_rows_dropped = original_count - len(df)

        if df.empty:
            logger.error("拉取到的数据全部未通过 OHLCV 合法性检查")
            return SyncReport(
                requested_symbols=len(tasks),
                received_symbols=len(received_symbols),
                failed_symbols=len(explicit_failures),
                rows_written=0,
                invalid_rows_dropped=invalid_rows_dropped,
            )

        count = len(df)
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO stock_daily
                    (symbol, date, open, high, low, close, volume, turnover)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, date) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    turnover=excluded.turnover
                """,
                df[
                    ["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]
                ].itertuples(index=False, name=None),
            )
            conn.commit()

        missing_responses = {task[0] for task in tasks} - received_symbols
        failed_count = len(explicit_failures | missing_responses)
        report = SyncReport(
            requested_symbols=len(tasks),
            received_symbols=len(received_symbols),
            failed_symbols=failed_count,
            rows_written=count,
            invalid_rows_dropped=invalid_rows_dropped,
        )
        logger.info(
            "同步结果：请求 %s 只 | 返回 %s 只 | 失败/无数据 %s 只 | 写入 %s 行 | 丢弃异常 %s 行",
            report.requested_symbols,
            report.received_symbols,
            report.failed_symbols,
            report.rows_written,
            report.invalid_rows_dropped,
        )
        return report

    def audit_data(self) -> DataAuditReport:
        """审计本地数据库，返回策略扫描前的质量闸门结果。"""
        with sqlite3.connect(self.db_path) as conn:
            latest_date = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()[0]
            stored_symbols = conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM stock_daily"
            ).fetchone()[0]
            universe_symbols = conn.execute("SELECT COUNT(*) FROM market_universe").fetchone()[0]
            total_symbols = universe_symbols or stored_symbols
            calendar_latest = conn.execute(
                "SELECT MAX(date) FROM trade_calendar WHERE is_trading_day = 1"
            ).fetchone()[0]
            coverage_date = calendar_latest or latest_date
            latest_symbols = 0
            if coverage_date:
                if universe_symbols:
                    latest_symbols = conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM market_universe AS u
                        WHERE EXISTS (
                            SELECT 1 FROM stock_daily AS d
                            WHERE d.symbol = u.symbol AND d.date = ?
                        ) OR EXISTS (
                            SELECT 1 FROM symbol_no_data AS n
                            WHERE n.symbol = u.symbol
                              AND n.date = ?
                              AND n.reason = 'suspended'
                        )
                        """,
                        (coverage_date, coverage_date),
                    ).fetchone()[0]
                else:
                    latest_symbols = conn.execute(
                        "SELECT COUNT(DISTINCT symbol) FROM stock_daily WHERE date = ?",
                        (coverage_date,),
                    ).fetchone()[0]
            invalid_rows = conn.execute(
                """
                SELECT COUNT(*) FROM stock_daily
                WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
                   OR volume IS NULL OR turnover IS NULL
                   OR open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
                   OR volume <= 0 OR turnover < 0
                   OR high < open OR high < close OR high < low
                   OR low > open OR low > close OR low > high
                """
            ).fetchone()[0]
            future_rows = conn.execute(
                "SELECT COUNT(*) FROM stock_daily WHERE date > ?",
                (date.today().isoformat(),),
            ).fetchone()[0]

        latest_coverage = latest_symbols / total_symbols if total_symbols else 0.0
        stale_symbols = max(total_symbols - latest_symbols, 0)
        data_age_days = None
        problems: list[str] = []

        if not latest_date or total_symbols == 0:
            problems.append("本地没有行情数据，请先执行 --backfill")
        else:
            data_age_days = (date.today() - date.fromisoformat(latest_date)).days
            if data_age_days > self.settings.max_data_age_days:
                problems.append(
                    f"最新数据为 {latest_date}，距今 {data_age_days} 天，"
                    f"超过上限 {self.settings.max_data_age_days} 天"
                )
            if latest_coverage < self.settings.minimum_latest_coverage:
                scope = "全市场最新交易日" if universe_symbols else "最新交易日"
                problems.append(
                    f"{scope}覆盖率 {latest_coverage:.2%}，"
                    f"低于闸值 {self.settings.minimum_latest_coverage:.2%}"
                )

        if invalid_rows:
            problems.append(f"数据库含 {invalid_rows} 行非法 OHLCV 数据")
        if future_rows:
            problems.append(f"数据库含 {future_rows} 行未来日期数据")

        return DataAuditReport(
            ready=not problems,
            latest_date=latest_date,
            total_symbols=total_symbols,
            stored_symbols=stored_symbols,
            latest_symbols=latest_symbols,
            stale_symbols=stale_symbols,
            latest_coverage=latest_coverage,
            invalid_rows=invalid_rows,
            future_rows=future_rows,
            data_age_days=data_age_days,
            problems=tuple(problems),
        )

    def backfill(
        self,
        symbols: list[str],
        progress_callback: Callable[[BackfillProgress], None] | None = None,
    ) -> BackfillReport:
        """回填全市场后复权日 K，并返回可审计的结构化结果。"""
        from datetime import timedelta

        import baostock as bs

        started_at = time.monotonic()
        today_str = date.today().isoformat()
        max_retries = 3
        reconnect_interval = 200
        requested = len(symbols)
        processed = successful = up_to_date = empty = failed = rows_written = 0
        failed_codes: list[str] = []
        connected = False

        def quiet_call(function, *args, **kwargs):
            with redirect_stdout(StringIO()):
                return function(*args, **kwargs)

        def login() -> tuple[bool, str | None]:
            try:
                result = quiet_call(bs.login)
            except Exception as exc:
                return False, str(exc)
            if result.error_code != "0":
                return False, result.error_msg or "未知连接错误"
            return True, None

        def logout() -> None:
            nonlocal connected
            if not connected:
                return
            try:
                quiet_call(bs.logout)
            except Exception as exc:
                logger.warning("⚠️ 数据服务会话关闭异常，将在下次连接时重建：%s", exc)
            connected = False

        def reconnect(reason: str) -> tuple[bool, str | None]:
            nonlocal connected
            logger.info("🔄 正在刷新数据服务连接：%s", reason)
            logout()
            last_error: str | None = None
            for attempt in range(1, max_retries + 1):
                connected, last_error = login()
                if connected:
                    logger.info("✅ 数据服务连接成功（%s）", reason)
                    return True, None
                logger.warning(
                    "⚠️ 数据服务第 %s/%s 次重连失败：%s",
                    attempt,
                    max_retries,
                    last_error,
                )
                if attempt < max_retries:
                    wait = 2**attempt
                    logger.info("⏳ %s 秒后继续重连……", wait)
                    time.sleep(wait)
            return False, last_error

        def emit(symbol: str | None, activity: str = "completed") -> None:
            if progress_callback is None:
                return
            progress_callback(
                BackfillProgress(
                    current_symbol=symbol,
                    processed_symbols=processed,
                    requested_symbols=requested,
                    successful_symbols=successful,
                    up_to_date_symbols=up_to_date,
                    empty_symbols=empty,
                    failed_symbols=failed,
                    rows_written=rows_written,
                    activity=activity,
                )
            )

        def report(completed: bool, aborted_reason: str | None = None) -> BackfillReport:
            return BackfillReport(
                requested_symbols=requested,
                processed_symbols=processed,
                successful_symbols=successful,
                up_to_date_symbols=up_to_date,
                empty_symbols=empty,
                failed_symbols=failed,
                rows_written=rows_written,
                elapsed_seconds=time.monotonic() - started_at,
                completed=completed,
                aborted_reason=aborted_reason,
                failed_codes=tuple(failed_codes),
            )

        connected, connection_error = login()
        if not connected:
            return report(False, f"首次连接数据服务失败：{connection_error}")
        logger.info("✅ 数据服务连接成功（匿名行情会话，不涉及个人账号）")

        try:
            since_reconnect = 0
            for symbol in symbols:
                last_date = self._get_last_date(symbol)
                if last_date and last_date >= today_str:
                    up_to_date += 1
                    processed += 1
                    emit(symbol)
                    continue

                emit(symbol, "fetching")

                if since_reconnect >= reconnect_interval:
                    ok, error = reconnect(f"已连续处理 {since_reconnect} 只股票，主动刷新长连接")
                    if not ok:
                        return report(False, f"数据服务重连失败：{error}")
                    since_reconnect = 0

                start = last_date or self.start_date
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()

                query_ok = False
                query_rows: list[list[str]] = []
                fields: list[str] = []
                last_error = "未知错误"
                for attempt in range(1, max_retries + 1):
                    try:
                        with redirect_stdout(StringIO()):
                            result = bs.query_history_k_data_plus(
                                self._to_baostock_code(symbol),
                                "date,open,high,low,close,volume,amount",
                                start_date=start,
                                end_date=today_str,
                                frequency="d",
                                adjustflag="1",
                            )
                            if result.error_code != "0":
                                raise RuntimeError(result.error_msg)
                            query_rows = []
                            while result.next():
                                query_rows.append(result.get_row_data())
                            fields = result.fields
                        query_ok = True
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        if attempt < max_retries:
                            wait = 2**attempt
                            logger.warning(
                                "⚠️ [%s] 拉取失败：%s；%s 秒后重试（%s/%s）",
                                symbol,
                                last_error,
                                wait,
                                attempt,
                                max_retries,
                            )
                            time.sleep(wait)
                            ok, reconnect_error = reconnect(f"恢复股票 {symbol} 的数据请求")
                            if not ok:
                                return report(False, f"数据服务重连失败：{reconnect_error}")

                if not query_ok:
                    failed += 1
                    failed_codes.append(symbol)
                    processed += 1
                    since_reconnect += 1
                    logger.error("❌ [%s] 连续 %s 次拉取失败，已记录后继续", symbol, max_retries)
                    emit(symbol)
                    continue

                if not query_rows:
                    empty += 1
                    processed += 1
                    since_reconnect += 1
                    emit(symbol)
                    continue

                frame = pd.DataFrame(query_rows, columns=fields)
                for column in ["open", "high", "low", "close", "volume", "amount"]:
                    frame[column] = pd.to_numeric(frame[column], errors="coerce")
                valid = (
                    frame[["open", "high", "low", "close", "volume", "amount"]].notna().all(axis=1)
                    & (frame[["open", "high", "low", "close"]] > 0).all(axis=1)
                    & (frame["volume"] > 0)
                    & (frame["amount"] >= 0)
                    & (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
                    & (frame["low"] <= frame[["open", "close", "high"]].min(axis=1))
                )
                frame = frame[valid].copy()
                if frame.empty:
                    empty += 1
                    processed += 1
                    since_reconnect += 1
                    emit(symbol)
                    continue

                frame["symbol"] = symbol
                frame = frame.rename(columns={"amount": "turnover"})
                columns = ["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]
                with sqlite3.connect(self.db_path) as conn:
                    conn.executemany(
                        """
                        INSERT INTO stock_daily
                            (symbol, date, open, high, low, close, volume, turnover)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(symbol, date) DO UPDATE SET
                            open=excluded.open,
                            high=excluded.high,
                            low=excluded.low,
                            close=excluded.close,
                            volume=excluded.volume,
                            turnover=excluded.turnover
                        """,
                        frame[columns].itertuples(index=False, name=None),
                    )
                    conn.commit()

                successful += 1
                processed += 1
                since_reconnect += 1
                rows_written += len(frame)
                emit(symbol)
        finally:
            logout()

        return report(True)

    def execute_backfill_plan(
        self,
        plan: BackfillPlan,
        progress_callback: Callable[[BackfillProgress], None] | None = None,
    ) -> BackfillReport:
        """依次执行缺失、陈旧/缺口、普通增量三个回填队列。"""
        import baostock as bs

        started_at = time.monotonic()
        requested = plan.requested_symbols + len(plan.current_symbols)
        processed = up_to_date = len(plan.current_symbols)
        successful = empty = failed = rows_written = 0
        repaired_gap_dates = suspended_dates = 0
        failed_codes: list[str] = []
        connected = False
        max_retries = 3
        reconnect_interval = 200

        def quiet_call(function, *args, **kwargs):
            with redirect_stdout(StringIO()):
                return function(*args, **kwargs)

        def login() -> tuple[bool, str | None]:
            try:
                result = quiet_call(bs.login)
            except Exception as exc:
                return False, str(exc)
            if result.error_code != "0":
                return False, result.error_msg or "未知连接错误"
            return True, None

        def logout() -> None:
            nonlocal connected
            if not connected:
                return
            try:
                quiet_call(bs.logout)
            except Exception as exc:
                logger.warning("⚠️ 数据服务会话关闭异常，将在下次连接时重建：%s", exc)
            connected = False

        def reconnect(reason: str) -> tuple[bool, str | None]:
            nonlocal connected
            logger.info("🔄 正在刷新数据服务连接：%s", reason)
            logout()
            last_error: str | None = None
            for attempt in range(1, max_retries + 1):
                connected, last_error = login()
                if connected:
                    logger.info("✅ 数据服务连接成功（%s）", reason)
                    return True, None
                logger.warning(
                    "⚠️ 数据服务第 %s/%s 次重连失败：%s",
                    attempt,
                    max_retries,
                    last_error,
                )
                if attempt < max_retries:
                    wait = 2**attempt
                    logger.info("⏳ %s 秒后继续重连……", wait)
                    time.sleep(wait)
            return False, last_error

        def emit(task: BackfillTask, queue_processed: int, activity: str) -> None:
            if progress_callback is None:
                return
            progress_callback(
                BackfillProgress(
                    current_symbol=task.symbol,
                    current_name=task.name,
                    queue=task.queue,
                    processed_symbols=processed,
                    requested_symbols=requested,
                    successful_symbols=successful,
                    up_to_date_symbols=up_to_date,
                    empty_symbols=empty,
                    failed_symbols=failed,
                    rows_written=rows_written,
                    activity=activity,
                    repaired_gap_dates=repaired_gap_dates,
                    suspended_dates=suspended_dates,
                    queue_processed=queue_processed,
                )
            )

        def make_report(completed: bool, reason: str | None = None) -> BackfillReport:
            return BackfillReport(
                requested_symbols=requested,
                processed_symbols=processed,
                successful_symbols=successful,
                up_to_date_symbols=up_to_date,
                empty_symbols=empty,
                failed_symbols=failed,
                rows_written=rows_written,
                elapsed_seconds=time.monotonic() - started_at,
                completed=completed,
                aborted_reason=reason,
                failed_codes=tuple(failed_codes),
                repaired_gap_dates=repaired_gap_dates,
                suspended_dates=suspended_dates,
            )

        if plan.requested_symbols == 0:
            logger.info("✅ 所有股票均已覆盖目标交易日且不存在待修复缺口")
            return make_report(True)

        connected, connection_error = login()
        if not connected:
            return make_report(False, f"首次连接数据服务失败：{connection_error}")
        logger.info("✅ 数据服务连接成功（匿名行情会话，不涉及个人账号）")

        trading_dates = set(plan.trading_dates)
        since_reconnect = 0
        queue_groups = (
            plan.missing_tasks,
            plan.stale_tasks,
            plan.incremental_tasks,
        )
        try:
            for tasks in queue_groups:
                for queue_index, task in enumerate(tasks, start=1):
                    emit(task, queue_index - 1, "fetching")
                    if since_reconnect >= reconnect_interval:
                        ok, error = reconnect(
                            f"已连续处理 {since_reconnect} 只股票，主动刷新长连接"
                        )
                        if not ok:
                            return make_report(False, f"数据服务重连失败：{error}")
                        since_reconnect = 0

                    valid_frames: list[pd.DataFrame] = []
                    resolved_dates: set[str] = set()
                    suspended_for_task: set[str] = set()
                    query_failed = False

                    for range_start, range_end in task.missing_ranges:
                        range_rows: list[list[str]] = []
                        fields: list[str] = []
                        range_ok = False
                        last_error = "未知错误"
                        for attempt in range(1, max_retries + 1):
                            try:
                                with redirect_stdout(StringIO()):
                                    result = bs.query_history_k_data_plus(
                                        self._to_baostock_code(task.symbol),
                                        "date,open,high,low,close,volume,amount,tradestatus",
                                        start_date=range_start,
                                        end_date=range_end,
                                        frequency="d",
                                        adjustflag="1",
                                    )
                                    if result.error_code != "0":
                                        raise RuntimeError(result.error_msg)
                                    while result.next():
                                        range_rows.append(result.get_row_data())
                                    fields = result.fields
                                range_ok = True
                                break
                            except Exception as exc:
                                last_error = str(exc)
                                if attempt < max_retries:
                                    wait = 2**attempt
                                    logger.warning(
                                        "⚠️ [%s] 区间 %s～%s 拉取失败：%s；%s 秒后重试（%s/%s）",
                                        task.display_name,
                                        range_start,
                                        range_end,
                                        last_error,
                                        wait,
                                        attempt,
                                        max_retries,
                                    )
                                    time.sleep(wait)
                                    ok, reconnect_error = reconnect(
                                        f"恢复 {task.display_name} 的缺口请求"
                                    )
                                    if not ok:
                                        return make_report(
                                            False,
                                            f"数据服务重连失败：{reconnect_error}",
                                        )
                        if not range_ok:
                            logger.error(
                                "❌ [%s] 区间 %s～%s 连续拉取失败：%s",
                                task.display_name,
                                range_start,
                                range_end,
                                last_error,
                            )
                            query_failed = True
                            break

                        expected_range_dates = {
                            day for day in trading_dates if range_start <= day <= range_end
                        }
                        if not range_rows:
                            continue
                        frame = pd.DataFrame(range_rows, columns=fields)
                        suspended = frame[frame["tradestatus"] == "0"]
                        suspended_for_task.update(
                            day for day in suspended["date"].tolist() if day in expected_range_dates
                        )
                        active = frame[frame["tradestatus"] != "0"].copy()
                        for column in ["open", "high", "low", "close", "volume", "amount"]:
                            active[column] = pd.to_numeric(active[column], errors="coerce")
                        valid = (
                            active[["open", "high", "low", "close", "volume", "amount"]]
                            .notna()
                            .all(axis=1)
                            & (active[["open", "high", "low", "close"]] > 0).all(axis=1)
                            & (active["volume"] > 0)
                            & (active["amount"] >= 0)
                            & (active["high"] >= active[["open", "close", "low"]].max(axis=1))
                            & (active["low"] <= active[["open", "close", "high"]].min(axis=1))
                        )
                        active = active[valid].copy()
                        if not active.empty:
                            active["symbol"] = task.symbol
                            active = active.rename(columns={"amount": "turnover"})
                            valid_frames.append(active)
                            resolved_dates.update(active["date"].tolist())

                    expected_dates = {
                        day
                        for start, end in task.missing_ranges
                        for day in trading_dates
                        if start <= day <= end
                    }
                    resolved_dates.update(suspended_for_task)
                    unresolved_dates = expected_dates - resolved_dates

                    if valid_frames or suspended_for_task:
                        with sqlite3.connect(self.db_path) as conn:
                            if valid_frames:
                                combined = pd.concat(valid_frames, ignore_index=True)
                                columns = [
                                    "symbol",
                                    "date",
                                    "open",
                                    "high",
                                    "low",
                                    "close",
                                    "volume",
                                    "turnover",
                                ]
                                conn.executemany(
                                    """
                                    INSERT INTO stock_daily
                                        (symbol, date, open, high, low, close, volume, turnover)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                    ON CONFLICT(symbol, date) DO UPDATE SET
                                        open=excluded.open,
                                        high=excluded.high,
                                        low=excluded.low,
                                        close=excluded.close,
                                        volume=excluded.volume,
                                        turnover=excluded.turnover
                                    """,
                                    combined[columns].itertuples(index=False, name=None),
                                )
                                conn.executemany(
                                    "DELETE FROM symbol_no_data WHERE symbol = ? AND date = ?",
                                    ((task.symbol, day) for day in combined["date"].tolist()),
                                )
                                rows_written += len(combined)
                                if task.last_local_date:
                                    repaired_gap_dates += sum(
                                        day <= task.last_local_date
                                        for day in combined["date"].tolist()
                                    )
                            if suspended_for_task:
                                checked = date.today().isoformat()
                                conn.executemany(
                                    """
                                    INSERT INTO symbol_no_data
                                        (symbol, date, reason, last_checked)
                                    VALUES (?, ?, 'suspended', ?)
                                    ON CONFLICT(symbol, date) DO UPDATE SET
                                        reason=excluded.reason,
                                        last_checked=excluded.last_checked
                                    """,
                                    ((task.symbol, day, checked) for day in suspended_for_task),
                                )
                                suspended_dates += len(suspended_for_task)
                            conn.commit()

                    if query_failed or unresolved_dates:
                        failed += 1
                        failed_codes.append(task.symbol)
                        if unresolved_dates:
                            logger.error(
                                "❌ [%s] 仍有 %s 个交易日未获得数据或明确停牌状态",
                                task.display_name,
                                len(unresolved_dates),
                            )
                    elif valid_frames:
                        successful += 1
                    else:
                        empty += 1

                    processed += 1
                    since_reconnect += 1
                    emit(task, queue_index, "completed")
        finally:
            logout()

        return make_report(True)

    # ── 股票列表 ──

    def get_market_universe(self) -> list[MarketSymbol]:
        """通过 baostock 获取当前上市 A 股的代码、名称和上市日期。"""
        import baostock as bs

        try:
            with redirect_stdout(StringIO()):
                lg = bs.login()
        except Exception as exc:
            logger.error("❌ 获取股票清单时连接数据服务异常：%s", exc)
            return []
        if lg.error_code != "0":
            logger.error("❌ 获取股票清单时连接数据服务失败：%s", lg.error_msg)
            return []

        try:
            with redirect_stdout(StringIO()):
                rs = bs.query_stock_basic(code_name="", code="")
                if rs.error_code != "0":
                    raise RuntimeError(rs.error_msg)
                stocks: list[MarketSymbol] = []
                while rs.next():
                    row = rs.get_row_data()
                    code = row[0]  # "sh.600000" or "sz.000001"
                    name = row[1]
                    ipo_date = row[2] or None
                    stock_type = row[4]  # "1" = 股票
                    status = row[5]  # "1" = 上市
                    if status == "1" and stock_type == "1":
                        stocks.append(MarketSymbol(code.split(".")[1], name, ipo_date))
            logger.info("✅ 全市场股票清单获取完成：共 %s 只", f"{len(stocks):,}")
            return stocks
        except Exception as e:
            logger.error("❌ 获取全市场股票清单失败：%s", e)
            return []
        finally:
            try:
                with redirect_stdout(StringIO()):
                    bs.logout()
            except Exception as exc:
                logger.warning("⚠️ 获取股票清单后的会话关闭异常：%s", exc)

    def get_all_symbols(self) -> list[str]:
        """兼容旧调用：仅返回当前上市 A 股代码。"""
        return [stock.symbol for stock in self.get_market_universe()]

    def refresh_trade_calendar(self) -> list[str]:
        """从 baostock 刷新配置起始日至今天的交易日历并持久化。"""
        import baostock as bs

        try:
            with redirect_stdout(StringIO()):
                login_result = bs.login()
            if login_result.error_code != "0":
                raise RuntimeError(login_result.error_msg)
            with redirect_stdout(StringIO()):
                result = bs.query_trade_dates(
                    start_date=self.start_date,
                    end_date=date.today().isoformat(),
                )
                if result.error_code != "0":
                    raise RuntimeError(result.error_msg)
                trading_dates: list[str] = []
                while result.next():
                    row = result.get_row_data()
                    if row[1] == "1":
                        trading_dates.append(row[0])
            if not trading_dates:
                raise RuntimeError("数据源未返回有效交易日")
            self.replace_trade_calendar(trading_dates)
            logger.info(
                "✅ 交易日历刷新完成：%s 至 %s，共 %s 个交易日",
                trading_dates[0],
                trading_dates[-1],
                f"{len(trading_dates):,}",
            )
            return trading_dates
        except Exception as exc:
            logger.error("❌ 交易日历刷新失败：%s", exc)
            return []
        finally:
            try:
                with redirect_stdout(StringIO()):
                    bs.logout()
            except Exception as exc:
                logger.warning("⚠️ 刷新交易日历后的会话关闭异常：%s", exc)

    def get_local_symbols(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT symbol FROM stock_daily
                WHERE date = (SELECT MAX(date) FROM stock_daily)
                ORDER BY symbol
                """
            ).fetchall()
        return [row[0] for row in rows]
