"""V3 策略契约：只消费不可变研究快照，不访问数据库或网络。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sequoia_x.data.research import ResearchSnapshot


@dataclass(frozen=True)
class StrategyResult:
    strategy_key: str
    as_of_date: str
    selected_instruments: tuple[str, ...]
    rule_basis: tuple[str, ...]
    input_instruments: int
    eligible_instruments: int
    data_exclusions: int
    calculation_errors: tuple[str, ...] = ()
    risk_note: str | None = None


class BaseStrategy(ABC):
    """选股策略抽象基类。"""

    webhook_key: str = "default"
    strategy_key: str = "default"
    rule_basis: tuple[str, ...] = ()
    risk_note: str | None = None

    @abstractmethod
    def run(self, snapshot: ResearchSnapshot) -> StrategyResult:
        ...

    def _result(
        self,
        snapshot: ResearchSnapshot,
        selected: list[str],
        errors: list[str] | None = None,
    ) -> StrategyResult:
        return StrategyResult(
            strategy_key=self.strategy_key,
            as_of_date=snapshot.as_of_date,
            selected_instruments=tuple(sorted(selected)),
            rule_basis=self.rule_basis,
            input_instruments=snapshot.requested_instruments,
            eligible_instruments=snapshot.eligible_instruments,
            data_exclusions=len(snapshot.exclusions),
            calculation_errors=tuple(errors or ()),
            risk_note=self.risk_note,
        )
