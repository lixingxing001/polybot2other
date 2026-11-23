// Read-only projection of the independent Paper wallet for the main dashboard.
(() => {
  const id = "MAIN_FIRST_STRONG";
  const label = "主账户·每局首个强信号";
  const tags = {variant_id: id, combo: label, account_scope: "strategy_experiment"};
  const role = "方向评分达到 0.65，每局只成交一次，持有到官方结算";
  const orderFilters = {
    active: ["PENDING", "OPEN"], filled: ["FILLED", "PARTIAL"],
    canceled: ["CANCELED", "CANCELLED"], expired: ["EXPIRED"], rejected: ["REJECTED"],
  };
  let snapshot = null, metrics = {}, trades = [], orders = [], curve = [];
  let request = null, lastAttempt = -Infinity, readError = "";

  function accept(next) {
    if (next?.key !== id || next.mode !== "paper" || !Number.isFinite(next.as_of) ||
        !Number.isFinite(next.account?.initial) || !Number.isFinite(next.account?.equity_at_cost) ||
        !Array.isArray(next.equity_curve) || !Array.isArray(next.recent_orders) || !Array.isArray(next.recent_trades)) {
      throw new Error("无效的独立 Paper 账本");
    }
    const account = next.account;
    let peak = account.initial, drawdown = 0;
    const nextCurve = next.equity_curve.map(row => {
      if (!Number.isFinite(row.at) || !Number.isFinite(row.equity)) throw new Error("无效的资金曲线");
      peak = Math.max(peak, row.equity);
      drawdown = Math.max(drawdown, peak - row.equity);
      return {created_at: row.at, total_equity: row.equity, realized_pnl: row.equity - account.initial};
    });
    const nextMetrics = {
      initial_balance: account.initial, total_equity: account.equity_at_cost,
      total_pnl: account.realized, realized_pnl: account.realized,
      unrealized_pnl: account.open_positions === 0 ? 0 : null, valuation_missing: account.open_positions > 0,
      cash_balance: account.cash, reserved_cash: account.reserved, open_risk: account.open_cost,
      win_rate: account.win_rate, max_drawdown: drawdown, open_trades: account.open_positions,
    };
    const nextTrades = next.recent_trades.map(row => {
      const settled = row.status === "SETTLED";
      return {...row, ...tags, id: row.round_id, strategy_type: label,
        payout: settled ? row.stake + row.pnl : null,
        roi_pct: settled && row.stake > 0 ? row.pnl / row.stake * 100 : null,
        settlement_source_label: settled ? "官方结算" : "待结算"};
    }).sort((a,b) => (b.settled_at || b.opened_at) - (a.settled_at || a.opened_at));
    const nextOrders = next.recent_orders.map(row => ({...row, ...tags, order_type: "FAK"}));
    curve = nextCurve;
    metrics = nextMetrics;
    trades = nextTrades;
    orders = nextOrders;
    snapshot = next;
    readError = "";
  }

  function warning() {
    if (readError) return readError;
    if (!snapshot) return "正在连接独立 Paper 账本";
    if ((Date.now()/1000 - snapshot.as_of) > 45) return "独立账本延迟更新，显示最近一次记录";
    if (snapshot.state === "degraded") return "独立策略正在重试，等待新判断";
    if (snapshot.state === "waiting_data") return "独立策略正在等待有效行情";
    return "";
  }

  function refresh() {
    if (document.hidden || request || Date.now() - lastAttempt < 5_000) return;
    lastAttempt = Date.now();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 6_000);
    request = (async () => {
      try {
        const response = await fetch("/api/first-strong-paper", {cache:"no-store", signal:controller.signal});
        if (!response.ok) throw new Error("HTTP " + response.status);
        accept(await response.json());
      } catch {
        readError = snapshot ? "独立账本连接中断，显示最近一次记录" : "独立 Paper 账本暂不可用，正在重试";
      } finally {
        clearTimeout(timeout);
        request = null;
        window.dispatchEvent(new Event("first-strong-paper-update"));
      }
    })();
  }

  function variant() {
    return {...tags, role, metrics, recent_trades_summary: {settled_count: snapshot?.account.settled, win_rate: metrics.win_rate},
      last_error: warning(), last_signal: {reason: snapshot?.last_event?.message}};
  }

  function tradePage({start_at, end_at}) {
    const rows = trades.filter(row => {
      const at = row.settled_at || row.opened_at;
      return (start_at == null || at >= start_at) && (end_at == null || at <= end_at);
    });
    const closed = rows.filter(row => row.status === "SETTLED");
    const sum = field => closed.reduce((total, row) => total + row[field], 0);
    const stake = sum("stake"), net = sum("pnl"), wins = closed.filter(row => row.pnl > 0).length;
    return {rows, summary: {total_count:rows.length, settled_count:closed.length, official_count:closed.length,
      win_count:wins, loss_count:closed.filter(row=>row.pnl<0).length, total_pnl:net,
      total_payout:sum("payout"), settled_stake:stake, roi_pct:stake ? net/stake*100 : null,
      win_rate:closed.length ? wins/closed.length*100 : null}};
  }

  window.FirstStrongPaper = {
    id, label, role, refresh, variant, tradePage,
    get snapshot() { return snapshot; },
    get curve() { return curve; },
    get warning() { return warning(); },
    get reason() { return warning() || snapshot?.last_event?.message || "等待首次判断"; },
    get openTrades() { return trades.filter(row => row.status === "OPEN"); },
    orderPage(filter) { return orders.filter(row => !orderFilters[filter] || orderFilters[filter].includes(row.status)); },
  };
})();
