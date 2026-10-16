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
- V3 A 股研究快照层：统一数据日、前复权因子证据、覆盖率闸门和明确排除原因。

默认四个策略已切换到 V3 研究快照。扫描只读本地数据，质量闸门或复权因子
覆盖不足时会停止，不会静默输出部分候选股。

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

## 研究命令

```bash
# 数据同步后，显式联网刷新沪深 A 股前复权因子
uv run python main.py research factors

# 只读检查研究日、因子覆盖和排除原因
uv run python main.py research audit

# 运行默认四个策略，原子写入 reports/latest.json
uv run python main.py research scan

# 显式运行单个策略；高风险策略只能这样显式选择
uv run python main.py research scan --strategy rps
uv run python main.py research scan --strategy shakeout

# 旧参数等价于默认策略扫描
uv run python main.py --scan-only
```

`research scan` 本身不访问网络。`research factors` 会对沪深证券逐只请求因子，
耗时取决于证券数量和源站状态。终端会实时显示当前证券、成功/失败/不支持数量、
累计写入量和耗时。当前因子源不覆盖北交所和中国存托凭证（CDR）；这些证券会
明确计入“不支持”，不会伪造因子或导致刷新失败，并从 QFQ 研究股票池排除。

`research scan` 会依次展示扫描配置、可信研究快照构建、逐策略计算和报告写入四个
阶段；完成后输出数据日期、覆盖率、各策略入选数量、去重候选数量和报告路径 Summary。

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
研究快照的质量和复权边界见 [V3 研究数据层](docs/architecture/research-data-layer-v3.md)。

## 文档与上游归档

- [数据底座 PRD](docs/prd/data-foundation-v3.md)
- [Provider 架构](docs/architecture/data-provider-architecture.md)
- [停牌识别](docs/data/suspension-detection.md)
- [a-stock-data 能力目录](docs/data/a-stock-data-capability-catalog.md)
- [归档的 a-stock-data v3.10.2 SKILL.md](docs/vendor/a-stock-data/v3.10.2/SKILL.md)

## 内置策略

| 策略 | 说明 | 规划状态 |
|---|---|---|
| MaVolume | 5/20 日均线金叉 + 放量 | V3 默认启用 |
| TurtleTrade | 20 日新高 + 成交额 + 阳线过滤 | V3 默认启用 |
| HighTightFlag | 强动量后高位收敛缩量 | V3 默认启用 |
| RpsBreakout | 120 日相对强度突破 | V3 默认启用 |
| LimitUpShakeout | 涨停后洗盘形态 | 已迁移，默认禁用，待独立回测 |
| UptrendLimitDown | 上升趋势中的放量跌停 | 已迁移，默认禁用，待独立回测 |
| PrivatePlacement | 最近定增事件监控 | 禁用，待回测 |

## 验证

```bash
uv run pytest
uv run ruff check .
uv run python -m compileall -q main.py sequoia_x
uv run python main.py --help
uv run python main.py data --help
uv run python main.py research --help
git diff --check
git status --short
```

## License

归档的 a-stock-data 代码依据其 Apache-2.0 许可证使用，许可证和来源记录保存在对应
`docs/vendor/` 目录。Sequoia-X 自身发布前仍需补充独立许可证文件。
