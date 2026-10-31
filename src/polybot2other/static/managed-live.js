/* Real wallet and live execution controls; credentials never enter this UI. */
(() => {
  const names={MAIN:"主账户",MAIN_TP_SL:"主账户止盈止损",MAIN_OPTIMIZED:"主账户（优化版）",VALUE_CONFIRM_V1:"价值确认 V1"};
  let previousMode=null,busy=false,token="",latest=null,acknowledged=null;
  let dataView="auto",runtimeMessage="";
  const el=id=>document.getElementById(id);
  const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const amount=(v,p=2)=>v==null||!Number.isFinite(Number(v))?"—":"$"+Number(v).toFixed(p);
  const pnl=v=>v==null?"—":(Number(v)>0?"+":"")+amount(v);
  const at=v=>v?new Date(v*1000).toLocaleString("zh-CN"):"—";
  const state=data=>{const r=data?.runtime?.managed_live||{};return acknowledged&&r.available&&r.settings?.revision<acknowledged.settings.revision?acknowledged:r;};
  const key=()=>accountScope==="main"?"MAIN":parseAccountScope(accountScope).variantId||"MAIN";
  const active=data=>dataView==="live"||(dataView==="auto"&&state(data).enabled===true&&state(data).settings?.strategies?.[key()]?.enabled===true);
  const variant=data=>(state(data).variants||[]).find(r=>r.variant_id===key())||null;
  const execution=data=>{const r=state(data),s=variant(data)?.execution,observed=r.market_data?.observed_at;
    return observed&&Date.now()/1000-observed>15?{...s,state:"blocked",label:"运行状态未知",reason:"界面超过 15 秒未收到服务状态，无法确认实盘运行情况；正在重连。",since:observed+15,checked_at:observed,recovery:"auto",disconnected:true}:s;};
  const stateLabel=v=>({PREPARED:"准备提交",SUBMITTING:"正在提交",PENDING:"待官方确认",UNKNOWN:"提交结果待核对",FILLED:"已确认成交",CANCELED:"未成交",REJECTED:"已拒绝",FAILED:"链上成交失败",OPEN:"持仓中",CLOSED:"已卖出",SETTLED:"已结算",REDEEMABLE:"已结算 · 待领取"}[v]||v);
  function valuationNote(v){
    if(!v)return {text:"估值获取中",detail:"正在获取真实持仓报价",status:"missing"};
    const age=Math.max(0,Math.floor(v.age_seconds||0)),elapsed=age<60?`${age} 秒前`:`${Math.floor(age/60)} 分钟前`;
    const text={fresh:"最新盘口估值",cached:"上次估值 · 更新中",stale:`上次估值 · ${elapsed}`,pending:"上次估值 · 待结算",missing:v.pending_settlement||v.pending_positions?"等待官方结算与估值":"估值获取中",empty:"当前无持仓"}[v.status]||"估值获取中";
    const detail=v.status==="empty"?"当前没有未结算持仓":v.status==="missing"?"部分真实持仓暂无完整报价，等待估值更新；不会用 0 替代。":`估值时间：${at(v.as_of)}。按剩余份额、买盘深度与卖出费用估算；历史报价不代表当前可成交金额。${v.pending_settlement||v.pending_positions?"包含到期待结算持仓，最终以官方结果为准。":""}`;
    return {text,detail,status:v.status};
  }

  async function post(path,payload){
    if(!token){const r=await fetch("/api/managed-live",{cache:"no-store"});const d=await r.json();if(!r.ok)throw new Error(d.error||"实盘连接暂不可用");token=d.control_token;}
    const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Live-Control":token},body:JSON.stringify(payload)});const d=await r.json();
    if(!r.ok){if(r.status===403)token="";throw new Error(d.error||"设置未生效，请重试");}return d;
  }
  function cardValues(id){
    const card=el("managed-strategy-controls").querySelector(`[data-live-card="${id}"]`),values={};
    card.querySelectorAll("input[data-setting]").forEach(input=>{values[input.dataset.setting]=Number(input.value);if(!input.value||!Number.isFinite(values[input.dataset.setting]))throw new Error("请填写有效的金额或百分比");});return values;
  }
  async function update(payload){
    if(busy)return;busy=true;el("managed-live-message").textContent="正在保存设置…";
    try{const d=await post("/api/managed-live/settings",payload);acknowledged=d;latestStatus.runtime.managed_live=d;
      for(const id of Object.keys(payload.strategies||{}))el("managed-strategy-controls").querySelectorAll(`[data-live-card="${id}"] input[data-setting]`).forEach(input=>delete input.dataset.dirty);
      el("managed-live-message").textContent="设置已保存。单笔预算用于后续新开仓，原有持仓份额保持不变。";renderAll(latestStatus,{force:true,forceChart:true});
    }catch(error){el("managed-live-message").textContent=error.message;}finally{busy=false;if(latestStatus)render(latestStatus);}
  }
  function controls(data){
    const container=el("managed-strategy-controls");
    if(!container.children.length)container.innerHTML=Object.entries(names).map(([id,name])=>`<div class="managed-strategy-card" data-live-card="${id}">
      <div class="managed-strategy-title"><strong>${esc(name)}</strong><label class="managed-small-switch"><input type="checkbox" data-live-toggle="${id}" aria-label="${esc(name)}实盘开关"><span>允许实盘开仓</span></label></div>
      <div class="managed-inputs"><label>每笔金额（含费预算）<div><span>$</span><input type="number" min="0.1" step="0.1" data-setting="stake_dollars" aria-label="${esc(name)}实盘每笔金额"></div></label><button type="button" data-live-save="${id}">保存</button></div>
      <details><summary>风险设置</summary><label>24 小时亏损限额（$）<input type="number" min="0" step="1" data-setting="max_daily_loss" aria-label="${esc(name)}实盘每日亏损限额"></label>
      ${id==="MAIN_TP_SL"?'<label>净盈利止盈（%）<input type="number" min="1" max="1000" step="1" data-setting="take_profit_pct" aria-label="实盘止盈百分比"></label><label>净亏损止损（%）<input type="number" min="1" max="99" step="1" data-setting="stop_loss_pct" aria-label="实盘止损百分比"></label><small>止盈止损参数也用于当前已有实盘持仓。</small>':''}</details><small class="managed-card-state"></small></div>`).join("");
    const root=state(data);
    for(const [id,c] of Object.entries(root.settings?.strategies||{})){const card=container.querySelector(`[data-live-card="${id}"]`);if(!card)continue;
      const toggle=card.querySelector("input[data-live-toggle]");toggle.checked=c.enabled;toggle.disabled=busy;card.classList.toggle("is-live",root.enabled&&c.enabled);
      card.querySelectorAll("input[data-setting]").forEach(input=>{if(document.activeElement!==input&&!input.dataset.dirty)input.value=c[input.dataset.setting];});card.querySelector("button").disabled=busy;
      const execution=(root.variants||[]).find(r=>r.variant_id===id)?.execution,caption=card.querySelector(".managed-card-state");
      caption.textContent=execution?`${execution.label} · ${execution.reason}`:root.enabled&&c.enabled?"正在读取开仓状态":c.enabled?"等待实盘总开关开启":"新开仓关闭";caption.dataset.state=execution?.state||"off";}
  }
  function renderExecution(data){
    const r=variant(data),s=execution(data),box=el("managed-execution-status");if(!s){box.textContent="正在读取实盘开仓状态…";return;}
    const seconds=Math.max(0,Math.floor(Date.now()/1000-s.since)),duration=seconds<60?`${seconds} 秒`:`${Math.floor(seconds/60)} 分 ${seconds%60} 秒`;
    const recovery=s.disconnected?"正在重连 · 恢复后自动更新状态":s.state==="off"?"已有持仓继续管理":s.state==="blocked"?(s.recovery==="manual"?"需要人工处理后恢复":"自动复查中 · 条件恢复后继续开仓"):s.state==="confirming"?"自动核对成交结果":"持续检查入场条件";
    box.dataset.state=s.state;box.innerHTML=`<div class="managed-execution-head"><strong>${esc(r.combo)} · ${esc(s.label)}</strong><span>${esc(recovery)}</span></div><p>${esc(s.reason)}</p><small>${s.state==="blocked"?`已持续 ${duration} · `:""}最近检查：${s.checked_at?at(s.checked_at):"等待首次检查"}${s.retry_at>Date.now()/1000?` · 约 ${Math.ceil(s.retry_at-Date.now()/1000)} 秒后重试`:""}</small>`;
    const events=(state(data).events||[]).filter(e=>/^(reconcile_|entry_|engine_)/.test(e.kind));
    el("managed-execution-events").innerHTML=events.length?events.map(e=>`<li><time>${at(e.at)}</time><span>${esc(e.message.replace(/0x[a-fA-F0-9]{64}/g,id=>id.slice(0,12)+"…"))}</span></li>`).join(""):'<li>暂无暂停或恢复记录</li>';
  }
  function render(data){
    latest=data;const root=state(data),panel=el("managed-wallet-panel");if(!panel)return;panel.hidden=!root.available;
    el("managed-view-control").hidden=!root.available;el("managed-data-view").value=dataView;
    const live=active(data),w=root.wallet||{};
    if(previousMode!==live){if(typeof metricAnimations!=="undefined"){metricAnimations.forEach(x=>{if(x.frame)cancelAnimationFrame(x.frame);});metricAnimations.clear();document.querySelectorAll("[data-metric-ready]").forEach(n=>{delete n.dataset.metricReady;delete n.dataset.metricValue;});}
      if(typeof equityCurveRows!=="undefined")equityCurveRows=[];if(typeof equityCurveMeta!=="undefined")equityCurveMeta={};if(typeof lastChartRenderMs!=="undefined")lastChartRenderMs=0;if(typeof lastEquityCurveFetchMs!=="undefined")lastEquityCurveFetchMs=0;previousMode=live;}
    document.body.classList.toggle("managed-mode-live",live);el("paper-table-stack").hidden=live;el("managed-live-tables").hidden=!live;
    document.querySelectorAll("[data-live-valuation-note]").forEach(n=>{n.hidden=!live;});
    const brand=document.querySelector(".brand-lockup .eyebrow");if(brand)brand.textContent=live?"LIVE · 真实资金":"Paper · 策略对照";
    el("equity-chart-title").textContent=live?"真实钱包总资产曲线 · 共享":"资金曲线";el("market-panel-title").textContent=live?"实时市场 · 实盘行情":"实时市场 · 原主账户";
    el("managed-chart-empty").hidden=!live||(root.wallet_curve||[]).some(r=>r.total_equity!=null);el("equity-chart").hidden=!el("managed-chart-empty").hidden;
    const labels=live?["钱包总资产（共享）","策略净盈亏","策略未实现盈亏","钱包可用资金（共享）","全钱包预留额度","本策略持仓成本","本策略胜率","已结算序列回撤"]:["总资产","总盈亏","未实现盈亏","可用资金","挂单预留","持仓风险","胜率","最大回撤"];
    document.querySelectorAll(".metric-grid > .metric > span").forEach((n,i)=>{if(labels[i])n.textContent=labels[i];});
    if(panel.hidden)return;
    el("managed-mode-label").textContent=root.enabled?"允许策略实盘开仓":"实盘新开仓关闭";el("managed-master-enabled").checked=Boolean(root.enabled);el("managed-master-enabled").disabled=busy;
    el("managed-wallet-balance").textContent=amount(w.balance,6);el("managed-wallet-positions").textContent=amount(w.portfolio_value);el("managed-wallet-total").textContent=amount(w.total_equity);
    el("managed-wallet-address").textContent=w.address_masked?`${w.address_masked} · ${w.currency||"资金余额"}`:"钱包连接中";
    el("managed-wallet-health").textContent=w.ready&&!w.stale?"已连接":w.balance!=null?"更新中 · 上次余额":"暂不可用";
    el("managed-wallet-updated").textContent=w.checked_at?"更新于 "+at(w.checked_at):"不会用模拟余额替代真实余额";
    const err=root.error||root.entry_block_reason||w.error;
    if(!busy){const message=el("managed-live-message");if(err){message.textContent=err;runtimeMessage=err;}
      else if(runtimeMessage){if(message.textContent===runtimeMessage)message.textContent="";runtimeMessage="";}}
    controls(data);renderExecution(data);if(live)renderTables(data);
  }
  function metrics(data){const r=variant(data),w=state(data).wallet||{};return {...(r?.metrics||{}),total_equity:w.total_equity,cash_balance:w.available,reserved_cash:w.reserved};}
  function renderMetrics(v){
    const fields=[[ids.totalEquity,"total_equity"],[ids.totalPnl,"total_pnl"],[ids.unrealizedPnl,"unrealized_pnl"],[ids.cashBalance,"cash_balance"],[ids.reservedCash,"reserved_cash"],[ids.openRisk,"open_risk"],[ids.winRate,"win_rate"],[ids.maxDrawdown,"max_drawdown"]];
    fields.forEach(([n,f])=>{const a=metricAnimations.get(n);if(a?.frame)cancelAnimationFrame(a.frame);metricAnimations.delete(n);delete n.dataset.metricReady;delete n.dataset.metricValue;const x=v[f];
      n.textContent=f==="win_rate"?x==null?"—":Number(x).toFixed(2)+"%":["total_pnl","unrealized_pnl"].includes(f)?pnl(x):amount(x);n.className=["total_pnl","unrealized_pnl"].includes(f)?x>0?"positive":x<0?"negative":"":"";});
    const note=valuationNote(v.valuation);
    document.querySelectorAll("[data-live-valuation-note]").forEach(n=>{n.hidden=false;n.textContent=v.valuation?.status==="empty"&&n.dataset.liveValuationNote==="total"?"已实现盈亏":note.text;n.title=note.detail;n.dataset.status=note.status;});
  }
  function rows(data){return(state(data).variants||[]).map(r=>`<tr><td><strong>${esc(r.combo)}</strong></td><td>${esc(r.execution?.label||"正在读取状态")}</td><td>${amount(r.metrics.position_value)}</td><td>${pnl(r.metrics.realized_pnl)}</td><td>${r.metrics.settled_trades}</td><td>${r.metrics.win_rate==null?"—":r.metrics.win_rate.toFixed(2)+"%"}</td><td>${r.metrics.open_trades}</td><td class="reason-cell">${esc(r.execution?.reason||r.last_signal?.reason||"等待实盘信号")}</td><td><button type="button" data-live-select="${r.variant_id}">查看实盘</button></td></tr>`).join("");}
  function renderTables(data){
    const r=variant(data);if(!r)return;const positions=r.open_trades||[],orders=r.orders||[],history=r.recent_trades||[];
    el("managed-position-account").textContent=r.combo;el("managed-position-count").textContent=positions.length+" 笔";
    el("managed-positions").innerHTML=positions.length?positions.map(p=>{const pending=orders.some(o=>o.position_id===p.id&&["PREPARED","SUBMITTING","PENDING","UNKNOWN"].includes(o.state));const expired=Number(p.round_id.split("-").pop())+300<=Date.now()/1000;
      const note=valuationNote(p.valuation);
      return `<tr><td>${esc(p.round_id)}<br><strong>${esc(p.outcome)}</strong></td><td>${amount(p.remaining_cost)}</td><td>${p.remaining_shares.toFixed(6)}</td><td>${amount(p.mark_value)}<small class="managed-valuation-note" data-status="${esc(note.status)}" title="${esc(note.detail)}">${esc(note.text)}</small></td><td>${pnl(p.unrealized_pnl)}</td><td><button type="button" data-live-sell="${p.id}" data-live-owner="${r.variant_id}" ${pending||expired?"disabled":""}>${pending?"订单确认中":expired?"等待官方结算":"卖出"}</button></td></tr>`;}).join(""):'<tr><td colspan="6">当前策略没有真实持仓。不会展示 Paper 持仓。</td></tr>';
    el("managed-orders").innerHTML=orders.length?orders.map(o=>`<tr><td>${at(o.created_at)}</td><td>${o.action==="BUY"?"买入":"卖出"} ${esc(o.outcome)}</td><td>${esc(stateLabel(o.state))}${o.error?"<br>"+esc(o.error):""}</td><td>${o.action==="BUY"?amount(o.requested_cash):o.requested_shares.toFixed(6)+" 份"}</td><td>${o.confirmed_shares.toFixed(6)}</td><td>${amount(o.confirmed_notional)}</td><td>${amount(o.confirmed_fee,6)}</td><td title="${esc(o.ack_id||o.external_id)}">${esc((o.ack_id||o.external_id).slice(0,12))}…</td></tr>`).join(""):'<tr><td colspan="8">暂无本程序提交的实盘订单。</td></tr>';
    el("managed-history").innerHTML=history.length?history.map(p=>`<tr><td>${at(p.opened_at)}</td><td>${esc(p.round_id)}<br>${esc(p.outcome)}</td><td>${esc(stateLabel(p.status))}</td><td>${amount(p.total_cost)}</td><td>${pnl(p.realized_pnl)}</td><td>${amount(p.claim_due)}</td></tr>`).join(""):'<tr><td colspan="6">暂无官方确认的实盘成交，实盘盈亏从实际成交开始统计。</td></tr>';
  }
  function market(data){const m=state(data).market_data||{},r=m.market,selected=variant(data);if(!r){ids.markets.innerHTML='<div class="managed-empty">实盘行情连接中</div>';return;}const left=Math.max(0,Math.floor(r.ends_at-Date.now()/1000));
    ids.markets.innerHTML=`<article class="market-card"><div class="panel-head"><h2>BTC 5m</h2><span>${esc(r.round_id)}</span></div><div class="managed-market-values"><div><span>剩余</span><strong>${Math.floor(left/60)}:${String(left%60).padStart(2,"0")}</strong></div><div><span>本期结算基准</span><strong>${amount(r.target_price)}</strong></div><div><span>Chainlink 现价</span><strong>${amount(m.spot)}</strong></div><div><span>当前 TWAP60</span><strong>${amount(m.twap)}</strong></div></div><p>${esc(selected?.execution?.reason||selected?.last_signal?.reason||"等待信号")}</p><p class="subtle">${m.reference_ready&&m.reference_verified?"实时参考价已校验":"正在校验行情与开盘基准"}</p></article>`;}

  document.addEventListener("input",e=>{if(e.target.matches("#managed-strategy-controls input[data-setting]"))e.target.dataset.dirty="true";});
  document.addEventListener("change",e=>{
    if(e.target.id==="managed-data-view"){dataView=e.target.value;renderAll(latestStatus,{force:true,forceChart:true});return;}
    if(e.target.id==="managed-master-enabled"){try{const strategies={};if(e.target.checked)for(const[id,c]of Object.entries(state(latest).settings?.strategies||{}))if(c.enabled)strategies[id]=cardValues(id);update({enabled:e.target.checked,strategies});}catch(error){el("managed-live-message").textContent=error.message;render(latest);}}
    const id=e.target.dataset.liveToggle;if(id){try{update({strategies:{[id]:e.target.checked?{...cardValues(id),enabled:true}:{enabled:false}}});}catch(error){el("managed-live-message").textContent=error.message;render(latest);}}
  });
  document.addEventListener("click",async e=>{
    const save=e.target.closest("[data-live-save]");if(save){try{update({strategies:{[save.dataset.liveSave]:cardValues(save.dataset.liveSave)}});}catch(error){el("managed-live-message").textContent=error.message;}}
    const select=e.target.closest("[data-live-select]");if(select){ids.accountScopeSelect.value=select.dataset.liveSelect==="MAIN"?"main":"experiment:"+select.dataset.liveSelect;ids.accountScopeSelect.dispatchEvent(new Event("change",{bubbles:true}));}
    const sell=e.target.closest("[data-live-sell]");if(sell){sell.disabled=true;try{await post("/api/managed-live/sell",{strategy_id:sell.dataset.liveOwner,position_id:Number(sell.dataset.liveSell)});el("managed-live-message").textContent="卖出请求已处理，请以官方确认后的成交为准。";}catch(error){el("managed-live-message").textContent=error.message;}finally{sell.disabled=false;}}
  });
  setInterval(()=>{if(!latest||!state(latest).available)return;renderExecution(latest);if(active(latest)&&typeof renderRuntime==="function")renderRuntime(latest.runtime);},1000);
  window.ManagedLiveUI={active,state,variant,execution,metrics,render,renderMetrics,rows,market,
    meta:d=>({label:(variant(d)?.combo||names[key()])+" · 实盘",source:"钱包余额与总资产共享；盈亏、持仓及胜率按当前策略统计"}),
    curve:d=>(state(d).wallet_curve||[]).filter(r=>r.total_equity!=null),
    currentPoint:d=>{const w=state(d).wallet||{};return w.total_equity==null?null:{created_at:w.checked_at,cash_balance:w.balance,total_equity:w.total_equity};}};
})();
