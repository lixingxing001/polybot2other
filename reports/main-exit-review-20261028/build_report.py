"""Render the frozen exploratory study; writes only beside this file."""
from pathlib import Path
import base64
import datetime as dt
import html
import json
from zoneinfo import ZoneInfo
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

OUT = Path(__file__).resolve().parent
DATA = json.loads((OUT / "analysis.json").read_text())
ROWS = json.loads((OUT / "trade-paths.json").read_text())
BY_ID = {x["id"]: x for x in ROWS}
TZ = ZoneInfo("Asia/Shanghai")
font_manager.fontManager.addfont("/System/Library/Fonts/STHeiti Medium.ttc")
plt.rcParams.update({"font.family": "Heiti TC", "axes.unicode_minus": False, "font.size": 11, "text.parse_math": False})


def money(value):
    return f'{"+" if value > 0 else "−" if value < 0 else ""}${abs(value):,.2f}'


def when(value):
    return dt.datetime.fromtimestamp(value, TZ).strftime("%Y-%m-%d %H:%M:%S")


fig, axes = plt.subplots(2, 2, figsize=(13, 8), constrained_layout=True)
for ax, trade_id in zip(axes.flat, [1118, 1120, 935, 1051]):
    row = BY_ID[trade_id]
    xx = [(m["at"] - row["opened_at"]) for m in row["marks"]]
    yy = [m["roi_pct"] for m in row["marks"]]
    ax.scatter(xx, yy, s=10, color="#246bad", label="记录盘口的扣费退出收益率")
    for i in range(1, len(xx)):
        if xx[i] - xx[i-1] <= 15:
            ax.plot(xx[i-1:i+1], yy[i-1:i+1], lw=1, color="#246bad", alpha=.7)
    end_x = row["ends_at"] - row["opened_at"]
    terminal = row["pnl"] / row["stake"] * 100
    ax.scatter([end_x], [terminal], marker="D", color="#b13c38" if terminal < 0 else "#20815e", s=45, label="实际终局收益（非卖价）")
    ax.axhline(0, lw=.7, color="#444444")
    ax.axhline(20, lw=.8, ls="--", color="#20815e", label="+20% 示例阈值")
    ax.axhline(-20, lw=.8, ls=":", color="#b13c38", label="−20% 示例阈值")
    ax.set_title(f'#{trade_id} · {row["side"]} · 投入 $5 · 终局 {money(row["pnl"])}')
    ax.set_xlabel("买入后秒数")
    ax.set_ylabel("退出净收益 / 原始投入（%）")
    ax.grid(alpha=.15)
axes[0, 0].legend(fontsize=8, loc="lower left")
fig.savefig(OUT / "holding-paths.png", dpi=150)
plt.close(fig)

chart = base64.b64encode((OUT / "holding-paths.png").read_bytes()).decode()
ledger = DATA["ledger"]
cov = DATA["coverage"]
table = []
for s in DATA["scenarios"]:
    if s["execution_assumption"] != "next_observation_stress":
        continue
    m = s["cohorts"]["all_observed"]
    base = next(x for x in DATA["scenarios"] if x["execution_assumption"] == "same_snapshot" and x["take_profit_pct"] == s["take_profit_pct"] and x["stop_loss_pct"] == s["stop_loss_pct"])["cohorts"]["all_observed"]
    name = (f'止盈 +{s["take_profit_pct"]}%' if s["take_profit_pct"] is not None else "不设止盈") + (f' / 止损 −{s["stop_loss_pct"]}%' if s["stop_loss_pct"] is not None else " / 不设止损")
    table.append(f'<tr><td>{name}</td><td>{money(base["scenario_pnl"])}</td><td>{money(m["scenario_pnl"])}</td><td>{money(m["difference"])}</td><td>{money(m["profit_given_up_on_original_winners"])}</td><td>{m["losers_to_profit"]}</td><td>{m["winners_to_loss"]}</td></tr>')
details = []
for r in ROWS:
    details.append(f'<tr data-search="{r["id"]} {r["round_id"]} {r["side"]}"><td>#{r["id"]}</td><td>{when(r["opened_at"])}</td><td>{r["side"]}</td><td>{r["entry_price"]:.4f}</td><td>{money(r["pnl"])}</td><td>{r["mfe"]["roi_pct"]:+.2f}%<br>{money(r["mfe"]["pnl"])}</td><td>{r["mae"]["roi_pct"]:+.2f}%</td><td>{len(r["marks"])}</td><td>{r["max_gap_seconds"]:.1f} 秒</td><td>{"是" if r["denser_path"] else "否"}</td></tr>')
cases = []
for ident in [1118, 1120, 935]:
    r = BY_ID[ident]
    cases.append(f'<tr><td>#{ident} · {r["side"]}</td><td>{when(r["opened_at"])}</td><td>${r["entry_price"]:.2f}</td><td>{money(r["mfe"]["pnl"])}（{r["mfe"]["roi_pct"]:+.1f}%）</td><td>{r["mae"]["roi_pct"]:+.1f}%</td><td>{money(r["pnl"])}</td></tr>')
page = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>主账户止盈止损 · 数据分析与方案</title>
<style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#1f2c3b;font:16px/1.75 system-ui,-apple-system,"PingFang SC",sans-serif}}main{{max-width:1180px;margin:auto;padding:44px 24px 72px}}h1{{font-size:34px;line-height:1.3;margin:8px 0 20px}}h2{{font-size:23px;margin:35px 0 12px}}h3{{font-size:18px}}p{{max-width:100ch}}.kicker{{color:#48677d;letter-spacing:.12em;font-size:13px}}.lead{{font-size:20px}}.box{{padding:18px 22px;border:1px solid #c4d8e8;background:#edf5fb;border-radius:10px}}.warn{{background:#fff7e9;border-color:#e5d1a6}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:22px 0}}.card{{background:white;border:1px solid #dde4eb;border-radius:12px;padding:18px}}.card strong{{display:block;font-size:26px}}.muted{{color:#637286;font-size:14px}}table{{border-collapse:collapse;width:100%;background:white;font-size:14px}}th,td{{text-align:left;padding:11px 12px;border-bottom:1px solid #e4e9ef;white-space:nowrap}}th{{background:#eaf0f6;font-weight:600}}.scroll{{overflow-x:auto;border:1px solid #dbe2e9;border-radius:9px}}img{{width:100%;height:auto;background:white;border:1px solid #dde4eb;border-radius:10px}}li{{margin:7px 0}}code{{background:#e7edf3;padding:3px 6px;border-radius:4px}}a{{color:#1762a3}}input{{width:min(440px,100%);padding:11px 14px;margin:12px 0;border:1px solid #bac9d8;border-radius:8px;font:inherit}}details{{margin-top:22px}}summary{{cursor:pointer;font-weight:600}}footer{{margin-top:40px;border-top:1px solid #dbe2e9;padding-top:18px}}@media(max-width:700px){{main{{padding:28px 16px}}h1{{font-size:27px}}.cards{{grid-template-columns:1fr 1fr}}}}
</style></head><body><main>
<div class="kicker">PAPER RESEARCH · READ ONLY</div><h1>主账户止盈止损<br>先保留入场，再验证退出</h1>
<p class="lead">历史记录支持“先浮盈、后终局亏损”确实存在。提前止盈值得独立验证；机械止损也会提前卖掉一部分最终赢家。现有数据尚不足以选出可靠的止盈、止损百分比。</p>
<p class="muted">账本冻结时间：{html.escape(DATA["captured_at"])}。报告生成于 2026-10-29，时间均为北京时间。本轮只读原数据；未修改策略、未新增模拟账户。</p>
<div class="cards"><div class="card">全部已结束交易<strong>{ledger["settled"]:,}</strong><span class="muted">另有 {ledger["open"]} 笔未结算</span></div><div class="card">全部亏损交易<strong>{ledger["losses"]:,}</strong><span class="muted">{ledger["full_stake_losses"]} 笔损失全部投入</span></div><div class="card">明确提前卖出<strong>1 笔</strong><span class="muted">另 {ledger["settlement_sources"]["polymarket_official"]:,} 笔为官方结算，1 笔来源未记录</span></div><div class="card">全账本已实现净收益<strong>{money(ledger["net_pnl"])}</strong><span class="muted">与下面的局部样本收益区分</span></div></div>
<h2>1. 能回答什么，不能回答什么</h2>
<p>逐笔核对了全部 {ledger["settled"]:,} 笔已结束交易。归档盘口来自旧策略的输局复盘：59 个证据包，44 个市场，匹配主账户 57 笔交易；其中 56 笔有买入后、到期前、时间与深度检查合格的盘口片段，涉及 43 个市场、40 笔亏损和 16 笔盈利。</p>
<div class="box warn"><strong>这 56 笔不是随机样本，也不是完整账户回测。</strong>旧系统只在旧策略亏损后保留这些市场的数据，样本本来就偏向输局。时间集中在 2026-06-03 23:15 至 06-05 09:35。不能把其中的比例推广到全部 947 笔亏损，也不能把局部改善当作全账户预期收益。</div>
<p>只保留记录时间不晚于观察时间、时间龄不超过 3 秒、买一与深度一致、买卖报价不交叉，且所记录买盘足以卖出整仓的片段。10 笔满足更密集的覆盖条件；其余存在较长缺口。旧记录未保存独立的交易所序号与接收时间，时间检查只能针对现存字段。</p>
<p>近期优化版日志只留了部分方向的买一报价，没有数量和买盘深度；价值确认日志主要是卖一报价。因此它们不能补足近期主账户的整仓退出路径。BTC 的涨跌数据也不能替代合约的可卖价格。</p>
<h2>2. 你的担心有具体交易支持</h2>
<p>40 笔亏损样本中，<strong>23 笔</strong>在至少一个有效记录点出现过扣费后的正浮盈；其中 <strong>14 笔</strong>曾达到 +20%，<strong>12 笔</strong>曾达到 +30%。未观察到的机会仍可能存在，不能把数据缺口当作“从未浮盈”。</p>
<div class="scroll"><table><thead><tr><th>主账户交易</th><th>买入时间</th><th>买入均价</th><th>记录中最高退出净收益</th><th>记录中最大浮亏</th><th>实际终局收益</th></tr></thead><tbody>{''.join(cases)}</tbody></table></div>
<p class="muted">以上每笔原始投入均为 $5，已包含买入手续费。峰值仅用于描述发生过什么，不能据此声称策略能在最高点卖出。#935 展示相反情况：中途大幅浮亏，最终盈利。</p>
<img src="data:image/png;base64,{chart}" alt="四笔持仓的记录退出收益率和实际终局收益对比">
<p class="muted">蓝点是记录盘口的费用后估值，只连接间隔不超过 15 秒的相邻点；空缺没有补值。终局菱形是实际结算收益，不是到期时可成交的买盘价。虚线阈值仅为解释示例。</p>
<h2>3. 不能只数“救回了多少亏损”</h2>
<p>16 笔最终赢家中，9 笔曾浮亏至少 20%，7 笔曾浮亏至少 40%。紧止损会使其中部分交易从最终盈利变成提前亏损；固定低止盈还可能削掉主账户的大盈利。因此要同时计算挽回的亏损、少赚的利润、赢家被止损的数量和额外卖出费用。</p>
<p>退出收益率定义为：<code>（整仓卖出所得 − 卖出手续费 − 原始实际投入）÷ 原始实际投入</code>。按持有方向的买盘逐档计算，不用卖一价、BTC 涨幅或最终最高赔付替代。买入手续费已经在原始投入中，避免重复扣费。费率模型沿用项目的 crypto taker 系数 0.07，与当前 <a href="https://docs.polymarket.com/trading/fees">Polymarket 官方手续费公式</a>一致；这不是对过去实际费率的独立审计。</p>
<h2>4. 有限片段上的候选规则对照</h2>
<div class="box warn">下面所有方案仅作用于同一组 56 笔原始入场，其实际终局合计是 <strong>{money(cov["baseline_pnl"])}</strong>。全账户是 {money(ledger["net_pnl"])}，两者不是同一个统计范围。所有候选都只是在该偏差样本上少亏，尚未证明稳定盈利。</div>
<p>“同盘口”假设触发后可按该记录立刻卖出；“延迟与深度压力”只使用一半可见深度、每份价格扣减 1 美分，并在至少 0.5 秒后的下一个有效记录卖出，最长等 10 秒。它是稀疏数据下的压力情景，不能称为精确的 500 毫秒执行回测。按时间顺序首次触发退出，未提前退出的保留原终局结果。</p>
<div class="scroll"><table><thead><tr><th>候选规则（不是推荐参数）</th><th>同盘口净收益</th><th>压力情景净收益</th><th>压力情景较原终局改善</th><th>原赢家让出的净收益</th><th>原亏损转正</th><th>原赢家变亏损</th></tr></thead><tbody>{''.join(table)}</tbody></table></div>
<p>例如“+20% 止盈、不另设止损”压力情景：原亏损交易合计改善 $74.89，同时原盈利交易合计少赚 $7.47，样本净改善 $67.42；56 笔仍合计亏 $90.61。“+20% 止盈、−20% 止损”让 9 笔原赢家提前亏损，虽在全部片段上略少亏，在覆盖较密集的 10 笔子集中却比只止盈更差。不能只挑某个数字最高的组合上线。</p>
<p class="muted">账户资金循环、提前释放仓位后的新交易、每日风控触发变化均未模拟，也未据此宣称账户回撤下降。没有完整盘口时，止损触发价不等于成交价；缺口中可能存在更早的触发。分析 JSON 同时保留前半市场、后半市场和密集片段子集结果，均为探索性诊断。</p>
<h2>5. 拟新增“主账户止盈止损”的方式</h2>
<ol><li><strong>保留主账户的入场逻辑，账户独立。</strong>以主账户实际入场作为初期配对对照，记录父交易 ID、同一成本和份额，仅检验退出规则的影响。若以后要独立捕捉更多入场机会，再单独评估资金释放和重入影响。</li>
<li><strong>先用固定阈值做清楚的对照。</strong>主实验单独评估固定止盈；止损在同批行情上做独立分项记录，避免把两类效果混在一起。10%–30% 只是待验证的止盈候选范围，尚不是“最佳参数”。</li>
<li><strong>退出由可卖净收益触发。</strong>必须检查本期、本方向、新鲜盘口及实际深度。遇到跳价、深度不足或部分成交，记录成交部分和剩余仓位，不把触发价当成保证成交价。</li>
<li><strong>防止刚卖出又重复买回。</strong>初期每一笔原主账户入场只对应一笔新策略持仓；退出后不会因为原信号持续而在同方向反复开仓。原主账户本来发生的反向交易仍可单独配对。</li>
<li><strong>把所有持仓轨迹记录完整。</strong>赢单、输单都记录，不再只保存输局；退出后继续观察原单到结算，用于算出挽回损失与错失利润。按同一时段、同一入场比较净收益、回撤、手续费、卖出失败及盈利截断。</li></ol>
<p>移动止盈（盈利后随最高利润上移保护线）可作为后续候选，但需要更连续的轨迹，暂不与固定止盈一起堆入第一版。当前证据足以支持做一个独立 Paper 退出实验，不能承诺它一定超过原主账户。</p>
<details><summary>逐笔查看：56 笔盘口覆盖与收益</summary><input id="search" aria-label="筛选交易" placeholder="搜索交易 ID、期次或 Up / Down"><div class="scroll"><table id="trades"><thead><tr><th>交易</th><th>买入时间</th><th>方向</th><th>均价</th><th>终局收益</th><th>最高观察浮盈</th><th>最低观察浮盈</th><th>有效记录数</th><th>最大记录缺口</th><th>覆盖较密集</th></tr></thead><tbody>{''.join(details)}</tbody></table></div></details>
<footer><strong>可复核文件：</strong>同目录 <a href="analysis.json">analysis.json</a>、<a href="trade-paths.json">trade-paths.json</a>、<a href="trade-summary.csv">trade-summary.csv</a>、<a href="analyze_exits.py">分析脚本</a>。原账本以 SQLite 只读连接制作快照，原始归档保留哈希。所有分析输出仅位于本报告目录。</footer>
</main><script>document.getElementById('search').addEventListener('input',function(){{const q=this.value.trim().toLowerCase();document.querySelectorAll('#trades tbody tr').forEach(r=>r.hidden=!r.dataset.search.toLowerCase().includes(q))}})</script></body></html>'''
(OUT / "主账户止盈止损分析.html").write_text(page, encoding="utf-8")
print(OUT / "主账户止盈止损分析.html")
