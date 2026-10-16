"""V3 信号回测领域模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class BacktestConfig:
    """一次信号回测的完整、可复现配置。"""

    start_date: str
    end_date: str
    strategy_keys: tuple[str, ...]
    horizons: tuple[int, ...] = (1, 5, 10, 20)
    history_bars: int = 121
    minimum_coverage: float = 0.95
    initial_cash: float = 1_000_000.0
    holding_period: int = 5
    exit_retry_days: int = 5
    max_positions: int = 10
    commission_rate: float = 0.0003
    minimum_commission: float = 5.0
    slippage_bps: float = 0.0

    def __post_init__(self) -> None:
        start = date.fromisoformat(self.start_date)
        end = date.fromisoformat(self.end_date)
        if end < start:
            raise ValueError("结束日期不能早于开始日期")
        if not self.strategy_keys:
            raise ValueError("至少需要一个回测策略")
        if not self.horizons or any(value < 1 for value in self.horizons):
            raise ValueError("前瞻持有期必须全部大于 0")
        if len(set(self.horizons)) != len(self.horizons):
            raise ValueError("前瞻持有期不能重复")
        if self.history_bars < 121:
            raise ValueError("历史窗口不能少于 121 个交易日")
        if not 0 < self.minimum_coverage <= 1:
            raise ValueError("最低覆盖率必须在 (0, 1] 范围内")
        if self.initial_cash <= 0:
            raise ValueError("初始资金必须大于 0")
        if self.holding_period < 1:
            raise ValueError("固定持有期必须大于 0")
        if self.exit_retry_days < 0:
            raise ValueError("卖出重试天数不能为负数")
        if self.max_positions < 1:
            raise ValueError("最大持仓数必须大于 0")
        if self.commission_rate < 0 or self.minimum_commission < 0:
            raise ValueError("佣金参数不能为负数")
        if self.slippage_bps < 0:
            raise ValueError("滑点不能为负数")


@dataclass(frozen=True)
class SignalRecord:
    strategy_key: str
    as_of_date: str
    instrument_id: str
    signal_close: float
    rule_basis: tuple[str, ...]
    risk_note: str | None
    snapshot_coverage: float


@dataclass(frozen=True)
class ForwardOutcome:
    strategy_key: str
    signal_date: str
    instrument_id: str
    horizon: int
    evaluation_date: str | None
    signal_close: float
    evaluation_close: float | None
    return_rate: float | None
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class BacktestResult:
    config: BacktestConfig
    signal_dates: tuple[str, ...]
    signals: tuple[SignalRecord, ...]
    outcomes: tuple[ForwardOutcome, ...]
    snapshot_coverages: tuple[tuple[str, float], ...]
    calculation_errors: tuple[str, ...]
    portfolios: tuple[PortfolioResult, ...] = ()


@dataclass(frozen=True)
class OrderRecord:
    strategy_key: str
    trade_date: str
    signal_date: str
    instrument_id: str
    side: str
    status: str
    reason: str | None
    quantity: int
    price: float | None
    notional: float
    commission: float
    stamp_duty: float


@dataclass(frozen=True)
class EquityPoint:
    trade_date: str
    cash: float
    market_value: float
    total_equity: float
    open_positions: int


@dataclass(frozen=True)
class OpenPosition:
    instrument_id: str
    quantity: int
    entry_date: str
    entry_price: float
    last_price: float
    exit_due_date: str


@dataclass(frozen=True)
class PortfolioResult:
    strategy_key: str
    initial_cash: float
    final_equity: float
    total_return: float
    annualized_return: float | None
    annualized_volatility: float | None
    sharpe_ratio: float | None
    max_drawdown: float
    filled_buys: int
    filled_sells: int
    rejected_orders: int
    deferred_orders: int
    total_commission: float
    total_stamp_duty: float
    orders: tuple[OrderRecord, ...]
    equity_curve: tuple[EquityPoint, ...]
    open_positions: tuple[OpenPosition, ...]

