from pathlib import Path
import base64
import datetime as dt
import html
import json
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
analysis = json.loads((ROOT / 'analysis.json').read_text())
trades = json.loads((ROOT / 'trade-details.json').read_text())
comparisons = json.loads((ROOT / 'comparisons.json').read_text())
extra = json.loads((ROOT / 'ledger-extra-checks.json').read_text())
TZ = ZoneInfo('Asia/Shanghai')
names = {'MAIN': '主账户', 'MAIN_OPTIMIZED': '优化版', 'MAIN_ADAPTIVE': '自适应成长版',
         'MAIN_OPTIMIZED_FAST': '极速版', 'MAIN_OPTIMIZED_PLUS': '进阶版'}
english = {'MAIN': 'Main', 'MAIN_OPTIMIZED': 'Optimized', 'MAIN_ADAPTIVE': 'Adaptive',
           'MAIN_OPTIMIZED_FAST': 'Fast', 'MAIN_OPTIMIZED_PLUS': 'Plus'}
colors = {'MAIN': '#087f5b', 'MAIN_OPTIMIZED': '#7950f2', 'MAIN_ADAPTIVE': '#d97706',
          'MAIN_OPTIMIZED_FAST': '#1971c2', 'MAIN_OPTIMIZED_PLUS': '#e03131'}
snapshot_ts = analysis['snapshot']['snapshot_finished_at']
snapshot = dt.datetime.fromtimestamp(snapshot_ts, TZ)
start_ts = min(v['account']['created_at'] for v in analysis['strategies'].values())
start = dt.datetime.fromtimestamp(start_ts, TZ)
total_pnl = sum(v['settled']['pnl'] for v in analysis['strategies'].values())

fig, ax = plt.subplots(figsize=(12.8, 5.4), layout='constrained')
for key, rec in analysis['strategies'].items():
    cumulative = 0
    xs, ys = [start], [0]
    for trade in sorted((r for r in trades[key] if r['status'] == 'SETTLED'), key=lambda r: (r['settled_at'], r['id'])):
        cumulative += trade['pnl']
        xs.append(dt.datetime.fromtimestamp(trade['settled_at'], TZ))
        ys.append(cumulative)
    xs.append(snapshot)
    ys.append(cumulative)
    ax.step(xs, ys, where='post', label=f'{english[key]}  {cumulative:+.2f}', color=colors[key], linewidth=1.9)
for revision, text in [(5, 'First observed revision 5 fill'), (10, 'First observed $10 fill')]:
    earliest = min(r['opened_at'] for group in trades.values() for r in group if r['config_revision'] == revision)
    x = dt.datetime.fromtimestamp(earliest, TZ)
    ax.axvline(x, color='#868e96', linewidth=.9, linestyle='--', alpha=.8)
    ax.text(x, .98, '  ' + text, transform=ax.get_xaxis_transform(),
            rotation=0, ha='right' if revision == 10 else 'left', va='top', color='#495057', fontsize=8)
ax.axhline(0, color='#adb5bd', linewidth=.8)
ax.set_ylabel('Cumulative realized P&L (USD)')
ax.set_xlabel('Asia/Shanghai (UTC+8); open positions excluded')
ax.set_title('Server Paper accounts: realized P&L since independent deployment', loc='left', fontsize=13)
ax.xaxis.set_major_locator(mdates.HourLocator(interval=4, tz=TZ))
ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M', tz=TZ))
ax.grid(axis='y', color='#e9ecef')
ax.spines[['top', 'right']].set_visible(False)
ax.legend(loc='lower left', ncol=2, frameon=False, fontsize=9)
fig.savefig(ROOT / 'pnl.png', dpi=160)
plt.close(fig)

rows = []
for key, rec in analysis['strategies'].items():
    s = rec['settled']
    rows.append(f"<tr><th>{names[key]}</th><td class='{'positive' if s['pnl'] >= 0 else 'negative'}'>${s['pnl']:+.2f}</td>"
                f"<td>{s['pnl']:+.2f}%</td><td>{s['wins']} / {s['losses']}</td><td>{s['win_rate']:.1%}</td>"
                f"<td>${s['fees']:.2f}</td><td>${rec['max_realized_drawdown']:.2f}</td>"
                f"<td>{len(rec['open'])} 笔 / ${rec['open_risk']:.2f}</td></tr>")
payoff_rows = []
phase_rows = []
for key, rec in analysis['strategies'].items():
    s = rec['settled']
    payoff_rows.append(f"<tr><th>{names[key]}</th><td>${s['avg_win']:.2f}</td><td>${s['avg_loss']:.2f}</td>"
                       f"<td>{s['win_rate']:.1%}</td><td>{s['empirical_breakeven']:.1%}</td><td>${s['gross_pnl']:+.2f}</td></tr>")
    phase_cells = []
    for revision in (0, 5, 10):
        phase = [v for k, v in rec['risk_revisions'].items() if k.startswith(f'({revision},')]
        if phase:
            n, wins, pnl = sum(v['n'] for v in phase), sum(v['wins'] for v in phase), sum(v['pnl'] for v in phase)
            phase_cells.append(f'<td>{n} 笔 / {wins} 胜 / ${pnl:+.2f}</td>')
        else:
            phase_cells.append('<td>无已结算交易</td>')
    phase_rows.append('<tr><th>' + names[key] + '</th>' + ''.join(phase_cells) + '</tr>')

both_rows = []
for key in ('MAIN', 'MAIN_OPTIMIZED'):
    for r in extra[key]['both_side_rounds']:
        state = f"已结算 ${r['actual_pnl']:+.2f}" if r['settled'] else '快照时未结算'
        both_rows.append(f"<tr><th>{names[key]}</th><td>{html.escape(r['opened'][0][5:19].replace('T', ' '))}</td>"
                         f"<td>${r['cost']:.2f}</td><td>${r['up_payout']:.2f}</td><td>${r['down_payout']:.2f}</td>"
                         f"<td>${r['worst_pnl']:+.2f} 至 ${r['best_pnl']:+.2f}</td><td>{state}</td></tr>")
chart = base64.b64encode((ROOT / 'pnl.png').read_bytes()).decode()
report = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>服务器 Paper 策略复盘 · 2026-11-18</title>
<style>
body{{font:16px/1.75 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;background:#f5f7fa;color:#17212f;margin:0}}
main{{max-width:1180px;margin:auto;padding:32px 24px 64px}}h1{{font-size:28px;margin:0 0 8px}}h2{{font-size:20px;margin:30px 0 12px}}
p{{margin:12px 0}}.muted{{color:#64748b;font-size:14px}}.card{{background:white;border:1px solid #dce2e9;border-radius:12px;padding:22px;margin:20px 0}}
.negative{{color:#c92a2a;font-weight:700}}.positive{{color:#087f5b;font-weight:700}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:14px;white-space:nowrap}}
th,td{{padding:11px 12px;border-bottom:1px solid #e9ecef;text-align:right}}th:first-child,td:first-child{{text-align:left}}thead{{background:#f1f4f8}}li{{margin:14px 0}}img{{width:100%;height:auto}}a{{color:#1971c2}}code{{font-size:13px}}@media print{{body{{background:white}}main{{padding:0}}.card{{break-inside:avoid}}}}
</style><main>
<h1>服务器 Paper 策略盈利复盘</h1>
<p class="muted">独立服务器账本；北京时间 {start:%Y-%m-%d %H:%M:%S} 至 {snapshot:%Y-%m-%d %H:%M:%S}，约 {(snapshot_ts-start_ts)/3600:.1f} 小时。每账户初始 $100，共 $500。只读复盘，未改策略或交易设置。</p>
<div class="card"><p><strong>这批模拟交易确实表现差：合计已实现亏损 ${-total_pnl:.2f}，相当于初始模拟本金的 {-total_pnl/500:.2%}。</strong>
进阶版最差，已亏掉其初始资金的 70.38%。主账户仅小幅盈利，且快照时已有一组正常到期必亏的双向持仓。现有样本不能证明任何一版具有稳定盈利能力。</p>
<div class="scroll"><table><thead><tr><th>策略</th><th>已实现净盈亏</th><th>初始资金收益率</th><th>胜 / 负</th><th>胜率</th><th>已结算交易手续费</th><th>已实现曲线最大回撤</th><th>未结算成本</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="muted">净盈亏已含买入手续费；不含未结算持仓浮动盈亏。回撤为已实现收益曲线从历史高点到后续低点的最大下降，未计持仓市值。主账户 60 笔、优化版 42 笔、自适应版 46 笔、极速版 68 笔、进阶版 66 笔。不同账户会交易同一 BTC 轮次，不是独立样本。</p></div>
<div class="card"><img alt="五个服务器 Paper 账户累计已实现盈亏曲线" src="data:image/png;base64,{chart}"><p class="muted">虚线标记订单记录里首次见到相应配置版本的成交时间，不代表准确的设置修改时间。曲线平台表示没有新增已结算收益，并不单独证明停机或风控阻止了全部交易。</p></div>
<div class="card"><h2>亏损主要来自哪里</h2><ol>
<li><strong>赢的次数不够覆盖一次亏损的金额。</strong>极速版胜率 61.8%，但赢一笔平均只赚 $2.61，输一笔平均亏 $4.78；按这批交易的平均盈亏，需要约 64.7% 的胜率才能打平。进阶版对应约 60.7%，实际只有 47.0%。这里的打平胜率是事后盈亏结构描述，不是可保证的未来门槛。</li>
<li><strong>进阶版的主要损失不是手续费。</strong>其未扣手续费结果已亏 $61.96，再加 $8.42 手续费得到净亏 $70.38。平均模型分数约 70.1%，实际胜率约 47.0%；分数 65%—70% 的 28 笔只赢 9 笔，净亏 $52.27。模型分数不是经本次样本验证的真实概率；这个事后切片只能提示校准问题，不能据此直接上调阈值并宣称优化成功。</li>
<li><strong>进阶版也没有在相同配置阶段胜过优化版。</strong>revision 5 阶段，优化版 38 笔净亏 $2.92，进阶版 35 笔净亏 $42.37；共同参与的 28 个轮次内，优化版 29 笔净亏 $17.95，进阶版 28 笔净亏 $30.05。两者至少有 2 个共同轮次买了相反方向，均为优化版盈利、进阶版亏损。交易选择和时机有差异，不能把全部差额直接归因于某一条过滤规则。</li>
<li><strong>自适应模型尚未接管决策。</strong>46 笔已结算交易都处于 <code>CONTEXT_LEARNING / BASELINE_SHADOW</code>，实际分数修正均为 0；理由是前向对照未达到独立市场数或观察时长。它仍按基线开仓，“成长版”名称不表示学习已改善收益。</li>
<li><strong>当前风险额度较宽，而且这五版不执行自动止盈止损退出。</strong>快照时有效配置 revision 10：每笔预算 $10、最多 2 笔持仓、滚动 24 小时净亏损限额 $100。每账户初始资金也是 $100，亏损限额自然不会在亏至 $70 时提前拦截。界面配置的止盈 20% / 止损 40% 仅在专用 <code>MAIN_TP_SL</code> 的退出逻辑使用，这五个账户主要持有至结果结算。大部分已发生亏损来自先前 $5 预算交易，不能归罪于刚加到 $10。</li>
</ol></div>
<div class="card"><h2>主账户还有明确的双向持仓锁损</h2>
<p>11 月 18 日 00:15:27 买入 Up，00:15:46 又买入同一轮 Down，总成本 $19.170350。若正常二选一到期结算，Up 胜收回 $14.593750，Down 胜收回 $15.311475，最终必亏 $3.86—$4.58。这一组持仓单独结算就足以抹掉快照时主账户的 $2.71 已实现盈利。主账户和优化版目前只限制同轮同方向重复买入，允许反方向再开仓；其余三版有每轮只开一次的限制。</p>
<p><strong>后续验证：</strong>00:29:36 再查服务器，该组已经按 Down 胜结算，净亏 $3.858875。下表和总收益表仍固定于 00:16:06 快照，未混入后续交易；核验结果保存在 <code>post-review-check.txt</code>。</p>
<div class="scroll"><table><thead><tr><th>策略</th><th>首次开仓时间</th><th>合计成本</th><th>Up 胜回款</th><th>Down 胜回款</th><th>正常到期盈亏范围</th><th>快照状态</th></tr></thead><tbody>{''.join(both_rows)}</tbody></table></div></div>
<div class="card"><h2>盈亏结构与配置阶段</h2>
<div class="scroll"><table><thead><tr><th>策略</th><th>平均盈利单</th><th>平均亏损单</th><th>实际胜率</th><th>按本样本盈亏比的打平胜率</th><th>不扣手续费的结果</th></tr></thead><tbody>{''.join(payoff_rows)}</tbody></table></div>
<p>按成交订单携带的配置版本分组：</p><div class="scroll"><table><thead><tr><th>策略</th><th>revision 0 · 预算 $5</th><th>revision 5 · 预算 $5</th><th>revision 10 · 预算 $10</th></tr></thead><tbody>{''.join(phase_rows)}</tbody></table></div>
<p class="muted">只有订单保存的配置版本和预算可回溯；配置事件表为空，无法确认亏损限额的准确修改时间、完整历史值或操作者。不同阶段时间和机会集合不同，不作因果比较。</p></div>
<div class="card"><h2>账目与运行核对</h2><ul>
<li>7 个 SQLite 文件通过在线 backup API 分别取得一致快照，采集窗口约 0.17 秒，完成于 00:16:06；不是跨库原子快照，逐文件 SHA-256 验证通过。五个交易账本均满足：已结算逐笔盈亏合计 = 账户已实现盈亏；单笔盈亏 = 回款 − 含手续费成本；二元胜方回款 = 持有份数。所有成交成本也与逐笔模拟 fill 的现金支出对齐。</li>
<li>所有账内已结算交易来源标记均为 POLYMARKET。另查 9 个轮次的官方 Gamma API 未取到历史条目；因此此次只确认账内来源标记及金额一致性，没有完成官方历史结果的独立二次核验。</li>
<li>手续费逐笔符合 <code>shares × 0.07 × price × (1-price)</code>，最大差异不超过 $0.0000005 的舍入量。规则与 <a href="https://docs.polymarket.com/trading/fees">Polymarket 官方费用说明</a>一致。约 $0.60 的买价加上此费用后，每份成本约 $0.6168，意味着即使赢 60% 也可能亏损。</li>
<li>五账户未成交取消订单的现金支出及手续费均为 0；已成交订单平均模拟到达时间约 1.05 秒，平均成交价比信号价低约 0.009—0.013。不能据此证明真实执行，但没有证据将本批亏损主要归因于平均滑点或取消重试扣款。</li>
<li>查询时容器 healthy、重启次数 0，五账户执行器没有 last_error；参考源 ready / verified，实盘 runtime 关闭。早前截图的 RuntimeError 不是这批已结算亏损的充分解释。网络质量可能影响机会选择，本次没有重放缺失盘口，因此不做“网络完全无影响”的结论。</li>
</ul></div>
<div class="card"><h2>建议的下一步</h2><p>优先处理同轮双向持仓可能锁损的问题，并明确界面止盈止损与实际执行范围；随后以固定小额 Paper 预算、固定配置、按独立市场划分的新时间段验证概率校准与净期望收益。进阶版暂时没有继续加码的证据，自适应版先验证前向对照何时满足放行条件。</p>
<p>历史回放可用于定位问题，不能反复挑选本次亏损样本上的最好阈值后直接宣称策略有效。主账户的微小已实现利润同样不构成稳定优势。此次只做复盘，所有策略和风控设置保持原状。</p></div>
<p class="muted">证据文件：同目录 <a href="trades.csv">trades.csv</a>、analysis.json、trade-details.json、comparisons.json、ledger-extra-checks.json、risk-settings-history.json，以及 snapshot/manifest.json。源代码指向本次服务器已部署版本，取样时与本机相应文件哈希一致。所有金额为 Paper 模拟资金。</p>
</main></html>'''
(ROOT / 'report.html').write_text(report, encoding='utf-8')
print(json.dumps({'report': str(ROOT / 'report.html'), 'chart': str(ROOT / 'pnl.png'),
                  'total_realized_pnl': round(total_pnl, 6), 'snapshot': snapshot.isoformat(),
                  'report_bytes': len(report.encode())}, ensure_ascii=False))
