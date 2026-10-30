"""Produce the self-contained retirement review from the frozen, read-only analysis."""
from pathlib import Path
import html
import json

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'reports/strategy-consolidation-20261026'
d=json.loads((OUT/'analysis.json').read_text())
esc=lambda s:html.escape(str(s))
money=lambda x:f'${x:+,.2f}' if x is not None else '—'
notes={
 'single_fak':'基线胜率不足以覆盖成交价格和费用；与主账户运行时段、结算来源不同，不能仅比较余额。',
 'single_fak_reverse':'简单反向没有建立正期望；同一信号取反并不自动产生优势。',
 'single_fak_chainlink_only':'只固定价格源不能解决买入过贵、赔率与胜率不匹配的问题。',
 'single_fak_anti_bot_guard':'防守过滤仍然亏完；减少交易或提高胜率并不等于提高收益。',
 'single_fak_fallback_only':'替代价格源存在基差与时效风险；这组负面对照没有提供可延续的优势。',
 'single_fak_reversal':'同一期允许换方向增加风险敞口和费用，历史账本仍然亏完。',
 'single_fak_stop_and_flip':'平仓再反手增加执行环节，历史净结果没有证明其有效。',
 'single_fak_aggressive_edge':'过滤后的真实成交仍亏损，且保留有未结算旧仓；不能把候选筛选当成盈利证明。',
 'single_fak_aggressive_edge_v1':'33 笔成交样本有限，已实现亏损；规则叠加未证明改善。',
 'single_fak_aggressive_edge_v2':'影子评分不是校准胜率，实际成交仍亏损。',
 'single_fak_aggressive_edge_v3':'11 笔成交不足以验证学习过滤器，历史结果为负。',
}
rows=[]
for x in d['ledgers']:
 name=Path(x['ledger']).stem;h=x['historical'];is_live=x['ledger'].startswith('live/') and '_paper' not in name
 if 'diagnostic' in name:
  note='只采样、没有实际成交。记录跨版本重复，部分规则后来回填；零亏损不代表有效策略。'
 elif 'market-scout' in x['ledger']:
  note='市场扫描账户缺少已结算成交，无法验证收益；长周期未结算仓位占用预算。'
 elif is_live:
  note='实盘历史单独归档，样本和资金口径与 Paper 不同；新策略不继承这些资金或密钥。'
 elif '_paper' in name:
  note='历史镜像账户有资金配置变更，账户余额不能直接当作交易净收益；以逐笔已实现盈亏为准。'
 elif name=='polybot2other-real-btc':
  note='保留原策略与账本。收益依赖少数大赢单；不同时间段表现不稳定，不能视为已验证盈利能力。'
 else:note=notes.get(name,'旧演示或空账户，缺少可比较的官方结算成交。')
 rows.append(f'<tr><td>{esc(x["ledger"])}</td><td>{h["n"]}</td><td>{money(h["pnl"])}</td><td>{h["win_pct"] if h["win_pct"] is not None else "—"}%</td><td>{money(x["fees"])}</td><td>{x["open_positions"]}</td><td>{esc(note)}</td></tr>')
price_rows=[]
for band,v in d['main_price_bins'].items():
 a,b=v['earlier'],v['later']
 price_rows.append(f'<tr><td>{band}</td><td>{a["n"]}</td><td>{money(a["pnl"])}</td><td>{a["turnover_roi_pct"]}%</td><td>{b["n"]}</td><td>{money(b["pnl"])}</td></tr>')
doc=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EdgePulse · 历史策略复盘与价值确认 V1</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#0d141c;color:#dce6ef;font:16px/1.75 system-ui,-apple-system,sans-serif}}main{{max-width:1180px;margin:auto;padding:40px 24px 80px}}h1{{font-size:36px;line-height:1.25;color:white}}h2{{margin-top:48px;color:#8ed4ff}}h3{{color:#b8e2cf}}a{{color:#8ed4ff}}.muted{{color:#9bafbf}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}}.card,.note{{background:#16222e;border:1px solid #2d4153;border-radius:14px;padding:20px}}.card b{{display:block;font-size:28px;color:#9ce0c6}}.scroll{{overflow:auto}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #2c3c4b;vertical-align:top}}th{{color:#a8cbe4;background:#16222e}}td:first-child{{word-break:break-word;min-width:180px}}.warning{{border-left:4px solid #e3b96a}}code{{overflow-wrap:anywhere}}li{{margin:8px 0}}@media(max-width:650px){{main{{padding:24px 14px}}h1{{font-size:28px}}.cards{{grid-template-columns:1fr}}}}</style>
<main><p class="muted">EDGE PULSE / 2026-10-26 / PAPER RESEARCH</p><h1>留下一个基准，验证一个新假设</h1><p>主账户保留原策略和全部历史。其余旧策略退出当前运行，新建独立的「价值确认 V1」。本报告先分析归档数据，再明确新策略的规则、风险预算和验证边界。</p>
<div class="cards"><div class="card"><b>{d['ledger_count']}</b>已备份并分析的非空账本</div><div class="card"><b>{d['shadow_raw_rows']:,} → {d['shadow_unique_rows']:,}</b>诊断记录原始数量 → 跨库去重数量</div><div class="card"><b>主账户 + 价值确认 V1</b>当前只运行两个 Paper 账户</div></div>
<h2>为什么旧结果不足以选出赢家</h2><ol><li><b>高胜率仍可能亏完。</b>七个交易实验账户各自亏掉 $100；获胜概率必须结合买入价、手续费和执行成本判断。</li><li><b>主账户的优势不是稳定的。</b>恢复前 2,159 笔交易净赚 $71.65，最赚钱五笔贡献 $167.17，其余合计亏 $95.52。与基线实验共同运行的完整市场时段内，主账户也亏 $22.94。</li><li><b>结算口径混杂。</b>部分旧交易使用 chainlink_fallback。分析另外汇总可确认的官方结果，但不回写任何旧账本；缺少官方结果、标签冲突或来源不明的交易不进入统一口径样本。</li><li><b>重复样本不是独立证据。</b>多个版本保存了同一期、同分钟桶、同方向的记录；先按这三个字段去重。</li><li><b>旧特征不是不可变快照。</b>旧表 UPSERT 会覆盖价格与特征，却保留最初 created_at；后续结算又会更新 updated_at。因此无法据此还原每次决策当刻的可用数据，也不能把回填标志当作事前预测。</li></ol>
<h2>每个旧账户的结果与不足</h2><p class="muted">以下是备份时的原始账本结果。实盘、镜像 Paper、普通 Paper 分开标注；没有合并成一个收益曲线。费用为旧订单表记录值，已包含在相应成交成本内，不再重复扣减。</p><div class="scroll"><table><thead><tr><th>账本</th><th>已结算</th><th>交易净盈亏</th><th>胜率</th><th>记录费用</th><th>未结算</th><th>不足 / 处理</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<h2>新策略从哪里来</h2><p>使用主账户历史中可统一到官方结果的已成交记录，按市场时间顺序分成前后两段，分界为 <code>{esc(d['main_time_split_utc'])}</code>。前段 0.20–0.40 价带有 90 笔、盈利约 $143；低于 0.20 的样本更少且依赖少数高倍赢家。初版据此只研究 0.20–0.39，但上线后过度限制了入场：23:38 首次排查时覆盖 5 个市场，0 笔订单；同期主账户有 3 笔成交。固定 42% 参考值叠加手续费和滑点后，实际买价上限约 0.36，与“方向已确认”的要求很难同时满足。</p>
<div class="scroll"><table><thead><tr><th>成交价区间</th><th>前段笔数</th><th>前段净盈亏</th><th>前段累计投入收益率</th><th>后段笔数</th><th>后段净盈亏</th></tr></thead><tbody>{''.join(price_rows)}</tbody></table></div>
<p class="note warning">这是已有成交的描述性分析，不是新策略完整回测。两段数据都已查看，后段也不再是独立未见样本。价格、资金路径、跳过的机会以及新增的方向和风控条件都会改变结果；新策略账本从 $100 开始，历史分析收益不会导入账户。</p>
<h2>V1.1：修正零成交问题</h2><p>内部策略版本为 <code>20261026-calibrated-v1.1</code>，沿用原空交易账本和历史日志，不补资金、不导入测试成交。用上述时间分界之前的官方结算成交，按基线分数做四个宽分组，并以 Beta(10,10) 先验向 50% 收缩：0.60–0.70 → 58.32%（575 笔）；0.70–0.80 → 62.39%（549 笔）；0.80–0.90 → 56.10%（267 笔）；0.90–1.00 → 57.67%（143 笔）。高分组没有自动获得更高胜率。以上是历史参考，不是未来胜率保证，也存在成交样本的选择偏差。</p><p>只检查价格、时间和扣费条件时，前段可覆盖 229 / 1,376 个已有交易市场，后段 92 / 591；这不是完整回测或预期实盘成交率，仍需通过新鲜度、方向、动量、深度和资金检查。主账户的规则未作调整。</p><h2>价值确认 V1 当前固定规则</h2><ul><li><b>市场：</b>只研究 BTC 五分钟市场，官方目标价必须可用；入场时间为开盘后 30–270 秒。</li><li><b>价带与费用：</b>候选买价 0.20–0.65、价差不超过 0.02。使用上述历史分数组的收缩参考胜率，不直接使用基线启发式分数，也不再固定为 42%。按最坏一美分滑点和每股 0.07 × p × (1-p) 手续费计算后，参考优势至少 0.03；实际允许价格会因此更窄。</li><li><b>方向确认：</b>新策略判断前重新取得同一市场的最新后台快照，保留原始行情时间；Chainlink 与本方向盘口不超过 3 秒；至少一个交易所价格经过已有基差校正后同向，不能出现明显反向分歧；20 秒短期动量同向，未热身完成则等待。</li><li><b>执行：</b>沿用 FAK 可见深度模拟与逐档费用，最多多付一美分；限价内深度必须覆盖预算，不做反手追单，每期最多成交一次。</li><li><b>资金：</b>独立初始 $100，每笔不超过 $2 或当前账面本金的 2%，同时最多一笔持仓；不自动补充资金。</li><li><b>亏损限制：</b>滚动 24 小时已实现亏损达到 $6 暂停，累计已实现亏损达到 $20 暂停；剩余亏损预算也限制下一笔金额。累计停止不会自动重置。</li><li><b>结算与日志：</b>新账户仅等待官方结果结算，不用本地现价先判胜负。决策日志只追加，记录真实观察时间、输入特征、拒绝原因、预算和成交关联，便于后续复核。</li></ul>
<p>费用公式与当前加密市场费率核对自 <a href="https://docs.polymarket.com/trading/fees">Polymarket 官方费用说明</a>。模拟执行仍不能完整反映网络延迟、真实成交优先级和未来费率变化。</p>
<h2>如何判定新策略是否值得留下</h2><p>从这次启动以后收集独立样本，固定规则，不看一次盈利就改参数。至少覆盖 14 个自然日、100 笔官方结算成交，再检查每笔净收益、亏损预算触发次数、逐日分布和大赢家集中度；样本不足时继续保留“待验证”状态。这些数量是研究复核门槛，不是统计盈利保证。</p><p>主账户原样作为同时期参照，但它保留历史资金且每笔预算不同；比较新阶段时同时看相同市场机会、单位投入收益和回撤，不能直接比较两个账户余额。</p>
<h2>归档与恢复</h2><p>完整源码、测试、36 个数据库快照及必要运行设置已备份。旧策略运行目录移出当前 data 路径，当前注册表只包含新策略；主账户使用原数据库。实盘与非 BTC 自动交易保持关闭。</p><p class="muted">备份：<code>{esc(d['backup'])}</code><br>机器可读分析：<code>reports/strategy-consolidation-20261026/analysis.json</code><br>复现：<code>.venv/macos/bin/python scripts/analyze_strategy_history.py</code></p>
</main></html>'''
(OUT/'strategy-review.html').write_text(doc)
(ROOT/'src/polybot2other/static/strategy-review.html').write_text(doc)
print(OUT/'strategy-review.html')
