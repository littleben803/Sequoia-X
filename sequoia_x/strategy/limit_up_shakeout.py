"""涨停洗盘策略：昨日涨停后今日放量收阴但不破昨收。"""

from sequoia_x.data.research import ResearchSnapshot
from sequoia_x.strategy.base import BaseStrategy, StrategyResult


class LimitUpShakeoutStrategy(BaseStrategy):
    """涨停洗盘策略。

    选股条件（向量化，严禁 iterrows）：
    1. 昨日涨停：昨日 close >= 前日 close * 1.095
    2. 今日收阴：今日 close < 今日 open
    3. 今日放量：今日 volume > 昨日 volume * 2.0
    4. 支撑不破：今日 low >= 昨日 close

    Attributes:
        webhook_key: 路由到 'shakeout' 专属飞书机器人。
    """

    webhook_key: str = "shakeout"
    strategy_key: str = "shakeout"
    _MIN_BARS: int = 3  # 至少需要 3 根 K 线（前日、昨日、今日）
    rule_basis = (
        "前一交易日收盘价不低于前二交易日收盘价的1.095倍",
        "当日收阴、成交量大于前一日2倍且最低价不低于前一日收盘价",
    )
    risk_note = "未完成独立回测；固定9.5%阈值未区分板块、ST和特殊交易规则，默认禁用。"

    def run(self, snapshot: ResearchSnapshot) -> StrategyResult:
        selected: list[str] = []
        for instrument_id, bars in snapshot.bars.items():
            if len(bars) < self._MIN_BARS:
                continue
            previous_two, previous, current = bars[-3:]
            if (
                previous.close >= previous_two.close * 1.095
                and current.close < current.open
                and current.volume > previous.volume * 2.0
                and current.low >= previous.close
            ):
                selected.append(instrument_id)
        return self._result(snapshot, selected)
