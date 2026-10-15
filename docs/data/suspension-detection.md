# 停牌与行情缺失判定

盘后包没有记录不等于停牌。

1. 非交易日标记 `NOT_APPLICABLE`。
2. 上市日前标记 `NOT_YET_LISTED`，退市后标记 `DELISTED`。
3. 盘后包有有效行情标记 `TRADED`。
4. 当前上市 A 股缺失时，只对缺失证券查询 Baostock `tradestatus`。
5. `tradestatus=0` 才标记 `SUSPENDED_CONFIRMED`。
6. Baostock 返回行情说明主源漏数，后续可写入降级行情。
7. 完整盘后包内存在零成交记录，或有效代码在完整盘后包中没有成交记录，标记
   `NO_TRADE_OBSERVED`；它可以解释覆盖，但不能生成当日策略信号。
8. 备源明确报告有交易、但完整盘后包和 canonical 行情均缺失时，才标记
   `MISSING_UNEXPLAINED`。

`SUSPENDED_CONFIRMED` 和 `NO_TRADE_OBSERVED` 都属于“已解释但无当日交易信号”；只有
canonical 行情才能进入策略计算。非 A 股资产在当前 MVP 只保证原始数据落库，之后为
不同资产建立独立质量规则。
