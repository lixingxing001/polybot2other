(() => {
  "use strict";
  const el = id => document.getElementById("forecast-paper-" + id);
  const panel = el("panel");
  if (!panel) return;
  const forwardIds = new Set(["FORECAST_LIMIT_060_PAPER", "FORECAST_LIMIT_CONTROL_PAPER"]);
  const sides = {Up: "偏涨", Down: "偏跌"};
  const actions = {WAIT: "等待", BUY: "考虑买入", HOLD: "持有", SELL: "考虑卖出"};
  const money = new Intl.NumberFormat("en-US", {style: "currency", currency: "USD", signDisplay: "exceptZero"});
  const startTime = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
  let selected = null, requestId = 0, pending = false, lastStart = 0;
  let comparisonPending = false, lastComparison = 0;

  function appendRow(body, cells) {
    const tr = body.insertRow();
    cells.forEach(value => { tr.insertCell().textContent = value; });
  }

  const clockTime = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  const hhmmss = seconds => Number.isFinite(seconds) ? clockTime.format(seconds * 1000) : "—";
  const cents = value => Number.isFinite(value) ? (100 * value).toFixed(2) + "¢" : "—";
  const scoreText = value => Number.isFinite(value) ? (100 * value).toFixed(1) + " 分" : "—";
  const margin = value => Number.isFinite(value) ? ` · 余量 ${value > 0 ? "+" : ""}${scoreText(value)}` : "";

  function showTrace(trace, now) {
    const section = el("trace");
    section.hidden = selected !== "FORECAST_EARLY_PAPER" && !forwardIds.has(selected);
    if (section.hidden) return;
    const rows = el("trace-rows");
    const summary = el("trace-summary");
    rows.replaceChildren();
    if (!trace?.available || !trace.started_at) {
      summary.textContent = trace?.available ? "等待第一个入场窗口。" : "暂时读不到逐局记录。";
      return;
    }
    const count = trace.counts;
    summary.textContent = `从北京时间 ${startTime.format(trace.started_at * 1000)} 开始，已观察 ${count.window_seen} 局，其中 ${count.first_direction} 局看清方向，${count.first_buy} 局出现可买信号，${count.arrival_fill} 局在模拟撮合时买到。含正在进行的局。`;
    for (const row of trace.rows) {
      const first = row.first_direction, buy = row.first_buy;
      const pending = Number.isFinite(row.ends_at) && row.ends_at > now;
      const direction = first ? `${hhmmss(first.at)} ${sides[first.side] || ""} · ${scoreText(first.score)}` : pending ? "观察中" : "未记录到明确方向";
      let price = Number.isFinite(first?.ask_all_in_cost) ? cents(first.ask_all_in_cost) + margin(first.ask_edge)
        : first?.quote_error ? "盘口无效" : "—";
      const priced = row.first_priced_direction;
      if (first && !Number.isFinite(first.ask_all_in_cost) && Number.isFinite(priced?.ask_all_in_cost)) {
        price += ` · 随后 ${hhmmss(priced.at)} ${sides[priced.side] || ""} ${cents(priced.ask_all_in_cost)}${margin(priced.ask_edge)}`;
      }
      const opportunity = buy ? `${hhmmss(buy.at)} ${sides[buy.side] || ""} · 含费限价 ${cents(buy.all_in_cost)}${margin(buy.edge)}` : "—";
      let progress;
      if (row.arrival_fill) progress = `${hhmmss(row.arrival_fill.at)} 模拟买入 · 均价 ${cents(row.arrival_fill.matched_price)}`;
      else if (row.arrival_empty) {
        const arrival = row.arrival_empty;
        progress = `${hhmmss(arrival.at)} 未成交 · 限价 ${cents(arrival.limit_price)}，到达卖一 ${cents(arrival.ask)}`;
      }
      else if (row.first_order) progress = `${hhmmss(row.first_order.at)} 已提交 · ${({FILLED:"已确认成交",PARTIAL:"部分成交",CANCELED:"已取消",IN_FLIGHT:"等待撮合",CONFIRMING:"等待确认"})[row.first_order.phase] || "待核对"}`;
      else if (row.pre_submit_reject) progress = `${hhmmss(row.pre_submit_reject.at)} ${row.pre_submit_reject.reason || "发单前复查未通过"}`;
      else progress = buy ? "有买入信号，未见买单" : pending ? "观察中" : "未提交买单";
      const coverage = row.window_seen?.late_start || row.observation_gap ? " · 记录不全" : "";
      const close = Number.isFinite(row.ends_at) ? startTime.format(row.ends_at * 1000) : "—";
      appendRow(rows, [close + coverage, direction, price, opportunity, progress]);
    }
  }

  function display(data) {
    const current = data.current;
    const now = Date.now() / 1000;
    const fresh = data.available && current && current.ends_at > now && now - current.as_of < 8;
    el("direction").textContent = fresh ? (sides[current.direction] || "暂时看不清") : "等待新行情";
    el("decision").textContent = fresh
      ? `距收盘 ${Math.max(0, Math.ceil(current.ends_at - now))} 秒 · ${actions[current.action] || "等待"} · ${current.reason || "等待有效判断"}`
      : "当前还没有及时更新的判断。";
    el("tie").hidden = !(fresh && Number.isFinite(current.remaining_average_to_tie));
    if (!el("tie").hidden) {
      const value = current.remaining_average_to_tie.toLocaleString("en-US", {maximumFractionDigits: 2});
      el("tie").textContent = `按本次行情估算，最后一分钟剩余时段的均价达到 $${value} 则判涨，低于则判跌。`;
    }
    const rows = el("checkpoints");
    rows.replaceChildren();
    const times = selected === "FORECAST_EARLY_PAPER" ? [240, 180, 60, 20] : [180, 60, 20];
    for (const seconds of times) {
      const c = data.checkpoints?.find(row => row.seconds_left === seconds);
      appendRow(rows, [seconds === 20 ? "20 秒" : `${seconds / 60} 分钟`,
        c ? `${c.calls} / ${c.tracked}` : "0 / 0",
        c?.graded ? `${c.correct} / ${c.graded}` : (c?.calls ? "待结算" : "—"),
        c?.comparable ? `${c.paired_correct} / ${c.comparable}` : "—",
        c?.comparable ? `${c.book_correct} / ${c.comparable}` : "—"]);
    }
    const exits = data.early_exits;
    el("exits").textContent = exits?.graded
      ? `已核对 ${exits.graded} 笔提前卖出，相比同样份额持有到期，合计${exits.relative_pnl >= 0 ? "多赚" : "少赚"} $${Math.abs(exits.relative_pnl).toFixed(2)}（含费）。`
      : "提前卖出与持有到期的差额，将在官方结算后核对。";
    const entries = data.entry_reasons;
    const reasons = (entries?.reasons || []).slice(0, 5).map(row => `${row.reason}（${row.rounds} 局）`);
    const entryNote = el("entry-reasons");
    if (entries?.rounds) {
      const totals = `近 ${entries.period_hours} 小时，本账户入场时段已记录 ${entries.rounds} 个收盘市场，其中 ${entries.no_entry} 局没有开仓。`;
      entryNote.textContent = totals + (reasons.length ? ` 常见原因（同局可多项） · ${reasons.join("、")}` : "");
    } else {
      entryNote.textContent = "等待本账户积累未开仓原因。";
    }
    showTrace(data.entry_trace, now);
  }

  function displayComparison(data) {
    const notice = el("comparison-notice");
    const body = el("comparison-body");
    if (!data.available) {
      body.hidden = true;
      notice.textContent = data.reason || "暂时无法读取两个账户的同期记录。";
      return;
    }
    const hasRounds = data.rounds > 0;
    const since = data.start_at ? startTime.format(new Date(data.start_at * 1000)) : "等待开始";
    notice.textContent = hasRounds
      ? `北京时间 ${since} 起，两个账户均记录完整并已收盘的市场共 ${data.rounds} 局。`
      : `北京时间 ${since} 起进行同期对照，正在等待第一局完整记录。`;
    const rows = el("comparison-rows");
    rows.replaceChildren();
    for (const [label, result] of [["原版 · 剩 3 分钟起", data.base], ["提前版 · 剩 4 分钟起", data.early]]) {
      const rate = Number.isFinite(result.open_rate) ? result.open_rate.toFixed(1) + "%" : "—";
      appendRow(rows, [label, `${result.opened} / ${data.rounds}`, rate, hasRounds ? money.format(result.realized_pnl) : "—"]);
    }
    const o = data.overlap;
    el("comparison-result").textContent = hasRounds
      ? `提前版独有 ${o.early_only} 局（已实现 ${money.format(o.early_only_realized_pnl)}），原版独有 ${o.base_only} 局，双方都开仓 ${o.both} 局。提前版在新增的 1 分钟内实际买入 ${data.early.early_entries} 局。两账户同期已实现净盈亏相差 ${money.format(data.pnl_delta)}（提前版减原版）。`
      : "首个完整市场结束后即可比较开仓次数和已实现盈亏。";
    el("comparison-coverage").textContent =
      `进行中 ${data.active_rounds} 局，已收盘但记录不齐 ${data.incomplete_rounds} 局，均未计入。同期市场中仍有持仓或待确认订单的，原版 ${data.base.unresolved} 局，提前版 ${data.early.unresolved} 局。`;
    if (data.base.excluded_opened || data.early.excluded_opened) {
      const excluded = [["原版", data.base], ["提前版", data.early]]
        .filter(([, r]) => r.excluded_opened)
        .map(([label, r]) => `${label}开仓 ${r.excluded_opened} 局、已实现 ${money.format(r.excluded_realized_pnl)}`);
      el("comparison-coverage").textContent += ` 记录不齐的市场另有${excluded.join("，")}，未计入上表。`;
    }
    body.hidden = false;
  }

  function displayForward(data) {
    const notice = el("forward-status");
    el("forward-body").hidden = !data.available;
    if (!data.available) {
      notice.textContent = data.reason || "等待两个模拟账户就绪。";
      return;
    }
    const status = {waiting: "等待第一个完整市场", collecting: "收集中", passed: "达到本轮模拟验收条件",
      failed: "本轮未通过", incomplete: "收盘估值记录不齐，暂时无法验收"};
    const since = startTime.format(data.start_at * 1000);
    const goals = data.requirements;
    notice.textContent = `北京时间 ${since} 起 · ${status[data.status] || "等待核对"} · 已观察 ${data.elapsed_days.toFixed(1)} / ${goals.days} 天 · 限价版已结清 ${data.candidate.closed_rounds} / ${goals.closed_rounds} 局`;
    if (data.candidate.flat_budget_exhausted) notice.textContent += " · 限价版的剩余回撤额度不足以再开一单";
    const rows = el("forward-rows");
    rows.replaceChildren();
    const value = number => Number.isFinite(number) ? money.format(number) : "—";
    for (const [label, result] of [["60 美分限价版", data.candidate], ["同期对照组", data.control]]) {
      appendRow(rows, [label, result.observed_rounds, result.submitted_orders, `${result.filled_rounds} / ${result.closed_rounds}`, value(result.closed_pnl),
        value(result.first_50_pnl), value(result.next_50_pnl), value(result.stressed_pnl),
        Number.isFinite(result.marked_drawdown) ? "$" + result.marked_drawdown.toFixed(2) : "—"]);
    }
    el("forward-coverage").textContent = `行情记录有缺口的市场，限价版 ${data.candidate.data_gap_rounds} 局，对照组 ${data.control.data_gap_rounds} 局。两段各满 50 个成交结清局后分别核算收益。`;
  }

  async function loadComparison() {
    if (comparisonPending || Date.now() - lastComparison < 14000) return;
    const generation = requestId;
    const forward = forwardIds.has(selected);
    comparisonPending = true;
    lastComparison = Date.now();
    try {
      const res = await fetch(forward ? "/api/forecast-forward" : "/api/forecast-comparison", {cache: "no-store", signal: AbortSignal.timeout(7000)});
      if (!res.ok) throw new Error("comparison_unavailable");
      const data = await res.json();
      if (generation !== requestId) return;
      if (forward) displayForward(data);
      else displayComparison(data);
    } catch {
      if (generation !== requestId) return;
      el(forward ? "forward-status" : "comparison-notice").textContent = "对照数据暂时读不到，稍后自动重试。";
      el(forward ? "forward-body" : "comparison-body").hidden = true;
    } finally { if (generation === requestId) comparisonPending = false; }
  }

  async function load() {
    if (!selected || document.hidden || !document.getElementById("bot-page")?.classList.contains("is-active")) return;
    loadComparison();
    if (pending || Date.now() - lastStart < 2500) return;
    const generation = requestId;
    pending = true;
    lastStart = Date.now();
    try {
      const res = await fetch(`/api/forecast-paper?strategy_id=${encodeURIComponent(selected)}`,
        {cache: "no-store", signal: AbortSignal.timeout(7000)});
      if (!res.ok) throw new Error("forecast_unavailable");
      const data = await res.json();
      if (generation === requestId && selected) {
        if (!data.available) throw new Error("forecast_unavailable");
        display(data);
      }
    } catch {
      if (generation === requestId) {
        el("direction").textContent = "读取失败";
        el("decision").textContent = "连接恢复后自动刷新。";
        el("tie").hidden = true;
      }
    } finally { if (generation === requestId) pending = false; }
  }

  window.ForecastObservationsUI = {sync(id, name) {
    if (selected !== id) {
      selected = id;
      requestId++;
      pending = false;
      lastStart = 0;
      comparisonPending = false;
      lastComparison = 0;
      panel.hidden = !id;
      const forward = forwardIds.has(id);
      el("time-study").hidden = forward;
      el("forward").hidden = !forward;
      el("trace").hidden = id !== "FORECAST_EARLY_PAPER" && !forward;
      if (id) {
        el(forward ? "forward-body" : "comparison-body").hidden = true;
        el(forward ? "forward-status" : "comparison-notice").textContent = "正在读取同期记录…";
        el("direction").textContent = "正在读取";
        el("decision").textContent = "";
        el("tie").hidden = true;
        el("exits").textContent = "";
        el("entry-reasons").textContent = "";
        el("checkpoints").replaceChildren();
        el("trace-rows").replaceChildren();
        el("trace-summary").textContent = "正在读取逐局记录…";
      }
    }
    if (selected) {
      el("title").textContent = (name || "本局方向预测") + " · Paper";
      load();
    }
  }};
  setInterval(load, 3000);
})();
