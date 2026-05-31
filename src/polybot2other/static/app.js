const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 });

const ids = {
  navItems: Array.from(document.querySelectorAll("[data-nav-page]")),
  botPage: document.getElementById("bot-page"),
  analysisPage: document.getElementById("analysis-page"),
  analysisRefresh: document.getElementById("analysis-refresh"),
  analysisStatus: document.getElementById("analysis-status"),
  analysisAffectsTrading: document.getElementById("analysis-affects-trading"),
  analysisDataScope: document.getElementById("analysis-data-scope"),
  analysisLlmPath: document.getElementById("analysis-llm-path"),
  analysisWallets: document.getElementById("analysis-wallets"),
  analysisSummary: document.getElementById("analysis-summary"),
  analysisProbability: document.getElementById("analysis-probability"),
  analysisRiskTags: document.getElementById("analysis-risk-tags"),
  analysisLlmReview: document.getElementById("analysis-llm-review"),
  analysisLlmReviewRefresh: document.getElementById("analysis-llm-review-refresh"),
  analysisRealtimeCard: document.getElementById("analysis-realtime-card"),
  analysisRealtime: document.getElementById("analysis-realtime"),
  analysisRealtimeToggle: document.getElementById("analysis-realtime-toggle"),
  runtime: document.getElementById("runtime-pill"),
  paperPauseToggle: document.getElementById("paper-pause-toggle"),
  liveEnabled: document.getElementById("live-enabled"),
  liveStatus: document.getElementById("live-status"),
  liveInitialBalance: document.getElementById("live-initial-balance"),
  liveStakeDollars: document.getElementById("live-stake-dollars"),
  liveMaxOpenTrades: document.getElementById("live-max-open-trades"),
  liveMaxEntryPrice: document.getElementById("live-max-entry-price"),
  liveMaxDailyLoss: document.getElementById("live-max-daily-loss"),
  liveMaxTotalDrawdown: document.getElementById("live-max-total-drawdown"),
  liveRetryCount: document.getElementById("live-retry-count"),
  liveRetryDelayMs: document.getElementById("live-retry-delay-ms"),
  liveComplianceAck: document.getElementById("live-compliance-ack"),
  liveFallbackSources: Array.from(document.querySelectorAll("[data-live-fallback-source]")),
  liveSettingsToggle: document.getElementById("live-settings-toggle"),
  liveSettingsPanel: document.getElementById("live-settings-panel"),
  liveSaveSettings: document.getElementById("live-save-settings"),
  liveReloadCredentials: document.getElementById("live-reload-credentials"),
  livePreflight: document.getElementById("live-preflight"),
  liveDoctor: document.getElementById("live-doctor"),
  liveOnce: document.getElementById("live-once"),
  liveOpenOrdersRefresh: document.getElementById("live-open-orders-refresh"),
  liveEmergencyStop: document.getElementById("live-emergency-stop"),
  liveGateStatus: document.getElementById("live-gate-status"),
  liveReadiness: document.getElementById("live-readiness"),
  livePreflightResult: document.getElementById("live-preflight-result"),
  liveDoctorResult: document.getElementById("live-doctor-result"),
  liveOnceResult: document.getElementById("live-once-result"),
  liveTerminalLines: document.getElementById("live-terminal-lines"),
  accountScopeSelect: document.getElementById("account-scope-select"),
  accountScopeLabel: document.getElementById("account-scope-label"),
  accountScopeSource: document.getElementById("account-scope-source"),
  totalEquity: document.getElementById("total-equity"),
  totalPnl: document.getElementById("total-pnl"),
  unrealizedPnl: document.getElementById("unrealized-pnl"),
  cashBalance: document.getElementById("cash-balance"),
  reservedCash: document.getElementById("reserved-cash"),
  openRisk: document.getElementById("open-risk"),
  winRate: document.getElementById("win-rate"),
  maxDrawdown: document.getElementById("max-drawdown"),
  lastTick: document.getElementById("last-tick"),
  markets: document.getElementById("markets"),
  strategyExperimentMeta: document.getElementById("strategy-experiment-meta"),
  strategyExperimentSummary: document.getElementById("strategy-experiment-summary"),
  strategyExperiments: document.getElementById("strategy-experiments"),
  openDataScope: document.getElementById("open-data-scope"),
  openTrades: document.getElementById("open-trades"),
  openTradesHead: document.getElementById("open-trades-head"),
  openFieldOptions: document.getElementById("open-field-options"),
  orderDataScope: document.getElementById("order-data-scope"),
  recentOrders: document.getElementById("recent-orders"),
  recentOrdersHead: document.getElementById("recent-orders-head"),
  orderFieldOptions: document.getElementById("order-field-options"),
  recentDataScope: document.getElementById("recent-data-scope"),
  recentTrades: document.getElementById("recent-trades"),
  recentTradesHead: document.getElementById("recent-trades-head"),
  recentFieldOptions: document.getElementById("recent-field-options"),
  recentStartTime: document.getElementById("recent-start-time"),
  recentEndTime: document.getElementById("recent-end-time"),
  applyRecentFilter: document.getElementById("apply-recent-filter"),
  resetRecentFilter: document.getElementById("reset-recent-filter"),
  recentSummary: document.getElementById("recent-summary"),
  openCount: document.getElementById("open-count"),
  orderCount: document.getElementById("order-count"),
  tradeCount: document.getElementById("trade-count"),
  orderPageInfo: document.getElementById("order-page-info"),
  loadMoreOrders: document.getElementById("load-more-orders"),
  orderStatusFilter: document.getElementById("order-status-filter"),
  cancelCurrentOrders: document.getElementById("cancel-current-orders"),
  cancelAllOrders: document.getElementById("cancel-all-orders"),
  recentPageInfo: document.getElementById("recent-page-info"),
  loadMoreRecent: document.getElementById("load-more-recent"),
  chart: document.getElementById("equity-chart"),
  chartTooltip: document.getElementById("equity-tooltip"),
  paperOnly: document.getElementById("paper-only"),
  tickButton: document.getElementById("tick-button"),
};

const MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
const RTDS_WS = "wss://ws-live-data.polymarket.com";
const OKX_WS = "wss://ws.okx.com:8443/ws/v5/public";
const BINANCE_MARKET_WS = "wss://stream.binance.com:9443/ws/btcusdt@ticker";
const MARKET_PING_MS = 10_000;
const RTDS_PING_MS = 5_000;
const OKX_PING_MS = 20_000;
const SNAPSHOT_POST_MS = 1_000;
const STATUS_POLL_MS = 2_000;
const STATUS_STREAM_STALE_MS = 4_000;
const RECENT_PAGE_SIZE = 100;
const ORDER_PAGE_SIZE = 20;
const CHART_RENDER_INTERVAL_MS = 5_000;
const EQUITY_CURVE_DAYS = 90;
const EQUITY_CURVE_MAX_POINTS = 1200;
const EQUITY_CURVE_REFRESH_MS = 30_000;
const ACTOR_ANALYSIS_REFRESH_MS = 5_000;
const LLM_REVIEW_REFRESH_MS = 15_000;
const METRIC_ANIMATION_MS = 360;
const RECENT_SKELETON_ROWS = 8;
const LIVE_LOG_LIMIT = 80;
const ANALYSIS_REALTIME_EVENT_LIMIT = 80;
const ANALYSIS_RECENT_WINDOW_MS = 10_000;
const TABLE_INTERACTION_HOLD_MS = 500;
const ANALYSIS_REALTIME_VISIBILITY_KEY = "polybot2other:analysis-realtime-visible";
const SNAPSHOT_LEADER_KEY = "polybot2other:snapshot-leader";
const SNAPSHOT_LEADER_TTL_MS = 2_500;
const TAB_ID = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
const FIELD_STORAGE_KEYS = {
  open: "polybot2other:open-trade-fields",
  order: "polybot2other:order-fields",
  recent: "polybot2other:recent-trade-fields",
};
const APP_PAGE_KEYS = new Set(["bot", "analysis"]);
const ORDER_STATUS_LABELS = {
  PENDING: "待官方确认",
  RESTING: "挂单中",
  PARTIAL_RESTING: "部分成交挂单",
  FILLED: "完全成交",
  PARTIAL: "部分成交",
  CANCELED: "已取消",
  EXPIRED: "已过期",
  REJECTED: "已拒绝",
};
const TRADE_STATUS_LABELS = {
  OPEN: "持仓中",
  SETTLED: "已结算",
  PENDING_SETTLEMENT: "等待官方结算",
};

let activeMarket = null;
let activeAppPage = "bot";
let marketSocket = null;
let priceSocket = null;
let okxSocket = null;
let binanceMarketSocket = null;
let marketPing = null;
let pricePing = null;
let okxPing = null;
let marketWsStatus = "waiting";
let priceWsStatus = "waiting";
let okxWsStatus = "waiting";
let binanceMarketWsStatus = "waiting";
let lastSnapshotPostMs = 0;
let snapshotInFlight = false;
let latestStatus = null;
let statusStream = null;
let statusStreamConnected = false;
let lastStatusStreamAt = 0;
let livePreflight = null;
let liveDoctor = null;
let liveOnce = null;
let liveLogRows = [];
const liveLogLastByKey = new Map();
let liveSettingsOpen = false;
const liveSettingsDirtyFields = new Set();
let recentRows = [];
let recentMeta = { limit: RECENT_PAGE_SIZE, offset: 0, loaded: 0, total: 0, has_more: false, start_at: null, end_at: null };
let recentSummary = null;
let recentFilters = { start_at: null, end_at: null };
let recentLoading = true;
let openDataScope = "main";
let orderDataScope = "main";
let recentDataScope = "main";
let accountScope = "main";
let accountScopeOptionsKey = "";
let tableScopeOptionsKey = "";
let strategyTables = null;
let strategyTablesLoading = false;
let lastStrategyTablesFetchMs = 0;
let strategyOrderLimit = ORDER_PAGE_SIZE;
let strategyTradeLimit = RECENT_PAGE_SIZE;
let actorAnalysis = null;
let actorAnalysisLoading = false;
let actorAnalysisError = "";
let lastActorAnalysisFetchMs = 0;
let llmReview = null;
let llmReviewLoading = false;
let llmReviewError = "";
let lastLlmReviewFetchMs = 0;
let analysisRealtime = createRealtimeAnalysisState();
let analysisRealtimeRenderQueued = false;
let analysisRealtimeVisible = loadAnalysisRealtimeVisible();
let recentTransitionTimer = null;
let orderRows = [];
let orderStatusFilter = "all";
let orderMeta = { limit: ORDER_PAGE_SIZE, offset: 0, loaded: 0, total: 0, has_more: false, status_filter: orderStatusFilter };
let expandedOrderId = null;
let loadingOrderId = null;
const orderFillCache = new Map();
let expandedExperimentId = null;
let loadingExperimentId = null;
const experimentDetailCache = new Map();
let pageVisible = document.visibilityState !== "hidden";
let renderQueued = false;
let pendingRenderData = null;
let pendingRenderOptions = {};
let lastChartRenderMs = 0;
let lastOpenRenderKey = "";
let lastRecentRenderKey = "";
let lastOrderRenderKey = "";
let lastExperimentRenderKey = "";
let lastOpenRenderedScope = "";
let lastOpenRenderedCount = 0;
let pendingOpenRender = false;
let pendingOrderRender = false;
let scopedRecentRefreshInFlight = false;
const tableInteractionHoldUntil = { open: 0, order: 0 };
let foregroundRefreshTimer = null;
let equityCurveRows = [];
let equityCurveMeta = {};
let lastEquityCurveFetchMs = 0;
let equityCurveInFlight = false;
let equityCurvePendingForce = false;
let currentChartRows = [];
let chartHoverX = null;
let chartHoverQueued = false;
const metricAnimations = new Map();
let quotes = {};
let untaggedPriceBatchCount = 0;
let priceState = {
  chainlink: null,
  chainlink_updated_ms: null,
  binance: null,
  binance_updated_ms: null,
  binance_market: null,
  binance_market_updated_ms: null,
  okx: null,
  okx_updated_ms: null,
  target_price: null,
  target_price_source: null,
  target_price_fallback: false,
  target_price_updated_ms: null,
  source: null,
};

const openTradeFields = [
  { key: "live_sell_action", label: "操作", render: (row) => liveSellButton(row) },
  { key: "position_state", label: "状态", render: (row) => positionStateText(row) },
  { key: "strategy_type", label: "策略", render: (row) => safe(row.strategy_type) },
  { key: "side", label: "方向", render: (row) => `<span class="${sideClass(row.side)}">${safe(row.side)}</span>` },
  { key: "stake", label: "本金", render: (row) => fmtMoneyCell(row.stake) },
  { key: "entry_price", label: "买入价", render: (row) => fmtNumberCell(row.entry_price, 4) },
  { key: "entry_probability_pct", label: "买入概率", render: (row) => fmtPctCell(row.entry_probability_pct) },
  { key: "shares", label: "份额", render: (row) => fmtNumberCell(row.shares, 6) },
  { key: "current_bid", label: "当前买一", render: (row) => positionNumberCell(row, "current_bid", 4) },
  { key: "current_ask", label: "当前卖一", render: (row) => positionNumberCell(row, "current_ask", 4) },
  { key: "exit_value", label: "可退出回款", render: (row) => positionMoneyCell(row, "exit_value") },
  { key: "unrealized_pnl", label: "未实现盈亏", render: (row) => positionSignedMoneyCell(row, "unrealized_pnl") },
  { key: "unrealized_roi_pct", label: "未实现ROI", render: (row) => positionSignedPctCell(row, "unrealized_roi_pct") },
  { key: "max_payout", label: "最大回款", render: (row) => fmtMoneyCell(row.max_payout) },
  { key: "max_profit", label: "最大盈利", render: (row) => fmtSignedMoneyCell(row.max_profit) },
  { key: "max_loss", label: "最大亏损", render: (row) => fmtMoneyCell(row.max_loss) },
  { key: "target_price", label: "目标价", render: (row) => fmtMoneyCell(row.target_price) },
  { key: "current_price", label: "当前价", render: (row) => positionMoneyCell(row, "current_price") },
  { key: "current_distance_bps", label: "距离bps", render: (row) => positionSignedBpsCell(row, "current_distance_bps") },
  { key: "opened_at", label: "开仓时间", render: (row) => fmtDateTimeCell(row.opened_at) },
  { key: "ends_at", label: "到期", render: (row) => fmtDateTimeCell(row.ends_at) },
  { key: "left", label: "剩余", render: (row) => safe(fmtLeft(row.ends_at)) },
  { key: "round_id", label: "市场", render: (row) => safe(row.round_id), cellClass: "mono-cell" },
  { key: "quote_source", label: "报价源", render: (row) => safe(row.quote_source) },
  { key: "exit_note", label: "退出标记", render: (row) => safe(row.exit_note), cellClass: "reason-cell" },
  { key: "reason", label: "开仓原因", render: (row) => safe(row.reason), cellClass: "reason-cell" },
];

const recentTradeFields = [
  { key: "opened_at", label: "开仓时间", render: (row) => fmtDateTimeCell(row.opened_at) },
  { key: "settled_at", label: "结算时间", render: (row) => fmtDateTimeCell(row.settled_at) },
  { key: "round_id", label: "市场", render: (row) => safe(row.round_id), cellClass: "mono-cell" },
  { key: "strategy_type", label: "策略", render: (row) => safe(row.strategy_type) },
  { key: "side", label: "方向", render: (row) => `<span class="${sideClass(row.side)}">${safe(row.side)}</span>` },
  { key: "status", label: "状态", render: (row) => tradeStatusText(row) },
  { key: "stake", label: "本金", render: (row) => fmtMoneyCell(row.stake) },
  { key: "entry_price", label: "买入价", render: (row) => fmtNumberCell(row.entry_price, 4) },
  { key: "entry_probability_pct", label: "买入概率", render: (row) => fmtPctCell(row.entry_probability_pct) },
  { key: "shares", label: "份额", render: (row) => fmtNumberCell(row.shares, 6) },
  { key: "payout", label: "结算回款", render: (row) => fmtMoneyCell(row.payout) },
  { key: "pnl", label: "净盈亏", render: (row) => fmtSignedMoneyCell(row.pnl) },
  { key: "roi_pct", label: "ROI", render: (row) => fmtSignedPctCell(row.roi_pct) },
  { key: "max_payout", label: "最大回款", render: (row) => fmtMoneyCell(row.max_payout) },
  { key: "max_profit", label: "最大盈利", render: (row) => fmtSignedMoneyCell(row.max_profit) },
  { key: "outcome", label: "结果", render: (row) => `<span class="${sideClass(row.outcome)}">${safe(row.outcome)}</span>` },
  { key: "settlement_source_label", label: "结算来源", render: (row) => safe(row.settlement_source_label || row.settlement_source || "-") },
  { key: "target_price", label: "目标价", render: (row) => fmtMoneyCell(row.target_price) },
  { key: "final_price", label: "最终价", render: (row) => fmtMoneyCell(row.final_price) },
  { key: "final_distance_bps", label: "最终距离bps", render: (row) => fmtSignedBpsCell(row.final_distance_bps) },
  { key: "confidence", label: "置信度", render: (row) => fmtPctCell(Number(row.confidence) * 100) },
  { key: "move_bps", label: "开仓距离bps", render: (row) => fmtSignedBpsCell(row.move_bps) },
  { key: "exit_note", label: "退出标记", render: (row) => safe(row.exit_note), cellClass: "reason-cell" },
  { key: "reason", label: "开仓原因", render: (row) => safe(row.reason), cellClass: "reason-cell" },
];

const recentOrderFields = [
  { key: "detail_toggle", label: "明细", render: (row) => orderToggleText(row), cellClass: "mono-cell" },
  { key: "cancel_action", label: "操作", render: (row) => orderCancelButton(row) },
  { key: "created_at", label: "时间", render: (row) => fmtDateTimeCell(row.created_at) },
  { key: "round_id", label: "市场", render: (row) => safe(row.round_id), cellClass: "mono-cell" },
  { key: "side", label: "方向", render: (row) => `<span class="${sideClass(row.side)}">${safe(row.side)}</span>` },
  { key: "order_type", label: "类型", render: (row) => safe(row.order_type) },
  { key: "status", label: "状态", render: (row) => orderStatusText(row.status) },
  { key: "limit_price", label: "限价", render: (row) => fmtNumberCell(row.limit_price, 4) },
  { key: "requested_cash", label: "预算", render: (row) => fmtMoneyCell(row.requested_cash) },
  { key: "avg_fill_price", label: "均价", render: (row) => fmtNumberCell(row.avg_fill_price, 4) },
  { key: "filled_shares", label: "成交份额", render: (row) => fmtNumberCell(row.filled_shares, 6) },
  { key: "cash_spent", label: "花费", render: (row) => fmtMoneyCell(row.cash_spent) },
  { key: "fee", label: "手续费", render: (row) => fmtMoneyCell(row.fee) },
  { key: "fill_count", label: "成交档", render: (row) => fmtNumberCell(row.fill_count, 0) },
  { key: "trade_id", label: "持仓ID", render: (row) => safe(row.trade_id || "-"), cellClass: "mono-cell" },
  { key: "reason", label: "原因", render: (row) => safe(row.reason), cellClass: "reason-cell" },
];

const comboField = {
  key: "combo",
  label: "组合",
  render: (row) => `
    <strong>${safe(row.combo || "主账户")}</strong>
    <span class="subtle mono-cell">${safe(row.variant_id || row.account_scope || "")}</span>
  `,
};

const experimentOpenFields = [comboField, ...openTradeFields];
const experimentRecentFields = [comboField, ...recentTradeFields];
const experimentOrderFields = [
  comboField,
  ...recentOrderFields.filter((field) => !["detail_toggle", "cancel_action"].includes(field.key)),
];
const scopedOrderFields = [comboField, ...recentOrderFields.filter((field) => field.key !== "cancel_action")];
const scopedOpenFields = [comboField, ...openTradeFields];
const scopedRecentFields = [comboField, ...recentTradeFields];

const defaultOpenFieldKeys = [
  "live_sell_action", "position_state", "strategy_type", "side", "stake", "entry_price", "shares", "current_bid", "current_ask",
  "exit_value", "unrealized_pnl", "unrealized_roi_pct", "max_payout",
  "max_profit", "target_price", "current_price", "current_distance_bps",
  "left", "reason",
];

const defaultRecentFieldKeys = [
  "opened_at", "settled_at", "strategy_type", "side", "status", "stake", "entry_price",
  "shares", "payout", "pnl", "roi_pct", "outcome", "target_price",
  "settlement_source_label", "final_price", "final_distance_bps", "exit_note", "reason",
];

const defaultOrderFieldKeys = [
  "detail_toggle", "cancel_action", "created_at", "side", "order_type", "status", "limit_price", "requested_cash",
  "avg_fill_price", "filled_shares", "cash_spent", "fee", "fill_count", "reason",
];

function experimentFieldKeys(kind) {
  const base = selectedFields[kind] || [];
  const extra = kind === "open" ? ["live_sell_action"] : [];
  return ["combo", ...extra, ...base.filter((key) => !["detail_toggle", "cancel_action", "live_sell_action"].includes(key))];
}

let selectedFields = {
  open: loadSelectedFields("open", openTradeFields, defaultOpenFieldKeys),
  order: loadSelectedFields("order", recentOrderFields, defaultOrderFieldKeys),
  recent: loadSelectedFields("recent", recentTradeFields, defaultRecentFieldKeys),
};

function cls(value) {
  if (value > 0) return "positive";
  if (value < 0) return "negative";
  return "";
}

function signedMoney(value) {
  const parsed = toNumber(value) || 0;
  return `${parsed > 0 ? "+" : ""}${money.format(parsed)}`;
}

function percentText(value) {
  return `${number.format(toNumber(value) || 0)}%`;
}

function fmtTime(seconds) {
  if (!seconds) return "-";
  return new Date(seconds * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

function fmtMs(ms) {
  if (!ms) return "-";
  return new Date(ms).toLocaleTimeString("zh-CN", { hour12: false });
}

function setLiveSettingsOpen(open) {
  liveSettingsOpen = Boolean(open);
  if (ids.liveSettingsPanel) ids.liveSettingsPanel.hidden = !liveSettingsOpen;
  if (ids.liveSettingsToggle) ids.liveSettingsToggle.setAttribute("aria-expanded", liveSettingsOpen ? "true" : "false");
}

function toggleLiveSettingsPanel() {
  setLiveSettingsOpen(!liveSettingsOpen);
}

function liveSettingsFieldKey(input) {
  return input?.id || input?.name || (input?.dataset?.liveFallbackSource ? `live-fallback-${input.dataset.liveFallbackSource}` : "");
}

function syncLiveSaveDirtyState() {
  if (!ids.liveSaveSettings) return;
  const dirty = liveSettingsDirtyFields.size > 0;
  ids.liveSaveSettings.classList.toggle("is-dirty", dirty);
  ids.liveSaveSettings.title = dirty ? "有未保存的实盘配置" : "";
}

function markLiveSettingsDirty(input) {
  const key = liveSettingsFieldKey(input);
  if (!key) return;
  const serverValue = input.dataset.serverValue;
  const currentValue = input.type === "checkbox" ? String(Boolean(input.checked)) : String(input.value ?? "");
  if (serverValue !== undefined && currentValue === serverValue) {
    liveSettingsDirtyFields.delete(key);
  } else {
    liveSettingsDirtyFields.add(key);
  }
  syncLiveSaveDirtyState();
}

function clearLiveSettingsDirty() {
  liveSettingsDirtyFields.clear();
  syncLiveSaveDirtyState();
}

function liveSettingsFormControls() {
  return [
    ids.liveInitialBalance,
    ids.liveStakeDollars,
    ids.liveMaxOpenTrades,
    ids.liveMaxEntryPrice,
    ids.liveMaxDailyLoss,
    ids.liveMaxTotalDrawdown,
    ids.liveRetryCount,
    ids.liveRetryDelayMs,
    ids.liveComplianceAck,
    ...(ids.liveFallbackSources || []),
  ].filter(Boolean);
}

function bindLiveSettingsDirtyTracking() {
  for (const input of liveSettingsFormControls()) {
    input.addEventListener("input", () => markLiveSettingsDirty(input));
    input.addEventListener("change", () => markLiveSettingsDirty(input));
  }
}

function appendLiveLog({ key = "", level = "info", title = "", message = "", details = [], code = "", at_ms = null } = {}) {
  if (!ids.liveTerminalLines) return;
  const normalizedDetails = Array.isArray(details) ? details.filter(Boolean).map((item) => String(item)) : [];
  const normalized = {
    level: String(level || "info"),
    title: String(title || "实盘日志"),
    message: String(message || ""),
    details: normalizedDetails,
    code: String(code || ""),
  };
  const signature = JSON.stringify(normalized);
  if (key) {
    const previous = liveLogLastByKey.get(key);
    if (previous === signature) return;
      liveLogLastByKey.set(key, signature);
  }
  const logAtMs = at_ms === null || at_ms === undefined ? Date.now() : normalizeMs(at_ms);
  liveLogRows.unshift({ ...normalized, at_ms: logAtMs });
  liveLogRows = liveLogRows
    .sort((left, right) => (toNumber(right.at_ms) || 0) - (toNumber(left.at_ms) || 0))
    .slice(0, LIVE_LOG_LIMIT);
  renderLiveTerminal();
}

function renderLiveTerminal() {
  if (!ids.liveTerminalLines) return;
  if (!liveLogRows.length) {
    ids.liveTerminalLines.innerHTML = `<div class="live-terminal-empty">等待实盘事件</div>`;
    return;
  }
  ids.liveTerminalLines.innerHTML = liveLogRows.map((row) => {
    const details = row.details.length
      ? `<div class="live-log-details">${row.details.map((item) => `<div>${safe(item)}</div>`).join("")}</div>`
      : "";
    const code = row.code ? `<pre class="live-log-code">${safe(row.code)}</pre>` : "";
    return `<div class="live-log-entry ${safe(row.level)}">` +
      `<div class="live-log-main">` +
        `<span class="live-log-time">${safe(fmtMs(row.at_ms))}</span>` +
        `<span class="live-log-title">${safe(row.title)}</span>` +
        `${row.message ? `<span class="live-log-message">${safe(row.message)}</span>` : ""}` +
      `</div>${details}${code}</div>`;
  }).join("");
}

function renderLlmTerminalLogs(data = latestStatus) {
  if (!ids.liveTerminalLines) return;
  const settings = data?.settings?.llm_super_agent || {};
  const variant = strategyExperimentVariants(data).find((row) => row.variant_id === "LLM_SUPER_AGENT_PAPER") || null;
  if (!settings.enabled && !variant) return;
  const apiKeyPresent = Boolean(settings.api_key_present);
  appendLiveLog({
    key: "llm:config",
    level: settings.enabled === false ? "warn" : apiKeyPresent ? "pass" : "warn",
    title: "[LLM] config",
    message: `${settings.model || "-"} · ${settings.base_url || "-"} · key:${apiKeyPresent ? "present" : "missing"}`,
    details: [
      `paper_only_variant:${variant ? "ready" : "missing"}`,
      `timeout:${fmtNumberCell(settings.timeout_seconds, 1)}s · interval:${fmtNumberCell(settings.min_interval_seconds, 1)}s`,
      "secrets:hidden · prompt:hidden · raw_response:hidden",
    ],
  });
  if (!variant) return;
  const decisions = Array.isArray(variant.recent_llm_decisions) ? variant.recent_llm_decisions : [];
  if (!decisions.length) {
    appendLiveLog({
      key: "llm:waiting",
      level: "info",
      title: "[LLM] waiting",
      message: "等待下一次 LLM_SUPER_AGENT_PAPER tick 产生路由决策",
      details: [
        `variant:${variant.variant_id}`,
        variant.last_signal?.reason ? `last_signal:${variant.last_signal.reason}` : "",
      ],
    });
    return;
  }
  for (const row of decisions.slice(0, 10)) {
    appendLlmDecisionLog(row);
  }
}

function appendLlmDecisionLog(row = {}) {
  const route = row.route || "NO_ROUTE";
  const source = row.source || "-";
  const allowTrade = row.allow_trade === true || toNumber(row.allow_trade) === 1;
  const confidence = toNumber(row.confidence);
  const error = String(row.error || "");
  const codes = parseJsonArray(row.reason_codes_json).map((item) => String(item)).filter(Boolean);
  appendLiveLog({
    key: `llm:decision:${row.id || row.created_at || route}`,
    at_ms: row.created_at || null,
    level: error ? "error" : allowTrade ? "pass" : "warn",
    title: `[LLM] ${source} ${route}`,
    message: `${allowTrade ? "ALLOW" : "BLOCK"} · conf ${confidence === null ? "-" : fmtNumberCell(confidence, 3)} · ${row.market_regime || "-"}`,
    details: [
      `round:${compactText(row.round_id, 42)}`,
      `execution:${llmRouteExecutionLabel(route, allowTrade)}`,
      row.reason ? `reason:${row.reason}` : "",
      codes.length ? `codes:${codes.join(", ")}` : "",
      row.valid_until ? `valid_until:${fmtTime(row.valid_until)}` : "",
      error ? `error:${error}` : "",
    ],
  });
}

function llmRouteExecutionLabel(route, allowTrade) {
  if (!allowTrade || route === "NO_TRADE") return "NO_TRADE";
  const labels = {
    PAIR_FAK: "PAIR + FAK paper path",
    SINGLE_FAK: "SINGLE + FAK paper path",
    SINGLE_FAK_REVERSAL: "SINGLE + FAK REVERSAL paper path",
    SINGLE_FAK_STOP_AND_FLIP: "SINGLE + FAK STOP_AND_FLIP paper path",
    SINGLE_FAK_MULTI_LEAD: "SINGLE + FAK MULTI_LEAD paper path",
    SINGLE_FAK_MULTI_CONFIRM: "SINGLE + FAK MULTI_CONFIRM paper path",
    SINGLE_FAK_ANTI_BOT_GUARD: "SINGLE + FAK ANTI_BOT_GUARD paper path",
  };
  return labels[route] || "whitelisted paper path";
}

function parseJsonArray(value) {
  if (Array.isArray(value)) return value;
  if (typeof value !== "string" || !value.trim()) return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch (_) {
    return [];
  }
}

function compactText(value, maxLength = 36) {
  const text = String(value || "-");
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(0, maxLength - 3))}...`;
}

async function withButtonLoading(button, task, after = null) {
  if (!button) return task();
  const wasDisabled = button.disabled;
  button.disabled = true;
  button.classList.add("is-loading");
  button.setAttribute("aria-busy", "true");
  try {
    return await task();
  } finally {
    button.classList.remove("is-loading");
    button.removeAttribute("aria-busy");
    button.disabled = wasDisabled;
    if (typeof after === "function") after();
  }
}

function fmtLeft(endsAt) {
  const left = Math.max(0, Math.round(endsAt - Date.now() / 1000));
  const m = Math.floor(left / 60);
  const s = String(left % 60).padStart(2, "0");
  return `${m}:${s}`;
}

function sideClass(side) {
  return side === "Up" || side === "UP" ? "positive" : side === "Down" || side === "DOWN" ? "negative" : "";
}

function setMetric(el, value, formatter = money, classResolver = cls) {
  animateMetric(el, toNumber(value) || 0, formatter, classResolver);
}

function animateMetric(el, value, formatter = money, classResolver = null) {
  const target = Number.isFinite(value) ? value : 0;
  const existing = metricAnimations.get(el);
  if (existing?.frame) cancelAnimationFrame(existing.frame);
  const ready = el.dataset.metricReady === "true";
  const from = ready ? Number(el.dataset.metricValue || target) : target;
  const renderValue = (nextValue) => {
    el.textContent = formatMetricValue(formatter, nextValue);
    el.className = classResolver ? classResolver(nextValue) : "";
  };
  if (!ready || Math.abs(from - target) < 0.000001) {
    el.dataset.metricReady = "true";
    el.dataset.metricValue = String(target);
    renderValue(target);
    metricAnimations.set(el, { value: target, frame: null });
    return;
  }

  const startedAt = performance.now();
  el.classList.add("metric-animating");
  const step = (timestamp) => {
    const progress = Math.min(1, (timestamp - startedAt) / METRIC_ANIMATION_MS);
    const eased = 1 - Math.pow(1 - progress, 3);
    const nextValue = from + (target - from) * eased;
    renderValue(nextValue);
    el.dataset.metricValue = String(nextValue);
    if (progress < 1) {
      const frame = requestAnimationFrame(step);
      metricAnimations.set(el, { value: nextValue, frame });
      return;
    }
    el.classList.remove("metric-animating");
    el.dataset.metricValue = String(target);
    renderValue(target);
    metricAnimations.set(el, { value: target, frame: null });
  };
  const frame = requestAnimationFrame(step);
  metricAnimations.set(el, { value: from, frame });
}

function formatMetricValue(formatter, value) {
  if (typeof formatter === "function") return formatter(value);
  if (formatter && typeof formatter.format === "function") return formatter.format(value);
  return String(value);
}

function normalizeAppPage(value) {
  const page = String(value || "").replace(/^#/, "").trim().toLowerCase();
  return APP_PAGE_KEYS.has(page) ? page : "bot";
}

function locationAppPage() {
  return normalizeAppPage(window.location.hash || "bot");
}

function setActiveAppPage(page, options = {}) {
  const nextPage = normalizeAppPage(page);
  activeAppPage = nextPage;
  for (const button of ids.navItems || []) {
    const selected = button.dataset.navPage === nextPage;
    button.classList.toggle("is-active", selected);
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  for (const [key, element] of [
    ["bot", ids.botPage],
    ["analysis", ids.analysisPage],
  ]) {
    if (!element) continue;
    const selected = key === nextPage;
    element.classList.toggle("is-active", selected);
    element.classList.toggle("is-hidden", !selected);
    element.setAttribute("aria-hidden", selected ? "false" : "true");
  }
  document.body.dataset.page = nextPage;
  if (options.syncHash !== false) {
    const hash = `#${nextPage}`;
    if (window.location.hash !== hash) {
      window.history.pushState({ page: nextPage }, "", hash);
    }
  }
  if (nextPage === "bot" && latestStatus) {
    window.setTimeout(() => renderAll(latestStatus, { force: true, forceChart: true }), 210);
  }
  if (nextPage === "analysis") {
    applyAnalysisRealtimeVisibility();
    renderActorAnalysis(actorAnalysis);
    renderLlmReview(llmReview);
    renderRealtimeAnalysis();
    if (!actorAnalysis || Date.now() - lastActorAnalysisFetchMs > 5_000) {
      loadActorAnalysis({ force: false }).catch(renderActorAnalysisError);
    }
    if (!llmReview || Date.now() - lastLlmReviewFetchMs > 15_000) {
      loadLlmReview({ force: false }).catch(renderLlmReviewError);
    }
  }
}

function createRealtimeAnalysisState() {
  return {
    market_slug: "",
    started_at_ms: null,
    last_event_at_ms: null,
    market_event_count: 0,
    price_event_count: 0,
    counts: {
      book: 0,
      best_bid_ask: 0,
      price_change: 0,
      last_trade_price: 0,
      market_resolved: 0,
      price_tick: 0,
    },
    last_trade: null,
    trades: [],
    events: [],
    price_ticks: [],
  };
}

function loadAnalysisRealtimeVisible() {
  try {
    const raw = localStorage.getItem(ANALYSIS_REALTIME_VISIBILITY_KEY);
    return raw === null ? true : raw !== "false";
  } catch (_) {
    return true;
  }
}

function setAnalysisRealtimeVisible(visible) {
  analysisRealtimeVisible = Boolean(visible);
  try {
    localStorage.setItem(ANALYSIS_REALTIME_VISIBILITY_KEY, analysisRealtimeVisible ? "true" : "false");
  } catch (_) {
    // localStorage may be unavailable in private or restricted contexts.
  }
  applyAnalysisRealtimeVisibility();
  if (analysisRealtimeVisible) renderRealtimeAnalysis();
}

function applyAnalysisRealtimeVisibility() {
  if (ids.analysisRealtimeToggle) ids.analysisRealtimeToggle.checked = analysisRealtimeVisible;
  if (ids.analysisRealtimeCard) {
    ids.analysisRealtimeCard.hidden = !analysisRealtimeVisible;
    ids.analysisRealtimeCard.setAttribute("aria-hidden", analysisRealtimeVisible ? "false" : "true");
  }
}

function resetRealtimeAnalysisForMarket(market = activeMarket) {
  analysisRealtime = createRealtimeAnalysisState();
  analysisRealtime.market_slug = market?.slug || market?.round_id || "";
  analysisRealtime.started_at_ms = Date.now();
  queueRealtimeAnalysisRender();
}

function recordRealtimeMarketEvent(message, details = {}) {
  if (!message?.event_type) return;
  if (analysisRealtime.market_slug !== (activeMarket?.slug || activeMarket?.round_id || "")) {
    resetRealtimeAnalysisForMarket(activeMarket);
  }
  const now = Date.now();
  const type = String(message.event_type || "");
  analysisRealtime.last_event_at_ms = now;
  analysisRealtime.market_event_count += 1;
  analysisRealtime.counts[type] = (analysisRealtime.counts[type] || 0) + 1;
  const side = details.side || tokenSide(message.asset_id) || "";
  const eventRow = {
    at_ms: now,
    type,
    side,
    price: details.price ?? message.price ?? message.best_ask ?? message.best_bid ?? null,
    size: details.size ?? message.size ?? null,
    action: details.action || message.side || "",
    source: "polymarket-market-ws",
  };
  if (type === "last_trade_price") {
    const trade = {
      ...eventRow,
      price: toNumber(message.price),
      size: toNumber(message.size),
      notional: (toNumber(message.price) || 0) * (toNumber(message.size) || 0),
      action: String(message.side || "").toUpperCase(),
    };
    analysisRealtime.last_trade = trade;
    analysisRealtime.trades.unshift(trade);
    analysisRealtime.trades = analysisRealtime.trades.slice(0, ANALYSIS_REALTIME_EVENT_LIMIT);
    eventRow.price = trade.price;
    eventRow.size = trade.size;
    eventRow.action = trade.action;
  }
  analysisRealtime.events.unshift(eventRow);
  analysisRealtime.events = analysisRealtime.events.slice(0, ANALYSIS_REALTIME_EVENT_LIMIT);
  queueRealtimeAnalysisRender();
}

function recordRealtimePriceEvent(source, value, updatedAtMs = Date.now()) {
  const parsed = toNumber(value);
  if (parsed == null) return;
  if (analysisRealtime.market_slug !== (activeMarket?.slug || activeMarket?.round_id || "")) {
    resetRealtimeAnalysisForMarket(activeMarket);
  }
  const now = Date.now();
  const row = {
    at_ms: now,
    updated_at_ms: updatedAtMs,
    type: "price_tick",
    source,
    value: parsed,
  };
  analysisRealtime.last_event_at_ms = now;
  analysisRealtime.price_event_count += 1;
  analysisRealtime.counts.price_tick = (analysisRealtime.counts.price_tick || 0) + 1;
  analysisRealtime.price_ticks.unshift(row);
  analysisRealtime.price_ticks = analysisRealtime.price_ticks.slice(0, ANALYSIS_REALTIME_EVENT_LIMIT);
  analysisRealtime.events.unshift(row);
  analysisRealtime.events = analysisRealtime.events.slice(0, ANALYSIS_REALTIME_EVENT_LIMIT);
  queueRealtimeAnalysisRender();
}

function queueRealtimeAnalysisRender() {
  if (!analysisRealtimeVisible || activeAppPage !== "analysis" || !pageVisible || analysisRealtimeRenderQueued) return;
  analysisRealtimeRenderQueued = true;
  window.requestAnimationFrame(() => {
    analysisRealtimeRenderQueued = false;
    renderRealtimeAnalysis();
  });
}

function renderRealtimeAnalysis() {
  applyAnalysisRealtimeVisibility();
  if (!analysisRealtimeVisible) return;
  if (!ids.analysisRealtime) return;
  if (!activeMarket) {
    setAnalysisBlock(ids.analysisRealtime, `<div class="analysis-empty">等待当前市场</div>`, true);
    return;
  }
  const now = Date.now();
  const up = quotes.Up || {};
  const down = quotes.Down || {};
  const pressure = realtimeTradePressure(now);
  const external = realtimeExternalState();
  const eventRows = analysisRealtime.events.slice(0, 18).map((row) => realtimeEventHtml(row, now)).join("");
  const html = `
    <div class="analysis-body realtime-analysis">
      <div class="analysis-mini-grid realtime-metrics">
        ${analysisMetric("Market WS", realtimeStatusText(marketWsStatus, analysisRealtime.last_event_at_ms))}
        ${analysisMetric("RTDS/OKX/Binance", `${safe(priceWsStatus)} / ${safe(okxWsStatus)} / ${safe(binanceMarketWsStatus)}`)}
        ${analysisMetric("盘口事件", fmtNumberCell(analysisRealtime.market_event_count, 0))}
        ${analysisMetric("价格事件", fmtNumberCell(analysisRealtime.price_event_count, 0))}
        ${analysisMetric("Up 买/卖", `${fmtQuotePrice(up.best_bid)} / ${fmtQuotePrice(up.best_ask)}`)}
        ${analysisMetric("Down 买/卖", `${fmtQuotePrice(down.best_bid)} / ${fmtQuotePrice(down.best_ask)}`)}
        ${analysisMetric("Up/Down spread", `${fmtSpread(up)} / ${fmtSpread(down)}`)}
        ${analysisMetric("成交压力 10s", realtimePressureHtml(pressure))}
        ${analysisMetric("Chainlink 距目标", fmtSignedBpsCell(external.chainlink_target_bps))}
        ${analysisMetric("OKX-Chainlink", fmtSignedBpsCell(external.okx_chainlink_bps))}
        ${analysisMetric("Binance-Chainlink", fmtSignedBpsCell(external.binance_chainlink_bps))}
        ${analysisMetric("最新成交", realtimeLastTradeText(now))}
      </div>
      <div class="analysis-realtime-split">
        <div class="analysis-realtime-book">
          ${realtimeBookSideHtml("Up", up)}
          ${realtimeBookSideHtml("Down", down)}
        </div>
        <div class="analysis-event-tape">
          <div class="analysis-event-title">实时事件流</div>
          <div class="analysis-event-list">${eventRows || "<div class=\"analysis-event-empty\">等待 WebSocket 事件</div>"}</div>
        </div>
      </div>
    </div>
  `;
  setAnalysisBlock(ids.analysisRealtime, html);
  renderAnalysisProbability((actorAnalysis?.actor_analysis || actorAnalysis || {}).probability || {});
}

function realtimeStatusText(status, lastAtMs) {
  const age = lastAtMs ? `${Math.max(0, Date.now() - lastAtMs)}ms` : "-";
  return `${safe(status || "-")} · ${age}`;
}

function realtimeTradePressure(now = Date.now()) {
  const cutoff = now - ANALYSIS_RECENT_WINDOW_MS;
  let up = 0;
  let down = 0;
  for (const trade of analysisRealtime.trades) {
    if (!trade.at_ms || trade.at_ms < cutoff) continue;
    const notional = Number(trade.notional || 0);
    if (!Number.isFinite(notional) || notional <= 0) continue;
    const action = String(trade.action || "").toUpperCase();
    if ((trade.side === "Up" && action === "BUY") || (trade.side === "Down" && action === "SELL")) up += notional;
    if ((trade.side === "Down" && action === "BUY") || (trade.side === "Up" && action === "SELL")) down += notional;
  }
  const total = up + down;
  const score = total > 0 ? (up - down) / total : 0;
  const direction = score >= 0.2 ? "Up" : score <= -0.2 ? "Down" : "Balanced";
  return { up, down, total, score, direction };
}

function realtimeExternalState() {
  const chainlink = toNumber(priceState.chainlink);
  const okx = toNumber(priceState.okx);
  const binance = toNumber(priceState.binance_market) ?? toNumber(priceState.binance);
  const target = marketTargetPrice(activeMarket) ?? toNumber(priceState.target_price);
  return {
    chainlink_target_bps: chainlink != null && target ? ((chainlink - target) / target) * 10_000 : null,
    okx_chainlink_bps: okx != null && chainlink ? ((okx - chainlink) / chainlink) * 10_000 : null,
    binance_chainlink_bps: binance != null && chainlink ? ((binance - chainlink) / chainlink) * 10_000 : null,
  };
}

function realtimeDirectionProbability(now = Date.now()) {
  const marketUp = realtimeMarketImpliedUp();
  const target = marketTargetPrice(activeMarket) ?? toNumber(priceState.target_price);
  const current = toNumber(priceState.chainlink) ?? toNumber(priceState.binance_market) ?? toNumber(priceState.binance) ?? toNumber(priceState.okx);
  const secondsLeft = activeMarket?.end_ts ? Math.max(0, activeMarket.end_ts - now / 1000) : null;
  const distanceBps = current != null && target ? ((current - target) / target) * 10_000 : null;
  const scale = secondsLeft != null ? Math.max(1.0, 12.0 * Math.sqrt(Math.max(secondsLeft, 1) / 300)) : 8.0;
  const targetUp = distanceBps == null ? null : logisticProbability(distanceBps, scale);
  const pressure = realtimeTradePressure(now);
  const pressureUp = pressure.total > 0 ? clamp01((pressure.score + 1) / 2) : null;
  const external = realtimeExternalState();
  const residuals = [external.okx_chainlink_bps, external.binance_chainlink_bps].filter((value) => toNumber(value) != null);
  const residualAvg = residuals.length ? residuals.reduce((sum, value) => sum + Number(value), 0) / residuals.length : null;
  const externalLeadUp = residualAvg == null ? null : logisticProbability(residualAvg, 6.0);
  const combined = weightedProbability([
    [marketUp, 0.45],
    [targetUp, 0.35],
    [pressureUp, 0.15],
    [externalLeadUp, 0.05],
  ]);
  const direction = combined == null ? "Balanced" : combined >= 0.55 ? "Up" : combined <= 0.45 ? "Down" : "Balanced";
  return {
    direction,
    combined_up: combined,
    combined_down: combined == null ? null : 1 - combined,
    market_up: marketUp,
    target_up: targetUp,
    pressure_up: pressureUp,
    external_lead_up: externalLeadUp,
    pressure,
    current_price: current,
    target_price: target,
    distance_bps: distanceBps,
    seconds_left: secondsLeft,
    latency_ms: analysisRealtime.last_event_at_ms ? Math.max(0, now - analysisRealtime.last_event_at_ms) : null,
    updated_at_ms: analysisRealtime.last_event_at_ms,
  };
}

function realtimeMarketImpliedUp() {
  const upMid = quoteMid("Up");
  if (upMid != null) return upMid;
  const downMid = quoteMid("Down");
  return downMid == null ? null : clamp01(1 - downMid);
}

function quoteMid(side) {
  const quote = quotes[side] || {};
  const bid = toNumber(quote.best_bid);
  const ask = toNumber(quote.best_ask);
  if (bid != null && ask != null) return clamp01((bid + ask) / 2);
  if (bid != null) return clamp01(bid);
  if (ask != null) return clamp01(ask);
  return null;
}

function logisticProbability(value, scale) {
  const normalizedScale = Math.max(0.1, Number(scale) || 1);
  return clamp01(1 / (1 + Math.exp(-Number(value) / normalizedScale)));
}

function weightedProbability(rows) {
  const available = rows.filter(([value, weight]) => toNumber(value) != null && Number(weight) > 0);
  if (!available.length) return null;
  const totalWeight = available.reduce((sum, [, weight]) => sum + Number(weight), 0);
  if (totalWeight <= 0) return null;
  return clamp01(available.reduce((sum, [value, weight]) => sum + Number(value) * Number(weight), 0) / totalWeight);
}

function clamp01(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 0;
  return Math.max(0, Math.min(1, parsed));
}

function realtimePressureHtml(pressure) {
  const clsName = sideClass(pressure.direction);
  const score = pressure.total > 0 ? `${pressure.score > 0 ? "+" : ""}${number.format(pressure.score * 100)}%` : "-";
  return `<span class="${clsName}">${safe(pressure.direction)}</span> <span class="muted">${safe(score)}</span>`;
}

function realtimeLastTradeText(now = Date.now()) {
  const trade = analysisRealtime.last_trade;
  if (!trade) return "-";
  const age = trade.at_ms ? `${Math.max(0, now - trade.at_ms)}ms` : "-";
  return `${safe(trade.side || "-")} ${safe(trade.action || "")} @ ${fmtQuotePrice(trade.price)} · ${fmtNumberCell(trade.size, 3)} · ${age}`;
}

function realtimeBookSideHtml(side, quote) {
  const bids = Array.isArray(quote.bids) ? quote.bids.slice(0, 5) : [];
  const asks = Array.isArray(quote.asks) ? quote.asks.slice(0, 5) : [];
  const rows = Array.from({ length: Math.max(bids.length, asks.length, 5) }).map((_, index) => {
    const bid = bids[index] || {};
    const ask = asks[index] || {};
    return `
      <tr>
        <td>${fmtQuotePrice(bid.price)}</td>
        <td>${fmtNumberCell(bid.size, 2)}</td>
        <td>${fmtQuotePrice(ask.price)}</td>
        <td>${fmtNumberCell(ask.size, 2)}</td>
      </tr>
    `;
  }).join("");
  return `
    <div class="analysis-book-side">
      <div class="analysis-book-title"><span class="${sideClass(side)}">${safe(side)}</span><small>${safe(quote.source || "-")}</small></div>
      <table class="analysis-book-table">
        <thead><tr><th>Bid</th><th>Size</th><th>Ask</th><th>Size</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

function realtimeEventHtml(row, now = Date.now()) {
  const age = row.at_ms ? `${Math.max(0, now - row.at_ms)}ms` : "-";
  let text = row.type;
  if (row.type === "price_tick") {
    text = `${row.source} ${fmtNumberCell(row.value, 2)}`;
  } else if (row.type === "last_trade_price") {
    text = `${row.side || "-"} ${row.action || ""} @ ${fmtQuotePrice(row.price)} · ${fmtNumberCell(row.size, 3)}`;
  } else if (row.type === "price_change" || row.type === "best_bid_ask") {
    text = `${row.side || "-"} ${row.type} @ ${fmtQuotePrice(row.price)}`;
  } else if (row.type === "book") {
    text = `${row.side || "-"} book snapshot`;
  }
  return `
    <div class="analysis-event-row">
      <span>${safe(age)}</span>
      <strong>${safe(text)}</strong>
    </div>
  `;
}

function fmtQuotePrice(value) {
  const parsed = toNumber(value);
  return parsed == null ? "-" : parsed.toFixed(3);
}

function fmtSpread(quote) {
  const bid = toNumber(quote.best_bid);
  const ask = toNumber(quote.best_ask);
  if (bid == null || ask == null) return "-";
  return `${number.format((ask - bid) * 100)}c`;
}

function renderActorAnalysis(payload) {
  const analysis = payload?.actor_analysis || payload || {};
  const summary = analysis.summary || {};
  const probability = analysis.probability || {};
  const status = analysis.status || (actorAnalysisLoading ? "LOADING" : "EMPTY");
  if (ids.analysisStatus) ids.analysisStatus.textContent = actorStatusLabel(status);
  if (ids.analysisAffectsTrading) ids.analysisAffectsTrading.textContent = analysis.affects_trading ? "是" : "否";
  if (ids.analysisDataScope) {
    const walletCount = summary.wallet_count ?? 0;
    const positionCount = summary.position_count ?? 0;
    const tradeCount = summary.trade_count ?? 0;
    ids.analysisDataScope.textContent = `${walletCount} 地址 / ${positionCount} 仓位 / ${tradeCount} 成交`;
  }
  if (ids.analysisLlmPath) {
    ids.analysisLlmPath.textContent = analysis.analysis_only === false ? "接入执行链路" : "复盘旁路";
  }
  renderAnalysisSummary(analysis, summary);
  renderAnalysisWallets(analysis.wallets || []);
  renderAnalysisProbability(probability);
  renderAnalysisRiskTags(analysis.risk_tags || [], analysis.notes || []);
}

function renderActorAnalysisError(error) {
  actorAnalysisError = error?.message || String(error || "分析接口异常");
  if (ids.analysisStatus) ids.analysisStatus.textContent = "异常";
  setAnalysisBlock(ids.analysisSummary, `<div class="analysis-alert">${safe(actorAnalysisError)}</div>`);
  setAnalysisBlock(ids.analysisWallets, `<div class="analysis-empty">暂无地址数据</div>`, true);
  renderAnalysisProbability((actorAnalysis?.actor_analysis || actorAnalysis || {}).probability || {});
  setAnalysisBlock(ids.analysisRiskTags, `<div class="analysis-empty">暂无风险标签</div>`, true);
}

function actorStatusLabel(status) {
  const labels = {
    READY: "已接入",
    PARTIAL: "部分数据",
    EMPTY: "暂无样本",
    NO_MARKET: "无当前市场",
    NO_CONDITION_ID: "缺少市场ID",
    LOADING: "加载中",
  };
  return labels[status] || safe(status || "-");
}

function renderAnalysisSummary(analysis, summary) {
  if (!ids.analysisSummary) return;
  const market = analysis.market || {};
  const sources = analysis.sources || {};
  const sourceRows = Object.entries(sources).map(([name, source]) => `
    <div class="analysis-source ${source.ok ? "is-ok" : "is-error"}">
      <span>${safe(name)}</span>
      <strong>${source.ok ? "OK" : "ERR"} · ${safe(source.count ?? 0)}</strong>
    </div>
  `).join("");
  const html = `
    <div class="analysis-body">
      <div class="analysis-mini-grid">
        ${analysisMetric("市场", market.slug || market.round_id || "-")}
        ${analysisMetric("检查时间", fmtTime(analysis.checked_at))}
        ${analysisMetric("Top 地址占比", fmtPctLike(summary.top_wallet_share_pct))}
        ${analysisMetric("活跃地址", summary.active_wallet_count ?? 0)}
      </div>
      <div class="analysis-sources">${sourceRows || "<span class=\"muted\">暂无来源</span>"}</div>
      <div class="analysis-note">订单簿当前挂单地址不可见，本页只用公开持仓、持有人和成交数据做旁路观察。</div>
    </div>
  `;
  setAnalysisBlock(ids.analysisSummary, html);
}

function renderAnalysisWallets(wallets) {
  if (!ids.analysisWallets) return;
  if (!wallets.length) {
    setAnalysisBlock(ids.analysisWallets, `<div class="analysis-empty">暂无地址数据</div>`, true);
    return;
  }
  const rows = wallets.slice(0, 10).map((row) => `
    <tr>
      <td><span class="mono">${safe(row.short_address || row.address)}</span><small>${safe(row.name || "")}</small></td>
      <td><span class="${sideClass(row.bias)}">${safe(row.bias || "Balanced")}</span></td>
      <td>${fmtMoneyCell(row.up_value)}</td>
      <td>${fmtMoneyCell(row.down_value)}</td>
      <td>${fmtSignedMoneyCell(row.pnl)}</td>
      <td>${fmtNumberCell(row.trade_count, 0)}</td>
      <td>${analysisTagsHtml(row.tags || [])}</td>
    </tr>
  `).join("");
  setAnalysisBlock(ids.analysisWallets, `
    <div class="analysis-table-wrap">
      <table class="analysis-table">
        <thead><tr><th>地址</th><th>偏向</th><th>Up</th><th>Down</th><th>PnL</th><th>成交</th><th>标签</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `);
}

function renderAnalysisProbability(probability) {
  if (!ids.analysisProbability) return;
  const actorProbability = probability && Object.keys(probability).length ? probability : {};
  const realtime = realtimeDirectionProbability();
  const hasRealtime = Boolean(activeMarket && (
    realtime.combined_up != null
    || realtime.market_up != null
    || realtime.target_up != null
    || realtime.pressure_up != null
  ));
  const hasActorProbability = Object.keys(actorProbability).length > 0;
  if (!hasRealtime && !hasActorProbability && !actorAnalysisError) {
    setAnalysisBlock(ids.analysisProbability, `<div class="analysis-empty">暂无概率数据</div>`, true);
    return;
  }
  const actorAnalysisPayload = actorAnalysis?.actor_analysis || actorAnalysis || {};
  const actorCheckedAt = actorAnalysisPayload.checked_at ? fmtTime(actorAnalysisPayload.checked_at) : "-";
  const actorCacheLabel = actorAnalysisPayload.cached ? "缓存" : "近实时";
  const actorHtml = actorAnalysisError
    ? `<div class="analysis-alert">${safe(actorAnalysisError)}</div>`
    : hasActorProbability
      ? `
        <div class="analysis-probability-main analysis-probability-slow">
          <span>地址修正概率 <small>Data API ${safe(actorCacheLabel)} · ${safe(actorCheckedAt)}</small></span>
          <strong class="${sideClass(actorProbability.direction)}">${safe(actorProbability.direction || "Balanced")}</strong>
          ${probabilityBarHtml(actorProbability.combined_up)}
        </div>
        <div class="analysis-mini-grid">
          ${analysisMetric("地址修正 Up", fmtProb(actorProbability.combined_up))}
          ${analysisMetric("市场隐含 Up", fmtProb(actorProbability.market_implied_up))}
          ${analysisMetric("价格模型 Up", fmtProb(actorProbability.price_model_up))}
          ${analysisMetric("地址敞口 Up", fmtProb(actorProbability.actor_up_ratio))}
          ${analysisMetric("Data API 当前价", fmtNumberCell(actorProbability.current_price, 2))}
          ${analysisMetric("Data API 目标价", fmtNumberCell(actorProbability.target_price, 2))}
          ${analysisMetric("Data API 距离", fmtSignedBpsCell(actorProbability.distance_bps))}
          ${analysisMetric("Data API 剩余秒", fmtNumberCell(actorProbability.seconds_left, 1))}
        </div>
      `
      : `<div class="analysis-note">地址修正概率等待 Data API 返回；实时方向概率不依赖地址画像。</div>`;
  const html = `
    <div class="analysis-body probability-body">
      <div class="analysis-probability-main analysis-probability-live">
        <span>实时方向概率 <small>WebSocket · ${safe(realtime.latency_ms == null ? "-" : `${Math.round(realtime.latency_ms)}ms`)}</small></span>
        <strong class="${sideClass(realtime.direction)}">${safe(realtime.direction || "Balanced")}</strong>
        ${probabilityBarHtml(realtime.combined_up)}
      </div>
      <div class="analysis-mini-grid">
        ${analysisMetric("实时 Up", fmtProb(realtime.combined_up))}
        ${analysisMetric("盘口隐含 Up", fmtProb(realtime.market_up))}
        ${analysisMetric("目标价模型 Up", fmtProb(realtime.target_up))}
        ${analysisMetric("成交压力 Up", fmtProb(realtime.pressure_up))}
        ${analysisMetric("外部领先 Up", fmtProb(realtime.external_lead_up))}
        ${analysisMetric("实时当前价", fmtNumberCell(realtime.current_price, 2))}
        ${analysisMetric("实时距离", fmtSignedBpsCell(realtime.distance_bps))}
        ${analysisMetric("实时剩余秒", fmtNumberCell(realtime.seconds_left, 1))}
      </div>
      <div class="analysis-probability-divider">地址修正概率</div>
      ${actorHtml}
    </div>
  `;
  setAnalysisBlock(ids.analysisProbability, html);
}

function probabilityBarHtml(value) {
  const parsed = toNumber(value);
  const pct = parsed == null ? 50 : clamp01(parsed) * 100;
  return `
    <div class="analysis-probability-bar" aria-hidden="true">
      <span style="width: ${pct.toFixed(2)}%"></span>
    </div>
  `;
}

function renderAnalysisRiskTags(tags, notes) {
  if (!ids.analysisRiskTags) return;
  const tagHtml = tags.length
    ? tags.map((tag) => `
        <div class="analysis-risk ${safe(tag.severity || "info")}">
          <strong>${safe(tag.label || tag.code)}</strong>
          <span>${safe(tag.message || "")}</span>
        </div>
      `).join("")
    : `<div class="analysis-empty">暂无风险标签</div>`;
  const noteHtml = notes.length
    ? `<div class="analysis-notes">${notes.map((note) => `<p>${safe(note)}</p>`).join("")}</div>`
    : "";
  setAnalysisBlock(ids.analysisRiskTags, `<div class="analysis-body">${tagHtml}${noteHtml}</div>`);
}

function renderLlmReview(payload) {
  if (!ids.analysisLlmReview) return;
  const root = payload?.llm_review || payload || {};
  const review = root.primary || (Array.isArray(root.variants) ? root.variants[0] : null) || root;
  if (!review || review.status === "DISABLED") {
    const text = llmReviewLoading ? "加载中" : (root.message || "LLM SUPER AGENT + PAPER 未启用");
    setAnalysisBlock(ids.analysisLlmReview, `<div class="analysis-empty">${safe(text)}</div>`, true);
    return;
  }
  if (review.status === "EMPTY") {
    setAnalysisBlock(ids.analysisLlmReview, `<div class="analysis-empty">暂无 LLM 决策样本</div>`, true);
    return;
  }
  if (llmReviewError) {
    setAnalysisBlock(ids.analysisLlmReview, `<div class="analysis-alert">${safe(llmReviewError)}</div>`);
    return;
  }
  const summary = review.summary || {};
  const routeRows = (review.route_stats || []).slice(0, 12).map((row) => `
    <tr>
      <td><span class="mono">${safe(row.key)}</span></td>
      <td>${fmtNumberCell(row.decision_count, 0)}</td>
      <td>${fmtPctCell(row.allow_rate)}</td>
      <td>${fmtNumberCell(row.trade_count, 0)}</td>
      <td>${fmtNumberCell(row.settled_trade_count, 0)}</td>
      <td>${fmtSignedMoneyCell(row.total_pnl)}</td>
      <td>${fmtSignedMoneyCell(row.no_trade_direction_estimated_pnl)}</td>
      <td>${fmtPctCell(row.no_trade_direction_win_rate)}</td>
    </tr>
  `).join("");
  const reasonRows = (review.reason_stats || []).slice(0, 18).map((row) => `
    <tr>
      <td><span class="analysis-tag">${safe(row.key)}</span></td>
      <td>${fmtNumberCell(row.decision_count, 0)}</td>
      <td>${fmtNumberCell(row.block_count, 0)}</td>
      <td>${fmtNumberCell(row.trade_count, 0)}</td>
      <td>${fmtSignedMoneyCell(row.total_pnl)}</td>
      <td>${fmtSignedMoneyCell(row.no_trade_direction_estimated_pnl)}</td>
      <td>${fmtPctCell(row.no_trade_direction_win_rate)}</td>
    </tr>
  `).join("");
  const decisionRows = (review.recent_decisions || []).slice(0, 30).map((row) => `
    <tr>
      <td>${fmtTime(row.created_at)}<small>${safe(row.round_id)}</small></td>
      <td><span class="mono">${safe(row.route)}</span><small>${safe(row.source)}</small></td>
      <td>${row.allow_trade ? "<span class=\"positive\">ALLOW</span>" : "<span class=\"negative\">BLOCK</span>"}</td>
      <td>${fmtNumberCell(row.confidence, 4)}</td>
      <td><span class="${sideClass(row.feature_direction_side)}">${safe(row.feature_direction_side || "-")}</span><small>${fmtSignedBpsCell(row.feature_distance_bps)}</small></td>
      <td><span class="${sideClass(row.outcome)}">${safe(row.outcome || "-")}</span><small>${safe(row.settlement_source || "-")}</small></td>
      <td>${fmtSignedMoneyCell(row.matched_trade_pnl)}<small>${fmtNumberCell(row.matched_settled_trade_count, 0)} settled</small></td>
      <td>${llmNoTradeEstimateHtml(row.no_trade_estimate)}</td>
      <td>${analysisTagsHtml(row.reason_codes || [])}<small>${safe(row.market_regime || "")}</small></td>
    </tr>
  `).join("");
  const html = `
    <div class="analysis-body">
      <div class="analysis-mini-grid">
        ${analysisMetric("组合", `${review.combo || review.variant_id || "-"}`)}
        ${analysisMetric("样本决策", `${fmtNumberCell(summary.decision_count, 0)} / ${fmtNumberCell(summary.total_decision_count, 0)}`)}
        ${analysisMetric("允许下单率", fmtPctCell(summary.allow_rate))}
        ${analysisMetric("LLM/本地来源", `${fmtNumberCell(summary.llm_source_count, 0)} / ${fmtNumberCell(summary.local_source_count, 0)}`)}
        ${analysisMetric("归因成交PnL", fmtSignedMoneyCell(summary.total_pnl))}
        ${analysisMetric("归因成交胜率", fmtPctCell(summary.win_rate))}
        ${analysisMetric("NO_TRADE方向胜率", fmtPctCell(summary.no_trade_direction_win_rate))}
        ${analysisMetric("NO_TRADE估算PnL", fmtSignedMoneyCell(summary.no_trade_direction_estimated_pnl))}
      </div>
      <div class="analysis-note">
        ${safe(review.attribution_note || "NO_TRADE 机会成本为估算值；实盘可成交价格、滑点和手续费不在此估算内。")}
        ${root.generated_at ? ` · 更新 ${safe(fmtTime(root.generated_at))}` : ""}
      </div>
      <div class="analysis-review-section">
        <div class="analysis-review-heading"><strong>Route 归因</strong><span>按 LLM route 汇总实际成交和 NO_TRADE 估算</span></div>
        <div class="analysis-table-wrap">
          <table class="analysis-table">
            <thead><tr><th>Route</th><th>决策</th><th>允许率</th><th>成交</th><th>已结算</th><th>实际PnL</th><th>NO_TRADE估算</th><th>NO_TRADE命中</th></tr></thead>
            <tbody>${routeRows || "<tr><td colspan=\"8\">暂无 route 数据</td></tr>"}</tbody>
          </table>
        </div>
      </div>
      <div class="analysis-review-section">
        <div class="analysis-review-heading"><strong>Reason Code 归因</strong><span>用于看哪些理由在赚钱或错过机会</span></div>
        <div class="analysis-table-wrap">
          <table class="analysis-table">
            <thead><tr><th>Code</th><th>决策</th><th>阻止</th><th>成交</th><th>实际PnL</th><th>NO_TRADE估算</th><th>NO_TRADE命中</th></tr></thead>
            <tbody>${reasonRows || "<tr><td colspan=\"7\">暂无 reason code 数据</td></tr>"}</tbody>
          </table>
        </div>
      </div>
      <div class="analysis-review-section analysis-review-table">
        <div class="analysis-review-heading"><strong>最近决策</strong><span>用于核对 LLM 具体判断是否有效</span></div>
        <div class="analysis-table-wrap">
          <table class="analysis-table">
            <thead><tr><th>时间</th><th>Route</th><th>动作</th><th>置信</th><th>方向</th><th>结算</th><th>实际PnL</th><th>NO_TRADE估算</th><th>Codes</th></tr></thead>
            <tbody>${decisionRows || "<tr><td colspan=\"9\">暂无最近决策</td></tr>"}</tbody>
          </table>
        </div>
      </div>
    </div>
  `;
  setAnalysisBlock(ids.analysisLlmReview, html);
}

function renderLlmReviewError(error) {
  llmReviewError = error?.message || String(error || "LLM 复盘接口异常");
  setAnalysisBlock(ids.analysisLlmReview, `<div class="analysis-alert">${safe(llmReviewError)}</div>`);
}

function llmNoTradeEstimateHtml(estimate) {
  if (!estimate || !estimate.evaluated) return "-";
  const result = estimate.direction_would_win ? "命中" : "未命中";
  return `${fmtSignedMoneyCell(estimate.direction_estimated_pnl)}<small>${safe(estimate.direction_side || "-")} -> ${safe(estimate.outcome || "-")} · ${safe(result)}</small>`;
}

function analysisMetric(label, value) {
  const raw = value === null || value === undefined ? "-" : String(value);
  const content = raw.startsWith("<span ") ? raw : safe(raw);
  return `<div class="analysis-metric"><span>${safe(label)}</span><strong>${content}</strong></div>`;
}

function analysisTagsHtml(tags) {
  return tags.map((tag) => `<span class="analysis-tag">${safe(tag)}</span>`).join(" ");
}

function fmtProb(value) {
  const parsed = toNumber(value);
  return parsed == null ? "-" : `${number.format(parsed * 100)}%`;
}

function fmtPctLike(value) {
  const parsed = toNumber(value);
  return parsed == null ? "-" : `${number.format(parsed)}%`;
}

function setAnalysisBlock(element, html, empty = false) {
  if (!element) return;
  element.classList.toggle("analysis-empty", empty);
  element.classList.toggle("analysis-body-wrap", !empty);
  element.innerHTML = html;
}

async function loadActorAnalysis({ force = false } = {}) {
  if (actorAnalysisLoading) return;
  actorAnalysisLoading = true;
  ids.analysisRefresh?.classList.add("is-loading");
  if (ids.analysisRefresh) ids.analysisRefresh.disabled = true;
  if (!actorAnalysis && ids.analysisStatus) ids.analysisStatus.textContent = "加载中";
  try {
    const params = new URLSearchParams({ refresh: force ? "true" : "false" });
    const res = await fetch(`/api/actor-analysis?${params.toString()}`);
    if (!res.ok) throw new Error(`actor analysis HTTP ${res.status}`);
    actorAnalysis = await res.json();
    actorAnalysisError = "";
    lastActorAnalysisFetchMs = Date.now();
    renderActorAnalysis(actorAnalysis);
  } finally {
    actorAnalysisLoading = false;
    ids.analysisRefresh?.classList.remove("is-loading");
    if (ids.analysisRefresh) ids.analysisRefresh.disabled = false;
  }
}

async function loadLlmReview({ force = false } = {}) {
  if (llmReviewLoading) return;
  llmReviewLoading = true;
  ids.analysisLlmReviewRefresh?.classList.add("is-loading");
  if (ids.analysisLlmReviewRefresh) ids.analysisLlmReviewRefresh.disabled = true;
  if (!llmReview && ids.analysisLlmReview) {
    setAnalysisBlock(ids.analysisLlmReview, `<div class="analysis-empty">加载中</div>`, true);
  }
  try {
    const params = new URLSearchParams({ limit: "80", refresh: force ? "true" : "false" });
    const res = await fetch(`/api/llm-review?${params.toString()}`);
    if (!res.ok) throw new Error(`llm review HTTP ${res.status}`);
    llmReview = await res.json();
    llmReviewError = "";
    lastLlmReviewFetchMs = Date.now();
    renderLlmReview(llmReview);
  } finally {
    llmReviewLoading = false;
    ids.analysisLlmReviewRefresh?.classList.remove("is-loading");
    if (ids.analysisLlmReviewRefresh) ids.analysisLlmReviewRefresh.disabled = false;
  }
}

function renderMetrics(metrics = {}) {
  setMetric(ids.totalEquity, metrics.total_equity, money, null);
  setMetric(ids.totalPnl, metrics.total_pnl, signedMoney, cls);
  setMetric(ids.unrealizedPnl, metrics.unrealized_pnl, signedMoney, cls);
  setMetric(ids.cashBalance, metrics.cash_balance, money, null);
  setMetric(ids.reservedCash, metrics.reserved_cash, money, null);
  setMetric(ids.openRisk, metrics.open_risk, money, null);
  setMetric(ids.winRate, metrics.win_rate, percentText, null);
  setMetric(ids.maxDrawdown, metrics.max_drawdown, money, null);
}

function renderAccountScope(data = latestStatus) {
  const options = accountScopeOptions(data);
  if (!options.some((option) => option.value === accountScope)) {
    accountScope = "main";
    equityCurveRows = [];
    equityCurveMeta = {};
    lastEquityCurveFetchMs = 0;
  }
  const optionsKey = options.map((option) => `${option.value}:${option.label}`).join("|");
  if (ids.accountScopeSelect && optionsKey !== accountScopeOptionsKey) {
    ids.accountScopeSelect.innerHTML = options
      .map((option) => `<option value="${safe(option.value)}">${safe(option.label)}</option>`)
      .join("");
    accountScopeOptionsKey = optionsKey;
  }
  if (ids.accountScopeSelect) ids.accountScopeSelect.value = accountScope;
  const current = accountScopeMeta(data);
  if (ids.accountScopeLabel) ids.accountScopeLabel.textContent = current.label;
  if (ids.accountScopeSource) ids.accountScopeSource.textContent = current.source;
  if (ids.paperOnly) ids.paperOnly.textContent = parseAccountScope(accountScope).scope === "live" ? `${current.label} · Live` : `${current.label} · Paper`;
  renderDataScopeOptions(data);
}

function renderDataScopeOptions(data = latestStatus) {
  const options = tableScopeOptions(data);
  const optionsKey = options.map((option) => `${option.value}:${option.label}`).join("|");
  if (optionsKey === tableScopeOptionsKey) return;
  tableScopeOptionsKey = optionsKey;
  for (const [select, current] of [
    [ids.openDataScope, openDataScope],
    [ids.orderDataScope, orderDataScope],
    [ids.recentDataScope, recentDataScope],
  ]) {
    if (!select) continue;
    select.innerHTML = options.map((option) => `<option value="${safe(option.value)}">${safe(option.label)}</option>`).join("");
    select.value = options.some((option) => option.value === current) ? current : "main";
  }
  if (!options.some((option) => option.value === openDataScope)) openDataScope = "main";
  if (!options.some((option) => option.value === orderDataScope)) orderDataScope = "main";
  if (!options.some((option) => option.value === recentDataScope)) recentDataScope = "main";
}

function accountScopeOptions(data = latestStatus) {
  const variants = strategyExperimentVariants(data);
  const live = liveVariant(data);
  return [
    { value: "main", label: "主账户" },
    ...variants.map((row) => ({
      value: `experiment:${row.variant_id}`,
      label: row.combo || row.variant_id,
    })),
    ...(live ? [{ value: `live:${live.variant_id || "SINGLE_FAK_REAL"}`, label: live.combo || "SINGLE_FAK_REAL" }] : []),
  ];
}

function tableScopeOptions(data = latestStatus) {
  const variants = strategyExperimentVariants(data);
  const live = liveVariant(data);
  return [
    { value: "main", label: "主账户" },
    { value: "experiment", label: "策略实验全部" },
    ...variants.map((row) => ({
      value: `experiment:${row.variant_id}`,
      label: row.combo || row.variant_id,
    })),
    ...(live ? [{ value: `live:${live.variant_id || "SINGLE_FAK_REAL"}`, label: live.combo || "SINGLE_FAK_REAL" }] : []),
  ];
}

function strategyExperimentVariants(data = latestStatus) {
  const variants = data?.runtime?.strategy_experiments?.variants;
  return Array.isArray(variants) ? variants : [];
}

function liveVariant(data = latestStatus) {
  const variant = data?.runtime?.live_trading?.variant;
  return variant && typeof variant === "object" ? variant : null;
}

function selectedAccountVariant(data = latestStatus) {
  const selection = parseAccountScope(accountScope);
  if (selection.scope === "live") return liveVariant(data);
  if (selection.scope !== "experiment") return null;
  return strategyExperimentVariants(data).find((row) => row.variant_id === selection.variantId) || null;
}

function selectedAccountMetrics(data = latestStatus) {
  const variant = selectedAccountVariant(data);
  if (variant) return variant.metrics || {};
  return data?.metrics || {};
}

function accountScopeMeta(data = latestStatus) {
  const variant = selectedAccountVariant(data);
  if (variant) {
    if (variant.account_scope === "live" || parseAccountScope(accountScope).scope === "live") {
      return {
        label: variant.combo || variant.variant_id,
        source: `实盘隔离账户 · ${variant.variant_id}`,
      };
    }
    return {
      label: variant.combo || variant.variant_id,
      source: `策略实验隔离账户 · ${variant.variant_id}`,
    };
  }
  return { label: "主账户", source: "主 Paper 账户" };
}

function parseAccountScope(value) {
  const text = String(value || "main");
  if (text.startsWith("experiment:")) {
    return { scope: "experiment", variantId: text.slice("experiment:".length).toUpperCase() };
  }
  if (text === "experiment") {
    return { scope: "experiment_all", variantId: null };
  }
  if (text.startsWith("live:") || text === "live") {
    const variantId = text.includes(":") ? text.slice("live:".length).toUpperCase() : "SINGLE_FAK_REAL";
    return { scope: "live", variantId };
  }
  return { scope: "main", variantId: null };
}

function isExperimentAggregateScope(value) {
  return parseAccountScope(value).scope === "experiment_all";
}

function isExperimentVariantScope(value) {
  return parseAccountScope(value).scope === "experiment";
}

function isLiveScope(value) {
  return parseAccountScope(value).scope === "live";
}

function isMainScope(value) {
  return parseAccountScope(value).scope === "main";
}

function scopeLabel(value) {
  const parsed = parseAccountScope(value);
  if (parsed.scope === "main") return "主账户";
  if (parsed.scope === "experiment_all") return "策略实验";
  if (parsed.scope === "live") return liveVariant()?.combo || "SINGLE_FAK_REAL";
  const variant = strategyExperimentVariants().find((row) => row.variant_id === parsed.variantId);
  return variant?.combo || parsed.variantId || "策略实验";
}

function appendScopeParams(params, value) {
  const parsed = parseAccountScope(value);
  if (parsed.scope === "live") {
    params.set("account_scope", "live");
  } else if (parsed.scope === "experiment") {
    params.set("account_scope", "strategy_experiment");
    params.set("variant_id", parsed.variantId);
  } else {
    params.set("account_scope", "main");
  }
  return params;
}

function renderRuntime(runtime) {
  const hasError = Boolean(runtime.last_error);
  const stalePrice = priceState.chainlink_updated_ms ? Date.now() - priceState.chainlink_updated_ms > 5_000 : true;
  const paperPaused = Boolean(runtime.paper_trading?.paused);
  const runtimeError = hasError || (!paperPaused && stalePrice);
  ids.runtime.textContent = hasError ? "异常" : paperPaused ? "Paper已暂停" : stalePrice ? "等待实时价" : "实时运行";
  ids.runtime.classList.toggle("error", runtimeError);
  ids.runtime.classList.toggle("paused", paperPaused && !hasError);
  ids.lastTick.textContent = runtime.last_error || `market ${marketWsStatus} · price ${priceWsStatus} · okx ${okxWsStatus} · binance ${binanceMarketWsStatus}`;
  if (ids.paperPauseToggle) {
    ids.paperPauseToggle.textContent = paperPaused ? "恢复Paper" : "暂停Paper";
    ids.paperPauseToggle.classList.toggle("is-paused", paperPaused);
    ids.paperPauseToggle.setAttribute("aria-pressed", paperPaused ? "true" : "false");
    ids.paperPauseToggle.title = paperPaused ? "恢复 Paper 自动下单" : "暂停 Paper 自动下单并取消活跃挂单";
  }
}

function renderLivePanel(data = latestStatus) {
  const live = data?.runtime?.live_trading || {};
  const settings = live.settings || data?.settings?.live_trading || {};
  const readiness = live.readiness || settings.readiness || {};
  if (ids.liveEnabled) ids.liveEnabled.checked = Boolean(settings.enabled ?? live.enabled);
  setCheckboxIfIdle(ids.liveComplianceAck, settings.compliance_acknowledged, { preserveDirty: true });
  setInputIfIdle(ids.liveInitialBalance, settings.initial_balance, { preserveDirty: true });
  setInputIfIdle(ids.liveStakeDollars, settings.stake_dollars, { preserveDirty: true });
  setInputIfIdle(ids.liveMaxOpenTrades, settings.max_open_trades, { preserveDirty: true });
  setInputIfIdle(ids.liveMaxEntryPrice, settings.max_entry_price, { preserveDirty: true });
  setInputIfIdle(ids.liveMaxDailyLoss, settings.max_daily_loss, { preserveDirty: true });
  setInputIfIdle(ids.liveMaxTotalDrawdown, settings.max_total_drawdown, { preserveDirty: true });
  setInputIfIdle(ids.liveRetryCount, settings.retry_count, { preserveDirty: true });
  setInputIfIdle(ids.liveRetryDelayMs, settings.retry_delay_ms, { preserveDirty: true });
  setLiveFallbackSourcesIfIdle(settings.fallback_sources || [], { preserveDirty: true });
  const enabled = Boolean(settings.enabled ?? live.enabled);
  const ready = Boolean(readiness.ready);
  if (ids.liveStatus) {
    const lastError = live.last_error;
    ids.liveStatus.textContent = enabled
      ? ready ? "实盘开启" : "实盘开启但未就绪"
      : "实盘关闭";
    if (lastError) ids.liveStatus.textContent = `${ids.liveStatus.textContent} · ${lastError}`;
  }
  renderLiveGateStatus(live);
  if (ids.liveReadiness) {
    const errors = Array.isArray(readiness.errors) ? readiness.errors : [];
    const wallet = readiness.wallet || {};
    const walletText = wallet.checked_at
      ? `wallet ${fmtNumberCell(wallet.balance, 4)} / allowance ${fmtNumberCell(wallet.allowance, 4)} / required ${fmtNumberCell(wallet.required_cash, 4)}`
      : "";
    const openOrders = live.open_orders || settings.open_orders || {};
    const openOrdersText = openOrders.ready
      ? `official open ${fmtNumberCell(openOrders.count, 0)}`
      : openOrders.skipped
        ? "official open skipped"
        : (openOrders.errors || []).length
        ? `official open error`
        : "";
    const presence = readiness.credential_presence || {};
    const credentialText = [
      `pk:${presence.private_key ? "yes" : "no"}`,
      `sig:${presence.signature_type ? "yes" : "no"}`,
      `funder:${presence.funder_address ? "yes" : "no"}`,
      `api:${presence.api_creds_complete ? "env" : presence.api_creds_partial ? "partial" : "derive"}`,
    ].join(" ");
    const addresses = readiness.credential_addresses || {};
    const addressText = [
      addresses.signer_address_masked ? `signer:${addresses.signer_address_masked}` : "",
      addresses.funder_address_masked ? `funder:${addresses.funder_address_masked}` : "",
      typeof addresses.signer_matches_funder === "boolean"
        ? `match:${addresses.signer_matches_funder ? "yes" : "no"}`
        : "",
    ].filter(Boolean).join(" ");
    const envFiles = Array.isArray(readiness.env_files) ? readiness.env_files : [];
    const envFileText = envFiles.length
      ? `env ${envFiles.map((item) => item.path || "-").join(",")}`
      : "env none";
    const geo = readiness.geo_check || {};
    const geoText = geo.ready
      ? `geo ${geo.blocked ? "blocked" : "ok"} ${geo.country || "-"}${geo.region ? `/${geo.region}` : ""}`
      : (geo.errors || []).length
        ? "geo error"
        : "";
    const readinessMessage = errors.length
      ? errors.join("；")
      : [
          `SDK ${readiness.sdk || "py_clob_client_v2"} · ${readiness.host || "-"} · chain ${readiness.chain_id || "-"}`,
          credentialText,
          addressText,
          envFileText,
          geoText,
          walletText,
          openOrdersText,
        ].filter(Boolean).join(" · ");
    ids.liveReadiness.textContent = readinessMessage;
    ids.liveReadiness.classList.toggle("ready", ready);
    ids.liveReadiness.classList.toggle("error", !ready);
    appendLiveLog({
      key: "readiness",
      level: ready ? "pass" : errors.length ? "error" : "warn",
      title: ready ? "实盘就绪" : "实盘未就绪",
      message: readinessMessage,
      details: [
        `switch:${enabled ? "ON" : "OFF"}`,
        credentialText,
        addressText,
        envFileText,
        geoText,
        walletText,
        openOrdersText,
        live.last_error ? `runner:${live.last_error}` : "",
      ],
    });
  }
  renderLivePreflightResult();
  renderLiveDoctorResult();
  renderLiveOnceResult();
}

function renderLiveGateStatus(live = {}) {
  if (!ids.liveGateStatus) return;
  const gate = live.gate_status || {};
  const checks = Array.isArray(gate.checks) ? gate.checks : [];
  if (!checks.length) {
    ids.liveGateStatus.innerHTML = `
      <div class="live-gate-summary live-gate-block">
        <div>
          <span>实盘下单条件</span>
          <strong>等待状态</strong>
        </div>
        <small>后端尚未返回条件明细</small>
      </div>
    `;
    return;
  }
  const status = gate.overall_status || "UNKNOWN";
  const statusClass = gateStatusClass(status);
  const blocked = Array.isArray(gate.blocked_checks) ? gate.blocked_checks : checks.filter((row) => row.status === "BLOCK");
  const warnings = Array.isArray(gate.warning_checks) ? gate.warning_checks : checks.filter((row) => row.status === "WARN");
  const primaryKey = gate.primary_blocker || blocked[0]?.key || warnings[0]?.key || "-";
  const primaryMessage = gate.primary_message || blocked[0]?.message || warnings[0]?.message || "全部条件通过";
  const nextAction = gate.next_action || "-";
  const metrics = gate.metrics || {};
  const signal = gate.signal || {};
  const priceSelection = gate.price_selection || {};
  const basisRows = Array.isArray(gate.price_basis) ? gate.price_basis : [];
  const visibleChecks = gateVisibleChecks(checks);
  ids.liveGateStatus.innerHTML = `
    <div class="live-gate-summary ${statusClass}">
      <div>
        <span>实盘下单条件</span>
        <strong>${safe(status)}(${safe(gate.overall_label || gateStatusLabel(status))})</strong>
      </div>
      <div>
        <span>主因</span>
        <strong>${safe(gateCheckName(primaryKey))}</strong>
        <small>${safe(primaryMessage)}</small>
      </div>
      <div>
        <span>下一步</span>
        <strong>${safe(nextAction)}</strong>
      </div>
      <div>
        <span>风控</span>
        <strong>${fmtSignedMoneyCell(metrics.daily_realized_pnl)} / -${fmtNumberCell(metrics.daily_loss_limit, 2)}</strong>
        <small>总 ${fmtSignedMoneyCell(metrics.total_pnl)} / -${fmtNumberCell(metrics.total_drawdown_limit, 2)}</small>
      </div>
      <div>
        <span>信号</span>
        <strong>${safe(signal.side || "-")}</strong>
        <small>${signal.entry_price == null ? "-" : `entry ${fmtNumberCell(signal.entry_price, 4)} · ${fmtSignedBpsCell(signal.move_bps)}`}</small>
      </div>
      <div>
        <span>价格源</span>
        <strong>${safe(priceSelection.selected_source || "-")}</strong>
        <small>${priceSelection.selected_price == null ? safe(priceSelection.message || "-") : `${fmtNumberCell(priceSelection.selected_price, 2)} · ${safe(priceSelection.message || "")}`}</small>
      </div>
    </div>
    <div class="live-gate-checks">
      ${visibleChecks.map((check) => `
        <div class="live-gate-check ${gateCheckClass(check.status)}" title="${safe(check.message || "")}">
          <span>${safe(gateCheckName(check.key))}</span>
          <strong>${safe(check.status || "-")}${gateCheckLabel(check) ? `(${safe(gateCheckLabel(check))})` : ""}</strong>
          <small>${safe(gateCheckMeta(check))}</small>
        </div>
      `).join("")}
    </div>
    ${basisRows.length ? `
      <div class="live-basis-strip">
        ${basisRows.map((row) => `
          <div class="live-basis-item ${row.ready ? "ready" : row.selected ? "warn" : ""}">
            <span>${safe(String(row.source || "").toUpperCase())}${row.selected ? " · 已选" : ""}</span>
            <strong>${row.median_bps == null ? "-" : fmtSignedBpsCell(row.median_bps)}</strong>
            <small>样本 ${safe(row.samples ?? 0)} · 现价 ${row.price == null ? "-" : fmtNumberCell(row.price, 2)} · 校正 ${row.adjusted_price == null ? "-" : fmtNumberCell(row.adjusted_price, 2)} · 差 ${row.basis_usd == null ? "-" : fmtSignedMoneyCell(row.basis_usd)} · ${safe(row.reason || "")}</small>
          </div>
        `).join("")}
      </div>
    ` : ""}
  `;
  appendLiveLog({
    key: "live-gate",
    level: gateLogLevel(status),
    title: `实盘条件 ${status}`,
    message: primaryMessage,
    details: [
      `primary:${primaryKey}`,
      `next:${nextAction}`,
      `blocked:${blocked.map((row) => row.key).join(", ") || "-"}`,
      `warn:${warnings.map((row) => row.key).join(", ") || "-"}`,
    ],
    at_ms: gate.checked_at ? gate.checked_at * 1000 : null,
  });
}

function gateVisibleChecks(checks) {
  const priority = [
    "enabled", "signal", "daily_loss", "total_drawdown", "max_open_trades",
    "pending_entry_order", "duplicate_direction", "quote_freshness", "price_source",
    "software_cash", "collateral_wallet", "official_open_orders_clear",
    "credentials", "geo_access", "target_price", "orderbook_depth", "min_order_size",
  ];
  const byKey = new Map(checks.map((row) => [row.key, row]));
  const ordered = priority.map((key) => byKey.get(key)).filter(Boolean);
  const rest = checks.filter((row) => !priority.includes(row.key));
  return ordered.concat(rest).slice(0, 18);
}

function gateStatusClass(status) {
  const raw = String(status || "").toUpperCase();
  if (raw === "READY") return "live-gate-pass";
  if (raw === "READY_WITH_WARN" || raw === "WAIT_SIGNAL" || raw === "WARN" || raw === "DISABLED") return "live-gate-warn";
  return "live-gate-block";
}

function gateLogLevel(status) {
  const raw = String(status || "").toUpperCase();
  if (raw === "READY") return "pass";
  if (raw === "READY_WITH_WARN" || raw === "WAIT_SIGNAL" || raw === "WARN" || raw === "DISABLED") return "warn";
  return "error";
}

function gateStatusLabel(status) {
  const labels = {
    READY: "可下单",
    READY_WITH_WARN: "可下单但有警告",
    WAIT_SIGNAL: "等待信号",
    DISABLED: "实盘关闭",
    BLOCKED: "已阻断",
    WARN: "有警告",
  };
  return labels[String(status || "").toUpperCase()] || "未知";
}

function gateCheckClass(status) {
  const raw = String(status || "").toUpperCase();
  if (raw === "PASS") return "pass";
  if (raw === "WARN") return "warn";
  return "block";
}

function gateCheckLabel(check) {
  const labels = { PASS: "通过", WARN: "警告", BLOCK: "阻断" };
  return labels[String(check?.status || "").toUpperCase()] || "";
}

function gateCheckName(key) {
  const names = {
    runtime: "运行时",
    enabled: "实盘开关",
    process_lock: "进程锁",
    compliance_acknowledged: "风险确认",
    geo_access: "地区检查",
    credentials: "凭证/SDK",
    market: "当前市场",
    target_price: "目标价",
    signal: "策略信号",
    price_source: "价格源",
    daily_loss: "日亏停止",
    total_drawdown: "总回撤停止",
    max_open_trades: "最大持仓",
    duplicate_direction: "同向持仓",
    pending_entry_order: "待确认买入",
    software_cash: "隔离资金",
    quote_freshness: "盘口新鲜度",
    max_entry_price: "最高买价",
    min_order_size: "最小订单",
    orderbook_depth: "盘口深度",
    official_open_orders_clear: "官方挂单",
    collateral_wallet: "余额/授权",
  };
  return names[String(key || "")] || String(key || "-");
}

function gateCheckMeta(check) {
  const parts = [];
  if (check.current !== null && check.current !== undefined && check.current !== "") parts.push(`当前 ${check.current}`);
  if (check.threshold !== null && check.threshold !== undefined && check.threshold !== "") parts.push(`阈值 ${check.threshold}`);
  if (check.required !== null && check.required !== undefined && check.required !== "") parts.push(`要求 ${check.required}`);
  if (check.age_ms !== null && check.age_ms !== undefined) parts.push(`age ${fmtNumberCell(check.age_ms, 0)}ms`);
  return parts.join(" · ") || check.message || "-";
}

function renderLivePreflightResult() {
  if (!ids.livePreflightResult) return;
  ids.livePreflightResult.hidden = true;
  ids.livePreflightResult.innerHTML = "";
  if (!livePreflight) {
    return;
  }
  const checks = Array.isArray(livePreflight.checks) ? livePreflight.checks : [];
  const blocked = Array.isArray(livePreflight.blocked_checks)
    ? livePreflight.blocked_checks
    : checks.filter((check) => check.status !== "PASS");
  const statusText = livePreflight.can_place_next_order
    ? "可真实下单"
    : livePreflight.arming_ready
      ? "可开启实盘"
      : "不可下单";
  const signal = livePreflight.signal || {};
  const entry = livePreflight.entry || {};
  const softwareAccount = livePreflight.software_account || {};
  const stakeSource = softwareAccount.stake_locked_to_current_market ? "当前市场锁定" : "配置";
  appendLiveLog({
    key: "preflight",
    level: livePreflight.can_place_next_order ? "pass" : livePreflight.arming_ready ? "warn" : "error",
    title: `预检 ${statusText}`,
    message: blocked.length
      ? blocked.map((check) => check.message || check.key).filter(Boolean).join("；")
      : "全部检查通过",
    details: [
      `time:${fmtTime(livePreflight.checked_at)}`,
      `signal:${signal.side || "-"} budget:${fmtNumberCell(entry.stake ?? softwareAccount.stake, 4)} source:${stakeSource}`,
      checks.map((check) => `${check.key}:${check.status}`).join(" · "),
    ],
  });
}

function renderLiveDoctorResult() {
  if (!ids.liveDoctorResult) return;
  ids.liveDoctorResult.hidden = true;
  ids.liveDoctorResult.innerHTML = "";
  if (!liveDoctor) {
    updateLiveOnceButtonState();
    return;
  }
  const status = liveDoctor.status || "UNKNOWN";
  const fatal = Array.isArray(liveDoctor.fatal_one_shot_blockers) ? liveDoctor.fatal_one_shot_blockers : [];
  const waitable = Array.isArray(liveDoctor.waitable_one_shot_blockers) ? liveDoctor.waitable_one_shot_blockers : [];
  const actions = Array.isArray(liveDoctor.next_actions) ? liveDoctor.next_actions : [];
  const firstOrder = liveDoctor.first_order || {};
  const stakeRequirement = firstOrder.stake_requirement || {};
  const credentials = liveDoctor.credential_setup || {};
  const sdkStatus = liveDoctor.sdk_status || {};
  const missingCredentials = Array.isArray(credentials.missing_required_keys) ? credentials.missing_required_keys : [];
  const emptyKeys = Array.isArray(credentials.empty_keys) ? credentials.empty_keys : [];
  const command = firstOrder.recommended_cli || "";
  const statusClass = status === "BLOCKED" ? "block" : status === "READY_FOR_LIVE_LOOP" ? "pass" : "warn";
  const sdkClass = sdkStatus.compatible === false ? "block" : sdkStatus.compatible === true ? "pass" : "warn";
  const sdkLabel = sdkStatus.package || liveDoctor.sdk || "SDK";
  const sdkVersion = liveDoctor.sdk_version || sdkStatus.version || "-";
  appendLiveLog({
    key: "doctor",
    level: status === "BLOCKED" ? "error" : status === "READY_FOR_LIVE_LOOP" ? "pass" : "warn",
    title: `首单检查 ${status}`,
    message: fatal.length || waitable.length
      ? `${fatal.length ? `fatal ${fatal.join(", ")}` : ""}${fatal.length && waitable.length ? " · " : ""}${waitable.length ? `wait ${waitable.join(", ")}` : ""}`
      : "one-shot 阻断为空",
    details: [
      `one-shot:${liveDoctor.ready_for_one_shot_now ? "now" : liveDoctor.can_wait_for_one_shot ? "wait" : "blocked"}`,
      `SDK:${sdkLabel} ${sdkVersion} ${sdkClass}`,
      missingCredentials.length ? `缺凭证:${missingCredentials.join(", ")}` : "",
      emptyKeys.length ? `空凭证:${emptyKeys.slice(0, 4).join(", ")}${emptyKeys.length > 4 ? "..." : ""}` : "",
      stakeRequirement.min_order_size !== undefined && stakeRequirement.min_order_size !== null
        ? `stake:${fmtNumberCell(stakeRequirement.stake_dollars, 2)} min:${fmtNumberCell(stakeRequirement.min_order_size, 2)} shortfall:${fmtNumberCell(stakeRequirement.shortfall, 2)}`
        : "",
      credentials.next_step || "",
      ...actions.slice(0, 4).map((row) => `${row.key || "-"}: ${row.action || ""}`),
    ],
    code: command,
  });
  updateLiveOnceButtonState();
}

function updateLiveOnceButtonState() {
  if (!ids.liveOnce) return;
  const fatal = Array.isArray(liveDoctor?.fatal_one_shot_blockers) ? liveDoctor.fatal_one_shot_blockers : [];
  const canRun = Boolean(liveDoctor && (liveDoctor.ready_for_one_shot_now || liveDoctor.can_wait_for_one_shot));
  const running = liveOnce?.local_status === "RUNNING";
  ids.liveOnce.disabled = running || !canRun || fatal.length > 0;
}

function renderLiveOnceResult() {
  if (!ids.liveOnceResult) return;
  ids.liveOnceResult.hidden = true;
  ids.liveOnceResult.innerHTML = "";
  if (!liveOnce) {
    updateLiveOnceButtonState();
    return;
  }
  const status = liveOnce.local_status || (liveOnce.submitted ? "SUBMITTED" : liveOnce.blocked ? "BLOCKED" : "READY");
  const statusClass = liveOnce.submitted ? "pass" : liveOnce.blocked || liveOnce.error ? "block" : "warn";
  const fatal = Array.isArray(liveOnce.fatal_blocked_keys) ? liveOnce.fatal_blocked_keys : [];
  const waitable = Array.isArray(liveOnce.waitable_blocked_keys) ? liveOnce.waitable_blocked_keys : [];
  const blocked = Array.isArray(liveOnce.blocked_keys) ? liveOnce.blocked_keys : [];
  const evidence = liveOnce.evidence || {};
  const evidenceOrder = evidence.order || {};
  const lastOrder = liveOnce.last_order || {};
  const reconcile = liveOnce.reconcile || {};
  const audit = liveOnce.audit || {};
  const orderId = lastOrder.order_id || lastOrder.external_order_id || reconcile.external_order_id || evidenceOrder.external_order_id || null;
  const localStatus = evidenceOrder.status || lastOrder.status || reconcile.local_status || null;
  const errors = [
    liveOnce.error,
    ...(Array.isArray(liveOnce.errors) ? liveOnce.errors : []),
  ].filter(Boolean);
  appendLiveLog({
    key: "live-once",
    level: liveOnce.submitted ? "pass" : liveOnce.blocked || liveOnce.error ? "error" : "warn",
    title: `one-shot 首单 ${status}`,
    message: errors.length ? errors.join("；") : liveOnce.message || (orderId ? `官方订单 id ${orderId}` : ""),
    details: [
      liveOnce.checked_at ? `time:${fmtTime(liveOnce.checked_at)}` : "",
      orderId ? `official_order:${orderId}` : "",
      localStatus ? `local_status:${localStatus}` : "",
      blocked.length ? `blocked:${blocked.join(", ")}` : "",
      fatal.length ? `fatal:${fatal.join(", ")}` : "",
      waitable.length ? `wait:${waitable.join(", ")}` : "",
      audit.path ? `audit:${audit.path}` : "",
    ],
  });
  updateLiveOnceButtonState();
}

function setInputIfIdle(input, value, { preserveDirty = false } = {}) {
  if (!input || document.activeElement === input) return;
  if (value === undefined || value === null) return;
  const nextValue = String(value);
  input.dataset.serverValue = nextValue;
  const key = liveSettingsFieldKey(input);
  if (preserveDirty && key && liveSettingsDirtyFields.has(key)) return;
  input.value = nextValue;
  if (key) liveSettingsDirtyFields.delete(key);
  syncLiveSaveDirtyState();
}

function setCheckboxIfIdle(input, checked, { preserveDirty = false } = {}) {
  if (!input || document.activeElement === input) return;
  const nextChecked = Boolean(checked);
  input.dataset.serverValue = String(nextChecked);
  const key = liveSettingsFieldKey(input);
  if (preserveDirty && key && liveSettingsDirtyFields.has(key)) return;
  input.checked = nextChecked;
  if (key) liveSettingsDirtyFields.delete(key);
  syncLiveSaveDirtyState();
}

function setLiveFallbackSourcesIfIdle(sources = [], { preserveDirty = false } = {}) {
  const selected = new Set(Array.isArray(sources) ? sources.map((item) => String(item).toLowerCase()) : []);
  for (const input of ids.liveFallbackSources || []) {
    setCheckboxIfIdle(input, selected.has(String(input.dataset.liveFallbackSource || "").toLowerCase()), { preserveDirty });
  }
}

function renderStrategyExperiments(runtime = {}) {
  if (!ids.strategyExperiments) return;
  const experiments = runtime.strategy_experiments || {};
  const variants = Array.isArray(experiments.variants) ? experiments.variants : [];
  const decision = experiments.decision_summary || {};
  const profit = experiments.profit_summary || {};
  const ranked = rankExperimentVariants(variants);
  const settledTotal = variants.reduce((sum, row) => sum + (toNumber(row.recent_trades_summary?.settled_count) || 0), 0);
  const activeTotal = variants.reduce((sum, row) => sum + (toNumber(row.active_orders) || 0), 0);
  const openTotal = variants.reduce((sum, row) => sum + (toNumber(row.metrics?.open_trades) || 0), 0);

  ids.strategyExperimentMeta.textContent = `${variants.length} 组 · tick ${experiments.run_count || 0}`;
  const renderKey = experimentRenderKey(experiments, variants, decision, profit);
  if (renderKey === lastExperimentRenderKey) return;
  lastExperimentRenderKey = renderKey;
  ids.strategyExperimentSummary.innerHTML = [
    experimentSummaryItem("推荐", decision.recommended_combo || decision.current_leader_combo || "等待样本"),
    experimentSummaryItem("盈利", profit.winner_combo || profit.current_profit_leader_combo || "等待样本"),
    experimentSummaryItem("状态", decision.status_label || "继续观察"),
    experimentSummaryItem("官方", fmtNumberCell(experiments.official_broadcast_count, 0)),
    experimentSummaryItem("可比", `${fmtNumberCell(decision.ready_count, 0)} / ${fmtNumberCell(decision.total_count || variants.length, 0)}`),
    experimentSummaryItem("淘汰", fmtNumberCell(decision.disqualified_count, 0)),
    experimentSummaryItem("已结算", fmtNumberCell(settledTotal, 0)),
    experimentSummaryItem("持仓", fmtNumberCell(openTotal, 0)),
    experimentSummaryItem("挂单", fmtNumberCell(activeTotal, 0)),
  ].join("");

  if (!experiments.enabled) {
    ids.strategyExperiments.innerHTML = `<tr><td colspan="15" class="empty">实验未启用</td></tr>`;
    return;
  }
  if (!ranked.length) {
    ids.strategyExperiments.innerHTML = `<tr><td colspan="15" class="empty">暂无实验数据</td></tr>`;
    return;
  }

  ids.strategyExperiments.innerHTML = ranked.map((row, index) => renderExperimentRow(row, index + 1)).join("");
}

function experimentSummaryItem(label, value) {
  return `<span><b>${safe(label)}</b>${safe(value)}</span>`;
}

function experimentRenderKey(experiments, variants, decision = {}, profit = {}) {
  return JSON.stringify({
    enabled: Boolean(experiments.enabled),
    official_broadcast_count: experiments.official_broadcast_count || 0,
    decision: {
      status: decision.status,
      leader: decision.current_leader_variant_id,
      recommended: decision.recommended_variant_id,
      ready: decision.ready_count,
      pending: decision.pending_count,
      disqualified: decision.disqualified_count,
      total: decision.total_count,
    },
    profit: {
      status: profit.status,
      leader: profit.current_profit_leader_variant_id,
      winner: profit.winner_variant_id,
      pnl: profit.current_profit_leader_pnl,
      ready: profit.ready_count,
      pending: profit.pending_count,
      disqualified: profit.disqualified_count,
    },
    expanded: expandedExperimentId,
    loading: loadingExperimentId,
    detail_keys: [...experimentDetailCache.keys()],
    variants: variants.map((row) => ({
      id: row.variant_id,
      pnl: row.recent_trades_summary?.total_pnl,
      roi: row.recent_trades_summary?.roi_pct,
      wins: row.recent_trades_summary?.win_count,
      settled: row.recent_trades_summary?.settled_count,
      fill_rate: row.order_summary?.fill_rate,
      orders: row.order_summary?.total_count,
      rejected: row.order_summary?.rejected_count,
      expired: row.order_summary?.expired_count,
      canceled: row.order_summary?.canceled_count,
      score: row.review_score?.score,
      decision: row.review_score?.decision,
      sample: row.review_score?.sample_status,
      open: row.metrics?.open_trades,
      active: row.active_orders,
      signal: row.last_signal?.side,
      error: row.last_error,
      official_error: row.official_broadcast_error,
    })),
  });
}

function rankExperimentVariants(variants) {
  const anySettled = variants.some((row) => (toNumber(row.recent_trades_summary?.settled_count) || 0) > 0);
  if (!anySettled) return [...variants];
  return [...variants].sort((a, b) => {
    const scoreA = toNumber(a.review_score?.score) || 0;
    const scoreB = toNumber(b.review_score?.score) || 0;
    const settledA = toNumber(a.recent_trades_summary?.settled_count) || 0;
    const settledB = toNumber(b.recent_trades_summary?.settled_count) || 0;
    const pnlA = toNumber(a.recent_trades_summary?.total_pnl) || 0;
    const pnlB = toNumber(b.recent_trades_summary?.total_pnl) || 0;
    const roiA = toNumber(a.recent_trades_summary?.roi_pct) || 0;
    const roiB = toNumber(b.recent_trades_summary?.roi_pct) || 0;
    if (Math.abs(scoreA - scoreB) > 0.000001) return scoreB - scoreA;
    if (settledA !== settledB && (settledA === 0 || settledB === 0)) return settledB - settledA;
    if (Math.abs(pnlA - pnlB) > 0.000001) return pnlB - pnlA;
    if (Math.abs(roiA - roiB) > 0.000001) return roiB - roiA;
    return String(a.variant_id || "").localeCompare(String(b.variant_id || ""));
  });
}

function renderExperimentRow(row, rank) {
  const summary = row.recent_trades_summary || {};
  const metrics = row.metrics || {};
  const orders = row.order_summary || {};
  const review = row.review_score || {};
  const settled = toNumber(summary.settled_count) || 0;
  const total = toNumber(summary.total_count) || 0;
  const open = toNumber(metrics.open_trades) || 0;
  const active = toNumber(row.active_orders) || 0;
  const orderQuality = `${fmtNumberCell(orders.filled_count, 0)} 成 · ${fmtNumberCell(orders.canceled_count, 0)} 取 · ${fmtNumberCell(orders.expired_count, 0)} 过 · ${fmtNumberCell(orders.rejected_count, 0)} 拒`;
  const rowError = row.last_error || row.official_broadcast_error;
  const signal = rowError || row.last_signal?.reason || row.last_signal?.side || "-";
  const signalSide = rowError ? "异常" : row.last_signal?.side || "-";
  const detailOpen = expandedExperimentId === row.variant_id;
  return `
    <tr>
      <td class="mono-cell">${rank}</td>
      <td>
        <strong class="${scoreClass(review.score)}">${fmtNumberCell(review.score, 2)}</strong>
        <span class="subtle">${safe(review.decision)}</span>
      </td>
      <td>${safe(review.sample_label || "-")}</td>
      <td>
        <strong>${safe(row.combo || row.variant_id)}</strong>
        <span class="subtle mono-cell">${safe(row.variant_id)}</span>
      </td>
      <td>${safe(row.role)}</td>
      <td>${fmtSignedMoneyCell(summary.total_pnl)}</td>
      <td>${fmtSignedPctCell(summary.roi_pct)}</td>
      <td>${fmtPctCell(summary.win_rate)}</td>
      <td>${fmtPctCell(orders.fill_rate)}</td>
      <td>${safe(orderQuality)}</td>
      <td>${fmtNumberCell(settled, 0)} / ${fmtNumberCell(total, 0)}</td>
      <td>${fmtNumberCell(open, 0)} 持仓 · ${fmtNumberCell(active, 0)} 挂单</td>
      <td>${safe(row.target_report_alignment)}</td>
      <td class="reason-cell"><span class="${rowError ? "negative" : ""}">${safe(signalSide)}</span> · ${safe(signal)}</td>
      <td><button class="table-action" type="button" data-experiment-id="${safe(row.variant_id)}">${detailOpen ? "收起" : "详情"}</button></td>
    </tr>
    ${detailOpen ? renderExperimentDetailRow(row.variant_id, 15) : ""}
  `;
}

function renderExperimentDetailRow(variantId, colspan) {
  const detail = experimentDetailCache.get(variantId);
  const body = loadingExperimentId === variantId
    ? `<div class="experiment-detail-empty">加载组合详情...</div>`
    : experimentDetailHtml(detail);
  return `
    <tr class="experiment-detail-row">
      <td colspan="${Math.max(1, colspan)}">${body}</td>
    </tr>
  `;
}

function experimentDetailHtml(detail) {
  if (!detail || !detail.variant) return `<div class="experiment-detail-empty">暂无组合详情</div>`;
  const variant = detail.variant || {};
  const tradePage = detail.recent_trades_page || {};
  const orderPage = detail.recent_orders_page || {};
  const trades = Array.isArray(tradePage.recent_trades) ? tradePage.recent_trades.slice(0, 6) : [];
  const orders = Array.isArray(orderPage.recent_orders) ? orderPage.recent_orders.slice(0, 6) : [];
  const summary = variant.recent_trades_summary || {};
  const orderSummary = variant.order_summary || {};
  const review = variant.review_score || {};
  return `
    <div class="experiment-detail">
      <div class="experiment-detail-metrics">
        ${experimentMetric("评分", `<span class="${scoreClass(review.score)}">${fmtNumberCell(review.score, 2)}</span>`)}
        ${experimentMetric("决策", safe(review.decision))}
        ${experimentMetric("净盈亏", fmtSignedMoneyCell(summary.total_pnl))}
        ${experimentMetric("ROI", fmtSignedPctCell(summary.roi_pct))}
        ${experimentMetric("官方/兜底", `${fmtNumberCell(summary.official_count, 0)} / ${fmtNumberCell(summary.chainlink_count, 0)}`)}
        ${experimentMetric("成交率", fmtPctCell(orderSummary.fill_rate))}
        ${experimentMetric("订单", `${fmtNumberCell(orderSummary.total_count, 0)} 总 · ${fmtNumberCell(orderSummary.active_count, 0)} 活跃`)}
      </div>
      <div class="experiment-score-reasons">${experimentScoreReasons(review)}</div>
      <div class="experiment-detail-grid">
        <div class="experiment-detail-section">
          <h3>最近交易</h3>
          ${experimentTradesTable(trades)}
        </div>
        <div class="experiment-detail-section">
          <h3>订单流水</h3>
          ${experimentOrdersTable(orders)}
        </div>
      </div>
    </div>
  `;
}

function experimentMetric(label, valueHtml) {
  return `<span><b>${safe(label)}</b>${valueHtml}</span>`;
}

function experimentScoreReasons(review) {
  const reasons = Array.isArray(review?.reasons) ? review.reasons : [];
  if (!reasons.length) return "";
  return reasons.map((item) => `<span>${safe(item)}</span>`).join("");
}

function scoreClass(value) {
  const parsed = toNumber(value);
  if (parsed == null) return "";
  if (parsed >= 70) return "positive";
  if (parsed < 40) return "negative";
  return "";
}

function experimentTradesTable(rows) {
  if (!rows.length) return `<div class="experiment-detail-empty">暂无最近交易</div>`;
  return `
    <table class="experiment-mini-table">
      <thead><tr><th>时间</th><th>方向</th><th>状态</th><th>盈亏</th><th>来源</th></tr></thead>
      <tbody>
        ${rows.map((row) => `
          <tr>
            <td>${fmtDateTimeCell(row.settled_at || row.opened_at)}</td>
            <td><span class="${sideClass(row.side)}">${safe(row.side)}</span></td>
            <td>${safe(row.status)}</td>
            <td>${fmtSignedMoneyCell(row.pnl)}</td>
            <td>${safe(row.settlement_source_label || row.settlement_source || "-")}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function experimentOrdersTable(rows) {
  if (!rows.length) return `<div class="experiment-detail-empty">暂无订单流水</div>`;
  return `
    <table class="experiment-mini-table">
      <thead><tr><th>时间</th><th>方向</th><th>类型</th><th>状态</th><th>成交</th></tr></thead>
      <tbody>
        ${rows.map((row) => `
          <tr>
            <td>${fmtDateTimeCell(row.created_at)}</td>
            <td><span class="${sideClass(row.side)}">${safe(row.side)}</span></td>
            <td>${safe(row.order_type)}</td>
            <td>${orderStatusText(row.status)}</td>
            <td>${fmtNumberCell(row.filled_shares, 4)} / ${fmtMoneyCell(row.cash_spent)}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function marketTargetPrice(market) {
  const target = toNumber(market?.target_price);
  return target != null && target > 0 ? target : null;
}

function displayTargetState(market) {
  const officialTarget = marketTargetPrice(market);
  if (officialTarget != null) {
    return { value: officialTarget, fallback: false, source: "market" };
  }
  const fallbackTarget = toNumber(priceState.target_price);
  if (fallbackTarget != null && fallbackTarget > 0 && priceState.target_price_fallback) {
    return { value: fallbackTarget, fallback: true, source: priceState.target_price_source || "fallback" };
  }
  return { value: null, fallback: false, source: null };
}

function renderMarket(runtime) {
  const market = activeMarket || runtime.current_market;
  const signal = runtime.last_signal || {};
  const pair = runtime.pair_strategy || {};
  const lastPairEvent = pair.last_event || {};
  const up = quotes.Up || runtime.latest_quotes?.Up || {};
  const down = quotes.Down || runtime.latest_quotes?.Down || {};
  const current = priceState.chainlink || runtime.latest_price?.chainlink || runtime.latest_price?.binance || null;
  const targetState = displayTargetState(market);
  const target = targetState.value;
  const distance = current && target ? ((current - target) / target) * 10_000 : null;
  const marketQuestion = market?.question || "-";
  const marketSlug = market?.slug || "-";
  const remaining = market ? fmtLeft(market.end_ts) : "-";
  const currentText = current == null ? "-" : money.format(current);
  const targetText = target == null ? "-" : `${money.format(target)}${targetState.fallback ? " 兜底" : ""}`;
  const targetTitle = targetState.fallback ? `fallback: ${targetState.source}` : "market.target_price";
  const distanceText = distance == null ? "-" : `${number.format(distance)} bps`;
  const pairCostText = pair.pair_cost == null ? "-" : number.format(pair.pair_cost);
  const pairBidText = pair.bid_sum == null ? "-" : number.format(pair.bid_sum);
  const signalSide = signal.side || "-";
  const signalReason = signal.reason || "-";
  const pairEvent = lastPairEvent.message || "-";
  const websocketText = `${marketWsStatus} / ${priceWsStatus}`;
  const chainlinkTime = fmtMs(priceState.chainlink_updated_ms);
  ids.markets.innerHTML = `
    <article class="market-row">
      <div class="market-top">
        <div class="market-title">
          <div class="market-symbol">BTC 5m</div>
          <div class="muted truncate" title="${safe(marketSlug)}">${safe(marketSlug)}</div>
        </div>
        <div class="market-price">${currentText}</div>
      </div>
      <div class="market-kpis">
        <div class="kpi"><span>剩余</span><strong>${remaining}</strong></div>
        <div class="kpi"><span>目标</span><strong title="${safe(targetTitle)}">${targetText}</strong></div>
        <div class="kpi"><span>差距</span><strong class="${cls(distance || 0)}">${distanceText}</strong></div>
        <div class="kpi"><span>信号</span><strong class="${sideClass(signalSide)}">${safe(signalSide)}</strong></div>
      </div>
      <div class="quote-strip">
        <div class="quote-row"><span>Up 买/卖</span><strong>${quoteText(up)}</strong></div>
        <div class="quote-row"><span>Down 买/卖</span><strong>${quoteText(down)}</strong></div>
        <div class="quote-row"><span>配对成本</span><strong>${pairCostText}</strong></div>
        <div class="quote-row"><span>退出 bid</span><strong>${pairBidText}</strong></div>
      </div>
      <div class="market-foot">
        <div class="foot-line"><span>市场</span><strong title="${safe(marketQuestion)}">${safe(marketQuestion)}</strong></div>
        <div class="foot-line"><span>原因</span><strong title="${safe(signalReason)}">${safe(signalReason)}</strong></div>
        <div class="foot-line"><span>配对</span><strong title="${safe(pairEvent)}">${safe(pairEvent)}</strong></div>
        <div class="foot-line"><span>状态</span><strong title="${safe(websocketText)}">${safe(websocketText)}</strong></div>
        <div class="foot-line"><span>更新</span><strong title="${safe(chainlinkTime)}">${safe(chainlinkTime)}</strong></div>
      </div>
    </article>
  `;
}

function quoteText(row) {
  const bid = row?.best_bid == null ? "-" : number.format(Number(row.best_bid));
  const ask = row?.best_ask == null ? "-" : number.format(Number(row.best_ask));
  return `${bid} / ${ask}`;
}

function renderOpenTrades(rows, scope = openDataScope, options = {}) {
  if ((isExperimentAggregateScope(scope) || isExperimentVariantScope(scope)) && strategyTablesLoading && !strategyTables) {
    ids.openCount.textContent = "加载中";
    if (!options.force && isOpenInteractionActive()) {
      pendingOpenRender = true;
      return;
    }
    renderTradeTable("open", [], experimentOpenFields, ids.openTradesHead, ids.openTrades, experimentFieldKeys("open"));
    lastOpenRenderedScope = scope;
    lastOpenRenderedCount = 0;
    return;
  }
  ids.openCount.textContent = isExperimentAggregateScope(scope) ? `${rows.length} / ${strategyExperimentVariants().length}组` : rows.length;
  const renderKey = openRenderKey(rows, scope);
  if (!options.force && renderKey === lastOpenRenderKey) return;
  const shouldClearStaleOpenRows = rows.length === 0 && (scope !== lastOpenRenderedScope || lastOpenRenderedCount > 0);
  if (!options.force && !shouldClearStaleOpenRows && isOpenInteractionActive()) {
    pendingOpenRender = true;
    return;
  }
  pendingOpenRender = false;
  lastOpenRenderKey = renderKey;
  lastOpenRenderedScope = scope;
  lastOpenRenderedCount = rows.length;
  const tableWrap = openTableWrap();
  const scrollTop = tableWrap ? tableWrap.scrollTop : 0;
  const scrollLeft = tableWrap ? tableWrap.scrollLeft : 0;
  const fields = isMainScope(scope) ? openTradeFields : scopedOpenFields;
  const selected = isMainScope(scope) ? selectedFields.open : experimentFieldKeys("open");
  renderTradeTable("open", rows, fields, ids.openTradesHead, ids.openTrades, selected);
  if (tableWrap) {
    tableWrap.scrollTop = Math.min(scrollTop, Math.max(0, tableWrap.scrollHeight - tableWrap.clientHeight));
    tableWrap.scrollLeft = Math.min(scrollLeft, Math.max(0, tableWrap.scrollWidth - tableWrap.clientWidth));
  }
}

function renderRecentOrders(rows, options = {}) {
  const scope = options.scope || orderDataScope;
  const meta = options.meta || (isExperimentAggregateScope(scope) ? strategyTables?.recent_orders_meta || {} : orderMeta);
  const total = Number(meta.total || rows.length || 0);
  const loaded = rows.length;
  ids.orderCount.textContent = total > loaded ? `${loaded} / ${total}` : `${loaded}`;
  ids.orderPageInfo.textContent = `${scopeLabel(scope)} · ${total > loaded ? `最近 ${loaded} / ${total} 条` : `最近 ${loaded} 条`}`;
  ids.loadMoreOrders.hidden = !meta.has_more;
  ids.loadMoreOrders.disabled = false;
  updateOrderActionButtons(rows);
  const renderKey = orderRenderKey(rows, scope, meta);
  if (!options.force && renderKey === lastOrderRenderKey) return;
  if (!options.force && isOrderInteractionActive()) {
    pendingOrderRender = true;
    return;
  }
  pendingOrderRender = false;
  lastOrderRenderKey = renderKey;
  const tableWrap = orderTableWrap();
  const scrollTop = tableWrap ? tableWrap.scrollTop : 0;
  const scrollLeft = tableWrap ? tableWrap.scrollLeft : 0;
  const fields = isMainScope(scope) ? recentOrderFields : scopedOrderFields;
  const selected = isMainScope(scope) ? selectedFields.order : experimentFieldKeys("order");
  renderTradeTable("order", rows, fields, ids.recentOrdersHead, ids.recentOrders, selected);
  if (tableWrap) {
    tableWrap.scrollTop = Math.min(scrollTop, Math.max(0, tableWrap.scrollHeight - tableWrap.clientHeight));
    tableWrap.scrollLeft = Math.min(scrollLeft, Math.max(0, tableWrap.scrollWidth - tableWrap.clientWidth));
  }
}

function renderRecentTrades(rows, options = {}) {
  const scope = options.scope || recentDataScope;
  const meta = options.meta || (isExperimentAggregateScope(scope) ? strategyTables?.recent_trades_meta || {} : recentMeta);
  const summary = options.summary || (isExperimentAggregateScope(scope) ? strategyTables?.recent_trades_summary || {} : recentSummary);
  if (recentLoading) {
    renderRecentSkeleton();
    return;
  }
  const total = Number(meta.total || rows.length || 0);
  const loaded = rows.length;
  const filtered = recentFilterActive();
  ids.tradeCount.textContent = total > loaded ? `${loaded} / ${total}` : `${loaded}`;
  ids.recentPageInfo.textContent = total > loaded
    ? `${scopeLabel(scope)} · ${filtered ? "范围" : "最近"} ${loaded} / ${total} 条`
    : `${scopeLabel(scope)} · ${filtered ? "范围" : "最近"} ${loaded} 条`;
  ids.loadMoreRecent.hidden = !meta.has_more;
  ids.loadMoreRecent.disabled = false;
  renderRecentSummary(summary, scope);
  const renderKey = recentRenderKey(rows, scope, meta, summary);
  if (renderKey === lastRecentRenderKey) return;
  lastRecentRenderKey = renderKey;
  const fields = isMainScope(scope) ? recentTradeFields : scopedRecentFields;
  const selected = isMainScope(scope) ? selectedFields.recent : experimentFieldKeys("recent");
  renderTradeTable("recent", rows, fields, ids.recentTradesHead, ids.recentTrades, selected);
  applyRecentContentTransition();
}

function renderRecentSkeleton() {
  ids.tradeCount.textContent = "加载中";
  ids.recentPageInfo.textContent = "正在加载交易记录";
  ids.loadMoreRecent.hidden = true;
  renderRecentSummarySkeleton();
  ids.recentTradesHead.innerHTML = `
    <tr>
      ${Array.from({ length: 8 }, (_, index) => `<th><span class="skeleton skeleton-head skeleton-w-${(index % 4) + 1}"></span></th>`).join("")}
    </tr>
  `;
  ids.recentTrades.innerHTML = Array.from({ length: RECENT_SKELETON_ROWS }, (_, rowIndex) => `
    <tr class="recent-skeleton-row">
      ${Array.from({ length: 8 }, (_, colIndex) => `
        <td><span class="skeleton skeleton-cell skeleton-w-${((rowIndex + colIndex) % 4) + 1}"></span></td>
      `).join("")}
    </tr>
  `).join("");
  lastRecentRenderKey = "recent-loading";
}

function renderRecentSummarySkeleton() {
  if (!ids.recentSummary) return;
  ids.recentSummary.innerHTML = Array.from({ length: 9 }, (_, index) => `
    <div class="recent-summary-item skeleton-summary-item">
      <span class="skeleton skeleton-label skeleton-w-${(index % 3) + 1}"></span>
      <strong><span class="skeleton skeleton-value skeleton-w-${(index % 4) + 1}"></span></strong>
    </div>
  `).join("");
}

function setRecentLoading(loading) {
  recentLoading = Boolean(loading);
  if (recentLoading) {
    if (recentTransitionTimer) {
      window.clearTimeout(recentTransitionTimer);
      recentTransitionTimer = null;
    }
    ids.recentSummary?.classList.remove("recent-content-enter");
    ids.recentTradesHead?.classList.remove("recent-content-enter");
    ids.recentTrades?.classList.remove("recent-content-enter");
    renderRecentSkeleton();
  }
}

function applyRecentContentTransition() {
  ids.recentSummary?.classList.remove("recent-content-enter");
  ids.recentTradesHead?.classList.remove("recent-content-enter");
  ids.recentTrades?.classList.remove("recent-content-enter");
  void ids.recentTrades.offsetWidth;
  ids.recentSummary?.classList.add("recent-content-enter");
  ids.recentTradesHead?.classList.add("recent-content-enter");
  ids.recentTrades?.classList.add("recent-content-enter");
  if (recentTransitionTimer) window.clearTimeout(recentTransitionTimer);
  recentTransitionTimer = window.setTimeout(() => {
    ids.recentSummary?.classList.remove("recent-content-enter");
    ids.recentTradesHead?.classList.remove("recent-content-enter");
    ids.recentTrades?.classList.remove("recent-content-enter");
    recentTransitionTimer = null;
  }, 260);
}

function renderRecentSummary(summary, scope = recentDataScope) {
  if (!ids.recentSummary) return;
  const data = summary || {};
  const sourceText = `官 ${Number(data.official_count || 0)} / 兜 ${Number(data.chainlink_count || 0)} / 平 ${Number(data.early_exit_count || 0)}`;
  const items = [
    ["范围", scopeLabel(scope)],
    ["交易", `${Number(data.total_count || 0)} 笔`],
    ["已结算", `${Number(data.settled_count || 0)} 笔`],
    ["总盈亏", fmtSignedMoneyCell(data.total_pnl)],
    ["ROI", fmtSignedPctCell(data.roi_pct)],
    ["胜率", `${fmtPctCell(data.win_rate)} (${Number(data.win_count || 0)}/${Number(data.loss_count || 0)})`],
    ["累计投入", fmtMoneyCell(data.settled_stake)],
    ["回款", fmtMoneyCell(data.total_payout)],
    ["来源", sourceText],
  ];
  ids.recentSummary.innerHTML = items.map(([label, value]) => `
    <div class="recent-summary-item">
      <span>${safe(label)}</span>
      <strong>${value}</strong>
    </div>
  `).join("");
}

function recentRenderKey(rows, scope = recentDataScope, meta = recentMeta, summary = recentSummary) {
  const idsKey = rows.map((row) => [
    row.variant_id || row.account_scope || "main",
    row.id,
    row.status || "",
    row.status_display || "",
    row.status_label || "",
    row.settlement_pending ? "pending" : "",
    row.side || "",
    row.entry_price ?? "",
    row.shares ?? "",
    row.stake ?? "",
    row.payout ?? "",
    row.pnl ?? "",
    row.return_pct ?? "",
    row.outcome || "",
    row.official_outcome || "",
    row.settlement_source || "",
    row.settled_at || "",
    row.closed_at || "",
    row.reason || "",
  ].join(":")).join(",");
  return [
    scope,
    idsKey,
    (isMainScope(scope) ? selectedFields.recent : experimentFieldKeys("recent")).join(","),
    meta.loaded,
    meta.total,
    meta.has_more,
    meta.start_at || "",
    meta.end_at || "",
    summary?.total_pnl ?? "",
  ].join("|");
}

function openRenderKey(rows, scope = openDataScope) {
  const selected = isMainScope(scope) ? selectedFields.open : experimentFieldKeys("open");
  const leftBucket = selected.includes("left") ? Math.floor(Date.now() / 1000) : "";
  const rowsKey = rows.map((row) => [
    row.variant_id || row.account_scope || "main",
    row.id,
    row.status || "",
    row.position_state || "",
    row.position_state_label || "",
    row.settlement_pending ? "pending" : "",
    row.side || "",
    row.shares ?? "",
    row.current_bid ?? "",
    row.current_ask ?? "",
    row.exit_value ?? "",
    row.unrealized_pnl ?? "",
    row.unrealized_roi_pct ?? "",
    row.pending_live_sell_order_id || "",
    row.reason || "",
  ].join(":")).join(",");
  return [scope, rowsKey, selected.join(","), rows.length, leftBucket].join("|");
}

function orderRenderKey(rows, scope = orderDataScope, meta = orderMeta) {
  const expandedFills = expandedOrderId ? orderFillCache.get(expandedOrderId) || [] : [];
  const rowsKey = rows.map((row) => [
    row.variant_id || "main",
    row.id,
    row.status,
    row.updated_at || "",
    row.remaining_cash ?? "",
    row.filled_shares ?? "",
    row.avg_fill_price ?? "",
    row.cash_spent ?? "",
    row.fee ?? "",
    row.fill_count ?? "",
    row.reason || "",
  ].join(":")).join(",");
  return [
    scope,
    rowsKey,
    (isMainScope(scope) ? selectedFields.order : experimentFieldKeys("order")).join(","),
    meta.loaded,
    meta.total,
    meta.has_more,
    meta.status_filter,
    expandedOrderId || "",
    loadingOrderId || "",
    expandedFills.length,
  ].join("|");
}

function tableRoot(kind) {
  if (kind === "open") return ids.openTrades.closest(".panel") || ids.openTrades;
  if (kind === "order") return ids.recentOrders.closest(".panel") || ids.recentOrders;
  return null;
}

function tableWrap(kind) {
  if (kind === "open") return ids.openTrades.closest(".table-wrap");
  if (kind === "order") return ids.recentOrders.closest(".order-table-wrap") || ids.recentOrders.closest(".table-wrap");
  return null;
}

function orderRoot() {
  return tableRoot("order");
}

function orderTableWrap() {
  return tableWrap("order");
}

function openTableWrap() {
  return tableWrap("open");
}

function nodeInside(root, node) {
  if (!root || !node) return false;
  const element = node.nodeType === 1 ? node : node.parentElement;
  return Boolean(element && root.contains(element));
}

function tableSelectionActive(kind) {
  const selection = window.getSelection ? window.getSelection() : null;
  if (!selection || selection.isCollapsed || !selection.toString().trim()) return false;
  const root = tableRoot(kind);
  return nodeInside(root, selection.anchorNode) || nodeInside(root, selection.focusNode);
}

function isTableInteractionActive(kind) {
  const root = tableRoot(kind);
  const active = document.activeElement;
  if (Date.now() < (tableInteractionHoldUntil[kind] || 0)) return true;
  if (!root) return false;
  if (active && active !== document.body && root.contains(active)) return true;
  return tableSelectionActive(kind);
}

function markTableInteraction(kind) {
  tableInteractionHoldUntil[kind] = Date.now() + TABLE_INTERACTION_HOLD_MS;
}

function orderSelectionActive() {
  return tableSelectionActive("order");
}

function isOrderInteractionActive() {
  return isTableInteractionActive("order");
}

function isOpenInteractionActive() {
  return isTableInteractionActive("open");
}

function currentOpenRows() {
  return scopedOpenRows(openDataScope, latestStatus);
}

function currentOrderRows() {
  if (isExperimentAggregateScope(orderDataScope)) return strategyTables?.recent_orders || [];
  if (orderRows.length) return orderRows;
  return latestStatus?.recent_orders || [];
}

function flushPendingOpenRender() {
  if (!pendingOpenRender || isOpenInteractionActive()) return;
  renderOpenTrades(currentOpenRows(), openDataScope, { force: true });
}

function flushPendingOrderRender() {
  if (!pendingOrderRender || isOrderInteractionActive()) return;
  renderRecentOrders(currentOrderRows(), { force: true });
}

function flushPendingTableRenders() {
  flushPendingOpenRender();
  flushPendingOrderRender();
}

function scheduleProtectedTableFlush() {
  window.setTimeout(flushPendingTableRenders, 120);
  window.setTimeout(flushPendingTableRenders, TABLE_INTERACTION_HOLD_MS + 80);
}

function bindProtectedTableInteraction(kind) {
  const root = tableRoot(kind);
  if (!root) return;
  const mark = () => markTableInteraction(kind);
  root.addEventListener("pointerdown", mark, true);
  root.addEventListener("mousedown", mark, true);
  root.addEventListener("copy", mark, true);
  root.addEventListener("keydown", (event) => {
    if (event.key === "Shift" || event.key.startsWith("Arrow") || event.ctrlKey || event.metaKey) {
      mark();
    }
  }, true);
}

function orderToggleText(row) {
  const count = Number(row.fill_count || 0);
  if (!count) return "-";
  if (loadingOrderId === row.id) return "加载中";
  return expandedOrderId === row.id ? "收起" : "展开";
}

function canCancelOrder(row) {
  if (row?.account_scope === "strategy_experiment" || row?.account_scope === "live") return false;
  return row?.status === "RESTING" || row?.status === "PARTIAL_RESTING";
}

function liveSellButton(row) {
  if (row?.account_scope !== "live" || row?.status !== "OPEN") return "-";
  if (row?.settlement_pending) {
    return `<button class="table-action live-sell-button" type="button" disabled title="市场已结束，等待官方结算">等待结算</button>`;
  }
  if (row?.pending_live_sell_order_id) {
    return `<button class="table-action live-sell-button" type="button" disabled title="等待官方确认卖出订单">卖出确认中</button>`;
  }
  return `<button class="table-action live-sell-button" type="button" data-live-sell-trade-id="${safe(row.id)}">卖出</button>`;
}

function statusWithLabel(status, label) {
  const raw = String(status || "").trim();
  if (!raw) return "-";
  return label ? `${safe(raw)}(${safe(label)})` : safe(raw);
}

function positionStateText(row) {
  const raw = row?.position_state || row?.status_display || row?.status || "";
  const label = row?.position_state_label || row?.status_label || TRADE_STATUS_LABELS[raw] || "";
  return statusWithLabel(raw, label);
}

function tradeStatusText(row) {
  const raw = row?.status_display || row?.position_state || row?.status || "";
  const label = row?.status_label || row?.position_state_label || TRADE_STATUS_LABELS[raw] || "";
  return statusWithLabel(raw, label);
}

function endedPositionCell(row, fallback = "已结束") {
  if (!row?.settlement_pending) return null;
  return `<span class="muted">${safe(fallback)}</span>`;
}

function positionNumberCell(row, key, digits = 4) {
  return endedPositionCell(row) || fmtNumberCell(row?.[key], digits);
}

function positionMoneyCell(row, key) {
  return endedPositionCell(row, "等待结算") || fmtMoneyCell(row?.[key]);
}

function positionSignedMoneyCell(row, key) {
  return endedPositionCell(row, "等待结算") || fmtSignedMoneyCell(row?.[key]);
}

function positionSignedPctCell(row, key) {
  return endedPositionCell(row, "等待结算") || fmtSignedPctCell(row?.[key]);
}

function positionSignedBpsCell(row, key) {
  return endedPositionCell(row, "等待结算") || fmtSignedBpsCell(row?.[key]);
}

function orderStatusText(status) {
  const raw = String(status || "").trim();
  if (!raw) return "-";
  const label = ORDER_STATUS_LABELS[raw];
  return statusWithLabel(raw, label);
}

function isCurrentMarketOrder(row) {
  const currentIds = [activeMarket?.round_id, activeMarket?.slug].filter(Boolean).map(String);
  return currentIds.includes(String(row?.round_id || ""));
}

function cancelableOrderCount(rows, scope) {
  const activeRows = (rows || []).filter(canCancelOrder);
  if (scope === "current_market") return activeRows.filter(isCurrentMarketOrder).length;
  return activeRows.length;
}

function updateOrderActionButtons(rows) {
  if (!isMainScope(orderDataScope)) {
    ids.cancelCurrentOrders.disabled = true;
    ids.cancelAllOrders.disabled = true;
    return;
  }
  const reservedCash = Number(latestStatus?.metrics?.reserved_cash || 0);
  const hasReservedCash = reservedCash > 0.000001;
  ids.cancelCurrentOrders.disabled = !activeMarket || (!hasReservedCash && cancelableOrderCount(rows, "current_market") === 0);
  ids.cancelAllOrders.disabled = !hasReservedCash && cancelableOrderCount(rows, "all") === 0;
}

function orderCancelButton(row) {
  if (row?.account_scope === "strategy_experiment") return "-";
  if (!canCancelOrder(row)) return "-";
  return `<button class="table-action" type="button" data-cancel-order-id="${safe(row.id)}">取消</button>`;
}

function renderOrderFillRow(row, colspan) {
  if (expandedOrderId !== row.id) return "";
  const fills = orderFillCache.get(row.id) || [];
  const body = loadingOrderId === row.id
    ? `<div class="order-fill-empty">加载逐档成交...</div>`
    : orderFillsHtml(fills);
  return `
    <tr class="order-detail-row">
      <td colspan="${Math.max(1, colspan)}">${body}</td>
    </tr>
  `;
}

function orderFillsHtml(fills) {
  if (!fills.length) return `<div class="order-fill-empty">暂无逐档成交</div>`;
  return `
    <div class="order-fill-grid">
      ${fills.map((fill) => `
        <div class="order-fill-item">
          <span>#${safe(fill.level_index)}</span>
          <strong>${fmtNumberCell(fill.price, 4)}</strong>
          <span>${fmtNumberCell(fill.shares, 6)} 份</span>
          <span>${fmtMoneyCell(fill.cash_spent)}</span>
          <span>fee ${fmtMoneyCell(fill.fee)}</span>
        </div>
      `).join("")}
    </div>
  `;
}

function renderTradeTable(kind, rows, fields, headEl, bodyEl, selectedOverride = null) {
  const selected = selectedOverride || selectedFields[kind];
  const visibleFields = fields.filter((field) => selected.includes(field.key));
  headEl.innerHTML = `<tr>${visibleFields.map((field) => `<th>${safe(field.label)}</th>`).join("")}</tr>`;
  if (!rows.length) {
    const emptyText = kind === "open" ? "暂无持仓" : kind === "order" ? "暂无订单" : "暂无交易";
    bodyEl.innerHTML = `<tr><td class="empty" colspan="${Math.max(1, visibleFields.length)}">${emptyText}</td></tr>`;
    return;
  }
  bodyEl.innerHTML = rows.map((row) => {
    const experimentOrder = kind === "order" && row.account_scope === "strategy_experiment";
    return `
    <tr${kind === "order" && !experimentOrder ? ` data-order-id="${safe(row.id)}"` : ""}>
      ${visibleFields.map((field) => `<td class="${field.cellClass || ""}">${field.render(row)}</td>`).join("")}
    </tr>
    ${kind === "order" && !experimentOrder ? renderOrderFillRow(row, visibleFields.length) : ""}
  `;
  }).join("");
}

function initFieldOptions() {
  renderFieldOptions("open", openTradeFields, ids.openFieldOptions);
  renderFieldOptions("order", recentOrderFields, ids.orderFieldOptions);
  renderFieldOptions("recent", recentTradeFields, ids.recentFieldOptions);
}

function renderFieldOptions(kind, fields, container) {
  container.innerHTML = fields.map((field) => {
    const checked = selectedFields[kind].includes(field.key) ? "checked" : "";
    return `
      <label>
        <input type="checkbox" data-kind="${kind}" data-field="${field.key}" ${checked}>
        <span>${safe(field.label)}</span>
      </label>
    `;
  }).join("");
  container.addEventListener("change", (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement)) return;
    const key = input.dataset.field;
    if (!key) return;
    const next = new Set(selectedFields[kind]);
    if (input.checked) next.add(key);
    else next.delete(key);
    if (!next.size) {
      input.checked = true;
      next.add(key);
    }
    selectedFields[kind] = fields.filter((field) => next.has(field.key)).map((field) => field.key);
    localStorage.setItem(FIELD_STORAGE_KEYS[kind], JSON.stringify(selectedFields[kind]));
    if (kind === "open") lastOpenRenderKey = "";
    if (kind === "recent") lastRecentRenderKey = "";
    if (kind === "order") lastOrderRenderKey = "";
    renderAll(latestStatus, { forceOpen: kind === "open", forceOrder: kind === "order" });
  });
}

function loadSelectedFields(kind, fields, defaults) {
  try {
    const raw = localStorage.getItem(FIELD_STORAGE_KEYS[kind]);
    const parsed = raw ? JSON.parse(raw) : null;
    const allowed = new Set(fields.map((field) => field.key));
    const selected = Array.isArray(parsed) ? parsed.filter((key) => allowed.has(key)) : [];
    return selected.length ? selected : defaults.filter((key) => allowed.has(key));
  } catch (_) {
    return defaults;
  }
}

function safe(value) {
  if (value === null || value === undefined || value === "") return "-";
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function fmtMoneyCell(value) {
  const parsed = toNumber(value);
  return parsed == null ? "-" : money.format(parsed);
}

function fmtSignedMoneyCell(value) {
  const parsed = toNumber(value);
  if (parsed == null) return "-";
  return `<span class="${cls(parsed)}">${money.format(parsed)}</span>`;
}

function fmtNumberCell(value, digits = 4) {
  const parsed = toNumber(value);
  if (parsed == null) return "-";
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(parsed);
}

function fmtPctCell(value) {
  const parsed = toNumber(value);
  if (parsed == null) return "-";
  return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(parsed)}%`;
}

function fmtSignedPctCell(value) {
  const parsed = toNumber(value);
  if (parsed == null) return "-";
  return `<span class="${cls(parsed)}">${new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(parsed)}%</span>`;
}

function fmtSignedBpsCell(value) {
  const parsed = toNumber(value);
  if (parsed == null) return "-";
  return `<span class="${cls(parsed)}">${new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(parsed)} bps</span>`;
}

function fmtDateTimeCell(seconds) {
  if (!seconds) return "-";
  return safe(new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false }));
}

function drawChart(points) {
  const canvas = ids.chart;
  const ctx = canvas.getContext("2d");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 680;
  const height = canvas.clientHeight || 190;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#181c20";
  ctx.fillRect(0, 0, width, height);
  const initialBalance = chartInitialBalance();
  const rows = points.length ? points : [{
    cash_balance: initialBalance,
    open_risk: 0,
    realized_pnl: 0,
    total_equity: initialBalance,
    total_pnl: 0,
    created_at: Date.now() / 1000,
  }];
  const padX = 24;
  const padRight = 14;
  const padTop = 2;
  const padBottom = 2;
  const values = rows.map((p) => p.total_equity);
  const min = Math.min(...values, initialBalance);
  const max = Math.max(...values, initialBalance);
  const span = Math.max(1, max - min);
  const minTs = rows[0]?.created_at || Date.now() / 1000;
  const maxTs = rows[rows.length - 1]?.created_at || minTs;
  const timeSpan = Math.max(1, maxTs - minTs);
  const graphWidth = Math.max(1, width - padX - padRight);
  const graphHeight = Math.max(1, height - padTop - padBottom);
  const chartPoints = rows.map((row) => ({
    x: padX + ((row.created_at - minTs) / timeSpan) * graphWidth,
    y: height - padBottom - ((row.total_equity - min) / span) * graphHeight,
    data: row,
  }));

  ctx.strokeStyle = "#323a42";
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i += 1) {
    const y = padTop + (graphHeight / 3) * i;
    ctx.beginPath();
    ctx.moveTo(padX, y);
    ctx.lineTo(width - padX, y);
    ctx.stroke();
  }

  if (initialBalance >= min && initialBalance <= max) {
    const zeroY = height - padBottom - ((initialBalance - min) / span) * graphHeight;
    ctx.strokeStyle = "rgba(244, 191, 80, 0.45)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(padX, zeroY);
    ctx.lineTo(width - padRight, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  if (chartPoints.length > 1) {
    const area = ctx.createLinearGradient(0, padTop, 0, height - padBottom);
    area.addColorStop(0, "rgba(99, 179, 255, 0.24)");
    area.addColorStop(1, "rgba(99, 179, 255, 0)");
    ctx.fillStyle = area;
    ctx.beginPath();
    ctx.moveTo(chartPoints[0].x, height - padBottom);
    ctx.lineTo(chartPoints[0].x, chartPoints[0].y);
    drawSmoothChartPath(ctx, chartPoints, false);
    const lastPoint = chartPoints[chartPoints.length - 1];
    ctx.lineTo(lastPoint.x, height - padBottom);
    ctx.closePath();
    ctx.fill();
  }

  ctx.strokeStyle = "#63b3ff";
  ctx.lineWidth = 2;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.beginPath();
  drawSmoothChartPath(ctx, chartPoints);
  ctx.stroke();

  if (chartPoints.length === 1) {
    ctx.fillStyle = "#63b3ff";
    ctx.beginPath();
    ctx.arc(chartPoints[0].x, chartPoints[0].y, 3, 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.fillStyle = "#9aa7b1";
  ctx.font = "12px system-ui";
  ctx.fillText(money.format(max), padX, 13);
  ctx.fillText(money.format(min), padX, height - 5);

  const hoverPoint = chartHoverX == null ? null : nearestChartPoint(chartPoints, chartHoverX);
  if (hoverPoint) {
    drawChartHover(ctx, hoverPoint, padTop, height - padBottom);
    positionChartTooltip(hoverPoint);
  } else {
    ids.chartTooltip.hidden = true;
  }
}

function chartInitialBalance() {
  const metrics = selectedAccountMetrics(latestStatus);
  return toNumber(metrics?.initial_balance)
    || toNumber(equityCurveMeta?.initial_balance)
    || toNumber(latestStatus?.settings?.initial_balance)
    || 100;
}

function buildChartRows(statusRows = []) {
  const initialBalance = chartInitialBalance();
  const cutoff = Date.now() / 1000 - EQUITY_CURVE_DAYS * 24 * 60 * 60;
  const rowsByTime = new Map();
  for (const raw of equityCurveRows.concat(statusRows || [])) {
    const row = normalizeEquityPoint(raw, initialBalance);
    if (!row || row.created_at < cutoff) continue;
    rowsByTime.set(row.created_at.toFixed(3), row);
  }
  const rows = Array.from(rowsByTime.values()).sort((a, b) => a.created_at - b.created_at);
  return downsampleEquityRows(rows, EQUITY_CURVE_MAX_POINTS + 120);
}

function selectedAccountCurrentPoint(data = latestStatus) {
  const metrics = selectedAccountMetrics(data);
  const totalEquity = toNumber(metrics?.total_equity);
  if (totalEquity == null) return null;
  const initialBalance = toNumber(metrics?.initial_balance) || chartInitialBalance();
  return {
    cash_balance: toNumber(metrics?.cash_balance) ?? totalEquity,
    open_risk: toNumber(metrics?.open_risk) ?? 0,
    realized_pnl: toNumber(metrics?.realized_pnl) ?? totalEquity - initialBalance,
    total_equity: totalEquity,
    total_pnl: toNumber(metrics?.total_pnl) ?? totalEquity - initialBalance,
    created_at: toNumber(data?.runtime?.last_tick_at) || Date.now() / 1000,
  };
}

function normalizeEquityPoint(row, initialBalance) {
  if (!row || typeof row !== "object") return null;
  const createdAt = toNumber(row.created_at);
  const totalEquity = toNumber(row.total_equity);
  if (createdAt == null || totalEquity == null) return null;
  const cashBalance = toNumber(row.cash_balance) ?? totalEquity;
  const openRisk = toNumber(row.open_risk) ?? 0;
  const realizedPnl = toNumber(row.realized_pnl) ?? totalEquity - initialBalance;
  const totalPnl = toNumber(row.total_pnl) ?? totalEquity - initialBalance;
  return {
    cash_balance: cashBalance,
    open_risk: openRisk,
    realized_pnl: realizedPnl,
    total_equity: totalEquity,
    total_pnl: totalPnl,
    created_at: createdAt,
  };
}

function downsampleEquityRows(rows, maxPoints) {
  if (rows.length <= maxPoints) return rows;
  const stride = Math.ceil(rows.length / Math.max(1, maxPoints - 1));
  const result = [];
  for (let index = 0; index < rows.length; index += stride) {
    result.push(rows[index]);
  }
  const last = rows[rows.length - 1];
  const limited = result.slice(0, maxPoints);
  if (limited[limited.length - 1] !== last) {
    if (limited.length >= maxPoints) limited[limited.length - 1] = last;
    else limited.push(last);
  }
  return limited;
}

function nearestChartPoint(points, x) {
  let nearest = points[0] || null;
  let nearestDistance = nearest ? Math.abs(nearest.x - x) : Infinity;
  for (const point of points) {
    const distance = Math.abs(point.x - x);
    if (distance < nearestDistance) {
      nearest = point;
      nearestDistance = distance;
    }
  }
  return nearest;
}

function drawChartHover(ctx, point, top, bottom) {
  ctx.strokeStyle = "rgba(238, 242, 244, 0.45)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(point.x, top);
  ctx.lineTo(point.x, bottom);
  ctx.stroke();
  ctx.fillStyle = "#63b3ff";
  ctx.strokeStyle = "#181c20";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(point.x, point.y, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
}

function positionChartTooltip(point) {
  const tooltip = ids.chartTooltip;
  const row = point.data;
  const pnlClass = cls(row.total_pnl);
  const pnlLabel = row.total_pnl > 0 ? "盈利" : row.total_pnl < 0 ? "亏损" : "持平";
  tooltip.innerHTML = `
    <div class="chart-tooltip-time">${safe(formatChartTime(row.created_at))}</div>
    <div class="chart-tooltip-row"><span>总资产</span><strong>${money.format(row.total_equity)}</strong></div>
    <div class="chart-tooltip-row"><span>总盈亏</span><strong class="${pnlClass}">${signedMoney(row.total_pnl)}</strong></div>
    <div class="chart-tooltip-row"><span>状态</span><strong class="${pnlClass}">${pnlLabel}</strong></div>
    <div class="chart-tooltip-row"><span>可用资金</span><strong>${money.format(row.cash_balance)}</strong></div>
    <div class="chart-tooltip-row"><span>持仓风险</span><strong>${money.format(row.open_risk)}</strong></div>
  `;
  tooltip.hidden = false;
  const canvasRect = ids.chart.getBoundingClientRect();
  const parentRect = tooltip.parentElement.getBoundingClientRect();
  const width = tooltip.offsetWidth;
  const height = tooltip.offsetHeight;
  let left = canvasRect.left - parentRect.left + point.x + 12;
  let top = canvasRect.top - parentRect.top + point.y - height - 10;
  if (left + width > parentRect.width - 6) left = canvasRect.left - parentRect.left + point.x - width - 12;
  top = Math.max(4, Math.min(parentRect.height - height - 4, top));
  tooltip.style.left = `${Math.max(4, left)}px`;
  tooltip.style.top = `${top}px`;
}

function formatChartTime(seconds) {
  return new Date(seconds * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function drawSmoothChartPath(ctx, points, moveToFirst = true) {
  if (!points.length) return;
  if (moveToFirst) ctx.moveTo(points[0].x, points[0].y);
  if (points.length === 1) return;
  for (let index = 1; index < points.length - 1; index += 1) {
    const current = points[index];
    const next = points[index + 1];
    const midX = (current.x + next.x) / 2;
    const midY = (current.y + next.y) / 2;
    ctx.quadraticCurveTo(current.x, current.y, midX, midY);
  }
  const last = points[points.length - 1];
  ctx.lineTo(last.x, last.y);
}

async function loadStatus(manual = false) {
  const res = await fetch(manual ? "/api/tick" : "/api/status", manual ? { method: "POST" } : {});
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  applyStatusPayload(data, { manual, refreshAuxiliary: true });
}

async function runManualTick() {
  await loadStatus();
}

function applyStatusPayload(data, options = {}) {
  const manual = Boolean(options.manual);
  const refreshAuxiliary = options.refreshAuxiliary !== false;
  latestStatus = data;
  if (data.runtime.current_market) applyMarket(data.runtime.current_market);
  if (data.runtime.latest_quotes && !Object.keys(quotes).length) quotes = data.runtime.latest_quotes;
  if (data.runtime.latest_price && !priceState.chainlink && !priceState.binance) {
    priceState = { ...priceState, ...data.runtime.latest_price };
  }
  if (!isMainScope(recentDataScope)) {
    data.recent_trades = recentRows;
    data.recent_trades_meta = recentMeta;
    data.recent_trades_summary = recentSummary;
  } else if (recentFilterActive()) {
    data.recent_trades = recentRows;
    data.recent_trades_meta = recentMeta;
    data.recent_trades_summary = recentSummary;
  } else {
    applyRecentPage(data.recent_trades, data.recent_trades_meta, data.recent_trades_summary);
  }
  if (!isMainScope(orderDataScope)) {
    data.recent_orders = orderRows;
    data.recent_orders_meta = orderMeta;
  } else if (orderStatusFilter === "all") {
    applyOrderPage(data.recent_orders, data.recent_orders_meta);
  } else {
    data.recent_orders = orderRows;
    data.recent_orders_meta = orderMeta;
  }
  renderAll(data, { forceOrder: manual });
  if (!refreshAuxiliary) return;
  loadEquityCurve(false).catch(showError);
  loadOrders(orderStatusFilter !== "all").catch(showError);
  refreshVisibleScopedRecentTrades().catch(showError);
  if (strategyExperimentViewActive()) {
    loadStrategyExperimentTables({ force: manual }).catch(showError);
  }
}

function connectStatusStream() {
  if (!window.EventSource || statusStream) return;
  statusStream = new EventSource("/api/status-stream");
  statusStream.onopen = () => {
    statusStreamConnected = true;
  };
  statusStream.onmessage = (event) => {
    lastStatusStreamAt = Date.now();
    if (!pageVisible) return;
    try {
      applyStatusPayload(JSON.parse(event.data), { refreshAuxiliary: false });
    } catch (error) {
      showError(error);
    }
  };
  statusStream.onerror = () => {
    statusStreamConnected = false;
    if (statusStream) {
      statusStream.close();
      statusStream = null;
    }
    setTimeout(connectStatusStream, 2_000);
  };
}

async function loadEquityCurve(force = false) {
  const now = Date.now();
  if (equityCurveInFlight) {
    if (force) equityCurvePendingForce = true;
    return;
  }
  if (!force && now - lastEquityCurveFetchMs < EQUITY_CURVE_REFRESH_MS) return;
  equityCurveInFlight = true;
  const requestScope = accountScope;
  const selection = parseAccountScope(requestScope);
  try {
    const params = new URLSearchParams({
      days: String(EQUITY_CURVE_DAYS),
      max_points: String(EQUITY_CURVE_MAX_POINTS),
    });
    if (selection.scope === "experiment") {
      params.set("account_scope", "strategy_experiment");
      params.set("variant_id", selection.variantId);
    } else if (selection.scope === "live") {
      params.set("account_scope", "live");
      params.set("variant_id", selection.variantId);
    } else {
      params.set("account_scope", "main");
    }
    const res = await fetch(`/api/equity-curve?${params.toString()}`);
    if (!res.ok) throw new Error(`equity curve HTTP ${res.status}`);
    const payload = await res.json();
    if (requestScope !== accountScope) return;
    const rows = Array.isArray(payload.equity_curve) ? payload.equity_curve : [];
    equityCurveRows = rows;
    equityCurveMeta = payload.equity_curve_meta || {};
    lastEquityCurveFetchMs = Date.now();
    renderAll(latestStatus, { forceChart: true });
  } finally {
    equityCurveInFlight = false;
    if (equityCurvePendingForce) {
      equityCurvePendingForce = false;
      loadEquityCurve(true).catch(showError);
    }
  }
}

function applyRecentPage(rows = [], meta = {}, summary = {}, options = {}) {
  const incoming = Array.isArray(rows) ? rows : [];
  if (options.replace) {
    recentRows = incoming;
  } else if (recentRows.length > incoming.length) {
    const seen = new Set(incoming.map((row) => row.id));
    recentRows = incoming.concat(recentRows.filter((row) => !seen.has(row.id)));
  } else {
    recentRows = incoming;
  }
  const total = Number(meta.total || recentRows.length || 0);
  recentMeta = {
    limit: Number(meta.limit || RECENT_PAGE_SIZE),
    offset: 0,
    loaded: recentRows.length,
    total,
    has_more: Boolean(meta.has_more || recentRows.length < total),
    start_at: meta.start_at ?? recentFilters.start_at,
    end_at: meta.end_at ?? recentFilters.end_at,
  };
  recentSummary = summary || recentSummary || {};
  recentLoading = false;
  if (latestStatus) {
    latestStatus.recent_trades = recentRows;
    latestStatus.recent_trades_meta = recentMeta;
    latestStatus.recent_trades_summary = recentSummary;
  }
}

function applyOrderPage(rows = [], meta = {}, options = {}) {
  const incoming = Array.isArray(rows) ? rows : [];
  const incomingFilter = String(meta.status_filter || orderStatusFilter || "all");
  const replaceRows = Boolean(options.replace) || incomingFilter !== orderMeta.status_filter;
  if (replaceRows) {
    orderRows = incoming;
  } else if (orderRows.length > incoming.length) {
    const seen = new Set(incoming.map((row) => row.id));
    orderRows = incoming.concat(orderRows.filter((row) => !seen.has(row.id)));
  } else {
    orderRows = incoming;
  }
  const total = Number(meta.total || orderRows.length || 0);
  orderMeta = {
    limit: Number(meta.limit || ORDER_PAGE_SIZE),
    offset: 0,
    loaded: orderRows.length,
    total,
    has_more: Boolean(meta.has_more || orderRows.length < total),
    status_filter: incomingFilter,
  };
  if (latestStatus) {
    latestStatus.recent_orders = orderRows;
    latestStatus.recent_orders_meta = orderMeta;
  }
}

function strategyExperimentViewActive() {
  return [openDataScope, orderDataScope, recentDataScope].some((scope) => (
    isExperimentAggregateScope(scope) || isExperimentVariantScope(scope)
  ));
}

async function loadStrategyExperimentTables(options = {}) {
  if (strategyTablesLoading) return;
  const force = Boolean(options.force);
  const now = Date.now();
  if (!force && strategyTables && now - lastStrategyTablesFetchMs < 5_000) return;
  strategyTablesLoading = true;
  try {
    const params = new URLSearchParams({
      trade_limit: String(options.tradeLimit || strategyTradeLimit),
      order_limit: String(options.orderLimit || strategyOrderLimit),
      status: orderStatusFilter,
    });
    if (recentFilters.start_at !== null) params.set("start_at", String(recentFilters.start_at));
    if (recentFilters.end_at !== null) params.set("end_at", String(recentFilters.end_at));
    const res = await fetch(`/api/strategy-experiments-tables?${params.toString()}`);
    if (!res.ok) throw new Error(`strategy experiment tables HTTP ${res.status}`);
    strategyTables = await res.json();
    lastStrategyTablesFetchMs = Date.now();
    strategyOrderLimit = Number(strategyTables?.recent_orders_meta?.limit || strategyOrderLimit);
    strategyTradeLimit = Number(strategyTables?.recent_trades_meta?.limit || strategyTradeLimit);
    if (recentDataScope === "experiment") recentLoading = false;
  } finally {
    strategyTablesLoading = false;
  }
  renderAll(latestStatus, { force: true, forceOrder: true });
}

async function loadOrders(force = false) {
  if (isExperimentAggregateScope(orderDataScope)) {
    await loadStrategyExperimentTables({ force, orderLimit: strategyOrderLimit });
    return;
  }
  if (!force && isMainScope(orderDataScope) && orderRows.length && orderMeta.status_filter === orderStatusFilter) return;
  const requestScope = orderDataScope;
  const requestStatusFilter = orderStatusFilter;
  const params = new URLSearchParams({ limit: String(ORDER_PAGE_SIZE), offset: "0", status: orderStatusFilter });
  appendScopeParams(params, requestScope);
  const res = await fetch(`/api/orders?${params.toString()}`);
  if (!res.ok) throw new Error(`orders HTTP ${res.status}`);
  const page = await res.json();
  if (requestScope !== orderDataScope || requestStatusFilter !== orderStatusFilter) return;
  applyOrderPage(page.recent_orders, page.recent_orders_meta || { status_filter: orderStatusFilter }, { replace: force });
  renderRecentOrders(orderRows, { force });
}

async function loadMoreOrders() {
  ids.loadMoreOrders.disabled = true;
  try {
    if (isExperimentAggregateScope(orderDataScope)) {
      strategyOrderLimit = Math.min(200, Number(strategyTables?.recent_orders_meta?.loaded || 0) + ORDER_PAGE_SIZE);
      await loadStrategyExperimentTables({ force: true, orderLimit: strategyOrderLimit });
      return;
    }
    const params = new URLSearchParams({
      limit: String(ORDER_PAGE_SIZE),
      offset: String(orderRows.length),
      status: orderStatusFilter,
    });
    appendScopeParams(params, orderDataScope);
    const res = await fetch(`/api/orders?${params.toString()}`);
    if (!res.ok) throw new Error(`orders HTTP ${res.status}`);
    const page = await res.json();
    const nextRows = Array.isArray(page.recent_orders) ? page.recent_orders : [];
    const seen = new Set(orderRows.map((row) => row.id));
    orderRows = orderRows.concat(nextRows.filter((row) => !seen.has(row.id)));
    const meta = page.recent_orders_meta || {};
    orderMeta = {
      limit: Number(meta.limit || ORDER_PAGE_SIZE),
      offset: 0,
      loaded: orderRows.length,
      total: Number(meta.total || orderRows.length),
      has_more: orderRows.length < Number(meta.total || orderRows.length),
      status_filter: String(meta.status_filter || orderStatusFilter),
    };
    if (latestStatus) {
      latestStatus.recent_orders = orderRows;
      latestStatus.recent_orders_meta = orderMeta;
    }
    renderRecentOrders(orderRows, { force: true });
  } finally {
    ids.loadMoreOrders.disabled = false;
  }
}

async function toggleOrderFills(orderId) {
  if (!orderId) return;
  if (expandedOrderId === orderId) {
    expandedOrderId = null;
    renderRecentOrders(orderRows, { force: true });
    return;
  }
  expandedOrderId = orderId;
  if (!orderFillCache.has(orderId)) {
    loadingOrderId = orderId;
    renderRecentOrders(orderRows, { force: true });
    try {
      const params = new URLSearchParams({ order_id: String(orderId) });
      appendScopeParams(params, orderDataScope);
      const res = await fetch(`/api/order-fills?${params.toString()}`);
      if (!res.ok) throw new Error(`order fills HTTP ${res.status}`);
      const payload = await res.json();
      orderFillCache.set(orderId, Array.isArray(payload.fills) ? payload.fills : []);
    } finally {
      loadingOrderId = null;
    }
  }
  renderRecentOrders(orderRows, { force: true });
}

async function toggleExperimentDetail(variantId) {
  if (!variantId) return;
  if (expandedExperimentId === variantId) {
    expandedExperimentId = null;
    lastExperimentRenderKey = "";
    renderStrategyExperiments(latestStatus?.runtime || {});
    return;
  }
  expandedExperimentId = variantId;
  loadingExperimentId = variantId;
  lastExperimentRenderKey = "";
  renderStrategyExperiments(latestStatus?.runtime || {});
  try {
    const params = new URLSearchParams({
      variant_id: variantId,
      trade_limit: "6",
      order_limit: "6",
    });
    const res = await fetch(`/api/strategy-experiments?${params.toString()}`);
    if (!res.ok) throw new Error(`strategy experiment HTTP ${res.status}`);
    experimentDetailCache.set(variantId, await res.json());
  } finally {
    loadingExperimentId = null;
  }
  lastExperimentRenderKey = "";
  renderStrategyExperiments(latestStatus?.runtime || {});
}

async function cancelOrder(orderId) {
  if (!orderId) return;
  const res = await fetch("/api/cancel-order", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ order_id: orderId }),
  });
  if (!res.ok) throw new Error(`cancel order HTTP ${res.status}`);
  const payload = await res.json();
  if (payload.not_canceled && Object.keys(payload.not_canceled).length) {
    const reason = payload.not_canceled[String(orderId)] || "取消失败";
    throw new Error(reason);
  }
  await loadStatus();
  await loadOrders(true);
  renderRecentOrders(orderRows, { force: true });
}

async function cancelOrders(scope) {
  const res = await fetch("/api/cancel-orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope }),
  });
  if (!res.ok) throw new Error(`cancel orders HTTP ${res.status}`);
  const payload = await res.json();
  if (!(payload.canceled || []).length && payload.not_canceled && Object.keys(payload.not_canceled).length) {
    throw new Error(Object.values(payload.not_canceled)[0] || "批量取消失败");
  }
  await loadStatus();
  await loadOrders(true);
  renderRecentOrders(orderRows, { force: true });
}

async function sellLiveTrade(tradeId) {
  if (!tradeId) return;
  const res = await fetch("/api/live-sell", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ trade_id: tradeId }),
  });
  if (!res.ok) throw new Error(`live sell HTTP ${res.status}`);
  await loadStatus();
  if (isLiveScope(openDataScope) || isLiveScope(orderDataScope) || isLiveScope(recentDataScope)) {
    await loadOrders(true);
    await loadRecentTradesPage(true);
  }
}

function confirmLiveSell(tradeId) {
  return window.confirm(`确认实盘卖出持仓 ${tradeId}？`);
}

function handleOrderStatusFilterChange() {
  orderStatusFilter = ids.orderStatusFilter.value || "all";
  expandedOrderId = null;
  loadingOrderId = null;
  orderRows = [];
  orderMeta = { limit: ORDER_PAGE_SIZE, offset: 0, loaded: 0, total: 0, has_more: false, status_filter: orderStatusFilter };
  if (latestStatus) {
    latestStatus.recent_orders = orderRows;
    latestStatus.recent_orders_meta = orderMeta;
  }
  renderRecentOrders(orderRows, { force: true });
  if (isExperimentAggregateScope(orderDataScope)) {
    strategyOrderLimit = ORDER_PAGE_SIZE;
    loadStrategyExperimentTables({ force: true, orderLimit: strategyOrderLimit }).catch(showError);
  } else {
    loadOrders(true).catch(showError);
  }
}

function handleDataScopeChange(kind, value) {
  const normalized = value || "main";
  if (kind === "open") {
    openDataScope = normalized;
    pendingOpenRender = false;
    lastOpenRenderKey = "";
  }
  if (kind === "order") {
    orderDataScope = normalized;
    expandedOrderId = null;
    loadingOrderId = null;
    orderRows = [];
    orderMeta = { limit: ORDER_PAGE_SIZE, offset: 0, loaded: 0, total: 0, has_more: false, status_filter: orderStatusFilter };
    if (latestStatus) {
      latestStatus.recent_orders = orderRows;
      latestStatus.recent_orders_meta = orderMeta;
    }
    lastOrderRenderKey = "";
  }
  if (kind === "recent") {
    recentDataScope = normalized;
    lastRecentRenderKey = "";
    recentRows = [];
    recentSummary = null;
    recentMeta = {
      limit: RECENT_PAGE_SIZE,
      offset: 0,
      loaded: 0,
      total: 0,
      has_more: false,
      start_at: recentFilters.start_at,
      end_at: recentFilters.end_at,
    };
    if (isExperimentAggregateScope(normalized)) recentLoading = false;
    else recentLoading = true;
  }
  renderAll(latestStatus, { force: true, forceOrder: true });
  if (isExperimentAggregateScope(normalized) || isExperimentVariantScope(normalized)) {
    if (kind === "order") strategyOrderLimit = ORDER_PAGE_SIZE;
    if (kind === "recent") strategyTradeLimit = RECENT_PAGE_SIZE;
    loadStrategyExperimentTables({ force: true }).catch(showError);
  }
  if (kind === "order") {
    loadOrders(true).catch(showError);
  } else if (kind === "recent" && !recentRows.length) {
    loadRecentTradesPage(true).catch(showError);
  }
}

function confirmCancelOrders(scope) {
  const label = scope === "all" ? "取消全部 Paper 活跃挂单" : "取消当前市场 Paper 活跃挂单";
  return window.confirm(`确认${label}？`);
}

function bindCancelOrdersButton(button, scope) {
  button.addEventListener("click", () => {
    if (!confirmCancelOrders(scope)) return;
    button.disabled = true;
    cancelOrders(scope).catch((error) => {
      button.disabled = false;
      showError(error);
    });
  });
}

function recentFilterActive() {
  return recentFilters.start_at !== null || recentFilters.end_at !== null;
}

function recentFilterKey() {
  return `${recentFilters.start_at ?? ""}:${recentFilters.end_at ?? ""}`;
}

function datetimeLocalToSeconds(value) {
  if (!value) return null;
  const millis = new Date(value).getTime();
  if (!Number.isFinite(millis)) throw new Error("时间格式不正确");
  return Math.floor(millis / 1000);
}

function recentTradeQueryParams(offset, scope = recentDataScope) {
  const params = new URLSearchParams({
    limit: String(RECENT_PAGE_SIZE),
    offset: String(offset),
  });
  if (recentFilters.start_at !== null) params.set("start_at", String(recentFilters.start_at));
  if (recentFilters.end_at !== null) params.set("end_at", String(recentFilters.end_at));
  appendScopeParams(params, scope);
  return params;
}

async function loadRecentTradesPage(replace = false, options = {}) {
  const requestScope = recentDataScope;
  const requestFilterKey = recentFilterKey();
  if (isExperimentAggregateScope(requestScope)) {
    strategyTradeLimit = replace ? RECENT_PAGE_SIZE : Math.min(500, Number(strategyTables?.recent_trades_meta?.loaded || 0) + RECENT_PAGE_SIZE);
    await loadStrategyExperimentTables({ force: true, tradeLimit: strategyTradeLimit });
    return;
  }
  const offset = replace ? 0 : recentRows.length;
  const params = recentTradeQueryParams(offset, requestScope);
  const res = await fetch(`/api/recent-trades?${params.toString()}`);
  if (!res.ok) throw new Error(`recent trades HTTP ${res.status}`);
  const page = await res.json();
  if (requestScope !== recentDataScope || requestFilterKey !== recentFilterKey()) return;
  const nextRows = Array.isArray(page.recent_trades) ? page.recent_trades : [];
  const meta = page.recent_trades_meta || {};
  if (replace) {
    applyRecentPage(nextRows, meta, page.recent_trades_summary, { replace: true });
  } else {
    const seen = new Set(recentRows.map((row) => row.id));
    recentRows = recentRows.concat(nextRows.filter((row) => !seen.has(row.id)));
    recentMeta = {
      limit: Number(meta.limit || RECENT_PAGE_SIZE),
      offset: 0,
      loaded: recentRows.length,
      total: Number(meta.total || recentRows.length),
      has_more: recentRows.length < Number(meta.total || recentRows.length),
      start_at: meta.start_at ?? recentFilters.start_at,
      end_at: meta.end_at ?? recentFilters.end_at,
    };
    recentSummary = page.recent_trades_summary || recentSummary || {};
    if (latestStatus) {
      latestStatus.recent_trades = recentRows;
      latestStatus.recent_trades_meta = recentMeta;
      latestStatus.recent_trades_summary = recentSummary;
    }
  }
  if (options.forceRender !== false) lastRecentRenderKey = "";
  renderRecentTrades(recentRows);
}

function recentScopeNeedsDirectRefresh(scope = recentDataScope) {
  return isLiveScope(scope) || isExperimentVariantScope(scope);
}

async function refreshVisibleScopedRecentTrades() {
  if (!recentScopeNeedsDirectRefresh(recentDataScope) || scopedRecentRefreshInFlight) return;
  scopedRecentRefreshInFlight = true;
  try {
    await loadRecentTradesPage(true, { forceRender: false });
  } finally {
    scopedRecentRefreshInFlight = false;
  }
}

async function applyRecentTradeFilter() {
  const startAt = datetimeLocalToSeconds(ids.recentStartTime.value);
  const endAt = datetimeLocalToSeconds(ids.recentEndTime.value);
  if (startAt !== null && endAt !== null && endAt < startAt) {
    throw new Error("结束时间不能早于开始时间");
  }
  recentFilters = { start_at: startAt, end_at: endAt };
  recentRows = [];
  recentSummary = null;
  recentMeta = { limit: RECENT_PAGE_SIZE, offset: 0, loaded: 0, total: 0, has_more: false, start_at: startAt, end_at: endAt };
  setRecentLoading(true);
  ids.applyRecentFilter.disabled = true;
  ids.resetRecentFilter.disabled = true;
  try {
    await loadRecentTradesPage(true);
  } finally {
    ids.applyRecentFilter.disabled = false;
    ids.resetRecentFilter.disabled = false;
  }
}

async function resetRecentTradeFilter() {
  ids.recentStartTime.value = "";
  ids.recentEndTime.value = "";
  recentFilters = { start_at: null, end_at: null };
  recentRows = [];
  recentSummary = null;
  recentMeta = { limit: RECENT_PAGE_SIZE, offset: 0, loaded: 0, total: 0, has_more: false, start_at: null, end_at: null };
  setRecentLoading(true);
  await loadRecentTradesPage(true);
}

async function togglePaperPause() {
  const paused = !Boolean(latestStatus?.runtime?.paper_trading?.paused);
  return withButtonLoading(ids.paperPauseToggle, async () => {
    const res = await fetch("/api/paper-pause", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paused }),
    });
    if (!res.ok) throw new Error(`paper pause HTTP ${res.status}`);
    const payload = await res.json();
    latestStatus = payload.snapshot || latestStatus;
    expandedOrderId = null;
    loadingOrderId = null;
    orderRows = [];
    recentRows = [];
    lastOrderRenderKey = "";
    lastRecentRenderKey = "";
    renderAll(latestStatus, { force: true, forceChart: true, forceOrder: true });
    loadOrders(true).catch(showError);
    loadRecentTradesPage(true).catch(showError);
  });
}

function liveSettingsPayload(overrides = {}) {
  return {
    enabled: ids.liveEnabled?.checked || false,
    initial_balance: toNumber(ids.liveInitialBalance?.value) ?? 20,
    stake_dollars: toNumber(ids.liveStakeDollars?.value) ?? 2,
    max_open_trades: Math.max(1, Math.round(toNumber(ids.liveMaxOpenTrades?.value) ?? 2)),
    max_entry_price: Math.max(0.01, Math.min(0.99, toNumber(ids.liveMaxEntryPrice?.value) ?? 0.72)),
    max_daily_loss: toNumber(ids.liveMaxDailyLoss?.value) ?? 6,
    max_total_drawdown: toNumber(ids.liveMaxTotalDrawdown?.value) ?? 12,
    retry_count: Math.max(0, Math.round(toNumber(ids.liveRetryCount?.value) ?? 2)),
    retry_delay_ms: Math.max(0, Math.round(toNumber(ids.liveRetryDelayMs?.value) ?? 250)),
    fallback_sources: (ids.liveFallbackSources || [])
      .filter((input) => input.checked)
      .map((input) => String(input.dataset.liveFallbackSource || "").toLowerCase())
      .filter(Boolean),
    compliance_acknowledged: ids.liveComplianceAck?.checked || false,
    ...overrides,
  };
}

async function saveLiveSettings(overrides = {}) {
  return withButtonLoading(ids.liveSaveSettings, async () => {
    appendLiveLog({ level: "info", title: "保存实盘配置", message: "请求已发送，等待返回" });
    try {
      const res = await fetch("/api/live-settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(liveSettingsPayload(overrides)),
      });
      if (!res.ok) throw new Error(`live settings HTTP ${res.status}`);
      const payload = await res.json();
      latestStatus = payload.snapshot || latestStatus;
      clearLiveSettingsDirty();
      if (!Object.keys(overrides || {}).length) setLiveSettingsOpen(false);
      appendLiveLog({ level: "pass", title: "保存实盘配置", message: "已保存" });
      renderAll(latestStatus, { force: true, forceChart: true, forceOrder: true });
    } catch (error) {
      appendLiveLog({ level: "error", title: "保存实盘配置", message: error.message || String(error) });
      throw error;
    }
  });
}

async function toggleLiveEnabled(enabled) {
  const res = await fetch("/api/live-toggle", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!res.ok) throw new Error(`live toggle HTTP ${res.status}`);
  const payload = await res.json();
  latestStatus = payload.snapshot || latestStatus;
  renderAll(latestStatus, { force: true, forceChart: true, forceOrder: true });
}

async function runLivePreflight() {
  return withButtonLoading(ids.livePreflight, async () => {
    appendLiveLog({ level: "info", title: "预检", message: "请求已发送，等待返回" });
    try {
      const res = await fetch("/api/live-preflight", { method: "POST" });
      if (!res.ok) throw new Error(`live preflight HTTP ${res.status}`);
      const payload = await res.json();
      livePreflight = payload.live_preflight || null;
      latestStatus = payload.snapshot || latestStatus;
      renderAll(latestStatus, { force: true, forceChart: true, forceOrder: true });
    } catch (error) {
      appendLiveLog({ level: "error", title: "预检", message: error.message || String(error) });
      throw error;
    }
  });
}

async function reloadLiveCredentials() {
  return withButtonLoading(ids.liveReloadCredentials, async () => {
    appendLiveLog({ level: "info", title: "重载凭证", message: "请求已发送，等待返回" });
    try {
      const res = await fetch("/api/live-reload-credentials", { method: "POST" });
      if (!res.ok) throw new Error(`live reload credentials HTTP ${res.status}`);
      const payload = await res.json();
      latestStatus = payload.snapshot || latestStatus;
      livePreflight = null;
      liveDoctor = null;
      appendLiveLog({ level: "pass", title: "重载凭证", message: "已刷新本进程凭证缓存" });
      renderAll(latestStatus, { force: true, forceChart: false, forceOrder: false });
    } catch (error) {
      appendLiveLog({ level: "error", title: "重载凭证", message: error.message || String(error) });
      throw error;
    }
  });
}

async function runLiveDoctor() {
  return withButtonLoading(ids.liveDoctor, async () => {
    appendLiveLog({ level: "info", title: "首单检查", message: "请求已发送，等待返回" });
    try {
      const res = await fetch("/api/live-doctor?refresh=true");
      if (!res.ok) throw new Error(`live doctor HTTP ${res.status}`);
      const payload = await res.json();
      liveDoctor = payload.live_doctor || null;
      if (payload.snapshot) latestStatus = payload.snapshot;
      renderAll(latestStatus, { force: true, forceChart: false, forceOrder: false });
    } catch (error) {
      appendLiveLog({ level: "error", title: "首单检查", message: error.message || String(error) });
      throw error;
    }
  });
}

async function fetchLiveDoctor(refresh = true) {
  const res = await fetch(`/api/live-doctor?refresh=${refresh ? "true" : "false"}`);
  if (!res.ok) throw new Error(`live doctor HTTP ${res.status}`);
  const payload = await res.json();
  liveDoctor = payload.live_doctor || null;
  if (payload.snapshot) latestStatus = payload.snapshot;
  return liveDoctor;
}

function liveOnceBodyFromDoctor(doctor) {
  const firstOrder = doctor?.first_order || {};
  const api = firstOrder.recommended_api || {};
  const body = { ...(api.body || {}) };
  body.confirm = "PLACE_REAL_ORDER";
  body.acknowledge_compliance = true;
  body.disable_after = body.disable_after !== false;
  body.wait_ready_seconds = toNumber(body.wait_ready_seconds) ?? 180;
  body.ready_poll_seconds = toNumber(body.ready_poll_seconds) ?? 2;
  body.reconcile_wait_seconds = toNumber(body.reconcile_wait_seconds) ?? 20;
  body.include_evidence = body.include_evidence !== false;
  body.max_stake_dollars = toNumber(body.max_stake_dollars ?? firstOrder.max_stake_dollars) ?? 2;
  return body;
}

async function runLiveOnce() {
  return withButtonLoading(ids.liveOnce, async () => {
    liveOnce = { local_status: "RUNNING", message: "刷新首单检查中" };
    renderLiveOnceResult();
    try {
      const doctor = await fetchLiveDoctor(true);
      renderAll(latestStatus, { force: true, forceChart: false, forceOrder: false });
      const fatal = Array.isArray(doctor?.fatal_one_shot_blockers) ? doctor.fatal_one_shot_blockers : [];
      const canRun = Boolean(doctor && (doctor.ready_for_one_shot_now || doctor.can_wait_for_one_shot));
      if (!canRun || fatal.length) {
        liveOnce = {
          local_status: "BLOCKED",
          blocked: true,
          message: "首单检查未通过，未调用 /api/live-once",
          fatal_blocked_keys: fatal,
          blocked_keys: Array.isArray(doctor?.one_shot_blockers) ? doctor.one_shot_blockers : [],
          checked_at: doctor?.checked_at,
        };
        renderLiveOnceResult();
        return;
      }
      const body = liveOnceBodyFromDoctor(doctor);
      const typed = prompt(`输入 PLACE_REAL_ORDER 执行 one-shot 首单，max stake ${fmtNumberCell(body.max_stake_dollars, 2)} USDC`);
      if (typed !== "PLACE_REAL_ORDER") {
        liveOnce = {
          local_status: "ABORTED",
          message: "确认短语不匹配，未提交真实订单请求",
          checked_at: Date.now() / 1000,
        };
        renderLiveOnceResult();
        return;
      }
      liveOnce = { local_status: "RUNNING", message: "已发送 one-shot 请求，等待官方返回" };
      renderLiveOnceResult();
      const res = await fetch("/api/live-once", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = await res.json().catch(() => ({ error: `live once HTTP ${res.status}` }));
      if (payload.snapshot) latestStatus = payload.snapshot;
      liveOnce = payload.live_once || {
        local_status: res.ok ? "DONE" : "BLOCKED",
        blocked: !res.ok,
        error: payload.error || `live once HTTP ${res.status}`,
      };
      if (liveOnce.preflight) livePreflight = liveOnce.preflight;
      renderAll(latestStatus, { force: true, forceChart: true, forceOrder: true });
      if (!res.ok && !payload.live_once) throw new Error(payload.error || `live once HTTP ${res.status}`);
    } catch (error) {
      appendLiveLog({ level: "error", title: "one-shot 首单", message: error.message || String(error) });
      throw error;
    } finally {
      updateLiveOnceButtonState();
    }
  }, updateLiveOnceButtonState);
}

async function liveEmergencyStop() {
  if (!confirm("确认关闭实盘并请求取消官方 CLOB 全部挂单？")) return;
  return withButtonLoading(ids.liveEmergencyStop, async () => {
    appendLiveLog({ level: "warn", title: "实盘急停", message: "请求已发送，等待取消挂单结果" });
    try {
      const res = await fetch("/api/live-emergency-stop", { method: "POST" });
      if (!res.ok) throw new Error(`live emergency stop HTTP ${res.status}`);
      const payload = await res.json();
      latestStatus = payload.snapshot || latestStatus;
      livePreflight = null;
      renderAll(latestStatus, { force: true, forceChart: true, forceOrder: true });
      const errors = payload.cancel_all?.errors || [];
      if (errors.length) throw new Error(errors[0]);
      appendLiveLog({ level: "pass", title: "实盘急停", message: "实盘已关闭，取消挂单请求已完成" });
    } catch (error) {
      appendLiveLog({ level: "error", title: "实盘急停", message: error.message || String(error) });
      throw error;
    }
  });
}

async function refreshLiveOpenOrders() {
  return withButtonLoading(ids.liveOpenOrdersRefresh, async () => {
    appendLiveLog({ level: "info", title: "刷新挂单", message: "请求已发送，等待官方 open orders 返回" });
    try {
      const res = await fetch("/api/live-open-orders");
      if (!res.ok) throw new Error(`live open orders HTTP ${res.status}`);
      const payload = await res.json();
      latestStatus = payload.snapshot || latestStatus;
      renderAll(latestStatus, { force: true, forceChart: true, forceOrder: true });
      const openOrders = payload.live_open_orders?.open_orders || {};
      const errors = openOrders.errors || [];
      if (errors.length && !openOrders.skipped) throw new Error(errors[0]);
      appendLiveLog({
        level: openOrders.skipped ? "warn" : "pass",
        title: "刷新挂单",
        message: openOrders.skipped ? "凭证未就绪，已跳过官方挂单读取" : `官方 open orders: ${fmtNumberCell(openOrders.count, 0)}`,
      });
    } catch (error) {
      appendLiveLog({ level: "error", title: "刷新挂单", message: error.message || String(error) });
      throw error;
    }
  });
}

async function loadMoreRecentTrades() {
  ids.loadMoreRecent.disabled = true;
  try {
    await loadRecentTradesPage(false);
  } finally {
    ids.loadMoreRecent.disabled = false;
  }
}

function renderAll(data = latestStatus, options = {}) {
  if (data) pendingRenderData = data;
  pendingRenderOptions = {
    force: Boolean(pendingRenderOptions.force || options.force),
    forceChart: Boolean(pendingRenderOptions.forceChart || options.forceChart),
    forceOpen: Boolean(pendingRenderOptions.forceOpen || options.forceOpen),
    forceOrder: Boolean(pendingRenderOptions.forceOrder || options.forceOrder),
  };
  if (!pageVisible && !pendingRenderOptions.force) return;
  if (renderQueued) return;
  renderQueued = true;
  window.requestAnimationFrame(() => {
    renderQueued = false;
    const nextData = pendingRenderData || latestStatus;
    const nextOptions = pendingRenderOptions;
    pendingRenderData = null;
    pendingRenderOptions = {};
    renderAllNow(nextData, nextOptions);
  });
}

function renderAllNow(data = latestStatus, options = {}) {
  if (!data) return;
  renderRuntime(data.runtime);
  renderLivePanel(data);
  renderLlmTerminalLogs(data);
  renderAccountScope(data);
  renderMetrics(selectedAccountMetrics(data));
  renderMarket(data.runtime);
  renderStrategyExperiments(data.runtime);
  if (strategyExperimentViewActive() && !strategyTablesLoading) {
    loadStrategyExperimentTables(false).catch(showError);
  }
  const openRows = scopedOpenRows(openDataScope, data);
  renderOpenTrades(openRows, openDataScope, { force: Boolean(options.force || options.forceOpen) });
  const useStatusOrders = isMainScope(orderDataScope) && orderStatusFilter === "all" && !orderRows.length;
  const visibleOrderRows = isExperimentAggregateScope(orderDataScope)
    ? strategyTables?.recent_orders || []
    : useStatusOrders ? data.recent_orders || [] : orderRows;
  renderRecentOrders(visibleOrderRows, {
    force: Boolean(options.force || options.forceOrder),
    scope: orderDataScope,
    meta: isExperimentAggregateScope(orderDataScope)
      ? strategyTables?.recent_orders_meta || {}
      : useStatusOrders ? data.recent_orders_meta || orderMeta : orderMeta,
  });
  const useStatusRecentTrades = isMainScope(recentDataScope) && !recentFilterActive() && !recentRows.length;
  const visibleRecentRows = isExperimentAggregateScope(recentDataScope)
    ? strategyTables?.recent_trades || []
    : useStatusRecentTrades ? data.recent_trades || [] : recentRows;
  renderRecentTrades(visibleRecentRows || [], {
    scope: recentDataScope,
    meta: isExperimentAggregateScope(recentDataScope)
      ? strategyTables?.recent_trades_meta || {}
      : useStatusRecentTrades ? data.recent_trades_meta || recentMeta : recentMeta,
    summary: isExperimentAggregateScope(recentDataScope)
      ? strategyTables?.recent_trades_summary || {}
      : useStatusRecentTrades ? data.recent_trades_summary || recentSummary : recentSummary,
  });
  const now = Date.now();
  if (options.forceChart || now - lastChartRenderMs >= CHART_RENDER_INTERVAL_MS) {
    const currentPoint = selectedAccountCurrentPoint(data);
    currentChartRows = buildChartRows(currentPoint ? [currentPoint] : []);
    drawChart(currentChartRows);
    lastChartRenderMs = now;
  }
  if (activeAppPage === "analysis") queueRealtimeAnalysisRender();
}

function scopedOpenRows(scope, data = latestStatus) {
  if (isLiveScope(scope)) {
    return data?.runtime?.live_trading?.open_trades || [];
  }
  if (isExperimentAggregateScope(scope)) {
    return strategyTables?.open_trades || [];
  }
  if (isExperimentVariantScope(scope)) {
    const variantId = parseAccountScope(scope).variantId;
    return (strategyTables?.open_trades || []).filter((row) => row.variant_id === variantId);
  }
  return data?.open_trades || [];
}

function applyMarket(market) {
  if (!market?.slug) return;
  const previous = activeMarket;
  const sameMarket = previous?.slug === market.slug;
  const tokenChanged = previous?.up_token !== market.up_token || previous?.down_token !== market.down_token;
  const incomingTarget = marketTargetPrice(market);
  const previousTarget = marketTargetPrice(previous);
  activeMarket = sameMarket
    ? { ...previous, ...market, target_price: incomingTarget ?? previousTarget ?? market.target_price }
    : market;

  const officialTarget = marketTargetPrice(activeMarket);
  if (officialTarget != null) {
    priceState = {
      ...priceState,
      target_price: officialTarget,
      target_price_source: "market.target_price",
      target_price_fallback: false,
      target_price_updated_ms: Date.now(),
    };
  } else if (!sameMarket) {
    priceState = {
      ...priceState,
      target_price: null,
      target_price_source: null,
      target_price_fallback: false,
      target_price_updated_ms: null,
    };
  }

  if (!sameMarket || tokenChanged) {
    quotes = {};
    resetRealtimeAnalysisForMarket(activeMarket);
    connectMarketSocket();
  }
}

function connectMarketSocket() {
  if (!activeMarket?.up_token || !activeMarket?.down_token) return;
  if (marketSocket) marketSocket.close();
  if (marketPing) clearInterval(marketPing);
  marketWsStatus = "connecting";
  const socket = new WebSocket(MARKET_WS);
  marketSocket = socket;
  socket.onopen = () => {
    if (socket !== marketSocket) return;
    marketWsStatus = "connected";
    socket.send(JSON.stringify({
      type: "market",
      assets_ids: [activeMarket.up_token, activeMarket.down_token],
      custom_feature_enabled: true,
    }));
    socket.send("PING");
    marketPing = setInterval(() => {
      if (socket === marketSocket && socket.readyState === WebSocket.OPEN) socket.send("PING");
    }, MARKET_PING_MS);
    postSnapshotSoon(true);
  };
  socket.onmessage = (event) => {
    const message = parseMessage(event.data);
    if (!message) return;
    if (message.event_type === "book") {
      const side = tokenSide(message.asset_id);
      if (side) updateQuote(side, {
        token_id: message.asset_id,
        best_bid: bestBid(message.bids)?.price,
        best_ask: bestAsk(message.asks)?.price,
        bid_size: bestBid(message.bids)?.size,
        ask_size: bestAsk(message.asks)?.size,
        bids: normalizeBookLevels(message.bids, true),
        asks: normalizeBookLevels(message.asks, false),
        source: "market-ws-book",
      });
      recordRealtimeMarketEvent(message, { side });
    } else if (message.event_type === "best_bid_ask") {
      const side = tokenSide(message.asset_id);
      if (side) updateQuote(side, {
        token_id: message.asset_id,
        best_bid: message.best_bid,
        best_ask: message.best_ask,
        source: "market-ws-best",
      });
      recordRealtimeMarketEvent(message, { side, price: message.best_ask ?? message.best_bid });
    } else if (message.event_type === "price_change") {
      for (const change of message.price_changes || []) {
        const side = tokenSide(change.asset_id);
        if (side) updateQuote(side, {
          token_id: change.asset_id,
          best_bid: change.best_bid,
          best_ask: change.best_ask,
          source: "market-ws-price-change",
        });
        recordRealtimeMarketEvent(
          { ...message, asset_id: change.asset_id, event_type: "price_change" },
          { side, price: change.best_ask ?? change.best_bid },
        );
      }
    } else if (message.event_type === "last_trade_price") {
      recordRealtimeMarketEvent(message, { side: tokenSide(message.asset_id), price: message.price, size: message.size });
    } else if (message.event_type === "market_resolved") {
      recordRealtimeMarketEvent(message);
      postSnapshotSoon(true);
    }
    renderAll();
    postSnapshotSoon();
  };
  socket.onclose = () => {
    if (socket !== marketSocket) return;
    marketWsStatus = "closed";
    if (marketPing) clearInterval(marketPing);
    setTimeout(connectMarketSocket, 1500);
  };
  socket.onerror = () => {
    if (socket !== marketSocket) return;
    marketWsStatus = "error";
  };
}

function connectPriceSocket() {
  if (priceSocket && priceSocket.readyState <= 1) return;
  if (pricePing) clearInterval(pricePing);
  priceWsStatus = "connecting";
  const socket = new WebSocket(RTDS_WS);
  priceSocket = socket;
  socket.onopen = () => {
    if (socket !== priceSocket) return;
    priceWsStatus = "connected";
    untaggedPriceBatchCount = 0;
    socket.send(JSON.stringify({
      action: "subscribe",
      subscriptions: [
        { topic: "crypto_prices_chainlink", type: "*", filters: "{\"symbol\":\"btc/usd\"}" },
        { topic: "crypto_prices", type: "update", filters: "{\"symbol\":\"btcusdt\"}" },
      ],
    }));
    pricePing = setInterval(() => {
      if (socket === priceSocket && socket.readyState === WebSocket.OPEN) socket.send("PING");
    }, RTDS_PING_MS);
  };
  socket.onmessage = (event) => {
    const message = parseMessage(event.data);
    if (!message) return;
    processPriceMessage(message);
    renderAll();
    postSnapshotSoon();
  };
  socket.onclose = () => {
    if (socket !== priceSocket) return;
    priceWsStatus = "closed";
    if (pricePing) clearInterval(pricePing);
    setTimeout(connectPriceSocket, 1500);
  };
  socket.onerror = () => {
    if (socket !== priceSocket) return;
    priceWsStatus = "error";
  };
}

function connectOkxSocket() {
  if (okxSocket && okxSocket.readyState <= 1) return;
  if (okxPing) clearInterval(okxPing);
  okxWsStatus = "connecting";
  const socket = new WebSocket(OKX_WS);
  okxSocket = socket;
  socket.onopen = () => {
    if (socket !== okxSocket) return;
    okxWsStatus = "connected";
    socket.send(JSON.stringify({
      op: "subscribe",
      args: [{ channel: "tickers", instId: "BTC-USDT" }],
    }));
    okxPing = setInterval(() => {
      if (socket === okxSocket && socket.readyState === WebSocket.OPEN) socket.send("ping");
    }, OKX_PING_MS);
  };
  socket.onmessage = (event) => {
    if (event.data === "pong") return;
    const message = parseMessage(event.data);
    if (!message) return;
    processOkxMessage(message);
    renderAll();
    postSnapshotSoon();
  };
  socket.onclose = () => {
    if (socket !== okxSocket) return;
    okxWsStatus = "closed";
    if (okxPing) clearInterval(okxPing);
    setTimeout(connectOkxSocket, 1500);
  };
  socket.onerror = () => {
    if (socket !== okxSocket) return;
    okxWsStatus = "error";
  };
}

function connectBinanceMarketSocket() {
  if (binanceMarketSocket && binanceMarketSocket.readyState <= 1) return;
  binanceMarketWsStatus = "connecting";
  const socket = new WebSocket(BINANCE_MARKET_WS);
  binanceMarketSocket = socket;
  socket.onopen = () => {
    if (socket !== binanceMarketSocket) return;
    binanceMarketWsStatus = "connected";
  };
  socket.onmessage = (event) => {
    const message = parseMessage(event.data);
    if (!message) return;
    processBinanceMarketMessage(message);
    renderAll();
    postSnapshotSoon();
  };
  socket.onclose = () => {
    if (socket !== binanceMarketSocket) return;
    binanceMarketWsStatus = "closed";
    setTimeout(connectBinanceMarketSocket, 1500);
  };
  socket.onerror = () => {
    if (socket !== binanceMarketSocket) return;
    binanceMarketWsStatus = "error";
  };
}

function processPriceMessage(message) {
  const payload = message.payload || {};
  const topic = inferPriceTopic(message);
  const now = Date.now();
  const rows = Array.isArray(payload.data) ? payload.data : [payload];
  for (const row of rows) {
    const value = toNumber(row?.value);
    if (!row || value == null) continue;
    const symbol = row.symbol || payload.symbol;
    const ts = normalizeMs(row.timestamp || payload.timestamp || message.timestamp || now);
    if (topic === "crypto_prices_chainlink" || symbol === "btc/usd") {
      priceState.chainlink = value;
      priceState.chainlink_updated_ms = ts;
      priceState.source = "polymarket-rtds-chainlink";
      recordRealtimePriceEvent("chainlink", value, ts);
      if (!marketTargetPrice(activeMarket) && !priceState.target_price && activeMarket?.start_ts && Math.abs(ts - activeMarket.start_ts * 1000) <= 20_000) {
        priceState.target_price = value;
        priceState.target_price_source = "rtds-chainlink-fallback";
        priceState.target_price_fallback = true;
        priceState.target_price_updated_ms = ts;
      }
    } else if (topic === "crypto_prices" || symbol === "btcusdt") {
      priceState.binance = value;
      priceState.binance_updated_ms = now;
      if (!priceState.source) priceState.source = "polymarket-rtds-binance";
      recordRealtimePriceEvent("polymarket-binance", value, now);
    }
  }
}

function processOkxMessage(message) {
  const rows = Array.isArray(message.data) ? message.data : [];
  for (const row of rows) {
    const value = toNumber(row?.last);
    if (value == null) continue;
    priceState.okx = value;
    priceState.okx_updated_ms = normalizeMs(row.ts || Date.now());
    priceState.okx_source = "okx-spot-ws";
    recordRealtimePriceEvent("okx", value, priceState.okx_updated_ms);
  }
}

function processBinanceMarketMessage(message) {
  const value = toNumber(message.c || message.lastPrice);
  if (value == null) return;
  priceState.binance_market = value;
  priceState.binance_market_updated_ms = normalizeMs(message.E || Date.now());
  priceState.binance_market_source = "binance-spot-ws";
  recordRealtimePriceEvent("binance", value, priceState.binance_market_updated_ms);
}

function inferPriceTopic(message) {
  if (message.topic) return message.topic;
  const rows = Array.isArray(message.payload?.data) ? message.payload.data : [];
  if (!rows.length) return "";
  untaggedPriceBatchCount += 1;
  return untaggedPriceBatchCount === 1 ? "crypto_prices_chainlink" : "crypto_prices";
}

function updateQuote(side, row) {
  quotes[side] = {
    ...quotes[side],
    ...row,
    outcome: side,
    best_bid: toNumber(row.best_bid),
    best_ask: toNumber(row.best_ask),
    bid_size: toNumber(row.bid_size),
    ask_size: toNumber(row.ask_size),
    updated_at_ms: Date.now(),
  };
}

function tokenSide(assetId) {
  if (!activeMarket) return null;
  if (String(assetId) === String(activeMarket.up_token)) return "Up";
  if (String(assetId) === String(activeMarket.down_token)) return "Down";
  return null;
}

function bestBid(levels) {
  return (levels || []).slice().sort((a, b) => Number(b.price) - Number(a.price))[0] || null;
}

function bestAsk(levels) {
  return (levels || []).slice().sort((a, b) => Number(a.price) - Number(b.price))[0] || null;
}

function normalizeBookLevels(levels, reverse) {
  return (levels || [])
    .map((level) => ({
      price: toNumber(level.price),
      size: toNumber(level.size),
    }))
    .filter((level) => level.price > 0 && level.price < 1 && level.size > 0)
    .sort((a, b) => reverse ? b.price - a.price : a.price - b.price)
    .slice(0, 50);
}

function parseMessage(data) {
  if (data === "PONG" || data === "PING") return null;
  try {
    const parsed = JSON.parse(data);
    return Array.isArray(parsed) ? parsed[0] : parsed;
  } catch (_) {
    return null;
  }
}

function postSnapshotSoon(force = false) {
  const now = Date.now();
  if (!force && now - lastSnapshotPostMs < SNAPSHOT_POST_MS) return;
  if (!claimSnapshotLeadership(now)) return;
  lastSnapshotPostMs = now;
  postSnapshot().catch(showError);
}

function claimSnapshotLeadership(now = Date.now()) {
  try {
    const raw = localStorage.getItem(SNAPSHOT_LEADER_KEY);
    const current = raw ? JSON.parse(raw) : null;
    if (current?.id && current.id !== TAB_ID && Number(current.expires_at || 0) > now) {
      return false;
    }
    localStorage.setItem(SNAPSHOT_LEADER_KEY, JSON.stringify({ id: TAB_ID, expires_at: now + SNAPSHOT_LEADER_TTL_MS }));
    return true;
  } catch (_) {
    return true;
  }
}

function releaseSnapshotLeadership() {
  try {
    const raw = localStorage.getItem(SNAPSHOT_LEADER_KEY);
    const current = raw ? JSON.parse(raw) : null;
    if (current?.id === TAB_ID) localStorage.removeItem(SNAPSHOT_LEADER_KEY);
  } catch (_) {
    return;
  }
}

async function postSnapshot() {
  if (!activeMarket) return;
  if (snapshotInFlight) return;
  snapshotInFlight = true;
  const officialTarget = marketTargetPrice(activeMarket);
  const snapshotPrice = {
    ...priceState,
    target_price: officialTarget,
    target_price_source: officialTarget == null ? null : "market.target_price",
    target_price_fallback: false,
    realtime_probability: realtimeDirectionProbability(Date.now()),
    actor_probability: snapshotActorProbability(),
  };
  try {
    const res = await fetch("/api/live-snapshot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        market: activeMarket,
        target_price: officialTarget,
        target_price_source: officialTarget == null ? null : "market.target_price",
        price: snapshotPrice,
        quotes,
        market_ws_status: marketWsStatus,
        price_ws_status: priceWsStatus,
      }),
    });
    if (!res.ok) throw new Error(`snapshot HTTP ${res.status}`);
    await res.json();
  } finally {
    snapshotInFlight = false;
  }
}

function snapshotActorProbability() {
  const payload = actorAnalysis?.actor_analysis || actorAnalysis || {};
  const probability = payload.probability;
  if (!probability || typeof probability !== "object" || !Object.keys(probability).length) return null;
  return {
    ...probability,
    checked_at: payload.checked_at || null,
    status: payload.status || null,
    cached: Boolean(payload.cached),
  };
}

function toNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizeMs(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return Date.now();
  return parsed < 10_000_000_000 ? parsed * 1000 : parsed;
}

function showError(error) {
  ids.runtime.textContent = "异常";
  ids.runtime.classList.add("error");
  ids.lastTick.textContent = error.message;
}

async function refreshMarketBoundary() {
  if (!pageVisible) return;
  if (activeMarket && Date.now() / 1000 < activeMarket.end_ts - 1) return;
  await loadStatus();
}

function handleVisibilityChange() {
  pageVisible = document.visibilityState !== "hidden";
  if (!pageVisible) return;
  connectStatusStream();
  if (foregroundRefreshTimer) clearTimeout(foregroundRefreshTimer);
  renderAll(latestStatus, { force: true, forceChart: true });
  loadEquityCurve(false).catch(showError);
  if (activeAppPage === "analysis") {
    loadActorAnalysis({ force: false }).catch(renderActorAnalysisError);
    loadLlmReview({ force: false }).catch(renderLlmReviewError);
  }
  foregroundRefreshTimer = setTimeout(() => {
    loadStatus().catch(showError);
  }, 150);
}

function handleChartMove(event) {
  const rect = ids.chart.getBoundingClientRect();
  chartHoverX = Math.max(0, Math.min(rect.width, event.clientX - rect.left));
  queueChartHoverDraw();
}

function handleChartLeave() {
  chartHoverX = null;
  ids.chartTooltip.hidden = true;
  queueChartHoverDraw();
}

function handleAccountScopeChange() {
  accountScope = ids.accountScopeSelect?.value || "main";
  equityCurveRows = [];
  equityCurveMeta = {};
  lastEquityCurveFetchMs = 0;
  chartHoverX = null;
  if (ids.chartTooltip) ids.chartTooltip.hidden = true;
  renderAll(latestStatus, { force: true, forceChart: true });
  loadEquityCurve(true).catch(showError);
}

function queueChartHoverDraw() {
  if (chartHoverQueued) return;
  chartHoverQueued = true;
  requestAnimationFrame(() => {
    chartHoverQueued = false;
    drawChart(currentChartRows);
  });
}

for (const navItem of ids.navItems || []) {
  navItem.addEventListener("click", () => setActiveAppPage(navItem.dataset.navPage));
}
bindLiveSettingsDirtyTracking();
window.addEventListener("popstate", () => setActiveAppPage(locationAppPage(), { syncHash: false }));
window.addEventListener("hashchange", () => setActiveAppPage(locationAppPage(), { syncHash: false }));
setActiveAppPage(locationAppPage(), { syncHash: false });
applyAnalysisRealtimeVisibility();

ids.tickButton.addEventListener("click", () => runManualTick().catch(showError));
ids.paperPauseToggle?.addEventListener("click", () => togglePaperPause().catch(showError));
ids.liveEnabled?.addEventListener("change", () => saveLiveSettings({ enabled: ids.liveEnabled.checked }).catch(showError));
ids.liveSettingsToggle?.addEventListener("click", (event) => {
  event.stopPropagation();
  toggleLiveSettingsPanel();
});
ids.liveSettingsPanel?.addEventListener("click", (event) => event.stopPropagation());
ids.liveSaveSettings?.addEventListener("click", () => saveLiveSettings().catch(showError));
ids.liveReloadCredentials?.addEventListener("click", () => reloadLiveCredentials().catch(showError));
ids.livePreflight?.addEventListener("click", () => runLivePreflight().catch(showError));
ids.liveDoctor?.addEventListener("click", () => runLiveDoctor().catch(showError));
ids.liveOnce?.addEventListener("click", () => runLiveOnce().catch(showError));
ids.liveOpenOrdersRefresh?.addEventListener("click", () => refreshLiveOpenOrders().catch(showError));
ids.liveEmergencyStop?.addEventListener("click", () => liveEmergencyStop().catch(showError));
ids.analysisRefresh?.addEventListener("click", () => loadActorAnalysis({ force: true }).catch(renderActorAnalysisError));
ids.analysisLlmReviewRefresh?.addEventListener("click", () => loadLlmReview({ force: true }).catch(renderLlmReviewError));
ids.analysisRealtimeToggle?.addEventListener("change", () => setAnalysisRealtimeVisible(ids.analysisRealtimeToggle.checked));
document.addEventListener("click", () => {
  if (liveSettingsOpen) setLiveSettingsOpen(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && liveSettingsOpen) setLiveSettingsOpen(false);
});
ids.accountScopeSelect.addEventListener("change", handleAccountScopeChange);
ids.strategyExperiments.addEventListener("click", (event) => {
  const button = event.target.closest("[data-experiment-id]");
  if (!button) return;
  event.stopPropagation();
  toggleExperimentDetail(String(button.dataset.experimentId || "")).catch(showError);
});
ids.recentOrders.addEventListener("click", (event) => {
  const cancelButton = event.target.closest("[data-cancel-order-id]");
  if (cancelButton) {
    event.stopPropagation();
    cancelButton.disabled = true;
    cancelOrder(Number(cancelButton.dataset.cancelOrderId)).catch((error) => {
      cancelButton.disabled = false;
      showError(error);
    });
    return;
  }
  const row = event.target.closest("tr[data-order-id]");
  if (!row) return;
  toggleOrderFills(Number(row.dataset.orderId)).catch(showError);
});
ids.openTrades.addEventListener("click", (event) => {
  const sellButton = event.target.closest("[data-live-sell-trade-id]");
  if (!sellButton) return;
  event.stopPropagation();
  const tradeId = Number(sellButton.dataset.liveSellTradeId);
  if (!confirmLiveSell(tradeId)) return;
  sellButton.disabled = true;
  sellLiveTrade(tradeId).catch((error) => {
    sellButton.disabled = false;
    showError(error);
  });
});
ids.loadMoreOrders.addEventListener("click", () => loadMoreOrders().catch(showError));
ids.orderStatusFilter.addEventListener("change", handleOrderStatusFilterChange);
ids.openDataScope.addEventListener("change", () => handleDataScopeChange("open", ids.openDataScope.value));
ids.orderDataScope.addEventListener("change", () => handleDataScopeChange("order", ids.orderDataScope.value));
ids.recentDataScope.addEventListener("change", () => handleDataScopeChange("recent", ids.recentDataScope.value));
bindCancelOrdersButton(ids.cancelCurrentOrders, "current_market");
bindCancelOrdersButton(ids.cancelAllOrders, "all");
ids.loadMoreRecent.addEventListener("click", () => loadMoreRecentTrades().catch(showError));
ids.applyRecentFilter.addEventListener("click", () => applyRecentTradeFilter().catch(showError));
ids.resetRecentFilter.addEventListener("click", () => resetRecentTradeFilter().catch(showError));
ids.chart.addEventListener("mousemove", handleChartMove);
ids.chart.addEventListener("mouseleave", handleChartLeave);
bindProtectedTableInteraction("open");
bindProtectedTableInteraction("order");
document.addEventListener("selectionchange", scheduleProtectedTableFlush);
document.addEventListener("focusout", scheduleProtectedTableFlush);
document.addEventListener("mouseup", scheduleProtectedTableFlush);
document.addEventListener("pointerup", scheduleProtectedTableFlush);

initFieldOptions();
	setRecentLoading(true);
	loadStatus().then(() => {
	  connectStatusStream();
	  connectPriceSocket();
	  connectOkxSocket();
	  connectBinanceMarketSocket();
	}).catch(showError);
	setInterval(() => {
	  if (!pageVisible) return;
	  if (statusStreamConnected && Date.now() - lastStatusStreamAt < STATUS_STREAM_STALE_MS) return;
	  loadStatus().catch(showError);
	}, STATUS_POLL_MS);
setInterval(() => refreshMarketBoundary().catch(showError), 1_000);
setInterval(() => {
  if (!pageVisible || activeAppPage !== "analysis") return;
  loadActorAnalysis({ force: false }).catch(renderActorAnalysisError);
}, ACTOR_ANALYSIS_REFRESH_MS);
setInterval(() => {
  if (!pageVisible || activeAppPage !== "analysis") return;
  loadLlmReview({ force: false }).catch(renderLlmReviewError);
}, LLM_REVIEW_REFRESH_MS);
document.addEventListener("visibilitychange", handleVisibilityChange);
window.addEventListener("resize", () => renderAll(latestStatus, { force: true, forceChart: true }));
window.addEventListener("beforeunload", releaseSnapshotLeadership);
