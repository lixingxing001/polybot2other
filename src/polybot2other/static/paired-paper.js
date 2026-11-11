/* Paired Paper has its own controls and money. It never calls live-order APIs. */
function createPairedPaperView(options = {}) {
  let last=null,saving=false,dirty=false;
  const prefix=options.prefix||'pp-', panelId=options.panelId||'paired-paper-panel', detailsAttr=options.detailsAttr||'data-pp-details';
  const element=id=>document.getElementById(id==='paired-paper-panel'?panelId:id.replace(/^pp-/,prefix));
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num=v=>v==null||!Number.isFinite(Number(v))?null:Number(v);
  const usd=(v,d=2)=>num(v)==null?'—':`${Number(v)<0?'−':''}$${Math.abs(Number(v)).toFixed(d)}`;
  const signed=v=>num(v)==null?'—':`${Number(v)>0?'+':''}${usd(v)}`;
  const tone=v=>num(v)==null?'':Number(v)>0?'positive':Number(v)<0?'negative':'';
  const time=v=>v?new Date(v).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):'—';
  const state={QUOTING:'双边报价',MATCHED_WAIT:'配对已撮合 · 待确认',MANAGING:'管理持仓',HEDGING:'补齐配对',EXITING:'退出单边',UNPAIRED:'单边待处理',SMALL_TAIL:'小额余量待终局',WAIT_RESULT:'等待终局',CLAIM_PENDING:'配对完成 · 待领取',COMPLETE:'已完成',RESULT_CONFLICT:'结果冲突待核对',PENDING:'传输中',OPEN:'挂单排队',CANCELING:'撤单传输中',DRAINING:'等待撤单前成交消息',DONE:'已结束',MATCHED:'撮合待确认',CONFIRMED:'已确认'};
  const title={ORDER:'模拟委托',ARRIVAL:'委托到达',MATCH:'观察到假设成交',CONFIRM:'模拟确认',HEDGE:'尝试配平',EXIT:'退出单边',PAIR_MERGE:'配对合并',FINAL:'终局结算',CLAIM:'模拟资金到账',RECOVERY:'重启恢复',DATA_GAP:'行情中断',CROSS_EVIDENCE:'卖盘穿价依据',RULES:'市场参数已核对',RESULT:'收到官方结果',RESULT_WAIT:'等待结果',DECISION:'策略状态',SETTINGS:'设置已保存'};
  function row(d){
    if(!d?.enabled)return '';
    return `<tr class="pp-strategy-row"><td><strong>${esc(d.name||options.name||'双翼配对 V1')}</strong><small class="pp-small">${d.frozen?'历史对照 · 已冻结':'无需训练 · 独立 Paper'}</small></td><td>${esc(d.frozen?'历史记录保留':options.role||'配对做市 / 单边退出')}</td><td>${esc(usd(d.nav))}</td><td class="${tone(d.realized_pnl)}">${esc(signed(d.realized_pnl))}</td><td>${d.completed_markets||0}</td><td>${d.win_rate==null?'—':(d.win_rate*100).toFixed(1)+'%'}</td><td>${d.active_markets||0}</td><td class="reason-cell">${esc(d.error||d.rules_error||d.reason||'连接中')}</td><td><button type="button" ${detailsAttr}>查看策略</button></td></tr>`;
  }
  function metric(label,value,klass='',note=''){
    return `<div class="pp-metric"><span>${esc(label)}</span><strong class="${klass}">${esc(value)}</strong>${note?`<small>${esc(note)}</small>`:''}</div>`;
  }
  function curve(data){
    const el=element('pp-curve'),rows=data.curve||[];
    const valid=rows.filter(x=>num(x.nav)!=null);
    if(valid.length<2){el.innerHTML='<div class="pp-chart-empty">运行后持续记录净值；未成交时保持初始本金。</div>';return;}
    const width=900,height=165,pad=25,start=rows[0].at,end=rows[rows.length-1].at;
    const values=valid.map(x=>Number(x.nav)),lo=Math.min(100,...values),hi=Math.max(100,...values),gap=Math.max(.3,(hi-lo)*.15);
    const min=lo-gap,max=hi+gap,x=t=>pad+(t-start)/Math.max(1,end-start)*(width-2*pad),y=v=>height-pad-(v-min)/(max-min)*(height-2*pad);
    let segments=[],current=[];
    for(const r of rows){if(num(r.nav)==null){if(current.length)segments.push(current);current=[];}else current.push(`${x(r.at).toFixed(1)},${y(Number(r.nav)).toFixed(1)}`);}
    if(current.length)segments.push(current);
    el.innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="双翼配对独立模拟账户净值，缺失估值处断开"><line x1="${pad}" x2="${width-pad}" y1="${y(100)}" y2="${y(100)}" class="pp-chart-baseline"/>${segments.map(p=>`<polyline points="${p.join(' ')}" class="pp-chart-line ${values[values.length-1]<100?'is-loss':''}"/>`).join('')}<text x="${pad}" y="15">${esc(usd(hi))}</text><text x="${pad}" y="${height-5}">${esc(usd(lo))}</text></svg><div class="pp-chart-times"><span>${esc(time(start))}</span><span>${esc(time(end))}</span></div>`;
  }
  function tableHeight(){
    for(const wrap of document.querySelectorAll('.pp-scroll')){
      const table=wrap.querySelector('table'),body=table?.tBodies[0];if(!body||body.rows.length<15)continue;
      wrap.style.maxHeight=Math.ceil((table.tHead?.getBoundingClientRect().height||40)+Array.from(body.rows).slice(0,15).reduce((a,r)=>a+r.getBoundingClientRect().height,0)+2)+'px';
    }
  }
  function render(d){
    const panel=element('paired-paper-panel');if(!panel)return;
    panel.hidden=!d?.enabled;if(panel.hidden)return;last=d;
    const cfg=d.settings||{},blocked=Boolean(d.error||d.rules_error),paused=cfg.enabled===false||Boolean(d.external_pause);
    panel.dataset.state=blocked?'error':paused?'paused':'active';
    element('pp-state').textContent=d.frozen?'历史对照 · 已冻结':blocked?'需要处理':!d.running?'服务未运行':paused?'暂停新报价':'PAPER 自动运行';
    element('pp-reason').textContent=d.error||d.rules_error||d.reason||'正在连接';
    const estimate=d.nav==null?d.last_nav:null;
    element('pp-metrics').innerHTML=[
      metric('独立模拟净值',estimate?'≈ '+usd(estimate.value):usd(d.nav),tone(d.total_pnl),estimate?'上次有效估值 '+time(estimate.at):'初始本金 '+usd(d.initial_cash)),
      metric('已实现盈亏',signed(d.realized_pnl),tone(d.realized_pnl),'已退出 / 已确认配对 / 已结算'),
      metric('可用现金',usd(d.available_cash),'','委托预占 '+usd(d.reserved)),
      metric('待领取模拟资金',usd(d.claim_receivable),'pp-amber','已确认后等待 60 秒到账'),
      metric('有成交市场',d.filled_markets||0,'',`已完成 ${d.completed_markets||0} · 管理中 ${d.active_markets||0}`),
      metric('完成市场盈利占比',d.win_rate==null?'—':(d.win_rate*100).toFixed(1)+'%',tone(d.win_rate), `共 ${d.completed_markets||0} 个完成市场；不是稳定胜率`)].join('');
    const decision=d.last_decision,pair=decision?.prices;
    element('pp-pair').innerHTML=pair?`<div><span>Up 最近委托价</span><strong class="pp-blue">${usd(pair.Up,3)}</strong></div><b>＋</b><div><span>Down 最近委托价</span><strong class="pp-violet">${usd(pair.Down,3)}</strong></div><b>＝</b><div><span>目标配对成本 / 份</span><strong>${usd(decision.pair_cost,3)}</strong></div><div class="pp-pair-note">每边 ${Number(decision.quantity).toFixed(2)} 份<br>两边均成交时，本组毛差价约 <b>${usd(decision.potential_pair_profit,3)}</b><br><small>尚未配齐时仍承担单边风险</small></div>`:'<p>正在等待当前市场和两侧盘口。无需模型训练或历史样本准入。</p>';
    element('pp-health').innerHTML=[['实时成交流',(d.stats?.prints_valid||0)+' 条有效事件'],['模拟成交',(d.fills||[]).length+' 条最近记录'],['队列',d.queue_size||0],['采集丢弃',d.dropped_events||0]].map(([a,b])=>`<span><i></i>${esc(a)} <b>${esc(b)}</b></span>`).join('');
    element('pp-quality').textContent=d.quality_affected_markets?`${d.quality_affected_markets} 个市场经历重连或数据中断，记录保留并标记；不能把这些记录当作完整成交验证。`:'实际行情驱动的 Paper：排队、参与率与延迟为模拟假设，未接入真实下单。';
    const gs=d.groups||[];
    element('pp-groups').innerHTML=gs.map(g=>`<tr><td>${esc(time(g.created))}<small class="pp-small">${esc(g.market)}</small></td><td>${esc(state[g.phase]||g.phase)}${!g.quality_ok?'<small class="pp-small pp-amber">存在数据中断</small>':''}</td><td class="pp-blue">${usd(g.spent)}</td><td>${Number(g.paired).toFixed(4)}</td><td><span class="pp-blue">U ${Number(g.qty.Up).toFixed(4)}</span> / <span class="pp-violet">D ${Number(g.qty.Down).toFixed(4)}</span></td><td class="pp-amber">${usd(g.fees,4)}</td><td class="${tone(g.pnl)}">${signed(g.pnl)}</td><td>${usd(g.claim)}</td></tr>`).join('')||`<tr><td colspan="8" class="pp-empty">独立账户已就绪。进入开盘 ${esc(cfg.entry_start_seconds??15)} 秒至剩余 ${esc(cfg.entry_end_remaining_seconds??60)} 秒的窗口后，按实时盘口和策略条件提交模拟报价。</td></tr>`;
    element('pp-fills').innerHTML=(d.fills||[]).map(f=>`<tr><td>${esc(time(f.matched_at))}</td><td>${f.action==='BUY'?'买入':'卖出'} <span class="${f.side==='Up'?'pp-blue':'pp-violet'}">${esc(f.side)}</span><small class="pp-small">${f.kind==='MAKER'?(f.source==='ASK_THROUGH_CONSERVATIVE'?'卖盘穿价 · 保守假设':'成交事件 · 排队模拟'):f.kind==='HEDGE'?'主动补齐':'单边退出'}</small></td><td class="pp-blue">${usd(f.price,4)}</td><td>${Number(f.quantity).toFixed(4)}</td><td>${usd(f.cash,4)}</td><td class="pp-amber">${usd(f.fee,4)}</td><td>${esc(state[f.status]||f.status)}</td></tr>`).join('')||`<tr><td colspan="7" class="pp-empty">${esc(options.emptyFillsMessage||'尚无模拟成交。同价触及不算成交；真实成交事件消耗队列，严格卖盘穿价另作保守撮合。')}</td></tr>`;
    element('pp-orders').innerHTML=(d.orders||[]).map(o=>`<tr><td>${esc(time(o.created))}</td><td>${o.action==='BUY'?'买入':'卖出'} ${esc(o.side)}</td><td class="pp-blue">${usd(o.price,4)}</td><td>${Number(o.quantity).toFixed(4)}</td><td>${Number(o.filled).toFixed(4)}</td><td>${esc(state[o.status]||o.status)}<small class="pp-small">${esc(o.terminal_reason||'')}</small></td></tr>`).join('')||'<tr><td colspan="6" class="pp-empty">尚无委托</td></tr>';
    element('pp-events').innerHTML=(d.events||[]).slice(0,12).map(e=>`<li><time>${esc(time(e.at))}</time><span>${esc(title[e.kind]||e.kind)}</span><small>${esc(e.message||e.code||'')}</small></li>`).join('');
    if(!dirty&&!saving){element('pp-budget').value=cfg.budget||'5';element('pp-max').value=cfg.max_markets||5;element('pp-loss').value=cfg.daily_loss_limit||10;element('pp-enabled').checked=cfg.enabled!==false;}
    if(d.frozen){for(const input of panel.querySelectorAll('input'))input.disabled=true;element('pp-save').disabled=true;}
    if(options.decorate)options.decorate(d,{element,usd,signed,tone,esc,time,state});
    curve(d);requestAnimationFrame(tableHeight);
  }
  document.addEventListener('click',async e=>{
    if(e.target.closest(`[${detailsAttr}]`)){const panel=element('paired-paper-panel');const archive=panel?.closest('[data-paired-archive]');if(archive)archive.open=true;panel?.scrollIntoView({behavior:'smooth',block:'start'});}
    if(!e.target.closest('#'+prefix+'save')||saving||!last?.control_token)return;
    const button=element('pp-save'),message=element('pp-save-message');saving=true;button.disabled=true;message.textContent='正在保存…';
    try{
      const response=await fetch(options.settingsUrl||'/api/paired-paper/settings',{method:'POST',headers:{'Content-Type':'application/json','X-Paired-Control':last.control_token},body:JSON.stringify({enabled:element('pp-enabled').checked,budget:element('pp-budget').value,max_markets:Number(element('pp-max').value),daily_loss_limit:element('pp-loss').value})});
      const data=await response.json();if(!response.ok)throw Error(data.error||'保存失败');dirty=false;render(data);message.textContent='已保存，仅作用于双翼 Paper；已有持仓继续管理。';
    }catch(err){message.textContent=err.message;}finally{saving=false;button.disabled=false;}
  });
  document.addEventListener('input',e=>{if(e.target.closest('#'+prefix+'settings'))dirty=true;});
  return {row,render};
}
window.createPairedPaperView=createPairedPaperView;
window.PairedPaperUI=createPairedPaperView();
