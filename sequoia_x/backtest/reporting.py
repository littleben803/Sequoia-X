"""V3 回测 JSON 报告。"""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from statistics import fmean, median

from sequoia_x.backtest.models import BacktestResult


def _git_revision() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _summaries(result: BacktestResult) -> dict[str, dict[str, dict]]:
    grouped: dict[tuple[str, int], list[float]] = defaultdict(list)
    excluded: dict[tuple[str, int], int] = defaultdict(int)
    for outcome in result.outcomes:
        key = (outcome.strategy_key, outcome.horizon)
        if outcome.return_rate is None:
            excluded[key] += 1
        else:
            grouped[key].append(outcome.return_rate)
    output: dict[str, dict[str, dict]] = {}
    for strategy_key in result.config.strategy_keys:
        output[strategy_key] = {}
        for horizon in result.config.horizons:
            values = grouped[(strategy_key, horizon)]
            output[strategy_key][str(horizon)] = {
                "evaluated_signals": len(values),
                "excluded_signals": excluded[(strategy_key, horizon)],
                "average_return": fmean(values) if values else None,
                "median_return": median(values) if values else None,
                "win_rate": (sum(value > 0 for value in values) / len(values) if values else None),
            }
    return output


def _benchmark_comparisons(result: BacktestResult) -> dict[str, dict[str, dict]]:
    output: dict[str, dict[str, dict]] = {}
    for portfolio in result.portfolios:
        output[portfolio.strategy_key] = {}
        portfolio_years = {item.year: item for item in portfolio.annual_returns}
        for benchmark in result.benchmarks:
            benchmark_years = {item.year: item for item in benchmark.annual_returns}
            output[portfolio.strategy_key][benchmark.instrument_id] = {
                "benchmark_name": benchmark.name,
                "portfolio_total_return": portfolio.total_return,
                "benchmark_total_return": benchmark.total_return,
                "excess_total_return": portfolio.total_return - benchmark.total_return,
                "portfolio_annualized_return": portfolio.annualized_return,
                "benchmark_annualized_return": benchmark.annualized_return,
                "annualized_excess_return": (
                    portfolio.annualized_return - benchmark.annualized_return
                    if portfolio.annualized_return is not None
                    and benchmark.annualized_return is not None
                    else None
                ),
                "annual_returns": [
                    {
                        "year": year,
                        "portfolio_return": portfolio_years[year].return_rate,
                        "benchmark_return": benchmark_years[year].return_rate,
                        "excess_return": (
                            portfolio_years[year].return_rate - benchmark_years[year].return_rate
                        ),
                        "is_partial": (
                            portfolio_years[year].is_partial or benchmark_years[year].is_partial
                        ),
                    }
                    for year in sorted(portfolio_years.keys() & benchmark_years.keys())
                ],
            }
    return output


def write_backtest_report(
    *,
    result: BacktestResult,
    database_path: str,
    output_path: str,
) -> Path:
    """原子写入不含密钥的信号回测报告。"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 3,
        "report_type": "V3_PORTFOLIO_BACKTEST",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "code_revision": _git_revision(),
        "database_path": database_path,
        "disclaimer": "仅用于个人研究，不构成投资建议、专业意见或收益保证。",
        "methodology": {
            "signal_timing": "T日收盘后生成信号",
            "return_definition": "信号日QFQ收盘价至第N个完整交易日QFQ收盘价",
            "execution_simulated": True,
            "entry": "T+1开盘，100股整数手",
            "exit": "入场后固定持有N个完整交易日，于开盘尝试卖出",
            "unfilled_policy": "买入当日拒单；卖出受阻则后续交易日继续尝试",
            "stamp_duty": {
                "side": "SELL_ONLY",
                "before_2023_08_28": 0.001,
                "from_2023_08_28": 0.0005,
                "official_source": "财政部 税务总局公告2023年第39号",
            },
            "performance_metrics": {
                "annualization_days": 252,
                "sharpe": "日收益均值/日收益标准差，零无风险利率",
                "sortino": "日收益均值/下行偏差，零目标收益率",
                "trade_win_rate": "费用后净盈利的已平仓交易数/全部已平仓交易数",
                "profit_loss_ratio": "平均净盈利/平均净亏损绝对值",
                "turnover": "全部买卖成交额/平均账户权益，双边口径",
                "benchmarks": (
                    "SSE:000300（沪深300）和SSE:000985（中证全指）canonical收盘价，"
                    "与组合使用相同首尾日期"
                ),
                "excess_return": "组合收益减对应基准收益，算术口径",
            },
            "risk_boundary": "日线成交模拟，不代表实盘可成交结果或未来收益。",
        },
        "config": asdict(result.config),
        "signal_date_count": len(result.signal_dates),
        "signal_count": len(result.signals),
        "snapshot_coverages": dict(result.snapshot_coverages),
        "calculation_errors": list(result.calculation_errors),
        "summary": _summaries(result),
        "benchmark_comparisons": _benchmark_comparisons(result),
        "signals": [asdict(item) for item in result.signals],
        "outcomes": [asdict(item) for item in result.outcomes],
        "portfolios": [asdict(item) for item in result.portfolios],
        "benchmarks": [asdict(item) for item in result.benchmarks],
    }
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)
    return path
