# Sequoia-X

> 面向 A 股研究、可扩展到基金与债券的本地市场数据工具

Sequoia-X V3 当前优先建设可信数据底座：按交易日下载通达信全市场盘后包，将股票、
基金、ETF、债券、可转债、REITs 和未分类证券的原始日行情保存到本地 SQLite。

> [!WARNING]
> 本项目不是自动交易系统，不包含买卖、仓位和收益保证。策略输出仅用于个人研究。

## 当前阶段

已经提供：

- `AStockDataProvider`：通达信全市场盘后包、深交所交易日历、统一 A 股目录和复权因子。
- `BaostockProvider`：沪深 A 股主数据增强、日线和停牌状态降级。
- V3 多资产证券主数据、原始行情、canonical 行情、批次证据和质量审计。
- 按交易日回填、增量、断点续跑以及彩色终端进度。

策略代码仍保留，但尚未切换到 V3 数据。普通启动和 `--scan-only` 不会输出候选股，避免
把旧后复权模型误用到新的原始行情。下一阶段将建立 A 股研究视图后再接回策略。

## 安装

需要 Python 3.10 或更高版本，推荐使用 `uv`：

```bash
brew install uv
cd /Users/ideabox/repo/Sequoia-X
uv sync --extra dev
cp .env.example .env
```

默认数据库为 `data/sequoia_v3.db`。`.env`、数据库、日志和报告只保留在本机。

## 数据命令

```bash
# 首次按交易日回填全部原始盘后数据
uv run python main.py data backfill

# 指定范围验证或补数
uv run python main.py data backfill --start 2026-10-13 --end 2026-10-13

# 日常一键更新：行情补齐 + 增量质量处理 + 最终审计
uv run python main.py data sync

# 只读审计
uv run python main.py data audit

# 重建证券生命周期、代码别名和缺口状态（主数据规则升级后执行）
uv run python main.py data repair-quality

# 查看证券类型统计与最近批次
uv run python main.py data instruments
uv run python main.py data batches
```

旧命令暂时兼容：

```bash
uv run python main.py --backfill
uv run python main.py --audit
```

详细运行说明见 [V3 数据运行手册](docs/data/operations.md)。

## 数据源与降级

主路径每个交易日只下载一个通达信盘后包。包内三市场数据必须通过文件存在、块长度、
代码唯一性、有限数值和最低记录数检查后才能提交。

全市场盘后包失败时快速记录该交易日并继续，不会触发五千多只股票的逐只历史降级。
Baostock 只用于证券目录降级和无成交状态确认，不能把 A 股局部结果伪装成全品种成功。

完整盘后包中的零成交记录或代码有效期内未出现记录标记为 `NO_TRADE_OBSERVED`，只用于
解释覆盖，绝不生成当日交易信号。备源明确报告有交易但 canonical 行情缺失时，才保留为
`MISSING_UNEXPLAINED` 并阻止质量闸门通过。

## 数据模型

- 证券 ID 使用 `SSE:600519`、`SZSE:000001`、`BSE:920982`。
- `daily_bars_raw` 保存不同 Provider 和复权口径，来源互不覆盖。
- `daily_bars_canonical` 是可重建的选优层，未来策略只能读取这一层或研究视图。
- `instrument_code_aliases` 保存北交所旧代码到当前 `920` 代码的有效期映射。
- `daily_bars_research` 在不改写原始行情的前提下提供代码连续的研究数据。
- 无法识别的证券以 `UNKNOWN` 保存，不能丢弃。
- 每次下载记录在 `ingestion_batches`，包括状态、来源 URL、校验和、数量和错误。

数据库说明见 [V3 Schema](docs/architecture/database-schema-v3.md)。

## 文档与上游归档

- [数据底座 PRD](docs/prd/data-foundation-v3.md)
- [Provider 架构](docs/architecture/data-provider-architecture.md)
- [停牌识别](docs/data/suspension-detection.md)
- [a-stock-data 能力目录](docs/data/a-stock-data-capability-catalog.md)
- [归档的 a-stock-data v3.10.2 SKILL.md](docs/vendor/a-stock-data/v3.10.2/SKILL.md)

## 内置策略（暂未接入 V3）

| 策略 | 说明 | 规划状态 |
|---|---|---|
| MaVolume | 5/20 日均线金叉 + 放量 | 下一阶段接入 |
| TurtleTrade | 20 日新高 + 成交额 + 阳线过滤 | 下一阶段接入 |
| HighTightFlag | 强动量后高位收敛缩量 | 下一阶段接入 |
| RpsBreakout | 120 日相对强度突破 | 下一阶段接入 |
| LimitUpShakeout | 涨停后洗盘形态 | 禁用，待回测 |
| UptrendLimitDown | 上升趋势中的放量跌停 | 禁用，待回测 |
| PrivatePlacement | 最近定增事件监控 | 禁用，待回测 |

## 验证

```bash
uv run pytest
uv run ruff check .
uv run python -m compileall -q main.py sequoia_x
uv run python main.py --help
uv run python main.py data --help
git diff --check
git status --short
```

## License

归档的 a-stock-data 代码依据其 Apache-2.0 许可证使用，许可证和来源记录保存在对应
`docs/vendor/` 目录。Sequoia-X 自身发布前仍需补充独立许可证文件。
