/* Real wallet and live execution controls; credentials never enter this UI. */
(() => {
  const names={MAIN:"主账户",MAIN_TP_SL:"主账户止盈止损",MAIN_OPTIMIZED:"主账户（优化版）",MAIN_ADAPTIVE:"主账户自适应成长版",VALUE_CONFIRM_V1:"价值确认 V1"};
  let previousMode=null,busy=false,token="",latest=null,acknowledged=null;
  let dataView="auto",runtimeMessage="";
  const el=id=>document.getElementById(id);
  const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const amount=(v,p=2)=>v==null||!Number.isFinite(Number(v))?"—":"$"+Number(v).toFixed(p);
  const pnl=v=>v==null?"—":(Number(v)>0?"+":"")+amount(v);
  const pnlHtml=v=>`<span class="${v>0?"positive":v<0?"negative":""}">${pnl(v)}</span>`;
  const moneyHtml=(v,kind,precision=2)=>`<span class="managed-money ${v!=null&&Number(v)!==0?"managed-money--"+kind:"managed-money--muted"}">${amount(v,precision)}</span>`;
  const at=v=>v?new Date(v*1000).toLocaleString("zh-CN"):"—";
  const state=data=>{
    let r=data?.runtime?.managed_live||{};
    if(acknowledged&&r.available&&r.settings?.revision<acknowledged.settings.revision)r=acknowledged;
    const observed=r.market_data?.observed_at;
    if(r.error||r.entry_block_reason||!observed||Date.now()/1000-observed>15)return r;
    return {...r,variants:(r.variants||[]).map(v=>{
      const s=v.execution,risk=v.risk||{},limit=v.settings?.max_open_trades;
      if(s?.state!=="blocked"||s.reason!=="持仓与待确认订单已达到本策略持仓数量上限"
        ||risk.pending_slots!==0||risk.confirmed_slots!==risk.slots||!(risk.slots>0&&risk.slots>=limit))return v;
      return {...v,execution:{...s,state:"holding",label:"持仓管理中",recovery:"auto",
        reason:`已有 ${risk.confirmed_slots} 笔真实持仓，达到 ${limit} 笔持仓上限；平仓或结算后自动继续检查开仓。`}};
    })};
  };
  const key=()=>accountScope==="main"?"MAIN":parseAccountScope(accountScope).variantId||"MAIN";
  const active=data=>dataView==="live"||(dataView==="auto"&&state(data).enabled===true&&state(data).settings?.strategies?.[key()]?.enabled===true);
  const variant=data=>(state(data).variants||[]).find(r=>r.variant_id===key())||null;
  const walletCurve=data=>{
    const root=state(data),now=Date.now()/1000,cutoff=now-7*86400;
    const rows=(root.wallet_curve||[]).filter(r=>r.total_equity!=null&&r.created_at>=cutoff&&r.created_at<=now);
    const w=root.wallet||{},last=rows[rows.length-1];
    if(w.ready&&!w.stale&&w.total_equity!=null&&w.checked_at>=cutoff&&w.checked_at<=now&&(!last||w.checked_at>last.created_at))
      rows.push({created_at:w.checked_at,cash_balance:w.balance,total_equity:w.total_equity});
    return rows;
  };
  const executionFor=(data,selected)=>{const r=state(data),s=selected?.execution,observed=r.market_data?.observed_at;
    return observed&&Date.now()/1000-observed>15?{...s,state:"blocked",label:"运行状态未知",reason:"界面超过 15 秒未收到服务状态，无法确认实盘运行情况；正在重连。",since:observed+15,checked_at:observed,recovery:"auto",disconnected:true}:s;};
  const execution=data=>executionFor(data,variant(data));
  const executionTargets=data=>{
    const root=state(data),enabled=(root.variants||[]).filter(v=>root.settings?.strategies?.[v.variant_id]?.enabled===true);
    return enabled.length?enabled:[variant(data)||{variant_id:key(),combo:names[key()]}];
  };
  const stateLabel=v=>({PREPARED:"准备提交",SUBMITTING:"正在提交",PENDING:"待官方确认",UNKNOWN:"提交结果待核对",QUARANTINED:"隔离核对中",EXPIRED_UNFILLED:"到期核对无成交",FILLED:"已确认成交",CANCELED:"未成交",REJECTED:"已拒绝",FAILED:"链上成交失败",OPEN:"持仓中",CLOSED:"已卖出",SETTLED:"已结算",REDEEMABLE:"已结算 · 待领取"}[v]||v);
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
      <div class="managed-inputs"><label>每笔金额（Paper / 实盘共用，含费）<div><span>$</span><input type="number" min="0.1" step="0.1" data-setting="stake_dollars" aria-label="${esc(name)}每笔金额"></div></label><button type="button" data-live-save="${id}">保存</button></div>
      <details><summary>风险设置（Paper / 实盘共用）</summary><label>最多持仓（含待确认）<input type="number" min="1" max="10" step="1" data-setting="max_open_trades" aria-label="${esc(name)}最多持仓"></label><label>24 小时亏损限额（$）<input type="number" min="0" step="1" data-setting="max_daily_loss" aria-label="${esc(name)}每日亏损限额"></label>
      ${id==="MAIN_TP_SL"?'<label>净盈利止盈（%）<input type="number" min="1" max="1000" step="1" data-setting="take_profit_pct" aria-label="实盘止盈百分比"></label><label>净亏损止损（%）<input type="number" min="1" max="99" step="1" data-setting="stop_loss_pct" aria-label="实盘止损百分比"></label><small>止盈止损参数也用于当前已有实盘持仓。</small>':''}</details><small class="managed-card-state"></small></div>`).join("");
    const root=state(data);
    for(const [id,c] of Object.entries(root.settings?.strategies||{})){const card=container.querySelector(`[data-live-card="${id}"]`);if(!card)continue;
      const toggle=card.querySelector("input[data-live-toggle]");toggle.checked=c.enabled;toggle.disabled=busy;card.classList.toggle("is-live",root.enabled&&c.enabled);
      card.querySelectorAll("input[data-setting]").forEach(input=>{if(document.activeElement!==input&&!input.dataset.dirty)input.value=c[input.dataset.setting];});card.querySelector("button").disabled=busy;
      const execution=(root.variants||[]).find(r=>r.variant_id===id)?.execution,caption=card.querySelector(".managed-card-state");
      caption.textContent=execution?`${execution.label} · ${execution.reason}`:root.enabled&&c.enabled?"正在读取开仓状态":c.enabled?"等待实盘总开关开启":"新开仓关闭";caption.dataset.state=execution?.state||"off";}
  }
  function renderExecution(data){
    const container=el("managed-execution-status");if(!container)return;
    const targets=executionTargets(data),wanted=new Set(targets.map(r=>r.variant_id));
    const existing=new Map(Array.from(container.querySelectorAll('[data-execution-card]')).map(n=>[n.dataset.executionCard,n]));
    for(const [id,node] of existing)if(!wanted.has(id))node.remove();
    const enabled=targets.filter(r=>state(data).settings?.strategies?.[r.variant_id]?.enabled===true).length;
    const scope=el("managed-execution-scope");
    if(scope)scope.textContent=enabled?`实盘策略监控 · 跟随已启用的 ${enabled} 个策略${state(data).enabled?"":" · 总开关已关闭"}`:"实盘策略监控 · 当前没有启用策略";
    targets.forEach((r,index)=>{
      let box=existing.get(r.variant_id);
      if(!box){box=document.createElement('div');box.className='managed-execution-status';box.dataset.executionCard=r.variant_id;}
      if(container.children[index]!==box)container.insertBefore(box,container.children[index]||null);
      renderExecutionCard(data,r,box);
    });
    const events=(state(data).events||[]).filter(e=>/^(reconcile_|entry_|engine_)/.test(e.kind));
    el("managed-execution-events").innerHTML=events.length?events.map(e=>`<li><time>${at(e.at)}</time><span>${esc(e.message.replace(/0x[a-fA-F0-9]{64}/g,id=>id.slice(0,12)+"…"))}</span></li>`).join(""):'<li>暂无暂停或恢复记录</li>';
  }
  function renderExecutionCard(data,r,box){
    const s=executionFor(data,r)||{state:"connecting",label:"正在连接",reason:"正在读取实盘开仓状态…"};
    const seconds=Math.max(0,Math.floor(Date.now()/1000-(s.since||Date.now()/1000))),duration=seconds<60?`${seconds} 秒`:`${Math.floor(seconds/60)} 分 ${seconds%60} 秒`;
    const tone={blocked:"red",waiting:"amber",confirming:"amber",connecting:"amber",ready:"green",holding:"green",off:"neutral"}[s.state]||"neutral";
    const recovery=s.disconnected?"正在重连，恢复后自动更新":s.state==="off"?"已有持仓继续管理":s.state==="holding"?"释放名额后自动检查入场":s.state==="blocked"?(s.recovery==="manual"?"需要人工处理后恢复":"自动复查，条件恢复后继续开仓"):s.state==="confirming"?"正在核对官方成交结果":s.state==="connecting"?"等待服务连接":s.state==="ready"?"正在处理入场请求":"持续检查入场条件";
    // Keep lamp nodes stable across the one-second status refresh; rebuilding
    // them would restart the animation and make the lights flicker.
    if(!box.querySelector(".execution-signal-shell"))box.innerHTML=`
      <div class="execution-signal-shell" aria-hidden="true"><i class="execution-lamp is-red"></i><i class="execution-lamp is-amber"></i><i class="execution-lamp is-green"></i></div>
      <div class="execution-signal-content"><div class="execution-signal-heading"><strong data-execution-name></strong><span class="execution-signal-badge"><i aria-hidden="true"></i><span data-execution-label></span></span></div><p data-execution-reason></p><div class="execution-signal-meta" aria-live="off"><span data-execution-time></span><span data-execution-duration></span><span data-execution-retry></span></div></div>
      <div class="execution-signal-aside"><div class="execution-signal-legend" aria-hidden="true"><span data-lamp-tone="red"><i></i>异常</span><span data-lamp-tone="amber"><i></i>等待 / 确认</span><span data-lamp-tone="green"><i></i>运行</span></div><span data-execution-recovery></span></div>`;
    box.dataset.state=s.state;box.dataset.tone=tone;
    const put=(selector,value)=>{const node=box.querySelector(selector);if(node.textContent!==value)node.textContent=value;node.hidden=!value;};
    put("[data-execution-name]",r?.combo||names[key()]||"当前策略");
    put("[data-execution-label]",s.label||"状态待确认");
    const wallet=state(data).wallet||{};
    const cashDetail=s.reason==="真实可用余额不足本笔含费预算"&&wallet.available!=null
      ?`：可用 ${amount(wallet.available)}（余额 ${amount(wallet.balance)} − 预留 ${amount(wallet.reserved)}），本笔预算 ${amount(r?.settings?.stake_dollars)}`:"";
    put("[data-execution-reason]",(s.reason||"等待服务更新")+cashDetail);
    put("[data-execution-time]",`最近检查 ${s.checked_at?at(s.checked_at):"等待首次检查"}`);
    put("[data-execution-duration]",s.state==="blocked"?`已持续 ${duration}`:"");
    put("[data-execution-retry]",s.retry_at>Date.now()/1000?`约 ${Math.ceil(s.retry_at-Date.now()/1000)} 秒后重试`:"");
    put("[data-execution-recovery]",recovery);
  }
  function renderQuarantine(data){
    const orders=state(data).quarantined_orders||[],box=el("managed-quarantine-status"),current=variant(data),risk=current?.risk||{};
    box.hidden=!orders.length;if(box.hidden)return;
    const cash=orders.reduce((sum,o)=>sum+(o.action==="BUY"?o.requested_cash:0),0);
    box.innerHTML=`<strong>${orders.length} 笔订单隔离核对中 · 保留资金 ${amount(cash)}</strong><p>隔离订单继续计入持仓和亏损限额，原市场禁止重复开仓。其他市场通过余额和剩余额度检查后可继续交易。</p><p>${esc(current?.combo||"当前策略")}：已占 ${risk.slots??0} / ${current?.settings?.max_open_trades??"—"} 个持仓名额（含待确认），风险占用 ${amount(risk.loss_exposure)}。</p><ul>${orders.map(o=>{
      let check={};try{check=JSON.parse(o.verification_json||"{}");}catch{}
      const parts=[check.order==="missing"?"订单暂无记录":check.order==="found"?"已查到订单":"订单状态待核对",check.trades==="checked"?`成交查询完成 · 匹配 ${check.matching_trades??0} 笔`:"成交记录待核对"];
      if(check.open_orders==="checked")parts.push(check.matching_open_order?"仍有该笔挂单":"暂无该笔挂单");
      if(check.token_balance!=null)parts.push(`对应代币 ${Number(check.token_balance).toFixed(6)} 份`);
      if(check.wallet_activity==="checked")parts.push(`独立钱包活动 ${check.wallet_activity_count??0} 条`);
      if(check.wallet_activity==="unavailable")parts.push("钱包活动查询暂不可用");
      if(check.market_resolved)parts.push("市场已正式结算");
      if(o.closure_checks>0)parts.push(`最终核对 ${o.closure_checks}/3 次`);
      return `<li><b>${esc(names[o.strategy_id]||o.strategy_id)} · ${esc(o.outcome)} · ${o.action==="BUY"?amount(o.requested_cash):"卖出核对"}</b><span>${esc(o.round_id)}</span><small>提交于 ${at(o.created_at)} · 隔离于 ${at(o.quarantined_at)} · 最近核对 ${at(o.verification_at)}</small><small>${esc(parts.join(" · "))}；查询为空不会直接认定未成交。</small></li>`;}).join("")}</ul>`;
  }
  function renderDataHealth(data){
    const guard=data?.runtime?.continuity,feed=state(data).market_data?.feed_health,box=el("managed-data-health");
    box.hidden=!guard&&!feed;if(box.hidden)return;
    const now=Date.now()/1000,guardFresh=guard?.last_heartbeat_at&&now-guard.last_heartbeat_at<=15;
    const protection=guard?.keep_awake_active&&guardFresh?"防自动休眠已启用 · 保持开盖":guard?.enabled&&!guardFresh?"运行保护状态待确认":"本机防自动休眠未启用";
    const sides=Object.entries(feed?.sides||{}).map(([side,q])=>({...q,side,age:(q.age_ms??Infinity)+Math.max(0,now-(feed.checked_at||now))*1000,receiveAge:(q.receive_age_ms??Infinity)+Math.max(0,now-(feed.checked_at||now))*1000}));
    const fresh=sides.filter(q=>q.fresh&&q.age<=3000&&q.receiveAge<=2500),repaired=fresh.some(q=>q.source==="clob-rest-recovery");
    const marketText=state(data).market_paused?"行情已暂停":fresh.length===2?(repaired?"最新盘口已补齐":"盘口实时可用"):fresh.length?"盘口部分可用":"等待有效盘口";
    const ageText=sides.map(q=>`${q.side} ${Number.isFinite(q.age)?(Math.max(0,q.age)/1000).toFixed(2)+" 秒":"未就绪"}`).join(" · ");
    const detail=fresh.length<2&&feed?.last_rest_error_detail?`<small class="negative">${esc(feed.last_rest_error_detail)}</small>`:"";
    box.innerHTML=`<div><strong>${esc(protection)}</strong><small>${esc(guard?.note||"")}</small>${guard?.last_interruption?`<small>检测到约 ${Math.round(guard.last_interruption.seconds)} 秒中断，已请求重新同步行情与资金。</small>`:""}${guard?.error?`<small class="negative">${esc(guard.error)}</small>`:""}</div><div><strong>${esc(marketText)}</strong><small>最近可用报价距今：${esc(ageText||"连接中")}</small>${detail}<small>推送重连 ${feed?.reconnections||0} 次 · 官方盘口补齐 ${feed?.rest_successes||0} 次 · 保留 3 秒新鲜度检查</small></div>`;
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
    el("equity-chart-title").textContent=live?"真实钱包总资产曲线 · 近7天":"资金曲线";el("market-panel-title").textContent=live?"实时市场 · 实盘行情":"实时市场 · 原主账户";
    const curve=walletCurve(data),range=el("managed-chart-range");range.hidden=!live;
    if(live){const format=t=>new Date(t*1000).toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});
      range.textContent=curve.length?`实际记录：${format(curve[0].created_at)} — ${format(curve[curve.length-1].created_at)}${root.wallet_curve_meta?.first_recorded_at>Date.now()/1000-7*86400?" · 实盘记录尚不足7天":""}`:"近7天暂无真实钱包资产记录";}
    el("managed-chart-empty").hidden=!live||curve.length>0;el("equity-chart").hidden=!el("managed-chart-empty").hidden;
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
    controls(data);renderDataHealth(data);renderQuarantine(data);renderExecution(data);if(live)renderTables(data);
  }
  function metrics(data){const r=variant(data),w=state(data).wallet||{},positions=r?.open_trades||[];
    const summarize=pending=>{const rows=positions.filter(p=>Boolean(p.valuation?.pending_settlement)===pending);
      return {count:rows.length,pnl:rows.every(p=>p.unrealized_pnl!=null)?rows.reduce((sum,p)=>sum+Number(p.unrealized_pnl),0):null};};
    return {...(r?.metrics||{}),total_equity:w.total_equity,cash_balance:w.available,reserved_cash:w.reserved,
      valuation_split:{current:summarize(false),pending:summarize(true)}};}
  function renderMetrics(v){
    const fields=[[ids.totalEquity,"total_equity"],[ids.totalPnl,"total_pnl"],[ids.unrealizedPnl,"unrealized_pnl"],[ids.cashBalance,"cash_balance"],[ids.reservedCash,"reserved_cash"],[ids.openRisk,"open_risk"],[ids.winRate,"win_rate"],[ids.maxDrawdown,"max_drawdown"]];
    fields.forEach(([n,f])=>{const a=metricAnimations.get(n);if(a?.frame)cancelAnimationFrame(a.frame);metricAnimations.delete(n);delete n.dataset.metricReady;delete n.dataset.metricValue;const x=v[f];
      n.textContent=f==="win_rate"?x==null?"—":Number(x).toFixed(2)+"%":["total_pnl","unrealized_pnl"].includes(f)?pnl(x):amount(x);n.className=["total_pnl","unrealized_pnl"].includes(f)?x>0?"positive":x<0?"negative":"":"";});
    const note=valuationNote(v.valuation);
    document.querySelectorAll("[data-live-valuation-note]").forEach(n=>{n.hidden=false;n.textContent=v.valuation?.status==="empty"&&n.dataset.liveValuationNote==="total"?"已实现盈亏":note.text;n.title=note.detail;n.dataset.status=note.status;
      if(n.dataset.liveValuationNote==="unrealized"&&v.valuation_split?.pending.count){const split=v.valuation_split;
        n.innerHTML=`当前持仓 ${pnlHtml(split.current.pnl)} · ${split.current.count} 笔<br>待结算 ${pnlHtml(split.pending.pnl)} · ${split.pending.count} 笔（上次估值）`;
        n.title=note.detail+" 合计包含当前持仓和到期待结算持仓，按可卖出深度及买卖费用估算，与官方单个方向的持仓收益口径不同。";}
    });
  }
  function rows(data){return(state(data).variants||[]).map(r=>`<tr><td><strong>${esc(r.combo)}</strong></td><td>${esc(r.execution?.label||"正在读取状态")}</td><td>${amount(r.metrics.position_value)}</td><td>${pnlHtml(r.metrics.realized_pnl)}</td><td>${r.metrics.settled_trades}</td><td>${r.metrics.win_rate==null?"—":r.metrics.win_rate.toFixed(2)+"%"}</td><td>${r.metrics.open_trades}</td><td class="reason-cell">${esc(r.execution?.reason||r.last_signal?.reason||"等待实盘信号")}</td><td><button type="button" data-live-select="${r.variant_id}">查看实盘</button></td></tr>`).join("");}
  function renderScrollRows(id,html){
    const body=el(id),wrap=body.closest(".managed-table-scroll");
    if(!wrap){body.innerHTML=html;return;}
    const top=wrap.scrollTop,left=wrap.scrollLeft,viewport=wrap.getBoundingClientRect();
    const anchor=top>0?Array.from(body.rows).find(row=>row.getBoundingClientRect().bottom>viewport.top+50):null;
    const anchorKey=anchor?.dataset.rowKey,anchorTop=anchor?.getBoundingClientRect().top;
    if(body._renderedHtml!==html){
      const expanded=new Set(Array.from(body.querySelectorAll("details[open]")).map(n=>n.closest("tr")?.dataset.rowKey));
      body.innerHTML=html;body._renderedHtml=html;
      body.querySelectorAll("details").forEach(n=>{n.open=expanded.has(n.closest("tr")?.dataset.rowKey);});
    }
    const head=body.closest("table").tHead;
    const height=(head?.getBoundingClientRect().height||44)+Array.from(body.rows).slice(0,15).reduce((n,row)=>n+row.getBoundingClientRect().height,0)+2;
    if(height>2)wrap.style.maxHeight=Math.ceil(height)+"px";
    wrap.scrollTop=top;wrap.scrollLeft=left;
    if(anchorKey){const current=Array.from(body.rows).find(row=>row.dataset.rowKey===anchorKey);if(current)wrap.scrollTop+=current.getBoundingClientRect().top-anchorTop;}
  }
  function exitPriceHtml(p){
    const sale=p.sell_price==null?"":`<span class="managed-price-line">${moneyHtml(p.sell_price,"sell",4)}<small>卖出均价</small></span>`;
    const settlement=p.settlement_price==null?"":`<span class="managed-price-line"><span class="${p.settlement_price>0?"positive":"negative"}">${amount(p.settlement_price,4)}</span><small>官方结算价</small></span>`;
    return sale+settlement||'<span class="managed-money--muted">—</span>';
  }
  function renderTables(data){
    const r=variant(data);if(!r)return;const positions=r.open_trades||[],orders=r.orders||[],history=r.recent_trades||[];
    el("managed-position-account").textContent=r.combo;el("managed-position-count").textContent=positions.length+" 笔";
    renderScrollRows("managed-positions",positions.length?positions.map(p=>{const pending=orders.some(o=>o.position_id===p.id&&["PREPARED","SUBMITTING","PENDING","UNKNOWN","QUARANTINED"].includes(o.state));const expired=Number(p.round_id.split("-").pop())+300<=Date.now()/1000;
      const note=valuationNote(p.valuation);
      return `<tr data-row-key="p${p.id}"><td>${esc(p.round_id)}<br><strong>${esc(p.outcome)}</strong></td><td>${moneyHtml(p.entry_price,"buy",4)}</td><td>${moneyHtml(p.remaining_cost,"buy")}</td><td>${p.remaining_shares.toFixed(6)}</td><td>${moneyHtml(p.mark_value,"sell")}<small class="managed-valuation-note" data-status="${esc(note.status)}" title="${esc(note.detail)}">${esc(note.text)}</small></td><td>${pnlHtml(p.unrealized_pnl)}</td><td><button type="button" data-live-sell="${p.id}" data-live-owner="${r.variant_id}" ${pending||expired?"disabled":""}>${pending?"订单确认中":expired?"等待官方结算":"卖出"}</button></td></tr>`;}).join(""):'<tr><td colspan="7">当前策略没有真实持仓。不会展示 Paper 持仓。</td></tr>');
    renderScrollRows("managed-orders",orders.length?orders.map(o=>{let submission={};try{submission=JSON.parse(o.submission_json||"{}");}catch{}
      const details=submission.error_message?`<details><summary>提交错误详情（已脱敏）</summary>${esc(submission.error_message)}</details>`:"";
      const final=o.finalized_at?`<small class="managed-valuation-note">核对结束于 ${at(o.finalized_at)}${o.state==="REJECTED"?" · 预留已释放":" · 保留后续复查"}</small>`:"";
      const kind=o.action==="BUY"?"buy":"sell",avg=o.confirmed_shares>0?o.confirmed_notional/o.confirmed_shares:null;
      const noMatch=submission.error_message?.toLowerCase().startsWith("no orders found to match with fak order");
      const label=o.state==="REJECTED"&&noMatch?"未成交 · 无匹配盘口":stateLabel(o.state);
      return `<tr data-row-key="o${esc(o.id)}"><td>${at(o.created_at)}</td><td><span class="managed-money--${kind}">${o.action==="BUY"?"买入":"卖出"}</span> ${esc(o.outcome)}</td><td class="reason-cell"><span class="managed-order-state" data-order-state="${esc(o.state)}">${esc(label)}</span>${o.error?"<br>"+esc(o.error):""}${final}${details}</td><td>${o.action==="BUY"?moneyHtml(o.requested_cash,"buy"):o.requested_shares.toFixed(6)+" 份"}</td><td>${o.confirmed_shares.toFixed(6)}</td><td>${moneyHtml(avg,kind,4)}</td><td>${moneyHtml(o.confirmed_notional,kind)}</td><td>${moneyHtml(o.confirmed_fee,"fee",6)}</td><td title="${esc(o.ack_id||o.external_id)}">${esc((o.ack_id||o.external_id).slice(0,12))}…</td></tr>`;}).join(""):'<tr><td colspan="9">暂无本程序提交的实盘订单。</td></tr>');
    renderScrollRows("managed-history",history.length?history.map(p=>`<tr data-row-key="h${p.id}"><td>${at(p.opened_at)}</td><td>${esc(p.round_id)}<br>${esc(p.outcome)}</td><td>${esc(stateLabel(p.status))}</td><td>${moneyHtml(p.entry_price,"buy",4)}</td><td>${exitPriceHtml(p)}</td><td>${moneyHtml(p.total_cost,"buy")}</td><td>${pnlHtml(p.realized_pnl)}</td><td>${moneyHtml(p.claim_due,"claim")}</td></tr>`).join(""):'<tr><td colspan="8">暂无官方确认的实盘成交，实盘盈亏从实际成交开始统计。</td></tr>');
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
  setInterval(()=>{if(!latest||!state(latest).available)return;renderDataHealth(latest);renderExecution(latest);if(active(latest)&&typeof renderRuntime==="function")renderRuntime(latest.runtime);},1000);
  window.ManagedLiveUI={active,state,variant,execution,metrics,render,renderMetrics,rows,market,
    meta:d=>({label:(variant(d)?.combo||names[key()])+" · 实盘",source:"钱包余额与总资产共享；盈亏、持仓及胜率按当前策略统计"}),
    curve:walletCurve,
    currentPoint:d=>{const w=state(d).wallet||{};return w.total_equity==null?null:{created_at:w.checked_at,cash_balance:w.balance,total_equity:w.total_equity};}};
})();
