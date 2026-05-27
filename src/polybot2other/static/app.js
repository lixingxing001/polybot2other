const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 });

const ids = {
  runtime: document.getElementById("runtime-pill"),
  totalEquity: document.getElementById("total-equity"),
  totalPnl: document.getElementById("total-pnl"),
  unrealizedPnl: document.getElementById("unrealized-pnl"),
  cashBalance: document.getElementById("cash-balance"),
  openRisk: document.getElementById("open-risk"),
  winRate: document.getElementById("win-rate"),
  maxDrawdown: document.getElementById("max-drawdown"),
  lastTick: document.getElementById("last-tick"),
  markets: document.getElementById("markets"),
  openTrades: document.getElementById("open-trades"),
  openTradesHead: document.getElementById("open-trades-head"),
  openFieldOptions: document.getElementById("open-field-options"),
  recentTrades: document.getElementById("recent-trades"),
  recentTradesHead: document.getElementById("recent-trades-head"),
  recentFieldOptions: document.getElementById("recent-field-options"),
  openCount: document.getElementById("open-count"),
  tradeCount: document.getElementById("trade-count"),
  recentPageInfo: document.getElementById("recent-page-info"),
  loadMoreRecent: document.getElementById("load-more-recent"),
  chart: document.getElementById("equity-chart"),
  chartTooltip: document.getElementById("equity-tooltip"),
  tickButton: document.getElementById("tick-button"),
  pairStrategyToggle: document.getElementById("pair-strategy-toggle"),
  pairStrategyStatus: document.getElementById("pair-strategy-status"),
};

const MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
const RTDS_WS = "wss://ws-live-data.polymarket.com";
const MARKET_PING_MS = 10_000;
const RTDS_PING_MS = 5_000;
const SNAPSHOT_POST_MS = 1_000;
const STATUS_POLL_MS = 2_000;
const RECENT_PAGE_SIZE = 100;
const CHART_RENDER_INTERVAL_MS = 5_000;
const EQUITY_CURVE_DAYS = 90;
const EQUITY_CURVE_MAX_POINTS = 1200;
const EQUITY_CURVE_REFRESH_MS = 30_000;
const METRIC_ANIMATION_MS = 360;
const SNAPSHOT_LEADER_KEY = "polybot2other:snapshot-leader";
const SNAPSHOT_LEADER_TTL_MS = 2_500;
const TAB_ID = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
const FIELD_STORAGE_KEYS = {
  open: "polybot2other:open-trade-fields",
  recent: "polybot2other:recent-trade-fields",
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
let recentMeta = { limit: RECENT_PAGE_SIZE, offset: 0, loaded: 0, total: 0, has_more: false };
let pageVisible = document.visibilityState !== "hidden";
let renderQueued = false;
let pendingRenderData = null;
let pendingRenderOptions = {};
let lastChartRenderMs = 0;
let lastRecentRenderKey = "";
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
  { key: "target_price", label: "目标价", render: (row) => fmtMoneyCell(row.target_price) },
  { key: "final_price", label: "最终价", render: (row) => fmtMoneyCell(row.final_price) },
  { key: "final_distance_bps", label: "最终距离bps", render: (row) => fmtSignedBpsCell(row.final_distance_bps) },
  { key: "confidence", label: "置信度", render: (row) => fmtPctCell(Number(row.confidence) * 100) },
  { key: "move_bps", label: "开仓距离bps", render: (row) => fmtSignedBpsCell(row.move_bps) },
  { key: "exit_note", label: "退出标记", render: (row) => safe(row.exit_note), cellClass: "reason-cell" },
  { key: "reason", label: "开仓原因", render: (row) => safe(row.reason), cellClass: "reason-cell" },
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
  "final_price", "final_distance_bps", "exit_note", "reason",
];

let selectedFields = {
  open: loadSelectedFields("open", openTradeFields, defaultOpenFieldKeys),
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

function renderPairStrategy(data) {
  const pair = data.runtime?.pair_strategy || {};
  const enabled = Boolean(pair.enabled);
  ids.pairStrategyToggle.checked = enabled;
  ids.pairStrategyStatus.textContent = enabled ? "已开启" : "关闭";
  ids.pairStrategyStatus.className = `status-text ${enabled ? "positive" : ""}`;
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

function renderOpenTrades(rows) {
  ids.openCount.textContent = rows.length;
  renderTradeTable("open", rows, openTradeFields, ids.openTradesHead, ids.openTrades);
}

function renderRecentTrades(rows) {
  const total = Number(recentMeta.total || rows.length || 0);
  const loaded = rows.length;
  ids.tradeCount.textContent = total > loaded ? `${loaded} / ${total}` : `${loaded}`;
  ids.recentPageInfo.textContent = total > loaded ? `最近 ${loaded} / ${total} 条` : `最近 ${loaded} 条`;
  ids.loadMoreRecent.hidden = !recentMeta.has_more;
  ids.loadMoreRecent.disabled = false;
  const renderKey = recentRenderKey(rows);
  if (renderKey === lastRecentRenderKey) return;
  lastRecentRenderKey = renderKey;
  renderTradeTable("recent", rows, recentTradeFields, ids.recentTradesHead, ids.recentTrades);
}

function recentRenderKey(rows) {
  const idsKey = rows.map((row) => `${row.id}:${row.status}:${row.settled_at || ""}:${row.pnl ?? ""}:${row.reason || ""}`).join(",");
  return `${idsKey}|${selectedFields.recent.join(",")}|${recentMeta.loaded}|${recentMeta.total}|${recentMeta.has_more}`;
}

function renderTradeTable(kind, rows, fields, headEl, bodyEl) {
  const selected = selectedFields[kind];
  const visibleFields = fields.filter((field) => selected.includes(field.key));
  headEl.innerHTML = `<tr>${visibleFields.map((field) => `<th>${safe(field.label)}</th>`).join("")}</tr>`;
  if (!rows.length) {
    bodyEl.innerHTML = `<tr><td class="empty" colspan="${Math.max(1, visibleFields.length)}">${kind === "open" ? "暂无持仓" : "暂无交易"}</td></tr>`;
    return;
  }
  bodyEl.innerHTML = rows.map((row) => `
    <tr>
      ${visibleFields.map((field) => `<td class="${field.cellClass || ""}">${field.render(row)}</td>`).join("")}
    </tr>
  `).join("");
}

function initFieldOptions() {
  renderFieldOptions("open", openTradeFields, ids.openFieldOptions);
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
    renderAll();
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
  applyRecentPage(data.recent_trades, data.recent_trades_meta);
  renderAll(data);
  loadEquityCurve(false).catch(showError);
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

function applyRecentPage(rows = [], meta = {}) {
  const incoming = Array.isArray(rows) ? rows : [];
  if (recentRows.length > incoming.length) {
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
  };
  if (latestStatus) {
    latestStatus.recent_trades = recentRows;
    latestStatus.recent_trades_meta = recentMeta;
  }
}

async function loadMoreRecentTrades() {
  ids.loadMoreRecent.disabled = true;
  try {
    const params = new URLSearchParams({
      limit: String(RECENT_PAGE_SIZE),
      offset: String(recentRows.length),
    });
    const res = await fetch(`/api/recent-trades?${params.toString()}`);
    if (!res.ok) throw new Error(`recent trades HTTP ${res.status}`);
    const page = await res.json();
    const nextRows = Array.isArray(page.recent_trades) ? page.recent_trades : [];
    const seen = new Set(recentRows.map((row) => row.id));
    recentRows = recentRows.concat(nextRows.filter((row) => !seen.has(row.id)));
    const meta = page.recent_trades_meta || {};
    recentMeta = {
      limit: Number(meta.limit || RECENT_PAGE_SIZE),
      offset: 0,
      loaded: recentRows.length,
      total: Number(meta.total || recentRows.length),
      has_more: recentRows.length < Number(meta.total || recentRows.length),
    };
    if (latestStatus) {
      latestStatus.recent_trades = recentRows;
      latestStatus.recent_trades_meta = recentMeta;
    }
    renderRecentTrades(recentRows);
  } finally {
    ids.loadMoreRecent.disabled = false;
  }
}

async function setPairStrategyEnabled(enabled) {
  ids.pairStrategyToggle.disabled = true;
  try {
    const res = await fetch("/api/strategy-settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pair_strategy_enabled: enabled }),
    });
    if (!res.ok) throw new Error(`strategy settings HTTP ${res.status}`);
    latestStatus = await res.json();
    renderAll(latestStatus);
  } finally {
    ids.pairStrategyToggle.disabled = false;
  }
}

function renderAll(data = latestStatus, options = {}) {
  if (data) pendingRenderData = data;
  pendingRenderOptions = {
    force: Boolean(pendingRenderOptions.force || options.force),
    forceChart: Boolean(pendingRenderOptions.forceChart || options.forceChart),
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
  renderPairStrategy(data);
  renderMetrics(data.metrics);
  renderMarket(data.runtime);
  renderOpenTrades(data.open_trades);
  renderRecentTrades(recentRows.length ? recentRows : data.recent_trades);
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
ids.loadMoreRecent.addEventListener("click", () => loadMoreRecentTrades().catch(showError));
ids.pairStrategyToggle.addEventListener("change", () => {
  setPairStrategyEnabled(ids.pairStrategyToggle.checked).catch((error) => {
    ids.pairStrategyToggle.checked = !ids.pairStrategyToggle.checked;
    showError(error);
  });
});
ids.chart.addEventListener("mousemove", handleChartMove);
ids.chart.addEventListener("mouseleave", handleChartLeave);

initFieldOptions();
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
