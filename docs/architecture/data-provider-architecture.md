# V3 数据 Provider 架构

## 职责

- Provider：只请求、解析和标准化，不写数据库、不做策略。
- ProviderRouter：按能力选择主源与降级源，保留两侧错误。
- DataApplicationService：交易日调度、续跑、停牌确认和结构化进度。
- MarketDataRepository：SQLite 事务、幂等写入、批次证据和审计。
- Renderer：终端显示；未来 Web 页面消费同一事件模型。

## 来源顺序

1. AStockDataProvider：通达信盘后包、深交所日历、沪深北交易所官方 A 股目录、复权因子；
   官方目录失败时内部降级到东财聚合目录。
2. BaostockProvider：沪深 A 股主数据增强、单证券原始日线和停牌状态。
3. 两者都失败才返回失败；Baostock 只覆盖 A 股时状态为 `DEGRADED_PARTIAL`。

## 数据口径

`daily_bars_raw` 保存不同来源和不同复权口径，唯一键为
`(instrument_id, trade_date, provider, adjustment)`。`daily_bars_canonical` 是可重建的选优层，
策略未来只能读 canonical 或专门的研究视图，不能直接猜测原始来源。

## 安全边界

- 不按日期删除其他证券或其他来源数据。
- 盘后包必须通过三市场文件、块长度、代码唯一性和最低记录数检查后才提交。
- 一个完整快照在同一批次事务内提交；失败批次保留错误证据。
