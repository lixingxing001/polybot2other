衡准 V1 实现交付包
版本 1.0.0    日期 2026-11-06    面向 Lee 与实现 AI

阅读顺序
1 Hengzhun_V1_Design_Spec.docx：完整设计书，含全部参数与验收索引。
2 AI_IMPLEMENTATION_PROMPT.txt：可复制给实现 AI 的工作指令。
3 design_spec.json：与设计书正文同源的机器可读版本。
4 strategy_config.yaml 与 config.schema.json：默认参数及类型合同。
5 event_contracts.schema.json：内部标准化实体合同，远程接口须映射核验。
6 model_artifact.schema.json 与 model_unfitted.json：模型文件合同及未训练占位。
7 reference_math.py 与 golden_cases.json：离线纯数学参考和构造数值用例。
8 acceptance_matrix.json：五十条系统场景，当前全部 NOT_RUN。
9 implementation_checklist.json：M0 至 M5 的分阶段验收清单。
10 sources.json：官方依据、版本差异和接入阻塞。
11 verification_report.json：本次交付实际完成的文件及数值自检。
12 manifest.json：最终交付文件 SHA256 校验清单。
13 math_self_test_output.txt：本次二十七条数学自检的实际输出。

数学参考命令
python3 reference_math.py --self-test
python3 reference_math.py --example

当前证据范围
这是策略设计和实现合同。没有真实训练系数，没有完成历史回测、前向 Paper 或真实交易。数学用例与系统验收分开计数。
默认 RECORD，真实执行硬禁用。所有价格、延迟、样本门槛和风险阈值均按正文登记其证据性质，不能当作已经优化成功的参数。
JSON Schema 约束结构与局部类型，无法替代跨事件时间检查、账本守恒、预算证明及规则接入核验。
当前参考中的现金手续费为未做平台舍入的数学形式，不能代替 FeeAdapter。
