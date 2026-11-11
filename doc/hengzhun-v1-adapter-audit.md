# 衡准 V1 真实市场适配审计

审计对象是本次运行实际读取的 BTC 五分钟市场，不用人工测试合同替代远程事实。证据目录为 `reports/hengzhun-implementation-20261106-231438/public-audit/`；`manifest.json` 保存原始响应的 URL、接收时刻、HTTP 状态及 SHA256。本次全部为公开 GET，没有提交、撤销或领取交易。

## 已观察的字段

市场 `btc-updown-5m-1793982600`：

| 项目 | 当前证据 | 结论 |
|---|---|---|
| 合约窗口 | Gamma `eventStartTime` 与 `endDate` | 2026-11-06 16:30:00–16:35:00 UTC，300 秒；`startDate` 是更早的上线时间 |
| 结算流 | Gamma `resolutionSource`、`cryptoMarketConfig` | BTC/USD、Chainlink TWAP、60 秒回看；与现价分开 |
| 市场协议 | Gamma `version=v1`、CLOB `v=v1` | 本次为 CTF；两个 CLOB token 与两侧盘口 `asset_id`、`market` 一致 |
| 价格与最低量 | Gamma、CLOB 市场和两侧 book | tick 0.01、最低 5 份，三处一致 |
| 费用曲线 | Gamma `feeSchedule` 与 CLOB `fd` | rate 0.07、exponent 1、takerOnly true，两处一致 |
| 到达延迟 | 本次原始 Gamma / CLOB 响应 | 没有足够的确定延迟字段，保持 UNKNOWN，不能填零 |

官方说明分别核对了 [结算规则](https://docs.polymarket.com/concepts/resolution)、[费用](https://docs.polymarket.com/trading/fees)、[市场字段](https://docs.polymarket.com/market-data/market-details) 和 [下单金额及精度](https://docs.polymarket.com/trading/place-orders)。协议版本与签名域版本必须分开。费用文档的精度说明还不足以独立证明所有实际成交的取整细节。

## 尚未解除的接入门槛

- **IV01**：原采集器已有精确起点 TWAP 与官方开盘值的对照记录，但衡准新数据集还没有一套带原始字节、首次接收时间、起终点和最终结果的完整证据链。缺边界 tick 时不采用最近价或插值。
- **IV02**：费用曲线与最低量已对上；逐笔实际现金、净份额、逐档取整及 builder 费用尚未完成同一协议的完整对账。人工夹具通过不代表实际平台验收通过。
- **IV03**：本次 CTF 资产标识已对上；完整的现金资产、签名域、SDK、REST、WebSocket、Data API v2 版本组合尚未逐项验收。旧适配器能运行不等于本设计的合同已经核验。
- **IV05**：没有用于衡准验收的真实执行样本，S1 / S2 / S3 均保持 ASSUMED。

因此当前策略按原设计运行 RECORD，保存原始行情、缺席 landmark 和诊断信息；不把合同标成 VERIFIED，不生成假训练模型，也不声称已完成正式 Paper 或收益验收。上述门槛需要补齐证据，单纯累计运行时间不会自动解除。
