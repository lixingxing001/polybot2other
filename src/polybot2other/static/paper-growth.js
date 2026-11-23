(() => {
  "use strict";
  const panel = document.getElementById("paper-growth-panel");
  if (!panel) return;
  const el = (id) => document.getElementById(`paper-growth-${id}`);
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const money = (value, signed = false) => finite(value)
    ? `${signed && value > 0 ? "+" : ""}${value.toLocaleString("en-US", {style:"currency", currency:"USD"})}` : "—";
  const percent = (value) => finite(value) ? `${value.toFixed(1)}%` : "—";
  const stamp = (value) => finite(value) ? new Intl.DateTimeFormat("zh-CN", {
    timeZone:"Asia/Shanghai", hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false,
  }).format(new Date(value * 1000)) : "—";
  const pnlClass = (value) => finite(value) && value !== 0 ? value > 0 ? "positive" : "negative" : "";
  const tone = (value) => ["green", "blue", "amber", "red"].includes(value) ? value : "neutral";
  let loading = false, lastAttempt = 0, lastGood = null, selectedId = "MAIN", lastScopeId = null;

  function visible() {
    return !panel.hidden && document.visibilityState !== "hidden"
      && document.getElementById("bot-page")?.classList.contains("is-active");
  }

  function unavailable(message) {
    panel.dataset.stale = "true";
    el("notice").textContent = message;
    el("body").hidden = !lastGood;
    if (lastGood) {
      renderOverview(lastGood, true);
      renderDetails(lastGood, true);
    }
    el("updated").textContent = lastGood
      ? `显示 ${stamp(lastGood.as_of)} 的历史结果，正在等待更新`
      : "稍后自动重试";
  }

  function renderOverview(data, stale = false) {
    const labels = {today:"今日赚亏", last1h:"近 1 小时", last3h:"近 3 小时", last6h:"近 6 小时"};
    const focusedId = document.activeElement?.dataset?.growthStrategy;
    el("strategies").innerHTML = data.strategies.map((strategy) => {
      const id = escape(strategy.strategy_id), name = escape(strategy.name);
      const values = Object.entries(labels).map(([key, label]) => {
        const value = strategy.available ? strategy.windows[key].pnl : null;
        return `<td data-label="${label}" class="${pnlClass(value)}">${money(value, true)}</td>`;
      }).join("");
      return `<tr data-selected="${strategy.strategy_id === selectedId}"><th scope="row">${name}</th>`
        + `<td class="paper-growth-row-state"><span class="paper-growth-badge" data-tone="${stale ? "neutral" : tone(strategy.state.tone)}">${stale ? "等待更新" : escape(strategy.state.label)}</span></td>`
        + values + `<td class="paper-growth-row-action"><button type="button" data-growth-strategy="${id}" aria-controls="paper-growth-detail" aria-label="查看${name}的增长详情">查看详情</button></td></tr>`;
    }).join("");
    if (focusedId) {
      Array.from(el("strategies").querySelectorAll("button")).find((button) => button.dataset.growthStrategy === focusedId)?.focus({preventScroll:true});
    }
  }

  function renderDetails(data, stale = false) {
    const strategy = data.strategies.find((item) => item.strategy_id === selectedId);
    if (!strategy) {
      el("detail-name").textContent=el("detail-title").textContent=selectedId==="FORECAST_EXIT_SETTLE"?"拿到官方结算":selectedId;
      el("state").textContent="等待该策略的记录";el("state").dataset.tone="neutral";
      el("summary").textContent="正在读取这个策略的 Paper 增长记录。";
      el("detail-body").hidden=true;
      return;
    }
    el("detail-name").textContent = strategy.name;
    el("detail-title").textContent = strategy.name;
    el("state").textContent = stale ? "等待更新" : strategy.state.label;
    el("state").dataset.tone = stale ? "neutral" : tone(strategy.state.tone);
    el("summary").textContent = stale ? "下面是上次读到的数据，等更新后再看状态。"
      : strategy.available ? `${strategy.day_label} · ${strategy.state.reason}` : strategy.state.reason;
    el("detail-body").hidden = !strategy.available;
    if (!strategy.available) return;
    const windows = strategy.windows;
    const values = {today: windows.today.pnl, hour: windows.last1h.pnl, peak: strategy.today_curve.peak_pnl,
      drawdown: strategy.today_curve.giveback};
    for (const [key, value] of Object.entries(values)) {
      el(key).textContent = money(value, ["today", "hour"].includes(key));
      el(key).className = ["today", "hour"].includes(key) ? pnlClass(value) : "";
    }
    el("peak-note").textContent = strategy.today_curve.peak_pnl > 0 ? `当时是 ${stamp(strategy.today_curve.peak_at)}` : "今天还没有出现过净盈利";
    el("drawdown-note").textContent = finite(strategy.today_curve.giveback_pct_of_peak_profit)
      ? `少了最高盈利的 ${percent(strategy.today_curve.giveback_pct_of_peak_profit)}` : "只计算这个策略的已结束交易";
    const labels = {last1h:"最近 1 小时", previous1h:"再前 1 小时", last3h:"最近 3 小时", last6h:"最近 6 小时", night:strategy.night_label};
    el("windows").innerHTML = Object.entries(labels).map(([key,label]) => {
      const row = windows[key];
      const coverage = row.complete_history ? "" : `<small>历史覆盖 ${Number(row.coverage_hours).toFixed(1)} 小时</small>`;
      return `<tr><th scope="row">${escape(label)}${coverage}</th><td class="${pnlClass(row.pnl)}">${money(row.pnl,true)}</td>`
        + `<td class="${pnlClass(row.pnl_per_hour)}">${money(row.pnl_per_hour,true)}</td><td>${percent(row.win_rate)}</td>`
        + `<td><strong>${row.markets}</strong> 个市场 / ${row.trades} 笔</td></tr>`;
    }).join("");
    el("reasons").innerHTML = strategy.reasons.slice(1).map((reason) => `<li>${escape(reason)}</li>`).join("");
    el("positions").textContent = strategy.open_positions
      ? `这个策略还有 ${strategy.open_positions} 笔交易没结束，投入 ${money(strategy.open_cost)}；暂时没计入这里的赚亏金额。`
      : "这个策略目前没有尚未结算的 Paper 持仓。";
  }

  function render(data) {
    if (data?.scope !== "paper_by_strategy" || !Array.isArray(data.strategies) || !data.strategies.length) {
      unavailable("暂时读不到各策略的数据，稍后自动重试。");
      return;
    }
    if (!finite(data.as_of) || Date.now() / 1000 - data.as_of > 60) {
      unavailable("数据超过一分钟没更新了，先等新数据再看状态。");
      return;
    }
    lastGood = data;
    if (selectedId!=="FORECAST_EXIT_SETTLE" && !data.strategies.some((strategy) => strategy.strategy_id === selectedId)) selectedId = data.strategies[0].strategy_id;
    panel.dataset.stale = "false";
    el("body").hidden = false;
    el("notice").textContent = data.complete
      ? "每行只计算这个策略自己的交易。点“查看详情”，看它最近赚亏的具体情况。"
      : data.available ? "部分策略暂时读不到数据，其余策略仍各自更新。"
      : "各策略的数据暂时都读不到，稍后自动重试。";
    renderOverview(data);
    renderDetails(data);
    el("updated").textContent = `更新于 ${stamp(data.as_of)} · 每 15 秒刷新`;
  }

  async function load(force = false) {
    if (!visible() || loading || (!force && Date.now() - lastAttempt < 12000)) return;
    loading = true;
    lastAttempt = Date.now();
    el("refresh").disabled = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch("/api/paper-growth", {signal:controller.signal, cache:"no-store"});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch (_) {
      unavailable("各策略状态更新失败，等连接恢复后自动重试。");
    } finally {
      clearTimeout(timeout);
      loading = false;
      el("refresh").disabled = false;
    }
  }

  window.PaperGrowthUI = {
    sync({paper, strategyId = "MAIN"}) {
      panel.hidden = !paper;
      if (strategyId !== lastScopeId) {
        lastScopeId = strategyId;
        selectedId = strategyId;
        if (lastGood && panel.dataset.stale !== "true") render(lastGood);
      }
      if (paper) load();
    },
  };
  el("strategies").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-growth-strategy]");
    if (!button || !lastGood) return;
    selectedId = button.dataset.growthStrategy;
    const stale = panel.dataset.stale === "true";
    renderOverview(lastGood, stale);
    renderDetails(lastGood, stale);
    el("detail").open = true;
    el("detail").scrollIntoView({behavior:"smooth", block:"nearest"});
  });
  el("refresh").addEventListener("click", () => load(true));
  document.addEventListener("visibilitychange", () => load(true));
  window.addEventListener("hashchange", () => setTimeout(() => load(), 0));
  setInterval(() => load(), 15000);
})();
