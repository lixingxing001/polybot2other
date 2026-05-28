# polybot2other-progress

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
