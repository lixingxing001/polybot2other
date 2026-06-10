# Aggressive Edge V4 到 V12 交接文档

生成时间：2026-06-09 13:53:02 +08  
项目路径：`/home/project/polybot2other`  
服务端口：`8791`  
面向对象：换电脑、换 API 入口后的下一次 Codex 开发会话

## 先读结论

这份文档用于让新的 Codex/API 会话接续开发 `SINGLE + FAK Aggressive Edge` 系列，重点覆盖 V4 到 V12 的演进过程、当前样本池口径、实盘状态和后续开发边界。

当前最重要的结论：

1. 样本页的 V4 到 V12 现在统一读取同一个诊断数据池：`SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC`。
2. V4 到 V12 共用基础样本池，但各版本按自己的 `v*_would_trade` 字段筛选放行样本，所以胜率和 ROI 会不同。
3. 当前实盘策略是 `SINGLE_FAK_AGGRESSIVE_EDGE_V11_REAL`，V12 目前是影子诊断和候选验证，尚未作为 REAL 策略上线。
4. 当前实时快照里 V12 的影子样本表现最好，但不要直接把 V12 切到实盘，先做 V12 输单复盘、REAL 迁移设计和预检。
5. 新会话第一步必须先看 `git status --short`，当前工作区有未提交改动。

## 协作规则

Lee 的本地项目规则：

1. 中文回复，称呼 Lee。
2. Shell 命令使用 `rtk` 前缀。
3. 开发任务先说明理解，Lee 确认“开始”后再改代码。
4. 后端接口参数不要直接用 `Map` 接收，优先 DTO/VO 或项目现有结构。
5. 改真实交易相关逻辑时必须非常克制，先查现有代码、日志、接口、数据库，再给结论。
6. 不要把历史 dirty 改动回滚掉，除非 Lee 明确要求。
7. 每次完成回答末尾带版本号，方便回退沟通。

## 项目运行信息

常用启动命令：

```bash
rtk bash -lc 'setsid -f .venv/bin/python -m polybot2other.web --host 0.0.0.0 --port 8791 >> .runtime/bot-8791.log 2>&1 < /dev/null'
```

健康检查：

```bash
rtk bash -lc 'curl -sS -m 10 http://127.0.0.1:8791/api/status | jq "{running:.runtime.running,paper:.runtime.paper_trading,current_market:.runtime.current_market.slug}"'
```

Windows 访问地址，按当前 WSL IP：

```text
http://172.26.159.17:8791
```

日志：

```text
/home/project/polybot2other/.runtime/bot-8791.log
```

主要数据库：

```text
data/polybot2other-real-btc.sqlite3
data/live/single_fak_aggressive_edge_v11_real.sqlite3
data/live/single_fak_aggressive_edge_v10_real.sqlite3
data/live/single_fak_aggressive_edge_real.sqlite3
```

## 环境变量和敏感配置交接

实盘相关配置当前通过 `.env.live` 加载。2026-06-09 检查时，文件权限是 `0o600`，敏感字段存在，权限状态正常。

换电脑或换 API 开发入口时，只同步变量名、权限要求和安全存储方式，不要把真实密钥值写进交接文档，也不要提交 `.env.live`。

实盘交易必要字段：

```text
POLYBOT2OTHER_LIVE_PRIVATE_KEY
POLYBOT2OTHER_LIVE_SIGNATURE_TYPE
POLYBOT2OTHER_LIVE_FUNDER_ADDRESS
POLYBOT2OTHER_LIVE_API_KEY
POLYBOT2OTHER_LIVE_API_SECRET
POLYBOT2OTHER_LIVE_API_PASSPHRASE
```

实盘运行和风控字段：

```text
POLYBOT2OTHER_LIVE_TRADING_DB_PATH
POLYBOT2OTHER_LIVE_TRADING_SETTINGS_PATH
POLYBOT2OTHER_LIVE_CHAIN_ID
POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED
POLYBOT2OTHER_LIVE_DEFAULT_INITIAL_BALANCE
POLYBOT2OTHER_LIVE_DEFAULT_STAKE_DOLLARS
POLYBOT2OTHER_LIVE_DEFAULT_MAX_DAILY_LOSS
POLYBOT2OTHER_LIVE_DEFAULT_MAX_TOTAL_DRAWDOWN
POLYBOT2OTHER_LIVE_DEFAULT_RETRY_COUNT
POLYBOT2OTHER_LIVE_DEFAULT_RETRY_DELAY_MS
```

LLM 复盘和智能体字段：

```text
POLYBOT2OTHER_LLM_SUPER_AGENT_ENABLED
POLYBOT2OTHER_LLM_API_KEY
POLYBOT2OTHER_LLM_BASE_URL
POLYBOT2OTHER_LLM_MODEL
POLYBOT2OTHER_LLM_TIMEOUT_SECONDS
POLYBOT2OTHER_LLM_MIN_INTERVAL_SECONDS
```

新环境预检命令：

```bash
rtk bash -lc 'curl -sS -m 10 http://127.0.0.1:8791/api/live-settings | jq ".readiness.env_files[]? | {path,loaded_keys,sensitive_keys_present,secure_permissions,mode}"'
```

当前服务快照：

```text
Paper：运行中
Live enabled：true
Live strategy：SINGLE_FAK_AGGRESSIVE_EDGE_V11_REAL
Live db：data/live/single_fak_aggressive_edge_v11_real.sqlite3
Stake：2.0
Max open trades：3
Initial balance：15.0
Readiness：true
Open official orders：0
```

## 当前工作区状态

2026-06-09 检查时，工作区有以下未提交改动：

```text
 M src/polybot2other/bot.py
 M src/polybot2other/experiments.py
 M src/polybot2other/live.py
 M src/polybot2other/static/app.js
 M src/polybot2other/static/index.html
 M src/polybot2other/storage.py
 M src/polybot2other/web.py
 M tests/test_core.py
?? .runtime/
?? tests/test_sample_monitor.py
```

接续开发时不要直接 `git checkout` 或 `git reset`。先读 diff，确认哪些是当前 Aggressive Edge 相关改动，哪些是运行日志目录。

推荐检查命令：

```bash
rtk git status --short
rtk git diff -- src/polybot2other/bot.py src/polybot2other/storage.py src/polybot2other/static/app.js src/polybot2other/static/index.html
```

## 关键代码入口

| 模块 | 用途 |
| --- | --- |
| `src/polybot2other/signal_filters.py` | 原始 Aggressive Edge 基础过滤，V1/V2 早期学习过滤 |
| `src/polybot2other/bot.py` | Paper 主循环、V4 到 V12 影子样本记录、各版本 block reason |
| `src/polybot2other/experiments.py` | 策略实验组合注册，V4 到 V12 Diagnostic 定义 |
| `src/polybot2other/storage.py` | `aggressive_edge_v2_shadow_samples` 表、V4 到 V12 汇总、实盘准入判断 |
| `src/polybot2other/live.py` | REAL 策略执行，当前只实现到 V11 REAL |
| `src/polybot2other/static/app.js` | 样本页版本切换、统一样本池、候选分页和局部刷新 |
| `src/polybot2other/static/index.html` | 样本页下拉、候选分页 DOM、live 策略选项 |
| `tests/test_core.py` | 核心测试，包含 V4 到 V12 汇总和策略逻辑测试 |
| `tests/test_sample_monitor.py` | V5 监控任务测试，当前未跟随 V12 完整改造 |

## 原始 Aggressive Edge 的设计

原始 `SINGLE + FAK Aggressive Edge` 是在 `SINGLE + FAK` 基础上新增更激进的单边入场过滤。基础过滤在 `signal_filters.py`：

```text
low_entry_high_edge：
  entry < 0.50
  confidence >= 0.65
  edge >= 0.12
  abs_bps >= 2.0

sweet_move_6_8bps：
  6.0 <= abs_bps <= 8.0
  confidence >= 0.70
  edge >= 0.04

high_confidence_high_entry：
  entry >= 0.70
  confidence >= 0.75
  edge >= 0.02
```

它还会检查外部价格是否反向，涉及 `chainlink`、`okx`、`binance`。如果外部价格和入场方向反向超过阈值，会拦截。

早期问题：

1. 原始 Aggressive Edge 的命中并不稳定，某些看似强势的 6 到 8 bps 突破会反转。
2. 价格、盘口、时间桶这些特征没有形成统一样本学习口径。
3. 早期曾出现直接按输单硬编码堵漏洞的倾向，后续被明确否定。

## V1 到 V3 的历史定位

V1 到 V3 已经被前端归入 deprecated，当前样本页重点从 V4 开始。

大致定位：

| 版本 | 定位 |
| --- | --- |
| V1 | 在原始 Aggressive Edge 上加早期学习过滤 |
| V2 | 开始影子样本记录，支持事后复盘 |
| V3 | 继续做直觉守卫和输单记忆，但仍有过拟合风险 |

后续开发中不要把 V1 到 V3 作为主要对比对象。当前横向对比口径是 V4 到 V12。

## V4 到 V12 演进总览

| 版本 | 核心目标 | 关键变化 | 当前定位 |
| --- | --- | --- | --- |
| V4 | 对反转结构做第一轮守卫 | 拦截早段 Up、m0 高入场、过度位移、弱盘口高 edge | 早期防守版 |
| V5 | 复盘 V4 后收紧 Down 风险 | Up 等 m2 以后，Down 只验证 m2/m3，并要求低入场和盘口支撑 | 过度收紧版 |
| V6 | 低风险和非极端位移 | risk_score < 0.25，abs_move < 8 bps | 过度筛选版 |
| V7 | 检查 Up 深度和 Down 赔率 | Up m2/m3 深度门槛，Down entry <= 0.68 | 样本太少，参考价值有限 |
| V8 | 放宽采样提速 | 只拦截字段缺失、极端风险、极端位移，记录学习标签 | 学习采样版 |
| V9 | 屏蔽 V8 亏损集中的 m1 | V8 基础上 block m1 | 第一轮实盘候选诊断 |
| V10 | 修 Up 反转弱点 | Up 要 abs_move >= 5.7 且 top_level_skew >= 0.20 | 有改善，但样本较少 |
| V11 | 从全样本筛出实盘候选 | 只放 m2/m3、abs_move >= 5.5、depth >= 0.35、risk <= 0.25 | 当前 REAL 使用版本 |
| V12 | 在 V11 上验证反转守卫 | abs_move < 8，Up top >= 0.20，Down top >= 0.30 | 当前最强影子诊断，未上线 REAL |

## V4 设计细节

代码入口：

```text
PaperTradingBot._aggressive_edge_v4_block_reason
```

V4 的目标是把前几轮输单中的“假突破和反转形态”转成可观察规则。核心拦截：

1. Up 在前两分钟缺少强加速时等待确认。
2. m0 入场价高于 0.70 时拦截。
3. abs_move >= 15 bps 视为过度位移。
4. momentum decay 平缓且 edge 高时拦截，防止买在动能衰退后的高位。
5. depth_skew 和 top_level_skew 都弱，同时 edge 高时拦截。

经验教训：

1. V4 有效地把“早段冲高回落”放入关注范围。
2. 它仍然偏规则化，对市场变化的泛化能力一般。
3. 后续 V5 到 V7 在 V4 基础上继续收紧，导致样本速度下降。

## V5 设计细节

代码入口：

```text
PaperTradingBot._aggressive_edge_v5_block_reason
```

注释里记录了来源：

```text
阈值来自 91 条 V4 已结算样本复盘，先验证再考虑交易化。
```

关键阈值：

```text
AGGRESSIVE_EDGE_V5_UP_MIN_BUCKET = 2
AGGRESSIVE_EDGE_V5_DOWN_ALLOWED_BUCKETS = {2, 3}
AGGRESSIVE_EDGE_V5_DOWN_MAX_ENTRY_PRICE = 0.70
AGGRESSIVE_EDGE_V5_DOWN_MIN_DEPTH_SKEW = 0.25
AGGRESSIVE_EDGE_V5_DOWN_MIN_TOP_LEVEL_SKEW = 0.25
AGGRESSIVE_EDGE_V5_DOWN_MAX_ABS_MOVE_BPS = 10.0
```

V5 变化：

1. Up 只验证 m2 以后候选。
2. Down 只验证 m2/m3。
3. Down 要求 entry < 0.70。
4. Down 要求 depth_skew 和 top_level_skew 至少 0.25。
5. Down abs_move 不能超过 10 bps。

经验教训：

1. V5 比 V4 更严，但当前样本回看表现不佳。
2. 当前统一样本池快照中 V5 已结算 219，胜率 66.67%，模拟 ROI -3.695%。
3. V5 的失败说明单纯收紧某些输单形态，会造成样本选择偏差。

## V6 设计细节

代码入口：

```text
PaperTradingBot._aggressive_edge_v6_block_reason
```

注释里记录了来源：

```text
阈值来自 58 条 V5 已结算样本复盘，先采样验证再考虑交易化。
```

关键阈值：

```text
AGGRESSIVE_EDGE_V6_MAX_RISK_SCORE = 0.25
AGGRESSIVE_EDGE_V6_MAX_ABS_MOVE_BPS = 8.0
```

V6 变化：

1. 继承 V5。
2. 只保留低风险候选。
3. 拦截 abs_move >= 8 bps 的极端位移。

经验教训：

1. V6 的思路更保守，但保守并没有自动带来正收益。
2. 当前统一样本池快照中 V6 已结算 95，胜率 68.42%，模拟 ROI -0.751%。
3. 这轮证明仅靠 `risk_score` 和位移上限，仍然无法稳定避开反转。

## V7 设计细节

代码入口：

```text
PaperTradingBot._aggressive_edge_v7_block_reason
```

注释里记录了来源：

```text
阈值来自 V6 35 条已结算放行样本复盘，目标是验证 Up 盘口支撑和 Down 赔率约束。
```

关键阈值：

```text
AGGRESSIVE_EDGE_V7_UP_M2_MIN_DEPTH_SKEW = 0.55
AGGRESSIVE_EDGE_V7_UP_M3_MIN_DEPTH_SKEW = 0.70
AGGRESSIVE_EDGE_V7_DOWN_MAX_ENTRY_PRICE = 0.68
```

V7 变化：

1. Up 只验证 m2/m3。
2. Up m2 要求 depth_skew >= 0.55。
3. Up m3 要求 depth_skew >= 0.70。
4. Down 入场价上限压到 0.68。

经验教训：

1. 当前统一样本池快照中 V7 已结算只有 18，胜率 77.78%，模拟 ROI 11.988%。
2. V7 看起来转正，但样本量太小，不能作为实盘依据。
3. V7 的问题是过度缩窄了样本，降低了学习速度。

## V8 设计细节

代码入口：

```text
PaperTradingBot._aggressive_edge_v8_learning_block_reason
PaperTradingBot._aggressive_edge_v8_learning_tags
```

V8 是一次方向修正。它的目标是扩大样本采集速度，不继续盲目收紧规则。

关键阈值：

```text
AGGRESSIVE_EDGE_V8_MAX_RISK_SCORE = 0.90
AGGRESSIVE_EDGE_V8_MAX_ABS_MOVE_BPS = 20.0
AGGRESSIVE_EDGE_V8_ALLOWED_BUCKETS = {0, 1, 2, 3, 4}
```

V8 变化：

1. 只拦截缺失核心字段、极端 risk、极端 move。
2. m0 到 m4 都采。
3. 记录学习标签，如 `weak_depth`、`weak_top`、`momentum_decay`、`thin_edge`、`wide_move`、`high_entry`。

经验教训：

1. V8 的目的不是直接盈利，是为了让输单复盘有足够样本。
2. 当前统一样本池快照中 V8 已结算 238，胜率 65.55%，模拟 ROI -4.5367%。
3. V8 证明原始候选中噪声很大，但它提供了后续 V9 到 V12 的数据基础。

## V9 设计细节

代码入口：

```text
PaperTradingBot._aggressive_edge_v9_m1_guard_block_reason
```

注释记录：

```text
m1 当前 12 单 5胜7负，先整桶排除再重新采样。
```

关键阈值：

```text
AGGRESSIVE_EDGE_V9_BLOCKED_BUCKETS = {1}
```

V9 变化：

1. 继承 V8 的学习采样基础。
2. 直接剔除 m1 时间桶。

经验教训：

1. V9 的逻辑简单，目标是验证 m1 是否长期拖累。
2. 当前统一样本池快照中 V9 已结算 160，胜率 65.625%，模拟 ROI -3.9214%。
3. 单独去掉 m1 不够，主要问题还在 Up 反转和盘口支撑。

## V10 设计细节

代码入口：

```text
PaperTradingBot._aggressive_edge_v10_up_reversal_guard_block_reason
```

关键阈值：

```text
AGGRESSIVE_EDGE_V10_UP_MIN_ABS_MOVE_BPS = 5.7
AGGRESSIVE_EDGE_V10_UP_MIN_TOP_LEVEL_SKEW = 0.20
```

V10 变化：

1. 继承 V9。
2. 只针对 Up 增加反转守卫。
3. Up 要求 abs_move >= 5.7 bps。
4. Up 要求 top_level_skew >= 0.20。

经验教训：

1. V10 明显改善了 V9。
2. 当前统一样本池快照中 V10 已结算 92，胜率 75.00%，模拟 ROI 9.0391%。
3. V10 达到实盘准入阈值，但后续 V11 从全样本里找出了更稳定结构。

## V11 设计细节

代码入口：

```text
PaperTradingBot._aggressive_edge_v11_depth_momentum_block_reason
LiveStrategyRunner._aggressive_edge_v11_block_reason
```

注释记录：

```text
V11 来自 773 条原始会下注样本的三段验证，保留 m2/m3 的强波动、深盘口、低风险候选。
```

关键阈值：

```text
AGGRESSIVE_EDGE_V11_ALLOWED_BUCKETS = {2, 3}
AGGRESSIVE_EDGE_V11_MIN_ABS_MOVE_BPS = 5.5
AGGRESSIVE_EDGE_V11_MIN_DEPTH_SKEW = 0.35
AGGRESSIVE_EDGE_V11_MAX_RISK_SCORE = 0.25
LIVE_AGGRESSIVE_EDGE_V11_UP_MIN_TOP_LEVEL_SKEW = 0.20
```

V11 变化：

1. 重新从全样本中寻找强结构，没有继续简单继承 V10。
2. 只放行 m2/m3。
3. 要求强位移，abs_move >= 5.5 bps。
4. 要求 depth_skew >= 0.35。
5. 要求 risk_score <= 0.25。
6. 实盘 V11 对 Up 额外要求 top_level_skew >= 0.20。

当前状态：

1. V11 已经有 REAL 实现。
2. 当前 live 策略是 `SINGLE_FAK_AGGRESSIVE_EDGE_V11_REAL`。
3. 当前统一样本池快照中 V11 放行 211，已结算 148，胜 117，负 31，胜率 79.0541%，模拟 ROI 17.257%。

实盘记录快照：

```text
live trades total：9
settled：9
win：6
loss：3
ROI：-1.6736%
最新 FILLED：btc-updown-5m-1780970700，2026-06-09 10:07:19 +08，Down，pnl +1.053108
之后 REJECTED：btc-updown-5m-1780971600，2026-06-09 10:22:51 +08，Up，filled_shares 0
```

重要边界：

1. V11 是当前实盘基线。
2. V11 live 样本量只有 9 笔 FILLED，不能只看 live ROI 下结论。
3. V11 影子样本表现仍然达标。

## V12 设计细节

代码入口：

```text
PaperTradingBot._aggressive_edge_v12_reversal_guard_block_reason
```

注释记录：

```text
V12 是 V11 REAL Guard 的影子验证版：保留 V11 的强样本，只验证过度位移和 Down 顶层盘口不足风险。
```

关键阈值：

```text
AGGRESSIVE_EDGE_V12_MAX_ABS_MOVE_BPS = 8.0
AGGRESSIVE_EDGE_V12_UP_MIN_TOP_LEVEL_SKEW = 0.20
AGGRESSIVE_EDGE_V12_DOWN_MIN_TOP_LEVEL_SKEW = 0.30
```

V12 变化：

1. 继承 V11。
2. 拦截 abs_move >= 8 bps 的过度位移。
3. Up 要求 top_level_skew >= 0.20。
4. Down 要求 top_level_skew >= 0.30。

当前样本表现：

```text
V12 会放行样本：125
V12 已结算样本：97
V12 未结算样本：28
V12 胜：82
V12 负：15
V12 胜率：84.5361%
V12 模拟 ROI：22.3784%
```

V12 当前是最强影子诊断版本，但还没有 REAL 策略实现。下一步如果 Lee 要推进，推荐流程：

1. 复盘 V12 的 15 笔输单，重点看过度位移边缘、Down top_level_skew、Up top_level_skew、m2/m3 分布。
2. 检查 V12 拦截掉的 V11 样本中，有多少是 V11 会赢但 V12 误杀。
3. 写 `SINGLE_FAK_AGGRESSIVE_EDGE_V12_REAL`，必须独立 live db。
4. 禁止覆盖 V11 REAL，先让 V12 REAL 作为可选项出现在实盘策略下拉。
5. 实盘开关切换前，检查 open orders、open trades、钱包、process lock。

## 当前统一样本池口径

样本页代码位置：

```text
src/polybot2other/static/app.js
```

核心常量：

```text
SAMPLE_DEFAULT_VERSION = "V12"
SAMPLE_VERSION_KEYS = ["V12", "V11", "V10", "V9", "V8", "V7", "V6", "V5", "V4"]
SAMPLE_DIAGNOSTIC_SOURCE_VARIANT_ID = "SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC"
```

当前样本页逻辑：

```text
V4 到 V12 全部读取 SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC
然后按 v4_would_trade 到 v12_would_trade 字段筛选
候选列表接口也从这个统一样本池分页读取
```

这解决了一个旧问题：

```text
独立 V12 Diagnostic 组合启动晚，样本很小。
统一样本池能公平比较 V4 到 V12 在同一批市场样本上的放行效果。
```

注意：

1. 同一份样本池不等于同一批放行样本。
2. 胜率不同的原因是各版本筛选出的子集不同。
3. 页面如果未来又显示 V12 样本为 0，要先检查是否又混到了 `SINGLE_FAK_AGGRESSIVE_EDGE_V12_DIAGNOSTIC` 自己的库。

相关接口：

```text
GET /api/aggressive-edge-sample-candidates?version=V12&limit=8&offset=0
```

当前返回示例：

```text
version：V12
limit：8
offset：8
loaded：8
total：125
total_pages：16
```

## 历史回填口径

`storage.py` 在 schema ensure 阶段会补齐 V9 到 V12 的历史字段。换电脑后第一次启动服务，或者数据库 schema 新增字段后，样本统计可能因为回填而跳变。看到样本数变化时，先确认是否刚执行过 schema ensure。

回填函数：

```text
_backfill_aggressive_edge_v9_shadow_samples()
_backfill_aggressive_edge_v10_shadow_samples()
_backfill_aggressive_edge_v11_shadow_samples()
_backfill_aggressive_edge_v12_shadow_samples()
```

V9 回填：

```text
来源：V8 历史样本
放行：v8_would_trade = 1 且 bucket 不是 m1
拦截：m1
```

V10 回填：

```text
来源：V9 历史样本
拦截 Up abs_move_bps < 5.7
拦截 Up top_level_skew 缺失或 < 0.20
Down 继承 V9 放行结果
```

V11 回填：

```text
来源：原始 Aggressive Edge 会下注历史样本
放行：bucket 在 m2/m3
放行：abs_move_bps >= 5.5
放行：depth_skew >= 0.35
放行：risk_score <= 0.25
```

V12 回填：

```text
来源：V11 历史样本
拦截 abs_move_bps >= 8.0
拦截 Up top_level_skew 缺失或 < 0.20
拦截 Down top_level_skew 缺失或 < 0.30
```

## 当前 V4 到 V12 样本快照

数据时间：2026-06-09 13:53 +08  
来源：`/api/status` 中 `SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC` 的 `diagnostic_version_summaries`

注意：这是当时快照。样本池会继续增长，未结算样本会变成已结算样本，回填也可能让历史版本字段变化。做实盘决策前必须重新查询当前值。

| 版本 | 放行样本 | 已结算 | 未结算 | 胜 | 负 | 胜率 | 模拟 ROI |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| V4 | 493 | 488 | 5 | 341 | 147 | 69.8770% | 1.5252% |
| V5 | 219 | 219 | 0 | 146 | 73 | 66.6667% | -3.6950% |
| V6 | 95 | 95 | 0 | 65 | 30 | 68.4211% | -0.7510% |
| V7 | 18 | 18 | 0 | 14 | 4 | 77.7778% | 11.9880% |
| V8 | 238 | 238 | 0 | 156 | 82 | 65.5462% | -4.5367% |
| V9 | 160 | 160 | 0 | 105 | 55 | 65.6250% | -3.9214% |
| V10 | 92 | 92 | 0 | 69 | 23 | 75.0000% | 9.0391% |
| V11 | 211 | 148 | 63 | 117 | 31 | 79.0541% | 17.2570% |
| V12 | 125 | 97 | 28 | 82 | 15 | 84.5361% | 22.3784% |

解读：

1. V12 当前影子表现最好。
2. V11 是当前实盘版本。
3. V7 虽然 ROI 高，但样本只有 18，不能作为实盘依据。
4. V8 是学习采样版，表现差符合预期，它的价值是提供足够输单样本。
5. V5/V6/V9 都说明简单防守或单点规则不足以解决反转。

## 样本页 UI 近期改动

文件：

```text
src/polybot2other/static/index.html
src/polybot2other/static/app.js
```

当前状态：

1. 样本页顶部菜单旁有“样本”页面。
2. 样本页可切换 V4 到 V12。
3. `最近当前版本放行候选` 默认每页 8 条。
4. 上一页/下一页放在候选卡片底部，样式复用 Bot 页面交易记录分页。
5. 分页已改为局部刷新，只更新候选卡片，不重绘整个样本页。
6. 当前脚本版本号：`app.js?v=20260609-sample-local-pagination`。

分页相关函数：

```text
loadSampleCandidates(options)
renderSampleRecentPanel(...)
changeSampleRecentPage(step)
```

开发注意：

1. 分页点击使用 `localOnly: true`。
2. 加载期间保留原表格行，只更新按钮和页码，减少闪动。
3. 修改 JS 后记得更新 `index.html` 的 app.js 版本号。

## Live 实盘状态和边界

当前 live 设置：

```text
enabled：true
live_strategy_id：SINGLE_FAK_AGGRESSIVE_EDGE_V11_REAL
db_path：data/live/single_fak_aggressive_edge_v11_real.sqlite3
stake_dollars：2.0
max_open_trades：3
initial_balance：15.0
wallet_balance：13.814446
open_orders_count：0
```

最近 live trades：

| id | round_id | 时间戳 | 组合 | 方向 | 状态 | entry | pnl |
| ---: | --- | ---: | --- | --- | --- | ---: | ---: |
| 9 | btc-updown-5m-1780970700 | 1780970839.0030317 | V11 REAL | Down | SETTLED | 0.64 | 1.053108 |
| 8 | btc-updown-5m-1780961100 | 1780961309.741513 | V11 REAL | Down | SETTLED | 0.72 | 0.723806 |
| 7 | btc-updown-5m-1780960200 | 1780960352.15515 | V11 REAL | Down | SETTLED | 0.64 | -2.009392 |
| 6 | btc-updown-5m-1780953000 | 1780953181.1764054 | V11 REAL | Down | SETTLED | 0.67 | 0.920097 |
| 5 | btc-updown-5m-1780946700 | 1780946828.9669693 | V11 REAL | Up | SETTLED | 0.69 | 0.838047 |
| 4 | btc-updown-5m-1780939500 | 1780939652.6729538 | V11 REAL | Up | SETTLED | 0.65 | 1.007364 |
| 3 | btc-updown-5m-1780928700 | 1780928900.485895 | V11 REAL | Up | SETTLED | 0.58 | -2.017623 |
| 2 | btc-updown-5m-1780928400 | 1780928528.3518572 | V11 REAL | Up | SETTLED | 0.58 | -2.017623 |
| 1 | btc-updown-5m-1780927800 | 1780927951.003569 | V11 REAL | Up | SETTLED | 0.61 | 1.199606 |

关键边界：

```text
btc-updown-5m-1780970700 是目前最新一笔 FILLED 实盘成交。
它之后 btc-updown-5m-1780971600 是 REJECTED，filled_shares 0。
```

不要把 REJECTED 计入实盘盈亏。

## 如何继续做 V12

推荐下一步顺序：

1. 固定快照  
   先拉当前 V12 输单和 V12 拦截样本，保存成可复盘 JSON 或 Markdown。

2. 复盘 V12 输单  
   重点看：
   ```text
   m2/m3 分布
   Up 和 Down 分布
   abs_move 是否接近 8
   top_level_skew 是否刚过门槛
   depth_skew 是否足够但顶层盘口薄
   risk_score 是否集中在 0.20 到 0.25
   entry_price 是否过高导致赔率不足
   ```

3. 查 V12 误杀  
   对比：
   ```text
   v11_would_trade = 1
   v12_would_trade = 0
   would_win = 1
   ```
   这些是 V12 拦掉但 V11 会赢的样本，决定 V12 是否过严。

4. 设计 V12 REAL  
   如果复盘通过，再新增：
   ```text
   SINGLE_FAK_AGGRESSIVE_EDGE_V12_REAL
   data/live/single_fak_aggressive_edge_v12_real.sqlite3
   ```
   不要复用 V11 REAL 库。

5. 上线前预检  
   必须检查：
   ```text
   live enabled
   current live strategy
   open live trades
   official open orders
   wallet balance
   process lock
   compliance ack
   ```

## V12 REAL 缺失清单

当前 `experiments.py` 已经有 `SIGNAL_FILTER_MODE_AGGRESSIVE_EDGE_V12_DIAGNOSTIC`。实盘侧 `live.py` 只注册到 V11 REAL，尚未注册 `SINGLE_FAK_AGGRESSIVE_EDGE_V12_REAL`。

如果后续推进 V12 REAL，最小落地清单：

1. 在 `live.py` 增加 V12 REAL 常量：
   ```text
   LIVE_AGGRESSIVE_EDGE_V12_VARIANT_ID
   LIVE_AGGRESSIVE_EDGE_V12_COMBO
   LIVE_AGGRESSIVE_EDGE_V12_ENTRY_MARKER
   LIVE_AGGRESSIVE_EDGE_V12_MAX_ABS_MOVE_BPS
   LIVE_AGGRESSIVE_EDGE_V12_UP_MIN_TOP_LEVEL_SKEW
   LIVE_AGGRESSIVE_EDGE_V12_DOWN_MIN_TOP_LEVEL_SKEW
   ```
2. 在 `LIVE_STRATEGY_OPTIONS` 增加 `SINGLE_FAK_AGGRESSIVE_EDGE_V12_REAL`。
3. 在 `_live_strategy_options_payload` 给 V12 增加 `_live_aggressive_edge_readiness(settings, "V12")`。
4. 在 `LiveStrategyRunner` 增加 V12 guard 路径，沿用 V11 的 m2/m3、abs_move、depth_skew、risk_score 基础，再叠加 V12 的极端位移和顶层盘口守卫。
5. V12 REAL 数据库使用独立路径：
   ```text
   data/live/single_fak_aggressive_edge_v12_real.sqlite3
   ```
6. 在 `static/index.html` 增加实盘策略下拉选项，在 `static/app.js` 补齐 label 和 fallback。
7. 增加测试覆盖：
   ```text
   V12 REAL 出现在 live strategy options
   V12 REAL 使用独立 live db
   V12 guard 不影响 V11 REAL
   V12 readiness 可以读取统一样本池
   切换策略时 open orders/open trades 风控仍生效
   ```
8. 首次上线流程必须 live disabled 预检通过，再由 Lee 手动确认打开实盘。

## 常用查询命令

当前 V4 到 V12 样本概览：

```bash
rtk bash -lc 'curl -sS -m 10 http://127.0.0.1:8791/api/status | jq -r ".runtime.strategy_experiments.variants[] | select(.variant_id==\"SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC\") | .aggressive_edge_v2_shadow_summary.diagnostic_version_summaries[] | [.version,.would_trade_count,.settled_count,.unsettled_count,.win_count,.loss_count,(.win_rate_pct//\"\"),(.simulated_roi_pct//\"\")] | @tsv"'
```

原始 Aggressive Edge 样本池总量：

```bash
rtk bash -lc 'curl -sS -m 10 http://127.0.0.1:8791/api/status | jq ".runtime.strategy_experiments.variants[] | select(.variant_id==\"SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC\") | .aggressive_edge_v2_shadow_summary | {total_count,settled_count,base_would_trade_settled_count,base_would_win_rate_pct}"'
```

V12 最近候选分页：

```bash
rtk bash -lc 'curl -sS -m 10 "http://127.0.0.1:8791/api/aggressive-edge-sample-candidates?version=V12&limit=8&offset=0" | jq ".meta"'
```

Live 设置：

```bash
rtk bash -lc 'curl -sS -m 10 http://127.0.0.1:8791/api/live-settings | jq "{enabled,live_strategy_id,db_path,stake_dollars,max_open_trades,initial_balance,readiness_ready:.readiness.ready,wallet_balance:.readiness.wallet.balance,open_orders_count:.open_orders.count}"'
```

Live 最近交易：

```bash
rtk bash -lc 'curl -sS -m 10 "http://127.0.0.1:8791/api/recent-trades?account_scope=live&limit=20&offset=0" | jq ".recent_trades_summary,.recent_trades[]"'
```

Live 最近订单：

```bash
rtk bash -lc 'curl -sS -m 10 "http://127.0.0.1:8791/api/orders?account_scope=live&limit=20&offset=0" | jq ".recent_orders[]"'
```

样本页 HTML 是否加载最新脚本：

```bash
rtk bash -lc 'curl -sS -m 5 http://127.0.0.1:8791/ | rg -n "app.js\\?v=20260609-sample-local-pagination"'
```

## 已知风险和不要踩的坑

1. 不要把 V12 影子胜率直接当作实盘结果。
2. 不要把独立 `SINGLE_FAK_AGGRESSIVE_EDGE_V12_DIAGNOSTIC` 的小样本拿来和统一样本池比较。
3. 不要用 REJECTED 订单计算 live 盈亏。
4. 不要复用 V11 REAL 数据库给 V12 REAL。
5. 不要只看胜率，必须同时看 ROI、entry_price、方向、时间桶和误杀样本。
6. 不要继续堆单点硬编码，Gemini 曾指出过这个问题，后续策略应该优先通过统一样本池和可复盘特征验证。
7. 不要在 open live trade 或 official open order 存在时切换实盘策略。
8. 不要回滚当前 dirty 工作区里的 V12、样本页、live 相关改动，除非 Lee 明确要求。

## 对下一位 API 会话的推荐开场提示

可以把下面这段直接作为新会话的第一条上下文：

```text
你在 /home/project/polybot2other 继续开发。先阅读 docs/aggressive-edge-v4-v12-handoff-20260609.md。当前重点是 SINGLE + FAK Aggressive Edge V4 到 V12 的样本学习和 V11 REAL/V12 Diagnostic 交接。样本页 V4 到 V12 必须统一读取 SINGLE_FAK_AGGRESSIVE_EDGE_DIAGNOSTIC，再按 v4_would_trade 到 v12_would_trade 过滤。当前 REAL 是 SINGLE_FAK_AGGRESSIVE_EDGE_V11_REAL，V12 只是影子诊断，不能直接当作已上线实盘。所有命令用 rtk，先 git status，不要回滚 dirty 改动。
```

## 推荐后续开发任务

优先级从高到低：

1. 导出 V12 输单和 V12 误杀样本复盘报告。
2. 给样本页增加 V11 对 V12 的差异对比区：
   ```text
   V11 放行且 V12 拦截
   V11 拦截且 V12 放行
   V11/V12 都放行但输赢不同
   ```
3. 做 V12 REAL 设计文档，先不写交易代码。
4. 若 Lee 确认，再新增 `SINGLE_FAK_AGGRESSIVE_EDGE_V12_REAL` 和独立 live db。
5. 给 live 切换增加更明显的前端提示：
   ```text
   当前运行 REAL 策略
   当前资金库
   最新 FILLED 实盘成交
   最新 REJECTED 尝试
   ```

## 当前一句话状态

Aggressive Edge 已经从原始激进入场，经过 V4 到 V7 的防守型过拟合尝试，转向 V8 的学习采样，再由 V9 到 V12 用统一样本池筛出更稳定的盘口强动量候选；当前实盘停在 V11 REAL，V12 影子表现更强，下一步应先复盘 V12 输单和误杀样本，再决定是否开发 V12 REAL。
