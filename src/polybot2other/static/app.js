const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 });

const ids = {
  runtime: document.getElementById("runtime-pill"),
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
  tickButton: document.getElementById("tick-button"),
};

const MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
const RTDS_WS = "wss://ws-live-data.polymarket.com";
const MARKET_PING_MS = 10_000;
const RTDS_PING_MS = 5_000;
const SNAPSHOT_POST_MS = 1_000;
const STATUS_POLL_MS = 2_000;
const RECENT_PAGE_SIZE = 100;
const ORDER_PAGE_SIZE = 20;
const CHART_RENDER_INTERVAL_MS = 5_000;
const EQUITY_CURVE_DAYS = 90;
const EQUITY_CURVE_MAX_POINTS = 1200;
const EQUITY_CURVE_REFRESH_MS = 30_000;
const METRIC_ANIMATION_MS = 360;
const RECENT_SKELETON_ROWS = 8;
const SNAPSHOT_LEADER_KEY = "polybot2other:snapshot-leader";
const SNAPSHOT_LEADER_TTL_MS = 2_500;
const TAB_ID = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
const FIELD_STORAGE_KEYS = {
  open: "polybot2other:open-trade-fields",
  order: "polybot2other:order-fields",
  recent: "polybot2other:recent-trade-fields",
};
const ORDER_STATUS_LABELS = {
  RESTING: "挂单中",
  PARTIAL_RESTING: "部分成交挂单",
  FILLED: "完全成交",
  PARTIAL: "部分成交",
  CANCELED: "已取消",
  EXPIRED: "已过期",
  REJECTED: "已拒绝",
};

let activeMarket = null;
let marketSocket = null;
let priceSocket = null;
let marketPing = null;
let pricePing = null;
let marketWsStatus = "waiting";
let priceWsStatus = "waiting";
let lastSnapshotPostMs = 0;
let snapshotInFlight = false;
let latestStatus = null;
let recentRows = [];
let recentMeta = { limit: RECENT_PAGE_SIZE, offset: 0, loaded: 0, total: 0, has_more: false, start_at: null, end_at: null };
let recentSummary = null;
let recentFilters = { start_at: null, end_at: null };
let recentLoading = true;
let openDataScope = "main";
let orderDataScope = "main";
let recentDataScope = "main";
let strategyTables = null;
let strategyTablesLoading = false;
let lastStrategyTablesFetchMs = 0;
let strategyOrderLimit = ORDER_PAGE_SIZE;
let strategyTradeLimit = RECENT_PAGE_SIZE;
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
let lastRecentRenderKey = "";
let lastOrderRenderKey = "";
let lastExperimentRenderKey = "";
let pendingOrderRender = false;
let foregroundRefreshTimer = null;
let equityCurveRows = [];
let lastEquityCurveFetchMs = 0;
let equityCurveInFlight = false;
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
  target_price: null,
  target_price_source: null,
  target_price_fallback: false,
  target_price_updated_ms: null,
  source: null,
};

const openTradeFields = [
  { key: "strategy_type", label: "策略", render: (row) => safe(row.strategy_type) },
  { key: "side", label: "方向", render: (row) => `<span class="${sideClass(row.side)}">${safe(row.side)}</span>` },
  { key: "stake", label: "本金", render: (row) => fmtMoneyCell(row.stake) },
  { key: "entry_price", label: "买入价", render: (row) => fmtNumberCell(row.entry_price, 4) },
  { key: "entry_probability_pct", label: "买入概率", render: (row) => fmtPctCell(row.entry_probability_pct) },
  { key: "shares", label: "份额", render: (row) => fmtNumberCell(row.shares, 6) },
  { key: "current_bid", label: "当前买一", render: (row) => fmtNumberCell(row.current_bid, 4) },
  { key: "current_ask", label: "当前卖一", render: (row) => fmtNumberCell(row.current_ask, 4) },
  { key: "exit_value", label: "可退出回款", render: (row) => fmtMoneyCell(row.exit_value) },
  { key: "unrealized_pnl", label: "未实现盈亏", render: (row) => fmtSignedMoneyCell(row.unrealized_pnl) },
  { key: "unrealized_roi_pct", label: "未实现ROI", render: (row) => fmtSignedPctCell(row.unrealized_roi_pct) },
  { key: "max_payout", label: "最大回款", render: (row) => fmtMoneyCell(row.max_payout) },
  { key: "max_profit", label: "最大盈利", render: (row) => fmtSignedMoneyCell(row.max_profit) },
  { key: "max_loss", label: "最大亏损", render: (row) => fmtMoneyCell(row.max_loss) },
  { key: "target_price", label: "目标价", render: (row) => fmtMoneyCell(row.target_price) },
  { key: "current_price", label: "当前价", render: (row) => fmtMoneyCell(row.current_price) },
  { key: "current_distance_bps", label: "距离bps", render: (row) => fmtSignedBpsCell(row.current_distance_bps) },
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
  { key: "status", label: "状态", render: (row) => safe(row.status) },
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

const defaultOpenFieldKeys = [
  "strategy_type", "side", "stake", "entry_price", "shares", "current_bid", "current_ask",
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
  return ["combo", ...base.filter((key) => !["detail_toggle", "cancel_action"].includes(key))];
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

function renderMetrics(metrics) {
  setMetric(ids.totalEquity, metrics.total_equity, money, null);
  setMetric(ids.totalPnl, metrics.total_pnl, signedMoney, cls);
  setMetric(ids.unrealizedPnl, metrics.unrealized_pnl, signedMoney, cls);
  setMetric(ids.cashBalance, metrics.cash_balance, money, null);
  setMetric(ids.reservedCash, metrics.reserved_cash, money, null);
  setMetric(ids.openRisk, metrics.open_risk, money, null);
  setMetric(ids.winRate, metrics.win_rate, percentText, null);
  setMetric(ids.maxDrawdown, metrics.max_drawdown, money, null);
}

function renderRuntime(runtime) {
  const hasError = Boolean(runtime.last_error);
  const stalePrice = priceState.chainlink_updated_ms ? Date.now() - priceState.chainlink_updated_ms > 5_000 : true;
  ids.runtime.textContent = hasError ? "异常" : stalePrice ? "等待实时价" : "实时运行";
  ids.runtime.classList.toggle("error", hasError || stalePrice);
  ids.lastTick.textContent = runtime.last_error || `market ${marketWsStatus} · price ${priceWsStatus}`;
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

function renderOpenTrades(rows, scope = openDataScope) {
  if (scope === "experiment" && strategyTablesLoading && !strategyTables) {
    ids.openCount.textContent = "加载中";
    renderTradeTable("open", [], experimentOpenFields, ids.openTradesHead, ids.openTrades, experimentFieldKeys("open"));
    return;
  }
  ids.openCount.textContent = scope === "experiment" ? `${rows.length} / 8组` : rows.length;
  const fields = scope === "experiment" ? experimentOpenFields : openTradeFields;
  const selected = scope === "experiment" ? experimentFieldKeys("open") : selectedFields.open;
  renderTradeTable("open", rows, fields, ids.openTradesHead, ids.openTrades, selected);
}

function renderRecentOrders(rows, options = {}) {
  const scope = options.scope || orderDataScope;
  const meta = options.meta || (scope === "experiment" ? strategyTables?.recent_orders_meta || {} : orderMeta);
  const total = Number(meta.total || rows.length || 0);
  const loaded = rows.length;
  ids.orderCount.textContent = total > loaded ? `${loaded} / ${total}` : `${loaded}`;
  ids.orderPageInfo.textContent = `${scope === "experiment" ? "策略实验" : "主账户"} · ${total > loaded ? `最近 ${loaded} / ${total} 条` : `最近 ${loaded} 条`}`;
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
  const fields = scope === "experiment" ? experimentOrderFields : recentOrderFields;
  const selected = scope === "experiment" ? experimentFieldKeys("order") : selectedFields.order;
  renderTradeTable("order", rows, fields, ids.recentOrdersHead, ids.recentOrders, selected);
  if (tableWrap) {
    tableWrap.scrollTop = Math.min(scrollTop, Math.max(0, tableWrap.scrollHeight - tableWrap.clientHeight));
    tableWrap.scrollLeft = Math.min(scrollLeft, Math.max(0, tableWrap.scrollWidth - tableWrap.clientWidth));
  }
}

function renderRecentTrades(rows, options = {}) {
  const scope = options.scope || recentDataScope;
  const meta = options.meta || (scope === "experiment" ? strategyTables?.recent_trades_meta || {} : recentMeta);
  const summary = options.summary || (scope === "experiment" ? strategyTables?.recent_trades_summary || {} : recentSummary);
  if (recentLoading) {
    renderRecentSkeleton();
    return;
  }
  const total = Number(meta.total || rows.length || 0);
  const loaded = rows.length;
  const filtered = recentFilterActive();
  ids.tradeCount.textContent = total > loaded ? `${loaded} / ${total}` : `${loaded}`;
  ids.recentPageInfo.textContent = total > loaded
    ? `${scope === "experiment" ? "策略实验" : "主账户"} · ${filtered ? "范围" : "最近"} ${loaded} / ${total} 条`
    : `${scope === "experiment" ? "策略实验" : "主账户"} · ${filtered ? "范围" : "最近"} ${loaded} 条`;
  ids.loadMoreRecent.hidden = !meta.has_more;
  ids.loadMoreRecent.disabled = false;
  renderRecentSummary(summary, scope);
  const renderKey = recentRenderKey(rows, scope, meta, summary);
  if (renderKey === lastRecentRenderKey) return;
  lastRecentRenderKey = renderKey;
  const fields = scope === "experiment" ? experimentRecentFields : recentTradeFields;
  const selected = scope === "experiment" ? experimentFieldKeys("recent") : selectedFields.recent;
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
    ["范围", scope === "experiment" ? "策略实验" : "主账户"],
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
  const idsKey = rows.map((row) => `${row.variant_id || "main"}:${row.id}:${row.status}:${row.settled_at || ""}:${row.pnl ?? ""}:${row.reason || ""}`).join(",");
  return [
    scope,
    idsKey,
    (scope === "experiment" ? experimentFieldKeys("recent") : selectedFields.recent).join(","),
    meta.loaded,
    meta.total,
    meta.has_more,
    meta.start_at || "",
    meta.end_at || "",
    summary?.total_pnl ?? "",
  ].join("|");
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
    (scope === "experiment" ? experimentFieldKeys("order") : selectedFields.order).join(","),
    meta.loaded,
    meta.total,
    meta.has_more,
    meta.status_filter,
    expandedOrderId || "",
    loadingOrderId || "",
    expandedFills.length,
  ].join("|");
}

function orderRoot() {
  return ids.recentOrders.closest(".panel") || ids.recentOrders;
}

function orderTableWrap() {
  return ids.recentOrders.closest(".order-table-wrap");
}

function nodeInside(root, node) {
  if (!root || !node) return false;
  const element = node.nodeType === 1 ? node : node.parentElement;
  return Boolean(element && root.contains(element));
}

function orderSelectionActive() {
  const selection = window.getSelection ? window.getSelection() : null;
  if (!selection || selection.isCollapsed || !selection.toString().trim()) return false;
  const root = orderRoot();
  return nodeInside(root, selection.anchorNode) || nodeInside(root, selection.focusNode);
}

function isOrderInteractionActive() {
  const root = orderRoot();
  const active = document.activeElement;
  if (active && active !== document.body && root.contains(active)) return true;
  return orderSelectionActive();
}

function currentOrderRows() {
  if (orderDataScope === "experiment") return strategyTables?.recent_orders || [];
  if (orderRows.length) return orderRows;
  return latestStatus?.recent_orders || [];
}

function flushPendingOrderRender() {
  if (!pendingOrderRender || isOrderInteractionActive()) return;
  renderRecentOrders(currentOrderRows(), { force: true });
}

function orderToggleText(row) {
  const count = Number(row.fill_count || 0);
  if (!count) return "-";
  if (loadingOrderId === row.id) return "加载中";
  return expandedOrderId === row.id ? "收起" : "展开";
}

function canCancelOrder(row) {
  if (row?.account_scope === "strategy_experiment") return false;
  return row?.status === "RESTING" || row?.status === "PARTIAL_RESTING";
}

function orderStatusText(status) {
  const raw = String(status || "").trim();
  if (!raw) return "-";
  const label = ORDER_STATUS_LABELS[raw];
  return label ? `${safe(raw)}(${safe(label)})` : safe(raw);
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
  if (orderDataScope === "experiment") {
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
    if (kind === "recent") lastRecentRenderKey = "";
    if (kind === "order") lastOrderRenderKey = "";
    renderAll(latestStatus, { forceOrder: kind === "order" });
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
  return toNumber(latestStatus?.metrics?.initial_balance)
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
  latestStatus = data;
  if (data.runtime.current_market) applyMarket(data.runtime.current_market);
  if (data.runtime.latest_quotes && !Object.keys(quotes).length) quotes = data.runtime.latest_quotes;
  if (data.runtime.latest_price && !priceState.chainlink && !priceState.binance) {
    priceState = { ...priceState, ...data.runtime.latest_price };
  }
  if (recentFilterActive()) {
    data.recent_trades = recentRows;
    data.recent_trades_meta = recentMeta;
    data.recent_trades_summary = recentSummary;
  } else {
    applyRecentPage(data.recent_trades, data.recent_trades_meta, data.recent_trades_summary);
  }
  if (orderStatusFilter === "all") {
    applyOrderPage(data.recent_orders, data.recent_orders_meta);
  } else {
    data.recent_orders = orderRows;
    data.recent_orders_meta = orderMeta;
  }
  renderAll(data, { forceOrder: manual });
  loadEquityCurve(false).catch(showError);
  loadOrders(orderStatusFilter !== "all").catch(showError);
  if (strategyExperimentViewActive()) {
    loadStrategyExperimentTables({ force: manual }).catch(showError);
  }
}

async function loadEquityCurve(force = false) {
  const now = Date.now();
  if (equityCurveInFlight) return;
  if (!force && now - lastEquityCurveFetchMs < EQUITY_CURVE_REFRESH_MS) return;
  equityCurveInFlight = true;
  try {
    const params = new URLSearchParams({
      days: String(EQUITY_CURVE_DAYS),
      max_points: String(EQUITY_CURVE_MAX_POINTS),
    });
    const res = await fetch(`/api/equity-curve?${params.toString()}`);
    if (!res.ok) throw new Error(`equity curve HTTP ${res.status}`);
    const payload = await res.json();
    const rows = Array.isArray(payload.equity_curve) ? payload.equity_curve : [];
    equityCurveRows = rows;
    lastEquityCurveFetchMs = Date.now();
    renderAll(latestStatus, { forceChart: true });
  } finally {
    equityCurveInFlight = false;
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
  return openDataScope === "experiment" || orderDataScope === "experiment" || recentDataScope === "experiment";
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
  if (orderDataScope === "experiment") {
    await loadStrategyExperimentTables({ force, orderLimit: strategyOrderLimit });
    return;
  }
  if (!force && orderRows.length && orderMeta.status_filter === orderStatusFilter) return;
  const params = new URLSearchParams({ limit: String(ORDER_PAGE_SIZE), offset: "0", status: orderStatusFilter });
  const res = await fetch(`/api/orders?${params.toString()}`);
  if (!res.ok) throw new Error(`orders HTTP ${res.status}`);
  const page = await res.json();
  applyOrderPage(page.recent_orders, page.recent_orders_meta || { status_filter: orderStatusFilter }, { replace: force });
  renderRecentOrders(orderRows, { force });
}

async function loadMoreOrders() {
  ids.loadMoreOrders.disabled = true;
  try {
    if (orderDataScope === "experiment") {
      strategyOrderLimit = Math.min(200, Number(strategyTables?.recent_orders_meta?.loaded || 0) + ORDER_PAGE_SIZE);
      await loadStrategyExperimentTables({ force: true, orderLimit: strategyOrderLimit });
      return;
    }
    const params = new URLSearchParams({
      limit: String(ORDER_PAGE_SIZE),
      offset: String(orderRows.length),
      status: orderStatusFilter,
    });
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
  await loadStatus(true);
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
  await loadStatus(true);
  await loadOrders(true);
  renderRecentOrders(orderRows, { force: true });
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
  if (orderDataScope === "experiment") {
    strategyOrderLimit = ORDER_PAGE_SIZE;
    loadStrategyExperimentTables({ force: true, orderLimit: strategyOrderLimit }).catch(showError);
  } else {
    loadOrders(true).catch(showError);
  }
}

function handleDataScopeChange(kind, value) {
  const normalized = value === "experiment" ? "experiment" : "main";
  if (kind === "open") openDataScope = normalized;
  if (kind === "order") {
    orderDataScope = normalized;
    expandedOrderId = null;
    loadingOrderId = null;
    lastOrderRenderKey = "";
  }
  if (kind === "recent") {
    recentDataScope = normalized;
    lastRecentRenderKey = "";
    if (normalized === "experiment") recentLoading = false;
    else if (!recentRows.length) recentLoading = true;
  }
  renderAll(latestStatus, { force: true, forceOrder: true });
  if (normalized === "experiment") {
    if (kind === "order") strategyOrderLimit = ORDER_PAGE_SIZE;
    if (kind === "recent") strategyTradeLimit = RECENT_PAGE_SIZE;
    loadStrategyExperimentTables({ force: true }).catch(showError);
  } else if (kind === "order") {
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

function datetimeLocalToSeconds(value) {
  if (!value) return null;
  const millis = new Date(value).getTime();
  if (!Number.isFinite(millis)) throw new Error("时间格式不正确");
  return Math.floor(millis / 1000);
}

function recentTradeQueryParams(offset) {
  const params = new URLSearchParams({
    limit: String(RECENT_PAGE_SIZE),
    offset: String(offset),
  });
  if (recentFilters.start_at !== null) params.set("start_at", String(recentFilters.start_at));
  if (recentFilters.end_at !== null) params.set("end_at", String(recentFilters.end_at));
  return params;
}

async function loadRecentTradesPage(replace = false) {
  if (recentDataScope === "experiment") {
    strategyTradeLimit = replace ? RECENT_PAGE_SIZE : Math.min(500, Number(strategyTables?.recent_trades_meta?.loaded || 0) + RECENT_PAGE_SIZE);
    await loadStrategyExperimentTables({ force: true, tradeLimit: strategyTradeLimit });
    return;
  }
  const offset = replace ? 0 : recentRows.length;
  const params = recentTradeQueryParams(offset);
  const res = await fetch(`/api/recent-trades?${params.toString()}`);
  if (!res.ok) throw new Error(`recent trades HTTP ${res.status}`);
  const page = await res.json();
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
  lastRecentRenderKey = "";
  renderRecentTrades(recentRows);
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
  renderMetrics(data.metrics);
  renderMarket(data.runtime);
  renderStrategyExperiments(data.runtime);
  if (strategyExperimentViewActive() && !strategyTablesLoading) {
    loadStrategyExperimentTables(false).catch(showError);
  }
  const openRows = openDataScope === "experiment" ? strategyTables?.open_trades || [] : data.open_trades || [];
  renderOpenTrades(openRows, openDataScope);
  const visibleOrderRows = orderDataScope === "experiment"
    ? strategyTables?.recent_orders || []
    : orderStatusFilter === "all" && !orderRows.length ? data.recent_orders || [] : orderRows;
  renderRecentOrders(visibleOrderRows, {
    force: Boolean(options.force || options.forceOrder),
    scope: orderDataScope,
    meta: orderDataScope === "experiment" ? strategyTables?.recent_orders_meta || {} : orderMeta,
  });
  const visibleRecentRows = recentDataScope === "experiment"
    ? strategyTables?.recent_trades || []
    : recentRows.length ? recentRows : data.recent_trades;
  renderRecentTrades(visibleRecentRows || [], {
    scope: recentDataScope,
    meta: recentDataScope === "experiment" ? strategyTables?.recent_trades_meta || {} : recentMeta,
    summary: recentDataScope === "experiment" ? strategyTables?.recent_trades_summary || {} : recentSummary,
  });
  const now = Date.now();
  if (options.forceChart || now - lastChartRenderMs >= CHART_RENDER_INTERVAL_MS) {
    currentChartRows = buildChartRows(data.equity_curve);
    drawChart(currentChartRows);
    lastChartRenderMs = now;
  }
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
    } else if (message.event_type === "best_bid_ask") {
      const side = tokenSide(message.asset_id);
      if (side) updateQuote(side, {
        token_id: message.asset_id,
        best_bid: message.best_bid,
        best_ask: message.best_ask,
        source: "market-ws-best",
      });
    } else if (message.event_type === "price_change") {
      for (const change of message.price_changes || []) {
        const side = tokenSide(change.asset_id);
        if (side) updateQuote(side, {
          token_id: change.asset_id,
          best_bid: change.best_bid,
          best_ask: change.best_ask,
          source: "market-ws-price-change",
        });
      }
    } else if (message.event_type === "market_resolved") {
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
      priceState.chainlink_updated_ms = now;
      priceState.source = "polymarket-rtds-chainlink";
      if (!marketTargetPrice(activeMarket) && !priceState.target_price && activeMarket?.start_ts && Math.abs(ts - activeMarket.start_ts * 1000) <= 20_000) {
        priceState.target_price = value;
        priceState.target_price_source = "rtds-chainlink-fallback";
        priceState.target_price_fallback = true;
        priceState.target_price_updated_ms = now;
      }
    } else if (topic === "crypto_prices" || symbol === "btcusdt") {
      priceState.binance = value;
      priceState.binance_updated_ms = now;
      if (!priceState.source) priceState.source = "polymarket-rtds-binance";
    }
  }
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
    latestStatus = await res.json();
    renderAll(latestStatus);
  } finally {
    snapshotInFlight = false;
  }
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
  await loadStatus(true);
}

function handleVisibilityChange() {
  pageVisible = document.visibilityState !== "hidden";
  if (!pageVisible) return;
  if (foregroundRefreshTimer) clearTimeout(foregroundRefreshTimer);
  renderAll(latestStatus, { force: true, forceChart: true });
  loadEquityCurve(false).catch(showError);
  foregroundRefreshTimer = setTimeout(() => {
    loadStatus(true).catch(showError);
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

function queueChartHoverDraw() {
  if (chartHoverQueued) return;
  chartHoverQueued = true;
  requestAnimationFrame(() => {
    chartHoverQueued = false;
    drawChart(currentChartRows);
  });
}

ids.tickButton.addEventListener("click", () => loadStatus(true).catch(showError));
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
document.addEventListener("selectionchange", () => window.setTimeout(flushPendingOrderRender, 120));
document.addEventListener("focusout", () => window.setTimeout(flushPendingOrderRender, 120));
document.addEventListener("mouseup", () => window.setTimeout(flushPendingOrderRender, 120));

initFieldOptions();
setRecentLoading(true);
loadStatus(true).then(() => {
  connectPriceSocket();
}).catch(showError);
setInterval(() => {
  if (!pageVisible) return;
  loadStatus().catch(showError);
}, STATUS_POLL_MS);
setInterval(() => refreshMarketBoundary().catch(showError), 1_000);
document.addEventListener("visibilitychange", handleVisibilityChange);
window.addEventListener("resize", () => renderAll(latestStatus, { force: true, forceChart: true }));
window.addEventListener("beforeunload", releaseSnapshotLeadership);
