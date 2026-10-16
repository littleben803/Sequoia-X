"""上升趋势跌停策略：趋势中放量跌停，捕捉错杀机会。"""

from statistics import fmean

from sequoia_x.data.research import ResearchSnapshot
from sequoia_x.strategy.base import BaseStrategy, StrategyResult


class UptrendLimitDownStrategy(BaseStrategy):
    """上升趋势跌停策略。

    选股条件（向量化，严禁 iterrows）：
    1. 处于上升趋势：昨日20日均线 > 昨日60日均线
    2. 放量跌停：今日 close <= 昨日 close * 0.905
                且今日 volume > 20日均量的 2.0 倍

    Attributes:
        webhook_key: 路由到 'limit_down' 专属飞书机器人。
    """

    webhook_key: str = "limit_down"
    strategy_key: str = "limit_down"
    _MIN_BARS: int = 60  # 至少需要 60 根 K 线（60日均线）
    rule_basis = (
        "前一交易日20日均线高于60日均线",
        "当日收盘价不高于前一日收盘价的0.905倍",
        "当日成交量大于当日20日均量的2倍",
    )
    risk_note = "未完成独立回测；固定9.5%阈值未区分板块、ST和特殊交易规则，默认禁用。"

    def run(self, snapshot: ResearchSnapshot) -> StrategyResult:
        selected: list[str] = []
        for instrument_id, bars in snapshot.bars.items():
            if len(bars) < self._MIN_BARS + 1:
                continue
            previous = bars[:-1]
            current = bars[-1]
            previous_ma20 = fmean(bar.close for bar in previous[-20:])
            previous_ma60 = fmean(bar.close for bar in previous[-60:])
            current_volume_ma20 = fmean(bar.volume for bar in bars[-20:])
            if (
                previous_ma20 > previous_ma60
                and current.close <= previous[-1].close * 0.905
                and current.volume > current_volume_ma20 * 2.0
            ):
                selected.append(instrument_id)
        return self._result(snapshot, selected)
