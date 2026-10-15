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
