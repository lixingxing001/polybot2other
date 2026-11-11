# 衡准 V1：运行、证据与离线命令

策略 ID 为 `HENGZHUN_TWAP_VALUE_V1`，设计版本为 `1.0.0`。实现采用用户确认的现有 Python 技术栈。原附件放在 `src/polybot2other/hengzhun/spec/`；源文档和附件的校验结果在 `reports/hengzhun-implementation-20261106-231438/`。

## 当前接入方式

`start-macos.sh` 启用独立的衡准观察器。默认模式是 **RECORD**：接收原始行情、市场合同、决策特征、landmark 缺席情况和标签证据，不产生经济订单。界面新策略行和详细面板显示采集数量、模型状态、时钟和阻断原因；只读接口为 `GET /api/hengzhun`。这不是已经训练完毕或已经取得盈利的声明。

运行数据在 `data/hengzhun-v1/`：

| 路径 | 用途 |
|---|---|
| `run.json` | 冻结配置、策略版本和代码清单 |
| `events.sqlite3` | 原始事件、压缩原始字节、双时钟价格、合同、特征、landmark、官方结果 |
| `ledger.sqlite3` | 独立模式/运行/情景/账本范围的钱包、意图、成交及结算 |
| `models/current.json` | 当前模型，首次启动为原附件 `UNFITTED` |
| `verified-contracts/<condition_id>.json` | 有真实证据的市场合同；不能把模板改成 VERIFIED 来跳过审计 |
| `strategy_config.yaml` | 可选覆盖配置；同一 run 修改冻结配置会被拒绝，须建立新实验目录 |

本策略固定独立初始模拟资产 $100、每笔预算 $5（含费用），不继承原四个策略的实盘或 Paper 金额设置。只有 RECORD、REPLAY、PAPER 三种模式。不存在可用的真实签名、真实下单、撤单或领取入口；`RealVenue` 直接抛出 `LIVE_EXECUTION_DISABLED`。已有四个策略的原账本和设置不属于此目录。

当前官方接入证据详见 [IV01–03 接入审计](hengzhun-v1-adapter-audit.md)。这些合同问题需要逐项补齐证据和核验，不能仅靠等待采集时间自动变成 VERIFIED。

## 首轮启动采集与吞吐修复

2026-11-07 的首轮真实 RECORD 启动暴露出采集处理速度不足，队列溢出并丢失部分事件。该轮数据已完整保留在 `data/hengzhun-v1-startup-overflow-20261107-015032/`，其中保留 36,850 条原始记录；这一归档用于定位工程问题，不能当作连续完整的训练数据、有效完整 UTC 日或严格执行回放样本。

修复准备阶段曾临时停用衡准观察器，原四个策略继续运行且设置保留。完整盘口校验缓存、数据库批量事务、采集时间片和迟到结果处理修复后，最终全套 **735 项测试、169 个 subtest 通过**；原附件 **50 个工程场景全部通过**，新源码、测试源码和 JUnit 摘要已重新复核。真实运行的队列和丢弃数量仍须以新服务的采集观察为准，工程压测不能替代运行证据。

新的有效研究采集使用新的独立运行记录，不拼接首轮异常归档；不能把溢出缺口补写成当时已接收的数据。

## 可复制的检查命令

从项目根目录执行；所有输出目标都必须是尚不存在的新文件或目录，避免覆盖旧研究证据。

```bash
.venv/macos/bin/python -m polybot2other.hengzhun validate-config
.venv/macos/bin/python -m polybot2other.hengzhun golden --output /tmp/hengzhun-golden.json
.venv/macos/bin/python -m pytest -q tests/test_hengzhun_*.py --junitxml=/tmp/hengzhun-tests.xml
```

`golden` 对本实现运行附件中的全部 27 个人工数学样例，不能证明真实数据质量、模型资格或收益。实际 50 场景索引生成器为：

```bash
.venv/macos/bin/python reports/hengzhun-implementation-20261106-231438/build_acceptance_report.py \
  --junit /tmp/hengzhun-tests.xml
```

未经过原场景逐项覆盖审查的通过测试仍显示 `REVIEW_REQUIRED`；不能因总测试数很大就写 G1 通过。原始附件的 `NOT_RUN` 保持不变。

## 导出与训练

导出使用 SQLite 一致性只读快照，不停采集、不更新原数据库。`--as-of-ms` 是要保留的真实接收时刻截止（UTC epoch 毫秒），不能用本地显示时区字符串代替：

```bash
.venv/macos/bin/python -m polybot2other.hengzhun export \
  --database data/hengzhun-v1/events.sqlite3 \
  --as-of-ms 1794009600000 --output /tmp/hengzhun-dataset-example
```

示例时刻仅演示格式，实际研究应使用预登记的真实截止时刻。导出包括原始 JSONL、原始字节摘要复核、合同、特征、landmark、官方结果、训练候选行和排除原因。缺失 landmark 不补写；晚到和修订标签保留真实可得时间。仅有导出文件不足以证明一个 UTC 日完整，`complete_utc_days_status` 默认 `NOT_ESTABLISHED`。

训练请求是 JSON 对象，字段与 `calibration.train_candidate()` 对应：

```json
{
  "training_rows": [],
  "validation_rows": [],
  "complete_utc_days": [],
  "training_window": {"start_at": 0, "end_at": 2419200000},
  "validation_window": {"start_at": 2419200000, "end_at": 3024000000},
  "evaluated_at": 3024000000,
  "rule_version": "ACTUAL_VERIFIED_RULE_VERSION",
  "rule_contract_hash": "ACTUAL_64_HEX_RULE_CONTRACT_HASH",
  "code_revision": "ACTUAL_FROZEN_CODE_REVISION",
  "coverage_evidence": {}
}
```

上述空数据是格式示例，结果必为样本不足，不是可发布训练集。实际行来自可审计采集，至少包含 condition_id、rule_version、window_open_at、decision_at、label_available_at、label_verified_at、y、p_raw_up、p_market_up、tau_wall_seconds。训练 28 天、完整日 ≥20、市场 ≥3000、两类各 ≥300；后移 7 天验证 ≥1000 市场。每市场所有 landmark 合计权重为一，训练与验证市场不得重叠。不得把 tick 数当独立市场数。

```bash
.venv/macos/bin/python -m polybot2other.hengzhun train \
  --request /path/to/training-request.json --output /path/to/new-model-run
```

命令产生候选模型、100 次训练日 bootstrap、输入和参数清单及验证结果；不足样本输出 `INSUFFICIENT`，不会造零系数模型。通过验证的候选也不会自动替换运行模型。发布需要真实市场边界，模型七天过期；不能追溯应用于早于发布时刻的市场。

## 三情景回放

完整策略重放命令读取导出的原始事件、市场合同和冻结模型，每个情景独立运行特征、决策和账本，保留 S1/S2/S3 全部结果：

```bash
.venv/macos/bin/python -m polybot2other.hengzhun replay \
  --events /path/to/dataset/events.jsonl \
  --contracts /path/to/dataset/contracts.jsonl \
  --model /path/to/frozen-model.json \
  --clock-bound-ms 100 \
  --final-at-ms 1794009600000 --output /path/to/new-replay-run
```

`--clock-bound-ms 100` 只是命令格式，不能把无时钟证据的数据伪造为 100ms。显式外部时钟上界属于 `ASSUMED` 假设，报告必须披露；原始时钟证据存在时应优先保留原始证据。无效合同、未拟合模型和未满足的数据条件会产生阻断记录，不能强制开仓。没有原始首次接收时间的旧历史数据，不能升级为严格回放。

另有 `execution-replay --events ... --intents ... --output ...`，仅重放事先冻结的提交意图，适合定位成交/确认/结算状态。意图 JSON 数组每项须含 `decision_event_id`、`submitted_at`、`clock_bound_ms`、`contract`、`plan`；提交时刻必须与所引用输入事件接收时刻严格相等。此入口明确标记 `FROZEN_INTENT_EXECUTION_ONLY`，不能代替全策略回放或资金连续的样本外研究。

S1 为 1 秒、50% 深度；S2 为 2 秒、25%；S3 为 5 秒、25%。已知官方等待时间取与情景延迟的较大值，不重复相加；未知仍是 UNKNOWN。确认从撮合后 12 秒计算，领取从四项经济结算证据齐全后 60 秒计算。全部是 `ASSUMED` 执行参数，不能称为已验证实盘成交表现。

符合条件的模型信号还进入独立 `SIGNAL_SHADOW` 通道：每个市场只有一次固定预算的假设机会，使用相同的到达盘口、费用与确认结算流程。该通道用于观察被资金或持仓限制挡住的信号，报告中叫“假设损益”；其独立名义预算不能相加冒充一个 $100 连续账户。RECORD 只采集诊断，不运行这些经济影子订单。

## B1 对照与研究结论

冻结可取得的现有主账户代码和明确脱敏的参数：

```bash
.venv/macos/bin/python -m polybot2other.hengzhun freeze-baseline \
  --source-root . --settings /path/to/main-strategy-settings.json \
  --output /path/to/new-b1-snapshot
.venv/macos/bin/python -m polybot2other.hengzhun report \
  --evidence /path/to/research-evidence.json --output /path/to/new-research-report.json
```

冻结器拒绝凭据字段，并保存源文件原字节、参数和 SHA256。当前可取得的代码快照会明确记录 `historical_original_version_verified=false`；不能称作未找到的最早原始版本。仅有源代码快照并不等于已有连续 B1 对照收益。`report` 评估调用方提供的证据，标注尚未独立复核的来源范围。

G2 模型资格、G3 六周样本外净收益、G4 相对 B1 改善是不同门槛。没有足够真实数据时必须 `INSUFFICIENT`；没有连续基准结果时不能宣称优于主账户。原四个策略的历史盈利不作为本策略的校准或盈利证据。

## 完整周步进研究

`research-run` 负责实际编排训练、发布、连续账户回放与研究统计；不会逐周重置资金。每个 S1/S2/S3 各自运行完整测试期，同时保留 B2、B1 统一风控对照和 B1 原账户单独结果：

```bash
.venv/macos/bin/python -m polybot2other.hengzhun research-run \
  --request /path/to/research-request.json --output /path/to/new-registered-study
```

请求字段为：`events`（原始事件 JSONL）、`contracts`（合同 JSONL）、`landmark_rows`（导出训练候选 JSON）、`coverage`（完整 UTC 日证据 JSON，包含 `complete_utc_days`）、`first_day_at`、`end_at`（完整 UTC 日边界）、`candidate_count`、`candidate_history_known=true`、`rule_version`、`rule_contract_hash`、`code_revision`；可选 `config`、`clock_bound_ms`、`baseline_folder`、`acceptance_report`。数据、代码和请求在计算成绩之前写入新实验注册清单，不能覆盖已有实验。

历史回放的模型发布时延使用本机实际离线拟合耗时，映射至验证周结束后首个完整市场边界，报告标记 `ASSUMED`。不能把离线拟合说成历史当时真实完成。验证不通过、样本不足或没有可用发布边界的折保持缺席，不能倒填早期测试交易。

每日净收益按连续账户 UTC 日末 NAV 差计算，包含无交易日；未知 NAV 及受影响的次日收益均保留未知，不删除亏损或无法估值的日期。最后的未结算、UNKNOWN 和未领取尾部单列。额外一个 tick 的诊断只重算已观察成交集合的成本，不声称原限价仍会成交。完整样本、执行适用性或对账证据不足时，研究门槛继续显示 INSUFFICIENT。

## 本次启动验收

最终服务 PID `42774`，地址 `http://127.0.0.1:8791/`，运行 RECORD。重启前后的实盘设置 revision 48 完全一致：总开关关闭，主账户单笔 $5、最多 5 笔。原四账户没有重置。

连续 120 秒观察新增 15,866 条采集事件，13 次抽样无运行错误、丢弃数始终为 0，末次队列为 0；这只是短时运行验收。首次启动溢出的归档仍保留且排除。`deployment-final.json`、`runtime-observation.jsonl` 与 `runtime-observation-summary.json` 保存证据。页面及静态资源 HTTP 200，JavaScript 语法检查通过；浏览器/本机 UI 工具连接不可用，未做截图验收。

已按实际采集的参数冻结当前修正版主账户，见报告目录 `baseline-main-current/manifest.json`，与未来的未修订历史版本声明区分。当前模型 UNFITTED，IV01–03 仍待核验；现场时钟误差上界亦未通过，正式 Paper 不会自动获准。
