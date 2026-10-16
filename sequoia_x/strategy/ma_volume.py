"""均线+成交量选股策略：5日均线上穿20日均线且成交量放大。"""

from statistics import fmean

from sequoia_x.data.research import ResearchSnapshot
from sequoia_x.strategy.base import BaseStrategy, StrategyResult


class MaVolumeStrategy(BaseStrategy):
    """均线+成交量选股策略。

    选股条件（全部向量化，严禁 iterrows）：
    1. 5日收盘均线上穿20日收盘均线（金叉）
    2. 当日成交量 > 20日均量的 1.5 倍（放量确认）

    Attributes:
        webhook_key: 路由到 'ma_volume' 专属飞书机器人。
    """

    webhook_key: str = "ma_volume"
    strategy_key: str = "ma_volume"
    rule_basis = (
        "5日收盘均线由下向上穿越20日收盘均线",
        "当日成交量大于20日均量的1.5倍",
    )

    def run(self, snapshot: ResearchSnapshot) -> StrategyResult:
        selected: list[str] = []
        for instrument_id, bars in snapshot.bars.items():
            if len(bars) < 21:
                continue
            previous = bars[:-1]
            previous_ma5 = fmean(bar.close for bar in previous[-5:])
            previous_ma20 = fmean(bar.close for bar in previous[-20:])
            current_ma5 = fmean(bar.close for bar in bars[-5:])
            current_ma20 = fmean(bar.close for bar in bars[-20:])
            current_volume_ma20 = fmean(bar.volume for bar in bars[-20:])
            if (
                previous_ma5 < previous_ma20
                and current_ma5 > current_ma20
                and bars[-1].volume > current_volume_ma20 * 1.5
            ):
                selected.append(instrument_id)
        return self._result(snapshot, selected)
