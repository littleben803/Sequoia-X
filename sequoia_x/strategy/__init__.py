"""V3 行情策略注册表；高风险策略不在默认集合中。"""

from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy

DEFAULT_STRATEGY_KEYS = frozenset({"ma_volume", "turtle", "flag", "rps"})
EXPERIMENTAL_STRATEGY_KEYS = frozenset({"shakeout", "limit_down"})

STRATEGY_TYPES: dict[str, type[BaseStrategy]] = {
    "ma_volume": MaVolumeStrategy,
    "turtle": TurtleTradeStrategy,
    "flag": HighTightFlagStrategy,
    "rps": RpsBreakoutStrategy,
    "shakeout": LimitUpShakeoutStrategy,
    "limit_down": UptrendLimitDownStrategy,
}


def create_strategy(key: str) -> BaseStrategy:
    """创建已迁移的行情策略；事件策略不在此注册表。"""
    try:
        return STRATEGY_TYPES[key]()
    except KeyError as error:
        raise ValueError(f"未知或尚未迁移的策略：{key}") from error
