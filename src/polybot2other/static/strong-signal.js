"use strict";

(() => {
  const get = id => document.getElementById(id);
  const number = n => typeof n === "number" && Number.isFinite(n);
  const money = (n, signed = false) => {
    if (!number(n)) return "—";
    const sign = n < 0 ? "−" : signed && n > 0 ? "+" : "";
    return sign + "$" + Math.abs(n).toLocaleString("zh-CN", {minimumFractionDigits:2, maximumFractionDigits:2});
  };
  const clock = new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai", year:"numeric", month:"2-digit",
    day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false});
  const stamp = t => number(t) && t > 0 ? clock.format(t*1000).replaceAll("/", "-") : "—";
  const price = n => number(n) ? Number(n.toFixed(4)).toString() : "—";
  const side = s => ({Up:"涨", Down:"跌"}[s] || s || "—");
  const orderStates = {PENDING:"待成交确认", OPEN:"待成交", FILLED:"全部成交", PARTIAL:"部分成交",
    PARTIALLY_FILLED:"部分成交", CANCELED:"已取消", CANCELLED:"已取消", EXPIRED:"已到期", REJECTED:"未获接受"};
  const states = {waiting:"等待开始", starting:"正在启动", collecting:"模拟运行中", running:"模拟运行中",
    waiting_data:"等待新行情", degraded:"正在重试", stopped:"已停止", error:"采集中断"};
  const txt = (id, v) => { get(id).textContent = v ?? "—"; };
  const tone = value => value > 0 ? "positive" : value < 0 ? "negative" : "";
  let points = [], timer, busy = false, lastOk = null;

  function table(id, headings, rows, empty) {
    const target = get(id);
    if (!rows.length) {
      const note = document.createElement("p");
      note.className = "empty"; note.textContent = empty;
      target.replaceChildren(note);
      return;
    }
    const tbl = document.createElement("table");
    const titles = tbl.createTHead().insertRow();
    for (const label of headings) {
      const th = document.createElement("th");
      th.scope = "col"; th.textContent = label; titles.append(th);
    }
    const body = tbl.createTBody();
    for (const values of rows) {
      const row = body.insertRow();
      for (const value of values) {
        const cell = row.insertCell();
        if (Array.isArray(value)) {
          cell.textContent = value[0]; cell.className = value[1];
        } else {
          cell.textContent = value ?? "—";
        }
      }
    }
    target.replaceChildren(tbl);
  }

  function paintChart() {
    const canvas = get("curve"), ctx = canvas.getContext("2d");
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h || !ctx || points.length < 2) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(w * ratio); canvas.height = Math.round(h * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    const left = w < 420 ? 51 : 64, right = w - 8, top = 14, bottom = h - 28;
    let low = points[0].equity, high = low;
    for (const p of points) { low = Math.min(low, p.equity); high = Math.max(high, p.equity); }
    const padding = Math.max(.25, (high - low)*.08); low -= padding; high += padding;
    const first = points[0].at, last = points[points.length-1].at;
    const x = t => left + (t - first) / Math.max(1, last - first) * (right-left);
    const y = v => bottom - (v-low)/(high-low)*(bottom-top);
    ctx.font = '11px Inter,system-ui,sans-serif'; ctx.fillStyle = "#a7b3bd"; ctx.textAlign = "left";
    for (let i=0; i<3; i++) {
      const value = low + (high-low)*i/2, py = y(value);
      ctx.strokeStyle = "#384148"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(left,py); ctx.lineTo(right,py); ctx.stroke();
      ctx.fillText(money(value), 0, py+4);
    }
    ctx.beginPath(); ctx.moveTo(left,y(points[0].equity));
    let previous = points[0].equity;
    for (let i=1; i<points.length; i++) {
      ctx.lineTo(x(points[i].at),y(previous)); previous = points[i].equity;
      ctx.lineTo(x(points[i].at),y(previous));
    }
    ctx.lineWidth=2; ctx.strokeStyle="#71d5aa"; ctx.stroke();
    ctx.lineTo(x(last),bottom); ctx.lineTo(left,bottom); ctx.closePath();
    const grad = ctx.createLinearGradient(0,top,0,bottom);
    grad.addColorStop(0,"rgba(113,213,170,.22)"); grad.addColorStop(1,"rgba(113,213,170,.025)");
    ctx.fillStyle=grad; ctx.fill(); ctx.fillStyle="#a7b3bd";
    ctx.textAlign="left"; ctx.fillText(stamp(first).slice(5,16), left,h-6);
    ctx.textAlign="right"; ctx.fillText(stamp(last).slice(5,16), right,h-6);
  }

  function render(data) {
    const a = data.account, rule = data.rule, feed = data.feed || {}, event = data.last_event || {};
    const status = get("state");
    status.textContent = states[data.state] || data.state || "等待状态";
    status.dataset.kind = ["collecting","running"].includes(data.state) ? "ok" :
      ["degraded","error","stopped"].includes(data.state) ? "error" : "waiting";
    const heartbeat = feed.last_step || data.as_of;
    txt("start", stamp(data.start_at)); txt("heartbeat", stamp(heartbeat));
    txt("rules", "方向评分 ≥ " + price(rule.min_confidence) + " · 每局成交一次 · 每笔最多 " +
      money(rule.stake_dollars) + "（含费）· 最多 " + rule.max_open_trades + " 笔（含待确认）· 持有至官方结算");
    txt("initial",money(a.initial)); txt("equity",money(a.equity_at_cost)); txt("pnl",money(a.realized,true));
    get("pnl").className=tone(a.realized);
    for (const [id, value] of [["cash",a.cash],["reserved",a.reserved],["open-cost",a.open_cost],["fees",a.fees]]) {
      txt(id,money(value));
    }
    txt("win-rate",number(a.win_rate) ? a.win_rate.toFixed(2)+"%" : "—");
    txt("closed", "已结算 "+a.settled+" 局 · 盈利 "+a.wins+" 局");
    txt("round",feed.round_id); txt("reference",feed.reference_ready ? "已校验" : "等待校验");
    txt("quotes",Array.isArray(feed.quote_sides) && feed.quote_sides.length ?
      feed.quote_sides.map(side).join(" / ") : "等待盘口");
    txt("event",event.message || "等待首次判断"); txt("event-time",stamp(event.at));
    let warning = feed.last_error || "";
    if (feed.last_step && (Date.now()/1000-feed.last_step) > 45) {
      status.textContent="等待采集更新"; status.dataset.kind="error";
      warning = "采集记录已有一段时间未更新。"+(warning || "页面保留最近一次账本记录。");
    }
    get("warning").hidden=!warning; txt("warning",warning);
    points=(data.equity_curve || []).filter(row => number(row.at) && number(row.equity)).sort((u,v)=>u.at-v.at);
    const hasChart = points.length > 1;
    get("chart-empty").hidden=hasChart; get("curve").hidden=!hasChart;
    txt("chart-empty", !a.settled ? "首笔官方结算后显示净值走势" : "正在读取结算净值");
    get("curve").setAttribute("aria-label",hasChart ? "结算净值由 "+money(points[0].equity)+" 至 "+money(points[points.length-1].equity) : "等待结算数据");
    paintChart();
    const trades = data.recent_trades || [], orders = data.recent_orders || [];
    const opened = trades.filter(t=>t.status==="OPEN");
    const closed = trades.filter(t=>number(t.settled_at)).sort((x,y)=>y.settled_at-x.settled_at);
    txt("positions-count", a.open_positions+" 笔持仓 · "+a.pending_orders+" 笔待确认");
    txt("settled-count", "最近 "+closed.length+" 笔 / 已结算 "+a.settled+" 笔");
    txt("orders-count", "最近 "+orders.length+" 笔 / 已提交 "+a.submitted+" 笔");
    const tradeRow = t => [t.round_id, side(t.side), stamp(t.opened_at), price(t.entry_price), money(t.stake)];
    table("positions", ["市场","方向","开仓时间","成交均价","含费成本"], opened.map(tradeRow), "当前空仓");
    table("trades", ["市场","方向","开仓时间","成交均价","含费成本","结算时间","净盈亏"],
      closed.map(t=>[...tradeRow(t),stamp(t.settled_at),[money(t.pnl,true),tone(t.pnl)]]), "尚无已结算交易");
    table("orders", ["提交时间","市场","方向","提交评分","买入限价","状态","含费成交金额","成交费"],
      orders.map(o=>[stamp(o.created_at),o.round_id,side(o.side),price(o.confidence),price(o.limit_price),
        orderStates[o.status] || o.status,money(o.cash_spent),money(o.fee)]), "尚无委托");
  }

  async function refresh() {
    clearTimeout(timer);
    if (busy || document.hidden) return;
    busy = true;
    const ctrl = new AbortController();
    const timeout = setTimeout(()=>ctrl.abort(),8000);
    try {
      const res = await fetch("/api/status", {cache:"no-store",signal:ctrl.signal});
      if (!res.ok) throw new Error("HTTP "+res.status);
      const data = await res.json();
      render(data); lastOk = Date.now()/1000;
    } catch {
      const state = get("state"); state.textContent="连接中断"; state.dataset.kind="error";
      txt("warning", lastOk ? "最近一次读取成功于 "+stamp(lastOk)+"，正在重试。":"正在连接服务器上的模拟账本。");
      get("warning").hidden=false;
    } finally {
      clearTimeout(timeout); busy = false;
      if (!document.hidden) timer = setTimeout(refresh,5000);
    }
  }

  new ResizeObserver(paintChart).observe(get("curve").parentElement);
  document.addEventListener("visibilitychange",refresh);
  refresh();
})();
