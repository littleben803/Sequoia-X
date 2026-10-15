"""数据引擎属性测试。"""

import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import (
    BackfillPlan,
    BackfillTask,
    DataEngine,
    DataQualityError,
    MarketSymbol,
)


def make_engine_in(tmp_dir: str) -> tuple[DataEngine, Settings]:
    """创建使用临时数据库的 DataEngine 实例。"""
    settings = Settings(
        db_path=str(Path(tmp_dir) / "test.db"),
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
    )
    engine = DataEngine(settings)
    return engine, settings


# Property 4: (symbol, date) 唯一约束防止重复写入
@given(
    symbol=st.text(min_size=6, max_size=6, alphabet="0123456789"),
    trade_date=st.dates(min_value=date(2024, 1, 1), max_value=date(2025, 12, 31)),
)
@h_settings(max_examples=50, deadline=None)
def test_unique_symbol_date_constraint(symbol: str, trade_date: date) -> None:
    """相同 (symbol, date) 插入两次，数据库中该组合记录数应保持为 1。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        row = {
            "symbol": symbol,
            "date": str(trade_date),
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 1000.0,
            "turnover": 10500.0,
        }
        df = pd.DataFrame([row])
        with sqlite3.connect(engine.db_path) as conn:
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            try:
                df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            except sqlite3.IntegrityError:
                pass
            count = conn.execute(
                "SELECT COUNT(*) FROM stock_daily WHERE symbol=? AND date=?",
                (symbol, str(trade_date)),
            ).fetchone()[0]
        assert count == 1


def test_sync_preserves_other_symbols_on_same_date(tmp_path: Path) -> None:
    """部分股票增量更新时，不得删除其他股票的当日数据。"""
    engine, _ = make_engine_in(str(tmp_path))
    today = date.today()
    yesterday = today - timedelta(days=1)
    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            """
            INSERT INTO stock_daily
                (symbol, date, open, high, low, close, volume, turnover)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("000001", str(today), 10, 11, 9, 10.5, 1000, 10500),
                ("000002", str(yesterday), 20, 21, 19, 20.5, 2000, 41000),
            ],
        )

    fetched_rows = [["000002", str(today), "21", "22", "20", "21.5", "2100", "45150"]]
    worker_result = [[fetched_rows, {"000002"}, []]]
    with patch("multiprocessing.Pool") as pool_class:
        pool_class.return_value.__enter__.return_value.map.return_value = worker_result
        report = engine.sync_today_bulk()

    with sqlite3.connect(engine.db_path) as conn:
        symbols = conn.execute(
            "SELECT symbol FROM stock_daily WHERE date = ? ORDER BY symbol",
            (str(today),),
        ).fetchall()

    assert symbols == [("000001",), ("000002",)]
    assert report.rows_written == 1
    assert report.failed_symbols == 0


def test_audit_rejects_incomplete_latest_date(tmp_path: Path) -> None:
    """最新交易日覆盖率低于闸值时必须拦截策略。"""
    engine, settings = make_engine_in(str(tmp_path))
    object.__setattr__(settings, "minimum_latest_coverage", 0.75)
    today = date.today()
    yesterday = today - timedelta(days=1)
    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            """
            INSERT INTO stock_daily
                (symbol, date, open, high, low, close, volume, turnover)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("000001", str(today), 10, 11, 9, 10.5, 1000, 10500),
                ("000002", str(yesterday), 20, 21, 19, 20.5, 2000, 41000),
            ],
        )

    audit = engine.audit_data()

    assert audit.ready is False
    assert audit.latest_coverage == 0.5
    with pytest.raises(DataQualityError):
        audit.require_ready()


def test_audit_accepts_valid_common_latest_date(tmp_path: Path) -> None:
    engine, _ = make_engine_in(str(tmp_path))
    today = date.today()
    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            """
            INSERT INTO stock_daily
                (symbol, date, open, high, low, close, volume, turnover)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("000001", str(today), 10, 11, 9, 10.5, 1000, 10500),
                ("000002", str(today), 20, 21, 19, 20.5, 2000, 41000),
            ],
        )

    audit = engine.audit_data()

    assert audit.ready is True
    assert audit.latest_coverage == 1.0
    assert engine.get_local_symbols() == ["000001", "000002"]


def test_audit_uses_persisted_market_universe_as_coverage_denominator(tmp_path: Path) -> None:
    """部分回填不能只按已入库股票计算覆盖率。"""
    engine, settings = make_engine_in(str(tmp_path))
    object.__setattr__(settings, "minimum_latest_coverage", 0.75)
    engine.replace_market_universe(["000001", "000002", "000003", "000004"])

    today = date.today()
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute(
            """
            INSERT INTO stock_daily
                (symbol, date, open, high, low, close, volume, turnover)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("000001", str(today), 10, 11, 9, 10.5, 1000, 10500),
        )

    audit = engine.audit_data()

    assert audit.ready is False
    assert audit.total_symbols == 4
    assert audit.stored_symbols == 1
    assert audit.latest_symbols == 1
    assert audit.latest_coverage == 0.25
    assert any("全市场" in problem for problem in audit.problems)


def test_replace_market_universe_is_atomic_snapshot(tmp_path: Path) -> None:
    engine, _ = make_engine_in(str(tmp_path))
    engine.replace_market_universe(["000001", "000002"])
    engine.replace_market_universe(["600000"])

    with sqlite3.connect(engine.db_path) as conn:
        symbols = conn.execute("SELECT symbol FROM market_universe ORDER BY symbol").fetchall()

    assert symbols == [("600000",)]


def test_backfill_reports_initial_connection_failure(tmp_path: Path) -> None:
    engine, _ = make_engine_in(str(tmp_path))
    login_result = SimpleNamespace(error_code="1", error_msg="网络接收错误")

    with patch("baostock.login", return_value=login_result):
        report = engine.backfill(["000001"])

    assert report.completed is False
    assert report.succeeded is False
    assert report.processed_symbols == 0
    assert "首次连接" in (report.aborted_reason or "")


def test_backfill_emits_progress_for_every_processed_symbol(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine, _ = make_engine_in(str(tmp_path))
    today = date.today().isoformat()

    class FakeResult:
        error_code = "0"
        error_msg = ""
        fields = ["date", "open", "high", "low", "close", "volume", "amount"]

        def __init__(self) -> None:
            self._read = False

        def next(self) -> bool:
            if self._read:
                return False
            self._read = True
            return True

        def get_row_data(self) -> list[str]:
            return [today, "10", "11", "9", "10.5", "1000", "10500"]

    login_result = SimpleNamespace(error_code="0", error_msg="")
    events = []

    def noisy_login():
        print("login success!")
        return login_result

    def noisy_logout():
        print("logout success!")

    with (
        patch("baostock.login", side_effect=noisy_login),
        patch("baostock.logout", side_effect=noisy_logout),
        patch(
            "baostock.query_history_k_data_plus",
            side_effect=lambda *args, **kwargs: FakeResult(),
        ),
    ):
        report = engine.backfill(["000001", "000002"], progress_callback=events.append)

    assert report.succeeded is True
    assert report.processed_symbols == 2
    assert report.successful_symbols == 2
    assert report.rows_written == 2
    completed_events = [event for event in events if event.activity == "completed"]
    assert [event.processed_symbols for event in completed_events] == [1, 2]
    captured = capsys.readouterr()
    assert "login success!" not in captured.out
    assert "logout success!" not in captured.out


def test_get_all_symbols_returns_list_after_formatted_success_log(tmp_path: Path) -> None:
    """成功日志不能因为千分位格式错误而把有效清单误判为空。"""
    engine, _ = make_engine_in(str(tmp_path))

    class FakeStockList:
        error_code = "0"
        error_msg = ""

        def __init__(self) -> None:
            self._rows = iter(
                [
                    ["sh.600000", "", "", "", "1", "1"],
                    ["sz.000001", "", "", "", "1", "1"],
                ]
            )
            self._current = None

        def next(self) -> bool:
            self._current = next(self._rows, None)
            return self._current is not None

        def get_row_data(self) -> list[str]:
            return self._current

    login_result = SimpleNamespace(error_code="0", error_msg="")
    with (
        patch("baostock.login", return_value=login_result),
        patch("baostock.query_stock_basic", return_value=FakeStockList()),
        patch("baostock.logout"),
    ):
        symbols = engine.get_all_symbols()

    assert symbols == ["600000", "000001"]


def test_backfill_plan_prioritizes_missing_stale_gap_then_incremental(tmp_path: Path) -> None:
    engine, _ = make_engine_in(str(tmp_path))
    universe = [
        MarketSymbol("000001", "缺失股票", "2020-01-01"),
        MarketSymbol("000002", "陈旧股票", "2020-01-01"),
        MarketSymbol("000003", "普通增量", "2020-01-01"),
        MarketSymbol("000004", "已经最新", "2020-01-01"),
        MarketSymbol("000005", "中间缺口", "2020-01-01"),
    ]
    engine.replace_market_universe(universe)
    engine.replace_trade_calendar(["2026-10-09", "2026-10-12", "2026-10-13"])

    with sqlite3.connect(engine.db_path) as conn:
        rows = []
        for symbol, dates in {
            "000002": ["2026-10-09"],
            "000003": ["2026-10-09", "2026-10-12"],
            "000004": ["2026-10-09", "2026-10-12", "2026-10-13"],
            "000005": ["2026-10-09", "2026-10-13"],
        }.items():
            rows.extend((symbol, day, 10, 11, 9, 10.5, 1000, 10500) for day in dates)
        conn.executemany(
            """
            INSERT INTO stock_daily
                (symbol, date, open, high, low, close, volume, turnover)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    plan = engine.build_backfill_plan()

    assert [task.symbol for task in plan.missing_tasks] == ["000001"]
    assert [task.symbol for task in plan.stale_tasks] == ["000002", "000005"]
    assert [task.symbol for task in plan.incremental_tasks] == ["000003"]
    assert [stock.symbol for stock in plan.current_symbols] == ["000004"]
    gap_task = next(task for task in plan.stale_tasks if task.symbol == "000005")
    assert gap_task.missing_ranges == (("2026-10-12", "2026-10-12"),)


def test_confirmed_suspension_is_not_reported_as_middle_gap(tmp_path: Path) -> None:
    engine, _ = make_engine_in(str(tmp_path))
    engine.replace_market_universe([MarketSymbol("000001", "停牌股票", "2020-01-01")])
    engine.replace_trade_calendar(["2026-10-09", "2026-10-12", "2026-10-13"])
    with sqlite3.connect(engine.db_path) as conn:
        conn.executemany(
            """
            INSERT INTO stock_daily
                (symbol, date, open, high, low, close, volume, turnover)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("000001", "2026-10-09", 10, 11, 9, 10.5, 1000, 10500),
                ("000001", "2026-10-13", 10, 11, 9, 10.5, 1000, 10500),
            ],
        )
        conn.execute(
            """
            INSERT INTO symbol_no_data (symbol, date, reason, last_checked)
            VALUES ('000001', '2026-10-12', 'suspended', '2026-10-13')
            """
        )

    plan = engine.build_backfill_plan()

    assert plan.stale_tasks == ()
    assert [stock.symbol for stock in plan.current_symbols] == ["000001"]


def test_execute_plan_records_explicit_suspension_without_fake_ohlcv(tmp_path: Path) -> None:
    engine, _ = make_engine_in(str(tmp_path))
    task = BackfillTask(
        symbol="000001",
        name="停牌股票",
        queue="stale",
        missing_ranges=(("2026-10-12", "2026-10-12"),),
        last_local_date="2026-10-13",
        gap_count=1,
    )
    plan = BackfillPlan(
        target_trade_date="2026-10-13",
        trading_dates=("2026-10-12", "2026-10-13"),
        missing_tasks=(),
        stale_tasks=(task,),
        incremental_tasks=(),
        current_symbols=(),
    )

    class SuspendedResult:
        error_code = "0"
        error_msg = ""
        fields = ["date", "open", "high", "low", "close", "volume", "amount", "tradestatus"]

        def __init__(self) -> None:
            self._read = False

        def next(self) -> bool:
            if self._read:
                return False
            self._read = True
            return True

        def get_row_data(self) -> list[str]:
            return ["2026-10-12", "", "", "", "", "", "", "0"]

    login_result = SimpleNamespace(error_code="0", error_msg="")
    with (
        patch("baostock.login", return_value=login_result),
        patch("baostock.logout"),
        patch("baostock.query_history_k_data_plus", return_value=SuspendedResult()),
    ):
        report = engine.execute_backfill_plan(plan)

    assert report.succeeded is True
    assert report.suspended_dates == 1
    assert report.rows_written == 0
    with sqlite3.connect(engine.db_path) as conn:
        status = conn.execute(
            "SELECT reason FROM symbol_no_data WHERE symbol='000001' AND date='2026-10-12'"
        ).fetchone()
    assert status == ("suspended",)
