/* V2 shares the presentation component, never V1's money or controls. */
(() => {
  const original=document.getElementById('paired-paper-panel');if(!original)return;
  const panel=original.cloneNode(true);panel.id='paired-v2-panel';panel.classList.add('pp-v2');
  panel.querySelectorAll('[id]').forEach(n=>{n.id=n.id.replace(/^pp-/,'pp2-');});
  panel.querySelector('h2').textContent='双翼配对 V2';
  panel.querySelector('.pp-title p').textContent='先预留风险 · 独立退出通道 · 双情景验证';
  panel.querySelector('.pp-status-line > span').textContent='无需训练 · 独立前向试验';
  panel.querySelector('#pp2-loss').parentElement.firstChild.textContent='24小时负损益额度（$）';
  panel.querySelector('#pp2-fills').closest('.table-wrap').previousElementSibling.querySelector('span').textContent='主账本成交事件与主动退出；穿价假设另记压力账本';
  const overview=document.createElement('div');overview.id='pp2-risk';overview.className='pp-v2-risk';
  panel.querySelector('.pp-metrics').after(overview);
  const stress=document.createElement('div');stress.id='pp2-stress';stress.className='pp-v2-stress';overview.after(stress);
  const compare=document.createElement('div');compare.id='pp2-comparison';compare.className='pp-v2-stress';stress.after(compare);
  const exits=document.createElement('div');exits.id='pp2-exits';exits.className='pp-v2-exits';compare.after(exits);
  panel.querySelector('.pp-rules').innerHTML=`
    <li>主账本只按实际 SELL 成交事件模拟排队成交；压力账本加入严格穿价假设。两套账户各自从 $100 开始，收益不能相加。</li>
    <li>开盘30秒后至剩余120秒报价。需约2秒连续、平稳的双侧盘口，价差不超过$0.03，报价处于$0.30–$0.70。</li>
    <li>双侧各退至少一个 tick，总配对报价不高于$0.96。还要检查退出成本与配对价差的关系；这是未验证的筛选假设。</li>
    <li>下单前：过去24小时负损益总额＋持仓/在途/未确认风险＋新组预算，不得超过日额度；盈利不抵扣亏损。本轮相对初始本金也保留$10损失空间，不因跨日或重启重置。</li>
    <li>报价4秒到期，盘口移动会提前撤单；单边成交约2秒后进入管理，确认后的可用部分可先退出，不等待其他零碎成交全部确认。</li>
    <li>退出独立核验 CLOB 资产、费用与盘口。无法配平时尝试退出，失败或风险扩大后放宽卖出限价至最小 tick，但仍只按到达时实际买盘和深度成交；不能保证固定止损价。</li>
    <li>低于真实最小份额的残余仍需等待终局。行情缺口、未知估值、主账本与压力结果分歧均单独展示，不修改旧版亏损记录。</li>`;
  original.before(panel);
  const archive=document.createElement('details');archive.id='paired-v1-history';archive.dataset.pairedArchive='true';archive.className='pp-v1-history';
  const summary=document.createElement('summary');summary.id='pp-v1-summary';summary.textContent='双翼 V1 历史对照（原记录保留）';archive.append(summary);
  original.before(archive);archive.append(original);
  const decorate=(d,h)=>{
    const risk=d.risk||{},s=d.stress||{},r=s.risk||{};
    document.getElementById('pp2-risk').innerHTML=`<div class="pp-v2-section-title">风险预算 · 下单前先占用</div><div class="pp-v2-risk-grid">${[
      ['24h 已实现亏损',risk.gross_loss_24h],['未决风险',risk.unresolved_risk],['下一组预算',risk.next_budget],['可新增风险',risk.available]
    ].map(([k,v])=>`<div><span>${h.esc(k)}</span><strong>${h.usd(v)}</strong></div>`).join('')}</div><p>日额度 ${h.usd(risk.limit)} · 本轮损失空间 ${h.usd(risk.experiment_limit)} · ${risk.new_group_allowed?'风险预算允许新增；仍需满足行情与报价条件':'暂不新增风险，已有持仓继续处理'}</p>`;
    const oldStress=document.getElementById('pp2-stress');const stressOpen=Boolean(oldStress.querySelector('details')?.open);const stressScroll=oldStress.querySelector('.pp-scroll')?.scrollTop||0;
    const disagree=Number(d.realized_pnl||0)*Number(s.realized_pnl||0)<0 || (Number(d.realized_pnl||0)>=0 && Number(s.realized_pnl||0)<0);
    document.getElementById('pp2-stress').innerHTML=`<div class="pp-v2-section-title">独立穿价压力账本 <span>各 $100，不合并收益</span></div><div class="pp-v2-risk-grid"><div><span>主账本已实现</span><strong class="${h.tone(d.realized_pnl)}">${h.signed(d.realized_pnl)}</strong></div><div><span>压力账本已实现</span><strong class="${h.tone(s.realized_pnl)}">${h.signed(s.realized_pnl)}</strong></div><div><span>压力账本净值</span><strong>${h.usd(s.nav)}</strong></div><div><span>压力可新增风险</span><strong>${h.usd(r.available)}</strong></div></div><p class="${disagree?'pp-amber':''}">${disagree?'两个执行情景盈亏方向不同，尚不能宣称策略改善。':'策略收益尚未验证；须同时观察两套情景，不能只看主账本。'} 主账本 ${d.completed_markets||0} 个完成市场 · 压力 ${s.completed_markets||0} 个完成市场。累计模拟投入：主 ${h.usd(d.turnover)} / 压力 ${h.usd(s.turnover)}；交易量不同，不能只比盈利金额。</p><details><summary>压力账本状态与最近市场</summary><p>${h.esc(s.reason||'连接中')}</p><div class="table-wrap pp-scroll"><table class="trade-table"><thead><tr><th>市场</th><th>阶段</th><th>已实现</th><th>剩余份额</th></tr></thead><tbody>${(s.groups||[]).slice(0,30).map(g=>`<tr><td>${h.esc(g.market)}</td><td>${h.esc(h.state[g.phase]||g.phase)}</td><td class="${h.tone(g.pnl)}">${h.signed(g.pnl)}</td><td>U ${h.esc(g.qty.Up)} / D ${h.esc(g.qty.Down)}</td></tr>`).join('')||'<tr><td colspan="4">尚无压力情景成交</td></tr>'}</tbody></table></div></details>`;
    oldStress.querySelector('details').open=stressOpen;oldStress.querySelector('.pp-scroll').scrollTop=stressScroll;
    const comparison=d.comparison||{};
    document.getElementById('pp2-comparison').innerHTML=`<div class="pp-v2-section-title">与主账户优化版的同期对照 <span>起点 ${h.esc(h.time(comparison.started_at))}</span></div><div class="table-wrap"><table class="trade-table"><thead><tr><th>账户 / 情景</th><th>已完成记录</th><th>实际模拟投入</th><th>已实现盈亏</th><th>投入收益率</th></tr></thead><tbody>${(comparison.rows||[]).map(x=>`<tr><td>${h.esc(x.name)}</td><td>${x.available?x.completed:'—'}</td><td>${x.available?h.usd(x.spent):'—'}</td><td class="${h.tone(x.pnl)}">${x.available?h.signed(x.pnl):'—'}</td><td>${x.roi==null?'—':(Number(x.roi)*100).toFixed(2)+'%'}</td></tr>`).join('')}</tbody></table></div><p>${h.esc(comparison.scope||'等待对照数据')}</p>`;
    const statuses=[...(d.exit_statuses||[]).map(x=>({...x,scenario:'主账本'})),...(s.exit_statuses||[]).map(x=>({...x,scenario:'压力'}))];
    document.getElementById('pp2-exits').innerHTML=`<div class="pp-v2-section-title">退出管理通道 <span>${d.exit_worker_running?'独立运行中':'尚未运行'}</span></div>${statuses.length?statuses.map(x=>`<p><b>${h.esc(x.scenario)}</b> ${h.esc(x.market)} · ${h.esc(x.reason||'正在核对')}</p>`).join(''):'<p>当前没有需要处理的单边份额。</p>'}${Object.entries(d.exit_errors||{}).map(([k,v])=>`<p class="pp-amber">${h.esc(v)}</p>`).join('')}`;
    if(d.rules_error&&d.active_markets)document.getElementById('pp2-reason').textContent=`新开仓：${d.rules_error}；已有持仓由下方独立退出通道继续处理。`;
    const old=window.__pairedV1Snapshot;
    if(old)summary.textContent=`双翼 V1 · 历史已冻结 · 已实现 ${h.signed(old.realized_pnl)} · ${old.completed_markets||0} 个完成市场（展开查看）`;
  };
  window.PairedPaperV2UI=window.createPairedPaperView({prefix:'pp2-',panelId:'paired-v2-panel',name:'双翼配对 V2',role:'风险预算 / 双情景 Paper',detailsAttr:'data-pp2-details',settingsUrl:'/api/paired-v2/settings',emptyFillsMessage:'尚无主账本模拟成交。仅按实际成交事件消耗队列；穿价假设单独计入压力账本。',decorate});
})();
