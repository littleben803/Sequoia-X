# a-stock-data 能力目录

上游详细代码和说明见 `docs/vendor/a-stock-data/v3.10.2/SKILL.md`。

| 领域 | 主要能力 | 当前状态 | 未来 Provider 建议 |
|---|---|---|---|
| 行情 | 腾讯实时、K 线、逐笔；通达信盘后包；复权因子 | 盘后包/因子已接 | AStockDataProvider |
| 证券主数据 | 股票信息、上市退市、ST、行业 | 部分接入 | AStockDataProvider |
| 交易日历/指数 | 日历、成分、权重、估值 | 日历已接 | AStockDataProvider |
| 研报 | 东财、新浪、同花顺、问财 | 未接 | ResearchProvider |
| 新闻 | 财联社、东财、华尔街见闻、新闻联播 | 未接 | NewsProvider |
| 财务估值 | 财报三表、F10、估值、股东数据 | 未接 | FundamentalProvider |
| 资金面 | 资金流、两融、大宗、ETF 份额、筹码 | 未接 | CapitalFlowProvider |
| 公告 | 巨潮和交易所公告 | 未接 | AnnouncementProvider |
| 事件 | 龙虎榜、涨停、解禁、回购、调研、新股 | 未接 | EventProvider |
| 衍生品 | ETF 期权、期货、商品、可转债 | 未接 | 对应资产 Provider |

不再使用的 mootdx 行情能力仅留档；上游已经记录其行情命令从 2026-09 起返回空数据。
