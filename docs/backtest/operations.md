# V3 回测运行手册

回测命令只读本地 `data/sequoia_v3.db`，不会访问网络。运行前应完成对应历史区间的
行情回填、质量处理和 QFQ 因子刷新。

## 数据审计

```bash
uv run python main.py backtest audit --start 2025-01-01 --end 2025-12-31
```

审计检查区间是否存在完整交易日，并对首尾边界构建 121 日研究窗口。退出码 `0` 表示
边界质量闸门通过，退出码 `1` 表示数据不具备回测条件。

## 信号回测

```bash
# 默认策略，评价信号后 1/5/10/20 个完整交易日
uv run python main.py backtest run --start 2025-01-01 --end 2025-12-31

# 单策略和自定义前瞻周期
uv run python main.py backtest run \
  --start 2025-01-01 \
  --end 2025-12-31 \
  --strategy rps \
  --horizon 5 \
  --horizon 20 \
  --output reports/backtest-rps-2025.json

# 自定义组合执行参数
uv run python main.py backtest run \
  --start 2025-01-01 \
  --end 2025-12-31 \
  --holding-period 10 \
  --exit-retry-days 5 \
  --initial-cash 1000000 \
  --max-positions 10 \
  --commission-rate 0.0003 \
  --minimum-commission 5 \
  --slippage-bps 2
```

结束日之后必须还有足够的完整交易日覆盖最大 `--horizon`。例如包含 `--horizon 20`
时，本地数据库至少需要结束日之后 20 个完整交易日。

运行时会先执行一次 V3 全库质量审计，再批量载入回测区间所需行情和因子。大型本地库的
首次审计可能需要数分钟；历史快照不会再按交易日重复扫描 SQLite。

`shakeout` 和 `limit_down` 仍然默认禁用，只能通过 `--strategy` 显式运行；报告会保留其
未完成独立验证的风险备注。

## 结果解释

报告同时保留两种口径：

- 信号评价：信号日收盘价到第 N 个完整交易日收盘价，用于比较信号本身；
- 组合模拟：T+1 开盘入场、固定持有期、100 股整数手、仓位、费用和受阻订单。

组合模拟仍基于日线，无法证明盘中真实排队成交；没有完整历史 ST 和板块涨跌停证据时，
一字板采用保守方向判断。结果不能解释为实盘预期或未来收益。
