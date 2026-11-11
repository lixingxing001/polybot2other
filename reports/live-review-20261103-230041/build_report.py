"""Build a self-contained offline review and standalone financial plots."""
from pathlib import Path
from datetime import datetime
import html,json,sqlite3,sys

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,'/tmp/polybot-review-plots-20261103')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager

font_manager.fontManager.addfont('/System/Library/Fonts/STHeiti Medium.ttc')
font=font_manager.FontProperties(fname='/System/Library/Fonts/STHeiti Medium.ttc').get_name()
plt.rcParams.update({'font.family':font,'axes.unicode_minus':False,'font.size':10,'figure.facecolor':'#111820',
                    'axes.facecolor':'#16212c','axes.edgecolor':'#405264','text.color':'#e4edf5','axes.labelcolor':'#c7d4df',
                    'xtick.color':'#aebfcc','ytick.color':'#aebfcc','grid.color':'#334454','savefig.facecolor':'#111820'})
s=json.loads((ROOT/'analysis.json').read_text());positions=json.loads((ROOT/'positions-enriched.json').read_text())
official_trades=json.loads((ROOT/'official-trade-activity-linked.json').read_text())
db=sqlite3.connect('file:'+str(ROOT/'accounts.sqlite3')+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
orders=[dict(r) for r in db.execute('SELECT * FROM orders ORDER BY created_at,id')]
names={k:v['name'] for k,v in s['strategies'].items()};colors={'MAIN':'#70b7ff','MAIN_OPTIMIZED':'#c59cff','VALUE_CONFIRM_V1':'#ff8791','MAIN_TP_SL':'#f1c15b'}

fig,(ax,bx)=plt.subplots(1,2,figsize=(13.2,4.9),gridspec_kw={'width_ratios':[1.5,1]})
first_at=min(p['opened_at'] for p in positions);last_at=max(p['closed_at'] for p in positions)
for key,v in s['strategies'].items():
    curve=v['curve'];ax.step([datetime.fromtimestamp(first_at)]+[datetime.fromtimestamp(r['at']) for r in curve]+[datetime.fromtimestamp(last_at)],
                            [0]+[r['pnl'] for r in curve]+[curve[-1]['pnl']],where='post',label=v['name'],color=colors[key],linewidth=2)
ax.axhline(0,color='#8ba0b0',linewidth=.8);ax.set_title('累计已结算盈亏 · 本地账本',loc='left',pad=15);ax.set_ylabel('美元');ax.grid(alpha=.45)
ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0,12]));ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d\n%H:%M'))
ax.legend(loc='upper left',frameon=False,fontsize=9)
keys=['MAIN','MAIN_OPTIMIZED','MAIN_TP_SL','VALUE_CONFIRM_V1'];labels=['主账户','优化版','止盈止损','价值确认 V1'];ys=list(range(len(keys)))
bx.barh([y+.17 for y in ys],[s['strategies'][k]['ledger_pnl'] for k in keys],height=.30,color='#70b7ff',label='本地账本')
bx.barh([y-.17 for y in ys],[s['strategies'][k]['cash_result'] for k in keys],height=.30,color='#c59cff',label='实际现金归属')
bx.set_yticks(ys,labels);bx.invert_yaxis();bx.axvline(0,color='#8ba0b0',linewidth=.8);bx.grid(axis='x',alpha=.45);bx.set_xlabel('美元')
bx.set_title('两种口径分开看',loc='left',pad=15);bx.legend(loc='lower right',frameon=False,fontsize=9)
for y,k in enumerate(keys):
    for delta,field in [(.17,'ledger_pnl'),(-.17,'cash_result')]:
        v=s['strategies'][k][field];bx.text(v+(.18 if v>=0 else -.18),y+delta,f'{v:+.2f}',ha='left' if v>=0 else 'right',va='center',fontsize=9)
bx.set_xlim(-13,8)
fig.text(.02,.025,'本地账本未同步外部卖出；实际现金归属包含这些退出，不代表独立自动策略表现。',fontsize=10,color='#aebfcc')
fig.tight_layout(rect=[0,.07,1,1]);fig.savefig(ROOT/'performance.png',dpi=160);plt.close(fig)

c=s['cashflow'];labels=['起始现金','两次入金','程序开仓\n含外部退出','其他来源开仓','旧仓赎回','最终现金']
vals=[c['initial_cash'],c['deposits'],c['program_origin_net_cash_including_external_exits'],c['external_origin_net_cash'],c['preexisting_redemption_cash'],c['final_cash']]
fig,ax=plt.subplots(figsize=(12,4.6));running=0
for i,v in enumerate(vals):
    if i==len(vals)-1:bottom=0;height=v
    else:bottom=min(running,running+v);height=abs(v)
    color='#70b7ff' if i==1 else '#ff8791' if v<0 else '#9ac6cf' if i in (0,5) else '#f1c15b'
    ax.bar(i,height,bottom=bottom,color=color,width=.62)
    top=bottom+height;ax.text(i,top+2,f'${v:,.2f}' if i in (0,5) else f'{v:+,.2f}',ha='center',fontsize=12)
    if i<len(vals)-1:
        running+=v;ax.plot([i+.31,i+.69],[running,running],color='#64798d',linestyle='--',linewidth=1)
ax.set_xticks(range(6),labels);ax.set_ylim(0,98);ax.set_ylabel('美元');ax.grid(axis='y',alpha=.35)
ax.set_title('钱包现金已对平：余额 $50.79 ≠ 交易盈利',loc='left',pad=15)
fig.text(.025,.025,'剔除入金后的净现金变化为 -$30.76；现金口径不计尚未赎回的极小零碎份额。',fontsize=10,color='#aebfcc')
fig.tight_layout(rect=[0,.065,1,1]);fig.savefig(ROOT/'cashflow.png',dpi=160);plt.close(fig)

def money(v):return '—' if v is None else f'{v:+,.2f}'
def cell(v):
    if isinstance(v,(int,float)):return f'<span class="{"pos" if v>0 else "neg" if v<0 else ""}">{money(v)}</span>'
    return html.escape(str(v))
def table(headers,rows):return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+html.escape(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+v+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'
strategy_table=table(['策略','已成交持仓','终局／本地盈利笔数','本地净盈亏','官方现金归属*','本地费用'],[
    [names[k],str(s['strategies'][k]['positions']),f"{s['strategies'][k]['wins']} / {s['strategies'][k]['positions']}",cell(s['strategies'][k]['ledger_pnl']),cell(s['strategies'][k]['cash_result']),f"${s['strategies'][k]['fees']:.2f}"] for k in keys])
pair_table=table(['市场开始','两边合计成本','持有到期最佳可能结果','本地最终结果','实际现金结果*'],[
    [r['local_start'],f"${r['total_cost']:.2f}",cell(r['best_hold_pnl']),cell(r['ledger_pnl']),cell(r['cash_result'])] for r in s['pairs']])
hour_table=table(['入场小时（北京时间）','笔数','终局方向正确','本地净盈亏','实际现金结果*'],[
    [r['group']+':00–'+r['group']+':59',str(r['positions']),f"{r['wins']}/{r['positions']}",cell(r['ledger_pnl']),cell(r['cash_result'])] for r in s['main_by_hour']])
distance_table=table(['入场距离','笔数','终局方向正确','本地净盈亏'],[
    [r['group'],str(r['positions']),f"{r['wins']}/{r['positions']}",cell(r['ledger_pnl'])] for r in s['main_by_distance']])
tp_table=table(['开仓时间','实际退出逻辑','本地实际净盈亏','相同入场持有到期*'],[
    [r['opened_local'],r['exit_reason'] or '止损卖单未成交，最终结算',cell(r['realized_pnl']),cell(r['hold_result'])] for r in s['tp_sl_hold_comparison']])
external_table=table(['持仓','开仓时间','方向','程序外卖出份额','本地净盈亏','含外部退出的现金结果'],[
    [str(r['id']),r['opened_local'],r['outcome'],f"{r['external_sale_shares']:.6f}",cell(r['realized_pnl']),cell(r['cash_result'])] for r in s['external_exits']])
obs=s['observation'];obs_table=table(['首要等待原因（采样）','次数','占采样比例'],[
    [html.escape(k),str(v),f"{v/obs['samples']:.1%}"] for k,v in list(obs['primary_reasons'].items())[:10]])
fields=['id','strategy_id','strategy_name','opened_local','round_id','outcome','status','total_cost','realized_pnl','cash_result','fees','external_sale_shares','residual_shares','entry_reason']
payload={'positions':[{k:p.get(k) for k in fields} for p in positions],
         'orders':[{'id':o['id'],'strategy':names[o['strategy_id']],'time':datetime.fromtimestamp(o['created_at']).strftime('%Y-%m-%d %H:%M:%S'),'round':o['round_id'],'outcome':o['outcome'],'action':o['action'],'status':o['state'],'cash':o['requested_cash'],'shares':o['confirmed_shares'],'fee':o['confirmed_fee'],'reason':o['reason'],'error':o['error'] or ''} for o in orders],
         'official':[{'time':r['local_time'],'source':r['source_label'],'round':r['slug'],'outcome':r['outcome'],'side':r['side'],'shares':r['size'],'cash':r['usdc_size'],'order':r['order_id'],'trade':r['trade_id']} for r in official_trades]}
embedded=json.dumps(payload,ensure_ascii=False).replace('</','<\\/')
html_page=r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>实盘完整复盘 · 2026-11-03</title>
<style>:root{color-scheme:dark;--bg:#0d141b;--panel:#15212d;--line:#2c4052;--text:#e5eef5;--muted:#a1b5c7;--green:#62d5aa;--red:#ff8791;--blue:#8ac3ff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}main{max-width:1250px;margin:auto;padding:36px 26px 70px}h1{font-size:32px;margin:4px 0 10px}h2{font-size:23px;margin:0 0 14px}h3{font-size:18px;margin:20px 0 8px}p{margin:9px 0}.muted,small{color:var(--muted)}.tag{display:inline-block;border:1px solid #3f806b;color:var(--green);padding:2px 11px;border-radius:16px}nav{display:flex;gap:18px;flex-wrap:wrap;margin:22px 0}a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card,section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:22px;margin-bottom:20px}.card{margin-bottom:0}.card strong{display:block;font-size:28px;margin-top:8px}.pos{color:var(--green)}.neg{color:var(--red)}.warn{border-left:4px solid #efc36c;padding:12px 16px;background:#292719;border-radius:5px;margin:16px 0}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;vertical-align:top;padding:11px 12px;border-bottom:1px solid var(--line)}th{color:var(--muted);white-space:nowrap}td{font-variant-numeric:tabular-nums}img{width:100%;height:auto;border-radius:8px;margin:12px 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:16px 0}select,input,button{background:#101a24;color:var(--text);border:1px solid #426079;border-radius:6px;padding:9px 11px;font:inherit}input{flex:1;min-width:190px}button{cursor:pointer}.active{border-color:#8ac3ff;background:#23425d}.reason{min-width:280px;max-width:480px;font-size:12px;color:var(--muted)}.nowrap{white-space:nowrap}.foot{margin-top:28px;font-size:13px;color:var(--muted)}code{color:#b9d9ff}li{margin-bottom:8px}@media(max-width:750px){main{padding:20px 12px}.cards,.grid{grid-template-columns:1fr 1fr}.card{padding:14px}.card strong{font-size:23px}section{padding:16px}h1{font-size:27px}th,td{padding:9px}}@media(max-width:450px){.grid{grid-template-columns:1fr}}</style>
<main><span class="tag">服务已停止 · 本次未恢复交易</span><h1>所有实盘记录复盘</h1><p class="muted">程序交易：2026-10-30 23:06 — 2026-11-03 03:22　｜　停服账本快照：11-03 23:00　｜　全部时间为北京时间</p>
<nav><a href="#summary">策略与现金</a><a href="#findings">关键问题</a><a href="#main">主账户与时段</a><a href="#execution">执行复盘</a><a href="#records">全部记录</a><a href="#files">导出与证据</a></nav>
<div class="cards"><div class="card">程序已成交持仓<strong>69 笔</strong><small>81 笔订单，73 笔成交订单</small></div><div class="card">本地账本净盈亏<strong class="neg">-$18.91</strong><small>没有同步程序外卖出</small></div><div class="card">程序开仓的现金结果<strong class="neg">-$17.37</strong><small>包含程序外退出，不是纯自动成绩</small></div><div class="card">钱包净现金变化<strong class="neg">-$30.76</strong><small>已剔除 $80.78 入金</small></div></div>
<section id="summary" style="margin-top:22px"><h2>先把三种口径分清</h2><p>主账户仍是四个策略里唯一整体为正的账户，但优势很薄。其他三个策略合计明显亏损。钱包还混合了非本程序提交的买卖，不能把钱包余额变化全部归给机器人，也不能直接把本地账本当作实际现金收益。</p>__STRATEGY__<p class="muted">* 官方现金归属按资金来源追踪：程序内卖出按持仓编号归属，其他卖出按当时仍持有的份额来源归属；未发现跨来源歧义。主账户有 10 笔持仓被程序外卖出，因此右列包含外部干预。零碎未兑付获胜份额约 $0.0405，未计入现金列。</p><img src="performance.png" alt="四个策略的本地累计盈亏和实际现金归属比较"><img src="cashflow.png" alt="钱包现金收支核对">
<p><b>现金核对：</b>$0.771923 起始现金 + $80.783361 入金 − $17.366713 程序开仓净现金 − $13.619887 其他来源开仓净现金 + $0.226061 旧仓赎回 = <b>$50.794745</b>。与官方余额及活动流水完全对平。</p><p>官方挂单为 0；持仓汇总估值为 0。官方仍列有已结算的亏损代币和关闭记录中的少量碎片，不能把本地累计 <code>claim_due</code> 的 $297.81 当成当前还可以领取的现金。</p></section>
<section id="findings"><h2>确认的问题</h2><h3>1. 账本没有同步程序外卖出</h3><p>73 条本程序成交在官方认证成交记录中全部找到，份额和顶层成交价格一致。但同钱包还有 57 条程序外成交，其中 10 次卖出改变了主账户持仓。程序仍按原有份额持有到结算，导致部分逐笔收益、持仓占用和待领取金额与真实钱包不一致。</p>__EXTERNAL__<p class="muted">这里只认定“不是本程序订单”，不能仅凭链上记录区分官方界面操作还是其他程序操作。</p>
<h3>2. 同一期两边都买，可能锁定亏损</h3><p>主账户有 6 组双向持仓，12 笔订单的两边合计成本均高于任一结果下的最高赔付。本地持有至结算结果合计 <b class="neg">-$15.64</b>；包含外部卖出后的现金结果为 <b class="neg">-$8.22</b>。</p>__PAIRS__<div class="warn">不能据此声称“删掉第二笔就能赚回 $15.64”。在同一批已发生价格路径上，仅保留第一笔会是 -$21.62，第二笔合计贡献 +$5.98。应研究先退出旧方向再换向、方向反转确认等方案；简单事后删除坏回合不是可执行策略。</div>
<h3>3. 小金额设置与当前执行校验不兼容</h3><p>服务日志最后一次设置更新在 <b>11-03 03:35:32</b>，当前单笔金额为 <b>$3</b>。当前市场约束读取到最小数量 5 份、费率参数 0.07；现实现按 <code>3 ÷ 1.07 ÷ 至少0.60 ≈ 4.67份</code> 校验，低于 5 份。03:40 起持续出现“深度或最小份额不满足预算”，该配置之后没有新的程序订单。</p><p>这是保存参数时应提前检查并明确提示的适配问题。应按实际深度、限价、最小数量和费用计算可执行预算，而非保存一个实际无法通过当前检查的金额。本次未改金额或尝试真实下单。</p>
<h3>4. 数据接口迁移与多档成交记账</h3><p>复盘时旧 <code>/activity</code> 已返回 HTTP 410；官方明确 Data API v1 于 2026-11-02 退役。复盘改用 <code>/v2/activity</code> 游标分页读取完整数据，但交易程序内的旧活动接口尚未迁移，重启前应处理。<a href="https://docs.polymarket.com/migrate/data-api-v1-to-v2">官方迁移说明</a></p><p>另外有 4 笔多档成交，程序使用顶层 <code>trade.price × size</code> 计算成交额，未按 maker 明细加权。单笔现金差异最高约 $0.026，所有程序订单合计约 $0.006。金额不大，但应以成交分项或官方实际现金流校准，不能把正负误差抵消当作完全正确。</p></section>
<section id="main"><h2>主账户：有研究价值，但没有证明稳定盈利</h2><p>本地口径 57 笔，38 盈 19 亏，胜率 66.67%；平均盈利 $2.50、平均亏损 $4.80，对应盈亏平衡胜率约 65.74%。利润因子只有 1.04，累计已结算回撤约 $22.32；该回撤不包含每笔持仓内部浮动。</p><div class="grid"><div><h3>早期盈利支撑了全程结果</h3><p>最初 5 笔全部盈利，合计 <b class="pos">+$16.34</b>；后续 52 笔本地结果 <b class="neg">-$12.49</b>，包含外部退出后的现金结果也为 <b class="neg">-$10.96</b>。不能把最开始连续盈利外推成稳定优势。</p><p class="muted">57 笔方向胜率的 Wilson 95% 区间约 53.7%–77.5%，仍很宽。</p></div><div><h3>信号过近的样本较弱</h3>__DISTANCE__<p class="muted">这些是事后分组，不是已经验证的过滤阈值。距离、入场时间与行情互有关联，需要独立时段的回放或 Paper 对照。</p></div></div>
<h3>你关注的 23 点时段</h3>__HOURS__<p>23:00–23:59 共 14 笔，终局方向正确 12 笔，本地 +$22.72，实际现金归属 +$18.48；00 点段本地 -$13.43。与此前观察方向一致，但覆盖的夜晚很少，并且存在停机、资金和外部交易干扰，不能据此直接保证某一时段长期盈利。</p>
<h3>止盈止损没有在这 5 笔里整体改善结果</h3>__TP__<p>相同入场仅持有到期的本地结果约 -$8.83，实际止盈止损本地结果 -$9.94，差约 -$1.11。其中 03:10 的 Down 先止损，随后最终获胜；另有一次止损卖单未成交而承受终局亏损。样本仅 5 笔，不能从中拟合一组“必胜”的止盈止损阈值。</p></section>
<section id="execution"><h2>开仓与执行复盘</h2><p>误报修复后，11-02 22:35 至 11-03 03:25，主账户实际成交 <b>42 笔</b>，覆盖 58 个五分钟市场中的 36 个；这一段本地净亏 $9.70，现金归属净亏 $8.17。开仓频率恢复，并不等于收益改善。</p><p>从市场结束到本地记入结算，中位约 <b>6.5 分钟</b>，90 分位约 7.3 分钟。到期持仓在此期间继续占用名额；这描述的是本地账本释放延迟，不能仅凭它断言官方结算本身延迟。实际同时持仓最多 4 笔；虽然上限改为 5，$21 风险占用额度仍可能先约束 $5 的下一笔预算。</p><p>所有 81 笔程序订单现已终结：73 笔有官方确认成交（69 买入、4 卖出），8 笔到期多源核对无成交（7 买入、1 卖出）。没有遗留 UNKNOWN/PENDING、隔离订单或活动对账故障。未成交记录保留，未制造成交盈亏。</p>
<h3>19:33–00:15 的自动观察结果</h3>__OBS__<p class="muted">共 3377 次约 5 秒采样，记录到 25 次主账户买入订单、23 次已确认成交。表格是“首先遇到的等待原因”，不能换算成遗漏了多少次有效交易。报价过期还可能包含已结束市场缺少双边盘口，不能全部归咎 VPN。</p>
<p>之后的长期无新单还包括：03:27 起余额不足、03:35 改为 $3 后最小份额检查不通过、15 点后现金一度仅 $0.017879，以及运行中断和地区状态查询。最新 $50.78 入金发生在 22:58，接近停服时间，不代表此前一直有充足余额。</p>
<div class="warn"><b>后续优先级：</b>先同步外部买卖与赎回、修正多档成交成本、迁移 v2 活动查询、补参数可执行性检查；再用不混入外部操作的记录检验策略。对同轮换向、弱距离信号和退出机制做独立对照，暂不依据这批小样本扩大实盘规模。本次没有修改策略、资金设置或重启服务。</div></section>
<section id="records"><h2>全部记录，可筛选查看</h2><div class="controls"><button class="active" data-tab="positions">69 笔持仓</button><button data-tab="orders">81 笔订单</button><button data-tab="official">130 笔官方成交</button><select id="strategy"><option value="">全部来源</option><option>主账户</option><option>主账户（优化版）</option><option>主账户止盈止损</option><option>价值确认 V1</option><option>程序外提交</option></select><input id="search" placeholder="搜索时间、市场、方向、错误原因"><label><input id="loss" type="checkbox" style="min-width:0">仅本地亏损持仓</label></div><p id="count" class="muted"></p><div class="scroll"><table id="detail"></table></div><div class="controls"><button id="previous">上一页</button><span id="page"></span><button id="next">下一页</button></div></section>
<section id="files"><h2>导出与核对证据</h2><p><a href="全部81笔程序订单.csv">全部 81 笔程序订单 CSV</a>　<a href="全部69笔持仓复盘.csv">全部 69 笔持仓复盘 CSV</a></p><p><a href="同钱包130笔官方成交.csv">同钱包 130 笔官方成交 CSV</a>　<a href="同钱包395条资金活动.csv">395 条资金活动 CSV</a></p><p><a href="analysis.json">完整计算结果 JSON</a>　<a href="trade-reconciliation.json">成交核对结果</a>　<a href="official-wallet-trades.json">官方余额及成交快照</a>　<a href="official-activity-v2.json">完整 v2 资金活动快照</a></p><p class="muted">账本来自停服后的 SQLite 一致性备份；官方访问仅使用 GET，没有发起订单、卖出、提现、授权或赎回。原服务与观察进程均已停止，数据库保留。</p></section>
<p class="foot">统计依赖已记录的实际成交、官方资金流和本地状态；没有用 Paper 盈亏替代实盘。价格路径完整回放、未采集时段的漏单数量和未来盈利能力仍无法从这批数据证明。报告可离线打开，不加载任何外部脚本。</p></main>
<script type="application/json" id="data">__DATA__</script><script>
const data=JSON.parse(document.getElementById('data').textContent);let tab='positions',page=0;const size=20;
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dollars=v=>`<span class="${v>0?'pos':v<0?'neg':''}">${Number(v).toFixed(2)}</span>`;
function render(){const source=document.getElementById('strategy').value,query=document.getElementById('search').value.toLowerCase(),loss=document.getElementById('loss').checked;let rows=data[tab].filter(r=>(!source||(r.strategy_name||r.strategy||r.source)===source)&&(!query||JSON.stringify(r).toLowerCase().includes(query))&&(!loss||tab!=='positions'||r.realized_pnl<0));rows=rows.slice().reverse();const pages=Math.max(1,Math.ceil(rows.length/size));page=Math.max(0,Math.min(page,pages-1));document.getElementById('count').textContent=`筛选后 ${rows.length} 条；所有金额为美元。现金结果含程序外退出。`;document.getElementById('page').textContent=`${page+1} / ${pages}`;document.getElementById('previous').disabled=page===0;document.getElementById('next').disabled=page>=pages-1;let headers,body;
if(tab==='positions'){headers=['编号 / 策略','开仓时间 / 市场','方向 / 状态','本地成本','本地净盈亏','现金结果','外部卖出份额','原因'];body=rows.slice(page*size,(page+1)*size).map(r=>[esc(r.id)+' · '+esc(r.strategy_name),esc(r.opened_local)+'<br><small>'+esc(r.round_id)+'</small>',esc(r.outcome)+' / '+esc(r.status),Number(r.total_cost).toFixed(2),dollars(r.realized_pnl),dollars(r.cash_result),Number(r.external_sale_shares).toFixed(6),'<span class="reason">'+esc(r.entry_reason)+'</span>']);}
else if(tab==='orders'){headers=['策略 / 编号','时间 / 市场','买卖 / 方向','状态','预算','已成交份额','手续费','说明'];body=rows.slice(page*size,(page+1)*size).map(r=>[esc(r.strategy)+'<br><small>'+esc(r.id)+'</small>',esc(r.time)+'<br><small>'+esc(r.round)+'</small>',esc(r.action)+' / '+esc(r.outcome),esc(r.status),Number(r.cash).toFixed(2),Number(r.shares).toFixed(6),Number(r.fee).toFixed(6),'<span class="reason">'+esc(r.error||r.reason)+'</span>']);}
else{headers=['来源','时间 / 市场','买卖 / 方向','份额','实际现金流','程序订单'];body=rows.slice(page*size,(page+1)*size).map(r=>[esc(r.source),esc(r.time)+'<br><small>'+esc(r.round)+'</small>',esc(r.side)+' / '+esc(r.outcome),Number(r.shares).toFixed(6),Number(r.cash).toFixed(6),esc(r.order||'程序外提交')]);}
document.getElementById('detail').innerHTML='<thead><tr>'+headers.map(h=>'<th>'+h+'</th>').join('')+'</tr></thead><tbody>'+body.map(row=>'<tr>'+row.map(v=>'<td>'+v+'</td>').join('')+'</tr>').join('')+'</tbody>';}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{tab=b.dataset.tab;page=0;document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x===b));render();});['strategy','search','loss'].forEach(id=>document.getElementById(id).addEventListener('input',()=>{page=0;render();}));document.getElementById('previous').onclick=()=>{page--;render();};document.getElementById('next').onclick=()=>{page++;render();};render();
</script></html>'''
for key,value in {'__STRATEGY__':strategy_table,'__EXTERNAL__':external_table,'__PAIRS__':pair_table,'__HOURS__':hour_table,'__DISTANCE__':distance_table,'__TP__':tp_table,'__OBS__':obs_table,'__DATA__':embedded}.items():html_page=html_page.replace(key,value)
(ROOT/'实盘完整复盘.html').write_text(html_page,encoding='utf-8')
script=html_page.rsplit('<script>',1)[1].split('</script>',1)[0];(ROOT/'report-script.js').write_text(script)
print(ROOT/'实盘完整复盘.html')
