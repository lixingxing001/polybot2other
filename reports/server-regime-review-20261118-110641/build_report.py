from pathlib import Path
import base64
import html
import json
import math

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np

ROOT=Path(__file__).resolve().parent
summary=json.loads((ROOT/'regime-summary.json').read_text())
candidates=json.loads((ROOT/'candidate-checks.json').read_text())
paths=json.loads((ROOT/'path-diagnostics.json').read_text())['summary']
scores=json.loads((ROOT/'score-groups.json').read_text())
font_manager.fontManager.addfont('/System/Library/Fonts/Supplemental/Arial Unicode.ttf')
plt.rcParams['font.family']='Arial Unicode MS'
plt.rcParams['axes.unicode_minus']=False
phases=['evening','night','morning']
labels=['11/17 18–24 点','11/18 00–06 点','11/18 06–10:53']
data=[summary['by_decision_period'][p]['all'] for p in phases]
x=np.arange(3)
fig,axes=plt.subplots(1,2,figsize=(12.8,5.0),layout='constrained')
ax=axes[0]
series=[('实际胜率',[s['win_rate']*100 for s in data],'#087f5b'),
        ('平均模型分数',[s['features']['confidence']['mean']*100 for s in data],'#4263eb'),
        ('平均含费每份成本',[s['features']['all_in_cost']['mean']*100 for s in data],'#868e96')]
for i,(name,values,color) in enumerate(series):
    bars=ax.bar(x+(i-1)*.23,values,.22,label=name,color=color)
    ax.bar_label(bars,labels=[f'{v:.1f}' for v in values],fontsize=9,padding=3)
ax.set_xticks(x,labels,fontsize=10);ax.set_ylim(0,103);ax.set_ylabel('百分数 / 每份成本美分')
ax.set_title('命中率明显上升，模型分数和成本变化较小',fontsize=12)
ax.legend(loc='lower left',fontsize=9,framealpha=.95)
ax=axes[1]
values=[s['roi_per_trade']*5 for s in data]
bars=ax.bar(x,values,.5,color=['#e03131','#087f5b','#087f5b'])
ax.bar_label(bars,labels=[f'${v:+.2f}' for v in values],padding=5,fontsize=11)
ax.axhline(0,color='#adb5bd',lw=.9);ax.set_xticks(x,labels,fontsize=10)
ax.set_ylim(-.75,2);ax.set_ylabel('每笔净盈亏（美元）')
ax.set_title('把每笔实际成本统一折算为 $5，改善仍然存在',fontsize=12)
for ax in axes:
    ax.spines[['right','top']].set_visible(False)
    ax.set_axisbelow(True);ax.grid(axis='y',alpha=.2)
fig.suptitle('服务器 Paper：按开仓时刻比较，已结算结果截至 2026-11-18 10:53',fontsize=13)
fig.savefig(ROOT/'regime-comparison.png',dpi=160);plt.close(fig)

main_rows=[]
for label,s in zip(labels,data):
    main_rows.append(f"<tr><th>{label}</th><td>{s['n']}</td><td>{s['unique_rounds']}</td><td>{s['win_rate']:.1%}</td>"
                     f"<td>{s['features']['confidence']['mean']:.1%}</td><td>${s['features']['entry_price']['mean']:.3f}</td>"
                     f"<td>{s['features']['submission_edge']['mean']:.2%}</td><td>${s['roi_per_trade']*5:+.2f}</td></tr>")
rule_names={'baseline':'原交易全集','momentum60_same_direction':'近 60 秒动量与买入同向',
            'short_vol_not_above_15min':'近 2 分钟波动不高于近 15 分钟',
            'round_crossings_at_most_one':'本轮已知跨基准次数 ≤1',
            'score_at_least_075':'模型分数 ≥0.75',
            'submission_edge_at_least_005':'下单前含费模型优势 ≥0.05',
            'last20_settled_rounds_profitable':'此前 20 个已结算独立市场平均收益为正'}
rule_rows=[]
for name,rule in candidates['rules'].items():
    cells=[]
    for phase in ['earlier','evening','night','morning']:
        v=rule[phase]['all']
        cells.append(f"<td>{v['n']} 单 / {v['markets']} 轮<br><strong>{v['mean_roi']:+.1%}</strong></td>")
    rule_rows.append('<tr><th>'+rule_names[name]+'</th>'+''.join(cells)+'</tr>')
score_rows=[]
for label,phase in zip(labels,phases):
    low=scores[phase]['below_075'];high=scores[phase]['at_least_075']
    score_rows.append(f"<tr><th>{label}</th><td>{low['wins']} / {low['n']}（{low['win_rate']:.1%}）</td>"
                      f"<td>{high['wins']} / {high['n']}（{high['win_rate']:.1%}）</td></tr>")
image=base64.b64encode((ROOT/'regime-comparison.png').read_bytes()).decode()
report=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>昨晚盈利改善与可提前识别性分析</title><style>
body{{font:16px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#182230;background:#f4f6f9;margin:0}}
main{{max-width:1200px;margin:auto;padding:32px 24px 60px}}h1{{font-size:28px;margin:0}}h2{{font-size:20px;margin:0 0 14px}}
.card{{padding:22px;margin:20px 0;border:1px solid #dde3eb;border-radius:12px;background:white}}.muted{{color:#64748b;font-size:14px}}
.scroll{{overflow:auto}}table{{width:100%;border-collapse:collapse;white-space:nowrap;font-size:14px;font-variant-numeric:tabular-nums}}
th,td{{padding:10px 12px;border-bottom:1px solid #e7ebef;text-align:right}}th:first-child,td:first-child{{text-align:left}}thead{{background:#f1f4f8}}
li{{margin:12px 0}}img{{width:100%;height:auto}}a{{color:#1971c2}}code{{font-size:13px}}p{{margin:12px 0}}@media print{{body{{background:white}}.card{{break-inside:avoid}}}}
</style><main><h1>昨晚为什么盈利好，能否提前识别？</h1>
<p class="muted">服务器五个独立 Paper 账户；只读分析。行情及订单快照采集于 11 月 18 日约 11:07，结果统一截到 10:53。480 笔已结算交易，170 个不同市场。交易参数未修改。</p>
<div class="card"><p><strong>确认的事实是：后半夜方向命中率提高，且下注金额翻倍放大了资金曲线。当前数据还不足以确认一个能事先识别整段盈利行情的规则。</strong>
市场条件可能影响策略表现，但“这一晚赢得多”本身不是可用于提前判断的输入。把未来已经发生的顺势行情用作筛选标准，会造成事后偏差。</p>
<p>四个优化派生策略共用 TWAP 预测核心，主账户使用另一个信号函数，但行情来源相同；五个账户同时赚钱并不是五份独立证据。后半夜 124 笔交易实际上只覆盖 41 个不同市场。</p></div>
<div class="card"><img alt="按开仓时间比较实际胜率、模型分数、含费成本与等额单笔收益" src="data:image/png;base64,{image}">
<div class="scroll"><table><thead><tr><th>开仓时段（北京时间）</th><th>交易数</th><th>不同市场</th><th>实际胜率</th><th>平均模型分数</th><th>平均买价</th><th>下单前含费模型优势</th><th>每笔按 $5 成本折算</th></tr></thead><tbody>{''.join(main_rows)}</tbody></table></div>
<p class="muted">本表按开仓前的决策时刻分组，因此与之前按“结算时间”统计的 $304.35 / $222.53 不同。金额归一化是 <code>5 × 单笔净盈亏 / 单笔实际含费成本</code>，仅比较单位成本收益，不是重放 $5 订单成交的结果。表中合并胜率按交易加权，市场之间与账户之间仍有相关性。</p></div>
<div class="card"><h2>已找到的解释，以及还不能认定的原因</h2><ol>
<li><strong>下注金额只解释增长幅度的一部分。</strong>前半夜大部分预算 $5，后半夜均为 $10；统一为每笔 $5 成本后，平均净盈亏仍从 -$0.40 提高到 +$1.19，说明交易结果本身也变好了。不能只看资金曲线斜率比较策略。</li>
<li><strong>关键是命中率，买入成本并没有改善。</strong>平均买价从约 $0.618 升到 $0.644，含费成本也从 $0.634 升到 $0.659。下单前模型分数相对含费限价的优势约为 4.35% / 4.29%，没有同步大涨。并非买得更便宜、费用更低或报价优势突然更大。</li>
<li><strong>低分交易也在后半夜变得更容易赢。</strong>分数低于 0.75 的交易，前半夜胜率 52.9%，后半夜 79.2%。所以仅靠固定分数阈值不能解释或捕获这次整体转变。模型分数并不是已经校准的真实概率，某个时期有效不代表另一个时期同样有效。</li>
<li><strong>不能简单称为“波动更小、一路单边”。</strong>开仓前近 2 分钟波动均值由约 0.634 升到 0.691 bps/√秒，近 2 分钟趋势效率反而从 0.178 降到 0.141。后半夜买 Up 的胜率 86.7%，买 Down 的胜率 78.5%，两边都改善；54.8% 的夜间交易在持有期间现货还曾走到开盘基准的不利一侧。好的收益曲线不等于平滑的标的价格路径。</li>
<li><strong>不是模型学习突然生效。</strong>已部署源码哈希保持一致；81 笔自适应已结算单的实际修正都是 0。夜间从样本不足状态变成验证不通过的 FALLBACK，继续用基线开仓。因此其收益不能归功于已启用的自适应修正。</li>
</ol><div class="scroll"><table><thead><tr><th>开仓时段</th><th>分数 &lt;0.75：胜 / 总数</th><th>分数 ≥0.75：胜 / 总数</th></tr></thead><tbody>{''.join(score_rows)}</tbody></table></div>
<p>更准确的表述是：<strong>现有信号在后半夜的样本中，最终方向与开仓判断更一致；这种变化可能包含行情条件改变和随机波动。尚未识别出足以解释并提前预测它的稳健因果机制。</strong></p></div>
<div class="card"><h2>开仓前可见条件的离线检查</h2>
<p>只用了决策前已经收到的行情，以及决策前已经完成结算的结果。固定检查下面六个简单候选条件，没有扫描大量阈值或改动线上策略。表内数值是保留下来的交易的<strong>平均单笔成本收益率</strong>；缩小交易数会改变总体盈利机会。</p>
<div class="scroll"><table><thead><tr><th>条件</th><th>更早样本：11/16–11/17 18点</th><th>11/17 18–24 点</th><th>11/18 00–06 点</th><th>11/18 06–10:53</th></tr></thead><tbody>{''.join(rule_rows)}</tbody></table></div>
<ul><li>“短期波动下降”在后半夜样本里好，但前半夜筛选后的平均收益率从 -8.0% 变成 -22.8%，更早样本也更差，不能作为通用开关。</li>
<li>“当前轮次少跨基准”改善了前半夜，但在更早样本中仍明显亏损；不能只展示对它有利的那段。</li>
<li>分数 ≥0.75 值得作为一个观察候选，但夜间只保留 47/124 笔，归一化总收益从 $147.90 降到 $49.82，仅保留约 34%。前半夜的 24 笔也只有 11 个不同市场，样本很小。</li>
<li>“前 20 个已结算独立市场赚钱才开仓”是滞后识别。优化版要到 03:21 才首次转为允许；进阶版首次允许是 01:45，此前该夜已赚约 $36.40。它不能提前抓住整段行情，早期还有误报。</li></ul>
<p class="muted">这些是从原本已经成交的订单中删减得到的诊断结果，不是完整盘口重放。跳过订单后释放的仓位与后续信号变化没有模拟；结果也已经被查看过，因此不是未触碰的验证集。不能据此直接选择最漂亮的一条部署。<a href="https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf">回测过拟合研究</a>说明了重复试验和选择历史赢家带来的风险。</p></div>
<div class="card"><h2>怎样检验未来能否识别</h2><ol>
<li><strong>把目标定义为下一批机会是否值得参与。</strong>例如预测接下来 30–60 分钟内候选信号的含费收益质量，而不是预告“明天一定大赚”。预测窗口越长，对行情变化越敏感。</li>
<li><strong>先只记录判断，不改变订单。</strong>每轮在开仓前保存时间戳、模型分数与含费成本、近 2/15 分钟波动、短期方向稳定性、近 20/50 个已结算独立市场的校准表现，并记录“有利 / 不确定 / 不利”的候选判断。继续保留原基线作为对照。</li>
<li><strong>用未来数据验收。</strong>固定候选定义后，积累跨多个交易日、包含顺境和逆境的市场；按市场去重、按时间滚动检验。既看过滤掉多少亏损，也看漏掉多少盈利，以及判为“不利”后多久才能识别恢复。手续费按当期市场参数计入，不使用事后结果作为输入。</li>
<li><strong>只有新增证据支持，再考虑参与频率调整。</strong>当前不因一晚盈利就加大预算，也不凭一次回放直接换策略。现有这约 36 小时的服务器记录不足以检验多日可迁移性。</li>
</ol><p>本次完成的是数据核对、条件对比和离线候选检查；没有部署观察器，没有暂停或修改任何账户。</p></div>
<div class="card"><h2>证据与边界</h2><ul>
<li>7 个 SQLite 文件通过只读连接执行 backup，各自取得一致快照，采集窗口约 0.25 秒；不是跨库原子事务。文件哈希全部核对，9 个相关部署源码文件与本机一致。</li>
<li>480 笔交易对应 170 个不同市场。参考源同时记录的 TWAP 开盘与收盘价，与账内目标 / 最终价逐市场相符；170 个市场的结算方向全部一致，最终价最大差异约 5.1×10⁻¹¹ 美元。这是账本与记录行情的交叉验证，未替代官方历史接口的独立外部验证。</li>
<li>开仓前特征有 477 笔为 READY、3 笔历史源时间略陈旧；未来接收数据混入数为 0。那些交易不把缺失值伪造成正常行情。未来波动、持有期反向触碰等字段明确仅用于事后解释，没有用于筛选候选。</li>
<li>此次使用实际 Paper 成交成本与费用；当前费用依据为 <a href="https://docs.polymarket.com/trading/fees">Polymarket 官方费用规则</a>。Paper 不包含真实排队、真实网络与所有实盘成交约束，结果不等同于实盘收益。</li>
</ul><p class="muted">可复算文件：analyze_regimes.py、check_candidates.py；结果文件：regime-summary.json、candidate-checks.json、path-diagnostics.json、score-groups.json、trade-features.json；原始只读快照位于 snapshot/。</p></div>
</main></html>'''
(ROOT/'report.html').write_text(report,encoding='utf-8')
print(json.dumps({'report':str(ROOT/'report.html'),'chart':str(ROOT/'regime-comparison.png'),'bytes':len(report.encode())},ensure_ascii=False))
