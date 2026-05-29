# polybot2other-progress

## 2026-05-29 v3.31

### 已完成

1. 重构 Dashboard 实盘卡片：配置项从卡片头部常驻展示改为右上角齿轮按钮打开的设置表单。
2. 实盘操作按钮重新分组：常规操作包含重载凭证、预检、首单检查、刷新挂单；危险操作包含执行首单、实盘急停。
3. 按钮增加 hover、active 和 loading spinner 状态；点击请求期间禁用按钮，降低重复提交风险。
4. 新增 `Live Terminal` 区域，以终端日志风格展示 readiness、预检、首单检查、one-shot、刷新挂单、急停等事件。
5. 终端日志最新记录置顶，最大高度内部滚动，不再用多块普通结果面板撑高实盘卡片。
6. 原 `live-readiness`、`live-preflight-result`、`live-doctor-result`、`live-once-result` 保留为隐藏状态源，避免大幅改动现有状态流。
7. 静态资源版本提升到 `20260529-v2-86`，避免浏览器继续使用旧 CSS/JS。

### 已确认决策

1. 本次只改前端交互和展示，不改实盘下单、预检、doctor、资金授权和风控接口。
2. 设置面板使用点击打开/点击外部或 ESC 关闭，不使用 hover 展开，避免实盘配置误触。
3. 日志采用前端本地去重和最近 80 条上限；实时 API 返回的业务结果仍以后端接口为准。

### 待办和后期优化

1. 后续可以把终端日志持久化到后端审计表，方便刷新页面后仍保留操作历史。
2. 若后续实盘组合增多，设置面板可增加组合选择和组合级配置锁定提示。

### 已知坑位

1. 当前终端日志是浏览器内存态，刷新页面后会从最新 status 重新生成，不等同于后端审计日志。
2. 本机没有 Playwright/浏览器二进制，未做自动截图回归；已通过服务返回 HTML/CSS/JS 内容做烟测。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
2. 已执行 `rtk proxy git diff --check`，空白检查通过。
3. 已烟测 `http://127.0.0.1:8791/`，确认返回 HTML 包含 `live-settings-toggle`、`live-terminal-lines` 和 v86 静态资源。
4. 已烟测 `/static/styles.css?v=20260529-v2-86` 和 `/static/app.js?v=20260529-v2-86`，确认包含 terminal 样式和 `appendLiveLog` 逻辑。

### 回滚建议

1. 如需回滚本次 UI 调整，撤销 `src/polybot2other/static/index.html`、`src/polybot2other/static/styles.css`、`src/polybot2other/static/app.js` 和本进度文档的 v3.31 改动。

## 2026-05-29 v3.22

### 已完成

1. 新增本机交互式凭证初始化工具 `polybot2other.live_env_setup` / `polybot2other-live-setup`。
2. 工具会在终端里隐藏输入 private key、可选 CLOB API credentials，写入本地 `.env.live`，并设置文件权限为 `0600`。
3. 工具保留 `.env.live` 里已有的非凭证配置，只更新 live 凭证字段。
4. 工具校验 private key、signature type、funder address、API credentials 完整性；EOA 模式会校验 funder 等于私钥 signer。
5. 工具支持 `--service-url http://127.0.0.1:8791`，写完后自动调用运行中 Dashboard 的 `/api/live-reload-credentials`，输出只包含 masked address、布尔状态和错误，不打印密钥。
6. README 和实盘 runbook 补充本机凭证初始化命令。

### 已确认决策

1. 不要求 Lee 在聊天里粘贴私钥；所有敏感输入只在本机终端完成。
2. `.env.live` 的非凭证字段继续由模板/现有文件控制，setup 工具只负责 credentials，避免误改风控或数据库路径。
3. optional API credentials 必须三项全填或全空；全空时沿用 SDK 从 private key 派生 API credentials 的模式。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_env_setup -v`，2 个 setup 测试通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_env_setup --help`，CLI 帮助可正常输出。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，77 个 live 相关测试通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，145 个核心测试通过。
7. 已执行 `rtk proxy git diff --check`，空白检查通过。
8. 已重启服务 `http://127.0.0.1:8791`，`HEAD /` 返回 200。
9. 已烟测 `live_env_setup --help` 和 `/api/live-doctor?refresh=false&include_snapshot=false`；当前仍因 credentials 缺失阻断，未提交真实订单。

### 已知坑位

1. 工具只是安全写入和重载凭证，不会自动充值 collateral/pUSD，也不会自动授权 CLOB allowance。
2. 当前真实首单仍需要 Lee 在本机终端填入真实凭证后重新跑 doctor/preflight；没有官方 order id 前目标尚未完成。

### 回滚建议

1. 如需回滚本次凭证初始化工具，删除 `src/polybot2other/live_env_setup.py`，撤销 `pyproject.toml` 的 `polybot2other-live-setup` entry point，撤销 `tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.22 改动。

## 2026-05-29 v3.21

### 已完成

1. Dashboard 实盘面板新增 `执行首单` 按钮，复用现有 `/api/live-once` 受控首单路径。
2. 按钮默认禁用，只有刷新后的 `首单检查` 显示 one-shot 可执行且没有 fatal blocker 时才解锁。
3. 点击 `执行首单` 会再次刷新 doctor；如果新 doctor 有 fatal blocker，则不会调用 `/api/live-once`。
4. 真正提交前需要在浏览器 prompt 中输入 `PLACE_REAL_ORDER`；后端仍会二次校验同一个确认短语和 `max_stake_dollars` cap。
5. one-shot 返回后，页面新增结果面板展示 `submitted/blocked`、官方订单 id、本地订单状态、阻断项、错误和审计文件路径。
6. README 和实盘 runbook 补充 Dashboard 首单按钮的保护条件和使用方式。

### 已确认决策

1. 页面按钮只作为 CLI 的同等受控入口，不绕过 doctor、确认短语、max-stake cap、后端 preflight、disable_after 和 evidence。
2. 首单执行前必须刷新 doctor，避免用户看着旧的 `READY` 状态，在市场/信号/资金门槛变化后误点。
3. Fatal blocker 存在时页面只显示阻断结果，不调用 `/api/live-once`，避免无意义地进入真实下单接口。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_once -v`，9 个 one-shot 测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，75 个 live 相关测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，143 个核心测试通过。
6. 已执行 `rtk proxy git diff --check`，空白检查通过。
7. 已重启服务 `http://127.0.0.1:8791`，`HEAD /` 返回 200。
8. 已烟测首页 HTML，确认包含 `live-once` 按钮和 `live-once-result` 结果面板。
9. 已执行 `/api/live-once` 烟测；当前缺凭证/目标价/信号时返回 HTTP 409，`live_once.blocked=true`、`submitted=false`，未提交真实订单。

### 已知坑位

1. 当前页面首单按钮只是把现有 one-shot 能力接到 Dashboard；真实首单仍需要真实 `.env.live` 凭证、真实 collateral/pUSD、CLOB allowance、无 fatal blocker 和官方 order id 证据。
2. 浏览器 prompt 只是前端保护；真正的安全边界仍是服务端 `/api/live-once` 的确认短语、preflight、risk 和 SDK 提交前复查。

### 回滚建议

1. 如需回滚本次 Dashboard 首单入口，撤销 `src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.21 改动。

## 2026-05-29 v3.20

### 已完成

1. `live_preflight` 的 `min_order_size` 阻断项新增结构化字段：`stake`、`min_order_size`、`shortfall`。
2. `live_doctor` 保留 blocked check 的结构化字段，并新增 `first_order.stake_requirement`，包含当前 stake、官方最小订单、缺口、建议 stake、是否可通过设置修改修复。
3. Dashboard `首单检查` 结果新增首单金额提示，直观看到 `stake / min`、缺口和建议值。
4. README 和实盘 runbook 补充 `min_order_size` 阻断处理方式。
5. 新增测试覆盖 `$2 stake < $5 min_order_size` 时 doctor 的 fatal blocker、next action 和推荐 settings patch。

### 已确认决策

1. 不自动把 `$2` 订单提升到 `$5`，因为这会改变 Lee 配置的真实资金风险；只明确提示并要求人工保存新的 stake。
2. one-shot 推荐命令仍使用当前真实会下单的 stake，避免命令看起来可执行但和软件账户配置不一致。
3. `min_order_size` 是官方当前市场返回的门槛，必须按市场实时校验；不能只靠固定默认值判断。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k min_order_size -v`，新增 min order 测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，75 个 live 相关测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k single_fak_real_pending_order -v`，4 个 pending live order 测试通过；相关测试夹具将 quote age 放宽到 60 秒，避免长跑全套时受机器调度影响。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，143 个核心测试通过。
7. 已执行 `rtk proxy git diff --check`，空白检查通过。
8. 已重启服务 `http://127.0.0.1:8791`，`HEAD /` 返回 200。
9. 已烟测 `/api/live-doctor?refresh=false&include_snapshot=false`，响应包含 `first_order.stake_requirement`；当前仍因缺少 credentials 阻断，未提交真实订单。

### 已知坑位

1. `min_order_size` 会随当前市场和报价状态变化；填好凭证后必须重新运行 doctor/preflight，若 `stake_requirement.meets_min_order_size=false`，需要先提高单笔金额再 one-shot。
2. 当前仍缺真实 `.env.live` 凭证、真实 collateral/pUSD、CLOB allowance 和官方 order id；目标尚未完成。

### 回滚建议

1. 如需回滚本次 min order 结构化提示，撤销 `src/polybot2other/live.py`、`src/polybot2other/live_doctor.py`、`src/polybot2other/static/app.js`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.20 改动。

## 2026-05-29 v3.19

### 已完成

1. 新增运行中实盘凭证重载能力：`reload_live_credential_env()` 只重载 live private key、signature type、funder address 和 CLOB API credential env keys。
2. 新增 `POST /api/live-reload-credentials`，Dashboard 顶部实盘面板新增 `重载凭证` 按钮。
3. 重载后会清理 `PolymarketLiveClient` 缓存的 SDK client、wallet/open-order/readiness 状态，避免服务启动后编辑 `.env.live` 但进程仍使用旧凭证。
4. README 和实盘 runbook 补充运行中修改 `.env.live` 后的重载方式。
5. 单测覆盖 env 文件凭证刷新、非凭证 env 不被运行中重载覆盖、bot 重载时清理 client cache。

### 已确认决策

1. 本次只允许运行中刷新实盘凭证类配置，数据库路径、默认风控配置、基础运行参数仍要求重启或走现有 settings API，避免运行中半切换状态。
2. 如果某个凭证来自进程环境变量而不是 env 文件，重载不会删除它；进程环境变量继续优先于 `.env.live`。
3. 重载接口不提交、不取消、不卖出任何官方订单，只刷新本进程读取凭证和 SDK 缓存。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k reload_live -v`，2 个新增重载测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k load_settings -v`，4 个配置加载测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，74 个 live 相关测试通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，142 个核心测试通过。
7. 已执行 `rtk proxy git diff --check`，空白检查通过。
8. 已重启服务 `http://127.0.0.1:8791`，`HEAD /` 返回 200。
9. 已执行 `POST /api/live-reload-credentials`，响应包含 `live_trading.credential_reload`、`.env.live` 状态和 `snapshot`，当前 `enabled=false`。
10. 已执行 `live_preflight --service-url http://127.0.0.1:8791 --no-refresh --pretty`，预检仍因开关关闭、凭证缺失和当前信号阻断，未提交真实订单。

### 已知坑位

1. 该能力只是让 Lee 填好 `.env.live` 后不用重启服务即可刷新凭证，不代表真实首单已经完成。
2. 当前仍缺真实 `.env.live` 凭证、真实 collateral/pUSD、CLOB allowance 和官方 order id；目标尚未完成。
3. 修改数据库路径、settings 路径、默认下注/风控等非凭证 env 后仍需重启，不能靠 `重载凭证` 生效。

### 回滚建议

1. 如需回滚本次运行中凭证重载，撤销 `src/polybot2other/config.py`、`src/polybot2other/live.py`、`src/polybot2other/bot.py`、`src/polybot2other/web.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.19 改动。

## 2026-05-29 v3.18

### 已完成

1. `run_live_once` 新增结构化阻断异常 `LiveOnceBlockedError`，one-shot 在提交前被阻断时不再只返回一句错误字符串。
2. 阻断响应新增 `live_once.blocked=true`、`blocked_keys`、`fatal_blocked_keys`、`waitable_blocked_keys`、`preflight`、`preflight_attempts` 和等待耗时。
3. `/api/live-once` 对 `LiveOnceBlockedError` 返回 HTTP 409，但 body 保留完整结构化阻断 payload。
4. `polybot2other.live_once` 本地 CLI 捕获同一结构化阻断 payload，终端输出可直接看到 preflight 阻断细节。
5. README 和实盘 runbook 补充 one-shot 阻断响应说明。

### 已确认决策

1. 真实首单阻断时必须能从单次响应判断下一步修什么，不能只靠 `one-shot live preflight blocked: credentials, signal` 这种字符串。
2. `fatal_blocked_keys` 用于人工修复项，`waitable_blocked_keys` 用于可等待项；这和 doctor 的首单判断口径保持一致。
3. 阻断增强只影响提交前失败路径，不改变真实下单成功路径、风控检查或官方 SDK 下单调用。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_once -v`，9 个 one-shot 测试通过；新增测试确认 CLI 阻断输出包含结构化 `live_once`。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，72 个 live 相关测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，140 个核心测试通过。
6. 已执行 `rtk proxy git diff --check`，空白检查通过。
7. 已重启服务 `http://127.0.0.1:8791`，`HEAD /` 返回 200。
8. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_once --service-url http://127.0.0.1:8791 --no-refresh --confirm-real-order --acknowledge-compliance --max-stake 2 --wait-ready-seconds 0 --wait-reconcile-seconds 0 --require-submitted --pretty`，返回 code 2，body 包含 `live_once.blocked=true`、`blocked_keys`、`fatal_blocked_keys`、`waitable_blocked_keys` 和完整 `preflight`。
9. 已执行原始 HTTP `POST /api/live-once` 烟测，HTTP 409 body 顶层包含 `error` 和 `live_once`，其中 `blocked=true` 且 `has_preflight=true`。

### 已知坑位

1. 当前仍缺真实 `.env.live` 凭证、真实 collateral/pUSD、CLOB allowance 和官方 order id；目标尚未完成。
2. 阻断 payload 只说明提交前检查状态；如果订单已提交，仍以 `live_once.evidence`、本地审计 JSON 和官方 order/trade 回查为准。

### 回滚建议

1. 如需仅回滚本次 one-shot 阻断结构化响应，撤销 `src/polybot2other/bot.py`、`src/polybot2other/live_once.py`、`src/polybot2other/web.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.18 改动。

## 2026-05-29 v3.17

### 已完成

1. `run_live_once` 在真实 one-shot 已提交或已拿到订单 id 时，自动保存一份本地审计 JSON。
2. 审计文件路径为 `data/live/audit/live-once-*.json`，响应中通过 `live_once.audit.path` 返回。
3. 审计 JSON 递归剔除 `raw`、`raw_response`、`private_key`、`secret`、`signed_order`、`signature` 等敏感或过细字段，并且不包含完整 dashboard `snapshot`。
4. 如果审计文件写入失败，`live_once.audit.saved=false` 会返回错误原因，但不会遮蔽真实订单结果。
5. README 和实盘 runbook 补充 one-shot 审计文件说明。

### 已确认决策

1. 首单后的证据不能只依赖终端输出；真实订单提交后必须尽量持久化一份本机可审计文件。
2. 审计写入是辅助证据链，不应该成为真实下单路径的硬失败点；订单结果和官方 order id 优先返回给操作者。
3. 审计文件落在 `data/` 下，沿用当前 gitignore，不进入代码仓库。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_once -v`，8 个 one-shot 测试通过；新增断言确认审计文件存在、不含 `snapshot`、不含 `raw/raw_response`。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，71 个 live 相关测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，139 个核心测试通过。
6. 已执行 `rtk proxy git diff --check`，空白检查通过。
7. 已重启服务 `http://127.0.0.1:8791`，`HEAD /` 返回 200。
8. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --service-url http://127.0.0.1:8791 --pretty`，从运行中服务读取预检成功，当前仍因开关关闭、凭证缺失和当前信号阻断。
9. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_once --service-url http://127.0.0.1:8791 --no-refresh --confirm-real-order --acknowledge-compliance --max-stake 2 --wait-ready-seconds 0 --wait-reconcile-seconds 0 --require-submitted --pretty`，返回 code 2，阻断为 `credentials, signal`，未提交真实订单。

### 已知坑位

1. 当前仍缺真实 `.env.live` 凭证、真实 collateral/pUSD、CLOB allowance 和官方 order id；目标尚未完成。
2. 审计文件只在 one-shot 结果已提交或包含订单 id 时自动生成；纯预检阻断不会产生文件，避免大量无订单噪音。

### 回滚建议

1. 如需仅回滚本次 one-shot 审计文件，撤销 `src/polybot2other/bot.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.17 改动。

## 2026-05-29 v3.16

### 已完成

1. `live_preflight` CLI 新增 `--service-url` 和 `--service-timeout`，可直接读取运行中 dashboard 服务的 `/api/live-preflight`。
2. GET `/api/live-preflight` 新增 `include_snapshot=false` 支持，最终预检默认可返回轻量 `live_preflight` 结构。
3. README 和实盘 runbook 增加 `live_preflight --service-url http://127.0.0.1:8791` 命令，明确最终 arming check 应优先使用同一服务进程。
4. 增加 `test_live_preflight_cli_can_read_running_service`，覆盖 service URL、轻量输出和 `--require-arming-ready` 退出码。

### 已确认决策

1. 真实首单前的最终预检应尽量使用 dashboard 服务进程，而不是新建 CLI 本地 Bot；这样可以复用页面正在使用的 current market、live settings 和最新行情快照。
2. `--service-url` 模式仍然只读，只读取 `/api/live-preflight`，不提交、不取消、不卖出任何订单。
3. GET `/api/live-preflight` 保持默认包含完整 snapshot，只有显式 `include_snapshot=false` 时才裁剪，避免破坏已有调试调用。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_preflight -v`，7 个 live preflight 测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，71 个 live 相关测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，139 个核心测试通过。
6. 已执行 `rtk proxy git diff --check`，空白检查通过。
7. 已重启服务 `http://127.0.0.1:8791`，`HEAD /` 返回 200。
8. 已烟测 `GET /api/live-preflight?include_snapshot=false`，响应只包含 `live_preflight`，不包含完整 `snapshot`。
9. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --service-url http://127.0.0.1:8791 --pretty`，从运行中服务读取预检成功；当前仍因开关关闭、凭证缺失和当前信号阻断。

### 已知坑位

1. 当前仍缺真实 `.env.live` 凭证、真实 collateral/pUSD、CLOB allowance 和官方 order id；目标尚未完成。
2. service 模式预检依赖 dashboard 服务正在运行；如果服务未启动或端口错误，CLI 会返回机器可读错误，不会退回本地进程以免混淆验证口径。

### 回滚建议

1. 如需仅回滚本次 service-mode preflight，撤销 `src/polybot2other/live_preflight.py`、`src/polybot2other/web.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.16 改动。

## 2026-05-29 v3.15

### 已完成

1. 对照官方 `POST /order` 和 `GET /data/order/{orderID}` 文档，补齐官方订单状态解析：`ORDER_STATUS_INVALID`、rejected、failed、error 会映射为本地 `REJECTED(已拒绝)`。
2. 保持 no-fill 分流：canceled、cancelled、expired、unmatched、零成交 terminal/done 会映射为本地 `CANCELED(已取消)`。
3. 周期对账 `_reconcile_live_orders` 的 BUY 和 SELL pending 订单都使用统一的 terminal no-fill 本地状态映射，避免真实首单后 invalid 状态一直等到本地 pending timeout。
4. README 和实盘 runbook 补充 evidence 状态解释，说明 invalid/rejected/failed/error 与 canceled/expired/unmatched 的区别。

### 已确认决策

1. 官方 `ORDER_STATUS_INVALID` 属于无成交失败状态，本地应释放 reserved cash 并标记 `REJECTED`，不应该继续显示为 `PENDING`。
2. `CANCELED` 仍只表示官方或本地确认的 no-fill 取消、过期、未匹配、pending timeout，不混用 invalid 失败。

### 验证记录

1. 已核对官方 `POST /order` 响应字段：`success`、`orderID`、`status`、`makingAmount`、`takingAmount`、`tradeIDs`、`errorMsg`。
2. 已核对官方 `GET /data/order/{orderID}` 状态枚举：`ORDER_STATUS_LIVE`、`ORDER_STATUS_INVALID`、`ORDER_STATUS_CANCELED_MARKET_RESOLVED`、`ORDER_STATUS_CANCELED`、`ORDER_STATUS_MATCHED`。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_terminal_no_fill -v`，1 个状态映射测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k pending_order_reconciles -v`，4 个 pending 对账测试通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_manual_sell_pending_order_reconciles -v`，1 个手动卖出 pending 对账测试通过。
7. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，70 个 live 相关测试通过。
8. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
9. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，138 个核心测试通过。
10. 已执行 `rtk proxy git diff --check`，空白检查通过。
11. 已重启服务 `http://127.0.0.1:8791`，`HEAD /` 返回 200。
12. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_doctor --service-url http://127.0.0.1:8791 --no-refresh --pretty`，当前仍因 credentials 缺失保持 `BLOCKED`，且 target_price/signal 仍作为 one-shot 可等待阻断输出。
13. 已执行 `/api/live-evidence?external_order_id=OFFICIAL_ORDER_ID&force=false&include_snapshot=false` 烟测，响应仍为轻量 `live_evidence`。

### 已知坑位

1. 当前仍缺真实 `.env.live` 凭证、真实 collateral/pUSD、CLOB allowance 和官方 order id；目标尚未完成。
2. 真实 CLOB 如果返回 `delayed` 或 `live`，本地仍会先保留 `PENDING(待官方确认)`，直到官方 order/trade 回查确认成交或无成交终态。

### 回滚建议

1. 如需仅回滚本次官方 invalid 状态解析，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.15 改动。

## 2026-05-29 v3.14

### 已完成

1. 对照 Polymarket 官方 CLOB V2 文档重新核对 market order、FAK、signature type、deposit wallet 和 collateral 口径。
2. README、实盘 runbook、`.env.live.example` 和 `live_once --help` 将实盘资金准备口径从泛称 USDC 修正为 Polymarket CLOB collateral/pUSD，同时保留 SDK 字段名 `user_usdc_balance` 的说明。
3. `live_doctor` 的 `collateral_wallet` 下一步动作改为提示给 funder 钱包补足 collateral/pUSD 并授权 CLOB allowance。
4. 修正 `test_live_once_waits_for_transient_preflight_blockers_before_submit` 的时序抖动：模拟 REST fallback 行情每次刷新使用当前时间戳，并把等待窗口调到 3 秒。

### 已确认决策

1. 代码仍通过官方 CLOB `balance/allowance` 检查 collateral，不在本项目内自动创建 deposit wallet 或发起链上 approval；这类资金/授权动作必须由操作者在合规钱包流程里完成。
2. `user_usdc_balance` 是 SDK 参数名，不代表实盘准备时继续按旧抵押物口径操作；文档统一写成 collateral/pUSD dollar-denominated budget。

### 验证记录

1. 已核对官方文档：market order 的 `price` 是 worst-price limit，`create_market_order` 只本地签名，`post_order` 才提交；FAK 是成交可得部分并取消剩余；新 API 用户推荐 `signature_type=3` deposit wallet。
2. 已执行本机 SDK 签名审计，确认 `py_clob_client_v2==1.0.1` 的 `ClobClient`、`MarketOrderArgs`、`post_order`、`create_market_order`、`BalanceAllowanceParams` 和 `SignatureTypeV2` 签名满足当前调用路径。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
4. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k test_live_once_waits_for_transient_preflight_blockers_before_submit -v`，单个抖动用例通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_once -v`，8 个 one-shot 测试通过。
7. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，69 个 live 相关测试通过。
8. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，136 个核心测试通过。
9. 已执行 `rtk proxy git diff --check`，空白检查通过。
10. 已重启服务 `http://127.0.0.1:8791`，`HEAD /` 返回 200。
11. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_doctor --service-url http://127.0.0.1:8791 --no-refresh --pretty`，当前仍因缺 credentials 保持 `BLOCKED`，但 SDK 兼容和 service 命令输出正常。
12. 已执行 `/api/live-evidence?external_order_id=OFFICIAL_ORDER_ID&force=false&include_snapshot=false` 烟测，响应仍为轻量 `live_evidence`。

### 已知坑位

1. 当前仍缺真实 `.env.live` 凭证、真实 collateral/pUSD、CLOB allowance 和官方 order id；目标尚未完成。
2. 如果使用 `signature_type=3` deposit wallet，fund/approve 的对象必须是 deposit wallet/funder，不是只给 owner EOA 打钱或授权。

### 回滚建议

1. 如需仅回滚本次 collateral/pUSD 文档和测试稳定性改动，撤销 `.env.live.example`、`README.md`、`docs/live-trading-runbook.md`、`src/polybot2other/live_doctor.py`、`src/polybot2other/live_once.py`、`tests/test_core.py` 和本进度文档的 v3.14 改动。

## 2026-05-29 v3.13

### 已完成

1. `live_evidence` CLI 新增 `--service-url` 和 `--service-timeout`，可从运行中的 dashboard 服务读取 `/api/live-evidence`。
2. `live_doctor --service-url` 的 `post_order_evidence` 增加 `standalone_service_cli`，首单后可直接用同一服务进程核验官方订单证据。
3. `/api/live-evidence` 默认返回轻量结果：`include_snapshot=false` 时只返回 `live_evidence`，避免把完整 dashboard 快照塞进首单后核验响应。
4. README 和实盘 runbook 改为优先展示 service 模式的 `live_evidence --service-url http://127.0.0.1:8791`。

### 已确认决策

1. 首单后核验证据优先读取同一个 dashboard 服务进程，减少 CLI 新进程和页面实时快照不一致导致的误判。
2. 证据核验链路保持只读，只读取订单、软件账本、官方 open orders、readiness 和钱包状态，不提交订单、不取消订单、不卖出持仓。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_evidence -v`，3 个 evidence 测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_doctor_cli -v`，2 个 doctor CLI 测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_manual_sell -v`，8 个手动卖出相关测试通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，69 个 live 相关测试通过。
7. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，136 个核心测试通过。
8. 已执行 `rtk proxy git diff --check`，空白检查通过。
9. 已重启服务 `http://127.0.0.1:8791`，`HEAD /` 返回 200。
10. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_doctor --service-url http://127.0.0.1:8791 --no-refresh --pretty`，输出包含 `first_order.recommended_service_cli` 和 `post_order_evidence.standalone_service_cli`。
11. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_evidence --service-url http://127.0.0.1:8791 --external-order-id OFFICIAL_ORDER_ID --cached-open-orders --pretty`，返回轻量 `live_evidence` 结果，不包含完整 `snapshot`。
12. 已执行 `rtk proxy curl -s "http://127.0.0.1:8791/api/live-evidence?external_order_id=OFFICIAL_ORDER_ID&force=false&include_snapshot=false"`，接口返回顶层仅包含 `live_evidence`。

### 已知坑位

1. 当前仍缺真实 `.env.live` 凭证、钱包 USDC、CLOB allowance 和官方 order id；目标尚未完成。
2. 本轮 `-k live` 测试第一次出现过 1 次手动卖出用例抖动，随后单测、相关组合和完整 live 组合重跑均通过；后续如果再次出现，需要单独收敛该用例的 mock 顺序。

### 回滚建议

1. 如需仅回滚 service evidence 链路，撤销 `src/polybot2other/live_evidence.py`、`src/polybot2other/live_doctor.py`、`src/polybot2other/web.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.13 改动。

## 2026-05-29 v3.12

### 已完成

1. `live_once` CLI 新增 `--service-url` 和 `--service-timeout`，可直接 POST 到运行中 dashboard 服务的 `/api/live-once`。
2. 首单推荐路径改为优先通过运行中服务提交 one-shot，复用同一进程的当前市场、live settings 和浏览器/REST 行情快照。
3. `live_doctor --service-url` 输出的 `first_order` 增加 `recommended_service_cli`，可直接复制运行中服务的一次性下单命令。
4. 修正 one-shot 阻断报错口径：`enabled=false` 是 one-shot 首单的正常前置状态，不再出现在真实阻断列表中。
5. README 和实盘 runbook 改为优先展示 `live_once --service-url http://127.0.0.1:8791`。

### 已确认决策

1. 首单最终执行推荐链路是：先运行 dashboard 服务，再用 `live_doctor --service-url` 做只读最终检查，最后用 doctor 输出的 `recommended_service_cli` 做 one-shot 真实首单。
2. `--service-url` 只是把 CLI 请求转发到本机 dashboard API；真正下单仍由服务端原有 `run_live_once`、preflight、风控、process lock 和官方 SDK 路径执行。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_once -v`，8 个 one-shot 测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_doctor_cli -v`，2 个 doctor CLI 测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，68 个 live 相关测试通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，135 个核心测试通过。
7. 已重启服务 `http://127.0.0.1:8791`，`HEAD /` 返回 200。
8. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_doctor --service-url http://127.0.0.1:8791 --no-refresh --pretty`，输出包含 `recommended_service_cli`。
9. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_once --service-url http://127.0.0.1:8791 --no-refresh --confirm-real-order --acknowledge-compliance --max-stake 2 --wait-ready-seconds 0 --wait-reconcile-seconds 0 --require-submitted --pretty`，返回 code 2，阻断为 `credentials, signal`，未提交真实订单，且不再误报 `enabled`。

### 已知坑位

1. 当前仍缺真实 `.env.live` 凭证、钱包 USDC、CLOB allowance 和官方 order id；目标尚未完成。
2. 当前 `signal` 是 waitable 瞬时阻断；填完凭证和钱包授权后，可用 `--wait-ready-seconds 180` 等待策略信号窗口。

### 回滚建议

1. 如需仅回滚 service one-shot CLI，撤销 `src/polybot2other/live_once.py`、`src/polybot2other/live_doctor.py`、`src/polybot2other/bot.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.12 改动。

## 2026-05-29 v3.11

### 已完成

1. `live_doctor` CLI 新增 `--service-url` 和 `--service-timeout`。
2. `--service-url` 模式会读取运行中 dashboard 服务的 `/api/live-doctor`，复用同一进程内的当前市场、live settings 和浏览器/REST 行情快照。
3. README 和实盘 runbook 增加基于运行中服务执行最终首单检查的命令。

### 已确认决策

1. 真实首单前的最终 doctor 推荐优先使用 `--service-url http://127.0.0.1:8791`，避免新 CLI 进程因为没有 dashboard 内存快照而误报 market unavailable。
2. `--service-url` 仍然只读，不提交订单、不取消订单、不卖出订单。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_doctor -v`，5 个 doctor 测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，67 个 live 相关测试通过。
5. 首次全量 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v` 出现 1 次 pending reconcile 用例抖动；该用例单独运行通过，相邻 pending 组合运行通过。
6. 已再次执行全量 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，134 个核心测试通过。
7. 已重启服务 `http://127.0.0.1:8791`，`HEAD /` 返回 200。
8. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_doctor --service-url http://127.0.0.1:8791 --no-refresh --require-one-shot-ready --pretty`，返回 code 2，输出来自运行中服务，包含当前市场、SDK 兼容、缺失凭证和 waitable signal 阻断。

### 已知坑位

1. 本机 `.env.live` 仍未填写真实 private key、signature type、funder address；没有真实凭证、钱包 USDC、allowance 和官方 order id 前，不能标记目标完成。
2. 真实首单前应保持 dashboard 服务运行，并用 `live_doctor --service-url http://127.0.0.1:8791 --require-one-shot-ready` 做最终只读检查。

### 回滚建议

1. 如需仅回滚服务模式 doctor，撤销 `src/polybot2other/live_doctor.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.11 改动。

## 2026-05-29 v3.10

### 已完成

1. `live_doctor` 将 SDK 包名、版本和兼容性状态提升到顶层字段：`sdk`、`sdk_version`、`sdk_status`。
2. Dashboard `首单检查` 结果区新增 SDK 状态展示，能直接看到当前 `py_clob_client_v2` 是否兼容真实下单路径。
3. README 和实盘 runbook 补充 doctor 会返回 SDK compatibility。

### 已确认决策

1. SDK 状态只展示包名、版本、兼容性和错误列表，不展示签名订单、私钥或 API credential。
2. 这次只增强首单前可验证信息，不改变真实下单、预检、one-shot 或风控逻辑。

### 验证记录

1. 已核对 Polymarket 官方文档：CLOB 使用 `py-clob-client-v2`，新 API 用户推荐 `signature_type=3` deposit wallet；market order 的 `price` 是最差成交价保护；`FAK` 是部分成交后取消剩余。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_doctor -v`，4 个 doctor 测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，66 个 live 相关测试通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，133 个核心测试通过。
7. 已重启服务 `http://127.0.0.1:8791`，烟测 `/api/live-doctor?refresh=false` 返回 `sdk_version=1.0.1`、`sdk_status.compatible=true`，当前仍因 credentials 为空保持 `BLOCKED`。
8. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_doctor --no-refresh --require-one-shot-ready --pretty`，返回 code 2，输出包含顶层 `sdk_status.compatible=true` 和缺失凭证字段。

### 已知坑位

1. 本机 `.env.live` 仍未填写真实 private key、signature type、funder address；还没有钱包 USDC、allowance 和官方 order id 证据，不能标记目标完成。
2. `--no-refresh` CLI 使用新 bot 本地快照时可能没有当前 market；真实首单前应优先用运行中服务的 `/api/live-doctor?refresh=true` 或等待服务已有实时市场快照。

### 回滚建议

1. 如需仅回滚 SDK 顶层展示，撤销 `src/polybot2other/live_doctor.py`、`src/polybot2other/static/app.js`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.10 改动。

## 2026-05-29 v3.09

### 已完成

1. `live_doctor` 增加 `credential_setup` 区块，明确列出必填凭证、可选 API 凭证、缺失字段、空字段、已加载字段、API credential 模式和 env 文件权限状态。
2. Dashboard 的 `首单检查` 结果区会直接展示缺失凭证、空凭证字段和下一步动作，避免只看到 `credentials` 阻断但不知道应该填写哪些字段。
3. README 和实盘 runbook 补充 `/api/live-doctor` 会返回凭证配置状态。

### 已确认决策

1. doctor 仍保持只读，不提交订单、不取消订单、不卖出订单。
2. 页面只展示字段名和状态，不展示 private key、API secret、passphrase、签名 payload 或 raw response。
3. 当前 `.env.live` 空模板可以保留；空字段会出现在 `empty_keys`，但不会被当成有效凭证。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_doctor -v`，4 个 doctor 测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，66 个 live 相关测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，133 个核心测试通过。

### 已知坑位

1. 本机 `.env.live` 仍未填写真实 private key、signature type、funder address；没有真实凭证、资金、allowance 和官方 order id 前，不能声明实盘首单完成。

### 回滚建议

1. 如需仅回滚本次 doctor 凭证引导，撤销 `src/polybot2other/live_doctor.py`、`src/polybot2other/static/app.js`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.09 改动。

## 2026-05-29 v3.08

### 已完成

1. env loader 调整空值语义：env 文件中的空值会记录到 `empty_keys`，但不会写入进程环境变量。
2. 空 `.env.live` 模板不会再遮蔽后续 `.env.local`、`.env` 或进程环境中的真实 live 凭证。
3. `sensitive_keys_present` 只记录非空的敏感字段，避免把未填写的模板误判为含真实密钥。
4. README 和实盘 runbook 补充空值不会加载、不会遮蔽后续文件的说明。

### 已确认决策

1. 对实盘凭证来说，空值不是有效配置；保留空模板是为了引导填写，不应该覆盖后续更具体的真实配置。
2. 权限检查只针对非空敏感字段；空模板的权限仍建议 `0o600`，但不会因为未填密钥而触发 secret file 权限阻断。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k load_settings -v`，4 个配置加载测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_readiness -v`，6 个 readiness 测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，66 个 live 相关测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，133 个核心测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
6. 已执行 `rtk proxy git diff --check`，空白检查通过。
7. 已重启服务 `http://127.0.0.1:8791`，烟测 `/api/live-doctor?refresh=false` 返回 `.env.live` 的非空运行配置在 `loaded_keys`，空凭证字段在 `empty_keys`，`sensitive_keys_present=[]`，当前仍因 credentials 为空保持 `BLOCKED`。

### 已知坑位

1. 本机 `.env.live` 仍未填写真实 private key、signature type、funder address；真实 one-shot 首单仍需等凭证、钱包 USDC、allowance 和当前策略信号全部通过。

### 回滚建议

1. 如需仅回滚空值不覆盖语义，撤销 `src/polybot2other/config.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.08 改动。

## 2026-05-29 v3.07

### 已完成

1. 本地生成 `.env.live` 空模板，并设置权限为 `0o600`；该文件已被 `.gitignore` 忽略，不会进入版本库。
2. env loader 增加密钥文件权限元数据：`mode`、`secure_permissions`、`sensitive_keys_present`。
3. live readiness 增加密钥 env 文件权限闸门：如果载入的 env 文件包含 live 私钥/API credential 字段但不是 owner-only 权限，会阻断实盘开启和真实下单。
4. README、`.env.live.example` 和实盘 runbook 补充 `chmod 600 .env.live` 要求。

### 已确认决策

1. `.env.live` 可以在本机落地空模板，但不能提交真实密钥；实盘私钥文件必须只允许当前用户读写。
2. 权限检查只暴露字段名、路径、mode 和布尔状态，不暴露 private key、API secret 或 passphrase。

### 验证记录

1. 已执行 `rtk proxy ls -l .env.live .env.live.example`，确认 `.env.live` 为 `-rw-------`。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k load_settings -v`，3 个配置加载测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_readiness -v`，5 个 readiness 测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，65 个 live 相关测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，131 个核心测试通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
7. 已重启服务 `http://127.0.0.1:8791`，烟测 `/api/live-doctor?refresh=false` 返回 `env_path=.env.live`、`env_mode=0o600`、`env_secure=true`，当前仍因 credentials 为空保持 `BLOCKED`。

### 已知坑位

1. `.env.live` 已存在但仍是空模板；必须填入真实 private key、signature type、funder address，并确保钱包 USDC 和 allowance 通过后，才能执行真实 one-shot 首单。

### 回滚建议

1. 如需仅回滚权限检查，撤销 `src/polybot2other/config.py`、`src/polybot2other/live.py`、`tests/test_core.py`、`README.md`、`.env.live.example`、`docs/live-trading-runbook.md` 和本进度文档的 v3.07 改动。
2. 如需删除本地空模板，确认没有填入真实密钥后删除 `.env.live`。

## 2026-05-29 v3.06

### 已完成

1. `live_doctor` 的首单推荐命令不再硬编码 `--max-stake 2`，改为按当前 live preflight 的实际本次 stake 动态生成。
2. 如果当前市场已有 live 持仓并锁定了旧 stake，doctor 推荐的 one-shot `max_stake_dollars` 会使用这个锁定 stake；否则使用当前配置的 `stake_dollars`。
3. README 和实盘 runbook 补充说明：首单前优先复制 doctor 输出里的命令，避免使用过期的固定下注金额。

### 已确认决策

1. one-shot 的 `max-stake` 是安全上限，必须和下一笔真实订单实际会使用的 stake 保持一致；否则会造成误阻断或误导操作者。
2. 页面仍然只展示 doctor 输出，不直接提供真实下单按钮。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_doctor -v`，4 个 doctor 测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，64 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，130 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已重启服务 `http://127.0.0.1:8791`，烟测 `GET /api/live-doctor?refresh=false` 返回 `max_stake_dollars=2.0`、推荐命令 `--max-stake 2`、`fatal_one_shot_blockers=["credentials"]`，且默认不带完整 snapshot。

### 已知坑位

1. 当前机器仍没有 `.env.live` 私钥/signature/funder 配置，也没有可验证的钱包余额/allowance，所以还不能完成官方真实 order id 验收。

### 回滚建议

1. 如需仅回滚动态首单 stake 推荐，撤销 `src/polybot2other/live_doctor.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.06 改动。

## 2026-05-29 v3.05

### 已完成

1. Dashboard 实盘面板新增只读 `首单检查` 按钮，调用 `/api/live-doctor?refresh=true`。
2. 页面新增 `live-doctor-result` 展示 doctor 状态、one-shot 是否可执行、fatal/waitable 阻断、下一步动作和推荐 one-shot 命令。
3. `/api/live-doctor` 默认小响应的设计保持不变；页面只在 doctor 结果区局部渲染，不刷新订单/交易大表，避免影响列表选择和性能。

### 已确认决策

1. 页面只增加只读检查入口，不增加直接真实下单按钮；真实首单仍走带确认短语和 max stake cap 的 one-shot CLI/API。
2. doctor 结果不自动触发 live_once，不会因为误点首单检查而提交真实订单。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
3. 已执行 `rtk proxy git diff --check`，空白检查通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_doctor -v`，3 个 doctor 测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，63 个 live 相关测试通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，129 个核心测试通过。
7. 已重启服务 `http://127.0.0.1:8791`，首页返回 `live-doctor` 按钮和 `v=20260529-v2-84` 静态资源版本。
8. 已烟测 `GET /api/live-doctor?refresh=false`，响应约 4473 bytes，返回 `live_doctor.status=BLOCKED`、`fatal_one_shot_blockers=["credentials"]`，且默认不带完整 snapshot。

### 已知坑位

1. 页面 doctor 只能展示当前阻断和命令；当前机器仍缺真实实盘凭证、钱包余额/allowance 和官方 order id 验收。

### 回滚建议

1. 如需仅回滚页面 doctor 入口，撤销 `src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css` 和本进度文档的 v3.05 改动。

## 2026-05-29 v3.04

### 已完成

1. 新增只读实盘 doctor：`polybot2other.live_doctor` / `polybot2other-live-doctor` / `GET /api/live-doctor`。
2. doctor 汇总 live settings 与 live preflight，输出 `status`、`ready_for_one_shot_now`、`can_wait_for_one_shot`、`fatal_one_shot_blockers`、`waitable_one_shot_blockers`、`next_actions`、推荐 one-shot 首单命令和首单后证据核对清单。
3. doctor 将 `enabled` 视为 one-shot 首单的正常关闭态；如果只有 `enabled` 阻断，则标记为 `READY_FOR_ONE_SHOT_NOW`，因为 one-shot 本来要求实盘开关先关闭。
4. `/api/live-doctor` 默认不返回完整 snapshot，避免首单前轮询返回过大；需要调试全量快照时显式加 `include_snapshot=true`。
5. README 和实盘 runbook 增加 doctor CLI/API 说明，作为填完 `.env.live` 后、执行真实首单前的最后只读检查。

### 已确认决策

1. doctor 只做只读聚合，不提交订单、不取消订单、不卖出订单；它可以触发与 preflight 相同的官方只读检查和签名预检，但不调用 `post_order`。
2. 首单仍推荐使用 one-shot 命令，而不是常驻打开实盘 loop；doctor 的输出用于判断 one-shot 是“现在可执行”“可等待短暂阻断后执行”还是“存在 fatal 阻断”。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_doctor -v`，3 个 doctor 测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，63 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，129 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已执行 `rtk proxy git diff --check`，空白检查通过。
7. 已执行 `rtk proxy .venv/bin/python -m pip install -e .`，刷新本地 venv console scripts。
8. 已执行 `rtk proxy .venv/bin/polybot2other-live-doctor --no-refresh --require-one-shot-ready --pretty`，当前缺凭证环境返回 code 2，`fatal_one_shot_blockers=["credentials"]`。
9. 已重启服务 `http://127.0.0.1:8791`，`HEAD /api/live-doctor` 返回 HTTP 200 且 `Content-Length=4450`，确认默认不再返回完整 snapshot。
10. 已烟测 `GET /api/live-doctor?refresh=false`，返回 `live_doctor.status=BLOCKED`、`fatal_one_shot_blockers=["credentials"]`、`sdk_status.compatible=true` 和推荐 one-shot 命令。

### 已知坑位

1. doctor 能把实盘首单阻断项和下一步动作讲清楚，但当前机器仍缺真实凭证、钱包余额/allowance 和真实 order id 验收。

### 回滚建议

1. 如需仅回滚 doctor，删除 `src/polybot2other/live_doctor.py`，撤销 `pyproject.toml`、`src/polybot2other/web.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.04 改动。

## 2026-05-29 v3.03

### 已完成

1. 实盘 readiness 增加 `sdk_version` 和 `sdk_status`，直接暴露当前 `py_clob_client_v2` 包版本、兼容性结果和具体 SDK 兼容性错误。
2. README 和实盘 runbook 补充说明：预检不只看凭证，也会检查官方 SDK 包版本和本项目真实下单路径需要的类、枚举、方法。

### 已确认决策

1. 实盘下单前必须把 SDK 兼容性作为硬门槛；如果官方 SDK 包版本或接口漂移，readiness 应该先阻断实盘开启，而不是等到真实 `post_order` 阶段失败。
2. `sdk_status` 只暴露包名、版本和兼容性错误，不暴露私钥、API secret、passphrase 或签名 payload。

### 验证记录

1. 已通过本机 SDK introspection 确认当前安装 `py_clob_client_v2==1.0.1`，`ClobClient.create_market_order`、`post_order`、`MarketOrderArgsV2`、`PartialCreateOrderOptions`、`OrderType.FAK` 等本项目实盘路径使用的签名仍存在。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --pretty --require-arming-ready`，当前返回 code 2；市场、官方目标价、geoblock、软件账户通过，仍因缺少实盘凭证和当前无策略信号阻断。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_readiness -v`，4 个 readiness 测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_sdk -v`，3 个 SDK 兼容性测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，60 个 live 相关测试通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，126 个核心测试通过。
7. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
8. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
9. 已执行 `rtk proxy git diff --check`，空白检查通过。
10. 已重启服务 `http://127.0.0.1:8791`，`GET /api/live-settings` 返回 `readiness.sdk_version=1.0.1`、`readiness.sdk_status.compatible=true`，同时继续因缺少实盘凭证保持 `enabled=false`。
11. 已烟测 `POST /api/live-once` 携带 `include_evidence=true` 和 `wait_ready_seconds=3`，当前缺凭证环境返回 HTTP 409，错误为 `enabled, credentials, signal`，未进入真实下单。

### 已知坑位

1. 当前机器仍缺真实实盘凭证和资金/allowance，因此还不能验证真实 `post_order` 返回的官方 order id。

### 回滚建议

1. 如需仅回滚 SDK 状态暴露，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.03 改动。

## 2026-05-29 v3.02

### 已完成

1. one-shot live 成功路径默认把只读证据包合并到同一份返回里，字段为 `live_once.evidence`。
2. 证据包会根据本次返回的官方订单 id 自动查询本地 live 账本、软件账户、readiness/wallet、官方 open orders、open trades、pending orders 和近期订单/交易。
3. CLI 增加 `--no-evidence`，API 增加 `include_evidence=false`，仅用于需要压缩输出时关闭自动证据包。
4. README 和实盘 runbook 补充自动证据包开关说明，明确正常首单无需再额外执行证据命令。

### 已确认决策

1. 首笔真实订单继续推荐走 one-shot；同一份输出同时保留提交结果、短轮询 reconcile 结果和脱敏证据包，便于人工核对后再决定是否开启常驻实盘。
2. 证据包仍是只读路径，不会提交、取消或卖出订单，也不会输出私钥、API secret、passphrase、签名 payload 或本地 `raw_response`。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_once -v`，7 个 one-shot 测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，60 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，126 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已执行 `rtk proxy git diff --check`，空白检查通过。
7. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_once --no-refresh --max-stake 2 --wait-ready-seconds 3 --ready-poll-seconds 0.25 --pretty`，未传确认短语时立即返回机器可读错误，不进入等待或下单。
8. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_once --confirm-real-order --acknowledge-compliance --max-stake 2 --wait-ready-seconds 3 --ready-poll-seconds 0.25 --pretty`，当前缺凭证环境立即返回 `enabled, credentials, signal` 阻断，不下单。
9. 已重启服务 `http://127.0.0.1:8791`，烟测 `POST /api/live-once` 携带 `include_evidence=true` 和 `wait_ready_seconds=3` 在当前缺凭证环境返回 HTTP 409，错误为 `enabled, credentials, signal`，未进入真实下单。

### 已知坑位

1. 当前机器仍缺真实实盘凭证和资金/allowance，自动证据包只能验证本地路径，不能替代真实 order id 验收。

### 回滚建议

1. 如需仅回滚 one-shot 自动证据包，撤销 `src/polybot2other/bot.py`、`src/polybot2other/live_once.py`、`src/polybot2other/web.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.02 改动。

## 2026-05-29 v3.01

### 已完成

1. one-shot live 增加受控等待参数：CLI `--wait-ready-seconds` / `--ready-poll-seconds`，API `wait_ready_seconds` / `ready_poll_seconds`。
2. 等待只覆盖短暂型阻断：`market`、`target_price`、`signal`、`orderbook_depth` 以及 one-shot 允许忽略的 `enabled`。
3. 凭证、风险确认、钱包余额/allowance、软件账户现金、geoblock、官方 open orders、SDK 签名等非短暂阻断会立即失败，不会在真实资金路径里无意义等待。
4. one-shot 返回增加 `preflight_attempts`、`wait_ready_seconds`、`waited_ready_seconds`，便于首单后复盘它等了多久、预检了几次。
5. README 和实盘 runbook 更新首单推荐命令，默认示例增加 `--wait-ready-seconds 180` 和 API 对应参数。

### 已确认决策

1. 首笔真实订单推荐继续使用 one-shot，而不是长期开启 live loop；等待能力只用于减少市场刚切换、官方目标价短暂未传播、临时 `NO_TRADE` 或盘口薄导致的手动重试。
2. 等待期间仍要求显式确认短语和 `max_stake` 上限；不会绕过任何实盘风控或凭证检查。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_once -v`，7 个 one-shot 测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，60 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，126 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已执行 `rtk proxy git diff --check`，空白检查通过。
7. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_once --no-refresh --max-stake 2 --wait-ready-seconds 3 --ready-poll-seconds 0.25 --pretty`，未传确认短语时立即返回机器可读错误，不进入等待或下单。
8. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_once --confirm-real-order --acknowledge-compliance --max-stake 2 --wait-ready-seconds 3 --ready-poll-seconds 0.25 --pretty`，当前缺凭证环境立即返回 credentials 阻断，不等待、不下单。
9. 已重启服务 `http://127.0.0.1:8791`，烟测 `POST /api/live-once` 携带 `wait_ready_seconds=3` 在当前缺凭证环境返回 HTTP 409，错误为 `enabled, credentials, signal`，未进入真实下单。

### 已知坑位

1. `wait_ready_seconds` 不是交易信号生成器；如果一直没有 Up/Down 信号或官方目标价，命令会超时失败。
2. 当前机器仍缺真实实盘凭证和资金/allowance，不能完成真实 order id 验收。

### 回滚建议

1. 如需仅回滚本次 one-shot 等待能力，撤销 `src/polybot2other/bot.py`、`src/polybot2other/live_once.py`、`src/polybot2other/web.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.01 改动。

## 2026-05-29 v3.00

### 已完成

1. `readiness` 增加 `credential_addresses`，展示实盘私钥推导出的 signer address、配置的 funder address、短地址摘要和两者是否匹配。
2. 对 `POLYBOT2OTHER_LIVE_SIGNATURE_TYPE=0` 增加强校验：EOA 模式下 funder 必须等于私钥 signer address，否则 readiness 阻断实盘开启和真实下单。
3. proxy wallet、Gnosis Safe、deposit wallet 模式不强制 signer/funder 相等，只展示摘要供人工核对，避免误拦截正常代理/托管资金地址。
4. 前端 live readiness 状态栏展示 signer/funder 短地址和 match 状态，便于实盘前人工确认。
5. `.env.live.example`、README 和实盘 runbook 补充签名类型和 funder 地址关系说明。

### 已确认决策

1. Polymarket 官方文档要求初始化交易客户端时提供 signature type 和 funder address；其中 EOA 类型的 funder 是 EOA 钱包地址，deposit wallet 新用户建议使用 `signature_type=3`。
2. 地址校验只暴露公开地址和短地址摘要，不暴露私钥、API secret 或 passphrase。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_readiness -v`，4 个 readiness 测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，58 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，124 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --pretty`，当前能刷新到市场，但仍因缺少实盘凭证、风险确认未勾选、官方 target_price 暂不可用和当前信号不下单而阻断。
7. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_evidence --external-order-id smoke-order --pretty`，无凭证环境返回 `credential_addresses` 空摘要且不进入下单路径。

### 已知坑位

1. 当前机器仍没有真实 `.env.live` 凭证，因此无法验证真实资金钱包余额、allowance、官方 open orders，也不能完成首单真实 order id 验收。
2. 刚刷到的当前市场官方 `target_price=0`，策略会继续阻断真实下单；需要等官方 market target 出现且策略信号为 Up/Down。

### 回滚建议

1. 如需仅回滚本次凭证地址校验，撤销 `src/polybot2other/live.py`、`src/polybot2other/static/app.js`、`.env.live.example`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v3.00 改动。

## 2026-05-29 v2.99

### 已完成

1. 新增只读实盘证据包 `LiveStrategyRunner.evidence_payload()`，集中输出 `SINGLE_FAK_REAL` 本地账本、软件账户、readiness/wallet、官方 open orders、open trades、pending orders、recent orders/trades，以及指定官方订单 id 对应的本地订单行。
2. 新增 `PaperTradingBot.live_evidence()`、`GET/HEAD /api/live-evidence` 和 CLI `polybot2other.live_evidence` / `polybot2other-live-evidence`。
3. evidence 输出复用公开化订单结构，避免暴露本地 `raw_response`、私钥、API secret 或签名 payload。
4. README 和实盘 runbook 增加首单后证据采集命令，便于核对官方订单 id、本地订单状态、官方挂单和软件隔离账户。

### 已确认决策

1. evidence 是只读核验入口，不提交订单、不卖出、不取消订单；真实资金动作仍只通过 one-shot、live loop、manual sell、emergency stop 的原有受控入口。
2. 首单验收时优先用 `external_order_id` 定位本地 live order，配合官方 open orders 和 wallet/readiness 判断本地账本与官方状态是否一致。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_evidence -v`，2 个 evidence 测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，56 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，122 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已执行 `rtk proxy git diff --check`，空白检查通过。
7. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_evidence --external-order-id smoke-order --pretty`，无凭证环境返回机器可读 evidence，不进入下单路径。
8. 已重启服务 `http://127.0.0.1:8791`，烟测 `GET /api/live-evidence?external_order_id=smoke-order&force=true` 返回 `live_evidence` 和 `snapshot`，`requested_external_order_id=smoke-order`，`raw_response` 未暴露；`HEAD /api/live-evidence` 返回 HTTP 200。

### 已知坑位

1. evidence 会读取官方 open orders 和 readiness；缺少凭证时会返回明确阻断信息，但不会尝试下单。
2. 首单如果仍是 `PENDING(待官方确认)`，evidence 只负责呈现当前状态，最终收口仍依赖 live order reconciliation。

### 回滚建议

1. 如需仅回滚本次 evidence 入口，撤销 `src/polybot2other/live.py`、`src/polybot2other/bot.py`、`src/polybot2other/web.py`、`src/polybot2other/live_evidence.py`、`pyproject.toml`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.99 改动。

## 2026-05-29 v2.98

### 已完成

1. 将 one-shot live 提交后的官方对账等待移入 `LiveStrategyRunner.run_once_from_state()` 内部。
2. one-shot 的 `--wait-reconcile-seconds` / `reconcile_wait_seconds` 现在会在 live runner 运行锁和 live process lock 仍持有时执行，等待结束后才按 `disable_after=true` 关闭 live 并释放锁。
3. 删除外层 `PaperTradingBot.run_live_once()` 提交后再调用对账的流程，避免首单 pending 对账期间进程锁已经释放。
4. 测试增加对账等待期间锁状态断言：官方 order/trade 回查时 `process_lock.locked=true` 且 `config.enabled=true`，返回后才关闭 live 并释放锁。
5. README 和实盘 runbook 更新说明：等待窗口是在 one-shot 释放 live process lock 前执行。

### 已确认决策

1. 首笔真实订单验收期间，pending 对账也属于 one-shot 临界区，应该继续持有 live process lock，避免另一个服务进程抢先启用同一 live settings path。
2. 对账等待不提交新订单，只推进已有 official order id 的本地状态收口；因此放在 runner 锁内不会改变下单策略，只提升并发安全。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_once -v`，5 个 one-shot live 测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，54 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，120 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_once --no-refresh --max-stake 2 --wait-reconcile-seconds 1 --pretty`，未传确认短语时返回机器可读错误，不进入下单路径。
7. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --no-refresh --pretty`，CLI 预检输出正常，当前环境仍因开关关闭、风险确认未勾选、缺少实盘凭证和市场不可用等阻断。
8. 已执行 `rtk proxy git diff --check`，空白检查通过。
9. 已重启服务 `http://127.0.0.1:8791`。
10. 已烟测 `/api/live-once`：未传确认短语返回 HTTP 400；传确认和 `reconcile_wait_seconds` 但当前环境阻断时返回 HTTP 409 且不下单。

### 已知坑位

1. 如果 `reconcile_wait_seconds` 设置很长，one-shot 会在持锁状态下等待更久；建议首单用 20 到 30 秒，不要无脑拉到上限。
2. 如果等待窗口结束仍是 `PENDING(待官方确认)`，仍需要启动 dashboard 服务继续常规对账。

### 回滚建议

1. 如需仅回滚本次 one-shot 锁内对账调整，撤销 `src/polybot2other/live.py`、`src/polybot2other/bot.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.98 改动。

## 2026-05-29 v2.97

### 已完成

1. one-shot live 入口增加提交后的短窗口官方对账等待：CLI 参数 `--wait-reconcile-seconds` / `--reconcile-poll-seconds`，API 参数 `reconcile_wait_seconds` / `reconcile_poll_seconds`。
2. 新增 `LiveStrategyRunner.wait_for_order_reconciliation()`，提交后可强制触发 `_reconcile_live_orders()`，并返回本地 live order 状态、open trades 摘要和等待次数。
3. 新增 `TradeStore.live_order_by_external_id()`，用于按官方 `external_order_id` 读取本地 live 订单状态，供 one-shot 输出审计证据。
4. one-shot 输出新增 `reconcile` 字段；如果官方 order/trade 在等待窗口内收口，会显示 `FILLED` / `CANCELED` / `REJECTED` 等本地状态，否则仍显示 `PENDING`。
5. README 和实盘 runbook 更新首笔实盘命令，建议首单使用 `--wait-reconcile-seconds 20` / `reconcile_wait_seconds=20`，减少拿到 order id 但本地状态未收口的人工不确定性。

### 已确认决策

1. 对账等待不绕过原有 pending-first 规则；它只是主动轮询已有 pending order，不会提交第二笔订单。
2. 等待窗口最大限制在 120 秒，轮询间隔限制在 0.1 到 10 秒，避免 API 请求长时间挂住或高频打官方接口。
3. CLI 进程退出后无法继续长期对账；如果等待窗口后仍是 `PENDING(待官方确认)`，仍应启动 dashboard 服务继续常规官方对账。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_once -v`，5 个 one-shot live 测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，54 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，120 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_once --no-refresh --max-stake 2 --wait-reconcile-seconds 1 --pretty`，未传确认短语时返回机器可读错误，不进入下单路径。
7. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --no-refresh --pretty`，CLI 预检输出正常，当前环境仍因开关关闭、风险确认未勾选、缺少实盘凭证和市场不可用等阻断。
8. 已执行 `rtk proxy git diff --check`，空白检查通过。
9. 已重启服务 `http://127.0.0.1:8791`。
10. 已烟测 `/api/live-once` 新对账参数：未传确认短语返回 HTTP 400；传确认和 `reconcile_wait_seconds` 但当前环境阻断时返回 HTTP 409 且不下单。

### 已知坑位

1. one-shot 等待窗口只能提高首单验收便利性，不能保证官方一定在窗口内返回最终成交状态。
2. 如果官方 CLOB 已收到订单但本地数据库不可写，仍会触发已有 accounting failure 停机保护；等待窗口不能替代人工按官方 order id 核对。

### 回滚建议

1. 如需仅回滚本次 one-shot 对账等待，撤销 `src/polybot2other/live.py`、`src/polybot2other/bot.py`、`src/polybot2other/live_once.py`、`src/polybot2other/web.py`、`src/polybot2other/storage.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.97 改动。

## 2026-05-29 v2.96

### 已完成

1. 新增受控首笔实盘入口 `polybot2other.live_once` / `polybot2other-live-once`，用于在不长期打开 live 循环的情况下执行一次 `SINGLE_FAK_REAL` live run。
2. 新增服务端 `POST /api/live-once`，与 CLI 共用同一后端能力，便于通过运行中的 dashboard 服务执行首笔真实订单验收。
3. one-shot 入口必须传确认短语 `PLACE_REAL_ORDER`，CLI 必须使用 `--confirm-real-order`；否则不会刷新市场，也不会触发任何真实下单路径。
4. one-shot 入口必须传 `max_stake_dollars` / `--max-stake`，实际本次 stake 超过该上限时直接拒绝，不调用官方下单。
5. one-shot 入口要求开始前普通 live switch 处于关闭状态，执行时由后端持有 live runner 运行锁，临时开启、跑一次策略，默认再关闭 live 并释放进程锁，避免后台 tick 抢先提交或后续持续自动交易。
6. README 和实盘 runbook 增加首笔实盘 one-shot CLI/API 流程、curl 示例和 `disable_after=true` 的上线建议。

### 已确认决策

1. 首笔真实资金验证优先使用 one-shot，而不是直接把 dashboard live switch 长期开启；这样可以先拿到一个官方 order id 后再决定是否进入持续运行。
2. one-shot 不绕过任何现有硬闸门：market target、信号、软件预算、钱包 balance/allowance、geoblock、官方 open orders、process lock、SDK readiness 和 pending 保护都沿用同一条 live runner。
3. one-shot 默认执行后关闭 live；只有明确传 `--leave-enabled` 或 `disable_after=false` 才允许继续保持普通 live 循环。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_once -v`，4 个 one-shot live 测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，53 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，119 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --no-refresh --pretty`，CLI 预检输出正常，当前环境 `geo_access=PASS`，但仍因开关关闭、风险确认未勾选、缺少实盘凭证和市场不可用等阻断。
7. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_once --no-refresh --max-stake 2 --pretty`，未传确认短语时返回机器可读错误 `confirm must be PLACE_REAL_ORDER`，不会进入下单路径。
8. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_once --no-refresh --confirm-real-order --max-stake 2 --pretty`，确认短语存在但没有本地市场快照时返回 `current market unavailable for one-shot live run`，不会下单。
9. 已执行 `rtk proxy git diff --check`，空白检查通过。
10. 已重启服务 `http://127.0.0.1:8791`。
11. 已烟测 `/api/status` 和 `/api/live-once`：当前缺少实盘凭证，`/api/live-once` 未传确认短语返回 HTTP 400，传确认但环境阻断时返回 HTTP 409 且不下单。

### 已知坑位

1. one-shot 是“当前时刻尝试一次”，不是等待信号机器人；如果当前没有可下单信号或市场快照不可用，会拒绝而不是挂起等待。
2. CLI 直连模式执行后进程会退出；如果订单进入 `PENDING(待官方确认)`，需要启动 dashboard 服务或再次运行现有轮询路径继续官方对账。
3. 当前环境仍缺真实凭证和真实余额，因此 one-shot 入口已验证保护逻辑，但尚未验证真实 CLOB order id。

### 回滚建议

1. 如需仅回滚本次 one-shot live 入口，撤销 `src/polybot2other/live.py`、`src/polybot2other/bot.py`、`src/polybot2other/live_once.py`、`src/polybot2other/web.py`、`pyproject.toml`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.96 改动。

## 2026-05-29 v2.95

### 已完成

1. 新增进程级实盘硬开关 `POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED`，默认 `true`；设置为 `false` 时服务不创建 live runner，实盘设置、预检、卖出和急停写操作都无法从该进程触发真实订单。
2. `.env.live.example`、README 和实盘 runbook 补充该硬开关，明确它比页面 live switch 更外层，适合 Paper-only 运行、上线前隔离和紧急维护。
3. 加严 SDK 兼容性自检：`ClobClient` 构造参数、`MarketOrderArgs` 的 `price` / `order_type` / `user_usdc_balance`，以及 `BalanceAllowanceParams.signature_type` 都纳入 readiness 检查。
4. 本机 `py_clob_client_v2` 实际签名已确认支持当前真实下单路径需要的 market order、FAK、funder、signature type、balance/allowance、order/trade/open-orders 等方法和参数。
5. 新增回归测试覆盖：环境变量关闭 live runtime 时不会创建 live runner；缺少 market-order 预算参数时 SDK 兼容性检查会阻断。

### 已确认决策

1. 页面 live switch 负责日常开关；`POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED=false` 负责进程级隔离，避免某个服务进程具备任何 live runner 能力。
2. 对真实资金路径，SDK 兼容性不能只检查类和方法名，还必须检查会影响预算和滑点边界的关键参数。
3. `user_usdc_balance` 是 BUY 单笔预算保护的重要参数；如果 SDK 不支持该参数，readiness 必须在开启实盘前失败。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k disabled_live_runtime -v`，1 个 runtime 硬关闭测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_runtime -v`，2 个 runtime 配置测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k sdk_compatibility -v`，3 个 SDK 兼容性测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，49 个 live 相关测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，115 个核心测试通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
7. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
8. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --no-refresh --pretty`，CLI 预检输出正常，当前环境 `geo_access=PASS`，但仍因开关关闭、风险确认未勾选、缺少实盘凭证和市场不可用等阻断。
9. 已执行 `rtk proxy git diff --check`，空白检查通过。
10. 已重启服务 `http://127.0.0.1:8791`。
11. 已烟测 `/api/status`、`/api/live-preflight` 和 `/api/live-settings`，当前 `geo.blocked=false`、`country=KR`、`region=11`，实盘仍为 `enabled=false`，readiness 因缺少实盘凭证而阻断。

### 已知坑位

1. 该硬开关是进程级能力隔离，不会取消其他正在运行且已启用 live runner 的服务进程；同一 live settings path 仍依赖已有进程锁阻止多进程同时交易。
2. SDK 兼容性检查只能证明本机包接口满足当前调用方式，不能替代真实私钥、真实余额/allowance、官方 open orders 为 0 和真实 CLOB order id 的验收。

### 回滚建议

1. 如需仅回滚本次 runtime 硬开关和 SDK 参数兼容性加严，撤销 `src/polybot2other/config.py`、`src/polybot2other/live.py`、`tests/test_core.py`、`.env.live.example`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.95 改动。

## 2026-05-29 v2.94

### 已完成

1. 调整 live 手动 SELL 的本地落库顺序：只要官方返回 `order_id`，先写入本地 `PENDING(待官方确认)` 平仓订单，再做官方成交金额回查和本地持仓关闭。
2. 如果官方卖出响应带 matched/filled 语义但缺少可核对金额，并且立即回查仍无法拿到官方金额，本地订单保持 `PENDING`，不再用本地估算金额直接关闭持仓。
3. 如果官方卖出已经可能提交到 CLOB，但本地后续 accounting、pending 更新或持仓关闭失败，runner 会立即保存 `enabled=false`、释放实盘进程锁，并把错误写入 critical last_error。
4. pending 手动 SELL 会阻止同一 live 持仓再次提交卖出，避免“官方已卖出、本地没记上”时重复真实卖单。
5. 新增回归测试覆盖：官方 SELL matched 但金额回查失败时保持 pending；官方 SELL 后本地 accounting 失败时关闭实盘并保留 pending 阻断重复卖出。
6. README 和实盘 runbook 补充手动卖出的 pending-first、异常停机和人工核对规则。

### 已确认决策

1. 手动 SELL 与自动 BUY 使用同一类实盘安全边界：官方 `order_id` 优先于本地最终记账结果，必须先成为本地防重复证据。
2. 对真实卖出，宁可让软件账户短时间多保留一个 pending 平仓单，也不能在缺少官方成交金额时直接按估算关闭持仓。
3. 本地 accounting 失败后的处理是关闭实盘，而不是继续运行；真实资金场景下，重复卖出或账本与官方状态分叉的风险更高。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_manual_sell -v`，8 个手动实盘卖出相关测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，46 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，112 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --no-refresh --pretty`，CLI 预检输出正常，当前环境 `geo_access=PASS`，但仍因开关关闭、风险确认未勾选、缺少实盘凭证和市场不可用等阻断。
7. 已执行 `rtk proxy git diff --check`，空白检查通过。
8. 已重启服务 `http://127.0.0.1:8791`。
9. 已烟测 `/api/live-preflight` 和 `/api/live-settings`，当前 `geo.blocked=false`、`country=KR`、`region=11`，实盘仍为 `enabled=false`，readiness 因缺少实盘凭证而阻断。

### 已知坑位

1. 如果官方 SELL 已成交但本地数据库完全不可写，程序只能关闭实盘并保留 last_error；人工仍需要用官方 order id、official trades、wallet/token balance 和持仓列表核对。
2. pending SELL 会保守阻止重复卖出；如果官方最终确认没有成交，需要等待官方回查或人工核对后再恢复实盘。

### 回滚建议

1. 如需仅回滚本次手动 SELL pending-first 和本地 accounting failure 保护，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.94 改动。

## 2026-05-29 v2.93

### 已完成

1. 调整 live BUY 成功返回官方 `order_id` 后的本地落库顺序：先写入本地 `PENDING(待官方确认)` 订单并预留本次 stake，再做官方 order/trade 金额回查和本地持仓开仓。
2. 如果官方返回 matched/filled 语义但没有 matched amounts，且立即回查失败或仍没有官方金额，本地订单保持 `PENDING`，不再用下单前 orderbook sweep 估算直接创建 live 持仓。
3. 如果官方订单可能已经提交到 CLOB，但本地后续记账、平仓或 pending 更新失败，runner 会立即保存 `enabled=false`、释放实盘进程锁，并写入 critical last_error，防止下一轮按同一信号重复真实下单。
4. pending-first 后仍保持已有对账路径：后续官方 order/trade 回查能把 pending BUY 转成 FILLED 或 CANCELED，并释放/调整软件预算。
5. 新增回归测试覆盖：matched 无金额且立即回查失败时保持 PENDING；官方 BUY 后本地 accounting 失败时自动关闭实盘且不会重复 BUY。
6. README 和实盘 runbook 补充 pending-first、matched 无金额处理和 accounting failure 后的人工核对步骤。

### 已确认决策

1. 对真实 BUY，官方 `order_id` 是本地防重复的第一优先级证据；只要拿到 `order_id`，就先写本地 PENDING，避免后续异常导致没有任何本地阻断记录。
2. matched/filled 但没有官方金额时不再信任本地盘口估算直接开仓；必须等待官方 order/trade 给出可核对金额。
3. 本地记账失败后选择关闭实盘，而不是继续运行；真实资金场景下，漏记一笔官方订单比少下一笔单更危险。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k accounting_fails -v`，1 个本地记账失败保护测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k matched_response_amount_recheck_fails -v`，1 个 matched 无金额回查失败测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k single_fak_real -v`，13 个 SINGLE_FAK_REAL 测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，44 个 live 相关测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，110 个核心测试通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
7. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
8. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --no-refresh --pretty`，CLI 预检输出正常，当前环境 `geo_access=PASS`，但仍因缺少实盘凭证和市场不可用等阻断。
9. 已执行 `rtk proxy git diff --check`，空白检查通过。
10. 已重启服务 `http://127.0.0.1:8791`。
11. 已烟测 `/api/live-preflight` 和 `/api/live-settings`，当前 `geo.blocked=false`、`country=KR`、`region=11`，实盘仍为 `enabled=false`，readiness 因缺少实盘凭证而阻断。

### 已知坑位

1. 如果官方 CLOB 已成交但本地数据库完全不可写，程序只能关闭实盘并保留 last_error；人工仍需要用官方 order id、open orders、wallet/token balance 和订单流水核对。
2. 如果 matched 无金额长时间无法回查到官方 fill，订单会保持 PENDING 直到 timeout/no-fill 逻辑收口；这会保守占用软件预算，但避免错误开仓。

### 回滚建议

1. 如需仅回滚本次 pending-first 和本地 accounting failure 保护，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.93 改动。

## 2026-05-29 v2.92

### 已完成

1. 新增 Polymarket 主站 geoblock 状态检查，来源为 `https://polymarket.com/api/geoblock`。
2. 实盘开启、`/api/live-preflight` 和真实 BUY 前最后检查都会确认 `geo_access` 通过；如果返回 `blocked=true` 或无法确认地区访问状态，会阻断实盘开启和新 BUY。
3. `readiness.geo_check` 暴露 `blocked`、`country`、`region` 和检查时间，不暴露公网 IP、私钥、API secret 或 passphrase。
4. 前端实盘状态栏增加 `geo ok/blocked` 摘要，并把风险确认文案改为“确认地区/账户合规且承担真实资金风险”。
5. SDK client 增加凭证指纹缓存保护：private key、signature type、funder、API key/secret/passphrase、host 或 chain_id 变化后，会丢弃旧 authenticated client、wallet/token/open-orders 缓存并重建，避免同一进程误用旧钱包状态。
6. README 和实盘 runbook 补充 geoblock 检查、地区合规和凭证缓存保护边界。

### 已确认决策

1. geoblock 是硬门槛，不是绕过机制；如果 Polymarket 报告当前运行地区受限，程序停止开启实盘和新 BUY。
2. geoblock 检查的是服务运行环境，不替代用户对账户、居住地、实际操作地和当地法律的合规判断。
3. geoblock 不阻止手动刷新、急停和已有订单/持仓的对账；它只阻止开启实盘和新增 live BUY。
4. 凭证指纹只在内存中用于判断是否重建 SDK client，不向前端输出。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k geoblock -v`，3 个 geoblock 阻断测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k credential -v`，4 个凭证/缺凭证测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，43 个 live 相关测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，108 个核心测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
6. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
7. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --no-refresh --pretty`，CLI 预检输出包含 `geo_access=PASS`、`country=KR`、`region=11`，当前仍因开关关闭、风险确认未勾选、缺少实盘凭证和市场不可用而阻断。
8. 已执行 `rtk proxy git diff --check`，空白检查通过。
9. 已重启服务 `http://127.0.0.1:8791`。
10. 已烟测 `/api/live-preflight`，返回 `geo_access=PASS`、`geo_check.blocked=false`、`country=KR`、`region=11`；`/api/live-settings` readiness 同步暴露 `geo_check`，当前仍因缺少实盘凭证而 `readiness_ready=false`。

### 已知坑位

1. 当前 geoblock 检查只能证明服务运行环境没有被该接口标记为 blocked，不能证明用户本人、账户主体、居住地或所有适用法律均允许交易。
2. 如果官方 geoblock 接口不可用，程序会保守阻断开启实盘和新增 BUY；这是有意设计，不应改成失败放行。
3. 当前项目仍接的是国际站 `clob.polymarket.com`；如果后续要支持 `polymarket.us`，需要单独确认 API、认证、市场、合规和订单模型，不能直接复用国际站下单链路。

### 回滚建议

1. 如需仅回滚本次 geoblock 和凭证缓存保护，撤销 `src/polybot2other/live.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.92 改动。

## 2026-05-29 v2.91

### 已完成

1. 将官方 CLOB open orders 从只读监控升级为实盘硬门槛。
2. 开启实盘时会强制读取官方 open orders；只要官方账户还有开放订单，`enabled` 会保持 `false`。
3. `/api/live-preflight` 增加 `official_open_orders_clear` 阻断项；有官方挂单时 `arming_ready=false`，并且不会执行 SDK 签名预检。
4. 真实 BUY 提交前最后阶段会强制刷新官方 open orders；如果出现官方挂单，跳过 `place_market_buy`，不提交真实订单。
5. 调整 fake live client 默认状态为官方 open orders 为空，只在专门测试中显式构造官方挂单。
6. README 和实盘 runbook 补充官方 open orders 必须为 0 的上线前置规则。

### 已确认决策

1. 官方 open orders 的硬门槛只拦截自动 live BUY 和开启实盘，不阻止手动 `刷新挂单`、急停 cancel-all 和已有 pending 订单的官方回查。
2. 普通仪表盘轮询仍使用短缓存保护性能；开启实盘、预检和真实 BUY 前置检查使用强制刷新，避免缓存把外部挂单漏掉。
3. 有官方 open orders 时先处理官方账户状态，再继续实盘；这比让本地账本和官方挂单同时存在更可控。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k open_orders -v`，6 个 open orders 相关测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，40 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，104 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --no-refresh --pretty`，CLI 预检输出正常，当前环境阻断项为实盘开关关闭、风险确认未勾选、缺少实盘凭证和当前市场不可用。
7. 已执行 `rtk proxy git diff --check`，空白检查通过。
8. 已重启服务 `http://127.0.0.1:8791`，烟测 `/api/live-preflight` 返回 `ready=false`、`arming_ready=false`，阻断项包含 `enabled`、`compliance_acknowledged`、`credentials` 和当前信号；`/api/live-open-orders` 在缺凭证环境返回 `ready=false`、`skipped=true`、`count=0`。

### 已知坑位

1. 当前环境缺真实 private key、signature type、funder 和真实账户授权，因此仍不能验证官方 CLOB 实盘账户下 open orders 为 0 时的真实下单结果。
2. 如果用户在 BUY 前最后一次 open orders 检查通过后，又在外部账户立刻创建新挂单，软件无法阻止那个外部动作；这属于同一钱包多入口操作风险，实盘时应避免并行手动交易。
3. 官方 open orders 为 0 不代表 pending 订单一定已经完成本地结算；本地 pending 仍依赖 order/trade 回查收口。

### 回滚建议

1. 如需仅回滚本次官方 open orders 硬门槛，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.91 改动。

## 2026-05-29 v2.90

### 已完成

1. 修复实盘 stake 修改的生效边界：当前 market 已有 live 持仓时，同 market 后续反向腿继续沿用该持仓的原始 stake。
2. 新配置的 `stake_dollars` 会从下一场新 market 开始生效，符合“当前有持仓时使用修改前金额直到下一场”的要求。
3. `live_preflight.software_account` 增加 `configured_stake`、`stake_source` 和 `stake_locked_to_current_market`，用于区分预算来自当前配置还是当前市场持仓锁定。
4. 前端预检结果显示 `可开启实盘` / `可真实下单`，并在 stake 被当前市场锁定时显示 `当前市场锁定`。
5. 新增回归测试覆盖：先用 5 USDC 开当前 market，再把配置改为 9 USDC，同 market 反向腿仍用 5 USDC，下一场新 market 使用 9 USDC。
6. README 和实盘 runbook 补充 stake 修改的当前市场锁定规则。

### 已确认决策

1. stake 锁定粒度是 live market/round，而不是全账户；只有同一 market 已有 live open trade 时才沿用旧 stake。
2. 如果软件账户现金不足以覆盖旧 stake，仍以当前可用 cash 为上限，避免软件预算被透支。
3. 这只控制自动 BUY 入场预算；手动 SELL 按实际持仓 shares 卖出，不受新的 `stake_dollars` 影响。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k stake_change_applies_next_market -v`，1 个 stake 生效边界测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，38 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，101 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --no-refresh --pretty`，CLI 预检输出正常。
7. 已执行 `rtk proxy git diff --check`，空白检查通过。
8. 已重启服务 `http://127.0.0.1:8791`，`/api/live-preflight` 返回 `software_account.stake_source=config`、`configured_stake=2.0`、`stake_locked_to_current_market=false`。

### 已知坑位

1. 已平仓或已结算的旧 market 不会继续锁定 stake；新 market 使用当前最新配置。
2. 如果同一 market 多个 open trade 的 stake 不一致，后续反向腿沿用最早 open trade 的 stake；当前正常路径下同 market stake 会保持一致。

### 回滚建议

1. 如需仅回滚本次 stake 生效边界修复，撤销 `src/polybot2other/live.py`、`src/polybot2other/static/app.js`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.90 改动。

## 2026-05-29 v2.89

### 已完成

1. 实盘预检结果新增 `arming_ready`、`can_enable_live` 和 `blocked_checks`。
2. `arming_ready=true` 表示除 `enabled` 开关本身外，其余真实下单前置检查均已通过，解决“先预检再打开实盘”时 `ready=false` 不够清晰的问题。
3. 新增 `PaperTradingBot.refresh_live_preflight()`，可只刷新当前市场、REST 盘口和价格，不启动交易循环、不提交订单。
4. 新增命令式预检入口 `polybot2other.live_preflight` 和脚本 `polybot2other-live-preflight`，支持 `--pretty`、`--no-refresh`、`--require-ready`、`--require-arming-ready`。
5. CLI 默认只输出 `live_preflight`，避免把完整 dashboard snapshot 打到终端；需要完整状态时可加 `--include-snapshot`。
6. README 和实盘 runbook 补充 CLI 预检、`arming_ready`、`blocked_checks` 和退出码说明。

### 已确认决策

1. CLI 预检复用现有 `PaperTradingBot` 和 `LiveStrategyRunner.preflight`，避免页面预检和命令预检口径分叉。
2. CLI 默认会做一次 REST 刷新以获取当前市场、盘口和价格；`--no-refresh` 只用于本地快照/测试。
3. `ready` 保持严格语义：只有实盘开关已经开启且所有条件通过才为 true；打开前判断用 `arming_ready`。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k "preflight" -v`，4 个预检相关测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，38 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，100 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m polybot2other.live_preflight --no-refresh --pretty`，CLI 输出精简 JSON，当前阻断项为开关关闭、风险确认未勾选、缺少实盘凭证和当前市场不可用。
7. 已执行 `rtk proxy git diff --check`，空白检查通过。
8. 已重启服务 `http://127.0.0.1:8791`，`/api/live-preflight` 返回新增字段 `ready=false`、`arming_ready=false`、`can_enable_live=false`、`can_place_next_order=false` 和 `blocked_checks`。

### 已知坑位

1. CLI 的 `--pretty` 默认刷新会访问 Polymarket/公开 BTC 价格接口；网络失败会以 JSON error 输出并返回退出码 1。
2. `arming_ready=true` 仍不等于已经下单成功；真实验收还需要打开实盘后观察 order id、订单流水和官方 CLOB 状态。

### 回滚建议

1. 如需仅回滚本次命令式预检入口，撤销 `src/polybot2other/live.py`、`src/polybot2other/bot.py`、`src/polybot2other/live_preflight.py`、`pyproject.toml`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.89 改动。

## 2026-05-29 v2.88

### 已完成

1. 新增实盘 SDK 兼容性自检：readiness 会检查 `py_clob_client_v2` 是否导出实盘链路依赖的类、枚举和关键方法。
2. 检查范围覆盖签名/下单、balance/allowance 同步和读取、order/trades 回查、open orders 和 cancel-all。
3. 兼容性失败会进入 readiness errors，阻止 `enabled=true` 保存，避免到真实下单阶段才发现 SDK 包版本不兼容。
4. 新增当前安装包兼容测试和缺失方法回归测试。
5. README 和实盘 runbook 补充 SDK 兼容性自检说明。

### 已确认决策

1. SDK 兼容性检查放在 readiness，而不是下单时临时发现；这是实盘开关的前置硬闸门。
2. 检查采用本机安装包的真实导出和方法签名作为依据，不依赖 fake client 或文档猜测。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k sdk_compatibility -v`，2 个 SDK 兼容性测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，36 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，98 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已执行 `rtk proxy git diff --check`，空白检查通过。
7. 已重启服务 `http://127.0.0.1:8791`，`/api/status` 返回 `running=true`、`enabled=false`、`sdk=py_clob_client_v2`，当前仅因缺少 `POLYBOT2OTHER_LIVE_PRIVATE_KEY` 保持未就绪。

### 已知坑位

1. 兼容性自检只能证明 SDK 表面 API 可调用，不能替代真实私钥、API credentials、余额、allowance 和 CLOB 下单验收。
2. 如果 Polymarket 服务端语义变化但 SDK 表面签名不变，仍需要真实预检和小金额实盘订单验证。

### 回滚建议

1. 如需仅回滚本次 SDK 兼容性自检，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.88 改动。

## 2026-05-29 v2.87

### 已完成

1. 新增 `LiveProcessLock`：同一个 `live-settings.json` 会对应一个 sibling `.lock` 文件，实盘开启后必须持有该锁。
2. `SINGLE_FAK_REAL` 启用阶段接入进程锁；如果另一个服务进程或同进程 runner 已经持有同一路径锁，服务端会保持 `enabled=false` 并写入阻断原因。
3. 关闭实盘和 `实盘急停` 会释放进程锁；真实 BUY 前也会再次确认锁仍被当前 runner 持有。
4. 手动 `/api/live-sell` 在实盘开关关闭时会临时获取同一进程锁，避免多服务进程同时对同一 live 持仓提交重复 SELL。
5. `/api/status`、`/api/live-settings` 和 `/api/live-preflight` 增加 `process_lock_path` / `process_lock_acquired` 信息，便于排查多进程冲突。
6. README 和实盘 runbook 补充单实例运行、锁文件和手动卖出锁保护说明。

### 已确认决策

1. 锁粒度绑定 live settings path，而不是端口或主 Paper DB；这样同一个实盘账户/实盘库只能被一个 live-enabled runner 操作。
2. 进程锁不阻塞 Paper 采样；只有开启真实交易或手动真实卖出时才参与。
3. 手动 SELL 不强制要求 `enabled=true`，但必须拿到同一实盘锁；这样关闭自动买入后仍可减仓，同时防止重复卖出。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k process_lock -v`，1 个进程锁测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，34 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，96 个核心测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法检查通过。
6. 已执行 `rtk proxy git diff --check`，空白检查通过。
7. 已重启服务 `http://127.0.0.1:8791`，`/api/status` 返回 `running=true`、`enabled=false`、`process_lock_acquired=false`、`process_lock_path=data/live/live-settings.json.lock`。

### 已知坑位

1. 这是本机文件锁，不是分布式锁；如果未来把同一实盘账户部署到多台机器，需要外部数据库锁或交易所级幂等键。
2. 如果进程被强杀，OS 会释放文件锁，但 `.lock` 文件内容可能保留旧 pid；判断是否锁住以 `process_lock_acquired` 和实际获取结果为准，不要只看文件存在。
3. 当前环境仍缺真实凭证和真实资金，不能完成真实 CLOB 下单验收。

### 回滚建议

1. 如需仅回滚本次实盘进程锁，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.87 改动。

## 2026-05-29 v2.86

### 已完成

1. 新增官方 BUY 提交前的二次开关检查：wallet/depth 检查完成后、调用 `place_market_buy` 之前，会再次确认 `enabled=true` 且风险确认仍然存在。
2. 如果用户在 live run 前置检查过程中关闭实盘，本次 run 会停止在官方下单前，不再提交真实订单。
3. 新增竞态测试：live run 卡在 wallet 检查时关闭实盘，放行后不会调用 `place_market_buy`，也不会写入 live order。
4. README 和实盘 runbook 补充下单前二次开关检查说明。

### 已确认决策

1. 二次检查放在官方 BUY 提交前最后一刻；这样不会影响正常预检，但能覆盖关闭开关与下单之间的主要竞态窗口。
2. 手动 SELL 不使用这个入场开关阻断，因为关闭风险通常需要允许用户减仓；SELL 仍有 token allowance、pending、重复提交保护。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k rechecks_enabled -v`，1 个关闭开关竞态测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，33 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。

### 已知坑位

1. 如果官方 BUY 已经进入 SDK `post_order` 调用，再关闭开关不能撤回已经提交的请求；此时应使用 `实盘急停` 和官方 open orders 核对。
2. 当前仍缺真实凭证，不能用真实 CLOB 延迟场景验证该竞态。

### 回滚建议

1. 如需仅回滚本次下单前二次开关检查，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.86 改动。

## 2026-05-29 v2.85

### 已完成

1. 新增 live runner 并发互斥：`LiveStrategyRunner.run_from_state()` 使用非阻塞运行锁，重叠进入时直接跳过第二个 live run。
2. 新增 `overlap_skip_count` 运行态计数，便于从 `/api/status` 看是否发生过重叠 live run 跳过。
3. 新增并发测试：第一条 live BUY 卡在官方下单中时，第二次 run 不会触发第二次 `place_market_buy`。
4. README 和实盘 runbook 补充重叠 tick 的处理规则。

### 已确认决策

1. 重叠 live run 选择跳过而不是等待；这样可以避免过时的同一信号排队后再次提交真实订单。
2. 互斥只保护 live runner，不阻塞 Paper 采样和页面状态读取。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k overlapping_live_run -v`，1 个并发互斥测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，33 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
4. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。

### 已知坑位

1. 如果频繁看到 `overlap_skip_count` 增加，说明 tick 周期、手动同步或官方 API 延迟已经让 live run 重叠，需要降低手动操作频率或拉长 tick interval。
2. 这个互斥是单进程内保护；如果未来部署多进程/多实例，还需要引入跨进程锁或外部幂等机制。

### 回滚建议

1. 如需仅回滚本次 live runner 并发互斥，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.85 改动。

## 2026-05-29 v2.84

### 已完成

1. 新增 pending BUY 期间的同市场入场阻断：同一市场只要存在活动中的 live buy entry order，就不会再提交另一方向或同方向的新 live buy。
2. `SINGLE_FAK_REAL` 仍保留 legacy 反向双边逻辑，但前提是当前市场没有 `PENDING(待官方确认)` 的实盘买入订单。
3. 新增测试覆盖：Up 买入进入 `PENDING` 后，市场反向产生 Down 信号，不会提交第二笔 live buy。
4. README 和实盘 runbook 补充 pending BUY 期间不允许同市场新入场的规则。

### 已确认决策

1. Paper FAK 可以立即知道成交/取消，但实盘 pending BUY 无法确认官方状态前，不应按 Paper 的反向逻辑继续下第二笔单。
2. 阻断范围限定为同一 market 的 live buy entry order，不用 pending SELL 作为 entry blocker；pending SELL 已有独立重复卖出保护。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k blocks_opposite_entry -v`，1 个 pending BUY 反向阻断测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k pending_order -v`，4 个 pending order 对账测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。

### 已知坑位

1. 如果官方 pending BUY 长时间无终态，同一市场新入场会被阻断；这是为了避免状态未知时重复暴露风险。
2. 当前环境仍缺真实凭证，无法用真实 CLOB pending buy 订单验证官方返回格式。

### 回滚建议

1. 如需仅回滚本次 pending BUY 入场阻断，撤销 `src/polybot2other/live.py`、`src/polybot2other/storage.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.84 改动。

## 2026-05-29 v2.83

### 已完成

1. 新增 pending SELL 重复提交保护：同一笔 live trade 如果已有活动中的 `FAK_SELL + PENDING` 退出订单，后端会拒绝再次 `/api/live-sell`。
2. live open trades 出参新增 `pending_live_sell_order_id`、`pending_live_sell_external_order_id` 和 `pending_live_sell_status`，用于前端识别该持仓正在等待官方卖出确认。
3. 前端持仓表在 pending SELL 期间把按钮显示为 `卖出确认中` 并禁用，避免重复点击触发重复 SELL。
4. 静态资源版本号更新到 `20260529-v2-83`，避免浏览器缓存旧按钮逻辑。
5. README 和实盘 runbook 补充 pending SELL 重复提交保护说明。

### 已确认决策

1. pending SELL 期间本地持仓仍显示 open，但不能再次卖出同一 trade；这样既不提前平仓，也不重复提交官方订单。
2. 重复卖出阻断在服务端执行，前端禁用按钮只是体验增强，不能作为唯一保护。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_manual_sell_pending_order_reconciles_to_official_fill -v`，1 个 pending SELL 对账与重复提交保护测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_manual_sell -v`，6 个手动实盘卖出测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
4. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。

### 已知坑位

1. 如果官方 pending SELL 长时间没有终态，按钮会一直显示 `卖出确认中`；此时应先用 `刷新挂单` 和订单流水确认官方状态，而不是重复点击卖出。

### 回滚建议

1. 如需仅回滚本次重复卖出保护，撤销 `src/polybot2other/live.py`、`src/polybot2other/storage.py`、`src/polybot2other/static/app.js`、`src/polybot2other/static/index.html`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.83 改动。

## 2026-05-29 v2.82

### 已完成

1. 修复手动实盘 SELL 的状态未知缺口：如果官方返回订单 ID 但没有确认成交，本地退出订单现在记录为 `PENDING(待官方确认)`，不会误记为取消。
2. `PENDING` 的 FAK_SELL 会进入 live order reconcile；回查官方 order/trades 时使用 `side=SELL`，避免按 BUY 方向解析成交金额。
3. 官方回查确认 SELL 成交后，才调用本地 `close_trade_shares` 平仓；确认无成交时才把退出订单改为 `CANCELED(已取消)`。
4. 新增 `fill_external_pending_exit_order`，用于把 pending live exit order 与本地持仓平仓动作绑定。
5. README 和实盘 runbook 补充手动卖出 pending 对账规则。

### 已确认决策

1. 手动 SELL 状态未知时，本地持仓继续保持 open；这是保守处理，避免官方可能已成交但本地误判导致账本提前变化。
2. 只有官方成交金额明确时才平仓；缺成交金额时继续等待后续回查。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_manual_sell -v`，6 个手动实盘卖出测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，32 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
4. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。

### 已知坑位

1. 如果官方长时间无法确认 SELL 状态，本地持仓会继续显示 open，并保留 PENDING 退出订单；这会更保守，但可能短时间内看起来像“卖出按钮没平仓”。
2. 当前环境缺真实凭证，尚不能用真实 CLOB 订单验证 pending SELL 的官方回查返回格式。

### 回滚建议

1. 如需仅回滚本次 SELL pending 对账，撤销 `src/polybot2other/live.py`、`src/polybot2other/storage.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.82 改动。

## 2026-05-29 v2.81

### 已完成

1. 新增实盘启动重启保护：如果 `data/live/live-settings.json` 上次保存了 `enabled=true`，新进程启动时会立刻保存回 `enabled=false`。
2. 实盘运行态新增 `startup_rearmed` 标记，`last_error` 会显示 `服务启动后实盘开关已自动关闭，需要人工重新预检并开启`。
3. 补充测试覆盖“保存 enabled=true 后重启不会自动恢复真实下单”。
4. README 和实盘 runbook 补充启动重启保护说明。

### 已确认决策

1. 实盘开关不跨进程自动延续；每次服务启动后都必须人工重新预检并打开开关。
2. 保留其他 live 配置，例如初始金额、单笔金额、风控阈值和风险确认，只重置 `enabled`，避免用户配置丢失。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k startup_rearms -v`，1 个启动重启保护测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，31 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。

### 已知坑位

1. 这个保护会让自动重启后的实盘保持关闭，需要人工重新打开；这是刻意的安全取舍，不适合无人值守恢复交易。
2. 当前环境仍缺真实凭证，因此不能验证重启后再手动启用的真实 CLOB 下单链路。

### 回滚建议

1. 如需仅回滚本次启动重启保护，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.81 改动。

## 2026-05-29 v2.80

### 已完成

1. 新增官方 open orders 只读状态闭环：实盘快照、实盘设置和前端实盘面板都会暴露官方 CLOB 当前 open orders 数量。
2. 新增 `/api/live-open-orders` GET/HEAD 接口，GET 会强制刷新官方 `get_open_orders`；普通仪表盘轮询使用短缓存，避免频繁打官方 API。
3. 缺少实盘凭证时，open orders 查询返回 `skipped=true` 和明确阻断原因，不再每轮状态刷新都尝试官方认证读取。
4. 前端实盘面板新增 `刷新挂单` 按钮，用于手动核对官方 CLOB 仍有多少开放挂单。
5. README 和实盘 runbook 补充 open orders 接口、按钮和上线后核对步骤。
6. 静态资源版本号更新为 `20260529-v2-80`，避免浏览器缓存旧 `app.js`。

### 已确认决策

1. 当时官方 open orders 先作为监控/核对能力接入；从 v2.91 起已升级为实盘开启、预检和真实 BUY 前的硬门槛。
2. 普通轮询只用短缓存，手动按钮和接口强制刷新；这样兼顾上线可见性和性能。
3. 缺凭证时直接跳过官方读取；否则用户还没有进入实盘准备阶段，频繁认证失败没有业务价值。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k open_orders -v`，3 个 open orders 测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，30 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，90 个测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
6. 已执行 `rtk proxy git diff --check`，未发现空白错误。
7. 已重启本地服务 `http://127.0.0.1:8791`，`GET /api/live-open-orders` 在当前缺凭证环境返回 `ready=false`、`skipped=true`、`count=0` 和明确缺失凭证错误；`GET /api/live-settings` 同步暴露 `open_orders.skipped=true`。
8. 已请求首页 HTML，确认包含 `live-open-orders-refresh` 和 `20260529-v2-80` 静态资源版本。

### 已知坑位

1. 当前环境缺真实 private key、signature type 和 funder，因此只能验证 open orders 的缺凭证阻断和页面/API 出参，不能验证官方 CLOB 在真实账户下返回的 open orders 内容。
2. open orders 只说明官方仍有开放订单，不等于本地账本已完成结算；本地 `PENDING(待官方确认)` 仍要依赖 order/trade 回查或 pending timeout 收口。

### 回滚建议

1. 如需仅回滚本次官方挂单可见性，撤销 `src/polybot2other/live.py`、`src/polybot2other/bot.py`、`src/polybot2other/web.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.80 改动。

## 2026-05-29 v2.79

### 已完成

1. 审计本地 `py_clob_client_v2`，确认 SDK 提供 `get_open_orders`、`cancel_order`、`cancel_orders`、`cancel_all` 和 `cancel_market_orders`。
2. 新增实盘急停能力：`LiveStrategyRunner.emergency_stop()` 会先保存 `enabled=false`，再通过官方 SDK 调用 `cancel_all` 请求取消 CLOB 全部挂单。
3. 新增 `/api/live-emergency-stop` POST 接口，返回急停前后 open orders 采样、官方 cancel response、错误和重试信息。
4. 前端实盘面板新增 `实盘急停` 按钮，点击后关闭实盘并请求官方 cancel-all。
5. 急停不会直接把本地 `PENDING(待官方确认)` 改成 canceled；这些订单继续通过官方 order/trade 回查或 FAK pending timeout 释放预算，避免可能已成交订单被本地误释放。
6. README 和实盘 runbook 补充急停接口和命令行调用方式。

### 已确认决策

1. 实盘急停的第一步必须是保存 `enabled=false`，确保后续 tick 不再提交新订单。
2. 官方 cancel-all 和本地 PENDING 对账拆开处理；cancel-all 是控制风险，PENDING 对账是资金账本一致性，不能混为一条本地状态更新。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k emergency_stop -v`，1 个实盘急停测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，27 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，87 个测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
6. 已执行 `rtk proxy git diff --check`，未发现空白错误。
7. 已重启本地服务 `http://127.0.0.1:8791`，并执行 `POST /api/live-emergency-stop` 烟测；当前无真实 private key，接口返回 `enabled=false`，官方 cancel-all 被认证缺失明确阻断。

### 已知坑位

1. 当前环境缺真实凭证，因此接口烟测只能验证服务正确阻断，不能验证官方 CLOB cancel-all 的真实响应。
2. FAK 理论上应立即成交或取消；如果出现 `POST_STATUS_UNKNOWN` 或本地 PENDING，急停后仍需等待官方回查或本地 pending timeout 完成账本收口。

### 回滚建议

1. 如需仅回滚本次实盘急停能力，撤销 `src/polybot2other/live.py`、`src/polybot2other/bot.py`、`src/polybot2other/web.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.79 改动。

## 2026-05-29 v2.78

### 已完成

1. 审计当前 8791 运行态实盘预检阻断点：当前市场已经拿到官方 `target_price=73205.342`，此前 target 缺失属于市场切换时详情尚未补齐的瞬时状态。
2. 审计已安装 `py_clob_client_v2` 的 `MarketOrderArgsV2`：BUY 订单的 `amount` 是买入 USDC 金额，`user_usdc_balance` 会被 SDK 用于费用边界调整。
3. 实盘 BUY 下单和签名预检现在都会把本次 stake 传给 SDK `user_usdc_balance`，让 SDK 在费用边界下按本次软件预算缩小签名订单金额，而不是签出可能超过单笔预算的订单。
4. 签名预检返回的输入摘要新增 `user_usdc_balance`，用于确认当前预检和真实下单都带着单笔预算上限。
5. README 和实盘 runbook 补充说明：`user_usdc_balance` 是 SDK 费用调整边界，不是链上硬限制。

### 已确认决策

1. `user_usdc_balance` 使用本次 stake，而不使用钱包真实余额；这样更符合 Lee 的“按配置初始金额/单笔金额约束，而不是按钱包全部余额下注”的要求。
2. SELL 订单不传 `user_usdc_balance`，因为 SDK 文档和源码中该字段用于 BUY 费用调整。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k sign_market_order -v`，1 个签名预检测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live -v`，26 个 live 相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，86 个测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
6. 已执行 `rtk proxy git diff --check`，未发现空白错误。
7. 已重启本地服务 `http://127.0.0.1:8791`，`/api/live-settings` 返回 stake `2.0`，仍因缺少真实 private key、signature type、funder 而保持 `readiness.ready=false`；`/api/live-preflight` 当前拿到官方 target，但因开关、风险确认、凭证和当前策略信号阻断，未进入签名预检。

### 已知坑位

1. `user_usdc_balance` 只能影响 SDK 构造订单的金额边界；真实钱包仍必须使用低余额隔离钱包，防止外部手动操作或其他程序绕过本 bot 的软件预算。
2. 当前环境仍没有真实凭证和 allowance，不能实际验证 SDK 费用调整后的真实 CLOB 成交。

### 回滚建议

1. 如需仅回滚本次 SDK 预算边界增强，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.78 改动。

## 2026-05-29 v2.77

### 已完成

1. 实盘官方 API 的配置化快速重试范围从 create/post 下单扩展到 collateral balance/allowance 同步、conditional token balance/allowance 同步、官方 order 回查和官方 trades 回查。
2. `readiness`、实盘预检、启用硬闸门、真实开仓前 wallet 检查、手动卖出前 token 检查、成交金额后验和 PENDING 订单轮询都使用当前 live 配置里的 `retry_count` / `retry_delay_ms`。
3. 重试只覆盖 timeout/network/429/5xx 等暂时性失败；非重试类官方错误仍快速失败，避免把参数错误、权限错误或余额不足伪装成可恢复问题。
4. 官方回查成功但经历过重试时，会在 raw response 里记录 `order_retry_reasons`、`trades_retry_reasons`、`sync_retry_reasons` 或 `read_retry_reasons`，方便后续排查真实 API 抖动。
5. README 和实盘 runbook 补充重试配置的覆盖范围。

### 已确认决策

1. 重试次数继续沿用 UI/配置中的 live 参数，不额外引入另一套隐藏配置。
2. 下单 post 阶段仍只重发同一份签名订单；读/同步类 API 可以重新调用，因为不会创建重复真实订单。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k retry`，3 个重试相关测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m py_compile src/polybot2other/live.py tests/test_core.py`，语法检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -v`，86 个测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
6. 已执行 `rtk proxy git diff --check`，未发现空白错误。
7. 已重启本地服务 `http://127.0.0.1:8791`，`/api/live-settings` 返回当前 live retry 配置 `retry_count=2`、`retry_delay_ms=250`；因未配置真实 private key、signature type、funder，readiness 正确保持 `false`。

### 已知坑位

1. 重试不能解决永久性配置错误，例如错误 signature type、错误 funder、余额不足、allowance 不足、token_id 失效或市场不可交易。
2. 当前运行环境仍没有真实凭证和资金，不能验证真实官方 API 抖动下的实际成交闭环。

### 回滚建议

1. 如需仅回滚本次读/同步类 API 重试扩展，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.77 改动。

## 2026-05-29 v2.76

### 已完成

1. 实盘预检新增 `sign_market_order` 检查：当开关、风险确认、凭证、市场、目标价、策略信号、软件资金、策略风控、最小订单、盘口深度和 collateral wallet 都通过后，调用官方 SDK 构造并签名当前 FAK 订单参数。
2. 签名预检只执行 `create_market_order`，不调用 `post_order`，不会提交到 CLOB，也不会把可提交的签名订单 payload 返回给前端。
3. 实盘买入和签名预检共用 `_market_order_args`，确保 token、amount、side、price、tick_size、neg_risk 和 FAK order type 与真实下单路径一致。
4. 预检返回 `signing.submitted_to_clob=false`、`status=SIGNED`、可审计的 `signed_order_hash` 和输入摘要，用于开实盘前发现签名类型、funder、token、tick size、negative risk 等 SDK 参数问题。
5. README 和实盘 runbook 补充签名预检说明，避免把预检误解为真实下单。

### 已确认决策

1. 签名预检只在前置条件全部满足时执行；如果凭证或钱包未就绪，不额外触发 SDK 签名，避免干扰主阻断原因。
2. 不返回签名订单原文，因为签名后的订单 payload 具备被提交风险；页面只展示非敏感审计摘要。
3. 签名预检通过不等于真实成交，只代表 SDK 可以按当前快照构造并签名 FAK 参数；真实 tick 下单前仍会重新检查行情、钱包和风控。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_preflight`，2 个预检测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k sign_market_order`，1 个签名预检测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py`，84 个测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
6. 已执行 `rtk proxy git diff --check`，未发现空白错误。
7. 已重启本地服务 `http://127.0.0.1:8791`，`/api/live-settings` 返回 `enabled=false`、`readiness.ready=false`，明确阻断缺少 private key、signature type、funder。
8. 已执行 `/api/live-preflight` 烟测；当前因开关、风险确认、凭证和信号未满足而 `can_place_next_order=false`，未进入签名预检，符合无真实凭证环境的预期。

### 已知坑位

1. 当前运行环境仍缺真实私钥、signature type、funder、钱包资金和 allowance，因此接口烟测会在凭证阶段阻断，不会进入真实 SDK 签名。
2. 签名预检本身不会检测 CLOB 最终撮合结果；post 阶段网络异常和撮合状态仍按 PENDING/官方回查链路处理。

### 回滚建议

1. 如需仅回滚本次签名预检，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.76 改动。

## 2026-05-29 v2.75

### 已完成

1. `load_settings()` 的本地 env 文件加载结果 now 暴露为 `env_file_status()`，包含加载文件路径、加载的 `POLYBOT2OTHER_*` 键、被现有环境变量跳过的键和被忽略的非项目前缀数量。
2. live readiness 新增 `env_files` 和 `credential_presence`，用于确认服务是否实际读取 `.env.live`，以及 private key、signature type、funder、API creds 是否存在。
3. 前端实盘 readiness 区域显示凭证存在状态和 env 文件路径，但不显示任何私钥或 secret 值。
4. README 和实盘 runbook 补充 readiness 的 env 文件/凭证存在状态说明。
5. 新增测试确认 readiness 不泄露私钥值，并确认 env 文件状态记录 loaded/skipped/ignored 信息。

### 已确认决策

1. readiness 只暴露布尔存在状态，不暴露任何密钥、secret、passphrase、私钥或 funder 以外的敏感值。
2. env 文件路径和键名用于排障，密钥内容仍只存在本机环境变量。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k settings`，2 个设置相关测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k readiness`，3 个 readiness 相关测试通过。

### 已知坑位

1. 修改 `.env.live` 后仍需重启服务进程，readiness 会显示当前进程实际加载到的状态。

### 回滚建议

1. 如需仅回滚本次 readiness/env 可见性增强，撤销 `src/polybot2other/config.py`、`src/polybot2other/live.py`、`src/polybot2other/static/app.js`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.75 改动。

## 2026-05-29 v2.74

### 已完成

1. `load_settings()` 增加本地 env 文件自动加载，默认按 `.env.live`、`.env.local`、`.env` 顺序读取 `POLYBOT2OTHER_*` 键。
2. 新增 `POLYBOT2OTHER_ENV_FILE` 支持，可显式指定本机 env 文件路径。
3. env 文件加载不会覆盖进程里已有环境变量，也不会加载非 `POLYBOT2OTHER_` 前缀的键，降低密钥误污染和误覆盖风险。
4. README 和实盘 runbook 更新启动方式：不再需要手动 `set -a; . ./.env.live`。
5. 新增测试覆盖 `.env.live` 自动加载、显式 env 文件加载、进程环境变量优先级和非项目前缀忽略。

### 已确认决策

1. 继续允许真实私钥只存在本机 `.env.live` 或真实环境变量中；`.env.live` 已在 `.gitignore` 中忽略。
2. 现有环境变量优先于 env 文件，便于系统服务、容器或临时命令覆盖本地文件。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k load_settings`，2 个配置加载测试通过。

### 已知坑位

1. `.env.live` 自动加载只发生在服务进程当前工作目录；如果从其他目录启动，需要设置 `POLYBOT2OTHER_ENV_FILE` 指向绝对路径。

### 回滚建议

1. 如需仅回滚本次 env 自动加载，撤销 `src/polybot2other/config.py`、`tests/test_core.py`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.74 改动。

## 2026-05-29 v2.73

### 已完成

1. 实盘 post_order 阶段在提交前尝试从 SDK builder 推导签名订单 hash，写入 raw response 作为 `signed_order_hash`。
2. 如果 post_order 最终失败且最后错误属于网络/超时/5xx 等可重试类别，并且已经有签名订单 hash，则返回 `POST_STATUS_UNKNOWN`，把该 hash 作为 `external_order_id` 进入 PENDING 对账路径。
3. 这样真实请求“可能已经到达 CLOB 但本地没有收到响应”时，不会立即按 REJECTED 释放软件预算，也不会创建本地持仓；后续通过官方 order/trades 回查修正。
4. FAK PENDING 增加 120 秒本地最大等待；超过等待时间仍没有官方成交/无成交证据时，转为 `CANCELED` 并释放软件预算。
5. `POST_STATUS_UNKNOWN` 后如果立即回查遇到 `RECONCILE_ERROR`，保留原 PENDING 状态，不用一次回查失败覆盖成 REJECTED。
6. 新增测试覆盖同一签名订单连续超时后保留 signed order hash 并返回 `POST_STATUS_UNKNOWN`，以及 PENDING 超时释放软件预算。

### 已确认决策

1. 对提交状态未知的真实订单，优先保守记为 PENDING，而不是 REJECTED。
2. 只有可重试/不确定类异常才进入 `POST_STATUS_UNKNOWN`；非重试类错误仍按拒单处理。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k retry`，3 个 retry 相关测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live`，21 个 live 相关测试通过。

### 已知坑位

1. signed order hash 是否等同官方 get_order 可查 id 依赖 Polymarket CLOB 当前实现；已按 SDK builder 生成的 EIP712 order hash 处理。
2. 120 秒超时后如果官方后续才出现成交，本地不会自动恢复该订单；因为 FAK 应立即成交/取消，这里选择优先释放小资金实盘的软件预算。

### 回滚建议

1. 如需仅回滚本次未知提交状态追踪，撤销 `src/polybot2other/live.py`、`tests/test_core.py` 和本进度文档的 v2.73 改动。

## 2026-05-29 v2.72

### 已完成

1. 实盘买入响应如果是 `matched/filled` 但缺少官方成交金额，会先使用 `external_order_id` 回查官方 order/trades；只有回查没有可用成交金额时才退回盘口 sweep 估算。
2. 手动实盘卖出响应如果是 `matched/filled` 但缺少官方成交金额，也会先回查官方 order/trades，再按官方金额决定实际平仓份额。
3. 新增 `_response_has_fill_amounts`，把“有成交状态”和“有可用于本地账本的成交金额”拆开判断。
4. 新增测试覆盖：买入 matched 但无金额时回查官方成交金额；手动卖出 matched 但无金额时回查官方成交金额。

### 已确认决策

1. `matched/filled` 状态可以作为成交证据，但本地账本金额优先使用官方回查到的 shares/notional/avg price。
2. 若官方响应和回查都没有成交金额，才允许退回下单前 orderbook sweep 估算，避免实盘账本过早依赖预估。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live`，20 个 live 相关测试通过。

### 已知坑位

1. 当前环境仍缺真实凭证和真实钱包授权，不能执行真实成交验证。
2. 若官方 order/trades 接口短时不可用，但 post 响应已经明确 matched，本地仍可能退回盘口 sweep 估算；后续可继续增加成交后异步金额校正。

### 回滚建议

1. 如需仅回滚本次官方金额回查增强，撤销 `src/polybot2other/live.py`、`tests/test_core.py` 和本进度文档的 v2.72 改动。

## 2026-05-29 v2.71

### 已完成

1. 实盘开关从普通配置位升级为 armed-state 硬闸门：启用时必须通过风险确认、软件隔离资金、SDK 凭证、真实 collateral balance 和 allowance 检查。
2. readiness 不通过时，服务端会把 `enabled` 自动保存回 `false`，并把阻断原因写入 `last_error`，避免页面显示“实盘开启”但实际 tick 才阻断。
3. 新增测试覆盖：缺少私钥时 `set_live_enabled(true)` 保持关闭；collateral allowance 不足时启用阶段即阻断。
4. live 相关单测中的部分入场动作改为直接调用 `LiveStrategyRunner.run_from_state`，减少与主 Paper 路径的时间耦合，避免盘口时间戳在测试中意外过期。
5. 实盘 runbook 补充：开关是 armed-state 控制，不是普通 UI 标记。

### 已确认决策

1. 允许保存实盘参数和风险确认，但不允许在 readiness 失败时保存 `enabled=true`。
2. 当前硬闸门只检查进入待下单状态所必需的内容；具体策略信号、当前盘口深度、最小订单量仍在预检和每次 tick 下单前再次检查。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live`，19 个 live 相关测试通过。

### 已知坑位

1. 当前环境仍缺真实私钥、signature type、funder、钱包资金和 allowance，因此服务会正确保持 `enabled=false`。

### 回滚建议

1. 如需仅回滚本次实盘开关硬闸门，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`docs/live-trading-runbook.md` 和本进度文档的 v2.71 改动。

## 2026-05-29 v2.70

### 已完成

1. 实盘 collateral 和 conditional token 预检改为先调用 Polymarket CLOB `update_balance_allowance` 同步缓存，再读取 `get_balance_allowance`，降低刚充值或刚授权后读取到旧 allowance 的风险。
2. 下单前的 `create_market_order` 阶段也接入配置化快速重试；如果该阶段失败，订单还没有提交到 CLOB，返回 `CREATE_ERROR` 且不会记为已成交。
3. 继续保持 post 阶段重试只重发同一份签名订单，避免网络异常时生成多张不同签名订单。
4. 不再把缺少成交金额、成交记录或 matched/filled 状态的 `OK` 响应当成本地成交；有 order id 但未确认成交时进入 `PENDING(待官方确认)`。
5. 新增 `.env.live.example` 和 `docs/live-trading-runbook.md`，把实盘密钥、signature type、funder、启动、预检、开启、订单后验和回滚步骤写清楚。
6. `.gitignore` 增加 `.env`、`.env.live`、`.env.local`，防止真实密钥文件误提交。

### 已确认决策

1. 密钥仍只允许本机环境变量或本机忽略文件承载，不进入数据库、页面配置或仓库。
2. `initial_balance` 继续作为软件预算，不伪装成链上硬限制；真实风控仍建议使用低余额隔离钱包。
3. 没有官方成交证据时，本地账本不创建 live 持仓，优先进入 PENDING 或 no-fill 状态等待后验。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live`，18 个 live 相关测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。

### 已知坑位

1. 代码侧已经具备真实下单路径，但当前运行环境仍缺真实私钥、signature type、funder、钱包资金和 allowance，不能验证真实成交。
2. `update_balance_allowance` 依赖官方 CLOB API 可用；若官方接口短时失败，实盘开仓/卖出会被预检阻断。

### 回滚建议

1. 如需仅回滚本次实盘强化，撤销 `src/polybot2other/live.py`、`tests/test_core.py`、`.gitignore`、`.env.live.example`、`README.md`、`docs/live-trading-runbook.md` 和本进度文档的 v2.70 改动。

## 2026-05-29 v2.69

### 已完成

1. 新增实盘预检链路：`LiveStrategyRunner.preflight` 汇总开关、风险确认、凭证、当前市场、官方目标价、策略信号、软件隔离资金、策略风控、最小订单、盘口深度和真实 collateral wallet balance/allowance。
2. 新增 `/api/live-preflight` GET/HEAD/POST 接口；预检只读，不会触发真实下单。
3. 前端实盘面板新增“预检”按钮和结果区，展示是否可真实下单、当前信号、预算和阻断原因。
4. 新增单测覆盖缺少真实凭证时预检阻断，以及 fake wallet/盘口满足时预检通过。

### 已确认决策

1. 预检不会自动保存页面正在编辑但未点击“保存”的参数，避免误把开关或资金参数写入运行配置。
2. 预检返回的 `can_place_next_order=true` 只代表当前快照具备下单条件；真实下单仍以 tick 到达时的最新市场、盘口、钱包和风控检查为准。
3. 缺少 private key、signature type、funder address 等凭证时，不继续检查真实 wallet，避免误导为链上余额问题。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py -k live_preflight`，2 个新增预检测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py`，73 个测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
4. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
5. 已执行 `rtk proxy git diff --check`，未发现空白错误。

### 已知坑位

1. 当前仍未提供真实密钥、funder 地址、钱包余额和 allowance，无法验证真实成交。
2. 预检是某一时刻的只读快照，市场切换、盘口过期、钱包余额变化都可能让下一次 tick 的真实下单条件发生变化。

### 回滚建议

1. 如需仅回滚本次预检入口，撤销 `src/polybot2other/live.py`、`src/polybot2other/bot.py`、`src/polybot2other/web.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`tests/test_core.py` 和本进度文档的 v2.69 改动。

## 2026-05-29 v2.68

### 已完成

1. 实盘 readiness 增加 `POLYBOT2OTHER_LIVE_SIGNATURE_TYPE` 和 `POLYBOT2OTHER_LIVE_FUNDER_ADDRESS` 强校验，API key/secret/passphrase 只能同时配置或全部留空由 SDK 派生。
2. 实盘 FAK 下单改为把当前 CLOB quote 的 `tick_size` 和 `neg_risk` 传给官方 SDK，避免固定 `0.01` tick size 在特殊市场被拒单。
3. 官方下单响应包含 `makingAmount` / `takingAmount` 时，live 持仓优先按官方 matched amount 记录 shares、notional 和 entry price；只有响应缺少金额时才退回下单前盘口 sweep 估算。
4. 手动卖出只有在官方响应确认成交后才本地平仓；如果 FAK sell 只返回 live/delayed/unmatched，会记录取消/未成交订单并保留持仓。
5. 手动卖出如官方返回部分成交金额，只按实际成交份额部分平仓，剩余份额继续保留为 OPEN。
6. 盘口 REST 深度兜底不再用空 REST quote 覆盖已有 best ask/ask size，避免短暂 REST 空响应导致策略跳过。
7. 新建本地 `.venv` 并安装项目依赖，确认 `py_clob_client_v2` 在项目运行环境可导入。
8. 新增 live `PENDING` 订单状态：FAK 返回 order id 但未确认成交时先锁定软件预算，并通过官方 order/trades 查询回查；确认成交后转 FILLED，确认无成交后转 CANCELED 并释放预算。
9. 若响应没有 order id，则不会进入 `PENDING`，避免无法回查的订单永久占用软件预算。
10. 新增真实 Polymarket collateral `balance` / `allowance` 预检；开仓前要求两者都覆盖本次实际 stake，避免只靠软件预算导致实盘下单才失败。
11. 前端实盘 readiness 展示 wallet balance、allowance 和 required cash，便于开关前确认真实钱包准备情况。
12. 新增手动卖出前 conditional token `balance` / `allowance` 预检；真实 token 余额或授权不足时不发卖单，并在订单流水写入 `TOKEN_PRECHECK_FAILED`。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_core.py`，71 个测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src .venv/bin/python -m compileall src`，编译检查通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已执行 `rtk git diff --check`，未发现空白错误。
5. 已启动 venv 最新服务 `http://127.0.0.1:8791`，`/api/live-settings` 确认 SDK 可用，当前仅缺真实 `PRIVATE_KEY`、`SIGNATURE_TYPE`、`FUNDER_ADDRESS`。

### 已知坑位

1. 当前仍未配置真实私钥/API 凭证，也未执行真实订单；不能把 SDK readiness 通过等同于真实下单完成。
2. 当前已对有 order id 的 delayed/unmatched/live 状态做轮询回查；没有 order id 的网络超时仍无法证明是否到达 CLOB，后续可继续研究 SDK 是否能稳定暴露签名订单 hash。

## 2026-05-29 v2.67

### 已完成

1. 新增 `SINGLE_FAK_REAL` 实盘组合，默认关闭，策略行为沿用当前 `SINGLE_FAK`：FAK taker 入场，保留 legacy 反转双边买入行为。
2. 新增独立实盘库 `data/live/single_fak_real.sqlite3` 和实盘配置文件 `data/live/live-settings.json`，Paper 主库和策略实验库继续独立运行。
3. 接入 `py_clob_client_v2` 官方 CLOB SDK 适配层，实盘密钥只从 `POLYBOT2OTHER_LIVE_*` 环境变量读取。
4. 实盘配置支持页面调整：开关、初始金额、单笔金额、最大持仓、单日亏损停止、总回撤停止、API 快速重试次数和重试间隔。
5. 实盘使用软件隔离预算：以配置的 `initial_balance` 作为 bot 内部风控本金，不直接按钱包全余额下注。
6. 新增实盘持仓手动卖出按钮，按当前同方向 bid 发起 FAK sell，并把提前退出写为 `early_exit`。
7. 持仓、订单流水、交易记录的数据范围下拉扩展为主账户、策略实验全部、单个 Paper 组合和 `SINGLE_FAK_REAL`。
8. 订单流水增加 live 外部订单字段：`execution_mode`、`external_order_id`、`client_order_id`、`external_status`、`raw_response`。
9. 实盘结算沿用官方结果优先、Chainlink 兜底、后续官方重查修正的现有结算链路。

### 已确认决策

1. Paper 采集不因实盘开关而停止，方便后续对比 Paper 与 Live 差异。
2. `SINGLE_FAK_REAL` 暂不使用 `STRICT`、`REVERSAL`、`STOP_AND_FLIP` 新变体，而是完全复刻当前 `SINGLE_FAK` 基线。
3. 当前版本先做单实盘组合的可扩展结构，后续新增 live 组合时继续复用独立 store + variant payload。
4. API 重试按配置快速重试；真实下单会先生成一份签名 FAK 订单，异常时重发同一份签名订单，避免每次重试生成不同订单。

### 待办和后期优化

1. 接入 Polymarket user/order websocket 或定时订单查询，补齐 delayed/unmatched/请求超时后的官方最终订单状态对账。
2. 增加 live 专用审计日志和密钥启动自检页，避免密钥缺失时只在 readiness 文案中提示。
3. 后续接入 `SINGLE_FAK_STRICT_REAL`、`SINGLE_FAK_REVERSAL_REAL`、`SINGLE_FAK_STOP_AND_FLIP_REAL` 时，需要继续保持独立库和独立风控。
4. 增加 FAK 下单超时后的官方订单查询确认，把“请求失败但订单可能已到达 CLOB”的状态自动修正。

### 已知坑位

1. `initial_balance` 是 bot 内的软件预算，不是钱包链上限制；实盘必须使用低余额隔离钱包。
2. Polymarket 市场存在最小订单量，若配置单笔金额低于 CLOB `min_order_size`，实盘会跳过下单。
3. 当前 matched 响应已有官方金额优先记录；如果官方返回 delayed/unmatched 或请求超时，需要后续订单同步模块修正最终状态。
4. 美国等受限地区可能不能合法使用 Polymarket，实盘前必须自行确认所在地合规性。

### 验证记录

1. 已执行 `rtk proxy python3 -m compileall src`，编译检查通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core`，当前测试总数更新为 64。
4. 新增测试覆盖：实盘关闭不下单、`SINGLE_FAK_REAL` fake SDK 成交、live scope 查询、手动卖出关闭 live 持仓、官方 fixed-math 成交金额解析、tick/neg risk 传参、signature/funder readiness 校验。
5. 已补充同一签名订单重试测试，确认网络异常后不会重新生成另一张订单再提交。

### 回滚建议

1. 如需回滚实盘接入，撤销 `pyproject.toml`、`src/polybot2other/live.py`、`src/polybot2other/bot.py`、`src/polybot2other/config.py`、`src/polybot2other/storage.py`、`src/polybot2other/polymarket.py`、`src/polybot2other/web.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`tests/test_core.py`、`README.md` 和本进度文档的 v2.67 改动。
2. 如只需清空实盘数据，停止服务后删除 `data/live/single_fak_real.sqlite3`、对应 WAL/SHM 文件和 `data/live/live-settings.json`。

## 2026-05-29 v2.66

### 已完成

1. 保留现有 `SINGLE_FAK` 行为不变，继续作为历史基线和隐式反转双边买入对照组。
2. 新增 `SINGLE_FAK_STRICT` 实验组合：同一市场已有任意方向持仓或挂单时，不再开反方向新仓，适合作为小资金实盘保守候选。
3. 新增 `SINGLE_FAK_REVERSAL` 实验组合：允许信号反转后买入另一边，但订单和持仓原因会写入 `SINGLE_REVERSAL`，便于单独统计反转腿表现。
4. 新增 `SINGLE_FAK_STOP_AND_FLIP` 实验组合：信号反转时先按旧方向买一价平旧仓，再尝试开新方向，用 `SINGLE_STOP_AND_FLIP` 标记真实止损反手路径。
5. 策略实验组合总数从 8 个扩展到 11 个，复盘 HTML 文案改为根据实际组合数动态展示。
6. 策略实验 payload 增加 `single_entry_mode`、`single_reversal_summary` 和 `single_stop_and_flip_summary`，用于区分基线、严格单边、显式反转和止损反手。

### 已确认决策

1. 不直接修改 `SINGLE_FAK` 的历史逻辑，避免新旧数据口径混在一起。
2. `SINGLE_FAK_REVERSAL` 先复刻当前隐式反转的核心行为，但必须显式标记和单独统计。
3. `SINGLE_FAK_STOP_AND_FLIP` 才代表真正止损反手；它和双边持有不是同一种策略。
4. 小资金实盘默认仍应优先评估 `SINGLE_FAK_STRICT`，不能把当前隐式双边行为当成天然止损。

### 待办和后期优化

1. 前端详情区可进一步展示 `single_reversal_summary` 和 `single_stop_and_flip_summary` 的 PnL、胜率、资金占用。
2. 后续需要给实盘配置增加显式策略模式选择，避免主账户默认沿用 Paper 实验模式。
3. 后续可增加反转专用风控：同一市场最多一次反转、反转价格上限、双边总投入上限和更强信号确认。

### 已知坑位

1. 新增组合会从新 SQLite 库开始采样，不能直接和已有 `SINGLE_FAK` 历史样本等量比较。
2. `SINGLE_FAK_REVERSAL` 是显式双边反转，不是止损；旧方向仍会持有到结算。
3. `SINGLE_FAK_STOP_AND_FLIP` 使用 Paper 盘口 `best_bid` 模拟退出，实盘还需要真实卖单、滑点、手续费、最小订单量和失败重试处理。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_single_fak_legacy_allows_implicit_opposite_side_entry tests.test_core.TradingCoreTest.test_single_fak_strict_blocks_opposite_side_entry_for_same_round tests.test_core.TradingCoreTest.test_single_fak_reversal_marks_opposite_side_entry tests.test_core.TradingCoreTest.test_single_fak_stop_and_flip_closes_old_side_before_new_entry`，4 个定向测试通过。
2. 已执行 `rtk proxy python3 -m py_compile src/polybot2other/bot.py src/polybot2other/experiments.py src/polybot2other/storage.py src/polybot2other/web.py src/polybot2other/report_snapshot.py`，编译检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_strategy_variants_cover_target_combinations_and_single_fak_modes tests.test_core.TradingCoreTest.test_strategy_experiments_run_all_variants_in_isolated_stores tests.test_core.TradingCoreTest.test_strategy_experiment_report_snapshot_writes_docs_html`，3 个策略实验定向测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，55 个测试通过。
6. 已执行 `rtk proxy git diff --check`，未发现空白错误。
7. 已重启默认库服务 `http://127.0.0.1:8788`。
8. 已请求 `/api/status`，确认 `runtime.running=true`、`runtime.last_error=null`、实验组合数为 11，且返回 `SINGLE_FAK`、`SINGLE_FAK_STRICT`、`SINGLE_FAK_REVERSAL`、`SINGLE_FAK_STOP_AND_FLIP` 四个 `SINGLE_FAK*` 组合。

### 回滚建议

1. 如需回滚本轮扩展，撤销 `src/polybot2other/experiments.py`、`src/polybot2other/bot.py`、`src/polybot2other/storage.py`、`src/polybot2other/web.py`、`src/polybot2other/report_snapshot.py`、`tests/test_core.py` 和本进度文档中的 v2.66 改动。
2. 本轮不修改数据库结构；如已运行服务并生成新实验库，只需停止服务后删除 `data/strategy-experiments/single_fak_strict.sqlite3`、`single_fak_reversal.sqlite3`、`single_fak_stop_and_flip.sqlite3` 及对应 WAL/SHM 文件即可清空新增组合样本。

## 2026-05-28 v2.65

### 已完成

1. 配对策略 Paper 采样日内亏损停止开仓阈值从 `3%` 提高到 `10%`，用于继续积累 `PAIR_GTC`、`PAIR_GTD`、`PAIR_POST_ONLY` 等实验组合样本。
2. 阈值常量旁增加代码注释，明确该值是 Paper-only sampling guard，不能复用为实盘风控。
3. 阻断文案改为 `配对策略日内回撤达到 10%（Paper采样阈值，实盘不得沿用），停止开新仓`。
4. `/api/status` 的 `config.pair_strategy` 增加 `daily_loss_note`，前端或后续排查可以直接看到该阈值备注。

### 已确认决策

1. 当前阶段优先积累 Paper 样本，不用 3% 过早停止实验组合。
2. 仍保留 10% 基本熔断，避免亏损组合完全无上限采样。
3. 该阈值只适用于 Paper 采样；未来接实盘时必须重新设计更严格的实盘账户风控，不能沿用本轮采样阈值。

### 待办和后期优化

1. 后续接实盘前，需要把 Paper 采样阈值和实盘风控阈值拆成独立配置，实盘默认必须更严格。
2. 可在策略实验页显式展示 `daily_loss_note`，避免只从 API 才能看到备注。

### 已知坑位

1. 提高阈值后，亏损组合会继续产生样本，短期 Paper 账户回撤可能扩大。
2. 如果后续看到 `10%`，必须先确认运行模式是 Paper；不能把它作为实盘默认值。

### 验证记录

1. 已执行 `rtk proxy python3 -m py_compile src/polybot2other/bot.py`，编译检查通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_pair_strategy_opens_two_sides_and_exits_on_bid_sum tests.test_core.TradingCoreTest.test_pair_strategy_does_not_open_without_official_target`，2 个配对策略定向测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，51 个测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
5. 已执行 `rtk git diff --check`，未发现空白错误。
6. 已重启默认库服务 `http://127.0.0.1:8788`，首页返回 HTTP 200。
7. 已请求 `/api/status`，确认 `settings.pair_strategy.daily_loss_pct = 10.0`，`daily_loss_note = Paper采样阈值，实盘不得沿用`，且 `running=True`。
8. 已请求 `/api/strategy-experiments`，确认 `PAIR_GTC`、`PAIR_GTD`、`PAIR_POST_ONLY` 仍处于继续观察/样本积累状态，未被标记为执行淘汰。

### 回滚建议

1. 如需恢复原风控采样阈值，把 `PAIR_DAILY_LOSS_PCT` 改回 `3.0`，并同步恢复阻断文案和本进度记录。
2. 本轮不涉及数据库结构，无需迁移回滚。

## 2026-05-28 v2.64

### 已完成

1. 修复 Paper resting maker 部分成交会生成大量 `$0.00` 微型持仓的问题。
2. 同一个 `paper_orders` 挂单的多次部分成交会合并更新同一个 `trades` 持仓，不再每次部分成交都新建一条持仓。
3. 低于 `$0.01` 的 resting 微型成交不会生成持仓；已有部分成交且剩余预留资金小于等于 `$0.05` 时，会释放残余资金并把挂单收口为 `FILLED`。
4. 当前持仓、仓位数量和“是否已有仓位”的判断忽略低于 `$0.01` 的历史碎片持仓，避免历史 Paper 碎片继续阻塞新样本。
5. README 补充 Paper resting dust 处理规则。
6. 补充测试，覆盖多次部分成交合并、残余 dust 释放、极小初始成交不建仓。

### 已确认决策

1. 本轮不删除历史 SQLite 数据，避免破坏复盘审计链路。
2. 历史低于 `$0.01` 的 OPEN 碎片持仓从当前持仓和风控计数中忽略，但结算逻辑仍会按原始 `trades` 表处理。
3. 本轮不引入数据库迁移，所有变更走代码层过滤和新成交写入规则。

### 待办和后期优化

1. 后续可把 `$0.01` 最小成交金额和 `$0.05` dust 释放阈值配置化。
2. 后续如接入真实 Polymarket 下单，需要改为读取市场 `mos` 等官方最小订单约束，而不是只用本地 Paper 阈值。
3. 后续可做一次只读 dust 审计报表，统计历史库里被忽略的碎片持仓数量和总金额。

### 已知坑位

1. 本轮会影响后续新 resting 成交和当前持仓展示，不会重算已经写入的历史成交均价。
2. 历史碎片 OPEN 记录仍在数据库里，最近交易或原始 SQL 审计仍可能看到它们。
3. Paper dust 阈值只是为了避免不现实的微型样本污染，不代表真实 CLOB 的全部最小订单规则。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_resting_order_partial_fills_update_existing_trade_and_release_dust tests.test_core.TradingCoreTest.test_resting_order_tiny_initial_fill_does_not_create_dust_trade tests.test_core.TradingCoreTest.test_post_only_rests_reserves_cash_and_later_fills_as_maker_queue tests.test_core.TradingCoreTest.test_gtc_resting_order_can_fill_without_post_only_queue_delay`，4 个定向测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已执行 `rtk proxy git diff --check`，未发现空白错误。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，51 个测试通过。
6. 已重启默认库服务 `http://127.0.0.1:8788`，新后台进程 PID 为 `3047284`。
7. 已请求 `/api/status`，确认 `last_error=None`，当前 `paper_entry_order_type=FAK`。
8. 已请求 `/api/strategy-experiments-tables?trade_limit=200&order_limit=20&status=all`，确认当前返回的实验持仓中低于 `$0.01` 的碎片持仓数量为 0。

### 回滚建议

1. 如需回滚本轮 dust 修复，撤销 `src/polybot2other/storage.py`、`tests/test_core.py`、`README.md` 和本进度文档中的 v2.64 改动即可。
2. 本轮没有数据库迁移；回滚后历史 dust 数据仍保持原样。

## 2026-05-28 v2.63

### 已完成

1. 首页顶部新增“资金口径”选择器，支持 `主账户` 和 8 个 `SINGLE/PAIR + FAK/GTC/GTD/POST_ONLY` 策略实验隔离账户。
2. 顶部资金卡片跟随资金口径切换，展示所选账户的总资产、总盈亏、未实现盈亏、可用资金、挂单预留、持仓风险、胜率和最大回撤。
3. 资金曲线跟随同一个资金口径切换；选择策略实验组合时读取对应 shadow SQLite 库的 equity curve。
4. `/api/equity-curve` 增加 `account_scope` 和 `variant_id` 查询参数，兼容默认主账户口径。
5. 策略实验组合 metrics 增加持仓 mark-to-market 估算，避免选中有持仓的组合时顶部未实现盈亏长期显示为 0。
6. 首页静态资源版本升级到 `v2-63`。
7. README 补充主账户和策略实验账户的资金曲线接口示例。

### 已确认决策

1. 不做“8 组合累计总资产”顶部卡片，避免把 8 个隔离实验账户误读成一个真实账户。
2. 顶部资金卡片和资金曲线使用同一个选择器，减少“卡片一个口径、曲线另一个口径”的混淆。
3. 策略实验账户只读展示资金状态，不改变主账户下单和撤单行为。

### 待办和后期优化

1. 后续可让持仓、订单流水、最近交易的数据范围跟顶部资金口径进一步联动到单个组合。
2. 后续可给资金曲线增加多组合对比模式，但需要独立命名为“实验对比”，不能叫总资产。
3. 后续可在曲线 tooltip 中补充当前组合、订单类型和策略族。

### 已知坑位

1. 选择策略实验组合后，顶部数据来自对应 shadow Paper 库，不代表主账户真实资金。
2. 历史 equity curve 不会因为后续执行模型增强而重算，只会从新采样继续追加。
3. 当前资金曲线仍是账户权益曲线，不是逐笔收益归因图。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_bot_equity_curve_window_supports_strategy_experiment_scope tests.test_core.TradingCoreTest.test_strategy_experiments_run_all_variants_in_isolated_stores`，2 个定向测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
4. 已执行 `rtk proxy git diff --check`，未发现空白错误。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，49 个测试通过。
6. 已重启默认库服务 `http://127.0.0.1:8788`，新后台进程 PID 为 `2912110`。
7. 已请求 `/api/equity-curve?account_scope=main&days=90&max_points=5`，确认返回 `equity_curve_meta.account_scope=main`。
8. 已请求 `/api/equity-curve?account_scope=strategy_experiment&variant_id=SINGLE_FAK&days=90&max_points=5`，确认返回 `variant_id=SINGLE_FAK` 和 `combo=SINGLE + FAK`。
9. 已请求首页，确认静态资源版本为 `v2-63`，包含 `account-scope-select` 和“资金口径”。

### 回滚建议

1. 如需回滚本轮口径切换，撤销 `src/polybot2other/bot.py`、`src/polybot2other/web.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`tests/test_core.py`、`README.md` 和本进度文档中的 v2.63 改动。
2. 本轮没有数据库迁移；回滚后已存在的 shadow equity curve 数据可保留。

## 2026-05-28 v2.62

### 已完成

1. 增强 POST_ONLY Paper maker 成交模型：不再只要 ask 触达限价就全额成交。
2. POST_ONLY 新增最短挂单等待、价格穿透限价缓冲和队列成交比例，成交后更容易表现为 `PARTIAL_RESTING`。
3. GTC/GTD 继续使用普通 resting maker 模型：ask 触达或穿过限价即可按可见深度成交，不套用 POST_ONLY 队列等待。
4. 保留 GTD 的限时语义，仍按 `POLYBOT2OTHER_PAPER_GTD_SECONDS` 提前过期释放预留资金。
5. README 补充 FAK/GTC/GTD/POST_ONLY Paper 执行差异。
6. 补充测试，覆盖 POST_ONLY 队列部分成交、GTC 无需 POST_ONLY 队列延迟成交、GTD 到期释放预留资金。

### 已确认决策

1. 本轮只增强 Paper 执行近似，不改数据库结构、接口返回结构和前端字段。
2. GTC 与 GTD 的成交逻辑应保持一致，核心差异是 GTD 会更早过期；强行给 GTD 加额外滑点或随机失败会降低可解释性。
3. POST_ONLY 的真实成交依赖队列位置和前序挂单，Paper 只能用保守近似模拟，不能宣称等价实盘撮合。

### 待办和后期优化

1. 后续可把 POST_ONLY 最短等待、价格穿透缓冲和队列比例配置化，便于按实盘回放校准。
2. 后续可在策略实验复盘中单独展示 POST_ONLY 部分成交率和平均挂单年龄。
3. 后续如接入真实 CLOB 下单，需要把 POST_ONLY 与 GTC/GTD 的 wire order 参数分开建模。

### 已知坑位

1. 当前队列成交比例仍是本地 Paper 近似，不知道真实排队位置、前序挂单量和网络延迟。
2. GTC/GTD 如果都在 GTD 到期前成交，历史数据仍可能接近，这是订单类型语义决定的，不一定是 bug。
3. 本轮改动只影响后续新成交和新样本，不会重算已经写入 SQLite 的历史订单。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_post_only_rests_reserves_cash_and_later_fills_as_maker_queue tests.test_core.TradingCoreTest.test_gtc_resting_order_can_fill_without_post_only_queue_delay tests.test_core.TradingCoreTest.test_gtd_resting_order_expires_and_releases_reserved_cash`，3 个定向测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已执行 `rtk proxy git diff --check`，未发现空白错误。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，48 个测试通过。
6. 已重启默认库服务 `http://127.0.0.1:8788`，新后台进程 PID 为 `2890808`。
7. 已请求 `/api/status`，确认 `running=true`、`last_error=null`、8 个策略实验组合继续返回，当前仍处于 `WAITING_FOR_SAMPLE`。

### 回滚建议

1. 如需回滚本轮增强，撤销 `src/polybot2other/bot.py`、`tests/test_core.py`、`README.md` 和本进度文档中的 v2.62 改动即可。
2. 本轮没有数据库迁移；回滚代码后，历史已生成的 `POST_ONLY_QUEUE_FILL` 订单记录可保留作为审计数据。

## 2026-05-28 v2.61

### 已完成

1. 从首页右上角移除“配对策略”开关和状态文案，避免误解为控制 8 组合策略实验。
2. 移除前端 `pairStrategyToggle` / `pairStrategyStatus` DOM 引用、渲染逻辑和事件绑定，避免删掉控件后出现空节点访问。
3. 保留后端 `/api/strategy-settings` 和主账户配对能力，后续如需主账户单独切换配对策略仍可恢复前端入口。
4. 清理不再使用的 `.strategy-toggle` 和 `.status-text` 样式。
5. 首页静态资源版本升级到 `v2-61`。

### 已确认决策

1. 右上角开关只影响主账户，不影响 8 个 shadow 策略实验账户。
2. 当前阶段页面核心是 8 组合并行复盘，保留该开关会误导策略实验的控制范围。
3. 本轮只移除前端入口，不删除后端能力，降低回滚成本。

### 待办和后期优化

1. 后续如仍需要主账户配对切换，可放到“主账户设置”区域，并明确标注“不影响 8 组合实验”。
2. 后续可以在策略实验面板增加更明显的说明：8 组合始终按配置并行运行，不受主账户开关影响。

### 已知坑位

1. `/api/strategy-settings` 仍存在，但首页已不提供入口。
2. 主账户 `pair_strategy_enabled` 的运行状态仍会在接口里返回，用于保留兼容。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
3. 已执行 `rtk proxy git diff --check`，未发现空白错误。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，47 个测试通过。
5. 已重启默认库服务 `http://127.0.0.1:8788`，新进程 PID 为 `2862360`。
6. 已请求首页，确认静态资源版本为 `v2-61`，不再包含 `pair-strategy-toggle`、`pair-strategy-status` 和“配对策略”文案。
7. 已请求 `/static/app.js?v=20260528-v2-61`，确认不再包含 `pairStrategyToggle`、`renderPairStrategy` 和 `strategy-settings` 前端调用。
8. 已请求 `/static/styles.css?v=20260528-v2-61`，确认不再包含 `.strategy-toggle` 和 `.status-text` 样式。

### 回滚建议

1. 如需恢复顶部开关，恢复 `src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css` 中 v2.61 的前端改动即可。
2. 本轮没有修改数据库和后端接口，不需要迁移回滚。

## 2026-05-28 v2.60

### 已完成

1. 新增 `/api/strategy-experiments-tables`，合并返回 8 个隔离实验账户的持仓、订单流水和交易记录。
2. 实验明细接口给每行数据补充 `combo`、`variant_id`、`strategy_family`、`experiment_order_type` 和 `account_scope`，用于页面明确标识数据归属。
3. 首页“持仓 / 订单流水 / 交易记录”新增 `主账户 / 策略实验` 数据范围切换。
4. 策略实验视图下的三张表第一列展示组合，例如 `SINGLE + FAK`、`PAIR + POST_ONLY`。
5. 策略实验订单视图禁用主账户撤单按钮和逐档成交展开，避免用 shadow 订单 ID 调主账户取消接口。
6. “最近交易”摘要中的“本金”改为“累计投入”，并增加“范围”字段，降低和初始本金、8 组合数据的混淆。
7. 首页静态资源版本升级到 `v2-60`。

### 已确认决策

1. 主账户和 8 组合实验账户不直接混成一个账户，只通过数据范围切换查看。
2. 策略实验明细只读展示，不提供取消 shadow 订单动作。
3. 主账户顶部资产和资金曲线仍保持主账户口径，避免把 8 个 shadow 账户当成真实总资产。

### 待办和后期优化

1. 后续可给策略实验视图增加组合筛选器，只看某一个组合的持仓、订单和交易。
2. 后续可把策略实验表格分页从“增大 limit 重新拉取”改为真正 offset 分页。
3. 后续可在顶部指标增加“主账户 / 策略实验汇总”口径说明。

### 已知坑位

1. 策略实验视图下的订单流水是只读 shadow 数据，不能取消订单。
2. 策略实验汇总交易记录按 8 个隔离库合并排序，和主账户最近交易不是同一账户口径。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_strategy_experiments_run_all_variants_in_isolated_stores`，定向测试通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
4. 已执行 `rtk proxy git diff --check`，未发现空白错误。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，47 个测试通过。
6. 已重启默认库服务 `http://127.0.0.1:8788`，新进程 PID 为 `2832950`。
7. 已请求 `/api/strategy-experiments-tables?trade_limit=20&order_limit=20&status=all`，确认返回 8 个组合、20 条实验订单、20 条实验交易、交易汇总 37 条，且行内包含组合名。
8. 已请求首页，确认静态资源版本为 `v2-60`，存在 `open-data-scope`、`order-data-scope`、`recent-data-scope`，旧 `v2-58` 不再出现在 HTML 中。
9. 已请求 `/static/app.js?v=20260528-v2-60`，确认包含 `strategy-experiments-tables`、`comboField` 和 `handleDataScopeChange`。

### 回滚建议

1. 如需回滚本轮数据范围切换，撤销 `src/polybot2other/bot.py`、`src/polybot2other/web.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`README.md`、`tests/test_core.py` 和本进度文档中的 v2.60 改动。
2. 本轮没有新增数据库字段，不需要迁移回滚。

## 2026-05-28 v2.59

### 已完成

1. 新增本地复盘快照命令 `python3 -m polybot2other.report_snapshot`，可把当前 8 组合复盘结果导出为静态 HTML。
2. 默认快照路径为 `docs/strategy-experiments-retrospective-<timestamp>.html`，也支持 `--output` 指定文件。
3. 快照命令支持 `--start-at` 和 `--end-at`，用于保存指定时间窗口的复盘证据。
4. 快照生成只读取现有 Paper 主库和 shadow 实验库，不启动交易循环，也不通过 Web 页面暴露写文件能力。
5. README 新增复盘快照生成命令。

### 已确认决策

1. 静态留档走本地 CLI，不走无鉴权 Web 写文件接口，降低误触和安全风险。
2. HTML 快照复用动态报告的展示口径，保证“实时查看”和“留档复盘”的字段一致。

### 待办和后期优化

1. 后续可以增加定时快照，例如每小时或每天收盘后自动保存一份。
2. 后续可以在快照里加入 Git 版本、配置摘要和环境变量摘要，增强可追溯性。
3. 后续可以增加 CSV 导出，便于表格软件继续分析。

### 已知坑位

1. 快照保存的是生成时刻的数据，后续实验库继续变化不会自动更新旧快照。
2. 如果当前样本不足，快照仍不会输出正式盈利胜出，只会记录当前盈利领先和待补样本。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_strategy_experiment_report_snapshot_writes_docs_html tests.test_core.TradingCoreTest.test_strategy_experiment_html_report_escapes_and_summarizes_variants`，2 个定向测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已执行 `rtk proxy git diff --check`，未发现空白错误。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，47 个测试通过。
6. 已执行 `rtk proxy env PYTHONPATH=src python3 -m polybot2other.report_snapshot --output docs/strategy-experiments-retrospective-latest.html`，生成静态快照。
7. 已验证 `docs/strategy-experiments-retrospective-latest.html` 包含“策略实验复盘报告”、“8 组合盈利排名”、`profitable_winner_ready`、`SINGLE + FAK` 和 `PAIR + POST_ONLY`。

### 回滚建议

1. 如需回滚本轮静态快照能力，撤销 `src/polybot2other/report_snapshot.py`、`README.md`、`tests/test_core.py` 和本进度文档中的 v2.59 改动。
2. 删除本轮生成的 `docs/strategy-experiments-retrospective-latest.html` 即可清理快照产物。

## 2026-05-28 v2.58

### 已完成

1. 新增动态 HTML 复盘报告 `/strategy-experiments-retrospective.html`，直接读取当前 8 个隔离实验库的复盘数据。
2. HTML 报告展示盈利状态、正式盈利胜出、当前盈利领先、样本可比数量、执行淘汰数量、8 组合盈利排名、待补样本和淘汰组合。
3. HTML 报告支持 `start_at/end_at` 查询参数，和 JSON 复盘接口使用同一时间窗口口径。
4. 首页策略实验面板新增“复盘报告”入口，点击后新窗口打开当前 HTML 复盘报告。
5. HTML 报告对策略原因、组合名和文本字段做转义，避免报告中展示的原因文本造成 HTML 注入。
6. 首页静态资源版本升级到 `v2-58`。

### 已确认决策

1. 报告采用动态生成，不写死到 docs，避免后续用过期静态报告做决策。
2. HTML 报告是留档和人工复盘入口，正式决胜仍以 `profit_summary.profitable_winner_ready` 和 `winner_*` 字段为准。

### 待办和后期优化

1. 后续可以增加一键导出到 `docs/` 的快照功能，用于保存某个具体时间点的复盘证据。
2. 后续可以增加报告时间窗口选择器和 CSV 下载。
3. 后续可以把最大回撤、连续亏损、按小时段表现加入报告。

### 已知坑位

1. 当前 HTML 报告是动态视图；浏览器另存为才是静态留档。
2. 如果样本不足，报告只展示当前盈利领先，不会输出正式盈利胜出。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_strategy_experiment_html_report_escapes_and_summarizes_variants tests.test_core.TradingCoreTest.test_strategy_experiment_profit_summary_separates_leader_from_final_winner tests.test_core.TradingCoreTest.test_strategy_experiments_run_all_variants_in_isolated_stores`，3 个定向测试通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
4. 已执行 `rtk proxy git diff --check`，未发现空白错误。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，46 个测试通过。
6. 已重启默认库服务 `http://127.0.0.1:8788`，新进程 PID 为 `2801245`。
7. 已请求 `/strategy-experiments-retrospective.html`，确认包含“策略实验复盘报告”、“8 组合盈利排名”、`profitable_winner_ready` 和核心组合内容。
8. 已请求首页，确认静态资源版本为 `v2-58`，存在 `strategy-experiments-retrospective.html` 入口，旧 `v2-57` 不再出现在 HTML 中。
9. 已请求 `/static/styles.css?v=20260528-v2-58`，确认包含 `.report-link` 和 `.experiment-head-actions` 样式。

### 回滚建议

1. 如需回滚本轮 HTML 报告入口，撤销 `src/polybot2other/web.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/styles.css`、`README.md`、`tests/test_core.py` 和本进度文档中的 v2.58 改动。
2. 本轮没有新增数据库字段，不需要迁移回滚。

## 2026-05-28 v2.57

### 已完成

1. 策略实验新增 `profit_summary`，把“综合评分推荐”和“盈利最高决胜”拆开，避免短期高收益样本被误读为最终主策略。
2. `/api/status` 和 `/api/strategy-experiments` 的实验数据中新增盈利复盘摘要：当前盈利领先、正式盈利胜出、盈利排名、样本状态和淘汰数量。
3. 新增 `/api/strategy-experiments-retrospective?start_at=&end_at=`，支持按时间窗口复盘 8 个隔离实验库。
4. 订单统计 `paper_order_summary` 支持 `start_at/end_at`，时间窗口复盘时订单质量和交易盈亏使用同一窗口口径。
5. 如果样本已可比较但最高净盈亏不大于 0，`profit_summary` 会返回 `NO_PROFIT`，不把亏损最少的组合误标为盈利胜出。
6. 首页策略实验摘要新增“盈利”字段，展示正式盈利胜出组合；样本不足时只展示当前盈利领先作为观察信号。
7. 首页静态资源版本升级到 `v2-57`。

### 已确认决策

1. “盈利最高”必须分成当前观察领先和正式胜出两种状态；正式胜出必须等未淘汰组合都达到样本阈值。
2. 复盘接口使用隔离 shadow 库数据，不污染主 Paper 账户。
3. 时间窗口复盘先覆盖交易汇总和订单质量，账户总资产类指标仍作为当前状态参考。
4. 正式盈利胜出的净盈亏必须大于 0；否则只能记录 `best_eligible_*`，不能输出 `winner_*`。

### 待办和后期优化

1. 后续可以把复盘接口生成 HTML/CSV 报告，保存每次决胜依据。
2. 后续可以给首页增加时间窗口选择器，直接在页面对比不同时间段的 8 组合表现。
3. 后续可以把正式胜出规则配置化，例如按净盈亏、ROI、最大回撤或综合评分切换。

### 已知坑位

1. `current_profit_leader_*` 只是当前盈利观察领先，不等于最终胜出。
2. `winner_*` 只有在样本可比较且未被淘汰后才会出现。
3. Paper 复盘仍不等于实盘收益，尤其 maker 排队成交概率需要后续实盘前专项验证。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_strategy_experiment_profit_summary_separates_leader_from_final_winner tests.test_core.TradingCoreTest.test_paper_order_summary_supports_time_window tests.test_core.TradingCoreTest.test_strategy_experiments_run_all_variants_in_isolated_stores`，3 个定向测试通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
4. 已执行 `rtk proxy git diff --check`，未发现空白错误。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，45 个测试通过。
6. 已重启默认库服务 `http://127.0.0.1:8788`，新进程 PID 为 `2794506`。
7. 已请求 `/api/status`，确认返回 8 个实验组合、`profit_summary`、8 条盈利排名，当前盈利复盘状态为 `WAITING_FOR_SAMPLE`，`profitable_winner_ready=false`。
8. 已请求 `/api/strategy-experiments-retrospective`，确认返回 8 个组合、`profit_summary.rankings`、`profitable_winner_ready` 和空时间窗口。
9. 已请求首页，确认静态资源版本为 `v2-57`，旧 `v2-56` 不再出现在 HTML 中。
10. 已请求 `/static/app.js?v=20260528-v2-57`，确认包含 `profit_summary`、“盈利”和 `current_profit_leader_variant_id`。

### 回滚建议

1. 如需回滚本轮盈利复盘摘要，撤销 `src/polybot2other/bot.py`、`src/polybot2other/storage.py`、`src/polybot2other/web.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`README.md`、`tests/test_core.py` 和本进度文档中的 v2.57 改动。
2. 本轮没有新增数据库字段，不需要迁移回滚。

## 2026-05-28 v2.56

### 已完成

1. 策略实验评分新增“执行淘汰”口径：订单样本足够但长期没有成交的组合，不再永久阻塞 8 组组合进入决胜比较。
2. `decision_summary` 新增并使用 `pending_count`、`disqualified_count` 和 `disqualified_variants`，正式推荐只从可比较且未淘汰的组合中产生。
3. 首页策略实验摘要新增“淘汰”数量，便于区分“还在等待样本”和“执行质量已不适合继续决胜”的组合。
4. 修复首页策略实验渲染 key 中 `decision` 未传入导致的前端运行时错误。
5. 首页静态资源版本升级到 `v2-56`。

### 已确认决策

1. 90%+ 报告契合度只能作为目标，不应强行让所有组合都达到 90%；单边组合主要保留为对照组。
2. 低成交淘汰是保守条件，只处理订单样本足够但几乎无法成交的执行组合，避免误淘汰仍在积累样本的策略。
3. 正式推荐必须同时满足“样本可比较”和“未被执行淘汰”，不能按短期 PnL 直接切主策略。

### 待办和后期优化

1. 后续可把淘汰阈值配置化，例如订单阈值、最低成交率、最低结算数。
2. 后续复盘报告应展示被淘汰组合的订单样本和成交率证据，避免只看结果标签。

### 已知坑位

1. `DISQUALIFIED` 代表当前 Paper 执行质量不可用于决胜，不代表该组合在所有市场环境下永久无效。
2. 如果 Polymarket 流动性变化，POST_ONLY/GTC/GTD 的成交率需要继续滚动观察。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_strategy_experiment_low_fill_variant_is_disqualified tests.test_core.TradingCoreTest.test_strategy_experiment_decision_can_finish_with_disqualified_variants tests.test_core.TradingCoreTest.test_strategy_experiments_run_all_variants_in_isolated_stores`，3 个定向测试通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
4. 已执行 `rtk proxy git diff --check`，未发现空白错误。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，43 个测试通过。
6. 已重启默认库服务 `http://127.0.0.1:8788`，新进程 PID 为 `2785664`。
7. 已请求 `/api/status`，确认返回 8 个实验组合、`decision_summary.pending_count`、`decision_summary.disqualified_count`，当前仍为样本等待态。
8. 已请求首页，确认静态资源版本为 `v2-56`，旧 `v2-55` 不再出现在 HTML 中。
9. 已请求 `/static/app.js?v=20260528-v2-56`，确认包含 `experimentRenderKey(experiments, variants, decision)`、“淘汰”和 `pending_count`。

### 回滚建议

1. 如需回滚本轮执行淘汰口径，撤销 `src/polybot2other/bot.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`tests/test_core.py` 和本进度文档中的 v2.56 改动。
2. 本轮没有新增数据库字段，不需要迁移回滚。

## 2026-05-28 v2.55

### 已完成

1. 策略实验汇总新增 `decision_summary`，明确区分“当前观察领先”和“正式决胜推荐”。
2. 只有全部组合达到样本阈值后，`decision_summary.comparison_ready` 才会为 `true`，并输出 `recommended_variant_id`。
3. 样本不足时返回 `WAITING_FOR_SAMPLE`，同时保留 `current_leader_variant_id` 作为观察对象，避免短期高分被误认为最终推荐。
4. 首页策略实验摘要从“领先”改为“推荐/状态/可比”，直接展示当前是否具备决胜条件。
5. 首页静态资源版本升级到 `v2-55`。

### 已确认决策

1. 决胜条件必须看全部 8 个组合是否达到可比较样本，不能只看单个组合短期领先。
2. 当前样本不足时只给观察信号，不给正式推荐。

### 待办和后期优化

1. 后续可把样本阈值配置化，并在页面提示每个组合距离阈值还差多少。
2. 后续复盘报告应优先读取 `decision_summary`，避免报告和首页使用不同决策口径。

### 已知坑位

1. `current_leader_variant_id` 只是当前评分领先，不等同于可切换主策略。
2. 如果某些组合长期没有成交，会阻止整体进入正式决胜状态；这符合保守复盘要求，但需要结合执行质量判断是否淘汰该组合。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_strategy_experiments_run_all_variants_in_isolated_stores`，定向测试通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
3. 已执行 `rtk proxy python3 -m py_compile src/polybot2other/bot.py tests/test_core.py`，Python 语法检查通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，41 个测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
6. 已执行 `rtk proxy git diff --check`，未发现空白错误。
7. 已重启默认库服务 `http://127.0.0.1:8788`，并请求 `/api/status`，确认返回 `decision_summary`、状态为 `WAITING_FOR_SAMPLE`、正式推荐为空。
8. 已请求 `/static/index.html`，确认包含 `v2-55`。
9. 已请求 `/static/app.js?v=20260528-v2-55`，确认包含“推荐”“可比”和 `decision_summary`。

### 回滚建议

1. 如需回滚本轮决胜摘要，撤销 `src/polybot2other/bot.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`tests/test_core.py` 和本进度文档中的 v2.55 改动。
2. 本轮没有新增数据库字段，不需要迁移回滚。

## 2026-05-28 v2.54

### 已完成

1. 每个策略实验组合新增后端复盘评分 `review_score`，包含总分、决策建议、样本状态、官方结算占比、异常订单占比、分项得分和扣分原因。
2. 复盘评分综合 ROI、净盈亏、胜率、成交率、官方结算占比、样本量、取消/过期/拒绝比例和运行异常，不再只按短期 PnL 排名。
3. 首页策略实验表新增“评分”和“样本”列，排名优先按后端评分排序。
4. 组合详情区新增评分、决策和扣分原因，方便解释为什么某个组合暂时不能作为主策略候选。
5. 首页静态资源版本升级到 `v2-54`。

### 已确认决策

1. 评分不是盈利承诺，只是复盘排序辅助；样本不足时即使短期盈利也显示“继续观察”。
2. 评分阈值先采用保守默认：至少 30 笔结算和 60 笔订单才进入“可比较”。

### 待办和后期优化

1. 后续可以把评分公式参数配置化，例如最低样本数、成交率权重、官方结算权重。
2. 后续复盘报告应记录评分分项，避免只给一个总分导致无法解释。

### 已知坑位

1. 当前评分是 Paper 复盘辅助，不代表实盘收益；POST_ONLY/GTC/GTD 的真实 maker 排队概率仍需要实盘前单独评估。
2. 样本不足阶段的排序只能用于观察，不适合直接切换主策略。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_strategy_experiments_run_all_variants_in_isolated_stores`，定向测试通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
3. 已执行 `rtk proxy python3 -m py_compile src/polybot2other/bot.py tests/test_core.py`，Python 语法检查通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，41 个测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
6. 已执行 `rtk proxy git diff --check`，未发现空白错误。
7. 已重启默认库服务 `http://127.0.0.1:8788`，并请求 `/api/status`，确认返回 8 个组合和 `review_score` 字段。
8. 已请求首页，确认包含 `v2-54`、“评分”和“样本”列。

### 回滚建议

1. 如需回滚本轮复盘评分，撤销 `src/polybot2other/bot.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`tests/test_core.py` 和本进度文档中的 v2.54 改动。
2. 本轮没有新增数据库字段，不需要迁移回滚。

## 2026-05-28 v2.53

### 已完成

1. 策略实验表新增“详情”列，每个组合可直接在首页展开。
2. 点击组合详情会请求 `/api/strategy-experiments?variant_id=<组合>&trade_limit=6&order_limit=6`，展示该组合自己的最近交易和订单流水摘要。
3. 详情区展示该组合净盈亏、ROI、官方/兜底结算数、成交率、订单数、最近交易和订单状态，方便从排名进入复盘证据。
4. 详情每次展开都会重新请求接口，避免长期使用旧缓存误判当前组合表现。
5. 首页静态资源版本升级到 `v2-53`。

### 已确认决策

1. 本轮只增加前端详情展开，不改变交易逻辑、结算逻辑和数据库结构。
2. 详情先展示最近 6 条交易和最近 6 条订单，避免在首页一次性渲染过多历史数据。

### 待办和后期优化

1. 后续可以把详情区升级为可分页详情面板，支持查看更多该组合订单和交易。
2. 后续复盘报告应直接读取单组合详情接口，生成每个组合的证据链。

### 已知坑位

1. 详情区展示的是最近样本摘要，不是完整历史；完整决策仍要结合后续复盘报告。
2. 如果组合近期没有交易或订单，详情区会显示空状态，这是正常现象。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
2. 已执行 `rtk proxy git diff --check`，未发现空白错误。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，41 个测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
5. 已请求首页，确认包含 `v2-53` 和“详情”列。
6. 已请求 `/static/app.js?v=20260528-v2-53`，确认包含 `data-experiment-id` 和 `toggleExperimentDetail`。

### 回滚建议

1. 如需回滚本轮前端详情展开，撤销 `src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css` 和本进度文档中的 v2.53 改动。
2. 本轮不涉及后端、数据库和实验库结构变更。

## 2026-05-28 v2.52

### 已完成

1. 主 bot 官方结算结果会广播到 8 个 shadow 实验库，覆盖首次官方结算、fallback 后官方修正、官方 final_price / target_price 回填三条路径。
2. shadow 实验库收到官方结果后，会先修正已按 Chainlink fallback 结算的交易，再结算仍未完成的 open trades，避免实验复盘长期停留在本地价格结果。
3. 策略实验运行状态增加 `official_broadcast_count`、`last_official_broadcast_at` 和单组合 `official_broadcast_error`。
4. 首页策略实验摘要增加“官方”计数，组合行出现官方广播异常时会在最后信号列标红展示。
5. 首页静态资源版本升级到 `v2-52`。

### 已确认决策

1. 官方结果只由主 bot 请求上游一次，再广播给 8 个实验库；shadow bot 不再各自重复请求 Polymarket 官方接口。
2. shadow 实验仍保留本地价格临时结算能力，但只作为官方结果出来前的临时状态。

### 待办和后期优化

1. 后续复盘报告需要按 `official_count`、`chainlink_count`、`unknown_source_count` 拆分每个组合的结算可信度。
2. 如果某个实验库广播失败，需要在前端加更明显的告警入口和一键重放官方广播。

### 已知坑位

1. 官方广播只会作用于 shadow 库里已经存在的 round；如果某个组合从未见过该市场，不会反向补整段历史。
2. 早退 `early_exit` 交易不会被官方结果重新改写，这是有意保留的风控行为。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_bot_broadcasts_official_resolution_to_strategy_experiments tests.test_core.TradingCoreTest.test_strategy_experiments_run_all_variants_in_isolated_stores`，2 个定向测试通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
3. 已执行 `rtk proxy python3 -m py_compile src/polybot2other/bot.py src/polybot2other/web.py src/polybot2other/storage.py tests/test_core.py`，Python 语法检查通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，41 个测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
6. 已执行 `rtk proxy git diff --check`，未发现空白错误。
7. 已重启默认库服务 `http://127.0.0.1:8788`，并请求 `/api/status`，确认 `running=True`、实验启用、返回 8 个组合、存在 `official_broadcast_count` 和单组合 `official_broadcast_error` 字段。
8. 已请求首页，确认包含 `v2-52` 和“策略实验”面板。

### 回滚建议

1. 如需回滚本轮官方广播，撤销 `src/polybot2other/bot.py`、`src/polybot2other/static/app.js`、`src/polybot2other/static/index.html`、`tests/test_core.py` 和本进度文档中的 v2.52 改动。
2. 本轮没有新增数据库字段，不需要迁移回滚。

## 2026-05-28 v2.51

### 已完成

1. 每个策略实验组合新增订单执行质量统计：总订单、活跃挂单、完全成交、部分成交、取消、过期、拒绝、POST_ONLY 数量、成交率、成交份额、花费和手续费。
2. `/api/strategy-experiments` 的每个组合返回 `order_summary`，方便复盘时同时看 PnL 和执行质量。
3. 新增单组合详情能力：`/api/strategy-experiments?variant_id=PAIR_GTD&trade_limit=50&order_limit=50` 返回该组合的概览、最近交易和订单流水。
4. 策略实验面板新增“成交率”和“订单质量”列，避免只按短期 ROI 判断组合优劣。
5. shadow runner 不再在同一把锁里跑完整 8 组合，也不再让 8 个 shadow bot 重复请求官方结算；shadow 组合先用本地价格结算，后续再做主 bot 官方结果广播。
6. 首页静态资源版本升级到 `v2-51`，避免浏览器继续使用旧实验面板脚本。

### 已确认决策

1. 复盘决胜不能只看净盈亏，还要看成交率、拒绝率、过期率和取消率。
2. 官方结算核对不应由 8 个 shadow bot 分别请求上游，后续推荐由主 bot 获取一次官方结果后广播给所有实验库。

### 待办和后期优化

1. 将主 bot 官方结算结果广播到 8 个 shadow 实验库，替代当前 shadow 的本地价格结算。
2. 增加单组合前端详情展开，展示该组合自己的最近交易和订单流水。
3. 在复盘报告中加入执行质量评分，避免高 ROI 但成交率极低的组合误导决策。

### 已知坑位

1. v2.51 后 shadow 实验接口响应会更轻，但官方结算广播还没完成；短期 shadow 结果仍可能先显示本地价格结算，后续需要官方修正链路。
2. 成交率是 Paper 订单生命周期统计，不代表真实 maker 排队成交概率。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_strategy_experiments_run_all_variants_in_isolated_stores`，定向测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，40 个测试通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
5. 已执行 `rtk proxy git diff --check`，未发现空白错误。
6. 已重启默认库服务 `http://127.0.0.1:8788`，并请求 `/api/strategy-experiments`，确认返回 8 个组合和 `order_summary`。
7. 已请求 `/api/strategy-experiments?variant_id=PAIR_GTD&trade_limit=5&order_limit=5`，确认返回 `PAIR_GTD` 详情、最近订单分页和最近交易分页。
8. 已请求首页，确认包含 `v2-51`、`成交率` 和 `订单质量`。

### 回滚建议

1. 如需回滚本轮执行质量统计和详情接口，撤销 `src/polybot2other/storage.py`、`src/polybot2other/bot.py`、`src/polybot2other/web.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`tests/test_core.py`、`README.md` 和本进度文档中的 v2.51 改动。
2. 本轮没有新增数据库字段，不需要迁移回滚。

## 2026-05-28 v2.50

### 已完成

1. 首页新增“策略实验”面板，展示 8 个组合的排名、定位、净盈亏、ROI、胜率、结算数、当前持仓/挂单、目标契合度和最后信号。
2. 策略实验面板直接读取 `/api/status` 中的 `runtime.strategy_experiments`，不额外增加轮询接口压力。
3. 实验表格不会因为单纯 tick 计数变化而整表重绘，降低选中文字、查看行内容时被实时刷新打断的概率。
4. 首页静态资源版本升级到 `v2-50`，避免浏览器继续使用旧 JS/CSS。

### 已确认决策

1. 本轮只做 8 组合可视化排名入口，不改变交易策略和 shadow 数据库结构。
2. 暂时按已结算组合的净盈亏/ROI 排名；没有任何组合结算前保留原始 8 组合顺序。

### 待办和后期优化

1. 后续增加更完整的实验复盘：最大回撤、成交率、撤单率、过期率、官方修正次数和按时间窗口筛选。
2. 后续可以给每个组合加独立详情页，展示该组合自己的订单流水和最近交易。

### 已知坑位

1. 当前排名只代表 shadow Paper 数据，不代表实盘可成交性；`POST_ONLY/GTC/GTD` 的 maker 排队位置仍是模拟。
2. 样本量不足时不要按短期 ROI 直接切主策略，需要至少跨多个市场周期观察。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
2. 已执行 `rtk proxy git diff --check`，未发现空白错误。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，40 个测试通过。
4. 已请求首页，确认包含 `v2-50`、`strategy-experiments` 和 `strategy-experiment-summary`。
5. 已请求 `/api/status`，确认 `running=True`、实验启用、`run_count=56`、返回 8 个组合。
6. 已请求 `/static/app.js?v=20260528-v2-50`，确认 HTTP 200。

### 回滚建议

1. 如需回滚本轮可视化入口，撤销 `src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css` 和本进度文档中的 v2.50 改动。
2. 本轮不涉及后端、数据库和实验库结构变更。

## 2026-05-28 v2.49

### 已完成

1. 新增 8 个策略实验组合定义：`SINGLE/PAIR + FAK/GTC/GTD/POST_ONLY`。
2. 新增隔离式 shadow Paper 实验 runner；每个组合使用独立 SQLite 库、独立账户、独立订单和独立盈亏，避免 8 个组合共用主账户导致数据污染。
3. `PAIR + GTC/GTD/POST_ONLY` 不再只是名义配置：配对策略现在能生成双边 resting orders，并按组合记录独立挂单。
4. 配对策略增加当前市场活跃挂单拦截，避免 resting pair 未成交时重复堆挂同一 round。
5. resting order 的订单流水 reason 追加原始策略 reason，避免挂单记录丢失 `PAIR_OPEN_RESTING` 等策略归因。
6. `/api/status` 和新增 `/api/strategy-experiments` 返回 8 组合实验状态、目标完成度、目标报告契合度、隔离库路径、指标和最近交易汇总。
7. README 增加 strategy experiments 说明、接口和环境变量。

### 已确认决策

1. 主 Paper 账户不直接 8 倍下注；8 个组合先用 shadow Paper 隔离运行，用数据复盘决胜。
2. 隔离库默认目录为 `data/strategy-experiments`，每个组合一个 SQLite 文件。
3. `Settings(...)` 直接构造时默认不启用实验，避免单元测试和脚本意外创建 8 个子 bot；`load_settings()` 运行服务时默认启用，可用 `POLYBOT2OTHER_STRATEGY_EXPERIMENTS_ENABLED=false` 关闭。

### 待办和后期优化

1. 前端还没有新增专门的 8 组合排名面板；当前可先通过 `/api/strategy-experiments` 查看数据。
2. shadow 实验的官方结算核对目前复用每个隔离 bot 自己的逻辑，后续可优化为主 bot 获取一次官方结果后广播给 8 个实验库，减少重复请求。
3. 后续复盘报告应按 `variant_id` 汇总 ROI、胜率、最大回撤、挂单成交率、早退收益和官方结算修正影响。

### 已知坑位

1. `PAIR + GTC/GTD/POST_ONLY` 已能双边挂单，但真实 maker 订单的排队位置、撮合优先级和撤改单延迟仍是 Paper 近似模拟，不等于实盘成交保证。
2. 如果单边先成交、另一边长期未成交，残余库存管理会接管，但仍需要后续继续强化“成交残缺后撤单/补单/止损”的完整闭环。
3. 8 个 shadow bot 会增加本地 SQLite 写入和少量官方结算查询压力，长期运行需要观察接口延迟。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_strategy_variants_cover_all_eight_target_combinations tests.test_core.TradingCoreTest.test_pair_strategy_gtd_places_two_resting_pair_orders tests.test_core.TradingCoreTest.test_pair_strategy_post_only_places_two_maker_pair_orders tests.test_core.TradingCoreTest.test_strategy_experiments_run_all_variants_in_isolated_stores`，4 个新增定向测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，40 个测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
4. 已执行 `rtk proxy git diff --check`，未发现空白错误。
5. 已重启默认库服务 `http://127.0.0.1:8788`，并请求 `/api/status` 和 `/api/strategy-experiments`，确认 `running=True`、实验启用、返回 8 个组合。
6. 已确认 `data/strategy-experiments` 下生成 8 个隔离 SQLite 文件。

### 回滚建议

1. 如需回滚本轮 8 组合实验基础，撤销 `src/polybot2other/experiments.py`、`src/polybot2other/bot.py`、`src/polybot2other/config.py`、`src/polybot2other/storage.py`、`src/polybot2other/web.py`、`tests/test_core.py`、`README.md` 和本进度文档中的 v2.49 改动。
2. 本轮没有修改主库 schema；回滚代码后，可直接保留或删除 `data/strategy-experiments` 下的 shadow 实验 SQLite 文件。

## 2026-05-28 v2.43

### 已完成

1. 修复订单流水和最近交易右侧“字段”菜单被裁切的问题。
2. 将订单流水、最近交易的字段菜单移出带 `overflow-x: auto` 的 actions 容器，避免下拉层被父容器裁剪。
3. 新增 `panel-head-actions` 右侧工具栏容器，保持筛选/按钮横向滚动，同时让字段菜单下拉层可正常覆盖在表格上方。
4. 给打开状态的 `.field-menu` 增加更高层级，降低被后续表格内容遮挡的概率。
5. 首页 CSS 版本号升级到 `/static/styles.css?v=20260528-v2-43`，避免浏览器继续使用旧样式。

### 已确认决策

1. 本轮只修复字段菜单展示层级和裁切问题，不改变订单/交易接口和字段选择逻辑。
2. 保留订单流水和最近交易操作区的横向布局，不回退到竖向堆叠。

### 待办和后期优化

1. 当前环境没有可用浏览器自动化工具，未做真实截图验证；后续可补 Playwright/Chromium 视觉回归。

### 已知坑位

1. 小屏幕下操作区仍会横向滚动，字段按钮固定在滚动区域外侧，避免再次裁切下拉层。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
2. 已执行 `rtk git diff --check -- src/polybot2other/static/index.html src/polybot2other/static/styles.css`，未发现空白错误。
3. 已请求首页 HTML，确认 CSS 链接为 `/static/styles.css?v=20260528-v2-43`，并且订单流水、最近交易均存在 `panel-head-actions`。
4. 已静态检查确认存在 `.field-menu[open]` 层级规则。

### 回滚建议

1. 如需回滚本轮字段菜单展示修复，撤销 `src/polybot2other/static/index.html`、`src/polybot2other/static/styles.css` 和本进度文档中的 v2.43 改动。
2. 本轮不涉及后端和数据库结构变更，无需服务端迁移回滚。

## 2026-05-28 v2.42

### 已完成

1. 官方结算结果继续以 Gamma `outcomePrices` 判断 Up/Down winner，不改胜负信任链路。
2. 结算路径新增 `eventMetadata.finalPrice` 和 `eventMetadata.priceToBeat` 读取，官方结算能写入最终价和官方目标价。
3. `find_current_btc_5m_market` 解析市场时保留 event 级 `eventMetadata`，减少不必要的页面目标价解析。
4. 如果 Gamma metadata 缺少最终价或目标价，才在市场结束后兜底解析一次 Polymarket 页面 payload，并按 slug 缓存。
5. 已结算且来源为 `polymarket_official`、但 `final_price` 缺失的近 24 小时记录，会每轮最多补 3 条；已补齐的记录不会重复请求。
6. 官方补偿核对如果拿到最终价，会同步更新 `market_rounds.final_price`；如果拿到更准确的 `priceToBeat`，会更新 `target_price`，最近交易的最终距离 bps 会随之恢复。
7. README 更新结算价来源说明。

### 已确认决策

1. 页面解析不是实时行情路径，只作为结算后 metadata 缺失时的兜底。
2. 不新增数据库字段，本轮复用 `market_rounds.final_price` 和 `market_rounds.target_price`。
3. 不重新核对已经有最终价的官方结算记录，避免重复请求和重复扰动历史数据。

### 待办和后期优化

1. 如果后续需要审计价格来源，可再新增 `final_price_source` 字段；当前为了避免迁移，只在运行时保留来源说明。
2. Polymarket 页面 payload 属于兜底路径，若页面结构变化，最多影响最终价展示，不影响官方 winner 和盈亏结算。

### 已知坑位

1. 历史很久以前的官方记录如果超过 24 小时窗口，本轮不会自动回填，避免大批量请求页面。
2. 如果 Polymarket 官方 metadata 临时缺少 `finalPrice`，记录仍会保持最终价为空，并按 60 秒间隔重试近期缺失记录。

### 验证记录

1. 已执行 `rtk proxy python3 -m py_compile src/polybot2other/polymarket.py src/polybot2other/storage.py src/polybot2other/bot.py tests/test_core.py`，语法检查通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_polymarket_resolution_reads_event_metadata_prices tests.test_core.TradingCoreTest.test_polymarket_resolution_falls_back_to_page_prices tests.test_core.TradingCoreTest.test_bot_records_official_resolution_final_and_target_prices tests.test_core.TradingCoreTest.test_bot_backfills_missing_official_final_price_once_available`，4 个结算价格定向测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，36 个测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
5. 已执行 `rtk git diff --check`，未发现空白错误。
6. 已重启默认库服务 `http://127.0.0.1:8788`，首页返回 HTTP 200。
7. 已请求 `/api/status`，确认 `running=True`，当前市场目标价来自 `market.target_price`。
8. 已请求 `/api/recent-trades?limit=5&offset=0`，确认 `Polymarket官方` 记录返回 `final_price` 和 `final_distance_bps`。

### 回滚建议

1. 如需回滚本轮官方最终价补全，撤销 `polymarket.py`、`storage.py`、`bot.py`、`README.md`、`tests/test_core.py` 和本进度文档中的 v2.42 改动。
2. 本轮不涉及数据库结构变更，无需 SQLite 迁移回滚。

## 2026-05-28 v2.41

### 已完成

1. 最近交易区域新增骨架 loading，覆盖统计卡片和表格行。
2. 页面首次加载时先展示最近交易骨架，数据返回后再淡入真实统计和表格。
3. 点击最近交易时间范围“查询”或“重置”时展示骨架 loading，避免空白或硬切。
4. “查看更多”不清空已有列表，不触发整表骨架，只保持按钮禁用，避免打断用户浏览。
5. 后台自动轮询不触发骨架 loading，避免重新引入闪烁、选中中断和焦点丢失问题。
6. 新增 `prefers-reduced-motion` 兼容，系统关闭动画时不播放 shimmer 和淡入动画。
7. 首页脚本版本号升级到 `/static/app.js?v=20260528-v2-41`。

### 已确认决策

1. 骨架 loading 只用于用户明确等待的场景，不用于后台轮询。
2. 数据展示使用短淡入过渡，避免骨架到真实表格的切换过硬。
3. 保持最近交易表格和统计区原尺寸结构，降低布局跳动。

### 待办和后期优化

1. 如果用户后续希望“查看更多”也有局部 loading，可以只在按钮或底部增加轻量状态，不替换表格主体。
2. 如果最近交易在自动刷新时也出现复制中断，可以再做和订单流水类似的 DOM 延迟刷新。

### 已知坑位

1. 网络异常时骨架会保持到请求失败并显示错误提示；后续可以增加“重试”状态。
2. 骨架列宽是视觉占位，不和用户自定义字段逐项一一对应。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
2. 已执行 `rtk git diff --check -- src/polybot2other/static/app.js src/polybot2other/static/styles.css src/polybot2other/static/index.html`，未发现空白错误。
3. 已静态检查确认存在 `renderRecentSkeleton`、`recent-content-enter`、`skeleton-shimmer`、`prefers-reduced-motion` 和脚本版本 `v2-41`。

### 回滚建议

1. 如需回滚本轮最近交易 loading 动画，撤销 `static/app.js`、`static/styles.css`、`static/index.html` 和本进度文档中的 v2.41 改动。
2. 本轮未修改后端和数据库结构，无需服务端迁移回滚。

## 2026-05-28 v2.40

### 已完成

1. 最近交易新增时间范围查询能力，前端可选择开始时间和结束时间后查询指定范围内的交易记录。
2. `/api/recent-trades` 支持 `start_at` / `end_at` 秒级时间戳参数，分页加载会继续沿用同一个时间范围。
3. 后端新增范围内交易统计，统计基于完整时间范围，不受当前页 `limit` 影响。
4. 最近交易区域新增统计展示：交易数、已结算数、总盈亏、ROI、胜率、本金、回款、结算来源数量。
5. 默认未选择时间范围时，保持原来的最近交易列表和分页行为。
6. README 补充 `/api/recent-trades` 时间范围查询示例。

### 已确认决策

1. 时间筛选口径使用 `COALESCE(settled_at, opened_at)`。
2. 已结算交易按 `settled_at` 进入范围，OPEN 交易按 `opened_at` 进入范围。
3. 总盈亏、ROI、胜率只按已结算交易计算，OPEN 交易单独计入交易数和 open risk，不混入已实现收益。
4. 统计必须由后端计算，不能只统计前端当前页，避免分页后总盈亏失真。

### 待办和后期优化

1. 后续可以增加“仅看官方修正过”“仅看 Chainlink 兜底”“仅看提前平仓”等筛选。
2. 如果最近交易也出现复制文本被自动刷新打断，可以复用订单流水的 DOM 延迟刷新策略。

### 已知坑位

1. 服务运行时数据库会继续变化，同一时间范围重复查询的结果可能因为新交易结算而变化。
2. 前端时间控件使用浏览器本地时区生成时间戳，后端只接收 Unix 秒级时间戳。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_recent_trades_time_range_summary_uses_full_range_not_page tests.test_core.TradingCoreTest.test_recent_trades_supports_count_and_offset`，2 个最近交易分页/统计测试通过。
2. 已执行 `rtk proxy python3 -m py_compile src/polybot2other/storage.py src/polybot2other/bot.py src/polybot2other/web.py tests/test_core.py`，编译检查通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，32 个测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
6. 已执行 `rtk git diff --check`，未发现空白错误。
7. 已重启默认库服务 `http://127.0.0.1:8788`，首页返回 HTTP 200。
8. 已请求首页 HTML，确认脚本地址为 `/static/app.js?v=20260528-v2-40`，且存在 `recent-start-time` 和 `recent-summary`。
9. 已请求 `/api/recent-trades?limit=1&offset=0&start_at=0&end_at=4102444800`，确认返回 `recent_trades_summary.total_pnl`、`win_rate`、`roi_pct`。
10. 已请求 `/api/status`，确认 `running=True`，且返回 `recent_trades_summary`。

### 回滚建议

1. 如需回滚本轮最近交易时间范围查询和统计，撤销 `storage.py`、`bot.py`、`web.py`、`static/index.html`、`static/app.js`、`static/styles.css`、`README.md`、`tests/test_core.py` 和本进度文档中的 v2.40 改动。
2. 本轮不涉及数据库结构变更，无需 SQLite 迁移回滚。

## 2026-05-28 v2.34

### 已完成

1. `trades` 新增交易级 `settlement_source` 字段，schema version 升到 6。
2. 市场到期时，官方结算写入 `polymarket_official`，Chainlink 兜底写入 `chainlink_fallback`，提前平仓写入 `early_exit`。
3. 新增官方补偿核对：最近 24 小时内使用 Chainlink 兜底或历史未记录来源的已结算 BTC 市场，会每轮最多检查 5 个，并对单个市场按 10 秒间隔节流。
4. 官方结果出现后，如果与兜底结果一致，只升级市场和交易来源为 `polymarket_official`，不改资金。
5. 官方结果与兜底结果不一致时，按官方 winner 重算对应市场结算交易的 `payout`、`pnl`、`exit_price`，并把账户 `cash_balance`、`realized_pnl` 做差额补偿。
6. 提前平仓交易不会参与官方补偿重算，避免被市场最终结果误改。
7. 最近交易优先展示交易级结算来源，缺省时才回退市场级来源。
8. README 补充 `polymarket_official`、`chainlink_fallback`、`early_exit` 与官方补偿核对说明。

### 已确认决策

1. 官方结果仍优先信任 Polymarket winner。
2. Polymarket 官方 winner 不是最终 BTC 价格；官方结算路径继续不伪造 `final_price`。
3. Chainlink 兜底只是临时兜底，后续官方结果出现后必须升级或修正。
4. 官方补偿只处理市场结算交易，不处理 `early_exit`。

### 待办和后期优化

1. 如果后续找到官方最终 BTC 价格的稳定来源，可以把官方 `final_price` 填入补偿路径。
2. 如果历史库里存在 v2.34 之前的提前平仓且交易级来源为空，展示仍可能依赖旧数据特征推断，不能做到 100% 反推。

### 已知坑位

1. 官方结果延迟期间，最近交易可能短暂显示 `Chainlink兜底`；补偿核对拿到官方 winner 后会更新为 `Polymarket官方`。
2. 如果官方 winner 与兜底方向相反，历史盈亏和现金余额会被差额修正，这是为了接近真实结算，不是重复结算。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_trade_settlement_updates_pnl_with_real_market_side tests.test_core.TradingCoreTest.test_chainlink_fallback_settlement_records_source_and_final_price tests.test_core.TradingCoreTest.test_official_recheck_upgrades_matching_chainlink_fallback_source tests.test_core.TradingCoreTest.test_official_recheck_corrects_mismatched_chainlink_fallback_pnl tests.test_core.TradingCoreTest.test_bot_rechecks_fallback_settlement_until_official_resolution tests.test_core.TradingCoreTest.test_partial_close_keeps_account_and_open_position_consistent`，6 个结算相关测试通过。
2. 已执行 `rtk proxy python3 -m py_compile src/polybot2other/storage.py src/polybot2other/bot.py tests/test_core.py`，编译检查通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，31 个测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
6. 已执行 `rtk git diff --check`，未发现空白错误。
7. 已重启默认库服务 `http://127.0.0.1:8788`，首页返回 HTTP 200。
8. 已请求 `/api/status`，确认 `running=True`，最近交易返回 `settlement_source_label` 字段。
9. 已只读打开默认库，确认 `meta.schema_version = 6`，且 `trades`、`market_rounds` 都存在 `settlement_source` 列。

### 回滚建议

1. 如需回滚本轮官方补偿核对，撤销 `storage.py`、`bot.py`、`README.md`、`tests/test_core.py` 和本进度文档中的 v2.34 改动。
2. SQLite 新增列不会影响旧代码读取；如必须彻底移除 `trades.settlement_source`，需要停服务、备份数据库后重建表，默认不建议。

## 2026-05-28 v2.32

### 已完成

1. `market_rounds` 新增 `settlement_source` 字段，schema version 升到 5。
2. 官方 Polymarket 结算路径记录 `settlement_source = polymarket_official`。
3. Chainlink 本地兜底结算路径记录 `settlement_source = chainlink_fallback`。
4. 官方 winner 结算时不再把 `target_price` 写入 `final_price` 作为占位；官方路径下没有真实最终 BTC 价时，`final_price` 保持 `NULL`。
5. 最近交易新增“结算来源”字段，默认展示 `Polymarket官方`、`Chainlink兜底`、`提前平仓` 或 `-`。
6. README 补充最近交易结算来源说明。
7. 补充测试，覆盖官方结算来源、官方路径 `final_price = NULL`、Chainlink 兜底来源和兜底 final price。

### 已确认决策

1. `结果 outcome` 继续优先信任 Polymarket 官方 winner。
2. 官方结果接口当前只提供 winner，不把目标价伪装成真实最终 BTC 价格。
3. Chainlink 兜底是 fallback，不等同官方真实结算；来源字段必须暴露给前端。
4. 提前平仓类最近交易没有市场结算来源，前端显示为“提前平仓”。

### 待办和后期优化

1. 如果后续找到官方 final BTC price 的稳定来源，可以在官方结算路径传入真实 `final_price`。
2. 历史 v2.32 之前已结算记录没有 `settlement_source`，只能显示为空或历史未记录，不能反推为官方或 Chainlink。

### 已知坑位

1. 本轮新增 SQLite 列，旧库会自动 `ALTER TABLE` 追加；不做破坏性迁移。
2. 官方结算记录的 `final_price` 为空是正确行为，表示没有官方最终 BTC 价，不是数据丢失。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_trade_settlement_updates_pnl_with_real_market_side tests.test_core.TradingCoreTest.test_chainlink_fallback_settlement_records_source_and_final_price`，2 个结算来源测试通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
3. 已执行 `rtk proxy python3 -m py_compile src/polybot2other/storage.py src/polybot2other/bot.py tests/test_core.py`，编译检查通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，28 个测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
6. 已执行 `rtk git diff --check`，未发现空白错误。
7. 已重启默认库服务 `http://127.0.0.1:8788`，首页返回 HTTP 200。
8. 已请求首页 HTML，确认脚本地址为 `/static/app.js?v=20260528-v2-32`。
9. 已只读打开默认库，确认 `meta.schema_version = 5`，且 `market_rounds` 存在 `settlement_source` 列。
10. 已请求 `/api/status`，确认 `running=True`，最近交易返回 `settlement_source_label` 字段；默认库历史记录首条来源为 `-`，符合 v2.32 前历史来源未记录的预期。

### 回滚建议

1. 如需回滚本轮结算来源能力，撤销 `storage.py`、`bot.py`、`static/app.js`、`static/index.html`、`README.md`、`tests/test_core.py` 和本进度文档中的 v2.32 改动。
2. SQLite 新增列不会影响旧代码读取；如必须彻底清理列，需要停服务、备份数据库后重建表，默认不建议。

## 2026-05-28 v2.30

### 已完成

1. 订单流水新增 `orderRenderKey`，订单数据、字段选择、分页状态、展开状态没有变化时不再重绘表格。
2. 自动轮询刷新时，如果用户正在订单流水区域选中文字、焦点在订单流水面板内，延后订单表格 DOM 替换，避免复制和选中被打断。
3. 必须重绘订单流水时会保留 `order-table-wrap` 的纵向和横向滚动位置。
4. 订单流水字段选择、展开逐档成交、撤单、切换筛选、查看更多等用户主动操作仍会强制刷新，保证操作反馈及时。
5. 新增 `selectionchange`、`focusout`、`mouseup` 后的延迟刷新检查，用户结束选择或焦点离开后会补渲染挂起的订单更新。
6. 首页脚本版本号升级到 `/static/app.js?v=20260528-v2-30`，避免浏览器继续使用旧脚本。

### 已确认决策

1. 本轮只优化前端渲染策略，不改变订单接口、订单状态和 Paper 执行逻辑。
2. 自动轮询不再无条件替换订单流水 DOM；用户主动动作仍优先立即生效。
3. 保护范围限定在订单流水面板，避免影响当前持仓、最近交易、行情和指标刷新。

### 待办和后期优化

1. 如果后续需要更细粒度，可以把订单流水改成按行 diff 更新，而不是整表更新。
2. 当前环境没有浏览器自动化工具，后续可补 Playwright 截图/DOM 坐标验证，覆盖复制文本和滚动保持。

### 已知坑位

1. 如果用户长时间保持订单流水里的文本选区，自动轮询数据会先进入内存状态，表格 DOM 会等选区释放后再补渲染。
2. 用户手动点击同步、撤单、查看更多、切换筛选时会强制刷新订单流水，这是预期行为。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
2. 已执行 `rtk git diff --check`，未发现空白错误。
3. 已请求首页 HTML，确认脚本地址为 `/static/app.js?v=20260528-v2-30`。
4. 已请求新版 app.js，确认存在 `orderRenderKey`、`isOrderInteractionActive`、`flushPendingOrderRender` 和 `forceOrder` 逻辑。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，27 个测试通过。
6. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。

### 回滚建议

1. 如需回滚本轮防闪烁逻辑，撤销 `static/app.js`、`static/index.html` 和本进度文档中的 v2.30 改动。
2. 本轮未修改数据库结构，无需迁移回滚。

## 2026-05-28 v2.29

### 已完成

1. 订单流水状态列改为 `状态码(中文含义)` 格式，例如 `FILLED(完全成交)`。
2. 新增前端状态展示映射：`RESTING(挂单中)`、`PARTIAL_RESTING(部分成交挂单)`、`FILLED(完全成交)`、`PARTIAL(部分成交)`、`CANCELED(已取消)`、`EXPIRED(已过期)`、`REJECTED(已拒绝)`。
3. 首页脚本增加版本参数 `/static/app.js?v=20260528-v2-29`，避免浏览器继续使用旧 JS。

### 已确认决策

1. 只改变前端展示，不改变 `/api/orders` 返回的原始 `status` 值。
2. 状态筛选仍继续使用原始状态分组，不受中文展示影响。
3. 本轮只作用于订单流水状态列，最近交易状态列保持原样。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
2. 已执行 `rtk git diff --check`，未发现空白错误。
3. 已请求首页 HTML，确认脚本地址为 `/static/app.js?v=20260528-v2-29`。
4. 已请求新版 app.js，确认存在 `ORDER_STATUS_LABELS` 和订单流水状态列 `orderStatusText(row.status)`。

### 回滚建议

1. 如需回滚本轮状态展示格式，撤销 `static/app.js`、`static/index.html` 和本进度文档中的 v2.29 改动。
2. 本轮未修改数据库结构，无需迁移回滚。

## 2026-05-28 v2.28

### 已完成

1. 订单流水表格容器新增 `order-table-wrap`，可视高度限制为约 4 条数据行，超出后在表格内部滚动查看。
2. 订单流水表头设置 sticky，内部滚动时字段头保持可见。
3. 订单流水原因列在该表内改为单行省略，避免长原因撑高行高导致 4 行可视高度失效。
4. 前端订单分页大小从 50 改为 20；超过 20 条时通过“查看更多”加载下一页。
5. 后端 `ORDERS_DEFAULT_LIMIT` 和 `/api/orders` 默认 limit 同步改为 20，避免 `/api/status` 首屏仍返回 50 条。
6. 首页 CSS 版本号升级到 `v2-28`，避免浏览器继续命中旧样式缓存。
7. README 中 `/api/orders` 示例 limit 更新为 20。

### 已确认决策

1. 可视高度按“约 4 条数据行 + 表头”控制，已加载的 20 条以内数据通过表格内部滚动查看。
2. 数据分页仍由后端负责，前端点击“查看更多”时按当前状态筛选继续请求下一页。
3. 默认库当前订单总数不足 20 时，“查看更多”隐藏是正确行为。

### 待办和后期优化

1. 如后续需要严格像素级 4 行，可根据真实浏览器截图再微调 `order-table-wrap` 的 `max-height`。
2. 后续可以给订单流水增加“当前已加载 20 条，每页 20 条”的更明确提示。

### 已知坑位

1. 展开逐档成交明细时，明细区域会占用表格内部滚动高度；这是可接受行为，不会把整个页面撑长。
2. 当前环境没有 Playwright/Chromium，未做截图级验证；已通过运行中 HTML/CSS 和接口返回验证。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
2. 已执行 `rtk proxy python3 -m py_compile src/polybot2other/bot.py src/polybot2other/web.py`，编译检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_bot_orders_page_and_order_fills_are_paginated tests.test_core.TradingCoreTest.test_orders_page_filters_by_paper_order_status`，2 个订单分页/筛选测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，27 个测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
6. 已执行 `rtk git diff --check`，未发现空白错误。
7. 已重启默认库服务 `http://127.0.0.1:8788`，首页返回 HTTP 200。
8. 已请求首页 HTML，确认 CSS 链接为 `/static/styles.css?v=20260528-v2-28`，订单流水容器为 `table-wrap order-table-wrap`。
9. 已请求新版 CSS，确认存在 `.order-table-wrap { max-height: 226px; overflow: auto; }`、sticky 表头和订单原因列单行规则。
10. 已请求 `/api/status`，确认 `recent_orders_meta.limit = 20`。
11. 已请求 `/api/orders?offset=0&status=all`，确认默认 `limit = 20`。

### 回滚建议

1. 如需回滚本轮 UI/分页调整，撤销 `static/index.html`、`static/styles.css`、`static/app.js`、`bot.py`、`web.py`、`README.md` 和本进度文档中的 v2.28 改动。
2. 本轮未修改数据库结构，无需迁移回滚。

## 2026-05-28 v2.24

### 已完成

1. `/api/orders` 新增 `status` 查询参数，支持 `all`、`active`、`filled`、`canceled`、`expired`、`rejected`。
2. `TradeStore.recent_paper_orders()` 和 `paper_order_count()` 增加服务端状态筛选，避免只过滤当前前端页导致漏看历史活跃挂单。
3. `PaperTradingBot.orders_page()` 返回 `recent_orders_meta.status_filter`，前端可识别当前分页对应的筛选条件。
4. 前端订单流水新增状态筛选下拉框；切换筛选时会清空旧分页缓存并重新请求后端。
5. 单笔撤单和批量撤单后会按当前筛选条件重新加载订单页，避免 active 筛选下继续显示已取消订单。
6. README 补充 `/api/orders` 的 `status` 参数和取值。
7. 补充测试，覆盖 active、canceled、filled 三类筛选和非法筛选值。

### 已确认决策

1. 状态筛选必须在后端 SQL 层完成，不能只靠前端过滤当前已加载行。
2. `active` 映射到 `RESTING` / `PARTIAL_RESTING`，对应仍有剩余挂单资金或等待撮合的订单。
3. `filled` 同时包含 `FILLED` 和 `PARTIAL`；当前 Paper 里的 FAK 部分成交属于终态成交审计，不属于 active。
4. `/api/status` 继续返回默认 all 首屏订单；当前端选择非 all 筛选时，前端会额外请求 `/api/orders?status=<filter>` 保持筛选视图准确。

### 待办和后期优化

1. 增加市场、方向和订单类型筛选。
2. 增加 maker 队列位置和排队成交概率模拟。
3. 实盘设计时需要把本地筛选和交易所订单状态同步结果区分开，避免把本地状态当作交易所最终状态。

### 已知坑位

1. 当前状态筛选只覆盖本地 Paper 订单状态，不代表真实 CLOB 订单状态。
2. 前端在非 all 筛选下会额外请求 `/api/orders`，状态轮询时多一次轻量查询；当前订单规模下影响可接受。
3. `filled` 包含 FAK 部分成交终态，名称偏审计视角；后续如果加真实 live 订单，需要单独区分部分成交后仍在挂单的状态。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_orders_page_filters_by_paper_order_status tests.test_core.TradingCoreTest.test_bot_orders_page_and_order_fills_are_paginated`，2 个订单分页/筛选测试通过。
2. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
3. 已执行 `rtk proxy python3 -m py_compile src/polybot2other/storage.py src/polybot2other/bot.py src/polybot2other/web.py tests/test_core.py`，编译检查通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，27 个测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
6. 已执行 `rtk git diff --check`，未发现空白错误。
7. 已重启默认库服务 `http://127.0.0.1:8788`，首页返回 HTTP 200。
8. 已请求 `/api/status`，确认 `running=True`、`metrics.reserved_cash = 0.0`、`settings.paper_entry_order_type = FAK`、`recent_orders_meta.status_filter = all`。
9. 已请求 `/api/orders?limit=5&offset=0&status=active`，返回 `recent_orders_meta.status_filter = active`，默认库当前 active 订单数为 0。
10. 已请求 `/api/orders?status=bad`，返回 HTTP 400，确认非法筛选值不会静默降级。

### 回滚建议

1. 如需回滚本轮状态筛选，撤销 `storage.py`、`bot.py`、`web.py`、`static/index.html`、`static/app.js`、`static/styles.css`、`README.md`、`tests/test_core.py` 和本进度文档中的 v2.24 改动。
2. 本轮未修改数据库结构，无需迁移回滚。

## 2026-05-28 v2.23

### 已完成

1. 新增 Paper 批量撤单接口 `POST /api/cancel-orders`，请求体支持 `{"scope":"current_market"}` 和 `{"scope":"all"}`。
2. `TradeStore.cancel_active_paper_orders()` 支持按 symbol 和可选 round_id 取消 `RESTING` / `PARTIAL_RESTING` 活跃挂单。
3. 批量取消会把订单状态改为 `CANCELED`，清零 `remaining_cash`，汇总释放预留资金回 `cash_balance`，并返回 `canceled`、`not_canceled`、`released_cash` 和订单快照。
4. `PaperTradingBot.cancel_orders()` 封装当前市场和全部活跃挂单两个作用域，并在取消后刷新订单分页。
5. 前端订单流水新增“取消当前市场”和“取消全部挂单”入口，点击前弹出确认；按钮会根据当前状态和预留资金自动禁用。
6. README 补充批量撤单 API。
7. 补充测试，覆盖当前市场批量取消不会误取消其他市场，全部取消会释放剩余挂单资金。

### 已确认决策

1. `scope=current_market` 只取消当前 `current_market.round_id` 对应的活跃 Paper 挂单。
2. `scope=all` 取消所有 BTC Paper 活跃挂单，等价于本地 Paper 的 cancel all。
3. 批量取消仍只影响本地 Paper 数据，不调用真实 CLOB 取消接口，不需要签名或 L2 认证。
4. 前端禁用按钮只做交互提示；后端仍以存储层活跃订单状态为最终判断，避免分页未加载完整时漏取消。

### 待办和后期优化

1. 增加订单状态筛选，只看 RESTING/PARTIAL_RESTING 活跃挂单。
2. 增加 maker 队列位置和排队成交概率模拟。
3. 实盘设计时需要处理真实取消部分失败、订单已成交但本地未同步、网络超时后状态不确定等竞态。

### 已知坑位

1. 当前批量取消是本地 Paper 状态变更，不代表真实 Polymarket CLOB 已取消。
2. 前端按钮根据最近订单页和 `reserved_cash` 判断是否可点；如果历史页里有很老的活跃挂单，按钮可点但具体取消范围仍以后端为准。
3. 当前市场不可用时，`scope=current_market` 会返回空取消结果和 `not_canceled.scope`，不会猜测市场。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_cancel_orders_scopes_current_market_and_all_active_orders`，1 个批量撤单作用域测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_cancel_resting_order_releases_reserved_cash tests.test_core.TradingCoreTest.test_cancel_orders_scopes_current_market_and_all_active_orders tests.test_core.TradingCoreTest.test_post_only_rests_reserves_cash_and_later_fills_as_maker tests.test_core.TradingCoreTest.test_gtd_resting_order_expires_and_releases_reserved_cash`，4 个挂单取消/生命周期测试通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已执行 `rtk proxy python3 -m py_compile src/polybot2other/storage.py src/polybot2other/bot.py src/polybot2other/web.py tests/test_core.py`，编译检查通过。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，26 个测试通过。
6. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
7. 已执行 `rtk git diff --check`，未发现空白错误。
8. 已重启默认库服务 `http://127.0.0.1:8788`，首页返回 HTTP 200。
9. 已请求 `/api/status`，确认 `running=True`、`metrics.reserved_cash = 0.0`、`settings.paper_entry_order_type = FAK`。
10. 已在默认库 `reserved_cash = 0.0` 的前提下请求 `/api/cancel-orders`，返回 `scope=current_market`、`canceled=[]`、`not_canceled={}`、`released_cash=0.0`。

### 回滚建议

1. 如需回滚本轮批量撤单能力，撤销 `storage.py`、`bot.py`、`web.py`、`static/index.html`、`static/app.js`、`static/styles.css`、`README.md`、`tests/test_core.py` 和本进度文档中的 v2.23 改动。
2. 本轮未修改数据库结构，无需迁移回滚。

## 2026-05-28 v2.22

### 已完成

1. 新增 Paper 手动撤单接口 `POST /api/cancel-order`，请求体为 `{"order_id": <id>}`。
2. `TradeStore.cancel_paper_order()` 只允许取消 `RESTING` / `PARTIAL_RESTING` 订单，取消后状态改为 `CANCELED`，`remaining_cash` 清零，并释放预留资金回 `cash_balance`。
3. 取消接口返回结构对齐 Polymarket 取消语义：包含 `canceled` 和 `not_canceled`；不可取消订单会给出原因。
4. `PaperTradingBot.cancel_order()` 封装取消后刷新订单分页，方便前端立即更新订单流水。
5. 前端订单流水新增“取消”操作，仅对 `RESTING` / `PARTIAL_RESTING` 显示按钮。
6. 点击取消按钮不会触发行展开；取消成功后刷新状态、订单流水和挂单预留指标。
7. README 补充本地 Paper 取消接口。
8. 补充测试，覆盖取消 RESTING 订单释放预留资金、重复取消返回 `not_canceled`。

### 已确认决策

1. 本地 Paper 取消使用 `POST /api/cancel-order`，不调用真实 CLOB DELETE 接口，不需要 L2 认证。
2. 只有 active resting order 可以取消；FILLED、EXPIRED、CANCELED、REJECTED 等终态不会被二次修改。
3. 取消释放的是 `remaining_cash`，已成交部分不会回滚，符合部分成交后取消剩余挂单的语义。

### 待办和后期优化

1. 增加批量取消当前市场/全部挂单能力，对齐官方 cancel market / cancel all 能力。
2. 对前端取消按钮增加确认弹层，避免误点。
3. 增加状态筛选，只看 RESTING/PARTIAL_RESTING 活跃挂单。
4. 后续实盘设计必须处理真实取消失败、认证失败、订单已成交但本地未同步等竞态。

### 已知坑位

1. 当前取消是本地 Paper 状态更新，不代表真实 CLOB 取消。
2. 如果取消点击和行情触发 fill 在同一个 tick 附近发生，当前由本地存储锁决定先后顺序；真实实盘还需要用交易所返回状态做最终仲裁。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_cancel_resting_order_releases_reserved_cash tests.test_core.TradingCoreTest.test_post_only_rests_reserves_cash_and_later_fills_as_maker tests.test_core.TradingCoreTest.test_gtd_resting_order_expires_and_releases_reserved_cash`，3 个挂单取消/生命周期测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m py_compile src/polybot2other/storage.py src/polybot2other/bot.py src/polybot2other/web.py tests/test_core.py`，编译检查通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，25 个测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
6. 已执行 `rtk git diff --check`，未发现空白错误。
7. 已重启默认库服务 `http://127.0.0.1:8788`，首页返回 HTTP 200。
8. 已请求 `/api/status`，确认 `running=True`、`metrics.reserved_cash = 0.0`。
9. 已请求 `/api/cancel-order` 取消默认库中的非活跃订单，返回 `canceled=[]` 且 `not_canceled` 包含该订单 ID，确认不会误改终态订单。

### 回滚建议

1. 如需撤销本轮取消订单改动，恢复 `src/polybot2other/storage.py`、`src/polybot2other/bot.py`、`src/polybot2other/web.py`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`tests/test_core.py`、`README.md` 和本进度文档顶部 `2026-05-28 v2.22` 记录。

## 2026-05-28 v2.21

### 已完成

1. 扩展 Paper 订单类型：`POLYBOT2OTHER_PAPER_ENTRY_ORDER_TYPE` 支持 `FAK`、`POST_ONLY`、`GTC`、`GTD`。
2. 新增 Paper resting order 生命周期：POST_ONLY/GTC/GTD 下单后进入 `RESTING`，不会立即生成持仓。
3. `paper_orders` 增加挂单字段：`post_only`、`expires_at`、`reserved_cash`、`remaining_cash`、`confidence`、`move_bps`；schema version 升到 4。
4. RESTING/PARTIAL_RESTING 订单会预留现金，`cash_balance` 代表可用资金，`reserved_cash` 代表挂单占用资金。
5. 账户总资产口径修正为 `cash_balance + open_risk + reserved_cash`，避免挂单预留导致总资产假性下降。
6. 策略开 POST_ONLY/GTC/GTD 时使用非 marketable maker 限价，优先挂在 best bid；若会吃到卖一则向下避让。
7. 行情更新时会扫描 RESTING/PARTIAL_RESTING 订单；当当前 ask 穿过挂单限价时，按 maker fill 模拟成交，手续费记为 0。
8. GTD 按 `POLYBOT2OTHER_PAPER_GTD_SECONDS` 到期；GTC/POST_ONLY 最晚在市场结束时过期。
9. 订单到期会更新为 `EXPIRED` 并释放 `remaining_cash`。
10. UI 顶部新增“挂单预留”指标。
11. README 补充 `POLYBOT2OTHER_PAPER_GTD_SECONDS`。
12. 补充测试，覆盖 POST_ONLY 预留现金后 maker 成交、GTD 到期释放现金。

### 已确认决策

1. 本轮只模拟 BUY 侧 maker 挂单；当前项目策略没有 SELL 开仓路径。
2. POST_ONLY 在本地作为 “GTC + post-only flag” 的便捷模式记录，符合官方 post-only 只能和 GTC/GTD 组合使用的约束。
3. Maker fill 暂按 0 fee 处理；maker rebate 暂未计入收益，避免把 Paper 做得过于乐观。
4. GTC 在 5 分钟市场结束时过期，避免有限市场结束后残留无效挂单。

### 待办和后期优化

1. 增加显式撤单接口和前端按钮，把手动取消记录为 `CANCELED` 并释放预留资金。
2. 增加 maker queue 模型，避免“ask 穿过限价就立即满额成交”的乐观假设。
3. 增加 maker rebate 参数和开关，默认仍建议关闭或单独展示。
4. 支持 POST_ONLY + GTD 的组合配置，而不是当前 `POST_ONLY` 固定近似为 GTC post-only。

### 已知坑位

1. 当前 maker 成交用 orderbook 快照判断，仍没有真实队列位置、前序挂单、网络延迟和撮合竞争。
2. RESTING 部分成交会生成独立 trade；同一个 order 多次部分成交时，`paper_fills` 可逐档追踪，但 `paper_orders.trade_id` 只保留第一笔关联 trade。
3. GTD 的真实 Polymarket wire expiration 有额外安全阈值；Paper 里用 `POLYBOT2OTHER_PAPER_GTD_SECONDS` 表示本地有效生命周期。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_post_only_rests_reserves_cash_and_later_fills_as_maker tests.test_core.TradingCoreTest.test_gtd_resting_order_expires_and_releases_reserved_cash tests.test_core.TradingCoreTest.test_bot_orders_page_and_order_fills_are_paginated`，3 个挂单生命周期相关测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，24 个测试通过。
5. 已执行 `rtk git diff --check`，未发现空白错误。
6. 已重启默认库服务 `http://127.0.0.1:8788`，首页返回 HTTP 200。
7. 已只读查询默认 SQLite，确认 `meta.schema_version = 4`，且 `paper_orders` 已存在 `post_only`、`expires_at`、`reserved_cash`、`remaining_cash`、`confidence`、`move_bps` 列。
8. 已请求 `/api/status`，确认 `running=True`、`settings.paper_gtd_seconds = 90.0`、`metrics.reserved_cash = 0.0`。

### 回滚建议

1. 如需撤销本轮挂单生命周期改动，恢复 `src/polybot2other/execution.py`、`src/polybot2other/config.py`、`src/polybot2other/storage.py`、`src/polybot2other/bot.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`tests/test_core.py`、`README.md` 和本进度文档顶部 `2026-05-28 v2.21` 记录。
2. 已迁移到 schema version 4 的 SQLite 追加列可保留，不影响 FAK 路径；如确需清理，先停服务并备份数据库。

## 2026-05-28 v2.20

### 已完成

1. 新增 `/api/orders?limit=50&offset=0`，支持 Paper 订单流水独立分页，返回 `recent_orders` 和 `recent_orders_meta`。
2. 新增 `/api/order-fills?order_id=<id>`，按订单 ID 返回该订单的逐档 `paper_fills` 明细。
3. `PaperTradingBot` 新增 `orders_page()` 和 `order_fills()`，复用存储层分页和逐档成交查询。
4. `TradeStore.recent_paper_orders()` 支持 offset，新增 `paper_order_count()`，避免前端只能依赖 `/api/status` 的固定 50 条。
5. 前端订单流水改为独立分页状态：支持“查看更多”，并显示订单总数/已加载数量。
6. 前端订单行支持点击展开，按需请求 `/api/order-fills` 展示逐档成交价格、份额、现金花费和 fee。
7. README 补充订单分页和逐档 fill 查询接口。
8. 补充测试，覆盖订单分页元数据和逐档 fill 查询。

### 已确认决策

1. `/api/status` 继续返回首屏最近订单，保持现有页面兼容；前端会同时使用 `/api/orders` 做独立分页。
2. 逐档 fill 明细按需加载，不放进 `/api/status`，避免页面轮询时 payload 持续变大。
3. 本轮不改变撮合逻辑，不新增订单状态机，只补审计查询和展示。

### 待办和后期优化

1. 实现 POST_ONLY/GTC/GTD resting order 状态机：挂单、排队、部分成交、撤单、过期。
2. 给 `/api/orders` 增加状态、市场、方向筛选。
3. 增加订单详情中的滑点汇总，例如 best ask、avg fill、limit、fee bps。
4. 对前端展开态增加键盘可访问性和更明确的点击区域。

### 已知坑位

1. 点击整行展开订单明细，当前还没有单独的“详情”按钮；如果后续表格可编辑，需要避免点击冲突。
2. 当前逐档明细只展示已成交订单；REJECTED/CANCELED/RESTING 订单没有 fill 明细是正常结果。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_storage_records_paper_order_and_each_fill_level tests.test_core.TradingCoreTest.test_storage_records_rejected_order_without_open_trade tests.test_core.TradingCoreTest.test_bot_orders_page_and_order_fills_are_paginated`，3 个订单接口相关测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m py_compile src/polybot2other/web.py src/polybot2other/bot.py src/polybot2other/storage.py tests/test_core.py`，编译检查通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，22 个测试通过。
5. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
6. 已执行 `rtk git diff --check`，未发现空白错误。
7. 已重启默认库服务 `http://127.0.0.1:8788`，首页返回 HTTP 200。
8. 已请求 `/api/status`，确认 `running=True` 且返回 `recent_orders`。
9. 已请求 `/api/orders?limit=5&offset=0`，返回 HTTP 200，`recent_orders_meta.total = 1`。
10. 已请求 `/api/order-fills?order_id=1`，返回 HTTP 200，`fills` 数量为 1。

### 回滚建议

1. 如需撤销本轮订单分页和展开明细改动，恢复 `src/polybot2other/storage.py`、`src/polybot2other/bot.py`、`src/polybot2other/web.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`tests/test_core.py`、`README.md` 和本进度文档顶部 `2026-05-28 v2.20` 记录。

## 2026-05-28 v2.19

### 已完成

1. 新增 `paper_orders` 表，记录 Paper 下单尝试：市场、方向、订单类型、状态、限价、预算、成交均价、成交份额、notional、fee、现金花费、关联 trade、原因和时间。
2. 新增 `paper_fills` 表，记录每个 Paper 订单的逐档成交：档位序号、价格、份额、notional、fee、现金花费。
3. `TradeStore.place_execution_result()` 统一处理 FAK/POST_ONLY 执行结果：有 fill 时写持仓、订单和逐档成交；无 fill 时也记录 REJECTED/CANCELED/RESTING 订单。
4. 普通策略开仓改为通过 `place_execution_result()` 入库，避免无成交订单丢失审计记录。
5. 配对策略的双腿 FAK fill 会分别写入 `paper_orders` / `paper_fills`，每腿记录各自真实现金花费。
6. `/api/status` 返回 `recent_orders`，前端新增“订单流水”表展示订单类型、状态、限价、预算、均价、成交份额、花费、fee、成交档和原因。
7. README 补充 Paper 订单和逐档成交会独立存储。
8. 补充测试，覆盖逐档 fill 持久化、被拒订单无持仓但有订单记录。

### 已确认决策

1. 本轮只追加新表，不修改旧 `trades` 表结构，不重算历史 Paper 交易。
2. `trades` 继续作为账户持仓和盈亏主表；`paper_orders` / `paper_fills` 作为执行质量审计表。
3. 旧历史交易不会反向生成订单流水，因为旧记录缺少原始 orderbook 和逐档 fill 证据。

### 待办和后期优化

1. 增加 `/api/orders` 独立分页接口，避免长期运行后 `/api/status` 返回过多订单。
2. 对 `paper_orders` 增加撤单、过期、POST_ONLY 后续成交等状态更新能力。
3. UI 增加订单详情展开，展示每一档 `paper_fills`，而不是只显示 `fill_count`。
4. 进一步模拟 tick size、min order size、接口拒单和网络延迟。

### 已知坑位

1. 订单流水从 v2.19 之后的新 Paper 执行开始记录；之前默认库里的老交易不会有对应 `paper_orders`。
2. `paper_fills` 已持久化逐档成交，但当前 UI 只展示订单级汇总和成交档数，逐档明细需要后续详情视图或接口查看。
3. POST_ONLY 仍未实现真实 resting queue，本轮只保证无成交订单也会有审计记录。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_storage_records_paper_order_and_each_fill_level tests.test_core.TradingCoreTest.test_storage_records_rejected_order_without_open_trade tests.test_core.TradingCoreTest.test_bot_fak_entry_records_fee_in_open_risk`，3 个订单持久化相关测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，21 个测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
4. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
5. 已执行 `rtk git diff --check`，未发现空白错误。
6. 已重启默认库服务 `http://127.0.0.1:8788`，首页返回 HTTP 200，`/api/status` 返回 `running=True`。
7. 已只读查询默认 SQLite，确认 `paper_orders` / `paper_fills` 表已创建，`meta.schema_version = 3`；旧历史交易未反向生成订单流水，当前新表计数为 0。

### 回滚建议

1. 如需撤销本轮订单流水改动，恢复 `src/polybot2other/models.py`、`src/polybot2other/execution.py`、`src/polybot2other/storage.py`、`src/polybot2other/bot.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`tests/test_core.py`、`README.md` 和本进度文档顶部 `2026-05-28 v2.19` 记录。
2. 已创建过的 SQLite 追加表可以保留不影响旧逻辑；如确需清理，必须先停服务并备份数据库后再处理。

## 2026-05-28 v2.18

### 已完成

1. REST CLOB quote 保留排序后的 `bids` / `asks` 多档深度，不再只留下 best bid / best ask。
2. 浏览器 market WebSocket 的 `book` 消息会把前 50 档 `bids` / `asks` 随 live snapshot 上报给后端。
3. FAK 买入模拟改为按 ask 多档逐档成交，逐档扣 taker fee，并用加权均价写入 Paper 持仓。
4. 普通策略 FAK 限价改为使用 `confidence - min_edge` 与 `max_entry_price` 推导的优势保护限价；只有在限价允许时才会从 0.34 继续吃到更高 ask 档位。
5. 配对策略开仓改为按双腿多档深度计算最大等量 shares，并以含费净成本 `< 1.0` 作为正毛边保护。
6. Paper 持仓 reason 记录多档成交均价、成交层数、限价、notional 和 fee，便于排查滑点。
7. README 更新 Paper 执行模型说明。
8. 补充测试，覆盖多档 FAK、bot 多档均价入场、best-only snapshot 触发 REST 深度补全、REST quote 多档排序保留。

### 已确认决策

1. 本轮不修改数据库结构，不新增 `paper_orders` / `paper_fills` 表；成交细节仍聚合到现有 trade reason 中。
2. 多档深度只使用官方 CLOB REST 或 market WebSocket 实际提供的 `asks`，不根据 best ask 人工虚构深度。
3. FAK 仍然是限价单模拟，不是无上限 market buy；如果限价不允许，价格不会吃穿到更高档。
4. WebSocket 上报和 REST quote 暂时保留前 50 档，避免 live snapshot payload 过大。

### 待办和后期优化

1. 增加 `paper_orders` / `paper_fills` 独立表，记录逐档 fill、取消、过期和 order lifecycle；这涉及数据库结构变更，需要单独确认。
2. 增加 UI 展示：成交均价、成交层数、limit、fee、order status、滑点。
3. 增加真实下单前的 tick size、min order size、min size、接口拒单原因模拟。
4. 如果后续要更贴近 maker-first 报告策略，需要完整实现 POST_ONLY/GTC/GTD 挂单队列和未成交率统计。

### 已知坑位

1. 当前多档 FAK 是按快照 orderbook 模拟，仍不包含网络延迟期间盘口变化、撮合竞争和接口失败。
2. 多档 fill 仍聚合为一笔 trade；没有独立 fill 表前，无法精确复盘每一档成交。
3. 普通策略会在保住 `min_edge` 的前提下允许吃更高 ask 档；这比之前更接近 marketable limit，但也会让新 Paper 成交均价高于页面卖一。
4. POST_ONLY 仍只做 marketable 拒单，不持久化 resting order。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_fak_execution_walks_multiple_ask_levels_when_limit_allows tests.test_core.TradingCoreTest.test_bot_fak_entry_uses_multi_level_average_with_edge_limit tests.test_core.TradingCoreTest.test_polymarket_quote_keeps_sorted_orderbook_levels`，3 个多档订单簿相关测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_bot_fetches_rest_depth_when_snapshot_has_only_best_ask tests.test_core.TradingCoreTest.test_bot_fak_entry_uses_multi_level_average_with_edge_limit`，2 个深度补全相关测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，19 个测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
5. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
6. 已执行 `rtk git diff --check`，未发现空白错误。
7. 已重启默认库服务 `http://127.0.0.1:8788`，首页返回 HTTP 200，`/api/status` 返回 `running=True`、`paper_entry_order_type=FAK`。
8. 已用 `PolymarketClient.get_quotes()` 实测当前 BTC 5m 市场 CLOB REST 深度，Up/Down 都能返回多档 `asks` / `bids`。

### 回滚建议

1. 如需撤销本轮多档 orderbook 改动，恢复 `src/polybot2other/execution.py`、`src/polybot2other/polymarket.py`、`src/polybot2other/bot.py`、`src/polybot2other/static/app.js`、`tests/test_core.py`、`README.md` 和本进度文档顶部 `2026-05-28 v2.18` 记录。

## 2026-05-27 v2.14

### 已完成

1. 新增 `execution.py` 纸面执行层，支持 FAK 买入模拟、POST_ONLY marketable 拒单判断、Crypto taker fee 计算。
2. 普通方向策略不再把信号直接写成持仓，而是先走纸面执行；只有 FAK 实际产生 fill 后才写入 OPEN trade。
3. FAK 成交会受当前卖一价、卖一深度、预算和 taker fee 约束；深度不足时会部分成交，剩余自动取消。
4. 持仓 `stake` 改为记录实际现金成本，包含成交 notional 和 taker fee；`shares` 因 fee 影响会低于 `stake / ask` 的乐观结果。
5. 配对策略开仓按双腿 ask、双腿可见 ask size 和双腿 taker fee 计算等量份额；reason 中记录 gross cost、net cost、fee 和 edge。
6. 配对提前平仓、尾盘强平和残余仓位处理会按 bid 侧 taker fee 扣减回款。
7. 新增配置项 `POLYBOT2OTHER_PAPER_ENTRY_ORDER_TYPE` 和 `POLYBOT2OTHER_PAPER_TAKER_FEE_RATE`；默认 `FAK` 和 `0.07`。
8. README 补充 Paper 执行模型和新增配置。
9. 补充回归测试，覆盖 FAK fee/部分成交、POST_ONLY marketable 拒单、bot FAK 成交持仓含 fee、配对开平仓 reason 含 fee。

### 已确认决策

1. 当前仍然只做 paper，不接私钥、不签名、不发真实订单。
2. 第一阶段先用 top-of-book ask/bid size 做执行模拟，不伪造不存在的多档深度。
3. FAK 是默认模拟模式，用于让 Paper 收益先扣除 taker fee 和可见深度限制。
4. POST_ONLY 当前只模拟“会立即成交则拒单”的安全边界，暂不持久化 resting order 队列。

### 待办和后期优化

1. 增加 `paper_orders` / `paper_fills` 独立表，记录订单生命周期、部分成交、取消和过期；这会涉及数据库结构变更，需要单独确认。
2. 接入多档 orderbook 深度，按多档价格计算滑点，而不是只看 top-of-book。
3. 完整实现 POST_ONLY/GTC/GTD resting order 队列和撮合更新。
4. 对配对策略增加双腿原子性控制，避免一腿成交后一腿失败形成过大残余。
5. UI 增加 fee、order type、fill status、net cost 等展示字段。

### 已知坑位

1. 当前 FAK 只使用可见卖一/买一档位，不能模拟 0.34 吃穿多档到 0.45 的完整滑点；多档深度接入前仍然偏保守但不完整。
2. POST_ONLY 不会生成持久挂单，因此还不能评估 maker-first 的真实未成交率和队列位置。
3. `POLYBOT2OTHER_PAPER_TAKER_FEE_RATE=0.07` 来自当前 Polymarket Crypto fee 文档；实盘前必须按具体 market 的 `feesEnabled` 和 fee params 再确认。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_fak_execution_charges_fee_and_caps_by_top_ask_size tests.test_core.TradingCoreTest.test_post_only_marketable_order_rejects_instead_of_taking_liquidity tests.test_core.TradingCoreTest.test_bot_fak_entry_records_fee_in_open_risk tests.test_core.TradingCoreTest.test_pair_strategy_opens_two_sides_and_exits_on_bid_sum`，4 个执行层相关测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，15 个测试通过。
5. 已执行 `rtk git diff --check`，未发现空白错误。
6. 已用 `/tmp/polybot2other-v214.sqlite3` 启动隔离服务 `http://127.0.0.1:8788`，首页返回 HTTP 200。
7. 已请求 `http://127.0.0.1:8788/api/status`，确认 `settings.paper_entry_order_type = FAK`、`settings.paper_taker_fee_rate = 0.07`，且实际 Paper 持仓 reason 出现 `FAK FILLED` 和 `fee`。

### 回滚建议

1. 如需撤销本轮实盘接近化改动，恢复 `src/polybot2other/execution.py`、`src/polybot2other/models.py`、`src/polybot2other/config.py`、`src/polybot2other/storage.py`、`src/polybot2other/bot.py`、`tests/test_core.py`、`README.md` 和本进度文档顶部 `2026-05-27 v2.14` 记录。

## 2026-05-27 v2.13

### 已完成

1. 实时市场目标价展示改为优先使用 `market.target_price`，只有官方目标价缺失时才显示前端 `priceState.target_price` 兜底值。
2. `applyMarket()` 支持同一 `slug` 下继续更新 `target_price`、`question`、`end_ts`、token 等市场字段，不再因为 slug 相同直接跳过。
3. RTDS Chainlink 首条价格如果被用于目标价兜底，会标记 `target_price_source = rtds-chainlink-fallback` 和 `target_price_fallback = true`。
4. 官方 `market.target_price` 一旦出现，会覆盖前端 fallback，并将 `target_price_fallback` 置为 `false`。
5. 浏览器上报 `/api/live-snapshot` 时只发送官方 `market.target_price`，不会把 RTDS fallback 目标价作为交易目标价传给后端。
6. 后端移除“从前端 payload 补齐 `market.target_price`”逻辑；如果后端未拿到官方目标价，会清理 payload 中的前端目标价字段。
7. 配对策略新增开仓保护：缺少官方目标价时不新开仓，避免目标价不可验证时产生交易决策。
8. 补充回归测试，覆盖前端 fallback 不能升级成后端 market target、官方目标价覆盖前端 fallback、配对策略缺少官方目标价不新开仓。

### 已确认决策

1. 交易决策只信任后端确认过的官方 `market.target_price`。
2. RTDS Chainlink fallback 只允许用于当前浏览器展示，不允许进入后端策略开仓逻辑。
3. 同一个 Polymarket slug 下，市场详情仍可能补齐或刷新，前端必须接受后端更新。

### 待办和后期优化

1. 后续可以在实时市场详情中单独展示目标价来源，方便肉眼区分 `market.target_price` 和 `rtds-chainlink-fallback`。
2. 如果 Polymarket 页面结构再次变化，需要继续增强官方目标价解析来源，而不是放宽前端 fallback 入场。

### 已知坑位

1. fallback 目标价只保证页面短时可读，不代表官方目标价；没有官方目标价时普通策略和配对策略都不应新开仓。
2. 当前前端没有独立单测框架，前端逻辑通过 `node --check` 和代码审计验证，交易安全边界由后端单测兜底。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest tests.test_core.TradingCoreTest.test_live_snapshot_does_not_promote_client_fallback_target_to_market_target tests.test_core.TradingCoreTest.test_live_snapshot_uses_official_market_target_over_client_fallback tests.test_core.TradingCoreTest.test_pair_strategy_does_not_open_without_official_target`，3 个目标价回归测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，12 个测试通过。
5. 已执行 `rtk git diff --check`，未发现空白错误。
6. 已用 `/tmp/polybot2other-v213.sqlite3` 启动隔离服务 `http://127.0.0.1:8788`，首页返回 HTTP 200。
7. 已请求 `http://127.0.0.1:8788/api/status`，确认 `latest_price.target_price_source = market.target_price` 且 `target_price_fallback = false`。

### 回滚建议

1. 如需撤销本轮目标价修复，恢复 `src/polybot2other/static/app.js`、`src/polybot2other/bot.py`、`tests/test_core.py` 和本进度文档顶部 `2026-05-27 v2.13` 记录。

## 2026-05-27 v2.12

### 已完成

1. 调整顶部账户指标口径：`total_equity` 和 `total_pnl` 保持已结算/成本口径，不再被当前持仓按买一价的未实现盈亏覆盖。
2. 保留当前持仓盘口估值字段：`open_mark_value`、`unrealized_pnl`、`estimated_total_equity`、`estimated_total_pnl`，用于观察浮动风险。
3. 顶部账户指标新增“未实现盈亏”卡片，单独展示当前持仓按可退出买一价估算的浮盈浮亏。
4. 前端指标网格从 6 列扩展为 7 列，保留原有数值滚动动画。
5. 补充测试，验证当前持仓浮亏不会进入 `total_equity` 和 `total_pnl`，但会进入 `unrealized_pnl` 和预估字段。

### 已确认决策

1. `总盈亏` 只代表已结算盈亏，不含未实现盈亏。
2. `总资产` 使用 `cash_balance + open_risk` 的成本口径，不按盘口浮动实时增减。
3. 未实现盈亏仍然保留并突出展示，避免隐藏当前持仓风险。

### 待办和后期优化

1. 如果后续希望同时看盘口估值，可在顶部再增加“预估总资产”，但不建议替代当前“总资产”。
2. 可以给“总资产”“总盈亏”“未实现盈亏”增加 tooltip，明确解释统计口径。

### 已知坑位

1. 当前资金曲线继续基于 `equity_curve.total_equity`，也就是已结算/成本口径，不展示盘口未实现盈亏的实时波动。
2. 未实现盈亏依赖当前市场买一价；如果盘口缺失，会按持仓本金兜底计算，不会强行制造浮动盈亏。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，9 个测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
4. 已重启 `http://127.0.0.1:8787` 服务。
5. 已执行 `/api/status`，确认 `total_pnl = realized_pnl`，`unrealized_pnl` 单独返回，`estimated_total_pnl` 保留盘口估值口径。
6. 已检查首页和静态脚本，确认新增 `unrealized-pnl` 指标节点和前端渲染绑定。

### 回滚建议

1. 如需撤销本轮资金口径调整，恢复 `src/polybot2other/bot.py`、`src/polybot2other/static/app.js`、`src/polybot2other/static/index.html`、`src/polybot2other/static/styles.css`、`tests/test_core.py` 和本进度文档。

## 2026-05-27 v2.9

### 已完成

1. 新增近 90 天资金曲线接口 `/api/equity-curve?days=90&max_points=1200`。
2. 后端按时间窗口查询 `equity_curve`，并在数据量超过上限时按时间顺序降采样，避免 3 个月高频样本拖慢页面。
3. 资金曲线点位补充 `total_pnl` 和 `total_pnl_pct`，前端悬浮时可以展示该点总资产、总盈亏、盈利/亏损状态、可用资金和持仓风险。
4. 前端资金曲线独立拉取 90 天数据，保留 `/api/status` 轻量轮询；状态接口只继续携带最近短窗口样本用于补齐最新点。
5. 资金曲线增加鼠标悬浮十字线、圆点和 tooltip。
6. 资金曲线卡片内容区上下各保留 `15px`，曲线 canvas 放在 `chart-body` 内，避免贴边不协调。
7. 总资产、总盈亏、可用资金、持仓风险、胜率、最大回撤改为数值滚动过渡，避免指标变动时生硬跳变。

### 已确认决策

1. 3 个月历史曲线不塞进 `/api/status`，用独立接口承载，降低 2 秒轮询压力。
2. 曲线“丝滑”只改变绘制路径、圆角和面积渐变，不对真实资金数据做数学平滑，避免误导盈亏判断。
3. tooltip 显示的是当前采样点的具体数值；当历史样本很多时，该点可能是降采样后的代表点。

### 待办和后期优化

1. 可继续增加时间范围切换，例如 7 天、30 天、90 天。
2. 如果后续需要在 tooltip 中查看未降采样的原始点，可增加按时间点查询附近原始样本的接口。

### 已知坑位

1. 如果数据库里本身没有 3 个月历史，只能展示现有历史数据，系统不会伪造历史曲线。
2. 高密度历史数据会被降采样到最多 1200 个后端点，前端再合并最近短窗口样本保证最新状态。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，8 个测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
4. 已重启 `http://127.0.0.1:8787` 服务。
5. 已执行 `/api/equity-curve?days=90&max_points=1200`，返回 HTTP 200，`days = 90`、`max_points = 1200`、`points = 1096`，并包含 `total_pnl`。
6. 已执行 `/api/equity-curve?days=90&max_points=5`，确认后端按 `max_points` 降采样并保留最新点。
7. 已执行首页和静态资源请求，确认 `chart-body`、`equity-tooltip`、`loadEquityCurve`、`animateMetric` 已加载。

### 回滚建议

1. 如需撤销本轮 90 天资金曲线和指标滚动动画，恢复 `src/polybot2other/storage.py`、`src/polybot2other/bot.py`、`src/polybot2other/web.py`、`src/polybot2other/static/app.js`、`src/polybot2other/static/index.html`、`src/polybot2other/static/styles.css`、`tests/test_core.py` 和本进度文档。

## 2026-05-27 v2.8

### 已完成

1. 将实时市场从 13 个大字段块改为紧凑布局：顶部价格、4 个关键指标、4 个报价指标和 5 行详情信息。
2. 保留市场问题、信号原因、配对事件、WebSocket 状态和 Chainlink 更新时间；长文本改为单行省略并通过 `title` 查看完整内容。
3. 将实时市场面板头部、列表间距、字段内边距和字号整体下调，减少首屏高度占用。
4. 将资金曲线 canvas 高度从 260px 降到 190px，并同步调整 HTML 固定高度。
5. 资金曲线增加圆角线条、渐变面积和二次曲线路径，优化观感；未改变资金曲线数据和采样逻辑。

### 已确认决策

1. 本轮只改 dashboard 展示密度和曲线绘制，不改交易策略、后端接口、数据库和资金计算。
2. 实时市场字段不删除，只改变信息层级：关键指标优先展示，长文本压到详情行。

### 待办和后期优化

1. 如果后续还觉得首屏偏高，可继续压缩顶部 6 个资产指标卡片，或改为两行更密集的信息条。
2. 如果需要更精确的曲线视觉，可增加真实浏览器截图回归检查，覆盖 1440px、980px 和 390px 宽度。

### 已知坑位

1. 曲线“丝滑”是前端视觉路径优化，不是对资金数据做平滑计算；尖锐盈亏变化仍会被保留。
2. 市场问题、原因和配对事件较长时会单行省略，需要鼠标悬停查看完整内容。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，7 个测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
4. 已重启 `http://127.0.0.1:8787` 服务。
5. 已执行首页请求，返回 HTTP 200。
6. 已执行 `/api/status`，确认实时运行数据正常返回。

### 回滚建议

1. 如需撤销本轮紧凑 UI，恢复 `src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`src/polybot2other/static/index.html` 和本进度文档。

## 2026-05-27 v2.5

### 已完成

1. 增加页面可见性判断，页面切到后台后不再执行 DOM 重绘。
2. WebSocket、snapshot、状态轮询等多路更新统一合并到下一帧渲染，避免切回前台时连续多次 `renderAll`。
3. 页面隐藏时暂停 `/api/status` 轮询和市场边界刷新，切回前台后延迟合并执行一次 `loadStatus(true)`。
4. 最近交易表格增加渲染签名，数据和字段未变化时不重建 100 行宽表 DOM。
5. 资金曲线降频重绘，默认最多 5 秒画一次；窗口尺寸变化和切回前台时强制刷新一次。

### 已确认决策

1. 本轮只优化前端切回页面卡顿，不改交易策略、风控参数和后端数据结构。
2. 保留 WebSocket snapshot 上报能力，重点暂停的是后台视觉重绘和无意义轮询。

### 待办和后期优化

1. 如果仍有明显卡顿，可继续给实时市场和当前持仓增加差异化渲染，避免每次重建整块 HTML。
2. 可增加简单性能计数器，记录每分钟 render 次数、snapshot 次数和接口耗时。

### 已知坑位

1. 浏览器自身会对后台标签页定时器和动画做节流，切回前台时仍可能有一次轻微恢复抖动；本轮优化目标是减少集中重绘造成的额外卡顿。
2. 当前目录不是 Git 仓库，无法用 Git 精确展示和回滚改动。

### 验证记录

1. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，7 个测试通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。

### 回滚建议

1. 如需撤销本轮前端性能优化，恢复 `src/polybot2other/static/app.js` 和本进度文档。

## 2026-05-27 v2.4

### 已完成

1. 最近交易默认加载最近 100 条，避免 `/api/status` 长期只返回 30 条。
2. 新增 `/api/recent-trades?limit=100&offset=100` 分页接口，用于按需加载更多历史交易。
3. 最近交易表格区域限制为约 8 行高度，内部使用纵向滚动条查看，不再把页面整体撑长。
4. 最近交易表头在滚动区域内固定，横向宽表和纵向滚动可以同时使用。
5. 最近交易面板底部增加“查看更多”按钮；当已加载条数小于总数时显示，点击后追加下一批 100 条。
6. 面板计数展示已加载数量和总数量，例如 `100 / 136`。

### 已确认决策

1. 当前只收起“最近交易”的展示高度，不改“当前持仓”区域。
2. 不把全部历史交易塞进 `/api/status`，用分页接口降低长期运行后的页面和接口压力。
3. 点击“查看更多”先在当前表格内追加，不做弹窗或新页面。

### 待办和后期优化

1. 如果历史交易很多，可继续增加按日期、市场、策略类型过滤。
2. 如果需要更强一致性，可把分页从 offset 改为基于 `id` 或时间游标，避免实时新增交易时 offset 轻微漂移。

### 已知坑位

1. 当前分页使用 offset；如果点击查看更多的同时有新交易插入顶部，前端会去重，但极端情况下可能需要再次点击查看更多补齐。
2. 当前目录不是 Git 仓库，无法用 Git 精确展示和回滚改动。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，7 个测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已重启 `http://127.0.0.1:8787` 服务，并执行 `/api/status`，确认 `recent_trades_meta.limit = 100`。
5. 已执行 `/api/recent-trades?limit=100&offset=100`，确认分页接口返回 `recent_trades` 和 `recent_trades_meta`。
6. 已检查首页和静态资源，确认存在 `recent-table-wrap`、`recent-page-info`、`load-more-recent` 和前端分页逻辑。

### 回滚建议

1. 如需撤销本轮最近交易分页和滚动区域改动，恢复 `src/polybot2other/storage.py`、`src/polybot2other/bot.py`、`src/polybot2other/web.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`tests/test_core.py` 和本进度文档。

## 2026-05-27 v2.3

### 已完成

1. 增加纸交易配对策略开关，默认关闭；关闭时继续使用原单边策略。
2. 增加 `/api/strategy-settings`，用于切换 `pair_strategy_enabled`。
3. 前端顶部增加“配对策略”开关，并在实时市场里展示配对成本 `ask_up + ask_down`、退出价格 `bid_up + bid_down` 和最近配对事件。
4. 配对策略开启后，按 `ask_up + ask_down <= 0.92` 开双边纸交易仓位。
5. 配对策略开启后，按 `bid_up + bid_down >= 0.98` 提前平掉配对份额。
6. 增加残余库存风控：尾盘 45 秒收缩残余腿、30 秒强制平仓、残余亏损达到 `-20%` 时平仓、残余方向未获 Chainlink 价格确认时平仓。
7. 增加账户级保护：日内亏损达到初始资金 `3%` 或连续 3 次残余止损后，不再开新的配对仓。
8. 增加纸交易部分平仓能力，不改数据库表结构；部分平仓会拆出一条已结算记录，原 open 记录保留剩余 stake 与 shares。
9. 当前持仓和最近交易增加 `strategy_type` 与 `exit_note` 字段，可区分 `PAIR` 与 `SINGLE`。

### 已确认决策

1. 当前只做纸交易，不接实盘下单。
2. 配对策略不是跟投策略，也不是简单预测涨跌；它按报告里的“双边配对桶 + 残余库存桶”实现。
3. 本轮不新增数据库字段，所有退出原因先追加到 `reason` 字段里，降低迁移风险。
4. 配对策略关闭时不执行配对开仓、配对退出和残余库存处理。

### 待办和后期优化

1. 后续如需更严格归因，可给 `trades` 增加 `strategy_type`、`exit_reason`、`parent_group_id` 字段，但这需要单独数据库迁移评审。
2. 后续可增加参数面板，让 Lee 在界面调整 `0.92`、`0.98`、尾盘秒数、残余止损比例和日内 kill switch。
3. 后续可补事件驱动回测，用历史 Up/Down 盘口与外部 BTC 价格流验证参数。

### 已知坑位

1. 当前配对策略使用现有 CLOB bid/ask 做纸面成交，真实成交仍会受延迟、滑点、部分成交和盘口撤单影响。
2. 当前只实现 BTC 5 分钟市场，报告里的 ETH 与 15 分钟市场没有纳入运行范围。
3. 当前目录不是 Git 仓库，无法用 Git 精确展示和回滚改动。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，6 个测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已重启 `http://127.0.0.1:8787` 服务，并执行 `/api/status`，确认返回 `runtime.pair_strategy` 与 `settings.pair_strategy`。
5. 已执行 `POST /api/strategy-settings` 开启和关闭配对策略，接口均返回 200，状态正确切换。
6. 已执行首页与 `/static/app.js` 静态资源检查，确认界面开关元素已输出。

### 回滚建议

1. 如需撤销本轮配对策略功能，恢复 `src/polybot2other/storage.py`、`src/polybot2other/bot.py`、`src/polybot2other/web.py`、`src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`tests/test_core.py` 和本进度文档。
2. 本轮未修改数据库结构；如果只想清空纸交易运行数据，可停止服务后删除 `data/polybot2other-real-btc.sqlite3` 及 WAL/SHM 文件。

## 2026-05-27 v1.2

### 已完成

1. 清理当前代码里的 ETH 支持残留，运行路径只保留 BTC。
2. 移除本地 synthetic price fallback；公开 BTC 价格兜底不可用时明确报错，不再生成假价格。
3. 将 `pyproject.toml` 和包 docstring 更新为真实 Polymarket BTC 5 分钟纸交易描述。
4. 将 RTDS Binance 订阅 filter 保持为实测可收到推送的 JSON 字符串格式。
5. 为浏览器 snapshot 上报增加 in-flight 限制，避免单个页面在高频行情下并发提交。
6. 为后端 snapshot 接收增加 0.5 秒全局处理节流，避免多个浏览器页面同时打开时重复写 SQLite 和重复跑策略。
7. 增加浏览器 leader-tab 机制，同一浏览器多页面打开时只允许一个页面上报 WebSocket snapshot。

### 已确认决策

1. “真实数据展示”优先于“永远有价格显示”；当真实来源都不可用时，不使用 synthetic 数据伪装实时行情。
2. BTC 以外的 symbol 不再由当前版本的价格客户端支持。

### 验证记录

1. 已通过 `rg` 确认当前 `README.md`、`src`、`tests`、`pyproject.toml` 中没有当前运行路径的 ETH、synthetic、BTC/ETH 描述残留。
2. RTDS 对比验证显示：官方文档示例的 Binance `filters: "btcusdt"` 当前没有推送；`filters: "{\"symbol\":\"btcusdt\"}"` 和不带 Binance filter 均能收到 Binance 与 Chainlink 数据。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，4 个测试通过。
4. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
5. 已执行 `/api/status` 复验，返回真实 `btc-updown-5m-*` 市场、真实 Up/Down token、目标价、Chainlink/Binance 实时价、CLOB WebSocket 报价、结算交易和 PnL。
6. 已执行连续两次 `/api/live-snapshot` 复验，后端返回 `throttled_snapshot: true`，确认多页面重复上报会被后端节流。
7. 前端已增加 `localStorage` leader 租约，减少多 tab 重复 snapshot 请求。

### 回滚建议

1. 如需恢复旧的 synthetic fallback，可从 v1.1 前版本恢复 `src/polybot2other/market.py`，但这会重新引入非真实行情，不推荐。

## 2026-05-27 v1.3

### 已完成

1. 当前持仓和最近交易改为可配置字段宽表。
2. 最近交易默认放在当前持仓下面，避免两张表并排压缩字段。
3. 当前持仓增加买入价、买入概率、份额、当前 bid/ask、可退出回款、未实现盈亏、未实现 ROI、最大回款、最大盈利、目标价、当前价、距离 bps、开仓原因等字段。
4. 最近交易增加买入价、份额、本金、结算回款、净盈亏、ROI、结果、目标价、最终价、最终距离 bps、开仓原因等字段。
5. 字段配置保存在浏览器 `localStorage`，刷新后保留；无需改数据库结构。
6. 后端按同方向当前 best bid 计算持仓可退出回款和未实现盈亏，顶部 `unrealized_pnl` 和 `total_equity` 也会使用 mark-to-market 估值。
7. Gamma 临时失败时，后端可从本地 SQLite 中读取仍未到期的 BTC 市场，避免页面直接打空。
8. 表格字段菜单不再被 panel 裁切，宽表通过横向滚动承载，不压缩字段值。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，4 个测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
3. 已执行 `rtk proxy node --check src/polybot2other/static/app.js`，前端脚本语法通过。
4. 已执行 `/api/status`，确认 `open_trades` 和 `recent_trades` 返回新增估值与结算字段。

### 回滚建议

1. 如需撤销本轮 UI 明细表改动，恢复 `src/polybot2other/static/index.html`、`src/polybot2other/static/app.js`、`src/polybot2other/static/styles.css`、`src/polybot2other/bot.py`、`src/polybot2other/storage.py` 和本进度文档。

## 2026-05-27 v1.1

### 已完成

1. 将旧的 BTC/ETH 模拟市场改为真实 Polymarket BTC 5 分钟 Up/Down 市场。
2. 接入 Gamma event slug 发现当前 BTC 5m 市场，slug 格式为 `btc-updown-5m-<window_start_unix>`。
3. 保存真实 `condition_id`、`up_token`、`down_token`、市场问题、市场 URL 和目标价。
4. 接入 CLOB orderbook，策略入场价改为真实 Up/Down token 最优卖价，不再使用模拟入场价。
5. 浏览器 dashboard 接入 CLOB market WebSocket 和 RTDS crypto price WebSocket，按 1 秒节流上报 snapshot。
6. 后端保留 CLOB REST 和公开 BTC 价格兜底，浏览器 WebSocket 断开或价格流过期时仍能展示市场和运行纸交易。
7. 将默认 SQLite 文件改为 `data/polybot2other-real-btc.sqlite3`，避免混入旧模拟 BTC/ETH 数据。
8. 为 SQLite 存储加互斥锁，避免后台 tick、HTTP 请求和 WebSocket snapshot 并发写同一连接导致偶发异常。
9. 修正 HTTP snapshot 的异常处理，市场切换边界和客户端断开时不再输出大段堆栈。

### 已确认决策

1. 当前只做 BTC，不做 ETH。
2. 当前只做 Paper Trading，不接入私钥、API secret、签名下单和真实资金。
3. 真实行情优先级：浏览器 WebSocket snapshot 优先；过期时后端 REST 兜底。
4. 交易策略必须同时满足目标价、实时价格、盘口报价新鲜度、价差、卖盘深度、置信度和赔率优势。
5. 无 WebSocket 页面打开时，后端可以继续展示 REST 兜底数据，但策略会按数据新鲜度限制开仓。

### 待办和后期优化

1. 若后续需要无人值守实时交易，应单独实现后端 WebSocket 客户端，而不是依赖浏览器页面保持打开。
2. 增加真实 Polymarket resolved 结果的更多兼容解析，减少极端情况下用最终价格兜底结算的概率。
3. 增加参数面板，让 Lee 在界面调整单笔金额、置信度阈值、最大亏损、开仓窗口和报价新鲜度阈值。
4. 增加交易回放和按市场窗口的策略诊断视图。

### 已知坑位

1. Polymarket 页面里的目标价不是稳定公开字段，当前会优先从 Gamma metadata 取，取不到时从 Polymarket 页面数据里解析。
2. RTDS 初始批量消息可能不带 topic，前端按订阅顺序兼容解析；后续带 topic 的实时 update 仍是主路径。
3. 浏览器页面不打开时，WebSocket 不会自动运行；此时后端会切到 REST 兜底，实时性弱于 WebSocket。
4. 当前目录不是 Git 仓库，无法用 Git 精确展示和回滚改动。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，4 个测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
3. 已执行 Node WebSocket 验证脚本，CLOB market WebSocket 返回真实 Up/Down 报价，RTDS 返回 BTC Chainlink 和 Binance 实时价格。
4. 已执行 `POST /api/live-snapshot`，后端返回 `status: 200`，并记录 `market: connected`、`price: connected`、真实 `chainlink`、真实 `target` 和真实 Up/Down token 报价。
5. 已执行 `rtk proxy sh -c 'curl -s http://127.0.0.1:8787/api/status | python3 -m json.tool'`，返回当前真实 `btc-updown-5m-*` 市场、`target_price`、`up_token`、`down_token`、PnL 指标和纸交易状态。

### 回滚建议

1. 当前目录不是 Git 仓库。如需回滚本轮修改，需要按文件恢复 `config.py`、`models.py`、`polymarket.py`、`storage.py`、`strategy.py`、`bot.py`、`web.py`、`static/index.html`、`static/app.js`、`tests/test_core.py`、`README.md` 和本进度文档。
2. 如果只想清空本轮真实 BTC 运行数据，可停止服务后删除 `data/polybot2other-real-btc.sqlite3` 及对应 WAL/SHM 文件。

## 2026-05-27 v1.0

### 已完成

1. 新建独立 Python 标准库项目，不复用相邻 `polybot` 的工作区文件。
2. 实现 `$100` 默认初始资金的纸交易账户。
3. 实现 BTC/ETH 短周期 Up/Down 模拟市场。
4. 实现公开价格读取，优先 Coinbase/Binance，失败时回退本地合成价格。
5. 实现 SQLite 存储交易、结算、价格样本和资金曲线。
6. 实现浏览器 dashboard，展示总资产、盈亏、可用资金、持仓风险、胜率、最大回撤、实时市场、当前持仓和最近交易。
7. 明确当前版本只做 Paper Trading，不读取私钥，不提交真实订单。
8. 补充 HTTP HEAD 支持，避免健康检查只用头部请求时误判页面不可用。

### 已确认决策

1. 第一版不做实盘交易。
2. 第一版不接入私钥、API secret、授权下单接口。
3. 交易执行按固定 `$2` 单笔纸交易金额和 `$100` 初始资金开始。
4. Polymarket 风格先用 BTC/ETH 5 分钟 Up/Down 纸交易模型承载，后续再接真实 CLOB 市场 token。

### 待办和后期优化

1. 接入真实 Polymarket Gamma/CLOB 市场发现，替换当前模拟市场 round。
2. 增加目标账户钱包地址归因模块，用 Data API 分析 `@username123123` 的历史交易。
3. 增加参数面板，让 Lee 在界面调整单笔金额、置信度阈值、最大亏损和开仓窗口。
4. 增加更严格的回测模块和按日/按策略统计。
5. 若进入实盘阶段，必须单独设计密钥管理、签名、撤单、滑点、限价、风控和审计。

### 已知坑位

1. Coinbase/Binance 公开 API 可能因网络或地区限制失败；当前有 synthetic fallback，适合保证界面可运行，但不能当作真实交易数据。
2. 当前入场价是策略置信度推导的模拟价格，不是真实 Polymarket orderbook 最优卖价。
3. 当前未计算未实现收益，开放持仓按风险金额计入总资产，结算后才产生已实现盈亏。
4. 当前目录不是 Git 仓库，无法用 Git 精确展示和回滚改动。

### 验证记录

1. 已执行 `rtk proxy env PYTHONPATH=src python3 -m unittest discover -s tests`，3 个测试通过。
2. 已执行 `rtk proxy env PYTHONPATH=src python3 -m compileall -q src tests`，编译检查通过。
3. 已执行 `rtk proxy env PYTHONPATH=src python3 -m polybot2other.web --host 127.0.0.1 --port 8787`，dashboard 已启动。
4. 已执行 `rtk proxy curl -s -I http://127.0.0.1:8787/`，返回 `HTTP/1.0 200 OK`。
5. 已执行 `rtk proxy curl -s http://127.0.0.1:8787/api/status`，返回 `initial_balance: 100.0`、`paper_only: true`、PnL 指标、当前持仓和资金曲线。
6. 等待首轮市场结算后再次检查 `/api/status`，已出现 `settled_trades: 4`、`total_pnl: -1.483176`、`win_rate: 50.0`，说明纸交易结算和盈亏更新链路生效。

### 回滚建议

1. 当前目录不是 Git 仓库。如需撤销本次新增项目，可删除本次新增文件和目录：`pyproject.toml`、`README.md`、`src/`、`tests/`、`docs/`、`data/`。
2. 如果只想清空运行数据，可停止服务后删除 `data/polybot2other.sqlite3` 及 SQLite WAL/SHM 文件。
