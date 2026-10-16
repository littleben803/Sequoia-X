"""V3 回测数据边界：只读完整交易日并复用研究快照质量闸门。"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date
from types import MappingProxyType

from sequoia_x.data.database import MarketDataRepository
from sequoia_x.data.models import V3AuditReport
from sequoia_x.data.research import (
    ResearchDataError,
    ResearchDataService,
    ResearchExclusion,
    ResearchSnapshot,
)


class BacktestDataService:
    """为历史回测提供 point-in-time 研究快照。"""

    def __init__(
        self,
        repository: MarketDataRepository,
        *,
        minimum_coverage: float = 0.95,
    ) -> None:
        self.repository = repository
        self.minimum_coverage = minimum_coverage
        self.research = ResearchDataService(repository)
        self._snapshot_cache: dict[tuple[str, int], ResearchSnapshot] = {}
        self._base_audit: V3AuditReport | None = None
        self._prepared_dates: frozenset[str] = frozenset()
        self._prepared_history_bars: int | None = None
        self._lifecycles: tuple[tuple[str, str, str | None], ...] = ()
        self._raw_bars: dict[str, tuple[tuple, ...]] = {}
        self._raw_bar_dates: dict[str, tuple[str, ...]] = {}
        self._factor_evidence: dict[
            str, tuple[tuple[str, float, str, str, str], ...]
        ] = {}

    def _audit(self) -> V3AuditReport:
        if self._base_audit is None:
            self._base_audit = self.repository.audit()
        return self._base_audit

    def complete_trading_dates(self, start_date: str) -> tuple[str, ...]:
        """返回起始日及之后具备完整全市场批次的日期。"""
        with self.repository.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT trade_date
                FROM ingestion_batches
                WHERE capability='DAILY_SNAPSHOT'
                  AND status='COMPLETE'
                  AND trade_date>=?
                ORDER BY trade_date
                """,
                (start_date,),
            ).fetchall()
        return tuple(row[0] for row in rows if row[0] is not None)

    def prepare_snapshots(self, dates: tuple[str, ...], history_bars: int) -> None:
        """一次读取回测区间行情、证券生命周期和因子证据。"""
        if not dates:
            raise ValueError("批量快照日期不能为空")
        audit = self._audit()
        if not audit.ready_for_a_share_research:
            raise ResearchDataError("数据质量闸门未通过：" + "；".join(audit.problems))
        if audit.latest_complete_date is None or dates[-1] > audit.latest_complete_date:
            raise ResearchDataError("批量快照结束日晚于最新完整交易日")
        missing = [day for day in dates if not self.repository.has_complete_snapshot(day)]
        if missing:
            raise ResearchDataError(f"研究日 {missing[0]} 没有完整全市场批次")

        calendar_span = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days
        bars_per_instrument = history_bars + calendar_span + 1
        self._lifecycles = self.research.research.load_a_share_lifecycles()
        instrument_ids = tuple(item[0] for item in self._lifecycles)
        self._raw_bars = self.research.research.load_backtest_raw_bars(
            dates[-1], bars_per_instrument, instrument_ids
        )
        self._raw_bar_dates = {
            instrument_id: tuple(row[0] for row in rows)
            for instrument_id, rows in self._raw_bars.items()
        }
        self._factor_evidence = self.research.research.load_qfq_factor_evidence(dates[-1])
        self._prepared_dates = frozenset(dates)
        self._prepared_history_bars = history_bars

    def build_snapshot(self, as_of_date: str, history_bars: int) -> ResearchSnapshot:
        """构造指定历史日快照；缓存不改变数据或质量语义。"""
        key = (as_of_date, history_bars)
        if (
            as_of_date in self._prepared_dates
            and history_bars == self._prepared_history_bars
        ):
            return self._build_prepared_snapshot(as_of_date, history_bars)
        if key not in self._snapshot_cache:
            self._snapshot_cache[key] = self.research.build_snapshot(
                history_bars=history_bars,
                as_of_date=as_of_date,
                minimum_coverage=self.minimum_coverage,
                base_audit=self._audit(),
            )
        return self._snapshot_cache[key]

    def _build_prepared_snapshot(
        self, as_of_date: str, history_bars: int
    ) -> ResearchSnapshot:
        universe = tuple(
            instrument_id
            for instrument_id, valid_from, valid_to in self._lifecycles
            if valid_from <= as_of_date and (valid_to is None or valid_to >= as_of_date)
        )
        adjusted = {}
        exclusions: list[ResearchExclusion] = []
        for instrument_id in universe:
            factors = self._factors_for_date(instrument_id, as_of_date)
            if not factors:
                exclusions.append(ResearchExclusion(instrument_id, "MISSING_QFQ_FACTORS"))
                continue
            all_bars = self._raw_bars.get(instrument_id, ())
            bar_dates = self._raw_bar_dates.get(instrument_id, ())
            end_index = bisect_right(bar_dates, as_of_date)
            bars = all_bars[max(0, end_index - history_bars) : end_index]
            if not bars or bars[-1][0] != as_of_date:
                exclusions.append(ResearchExclusion(instrument_id, "NO_BAR_ON_RESEARCH_DATE"))
                continue
            try:
                adjusted[instrument_id] = self.research.apply_qfq(
                    instrument_id, bars, factors
                )
            except ResearchDataError as error:
                exclusions.append(ResearchExclusion(instrument_id, str(error)))

        requested = len(universe)
        eligible = len(adjusted)
        coverage = eligible / requested if requested else 0.0
        if coverage < self.minimum_coverage:
            raise ResearchDataError(
                f"研究快照覆盖率 {coverage:.2%} 低于闸值 {self.minimum_coverage:.2%}"
            )
        return ResearchSnapshot(
            as_of_date=as_of_date,
            bars=MappingProxyType(adjusted),
            exclusions=tuple(exclusions),
            requested_instruments=requested,
            eligible_instruments=eligible,
            coverage=coverage,
            audit=self._audit(),
        )

    def _factors_for_date(
        self, instrument_id: str, as_of_date: str
    ) -> tuple[tuple[str, float], ...]:
        selected: list[tuple[str, float]] = []
        selected_dates: set[str] = set()
        evidence_rows = self._factor_evidence.get(instrument_id, ())
        for effective_date, factor, evidence_date, _fetched_at, _provider in evidence_rows:
            if effective_date > as_of_date or evidence_date < as_of_date:
                continue
            if effective_date in selected_dates:
                continue
            selected_dates.add(effective_date)
            selected.append((effective_date, factor))
        return tuple(selected)

    def audit_range(
        self,
        *,
        start_date: str,
        end_date: str,
        history_bars: int,
    ) -> dict:
        """只读检查区间边界及完整交易日数量。"""
        dates = tuple(
            day for day in self.complete_trading_dates(start_date) if day <= end_date
        )
        boundary_dates = tuple(dict.fromkeys((dates[0], dates[-1]))) if dates else ()
        audits = tuple(
            self.research.audit_snapshot(
                history_bars=history_bars,
                as_of_date=day,
                minimum_coverage=self.minimum_coverage,
                base_audit=self._audit(),
            )
            for day in boundary_dates
        )
        problems = [problem for audit in audits for problem in audit.problems]
        if not dates:
            problems.append("请求区间内没有完整全市场交易日")
        return {
            "ready": bool(dates) and all(audit.ready for audit in audits),
            "start_date": start_date,
            "end_date": end_date,
            "complete_trading_days": len(dates),
            "first_complete_date": dates[0] if dates else None,
            "last_complete_date": dates[-1] if dates else None,
            "boundary_audits": [audit.to_dict() for audit in audits],
            "problems": problems,
        }

