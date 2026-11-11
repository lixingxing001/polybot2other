/* Isolated research display. This file has no wallet/trading mutation requests. */
(() => {
  const safe=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money=v=>v==null||!Number.isFinite(Number(v))?'—':'$'+Number(v).toFixed(2);
  const tone=v=>v==null?'':Number(v)>0?'positive':Number(v)<0?'negative':'';
  const modeName=v=>({RECORD:'采集模式',PAPER:'独立 Paper',REPLAY:'历史回放'}[v]||v||'采集模式');
  const reasons={RECORD_ONLY:'采集模式：只记录，不下单',MODEL_UNFITTED:'校准模型尚未训练',VOL_WARMUP:'等待 600 秒有效波动样本',PROXY_UNCALIBRATED:'代理均价误差样本不足 120 组',CONTRACT_UNVERIFIED:'等待结算、费用和协议合同核验',CLOCK_UNVERIFIED:'等待时钟偏移核验',CLOCK_OFFSET:'时钟误差上界超过 250 毫秒',PRICE_MISSING:'等待官方价格',PRICE_STALE:'官方价格超过设计时效',BOOK_MISSING:'等待完整盘口',BOOK_STALE:'盘口超过 1 秒时效',BOOK_SKEW:'两侧盘口时间差超过 500 毫秒',RECOVERY_WARMUP:'重连后等待连续 10 秒健康数据',SHOCK_COOLDOWN:'价格冲击后冷却 60 秒'};
  const label=code=>reasons[code]||code;
  function row(data){
    if(!data?.enabled)return '';
    const economic=data.mode==='PAPER'||data.mode==='REPLAY',w=data.wallet||{},s=data.trade_stats||{};
    return `<tr class="hengzhun-strategy-row"><td><strong>衡准 V1${data.pilot?' · 试验':''}</strong></td><td>${safe(data.pilot?'现有数据 · 未校准 Paper':modeName(data.mode)+' · 独立研究')}</td><td>${economic?safe(money(w.nav)):'尚未启用'}</td><td class="${economic?tone(w.realized_pnl):''}">${economic?safe(money(w.realized_pnl)):'—'}</td><td>${safe(s.settled_count??0)}</td><td>${s.win_rate==null?'—':safe((Number(s.win_rate)*100).toFixed(1)+'%')}</td><td>${safe(w.open_risk_slots_used??0)}</td><td class="reason-cell">${safe(data.error||data.rules_error||(data.pilot?data.reason:null)||label((data.reason_codes||[]).find(x=>x!=='RECORD_ONLY')||'MODEL_UNFITTED'))}</td><td><button type="button" data-hengzhun-details>查看${data.pilot?'策略':'研究'}</button></td></tr>`;
  }
  function renderPilot(data){
    const seed=data.seed||{},w=data.wallet||{},prior=data.last_prior,time=x=>x?new Date(x).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}):'—';
    document.querySelector('#hengzhun-panel h2').textContent='衡准 V1 · 现有数据 Paper';
    document.getElementById('hengzhun-mode').textContent='未校准试验 · PAPER';
    document.getElementById('hengzhun-health').textContent=data.error||data.rules_error||data.reason||'独立 Paper 运行中';
    const cells=[['独立初始本金',money(w.initial_cash)],['已实现盈亏',money(w.realized_pnl),tone(w.realized_pnl)],['有成交 / 已完成',`${data.filled_markets||0} / ${data.completed_markets||0}`],['试验状态',data.running?'已启用 · 未校准':'未运行'],['已采用价格记录',Number(seed.price_rows||0).toLocaleString()],['历史有效收益片段',Number(seed.historical_return_count||0).toLocaleString()],['已估计均价误差样本',Number(seed.proxy_sample_count||0).toLocaleString()],['模型资格','未训练 · 未通过正式验证']];
    document.getElementById('hengzhun-metrics').innerHTML=cells.map(([k,v,c])=>`<div><span>${safe(k)}</span><strong class="${c||''}">${safe(v)}</strong></div>`).join('');
    document.getElementById('hengzhun-reasons').innerHTML=['独立模拟资金','现有数据先验，未经概率校准','S2：2 秒到达 / 25% 深度','原严格验收仍未通过'].map(x=>`<span class="hengzhun-reason">${safe(x)}</span>`).join('');
    document.getElementById('hengzhun-rules').innerHTML=[
      ['先验计算','沿用衡准终局 TWAP 数学；近期 120/600 秒波动，不足时使用本次已有历史方差，缺口不插值'],
      ['现有数据',`冻结截止 ${time(seed.created_at)}；均价偏差 ${Number(seed.proxy_bias_bps||0).toFixed(3)} bps，误差波动 ${Number(seed.proxy_sd_bps||0).toFixed(3)} bps`],
      ['提前试验','暂不要求 3,000 期训练、7 天验证和 bootstrap；先验减 0.02 作试验缓冲，不代表已校准胜率'],
      ['入场条件','剩余 30–90 秒；卖一 ≥ $0.20、限价 ≤ $0.70、价差 ≤ $0.02、最多追一个 tick；含费净优势 ≥ 0.03 且收益率 ≥ 5%'],
      ['持仓与预算','每笔含费 $5；最多 1 笔未决风险，最多 2 个含待领取市场；每期一次意图，零成交也不重试；持有至官方结果'],
      ['风险','24h 负损益＋在途/持仓风险＋新预算 ≤ $10；成本占用 ≤ $10；净值回撤 15% 停止新增'],
      ['数据试验口径','价格源年龄 ≤ 3 秒 / 接收 ≤ 2 秒；双侧偏差 ≤ 500ms；不再用 NTP 250ms 上界卡住本试验，未来价格仍拒绝'],
      ['成交与费用','只用提交后、到达时可见盘口的 25% 深度，允许部分成交；当前公开费率、保守现金加费及向上取整是假设，不等同实盘精确对账'],
      ['确认与结算','撮合后 12 秒确认，核验官方终局后 60 秒领取；不回填启动前的模拟盈利']
    ].map(([k,v])=>`<tr><th>${safe(k)}</th><td>${safe(v)}</td></tr>`).join('');
    document.getElementById('hengzhun-funnel').innerHTML=(data.failure_counts||[]).map(x=>`<tr><td>${safe(x.code)}</td><td>${safe(x.count)}</td></tr>`).join('')||'<tr><td colspan="2">开始记录试验决策</td></tr>';
    document.getElementById('hengzhun-detail').textContent=prior?`当前未校准先验：Up ${(prior.p_raw_up*100).toFixed(2)}%，Down ${(prior.p_raw_down*100).toFixed(2)}%。波动来源：${prior.volatility_source==='LIVE_CAUSAL_120_600_SECONDS'?'近期因果网格':'已有历史方差'}。这不是预测胜率保证。`:'等待实时价格与开盘参考；已有价格数据已加载。';
    document.getElementById('hengzhun-note').textContent=data.note;
    document.getElementById('hengzhun-account').hidden=false;
    document.getElementById('hengzhun-account-metrics').innerHTML=[['独立模拟净值',w.nav],['可用现金',w.cash_available],['预占资金',w.cash_reserved],['待领取金额',w.claim_receivable]].map(([k,v])=>`<div><span>${safe(k)}</span><strong>${safe(money(v))}</strong></div>`).join('');
    document.getElementById('hengzhun-shadow').textContent=`旧 RECORD 数据保留；本次 Paper 从独立 $100 开始。已完成 ${data.completed_markets||0} 笔，当前样本不代表稳定胜率。`;
    const phases={PENDING:'等待到达',MATCHED_WAIT:'撮合待确认',HOLD_TO_FINAL:'持有至终局',WAIT_RESULT:'等待官方结果',CLAIM_PENDING:'已结算待领取',COMPLETE:'已完成',RESULT_CONFLICT:'结果冲突待核验'};
    document.getElementById('hengzhun-trades').innerHTML=(data.groups||[]).map(g=>`<tr><td>${safe(time(g.created))}</td><td>${safe(g.market)}</td><td class="${g.selected_side==='Up'?'positive':'negative'}">${safe(g.selected_side)}</td><td>${safe(g.first_fill?phases[g.phase]||g.phase:g.order_status==='DONE'?'未成交':phases[g.phase]||g.phase)}<small class="pp-small">${safe(!g.first_fill?g.order_reason||'':'')}</small></td><td class="hengzhun-cash">${money(g.spent)}</td><td class="hengzhun-buy-price">${g.average_buy_price==null?'—':'$'+Number(g.average_buy_price).toFixed(4)}</td><td>${Number(g.buy_shares||0).toFixed(4)}</td><td class="${g.settlement_price==null?'':Number(g.settlement_price)>0?'positive':'negative'}">${g.settlement_price==null?'—':money(g.settlement_price)}</td><td class="${tone(g.pnl)}">${money(g.pnl)}</td></tr>`).join('')||'<tr><td colspan="9">Paper 已启用，满足当前行情和价值条件后自动提交模拟订单。</td></tr>';
    const body=document.getElementById('hengzhun-trades'),table=body.closest('table');
    table.tHead.rows[0].cells[4].textContent='已撮合投入';
    table.tHead.rows[0].cells[6].textContent='模拟份额';
    requestAnimationFrame(()=>{if(body.rows.length>15)body.closest('.table-wrap').style.maxHeight=Math.ceil(table.tHead.getBoundingClientRect().height+Array.from(body.rows).slice(0,15).reduce((n,r)=>n+r.getBoundingClientRect().height,0)+2)+'px';});
  }
  function render(data){
    const panel=document.getElementById('hengzhun-panel');if(!panel)return;
    panel.hidden=!data?.enabled;if(panel.hidden)return;
    if(data.pilot){renderPilot(data);return;}
    document.getElementById('hengzhun-mode').textContent=modeName(data.mode)+' · '+(data.mode||'RECORD');
    const counts=data.counts||{},wallet=data.wallet||{},config=data.config||{},frame=data.last_decision||{};
    document.getElementById('hengzhun-health').textContent=data.error||(!data.running?'研究服务未运行':data.mode==='RECORD'?'正在记录行情与缺失样本；正式 Paper 尚未启用':'独立模拟运行中');
    const cells=[['原始行情记录',Number(counts.raw_events||0).toLocaleString()],['已观察市场',data.independent_markets||0],['有效固定时点样本',data.landmarks?.PRESENT||0],['缺失固定时点样本',data.landmarks?.MISSING_OR_TOO_LATE||0],['已核验最终结果',counts.official_results||0],['校准模型',data.model_status==='FITTED'?'已拟合':'尚未训练'],['独立模拟初始本金',money(wallet.initial_cash||config.risk?.initial_paper_cash_usd||100)],['正式 Paper 状态',data.mode==='RECORD'?'尚未启用':data.research_status||'INSUFFICIENT']];
    document.getElementById('hengzhun-metrics').innerHTML=cells.map(([k,v])=>`<div><span>${safe(k)}</span><strong>${safe(v)}</strong></div>`).join('');
    const codes=[...new Set(data.reason_codes||[])];
    document.getElementById('hengzhun-reasons').innerHTML=codes.map(c=>`<span class="hengzhun-reason" title="${safe(c)}">${safe(label(c))}</span>`).join('');
    const entry=config.entry||{},risk=config.risk||{},cal=config.calibration||{};
    document.getElementById('hengzhun-rules').innerHTML=[
      ['入场窗口',`${entry.submit_remaining_min_seconds??30}–${entry.submit_remaining_max_seconds??90} 秒剩余；预计到达仍至少 25 秒`],
      ['价值门槛','保守净优势 ≥ 0.03，保守收益率 ≥ 5%；两侧分别评估'],
      ['价格限制','卖一 ≥ 0.20，限价 ≤ 0.70，价差 ≤ 0.02，最多追一个 tick'],
      ['预算与风险',`单笔含费 ${money(entry.budget_usd||5)}；最多 ${risk.max_open_risk_positions||1} 个未决市场；24h 亏损总额加风险 ≤ ${money(risk.gross_loss_24h_plus_risk_cap_usd||10)}`],
      ['持仓方式','每期最多一个意图，零成交也不重试；持有到官方最终结算，无固定止盈止损'],
      ['成本与领取','成本占用最多 $10，潜在待领取市场最多 2 个；有效净值回撤 15% 锁定新增'],
      ['模型最低样本',`${cal.min_train_markets||3000} 个训练市场 / ${cal.min_train_days||20} 个完整 UTC 日；7 天后移验证至少 ${cal.min_validation_markets||1000} 个市场`],
      ['执行情景','S1：1 秒 / 50% 深度；S2：2 秒 / 25%；S3：5 秒 / 25%。均为假设情景'],
      ['确认与领取','撮合后 12 秒确认；最终结果与交易核验同时完成后，60 秒模拟领取']
    ].map(([k,v])=>`<tr><th>${safe(k)}</th><td>${safe(v)}</td></tr>`).join('');
    document.getElementById('hengzhun-funnel').innerHTML=(data.failure_counts||[]).map(x=>`<tr><td>${safe(label(x.code))}</td><td>${safe(x.count)}</td></tr>`).join('')||'<tr><td colspan="2">等待采集</td></tr>';
    const prior=frame.prior,clock=data.clock||{};
    document.getElementById('hengzhun-detail').textContent=`研究结论：${data.research_status||'INSUFFICIENT'}。${prior?`未校准先验 Up ${(prior.p_raw_up*100).toFixed(2)}%，仅供研究；`:'尚无可用先验；'}时钟：${clock.status||'待核验'}${clock.offset_bound_ms!=null?'，偏移上界 '+Number(clock.offset_bound_ms).toFixed(1)+' ms':''}。记录队列 ${data.queue_size||0}，丢弃 ${data.dropped_events||0}。`;
    document.getElementById('hengzhun-note').textContent=data.note||'依据设计书独立实现，现有策略和实盘开关不控制本策略。';
    const account=document.getElementById('hengzhun-account');account.hidden=data.mode==='RECORD';
    if(!account.hidden){
      const funds=[['独立模拟净值',wallet.nav],['可用现金',wallet.cash_available],['预占资金',wallet.cash_reserved],['待领取金额',wallet.claim_receivable]];
      document.getElementById('hengzhun-account-metrics').innerHTML=funds.map(([k,v])=>`<div><span>${safe(k)}</span><strong>${safe(money(v))}</strong></div>`).join('');
      const shadow=data.signal_shadow;
      document.getElementById('hengzhun-shadow').textContent=shadow?`独立影子对照：${shadow.filled_markets||0} 个市场有假设成交，${shadow.settled_markets||0} 个市场已结算。每个市场独立模拟，不计入上方 $100 账户资金。`:'尚无影子成交记录。';
      const states={RESERVED:'已预占，待发送',SUBMITTING:'提交中',ACKNOWLEDGED:'已接受，等待撮合',UNKNOWN:'成交情况待核验',TERMINAL_NO_FILL:'已确认未成交',TERMINAL_WITH_FILL:'已成交',ABORTED_BEFORE_SEND:'发送前中止',REJECTED_CONFIRMED:'已确认拒绝',OPEN:'待结算',OPEN_UNRESOLVED:'已确认持仓，待结算',WINDOW_CLOSED:'市场结束，等待结果',RESULT_PENDING:'核验最终结果',SETTLEMENT_EXCEPTION:'结算修订待核验',RESOLVED_LOSS:'已结算亏损',CLAIM_PENDING:'待领取',CLOSED_CLAIMED:'已领取',CLOSED:'已结束'};
      document.getElementById('hengzhun-trades').innerHTML=(data.recent_intents||[]).map(o=>{
        const fills=(o.fills||[]).filter(f=>f.state==='CONFIRMED'),sum=k=>fills.reduce((a,f)=>a+Number(f.payload?.economics?.[k]||0),0);
        const net=sum('net_shares'),gross=sum('gross_shares'),notional=sum('cash_before_fees'),cash=sum('all_in_cash'),state=o.position_state||o.state;
        const settlement=o.recognized_at!=null&&net>0?Number(o.payout)/net:null;
        return `<tr><td>${safe(new Date(o.payload?.created_at).toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'}))}</td><td>${safe(o.plan?.market?.market_uid||o.condition_id)}</td><td class="${o.plan?.side==='UP'?'positive':'negative'}">${o.plan?.side==='UP'?'Up':'Down'}</td><td>${safe(states[state]||state)}</td><td class="hengzhun-cash">${fills.length?safe(money(cash)):'—'}</td><td class="hengzhun-buy-price">${gross>0?'$'+(notional/gross).toFixed(4):'—'}</td><td>${net>0?net.toFixed(4):'—'}</td><td class="${settlement==null?'':settlement>0?'positive':'negative'}">${settlement==null?'—':'$'+settlement.toFixed(4)}</td><td class="${tone(o.realized_pnl)}">${safe(money(o.realized_pnl))}</td></tr>`;
      }).join('')||'<tr><td colspan="9">还没有交易意图；上方会显示当前未满足的条件。</td></tr>';
      requestAnimationFrame(()=>{
        const body=document.getElementById('hengzhun-trades'),wrap=body?.closest('.table-wrap'),head=body?.closest('table')?.tHead;
        if(wrap&&body.rows.length>15)wrap.style.maxHeight=Math.ceil((head?.getBoundingClientRect().height||42)+Array.from(body.rows).slice(0,15).reduce((n,r)=>n+r.getBoundingClientRect().height,0)+2)+'px';
      });
    }
  }
  document.addEventListener('click',e=>{if(e.target.closest('[data-hengzhun-details]'))document.getElementById('hengzhun-panel')?.scrollIntoView({behavior:'smooth',block:'start'});});
  window.HengzhunUI={row,render};
})();
