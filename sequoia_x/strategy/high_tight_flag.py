"""高旗形整理策略：强动量后极度收敛缩量。"""

from statistics import fmean

from sequoia_x.data.research import ResearchSnapshot
from sequoia_x.strategy.base import BaseStrategy, StrategyResult


class HighTightFlagStrategy(BaseStrategy):
    """高旗形整理策略。

    选股条件（向量化，严禁 iterrows）：
    1. 强动量：过去40天区间最高价 / 区间最低价 > 1.6（涨幅超60%）
    2. 极度收敛：最近10天区间最高价 / 区间最低价 < 1.15（振幅低于15%）
    3. 缩量：今日 volume < 过去20日 volume 均值的 0.6 倍

    Attributes:
        webhook_key: 路由到 'flag' 专属飞书机器人。
    """

    webhook_key: str = "flag"
    strategy_key: str = "flag"
    _MIN_BARS: int = 40  # 至少需要 40 根 K 线
    rule_basis = (
        "过40日最高价/最低价大于1.6",
        "近10日最高价/最低价小于1.15且位于40日高位",
        "当日成交量小于前20日均量的0.6倍",
    )

    def run(self, snapshot: ResearchSnapshot) -> StrategyResult:
        selected: list[str] = []
        for instrument_id, bars in snapshot.bars.items():
            if len(bars) < self._MIN_BARS:
                continue
            tail40 = bars[-40:]
            tail10 = bars[-10:]
            high40 = max(bar.high for bar in tail40)
            low40 = min(bar.low for bar in tail40)
            high10 = max(bar.high for bar in tail10)
            low10 = min(bar.low for bar in tail10)
            previous_volume_ma20 = fmean(bar.volume for bar in bars[-21:-1])
            if (
                high40 / low40 > 1.6
                and high10 / low10 < 1.15
                and low10 >= high40 * 0.8
                and bars[-1].volume < previous_volume_ma20 * 0.6
            ):
                selected.append(instrument_id)
        return self._result(snapshot, selected)
