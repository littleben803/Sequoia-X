"""均线放量策略：在中期上升趋势中捕捉高质量金叉。"""

import math
from statistics import fmean, median, pstdev

from sequoia_x.data.research import ResearchBar, ResearchSnapshot
from sequoia_x.strategy.base import BaseStrategy, StrategyResult


class MaVolumeStrategy(BaseStrategy):
    """趋势确认后的均线放量策略。

    先用 5/20/60 日均线、历史回撤和金叉前结构确认趋势质量，再要求 5/20 日均线
    发生有意义但不过度的金叉。量能基准只使用此前交易日，并用成交额、ATR、波动率、
    跳空、实体和上影线过滤不可交易或单根 K 线驱动的信号。通过准入条件的候选再按
    趋势、金叉、量能、流动性、相对强弱、突破、波动率和均线距离综合排序。

    Attributes:
        webhook_key: 路由到 'ma_volume' 专属飞书机器人。
    """

    webhook_key: str = "ma_volume"
    strategy_key: str = "ma_volume"
    _MIN_BARS = 121
    _MIN_VOLUME_RATIO = 1.5
    _MAX_VOLUME_RATIO = 4.0
    _MIN_AVERAGE_AMOUNT = 50_000_000.0
    _MIN_MEDIAN_AMOUNT = 40_000_000.0
    _MIN_CURRENT_AMOUNT = 50_000_000.0
    _MAX_CLOSE_TO_MA20 = 1.08
    _MAX_DAILY_RETURN = 0.07
    _MAX_ATR_PERCENT = 0.06
    _MAX_DAILY_VOLATILITY = 0.04
    _MAX_DRAWDOWN_40 = 0.18
    _MAX_TRUE_RANGE_ATR = 3.5
    _MAX_BODY_ATR = 3.0
    _MAX_GAP_ATR = 1.5
    _MAX_PREVIOUS_CROSS_GAP = 0.02
    _MIN_CROSS_STRENGTH = 0.001
    _MAX_CROSS_STRENGTH = 0.02
    _MAX_CONSOLIDATION_RANGE = 0.18
    _LOW_LIQUIDITY_AMOUNT = 30_000_000.0
    _MAX_LOW_LIQUIDITY_DAYS = 4
    rule_basis = (
        "5/20日均线由收敛状态形成强度适中的金叉",
        "5/20/60日均线多头排列且三条均线分别向上",
        "近40日非持续下跌、最大回撤受控且金叉前价格保持收敛",
        "当日量比为此前20日均量的1.5至4倍，成交额充足且稳定",
        "当日涨幅、跳空、实体、上影线和真实波幅未出现异常",
        "ATR、20日波动率和距离20日均线均处于风险上限内",
        "候选按趋势、金叉、量能、流动性、相对强弱、突破和风险综合排序",
    )

    def run(self, snapshot: ResearchSnapshot) -> StrategyResult:
        prepared = {
            instrument_id: bars
            for instrument_id, bars in snapshot.bars.items()
            if len(bars) >= self._MIN_BARS
        }
        relative_strength = self._relative_strength(prepared)
        selected: list[str] = []
        scores: dict[str, float] = {}
        for instrument_id, bars in prepared.items():
            current = bars[-1]
            previous = bars[-2]
            previous_ma5 = fmean(bar.close for bar in bars[-6:-1])
            previous_ma20 = fmean(bar.close for bar in bars[-21:-1])
            current_ma5 = fmean(bar.close for bar in bars[-5:])
            current_ma20 = fmean(bar.close for bar in bars[-20:])
            current_ma60 = fmean(bar.close for bar in bars[-60:])
            ma5_three_days_ago = fmean(bar.close for bar in bars[-8:-3])
            ma20_five_days_ago = fmean(bar.close for bar in bars[-25:-5])
            ma60_ten_days_ago = fmean(bar.close for bar in bars[-70:-10])
            previous_volume_ma20 = fmean(bar.volume for bar in bars[-21:-1])
            previous_amounts = [bar.amount for bar in bars[-21:-1]]
            previous_amount_ma20 = fmean(previous_amounts)
            previous_amount_median20 = median(previous_amounts)
            if previous_volume_ma20 <= 0 or previous.close <= 0:
                continue
            volume_ratio = current.volume / previous_volume_ma20
            daily_return = current.close / previous.close - 1
            close_to_ma20 = current.close / current_ma20
            previous_cross_gap = (previous_ma20 - previous_ma5) / previous_ma20
            cross_strength = (current_ma5 - current_ma20) / current_ma20
            true_ranges = self._true_ranges(bars[-22:-1])
            atr20 = fmean(true_ranges[-20:])
            if atr20 <= 0:
                continue
            atr_percent = atr20 / previous.close
            current_true_range = self._true_range(current, previous.close)
            body = abs(current.close - current.open)
            gap = abs(current.open - previous.close)
            upper_shadow = current.high - max(current.open, current.close)
            returns20 = [
                bars[index].close / bars[index - 1].close - 1
                for index in range(len(bars) - 19, len(bars))
            ]
            volatility20 = pstdev(returns20)
            drawdown40 = self._maximum_drawdown([bar.close for bar in bars[-41:-1]])
            pre_cross_bars = bars[-11:-1]
            consolidation_range = (
                max(bar.high for bar in pre_cross_bars) / min(bar.low for bar in pre_cross_bars) - 1
            )
            return40 = previous.close / bars[-41].close - 1
            breakout = current.close / max(bar.high for bar in bars[-21:-1]) - 1
            low_liquidity_days = sum(
                amount < self._LOW_LIQUIDITY_AMOUNT for amount in previous_amounts
            )
            if (
                0 <= previous_cross_gap <= self._MAX_PREVIOUS_CROSS_GAP
                and current_ma5 > current_ma20
                and self._MIN_CROSS_STRENGTH <= cross_strength <= self._MAX_CROSS_STRENGTH
                and current_ma20 > current_ma60
                and current_ma5 > ma5_three_days_ago
                and current_ma20 > ma20_five_days_ago
                and current_ma60 > ma60_ten_days_ago
                and return40 > -0.08
                and drawdown40 <= self._MAX_DRAWDOWN_40
                and consolidation_range <= self._MAX_CONSOLIDATION_RANGE
                and self._MIN_VOLUME_RATIO <= volume_ratio <= self._MAX_VOLUME_RATIO
                and previous_amount_ma20 >= self._MIN_AVERAGE_AMOUNT
                and previous_amount_median20 >= self._MIN_MEDIAN_AMOUNT
                and current.amount >= self._MIN_CURRENT_AMOUNT
                and low_liquidity_days <= self._MAX_LOW_LIQUIDITY_DAYS
                and current.close > current.open
                and 0 < daily_return <= self._MAX_DAILY_RETURN
                and 1 < close_to_ma20 <= self._MAX_CLOSE_TO_MA20
                and atr_percent <= self._MAX_ATR_PERCENT
                and volatility20 <= self._MAX_DAILY_VOLATILITY
                and current_true_range <= atr20 * self._MAX_TRUE_RANGE_ATR
                and body <= atr20 * self._MAX_BODY_ATR
                and gap <= atr20 * self._MAX_GAP_ATR
                and upper_shadow <= max(body, atr20 * 0.25)
            ):
                selected.append(instrument_id)
                scores[instrument_id] = self._score(
                    current_ma5=current_ma5,
                    current_ma20=current_ma20,
                    current_ma60=current_ma60,
                    ma5_three_days_ago=ma5_three_days_ago,
                    ma20_five_days_ago=ma20_five_days_ago,
                    ma60_ten_days_ago=ma60_ten_days_ago,
                    cross_strength=cross_strength,
                    volume_ratio=volume_ratio,
                    average_amount=previous_amount_ma20,
                    relative_strength=relative_strength[instrument_id],
                    breakout=breakout,
                    atr_percent=atr_percent,
                    volatility20=volatility20,
                    close_to_ma20=close_to_ma20,
                )
        return self._result(snapshot, selected, scores=scores)

    @staticmethod
    def _score(
        *,
        current_ma5: float,
        current_ma20: float,
        current_ma60: float,
        ma5_three_days_ago: float,
        ma20_five_days_ago: float,
        ma60_ten_days_ago: float,
        cross_strength: float,
        volume_ratio: float,
        average_amount: float,
        relative_strength: float,
        breakout: float,
        atr_percent: float,
        volatility20: float,
        close_to_ma20: float,
    ) -> float:
        """返回 0 到 100 的可解释相对排序分，不表示收益概率。"""
        ma5_slope = MaVolumeStrategy._bounded((current_ma5 / ma5_three_days_ago - 1) / 0.03)
        ma20_slope = MaVolumeStrategy._bounded((current_ma20 / ma20_five_days_ago - 1) / 0.025)
        ma60_slope = MaVolumeStrategy._bounded((current_ma60 / ma60_ten_days_ago - 1) / 0.03)
        trend = ma5_slope * 0.25 + ma20_slope * 0.45 + ma60_slope * 0.30
        cross = MaVolumeStrategy._peak_score(cross_strength, 0.006, 0.001, 0.02)
        volume = MaVolumeStrategy._peak_score(volume_ratio, 2.2, 1.5, 4.0)
        liquidity = MaVolumeStrategy._bounded(
            (math.log10(average_amount) - math.log10(50_000_000))
            / (math.log10(500_000_000) - math.log10(50_000_000))
        )
        breakout_score = MaVolumeStrategy._peak_score(breakout, 0.02, -0.05, 0.08)
        risk = (
            MaVolumeStrategy._bounded(1 - atr_percent / 0.06) * 0.5
            + MaVolumeStrategy._bounded(1 - volatility20 / 0.04) * 0.5
        )
        distance = MaVolumeStrategy._peak_score(close_to_ma20 - 1, 0.025, 0.0, 0.08)
        return (
            trend * 25
            + cross * 12
            + volume * 12
            + liquidity * 10
            + relative_strength * 18
            + breakout_score * 10
            + risk * 7
            + distance * 6
        )

    @classmethod
    def _relative_strength(cls, prepared: dict[str, tuple[ResearchBar, ...]]) -> dict[str, float]:
        momentum = {
            instrument_id: (
                (bars[-1].close / bars[-61].close - 1) * 0.4
                + (bars[-1].close / bars[-121].close - 1) * 0.6
            )
            for instrument_id, bars in prepared.items()
        }
        ordered = sorted(momentum.items(), key=lambda item: item[1])
        ranks: dict[str, float] = {}
        total = len(ordered)
        index = 0
        while index < total:
            end = index + 1
            while end < total and ordered[end][1] == ordered[index][1]:
                end += 1
            percentile = (((index + 1) + end) / 2) / total
            for ranked_index in range(index, end):
                ranks[ordered[ranked_index][0]] = percentile
            index = end
        return ranks

    @staticmethod
    def _true_range(current: ResearchBar, previous_close: float) -> float:
        return max(
            current.high - current.low,
            abs(current.high - previous_close),
            abs(current.low - previous_close),
        )

    @classmethod
    def _true_ranges(cls, bars: tuple[ResearchBar, ...]) -> list[float]:
        return [
            cls._true_range(bars[index], bars[index - 1].close) for index in range(1, len(bars))
        ]

    @staticmethod
    def _maximum_drawdown(closes: list[float]) -> float:
        peak = closes[0]
        maximum = 0.0
        for close in closes:
            peak = max(peak, close)
            maximum = max(maximum, 1 - close / peak)
        return maximum

    @staticmethod
    def _bounded(value: float) -> float:
        return max(0.0, min(value, 1.0))

    @classmethod
    def _peak_score(cls, value: float, optimum: float, minimum: float, maximum: float) -> float:
        if value <= optimum:
            return cls._bounded((value - minimum) / (optimum - minimum))
        return cls._bounded((maximum - value) / (maximum - optimum))
