"""RPS 极强动量突破策略。"""

from sequoia_x.data.research import ResearchSnapshot
from sequoia_x.strategy.base import BaseStrategy, StrategyResult


class RpsBreakoutStrategy(BaseStrategy):
    """RPS 极强动量突破策略"""

    webhook_key: str = "rps"
    strategy_key: str = "rps"
    rps_period: int = 120
    rps_threshold: int = 90
    rule_basis = (
        "120个交易日涨幅的横截面百分位不低于90",
        "当日收盘价不低于近120日最高价的90%",
    )

    def run(self, snapshot: ResearchSnapshot) -> StrategyResult:
        returns: dict[str, float] = {}
        for instrument_id, bars in snapshot.bars.items():
            if len(bars) < self.rps_period + 1:
                continue
            base_close = bars[-(self.rps_period + 1)].close
            returns[instrument_id] = (bars[-1].close - base_close) / base_close
        if not returns:
            return self._result(snapshot, [])

        rps = self._percentile_ranks(returns)
        selected = [
            instrument_id
            for instrument_id, percentile in rps.items()
            if percentile >= self.rps_threshold
            and snapshot.bars[instrument_id][-1].close
            >= max(bar.high for bar in snapshot.bars[instrument_id][-self.rps_period :]) * 0.9
        ]
        return self._result(snapshot, selected)

    @staticmethod
    def _percentile_ranks(values: dict[str, float]) -> dict[str, float]:
        """与 pandas rank(pct=True) 的平均名次规则一致。"""
        ordered = sorted(values.items(), key=lambda item: item[1])
        ranks: dict[str, float] = {}
        index = 0
        total = len(ordered)
        while index < total:
            end = index + 1
            while end < total and ordered[end][1] == ordered[index][1]:
                end += 1
            average_rank = ((index + 1) + end) / 2
            percentile = average_rank / total * 100
            for item_index in range(index, end):
                ranks[ordered[item_index][0]] = percentile
            index = end
        return ranks
