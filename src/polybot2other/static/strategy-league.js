(() => {
  "use strict";
  const el = id => document.getElementById(id);
  const section = el("strategy-league");
  const filter = el("league-filter");
  const fmt = new Intl.NumberFormat("zh-CN", {style:"currency",currency:"USD"});
  const date = new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});
  const amount = v => Number.isFinite(v) ? fmt.format(v) : "—";
  const when = ts => Number.isFinite(ts) ? date.format(ts*1000) : "—";
  function node(tag, text) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    return element;
  }
  function note(parent, label, text) {
    const p=node("p");
    p.append(node("b", label+" "), document.createTextNode(text));
    parent.append(p);
  }
  function describeStage(details, label, stage) {
    if (!stage) return;
    note(details, label, `${when(stage.start)} 至 ${when(stage.end)}（北京），结清 ${stage.rounds} 局，净赚亏 ${amount(stage.pnl)}，同期对照 ${amount(stage.control_pnl)}，额外损耗后 ${amount(stage.stressed)}。`);
  }
  function row(arm, opened) {
    const tr=node("tr"), first=node("td"), details=node("details");
    details.dataset.key=arm.key;
    details.open=opened.has(arm.key);
    details.append(node("summary",arm.name));
    if(arm.key==="exit_settle"){
      const button=node("button","打开正式 Paper 账户");
      button.type="button";button.className="table-action";button.dataset.exitSettleAccount="";
      details.append(button);
    }
    note(details,"入场",arm.entry);
    note(details,"退出",arm.exit);
    note(details,"容易失效的情况",arm.weakness);
    note(details,"模拟钱包",`现金 ${amount(arm.cash)}，按可卖深度估值 ${amount(arm.equity)}，已实现 ${amount(arm.realized)}，累计手续费 ${amount(arm.fees)}。`);
    const positions=arm.positions ?? (arm.position ? [arm.position] : []);
    const pos=positions.length ? positions.map(p=>`${p.round_id} · ${p.side==="Up"?"涨":"跌"} ${p.shares.toFixed(2)} 份`).join("；") : "空仓";
    note(details,"持仓与执行",`${pos}，买单成交 ${arm.fills} / 提交 ${arm.submitted}，待成交 ${arm.pending}，与对照都成交并结清 ${arm.shared_closed} 局。`);
    if (arm.unpriced_shares>0) note(details,"估值",`${arm.unpriced_shares.toFixed(4)} 份缺少可卖深度，暂按零估值。`);
    if (arm.past_stop) note(details,"曾暂停买入",`北京时间 ${when(arm.past_stop.stopped_at)} 至 ${when(arm.past_stop.released_at)}。`);
    if (arm.screen_to_date && data.screen_start > data.start_at) {
      const recent=arm.screen_to_date;
      note(details,"新规则同期成绩",`已结清 ${recent.rounds} 局，扣费收益 ${amount(recent.pnl)}${arm.is_control ? "" : `，比对照 ${amount(recent.excess)}`}。`);
    }
    if (arm.last_reason) note(details,"最近记录",arm.last_reason);
    describeStage(details,"初筛",arm.grade.screen);
    describeStage(details,"复验",arm.grade.validation);
    first.append(details);tr.append(first);
    for (const value of [arm.grade.label, `${arm.fills} / ${arm.closed}`, amount(arm.closed_pnl), arm.is_control ? "—" : amount(arm.excess), amount(arm.stressed), amount(arm.max_drawdown)]) {
      tr.append(node("td",value));
    }
    if (arm.grade.code==="focus") tr.children[1].className="league-good";
    if (["stopped","rejected","drawdown"].includes(arm.grade.code)) tr.children[1].className="league-bad";
    return tr;
  }
  let data, pending=false, ready=false;
  function render() {
    const host=el("league-families");
    const opened = new Set(Array.from(host.querySelectorAll("details[data-key][open]"), d => d.dataset.key));
    const existingFamilies = new Map(Array.from(host.querySelectorAll("details[data-family]"), d => [d.dataset.family, d.open]));
    const focused = document.activeElement?.closest("details[data-key]")?.dataset.key;
    const groups=new Map();
    for (const arm of data.arms) {
      if (filter.value && !arm.is_control && arm.family!==filter.value) continue;
      if (!groups.has(arm.family)) groups.set(arm.family,[]);
      groups.get(arm.family).push(arm);
    }
    const fragment=document.createDocumentFragment();
    for (const [family,arms] of groups) {
      const control=arms[0].is_control;
      const group=node(control ? "section" : "details"), table=node("table"), headers=node("tr"), head=node("thead"), body=node("tbody"), wrap=node("div");
      group.className="league-family";
      if (control) {
        group.append(node("h3",family));
      } else {
        group.dataset.family=family;
        group.open=Boolean(filter.value) || (existingFamilies.get(family) ?? false);
        group.append(node("summary",family+" · "+arms.length+" 个候选"));
      }
      wrap.className="table-wrap";
      wrap.tabIndex=0;wrap.setAttribute("aria-label",family+"策略表，可横向滚动");
      table.className="paper-growth-table";
      for(const h of ["策略 · 点开看规则","筛选进度","买入成交 / 已结清","扣费净收益","比同期对照","每份再扣 2¢","盘中最大回撤"]) {
        const th=node("th",h);th.scope="col";headers.append(th);
      }
      head.append(headers);for(const arm of arms) body.append(row(arm,opened));
      table.append(head,body);wrap.append(table);group.append(wrap);fragment.append(group);
    }
    host.replaceChildren(fragment);
    if (focused) [...host.querySelectorAll("details")].find(e=>e.dataset.key===focused)?.querySelector("summary")?.focus({preventScroll:true});
  }
  function show(next) {
    data=next;
    section.hidden=!data.enabled;
    if (!data.enabled) return;
    el("league-body").hidden=!data.available;
    if (!data.available) {
      el("league-status").textContent=data.reason;return;
    }
    const candidates=data.arms.filter(arm=>!arm.is_control);
    const families=[...new Set(candidates.map(arm=>arm.family))];
    let submitted=0, fills=0, closed=0;
    for (const arm of candidates) {
      submitted+=arm.submitted;
      fills+=arm.fills;
      closed+=arm.closed;
    }
    if (filter.options.length!==families.length+1) {
      const selected=filter.value;
      filter.replaceChildren(new Option("全部家族",""),...families.map(f=>new Option(f,f)));
      filter.value=families.includes(selected) ? selected : "";
    }
    const c=data.coverage;
    const coverage=c.expected ? `已结束的 ${c.expected} 局中，${c.usable} 局行情完整` : "等待首局结束";
    const last=data.last_seen;
    const health=data.as_of < data.start_at ? "等待首局" : !last || data.as_of-last>30 ? "等待采集恢复" :
      !data.last_valid || data.as_of-data.last_valid>30 ? "等待可交易的行情" : "采集中";
    const policy=data.unrestricted
      ? `各策略持续按信号模拟买卖。重点观察 ${data.state_counts.focus||0} 个。`
      : `旧规则记录，停开 ${data.state_counts.stopped||0} 个。`;
    const boundary=data.unrestricted && data.screen_start > data.start_at
      ? ` · 新规则生效 ${when(data.unrestricted_at)} · 同期筛选从 ${when(data.screen_start)} 起` : "";
    el("league-start").textContent=`北京时间 · 累计自 ${when(data.start_at)} 起${boundary}`;
    el("league-status").textContent=`${data.candidate_count} 个候选 · ${data.family_count} 个家族 · 1 个同期对照 · ${health}。全部候选累计提交 ${submitted} 笔买单，成交 ${fills} 笔，结清 ${closed} 局（各策略分别计数）。已观察 ${data.observed} 局，${coverage}。${policy}`;
    render();
    if (!ready && location.hash==="#strategy-league") section.scrollIntoView();
    ready=true;
  }
  async function load() {
    if (pending || document.hidden || (document.body.dataset.page && document.body.dataset.page!=="bot")) return;
    pending=true;
    try {
      const response=await fetch("/api/strategy-league",{cache:"no-store",signal:AbortSignal.timeout(7000)});
      if (!response.ok) throw Error("HTTP");
      show(await response.json());
    } catch {
      section.hidden=false;
      el("league-status").textContent=ready ? "暂时读不到更新，当前显示上次读取的成绩。" : "暂时读不到候选策略成绩，稍后自动重试。";
    } finally {pending=false;}
  }
  filter.addEventListener("change",render);
  el("league-rules-button").addEventListener("click",()=>{
    const rules=el("league-rules");rules.hidden=!rules.hidden;
    el("league-rules-button").setAttribute("aria-expanded",String(!rules.hidden));
  });
  document.addEventListener("visibilitychange",load);
  window.addEventListener("hashchange",()=>window.setTimeout(load,0));
  window.setInterval(load,30000);
  load();
})();
