"""海龟交易策略：20日新高突破 + 成交额过亿 + 动量阳线过滤。"""

from sequoia_x.data.research import ResearchSnapshot
from sequoia_x.strategy.base import BaseStrategy, StrategyResult


class TurtleTradeStrategy(BaseStrategy):
    """海龟交易策略（A股防诱多改良版）。

    选股条件（向量化，严禁 iterrows）：
    1. 突破新高：今日 close > 前20个交易日 high 的最大值
    2. 流动性：今日 turnover > 100,000,000
    3. 防诱多过滤：今日必须是实体阳线（close > open），
       且必须真涨（今日 close > 昨日 close）

    Attributes:
        webhook_key: 路由到 'turtle' 专属飞书机器人。
    """

    webhook_key: str = "turtle"
    strategy_key: str = "turtle"
    _MIN_BARS: int = 21  # 至少需要 21 根 K 线（20日窗口 + 当日）
    rule_basis = (
        "当日收盘价高于前20个交易日最高价",
        "当日成交额大于1亿元",
        "当日收阳且收盘价高于前一交易日",
    )

    def run(self, snapshot: ResearchSnapshot) -> StrategyResult:
        candidates: list[str] = []
        for instrument_id, bars in snapshot.bars.items():
            if len(bars) < self._MIN_BARS:
                continue
            current = bars[-1]
            previous = bars[-2]
            high_20 = max(bar.high for bar in bars[-21:-1])
            if (
                current.close > high_20
                and current.amount > 100_000_000
                and current.close > current.open
                and current.close > previous.close
            ):
                candidates.append(instrument_id)
        return self._result(snapshot, candidates)
