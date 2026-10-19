# V3 数据运行手册

```bash
# 首次回填
uv run python main.py data backfill

# 指定小范围验证
uv run python main.py data backfill --start 2026-10-13 --end 2026-10-13

# 日常增量：检查最近 14 个自然日
uv run python main.py data sync

# 只读审计、证券统计、最近批次
uv run python main.py data audit
uv run python main.py data repair-quality
uv run python main.py data instruments
uv run python main.py data batches
```

`COMPLETE` 表示全市场盘后包完整写入。重复执行会跳过完整日期，只重试失败、
中断和未处理日期，因此不需要删除数据库重新开始。

## 中断与恢复

- 按 `Ctrl+C` 会安全结束当前批次，退出码为 `130`。
- 再次运行同一条命令即可续跑。
- 启动时会自动回收超过 60 分钟仍为 `RUNNING` 的僵尸批次。
- 终端每 15 秒输出一次当前步骤心跳；显示“仍在运行”表示进程没有失联。
- 全市场盘后包失败时会快速记录失败并继续下一个交易日，不会自动执行五千多只
  股票的逐只 Baostock 历史降级。Baostock 仍用于证券目录降级与停牌状态确认。
- 未指定 `--end` 时默认只处理到昨天，避免盘中请求尚未生成的当天盘后包。
- 显式指定今天且盘后包尚未发布时，会显示“等待发布”，不计入失败。

`data sync` 是日常唯一需要执行的命令。它会依次完成行情补齐、本次日期范围内的增量质量
状态重建、证券生命周期更新和最终审计。连续多日未运行时用 `--days` 扩大检查范围，例如：

```bash
uv run python main.py data sync --days 30
```

`data repair-quality` 只用于质量规则升级或全历史状态需要重建的场景，不需要每天执行。

`data sync` 在全市场同步和质量处理成功后，会统一执行数据底座附加项。目前包含通过腾讯
K线增量补齐 `SSE:000985`（中证全指）：只从最早缺失完整交易日请求到最新完整交易日，
无缺口时不联网。它记录独立 `BENCHMARK_BARS` 批次、原始payload、来源URL和校验和，
不会把局部指数数据伪装成完整全市场快照。以后新增的数据底座能力也必须接入该统一入口，
不得增加用户需要记忆的同步命令；`backtest run` 不会隐式联网。

## 研究扫描

研究扫描与数据同步分离，避免扫描时隐式联网：

```bash
# 1. 补齐行情并完成数据审计
uv run python main.py data sync

# 2. 显式刷新前复权因子（会联网）
uv run python main.py research factors

# 3. 只读审计研究快照
uv run python main.py research audit

# 4. 只读扫描并生成 reports/latest.json
uv run python main.py research scan
```

也可指定历史研究日或策略：

```bash
uv run python main.py research audit --date 2026-10-13
uv run python main.py research scan --date 2026-10-13 --strategy rps
uv run python main.py research scan --strategy ma_volume --strategy turtle
```

`research audit` 和 `research scan` 不访问网络。如果 V3 基础审计、统一数据日、
前复权因子新鲜度或覆盖率不符合要求，命令会以非零状态退出。不会写入新报告覆盖
上一份成功报告。

默认执行 `ma_volume,turtle,flag,rps`。`shakeout` 和 `limit_down` 必须通过
`--strategy` 或显式修改 `ENABLED_STRATEGIES` 选择，报告会附带未回测风险说明。
`private_placement` 尚未迁移，不能通过 V3 CLI 运行。

`research factors` 启动后会输出当前证券、处理进度、成功/失败/不支持数量、
累计写入因子和耗时。按 `Ctrl+C` 时当前批次会标记为中断，已完成证券的数据保留，
命令以状态码 `130` 退出。当前因子源不覆盖北交所和中国存托凭证（CDR）；二者
计入“不支持”并安全跳过，不计为刷新失败。

`research scan` 启动后会输出配置解析、研究快照构建、逐策略计算、报告写入四个
阶段。最终 Summary 会列出统一数据日期、QFQ 覆盖率、排除数量、各策略入选数量、
去重候选数量、耗时和实际报告路径。
