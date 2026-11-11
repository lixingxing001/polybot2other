# B1/B2 独立回放订单合同

设计书要求 B1 旧策略和 B2 未校准先验保留独立、真实受资金约束的对照账户。然而原附件 `event_contracts.schema.json:2439` 至 `:2459` 规定，`OrderIntent.calibration_status=UNCALIBRATED` 必须使用 `SIGNAL_SHADOW`。把 B1/B2 标为 `CALIBRATED` 会虚构模型校准；把资金对照写为影子通道又会混淆收益口径。

因此项目新增独立合同 `BaselineOrderIntent`，版本 `1.0.0-baseline.1`，对应 `src/polybot2other/hengzhun/spec/baseline_event_contracts.schema.json`。原始附件未修改，衡准主策略继续遵守原始 `OrderIntent` 合同。扩展只允许 `REPLAY + PAPER_PORTFOLIO`、`UNCALIBRATED` 和空模型哈希，并要求明确的基准身份与冻结证据。`Fill`、`OfficialResult`、`LedgerEvent` 继续使用原附件合同。

账本构造参数 `replay_baseline` 必须包含以下字段：

| 字段 | 要求 |
| --- | --- |
| `baseline_id` | `B1_FROZEN_UNIFIED` 或 `B2_PRIOR` |
| `strategy_id` | 本次基准唯一策略身份 |
| `frozen_provenance_sha256` | 实际冻结策略或先验实现的证据摘要，不是模型哈希 |
| `config_sha256` | 本次完整账本配置的确定性摘要 |
| `input_manifest_sha256` | 冻结输入数据清单摘要 |
| `registered_at` | 预登记时间，必须不晚于任何入场时间 |

该清单在创建钱包前保存，并在经济空间内保持不可变。调用方负责先核验对应原始文件；账本核验摘要格式、配置内容匹配、身份、时间与持久化不可变性。订单规划必须使用同一策略 ID、`UNCALIBRATED`、`model_artifact_sha256=null`，并携带 `baseline_registration_sha256=digest(replay_baseline)`。新订单保存清单摘要和冻结证据摘要。

所有固定五美元预算、手续费、盘口、到达时刻、钱包风险、应收和一次意图约束继续执行。该合同不允许实时 PAPER 绕过衡准模型门槛，不启用实盘，不将未校准控制组称为已校准策略。旧策略原始风控的 B1 对照须使用另外的原始引擎及独立账户，不能通过此统一风控扩展假冒。
