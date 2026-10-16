# V3 研究数据层

## 目标

研究数据层在 V3 原始数据和选股策略之间建立可审计边界。它只读取
`daily_bars_research`，统一确定研究日、A 股样本空间和价格口径，不包含选股公式。

## 质量闸门

生成快照前必须满足：

- V3 数据审计通过；
- 研究日存在完整全市场盘后批次；
- 所有纳入证券的最后一根行情均等于统一研究日；
- 前复权因子已在不早于研究日的完整批次中刷新；
- 因子序列覆盖所有返回的历史行情；
- 纳入证券覆盖率不低于调用方指定的闸值。

任何闸门失败都必须停止研究扫描，不得返回部分结果并宣称成功。

## 复权口径

- `adjustment_factors` 保存证券、生效日、口径、Provider、原始 payload、
  获取批次和时间。
- 研究快照使用 QFQ：`adjusted_price = raw_price / factor`。
- 成交量和成交额保持原始口径。
- 因子为空、非正数、非有限数或无法覆盖历史时，该证券必须排除。
- 当前因子源不覆盖北交所和中国存托凭证（CDR）。这些证券标记为
  `UNSUPPORTED_FACTOR_SOURCE` 并从 QFQ 研究股票池排除，不得使用原始价或固定
  `factor=1` 替代。

## 职责边界

- `AdjustmentFactorService`：调用 Provider，以单证券独立批次刷新因子。
- `MarketDataRepository`：幂等保存因子和批次证据。
- `ResearchRepository`：只读查询研究视图和因子。
- `ResearchDataService`：执行质量闸门并生成不可变研究快照。
- 策略层：下一阶段只消费 `ResearchSnapshot`，不得访问数据库或网络。
