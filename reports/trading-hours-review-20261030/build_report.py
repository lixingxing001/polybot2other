"""Render local research artifacts; does not connect to or change the bot."""
import base64
import html
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
from matplotlib import font_manager
import matplotlib.pyplot as plt
import numpy as np

OUT = Path(__file__).resolve().parent
DATA = json.loads((OUT / "analysis.json").read_text())
A = DATA["accounts"]
font_manager.fontManager.addfont("/System/Library/Fonts/STHeiti Medium.ttc")
plt.rcParams.update({"font.family": "Heiti TC", "font.size": 11, "axes.unicode_minus": False, "text.parse_math": False})


def money(value):
    return "—" if value is None else ("+" if value > 0 else "−" if value < 0 else "") + f"${abs(value):,.2f}"


def image_file(name):
    return "data:image/png;base64," + base64.b64encode((OUT / name).read_bytes()).decode()


fig, axes = plt.subplots(2, 1, figsize=(13, 7), constrained_layout=True)
for ax, period, title in zip(axes, ("all", "recent"), ("主账户 · 全历史（按买入小时归属最终净收益）", "主账户 · 10月26日恢复运行以来")):
    rows = A["MAIN"]["periods"][period]["entry_hours"]
    values = [r["pnl"] for r in rows]
    ax.bar(range(24), values, color=["#21846b" if x >= 0 else "#c95750" for x in values])
    ax.axhline(0, color="#555", lw=.7)
    ax.axvspan(21.5, 23.5, color="#edb94f", alpha=.13)
    ax.set_xticks(range(24), [f"{h:02d}" for h in range(24)])
    ax.set_title(title, loc="left")
    ax.set_ylabel("账本净盈亏（美元）")
    ax.set_xlabel("买入小时 · 北京时间；例如 22 代表 22:00–23:00")
    ax.grid(axis="y", alpha=.16)
    ax.margins(y=.18)
    for h in (14, 22, 23):
        shift = -9 if h == 22 else 9 if h == 23 else 0
        ax.annotate(money(values[h]), (h, values[h]), xytext=(shift, 6 if values[h] >= 0 else -16), textcoords="offset points", ha="center", fontsize=9)
fig.savefig(OUT / "main-hourly.png", dpi=150)
plt.close(fig)

fig, axes = plt.subplots(2, 2, figsize=(13, 7), constrained_layout=True)
for ax, record in zip(axes.flat, A["MAIN"]["recent_23_equity"]):
    xx, yy = [], []
    previous = None
    for point in record["points"]:
        if point["minutes"] < 0:
            previous = point
            continue
        if previous is not None and point["minutes"]-previous["minutes"] > 2:
            xx.append(float("nan")); yy.append(float("nan"))
        xx.append(point["minutes"]); yy.append(point["pnl"])
        previous = point
    ax.plot(xx, yy, color="#276fb2", lw=1.6)
    ax.axhline(0, color="#777", lw=.7)
    ax.set_xlim(0, 60)
    ax.set_xticks((0, 15, 30, 45, 60), ("23:00", "23:15", "23:30", "23:45", "24:00"))
    ax.set_title(f'{record["date"]} · 该小时权益增加 {money(record["observed_equity_change"])}', loc="left", fontsize=12)
    ax.set_ylabel("相对小时开始的账面权益变化（美元）")
    ax.grid(alpha=.16)
fig.savefig(OUT / "main-23-curves.png", dpi=150)
plt.close(fig)

keys = list(A)
matrix = np.array([[r["pnl"] if r["trades"] else np.nan for r in A[k]["periods"]["recent"]["entry_hours"]] for k in keys])
fig, ax = plt.subplots(figsize=(15, 4.3), constrained_layout=True)
cmap = plt.get_cmap("RdYlGn").copy(); cmap.set_bad("#e8edf2")
ax.imshow(matrix, cmap=cmap, vmin=-65, vmax=65, aspect="auto")
ax.set_yticks(range(4), [A[k]["name"] for k in keys])
ax.set_xticks(range(24), [f"{h:02d}" for h in range(24)])
ax.set_xlabel("买入小时（北京时间） · 数字为净盈亏美元 · 灰格表示无成交，不代表盈亏为零")
ax.set_title("近期四个账户的时段分布（运行天数、下注额不同，颜色不代表策略优劣）", loc="left", pad=16)
for y in range(4):
    for x in range(24):
        value = matrix[y, x]
        ax.text(x, y, "—" if np.isnan(value) else f"{value:+.1f}", ha="center", va="center", fontsize=8.5,
                color="white" if not np.isnan(value) and abs(value) > 48 else "#172d3b")
fig.savefig(OUT / "recent-strategy-hours.png", dpi=150)
plt.close(fig)


def stat_row(account, record, scope):
    return f'<tr><td>{html.escape(account)}</td><td>{record["label"]}</td><td>{scope}</td><td>{record["trades"]}</td><td>{money(record["pnl"])}</td><td>{record["positive_days"]}/{record["days"]}</td><td>{money(record["median_day_pnl"])}</td><td>{money(record["worst_day_pnl"])}</td></tr>'


shortlist = [
    ("MAIN", "all", 22, "全历史"),
    ("MAIN", "recent", 14, "近几日"),
    ("MAIN", "recent", 22, "近几日"),
    ("MAIN", "recent", 23, "近几日"),
    ("MAIN_OPTIMIZED", "recent", 16, "仅3个有成交日"),
    ("MAIN_OPTIMIZED", "recent", 23, "仅2个有成交日"),
    ("VALUE_CONFIRM_V1", "recent", 22, "仅2个有成交日"),
    ("VALUE_CONFIRM_V1", "recent", 23, "仅3个有成交日"),
    ("MAIN_TP_SL", "recent", 15, "仅今天"),
    ("MAIN_TP_SL", "recent", 16, "仅今天"),
]
shortlist_html = "".join(stat_row(A[k]["name"], A[k]["periods"][period]["entry_hours"][h], scope) for k, period, h, scope in shortlist)
night_rows = "".join(f'<tr><td>{r["date"]}</td><td>{money(r["realized_pnl"])}</td><td>{money(r["entry_pnl"])}</td><td>{r["entry_trades"]}</td><td>{money(-r["observed_intrahour_drawdown"])}</td><td>{r["max_sample_gap_seconds"] / 60:.1f} 分钟</td></tr>' for r in A["MAIN"]["recent_23_equity"])
combined = A["MAIN"]["periods"]["recent"]["entry_two_hours"][22]
combined_rows = "".join(f'<tr><td>{r["date"]}</td><td>{r["trades"]}</td><td>{money(r["pnl"])}</td></tr>' for r in combined["daily"])
account_rows = "".join(f'<tr><td>{a["name"]}</td><td>{a["closed_original_entries"]}</td><td>{a["active_entry_days"]}</td><td>{money(a["ledger_realized_pnl"])}</td><td>{a["first_entry"][:16].replace("T"," ")}</td></tr>' for a in A.values())
main_all = A["MAIN"]["periods"]["all"]["entry_hours"]
main_recent = A["MAIN"]["periods"]["recent"]["entry_hours"]
comparison_rows = "".join(f'<tr><td>{main_all[h]["label"]}</td><td>{money(main_all[h]["pnl"])}</td><td>{main_all[h]["positive_days"]}/{main_all[h]["days"]}</td><td>{money(main_recent[h]["pnl"])}</td><td>{main_recent[h]["positive_days"]}/{main_recent[h]["days"]}</td></tr>' for h in (14, 17, 20, 22, 23))

explorer = {k: {"name": a["name"], "periods": a["periods"], "cashflow": a["cashflow"]} for k, a in A.items()}
payload = json.dumps(explorer, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>四个策略的盈利时段分析</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#f3f6f9;color:#203040;font:16px/1.8 system-ui,-apple-system,"PingFang SC",sans-serif}}main{{max-width:1200px;margin:auto;padding:40px 24px 64px}}h1{{font-size:34px;line-height:1.35;margin:8px 0 16px}}h2{{font-size:23px;margin:34px 0 14px}}p{{max-width:105ch}}.lead{{font-size:20px}}.kicker{{font-size:13px;color:#536c83;letter-spacing:.12em}}.muted{{color:#63778a;font-size:14px}}.box{{background:#e9f2fb;border:1px solid #bdd5e9;border-radius:12px;padding:18px 22px}}.caution{{background:#fff5e6;border-color:#e5cda1}}.scroll{{overflow-x:auto;border:1px solid #d9e2eb;border-radius:10px}}table{{border-collapse:collapse;width:100%;font-size:14px;background:#fff}}th,td{{padding:11px 12px;border-bottom:1px solid #e4eaf0;text-align:left;white-space:nowrap}}th{{background:#e8eef5}}img{{width:100%;height:auto;border-radius:10px;background:#fff;border:1px solid #dde4ec;margin:16px 0 2px}}.controls{{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}}select{{padding:9px 12px;border:1px solid #b7c7d7;border-radius:7px;background:white;font:inherit}}.positive{{color:#157052}}.negative{{color:#b0433f}}.selected{{background:#edf5ff}}details{{margin-top:20px}}summary{{cursor:pointer;font-weight:600}}a{{color:#1e65a3}}li{{margin:8px 0}}footer{{margin-top:40px;padding-top:20px;border-top:1px solid #d6e0e8}}@media(max-width:650px){{main{{padding:25px 14px}}h1{{font-size:27px}}.lead{{font-size:18px}}}}</style>
</head><body><main><div class="kicker">PAPER · HISTORICAL ATTRIBUTION · UTC+8</div><h1>哪些时段盈利多，哪些时段重复性更好？</h1>
<p class="lead">你观察到的“主账户晚上 11 点到 12 点上涨”，近期确有数据支持。按全历史的买入时段看，<strong>22:00–23:00</strong>的证据更强；最近几日把<strong>22:00–24:00</strong>合起来看表现较好，<strong>14:00–15:00</strong>也值得继续观察。</p>
<p class="muted">冻结快照：{html.escape(DATA["as_of"])}。全部时间为北京时间。只分析当前四个账户，未改变策略、账户或交易时段。</p>
<h2>1. 先核对你看到的 23:00–24:00</h2>
<p>10 月 26–29 日，主账户这一个小时的账面权益四晚均增加，合计 <strong>+$114.63</strong>。逐笔兑现记录与资金曲线的小时净变化一致。但“这个小时曲线上涨”和“这个小时买入最赚钱”并不相同。</p>
<div class="scroll"><table><thead><tr><th>日期</th><th>23点这一小时兑现净收益</th><th>23点这一小时买入单的最终净收益</th><th>买入笔数</th><th>记录到的小时内权益回撤</th><th>最大曲线记录间隔</th></tr></thead><tbody>{night_rows}</tbody></table></div>
<p>这 $114.63 中，有 <strong>$46.31</strong> 来自 22 点买入、23 点兑现的 5 笔交易。另外，23 点买入的 6 笔交易到其他小时才兑现，合计 <strong>−$7.04</strong>。因此，只盯着 23 点这一段曲线，会把前一小时的盈利算进来，也会暂时看不到一部分延后兑现的亏损。</p>
<img src="{image_file('main-23-curves.png')}" alt="主账户四晚23点至24点的账面权益变化">
<p class="muted">图中超过 2 分钟的记录缺口不连线；10 月 26 日尤其不完整。小时净变化可与成交账本核对，但曲线缺口内的最大回撤无法重建。这里是系统账面权益，不是完整的逐秒盘口市值。即使整小时盈利，中途仍可能回落。</p>
<h2>2. 主账户：全历史最突出的是 22:00–23:00</h2>
<div class="box"><strong>22:00–23:00：</strong>126 笔交易，累计 <strong>+$210.89</strong>；14 个有成交日里 11 日盈利、3 日亏损，盈利日占 78.6%。每天该小时净收益的中位数为 <strong>+$14.11</strong>，最差一天 <strong>−$10.30</strong>。去掉最好的一天，仍有 <strong>+$175.42</strong>，因此并非只靠某一天的大赚。</div>
<p>对照来看，全历史 23:00–24:00 虽然累计 +$68.34，但 17 个有成交日只有 7 日盈利、10 日亏损，每日中位数 −$2.26。最近几晚走强，不能代表它在所有历史中都稳定。</p>
<div class="scroll"><table><thead><tr><th>买入时段</th><th>全历史净收益</th><th>全历史盈利日 / 有成交日</th><th>近期净收益</th><th>近期盈利日 / 有成交日</th></tr></thead><tbody>{comparison_rows}</tbody></table></div>
<img src="{image_file('main-hourly.png')}" alt="主账户全历史与近期按买入小时的净收益柱状图">
<p>两个变化值得保留：14:00–15:00 最近四个有成交日全为正，但全历史仍为 −$3.55；17:00–18:00 全历史不错，近期却为 −$11.82。说明时段表现会变化，不能把全历史排名直接当作当前规则。</p>
<h2>3. 最近主账户的两个观察窗口</h2>
<p><strong>22:00–24:00：</strong>59 笔，合计 <strong>+$121.01</strong>，四个有成交夜间窗口全为正。这个两小时窗口在主账户全历史也有 +$279.24，17 个有成交日中 13 日为正。第一晚运行覆盖不完整，计入的是实际发生的成交。</p>
<div class="scroll"><table><thead><tr><th>夜间日期</th><th>22点至24点买入笔数</th><th>这些交易的最终净收益</th></tr></thead><tbody>{combined_rows}</tbody></table></div>
<p><strong>14:00–15:00：</strong>最近 27 笔合计 <strong>+$62.94</strong>，4 个有成交日均为正，最差日也有 +$6.07。它是近期单小时金额第一，但目前只属于短期观察结果。</p>
<p class="muted">全历史的两小时窗口中，21:00–23:00 为 +$280.72、13/17 日为正；22:00–24:00 为 +$279.24、13/17 日为正，历史金额很接近。近期后者更突出。重叠窗口不能相加，也未据此运行“只在这些时间交易”的账户回测。</p>
<h2>4. 四个策略分别看</h2>
<div class="scroll"><table><thead><tr><th>策略</th><th>买入时段</th><th>统计范围</th><th>笔数</th><th>净收益</th><th>盈利日 / 有成交日</th><th>每日中位数</th><th>最差日</th></tr></thead><tbody>{shortlist_html}</tbody></table></div>
<p>优化版目前较好的观察时段是 <strong>16:00–17:00、23:00–24:00</strong>，但各只有 7 笔；价值确认 V1 的 <strong>22:00–23:00</strong>金额最多，但仅 5 笔、2 个有成交日。两者尚不足以称为稳定规律。优化版在 10 月 28 日 23 点没有成交，所以“2/2 日盈利”不等于每晚都盈利。</p>
<p>止盈止损今天 13:26 才启用，尚未经历任何夜间窗口。目前只能说今天 15:00–17:00 合计 +$11.19；14:00–15:00 为 −$6.70。它与主账户的时段表现可以不同，不能把主账户的好时段直接套过来。</p>
<img src="{image_file('recent-strategy-hours.png')}" alt="近期四个策略按小时净收益热图">
<div class="box caution">当前没有“所有策略共同验证的最佳夜间时段”。四个账户同时存在的共同观察期只从 {DATA['common_start'][:16].replace('T',' ')} 开始，尚不包含夜间。各账户历史长度、下注额和参与频率不同；金额排名不是策略优劣排名。</div>
<h2>5. 自己查看全部小时和逐日结果</h2>
<div class="controls"><label>账户 <select id="account">{''.join(f'<option value="{k}">{a["name"]}</option>' for k,a in A.items())}</select></label><label>范围 <select id="period"><option value="recent">10月26日起</option><option value="all">全部历史</option><option value="legacy">恢复运行前</option><option value="common">四账户共同观察期</option></select></label><label>时间口径 <select id="basis"><option value="entry_hours">按买入时段归属最终收益</option><option value="cashflow">按实际兑现时段</option><option value="market_end_hours">按市场到期时段归属</option></select></label></div>
<p id="scope" class="muted"></p><div class="scroll"><table><thead><tr><th>时段</th><th>笔数</th><th>净收益</th><th>累计投入</th><th>投入收益率</th><th>盈利日 / 有成交日</th><th>每日中位数</th><th>最差日</th><th>去掉最好一天</th><th>查看</th></tr></thead><tbody id="hours"></tbody></table></div>
<h3 id="day-title">逐日明细</h3><div class="scroll"><table><thead><tr><th>日期</th><th>交易笔数</th><th>投入</th><th>净收益</th></tr></thead><tbody id="days"></tbody></table></div>
<h2>6. 数据口径与限制</h2>
<div class="scroll"><table><thead><tr><th>账户</th><th>已结束原始入场笔数</th><th>有成交日期数</th><th>账本总净收益</th><th>首笔入场（北京时间）</th></tr></thead><tbody>{account_rows}</tbody></table></div>
<ul><li>主账户历史从 5 月开始，6 月到 10 月之间有明显空档。近期单独统计，未把没运行的月份补成零收益日。</li><li>盈利日比例以“该时段有成交的日期”为分母。没有成交可能源于没有信号、风控、未运行或数据缺口，不能直接视为稳定或不稳定。</li><li>盈亏使用账本原始净收益，保留当时记录的费用口径；止盈止损包括已记入的卖出费用。未记录费用没有凭空补算。</li><li>主账户保留了 1 笔未记录结算来源的历史交易（净亏 $1.30）；它不属于近期窗口。所有快照均通过 SQLite 完整性检查。</li><li>官方结算可能晚于市场到期；主账户有 6 笔官方结算延后超过一小时。页面的兑现时段不能替代可用于选择买入时间的时段。</li><li>当日末尾尚未走完的小时，以及账户刚启动的小时，都不是完整观察窗口。止盈止损只有今天的记录。</li><li>对 24 个小时及重叠窗口做的是事后描述。未做独立样本验证；也未模拟改变时段后资金、持仓上限及每日风控对后续交易的影响。</li></ul>
<footer>本次只读分析。<a href="analysis.json">完整计算结果</a> · <a href="analyze_hours.py">可复现分析脚本</a> · <a href="snapshot-manifest.json">快照信息</a>。主账户的观察优先级是晚间 22:00–24:00，尤其 22:00–23:00；近期下午 14:00–15:00 列为另一个待继续验证的窗口。</footer>
</main><script id="data" type="application/json">{payload}</script><script>
const data=JSON.parse(document.getElementById('data').textContent);let hour=23;
const money=v=>v==null?'—':(v>0?'+':v<0?'−':'')+'$'+Math.abs(v).toFixed(2);
const pct=v=>v==null?'—':v.toFixed(2)+'%';
const cell=v=>`<td class="${{v>0?'positive':v<0?'negative':''}}">${{money(v)}}</td>`;
function render(){{const key=document.getElementById('account').value,p=document.getElementById('period').value,b=document.getElementById('basis').value,a=data[key];let rows;
if(b==='cashflow') rows=a.cashflow[p];else rows=a.periods[p]?.[b];
if(!rows){{document.getElementById('scope').textContent='此范围不提供该时间口径，请选择“全部历史”或“10月26日起”，或改为按买入时段。';document.getElementById('hours').innerHTML='';document.getElementById('days').innerHTML='';return;}}
document.getElementById('scope').textContent=(b==='cashflow'?'按实际入账时间统计；可能包含更早买入的交易。':'按所选时间归属每笔已结束交易的最终收益。')+' 盈利日比例只以有成交日期为分母。点击任意小时查看逐日数据。';
document.getElementById('hours').innerHTML=rows.map(r=>`<tr class="${{r.hour===hour?'selected':''}}"><td>${{r.label}}</td><td>${{r.trades}}</td>${{cell(r.pnl)}}<td>${{money(r.stake)}}</td><td>${{pct(r.roi_pct)}}</td><td>${{r.days?r.positive_days+'/'+r.days:'无成交'}}</td>${{cell(r.median_day_pnl)}}${{cell(r.worst_day_pnl)}}${{cell(r.without_best_day)}}<td><button type="button" data-hour="${{r.hour}}">逐日</button></td></tr>`).join('');
const selected=rows[hour];document.getElementById('day-title').textContent=a.name+' · '+selected.label+' · 逐日明细';document.getElementById('days').innerHTML=selected.daily.length?selected.daily.map(r=>`<tr><td>${{r.date}}</td><td>${{r.trades}}</td><td>${{money(r.stake)}}</td>${{cell(r.pnl)}}</tr>`).join(''):'<tr><td colspan="4">该时段暂无成交记录。</td></tr>';}}
for(const id of ['account','period','basis'])document.getElementById(id).addEventListener('change',render);document.getElementById('hours').addEventListener('click',e=>{{const b=e.target.closest('button[data-hour]');if(b){{hour=Number(b.dataset.hour);render();}}}});render();
</script></body></html>'''
(OUT / "四个策略的盈利时段分析.html").write_text(page, encoding="utf-8")
print(OUT / "四个策略的盈利时段分析.html")
