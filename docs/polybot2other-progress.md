# polybot2other-progress

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
