"""A 股日频成交模型与组合账本。"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from statistics import fmean, stdev

from sequoia_x.backtest.models import (
    BacktestConfig,
    EquityPoint,
    OpenPosition,
    OrderRecord,
    PortfolioResult,
    SignalRecord,
)
from sequoia_x.data.research import ResearchBar, ResearchSnapshot


@dataclass
class _Position:
    instrument_id: str
    quantity: int
    entry_date: str
    entry_price: float
    last_price: float
    exit_due_date: str
    signal_date: str


class PortfolioSimulator:
    """按策略隔离资金，以次日开盘成交和固定持有期模拟组合。"""

    def run(
        self,
        *,
        config: BacktestConfig,
        dates: tuple[str, ...],
        signal_dates: tuple[str, ...],
        signals: tuple[SignalRecord, ...],
        snapshot_source: Callable[[str], ResearchSnapshot],
    ) -> tuple[PortfolioResult, ...]:
        instrument_ids = {signal.instrument_id for signal in signals}
        market_bars: dict[str, dict[str, ResearchBar]] = {}
        for day in dates:
            snapshot = snapshot_source(day)
            market_bars[day] = {
                instrument_id: bars[-1]
                for instrument_id in instrument_ids
                if (bars := snapshot.bars.get(instrument_id))
                and bars[-1].trade_date == day
            }
        results = []
        for strategy_key in config.strategy_keys:
            strategy_signals = tuple(
                signal for signal in signals if signal.strategy_key == strategy_key
            )
            results.append(
                self._run_strategy(
                    strategy_key=strategy_key,
                    config=config,
                    dates=dates,
                    signal_dates=signal_dates,
                    signals=strategy_signals,
                    market_bars=market_bars,
                )
            )
        return tuple(results)

    def _run_strategy(
        self,
        *,
        strategy_key: str,
        config: BacktestConfig,
        dates: tuple[str, ...],
        signal_dates: tuple[str, ...],
        signals: tuple[SignalRecord, ...],
        market_bars: dict[str, dict[str, ResearchBar]],
    ) -> PortfolioResult:
        signal_map: dict[str, list[SignalRecord]] = defaultdict(list)
        for signal in signals:
            signal_map[signal.as_of_date].append(signal)
        signal_date_set = set(signal_dates)
        cash = config.initial_cash
        positions: dict[str, _Position] = {}
        orders: list[OrderRecord] = []
        curve: list[EquityPoint] = []

        for index, day in enumerate(dates):
            day_bars = market_bars[day]
            for instrument_id in sorted(tuple(positions)):
                position = positions[instrument_id]
                if day < position.exit_due_date:
                    continue
                bar = day_bars.get(instrument_id)
                reason = self._sell_block_reason(bar)
                if reason:
                    orders.append(
                        self._unfilled_order(
                            position, strategy_key, day, "SELL", "DEFERRED", reason
                        )
                    )
                    continue
                assert bar is not None
                price = bar.open * (1 - config.slippage_bps / 10_000)
                notional = price * position.quantity
                commission = self._commission(notional, config)
                stamp_duty = notional * self.stamp_duty_rate(day)
                cash += notional - commission - stamp_duty
                position.last_price = bar.close
                orders.append(
                    OrderRecord(
                        strategy_key=strategy_key,
                        trade_date=day,
                        signal_date=position.signal_date,
                        instrument_id=instrument_id,
                        side="SELL",
                        status="FILLED",
                        reason=None,
                        quantity=position.quantity,
                        price=price,
                        notional=notional,
                        commission=commission,
                        stamp_duty=stamp_duty,
                    )
                )
                del positions[instrument_id]

            previous_day = dates[index - 1] if index else None
            entry_signals = (
                signal_map.get(previous_day, [])
                if previous_day in signal_date_set
                else []
            )
            for signal in sorted(entry_signals, key=lambda item: item.instrument_id):
                if signal.instrument_id in positions:
                    orders.append(
                        self._rejected_buy(signal, strategy_key, day, "ALREADY_HELD")
                    )
                    continue
                if len(positions) >= config.max_positions:
                    orders.append(
                        self._rejected_buy(signal, strategy_key, day, "MAX_POSITIONS")
                    )
                    continue
                bar = day_bars.get(signal.instrument_id)
                reason = self._buy_block_reason(bar)
                if reason:
                    orders.append(self._rejected_buy(signal, strategy_key, day, reason))
                    continue
                assert bar is not None
                price = bar.open * (1 + config.slippage_bps / 10_000)
                target_notional = self._current_equity(cash, positions) / config.max_positions
                quantity = int(min(cash, target_notional) / price / 100) * 100
                while quantity > 0:
                    notional = price * quantity
                    commission = self._commission(notional, config)
                    if notional + commission <= cash:
                        break
                    quantity -= 100
                if quantity <= 0:
                    orders.append(
                        self._rejected_buy(signal, strategy_key, day, "INSUFFICIENT_CASH")
                    )
                    continue
                notional = price * quantity
                commission = self._commission(notional, config)
                cash -= notional + commission
                exit_index = index + config.holding_period
                positions[signal.instrument_id] = _Position(
                    instrument_id=signal.instrument_id,
                    quantity=quantity,
                    entry_date=day,
                    entry_price=price,
                    last_price=bar.close,
                    exit_due_date=dates[exit_index],
                    signal_date=signal.as_of_date,
                )
                orders.append(
                    OrderRecord(
                        strategy_key=strategy_key,
                        trade_date=day,
                        signal_date=signal.as_of_date,
                        instrument_id=signal.instrument_id,
                        side="BUY",
                        status="FILLED",
                        reason=None,
                        quantity=quantity,
                        price=price,
                        notional=notional,
                        commission=commission,
                        stamp_duty=0.0,
                    )
                )

            market_value = 0.0
            for position in positions.values():
                bar = day_bars.get(position.instrument_id)
                if bar is not None:
                    position.last_price = bar.close
                market_value += position.last_price * position.quantity
            curve.append(
                EquityPoint(
                    trade_date=day,
                    cash=cash,
                    market_value=market_value,
                    total_equity=cash + market_value,
                    open_positions=len(positions),
                )
            )

        return self._result(strategy_key, config, cash, positions, orders, curve)

    @staticmethod
    def stamp_duty_rate(trade_date: str) -> float:
        return 0.0005 if trade_date >= "2023-08-28" else 0.001

    @staticmethod
    def _commission(notional: float, config: BacktestConfig) -> float:
        return max(notional * config.commission_rate, config.minimum_commission)

    @staticmethod
    def _is_one_price(bar: ResearchBar) -> bool:
        return math.isclose(bar.open, bar.high) and math.isclose(bar.high, bar.low)

    @classmethod
    def _buy_block_reason(cls, bar: ResearchBar | None) -> str | None:
        if bar is None:
            return "NO_BAR_ON_ENTRY_DATE"
        if (
            cls._is_one_price(bar)
            and bar.previous_close is not None
            and bar.open > bar.previous_close
        ):
            return "ONE_PRICE_UP_LOCK"
        return None

    @classmethod
    def _sell_block_reason(cls, bar: ResearchBar | None) -> str | None:
        if bar is None:
            return "NO_BAR_ON_EXIT_DATE"
        if (
            cls._is_one_price(bar)
            and bar.previous_close is not None
            and bar.open < bar.previous_close
        ):
            return "ONE_PRICE_DOWN_LOCK"
        return None

    @staticmethod
    def _current_equity(cash: float, positions: dict[str, _Position]) -> float:
        return cash + sum(item.last_price * item.quantity for item in positions.values())

    @staticmethod
    def _rejected_buy(
        signal: SignalRecord, strategy_key: str, day: str, reason: str
    ) -> OrderRecord:
        return OrderRecord(
            strategy_key=strategy_key,
            trade_date=day,
            signal_date=signal.as_of_date,
            instrument_id=signal.instrument_id,
            side="BUY",
            status="REJECTED",
            reason=reason,
            quantity=0,
            price=None,
            notional=0.0,
            commission=0.0,
            stamp_duty=0.0,
        )

    @staticmethod
    def _unfilled_order(
        position: _Position,
        strategy_key: str,
        day: str,
        side: str,
        status: str,
        reason: str,
    ) -> OrderRecord:
        return OrderRecord(
            strategy_key=strategy_key,
            trade_date=day,
            signal_date=position.signal_date,
            instrument_id=position.instrument_id,
            side=side,
            status=status,
            reason=reason,
            quantity=position.quantity,
            price=None,
            notional=0.0,
            commission=0.0,
            stamp_duty=0.0,
        )

    def _result(
        self,
        strategy_key: str,
        config: BacktestConfig,
        cash: float,
        positions: dict[str, _Position],
        orders: list[OrderRecord],
        curve: list[EquityPoint],
    ) -> PortfolioResult:
        equities = [item.total_equity for item in curve]
        daily_returns = [
            equities[index] / equities[index - 1] - 1
            for index in range(1, len(equities))
            if equities[index - 1] > 0
        ]
        total_return = equities[-1] / config.initial_cash - 1 if equities else 0.0
        periods = max(len(equities) - 1, 0)
        annualized_return = (
            (1 + total_return) ** (252 / periods) - 1
            if periods and 1 + total_return > 0
            else None
        )
        volatility = stdev(daily_returns) * math.sqrt(252) if len(daily_returns) > 1 else None
        sharpe = (
            fmean(daily_returns) / stdev(daily_returns) * math.sqrt(252)
            if len(daily_returns) > 1 and stdev(daily_returns) > 0
            else None
        )
        peak = 0.0
        max_drawdown = 0.0
        for equity in equities:
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, 1 - equity / peak)
        return PortfolioResult(
            strategy_key=strategy_key,
            initial_cash=config.initial_cash,
            final_equity=equities[-1] if equities else cash,
            total_return=total_return,
            annualized_return=annualized_return,
            annualized_volatility=volatility,
            sharpe_ratio=sharpe,
            max_drawdown=max_drawdown,
            filled_buys=sum(item.side == "BUY" and item.status == "FILLED" for item in orders),
            filled_sells=sum(item.side == "SELL" and item.status == "FILLED" for item in orders),
            rejected_orders=sum(item.status == "REJECTED" for item in orders),
            deferred_orders=sum(item.status == "DEFERRED" for item in orders),
            total_commission=sum(item.commission for item in orders),
            total_stamp_duty=sum(item.stamp_duty for item in orders),
            orders=tuple(orders),
            equity_curve=tuple(curve),
            open_positions=tuple(
                OpenPosition(
                    instrument_id=item.instrument_id,
                    quantity=item.quantity,
                    entry_date=item.entry_date,
                    entry_price=item.entry_price,
                    last_price=item.last_price,
                    exit_due_date=item.exit_due_date,
                )
                for item in sorted(positions.values(), key=lambda value: value.instrument_id)
            ),
        )
