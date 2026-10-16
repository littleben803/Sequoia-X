# V3 数据库说明

默认数据库：`data/sequoia_v3.db`。

| 表 | 用途 |
|---|---|
| `schema_metadata` | Schema 版本 |
| `instruments` | 当前证券主记录 |
| `instrument_source_records` | 各来源的原始证券记录 |
| `ingestion_batches` | 下载批次、状态、错误、校验和与统计 |
| `daily_bars_raw` | 不丢失来源的原始日行情 |
| `daily_bars_canonical` | 当前选优日行情，后续供研究层读取 |
| `adjustment_factors` | 可审计的前/后复权因子、Provider 和获取批次 |
| `trade_calendar` | 完整自然日及开闭市状态 |
| `instrument_daily_status` | 成交、停牌、未上市、退市和异常缺口证据 |
| `instrument_code_aliases` | 旧代码到当前代码的有效期映射，例如北交所 `43/83/87` 到 `920` |
| `daily_bars_research` | 将历史代码映射到当前证券身份的只读研究视图 |

证券 ID 使用 `交易所:代码`，例如 `SSE:600519`，避免只用六位代码造成市场碰撞。
未知证券类型保存为 `UNKNOWN`，不能丢弃。

`list_date` / `delist_date` 保存证券事实生命周期；`code_valid_from` /
`code_valid_to` 保存某个交易代码的有效期。两者不能混用。北交所代码切换不会改写原始记录，
研究层通过 `daily_bars_research.research_instrument_id` 获得连续历史。
当官方当前目录只能证明证券已不再上市、但没有直接给出截止日时，质量重建使用 raw 与
canonical 行情的联合日期边界推导 `code_valid_to`；不能只依赖 canonical，以免零成交等
仅保留在原始层的最后记录导致退市代码被错误延长。
复权因子不直接改写 canonical 行情，研究快照在读取时按生效日应用，
以保持原始价和派生研究价之间的可追溯性。

原始层只执行不破坏记录的基础结构校验。OHLC 业务合法性按资产类型分别审计；当前质量闸门
只对 `STOCK` 使用 A 股 OHLC 规则，不会用股票规则误删债券、回购或其他特殊证券。
