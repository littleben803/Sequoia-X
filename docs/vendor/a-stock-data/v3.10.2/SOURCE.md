# a-stock-data 来源记录

- 上游仓库：https://github.com/simonlin1212/a-stock-data
- 上游版本：v3.10.2
- 上游 `SKILL.md` Git blob：`8e334569f7cfd334b56f3b3bd067469d175ba892`
- 归档日期：2026-10-14
- `SKILL.md` SHA-256：`d30aa90edb881a45168c397a6ca1ccfebc440a8d4c93b3d94d3123d27b260a1d`
- 许可证：Apache-2.0，原文见同目录 `LICENSE`

本项目不会在运行时解释或执行该文档。正式实现由人工选择代码段、补充类型、错误契约、
来源记录和测试后放入 `sequoia_x/data/providers/`。

当前已提取：

- 通达信全市场盘后包下载、解析和完整性校验。
- 深交所官方交易日历。
- 新浪前/后复权因子。
- 统一 A 股目录接口。

当前没有提取腾讯 K 线：V3 主路径按交易日使用通达信盘后包；个股缺口暂由
`BaostockProvider` 明确降级，后续需要北交所或分钟线时再单独接入腾讯能力。
