const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 });

const ids = {
  runtime: document.getElementById("runtime-pill"),
  totalEquity: document.getElementById("total-equity"),
  totalPnl: document.getElementById("total-pnl"),
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
let quotes = {};
let untaggedPriceBatchCount = 0;
let priceState = {
  chainlink: null,
  chainlink_updated_ms: null,
  binance: null,
  binance_updated_ms: null,
  target_price: null,
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

function setMetric(el, value, formatter = money) {
  el.textContent = formatter.format(value || 0);
  el.className = cls(value || 0);
}

function renderMetrics(metrics) {
  ids.totalEquity.textContent = money.format(metrics.total_equity);
  setMetric(ids.totalPnl, metrics.total_pnl);
  ids.cashBalance.textContent = money.format(metrics.cash_balance);
  ids.openRisk.textContent = money.format(metrics.open_risk);
  ids.winRate.textContent = `${number.format(metrics.win_rate)}%`;
  ids.maxDrawdown.textContent = money.format(metrics.max_drawdown);
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

function renderMarket(runtime) {
  const market = activeMarket || runtime.current_market;
  const signal = runtime.last_signal || {};
  const pair = runtime.pair_strategy || {};
  const lastPairEvent = pair.last_event || {};
  const up = quotes.Up || runtime.latest_quotes?.Up || {};
  const down = quotes.Down || runtime.latest_quotes?.Down || {};
  const current = priceState.chainlink || runtime.latest_price?.chainlink || runtime.latest_price?.binance || null;
  const target = priceState.target_price || market?.target_price || null;
  const distance = current && target ? ((current - target) / target) * 10_000 : null;
  ids.markets.innerHTML = `
    <article class="market-row">
      <div class="market-top">
        <div>
          <div class="market-symbol">BTC 5m</div>
          <div class="muted">${market?.slug || "-"}</div>
        </div>
        <div class="market-price">${current ? money.format(current) : "-"}</div>
      </div>
      <div class="market-fields">
        <div class="field"><span>Polymarket 市场</span><strong>${market?.question || "-"}</strong></div>
        <div class="field"><span>剩余时间</span><strong>${market ? fmtLeft(market.end_ts) : "-"}</strong></div>
        <div class="field"><span>Chainlink 目标价</span><strong>${target ? money.format(target) : "-"}</strong></div>
        <div class="field"><span>当前差距</span><strong class="${cls(distance || 0)}">${distance == null ? "-" : `${number.format(distance)} bps`}</strong></div>
        <div class="field"><span>Up 买一 / 卖一</span><strong>${quoteText(up)}</strong></div>
        <div class="field"><span>Down 买一 / 卖一</span><strong>${quoteText(down)}</strong></div>
        <div class="field"><span>配对成本 ask</span><strong>${pair.pair_cost == null ? "-" : number.format(pair.pair_cost)}</strong></div>
        <div class="field"><span>配对退出 bid</span><strong>${pair.bid_sum == null ? "-" : number.format(pair.bid_sum)}</strong></div>
        <div class="field"><span>策略信号</span><strong class="${sideClass(signal.side)}">${signal.side || "-"}</strong></div>
        <div class="field"><span>信号原因</span><strong>${signal.reason || "-"}</strong></div>
        <div class="field"><span>配对事件</span><strong>${lastPairEvent.message || "-"}</strong></div>
        <div class="field"><span>Chainlink 更新时间</span><strong>${fmtMs(priceState.chainlink_updated_ms)}</strong></div>
        <div class="field"><span>WebSocket</span><strong>${marketWsStatus} / ${priceWsStatus}</strong></div>
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
  const height = canvas.clientHeight || 260;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#181c20";
  ctx.fillRect(0, 0, width, height);
  const pad = 28;
  const values = points.length ? points.map((p) => p.total_equity) : [100];
  const min = Math.min(...values, 100);
  const max = Math.max(...values, 100);
  const span = Math.max(1, max - min);
  ctx.strokeStyle = "#323a42";
  ctx.lineWidth = 1;
  for (let i = 0; i < 4; i += 1) {
    const y = pad + ((height - pad * 2) / 3) * i;
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(width - pad, y);
    ctx.stroke();
  }
  ctx.strokeStyle = "#63b3ff";
  ctx.lineWidth = 2;
  ctx.beginPath();
  values.forEach((value, index) => {
    const x = pad + ((width - pad * 2) * index) / Math.max(1, values.length - 1);
    const y = height - pad - ((value - min) / span) * (height - pad * 2);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = "#9aa7b1";
  ctx.font = "12px system-ui";
  ctx.fillText(money.format(max), pad, 18);
  ctx.fillText(money.format(min), pad, height - 8);
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
    drawChart(data.equity_curve);
    lastChartRenderMs = now;
  }
}

function applyMarket(market) {
  if (!market?.slug) return;
  if (activeMarket?.slug === market.slug) return;
  activeMarket = market;
  priceState = { ...priceState, target_price: market.target_price || null };
  quotes = {};
  connectMarketSocket();
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
      if (!priceState.target_price && activeMarket?.start_ts && Math.abs(ts - activeMarket.start_ts * 1000) <= 20_000) {
        priceState.target_price = value;
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
  try {
    const res = await fetch("/api/live-snapshot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        market: activeMarket,
        target_price: priceState.target_price || activeMarket.target_price || null,
        price: priceState,
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
  foregroundRefreshTimer = setTimeout(() => {
    loadStatus(true).catch(showError);
  }, 150);
}

ids.tickButton.addEventListener("click", () => loadStatus(true).catch(showError));
ids.loadMoreRecent.addEventListener("click", () => loadMoreRecentTrades().catch(showError));
ids.pairStrategyToggle.addEventListener("change", () => {
  setPairStrategyEnabled(ids.pairStrategyToggle.checked).catch((error) => {
    ids.pairStrategyToggle.checked = !ids.pairStrategyToggle.checked;
    showError(error);
  });
});

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
