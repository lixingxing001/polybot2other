"""Offline review of the stopped ledger and captured official GET responses."""
from pathlib import Path
from collections import defaultdict,Counter
from datetime import datetime
import csv,json,math,re,sqlite3

ROOT=Path(__file__).resolve().parent
NAMES={'MAIN':'主账户','MAIN_OPTIMIZED':'主账户（优化版）','VALUE_CONFIRM_V1':'价值确认 V1','MAIN_TP_SL':'主账户止盈止损','EXTERNAL':'其他来源开仓'}
db=sqlite3.connect('file:'+str(ROOT/'accounts.sqlite3')+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
orders=[dict(r) for r in db.execute('SELECT * FROM orders ORDER BY created_at,id')]
positions=[dict(r) for r in db.execute('SELECT * FROM positions ORDER BY opened_at,id')]
fills=[dict(r) for r in db.execute('SELECT * FROM fills')]
rounds={r['round_id']:json.loads(r['json']) for r in db.execute('SELECT * FROM rounds')}
events=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY at,id')]
activity=json.loads((ROOT/'official-activity-v2.json').read_text())['activity']
official=json.loads((ROOT/'official-wallet-trades.json').read_text())
trades=official['trades'];order_by_external={o['external_id'].lower():o for o in orders};order_by_id={o['id']:o for o in orders}
position_by_id={p['id']:p for p in positions};tx_trades=defaultdict(list)
for t in trades:tx_trades[t['transaction_hash']].append(t)

def local(t):return datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S') if t else ''
def rounded(x):return round(x,6)
def csv_file(name,rows):
    if not rows:return
    with (ROOT/name).open('w',encoding='utf-8-sig',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=list(rows[0]));writer.writeheader()
        writer.writerows({k:rounded(v) if isinstance(v,float) else v for k,v in row.items()} for row in rows)

inventory=defaultdict(list);lots=[];allocations=[];ambiguous=[];unmatched_sales=[];historical_redemptions=[]
trade_activity=[];matched_events=set();own_actual_cash=defaultdict(float)
for index,row in enumerate(activity):
    kind=row['type'];token=row.get('token_id');cash=float(row['usdc_size']);size=float(row['size'])
    if kind=='TRADE':
        matches=[t for t in tx_trades[row['transaction_hash']] if t['asset_id']==token and t['side']==row['side'] and abs(float(t['size'])-size)<.000001]
        assert len(matches)==1,(index,len(matches))
        trade=matches[0];matched_events.add(trade['id']);order=order_by_external.get(trade['taker_order_id'].lower())
        origin=order['strategy_id'] if order else 'EXTERNAL'
        trade_activity.append({**row,'trade_id':trade['id'],'order_id':order['id'] if order else '',
                               'origin':origin,'source_label':NAMES[origin] if order else '程序外提交','local_time':local(row['timestamp'])})
        if order:own_actual_cash[order['id']]+=cash
        if row['side']=='BUY':
            lot={'origin':origin,'position_id':order['position_id'] if order else None,'trade_id':trade['id'],
                 'token':token,'timestamp':row['timestamp'],'initial':size,'remaining':size,'cost':cash,
                 'remaining_cost':cash,'returns':0.,'external_sale_cash':0.,'external_sale_shares':0.}
            lots.append(lot);inventory[token].append(lot);continue
        candidates=[l for l in inventory[token] if l['remaining']>1e-8]
        if order:
            candidates=[l for l in candidates if l['position_id']==order['position_id']]
        elif len({l['origin'] for l in candidates})>1:
            ambiguous.append({'at':row['timestamp'],'token':token,'origins':sorted({l['origin'] for l in candidates}),'shares':size})
    elif kind=='REDEEM':
        order=None;candidates=[l for l in inventory[token] if l['remaining']>1e-8]
    else:continue
    remaining=size
    for lot in candidates:
        take=min(remaining,lot['remaining'])
        if take<=0:continue
        proceeds=cash*take/size if size else 0
        basis=lot['remaining_cost']*take/lot['remaining']
        lot['remaining']-=take;lot['remaining_cost']-=basis;lot['returns']+=proceeds;remaining-=take
        if kind=='TRADE' and order is None:
            lot['external_sale_cash']+=proceeds;lot['external_sale_shares']+=take
        allocations.append({'type':kind,'timestamp':row['timestamp'],'origin':lot['origin'],'position_id':lot['position_id'],
                            'shares':take,'cash':proceeds,'cost_basis':basis,'external_execution':kind=='TRADE' and order is None})
        if remaining<1e-8:break
    if remaining>1e-5:
        extra={'type':kind,'timestamp':row['timestamp'],'token':token,'unknown_shares':remaining,'cash':cash*remaining/size if size else 0}
        (unmatched_sales if kind=='TRADE' else historical_redemptions).append(extra)
assert len(matched_events)==len(trades)
assert not unmatched_sales,unmatched_sales
assert not ambiguous,ambiguous

lot_by_position={l['position_id']:l for l in lots if l['position_id'] is not None}
resolution_by_round={p['round_id']:p['resolved_outcome'] for p in positions if p['resolved_outcome']}
for p in positions:
    o=order_by_id[p['entry_order']];m=rounds[p['round_id']];lot=lot_by_position[p['id']]
    p['strategy_name']=NAMES[p['strategy_id']];p['opened_local']=local(p['opened_at']);p['closed_local']=local(p['closed_at'])
    p['limit_price']=o['limit_price'];p['entry_reason']=o['reason'];p['actual_buy_cash']=lot['cost'];p['actual_returns']=lot['returns']
    p['cash_result']=lot['returns']-lot['cost'];p['residual_shares']=max(0,lot['remaining'])
    p['external_sale_cash']=lot['external_sale_cash'];p['external_sale_shares']=lot['external_sale_shares']
    p['fees']=p['buy_fee']+p['sell_fee'];p['entry_price']=o['confirmed_notional']/p['initial_shares']
    p['entry_seconds']=p['opened_at']-m['started_at'];p['settlement_delay']=p['closed_at']-m['ends_at']
    p['market_outcome']=resolution_by_round.get(p['round_id'])
    p['hold_result']=p['initial_shares']*(p['outcome']==p['market_outcome'])-p['total_cost'] if p['market_outcome'] else None
    for key,pattern in [('distance_bps',r'距离 ([+-]?[\d.]+)bps'),('signal_ask',r'ask ([\d.]+)'),('signal_edge',r'edge ([\d.]+)')]:
        match=re.search(pattern,o['reason']);p[key]=float(match.group(1)) if match else None

def stats(rows):
    positive=[p['realized_pnl'] for p in rows if p['realized_pnl']>0];negative=[p['realized_pnl'] for p in rows if p['realized_pnl']<0]
    win=sum(positive);loss=-sum(negative);meanwin=win/len(positive) if positive else 0;meanloss=loss/len(negative) if negative else 0
    balance=peak=drawdown=0.;points=[]
    for p in sorted(rows,key=lambda p:(p['closed_at'],p['id'])):
        balance+=p['realized_pnl'];peak=max(peak,balance);drawdown=max(drawdown,peak-balance);points.append({'at':p['closed_at'],'pnl':rounded(balance)})
    return {'positions':len(rows),'wins':len(positive),'losses':len(negative),'win_rate':len(positive)/len(rows) if rows else None,
            'ledger_pnl':rounded(win-loss),'cash_result':rounded(sum(p['cash_result'] for p in rows)),
            'fees':rounded(sum(p['fees'] for p in rows)),'cost':rounded(sum(p['total_cost'] for p in rows)),
            'actual_buy_cash':rounded(sum(p['actual_buy_cash'] for p in rows)),
            'gross_ledger_pnl_before_fees':rounded(win-loss+sum(p['fees'] for p in rows)),
            'average_win':rounded(meanwin),'average_loss':rounded(meanloss),
            'break_even_win_rate':meanloss/(meanwin+meanloss) if meanwin+meanloss else None,
            'profit_factor':win/loss if loss else None,'settled_drawdown':rounded(drawdown),'curve':points}

strategies={key:{'name':NAMES[key],**stats([p for p in positions if p['strategy_id']==key])} for key in NAMES if key!='EXTERNAL'}
main=[p for p in positions if p['strategy_id']=='MAIN'];byround=defaultdict(list)
for p in main:byround[p['round_id']].append(p)
pairs=[]
for rid,group in byround.items():
    if len({p['outcome'] for p in group})<2:continue
    group.sort(key=lambda p:p['opened_at']);cost=sum(p['total_cost'] for p in group)
    payouts={side:sum(p['initial_shares'] for p in group if p['outcome']==side) for side in ['Up','Down']}
    pairs.append({'round_id':rid,'local_start':local(rounds[rid]['started_at']),'position_ids':[p['id'] for p in group],
                  'total_cost':rounded(cost),'payouts':payouts,'best_hold_pnl':rounded(max(payouts.values())-cost),
                  'ledger_pnl':rounded(sum(p['realized_pnl'] for p in group)),'cash_result':rounded(sum(p['cash_result'] for p in group)),
                  'first_only_result':group[0]['realized_pnl'],'second_result':group[1]['realized_pnl']})

def grouped(rows,key):
    groups=defaultdict(list)
    for row in rows:groups[key(row)].append(row)
    return [{'group':k,**{x:v for x,v in stats(g).items() if x!='curve'}} for k,g in sorted(groups.items())]

initial=db.execute('SELECT balance FROM wallet_curve ORDER BY id LIMIT 1').fetchone()[0]
final=db.execute('SELECT balance FROM wallet_curve ORDER BY id DESC LIMIT 1').fetchone()[0]
cash_by_type=defaultdict(float)
for a in activity:cash_by_type[a['type']+('_'+a['side'] if a['type']=='TRADE' else '')]+=a['usdc_size']
deposits=cash_by_type['DEPOSIT'];cash_change=final-initial-deposits+cash_by_type['WITHDRAWAL']
assert abs(initial+deposits-cash_by_type['WITHDRAWAL']-cash_by_type['TRADE_BUY']+cash_by_type['TRADE_SELL']+cash_by_type['REDEEM']-final)<1e-5
external_lots=[l for l in lots if l['origin']=='EXTERNAL'];unattributed=sum(r['cash'] for r in historical_redemptions)
program_cash=sum(l['returns']-l['cost'] for l in lots if l['origin']!='EXTERNAL')
external_cash=sum(l['returns']-l['cost'] for l in external_lots)
assert abs(program_cash+external_cash+unattributed-cash_change)<1e-5
split=[p for p in main if p['external_sale_shares']>0]
z=1.96;n=len(main);phat=strategies['MAIN']['win_rate'];center=(phat+z*z/(2*n))/(1+z*z/n);half=z*math.sqrt(phat*(1-phat)/n+z*z/(4*n*n))/(1+z*z/n)
fee_reconciliation=[]
for o in orders:
    if o['id'] not in own_actual_cash:continue
    expected=o['confirmed_notional']+(o['confirmed_fee'] if o['action']=='BUY' else -o['confirmed_fee'])
    fee_reconciliation.append({'order_id':o['id'],'position_id':o['position_id'],'strategy':o['strategy_id'],'action':o['action'],
                               'ledger_cash':rounded(expected),'official_cash':rounded(own_actual_cash[o['id']]),
                               'cash_difference':rounded(own_actual_cash[o['id']]-expected)})

summary={'scope':{'from':local(min(o['created_at'] for o in orders)),'last_order':local(max(o['created_at'] for o in orders)),
                  'stopped_snapshot':json.loads((ROOT.parent/'live-review-current.json').read_text())['at'],
                  'orders':len(orders),'filled_orders':sum(o['state']=='FILLED' for o in orders),'expired_unfilled':sum(o['state']=='EXPIRED_UNFILLED' for o in orders),
                  'positions':len(positions),'official_trades':len(trades),'outside_order_trades':len(trades)-len(fills)},
         'strategies':strategies,'total':stats(positions),'cashflow':{'initial_cash':initial,'final_cash':final,'deposits':deposits,
                  'withdrawals':cash_by_type['WITHDRAWAL'],'wallet_net_cash_excluding_funding':rounded(cash_change),
                  'program_origin_net_cash_including_external_exits':rounded(program_cash),'external_origin_net_cash':rounded(external_cash),
                  'preexisting_redemption_cash':rounded(unattributed),'cash_by_type':dict(cash_by_type),'reconciliation_difference':0.},
         'external_exits':[{k:p[k] for k in ['id','opened_local','outcome','external_sale_shares','external_sale_cash','realized_pnl','cash_result','residual_shares']} for p in split],
         'pairs':pairs,'pair_totals':{'rounds':len(pairs),'positions':sum(len(p['position_ids']) for p in pairs),
                   'ledger_pnl':rounded(sum(p['ledger_pnl'] for p in pairs)),'cash_result':rounded(sum(p['cash_result'] for p in pairs)),
                   'first_only_result':rounded(sum(p['first_only_result'] for p in pairs)),'second_result':rounded(sum(p['second_result'] for p in pairs))},
         'main_first_five':stats(main[:5]),'main_after_first_five':stats(main[5:]),'win_rate_95_interval':[center-half,center+half],
         'main_by_date':grouped(main,lambda p:p['opened_local'][:10]),'main_by_hour':grouped(main,lambda p:p['opened_local'][11:13]),
         'main_by_direction':grouped(main,lambda p:p['outcome']),
         'main_by_distance':grouped(main,lambda p:'3.5–5 bps' if abs(p['distance_bps'])<5 else '5–8 bps' if abs(p['distance_bps'])<8 else '≥8 bps'),
         'main_by_price':grouped(main,lambda p:'<0.55' if round(p['entry_price'],4)<.55 else '0.55–0.65' if round(p['entry_price'],4)<.65 else '0.65–0.70' if round(p['entry_price'],4)<.70 else '≥0.70'),
         'main_by_elapsed':grouped(main,lambda p:'15–60秒' if p['entry_seconds']<60 else '60–180秒' if p['entry_seconds']<180 else '≥180秒'),
         'fee_cash_reconciliation':fee_reconciliation,'ambiguous_lot_allocations':ambiguous,'unmatched_sales':unmatched_sales,
         'unclaimed_bot_residual_estimate':rounded(sum(p['residual_shares'] for p in positions if p['outcome']==p['market_outcome'])),
         'tp_sl_hold_comparison':[{k:p[k] for k in ['id','opened_local','realized_pnl','hold_result','exit_reason','market_outcome']} for p in positions if p['strategy_id']=='MAIN_TP_SL'],
         'limitations':['本地盈亏未同步程序外卖出；实际现金归属结果含这些卖出，不能视为独立自动策略表现。',
                        '同一期同代币的已知程序卖出优先按持仓编号归属，其余按当时持仓来源分配；没有检测到跨来源归属歧义。',
                        '现金结果不包含未赎回的零碎获胜份额；交易所持仓汇总估值返回0。',
                        '时间、价格和距离分组是小样本描述，不能直接当作有效的未来过滤规则。',
                        '事件日志不是全部逐次信号记录，不能换算完整漏单率。']}
runtime_start=datetime(2026,11,2,22,35).timestamp();runtime_end=datetime(2026,11,3,3,25).timestamp()
recovered=[p for p in main if runtime_start<=p['opened_at']<runtime_end]
delays=sorted(p['settlement_delay'] for p in main)
live=peak=0
for at,change in sorted([(p['opened_at'],1) for p in main]+[(p['closed_at'],-1) for p in main]):
    live+=change;peak=max(peak,live)
small_stake_at=datetime(2026,11,3,3,35,32).timestamp()
later=Counter(e['message'] for e in events if e['kind']=='entry_blocked' and e['at']>=small_stake_at)
summary['runtime']={'current_settings':json.loads(db.execute('SELECT json FROM live_config').fetchone()[0]),
                    'settlement_delay_median_seconds':delays[len(delays)//2],'settlement_delay_p90_seconds':delays[int((len(delays)-1)*.9)],
                    'settlement_delay_max_seconds':max(delays),'maximum_simultaneous_main_positions':peak,
                    'after_recovery_window':{'from':local(runtime_start),'to':local(runtime_end),'five_minute_markets':int((runtime_end-runtime_start)/300),
                                             'entered_markets':len({p['round_id'] for p in recovered}),**stats(recovered)},
                    'stake_changed_to_3_at':local(small_stake_at),'later_block_events':dict(later.most_common())}
observer_pointer=ROOT.parent/'live-entry-observation-20261102/current.json'
if observer_pointer.exists():
    pointer=json.loads(observer_pointer.read_text());observation=json.loads((Path(pointer['output'])/'summary.json').read_text())
    summary['observation']={k:observation.get(k) for k in ['started_local','planned_end_local','samples','primary_reasons','both_books_fresh_samples','observer_errors','observation_gaps','buy_orders','orders_with_confirmed_fill','finished']}
(ROOT/'analysis.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));(ROOT/'positions-enriched.json').write_text(json.dumps(positions,ensure_ascii=False,indent=2))
(ROOT/'official-trade-activity-linked.json').write_text(json.dumps(trade_activity,ensure_ascii=False,indent=2))
csv_file('全部81笔程序订单.csv',[{'订单编号':o['id'],'策略':NAMES[o['strategy_id']],'提交时间':local(o['created_at']),'市场':o['round_id'],'方向':o['outcome'],'买卖':o['action'],'状态':o['state'],'预算':o['requested_cash'],'实际份额':o['confirmed_shares'],'本地成交额':o['confirmed_notional'],'本地手续费':o['confirmed_fee'],'官方现金流':own_actual_cash.get(o['id'],0),'最终错误说明':o['error'] or '','入场或退出原因':o['reason']} for o in orders])
csv_file('全部69笔持仓复盘.csv',[{'持仓编号':p['id'],'策略':p['strategy_name'],'开仓时间':p['opened_local'],'市场':p['round_id'],'方向':p['outcome'],'状态':p['status'],'投入成本_本地':p['total_cost'],'净盈亏_本地':p['realized_pnl'],'真实买入现金':p['actual_buy_cash'],'归属回款':p['actual_returns'],'现金结果_含外部卖出':p['cash_result'],'外部卖出份额':p['external_sale_shares'],'未处理零碎份额':p['residual_shares'],'手续费_本地':p['fees'],'信号距离bps':p['distance_bps'],'信号edge':p['signal_edge'],'开盘后入场秒数':p['entry_seconds'],'入场原因':p['entry_reason']} for p in positions])
csv_file('同钱包130笔官方成交.csv',[{'时间':r['local_time'],'来源':r['source_label'],'市场':r['slug'],'方向':r['outcome'],'买卖':r['side'],'份额':r['size'],'官方现金流':r['usdc_size'],'程序订单编号':r['order_id'],'官方成交编号':r['trade_id'],'链上交易':r['transaction_hash']} for r in trade_activity])
csv_file('同钱包395条资金活动.csv',[{'时间':local(r['timestamp']),'类型':r['type'],'市场':r['slug'],'方向':r['outcome'],'买卖':r['side'],'份额':r['size'],'现金金额':r['usdc_size'],'链上交易':r['transaction_hash']} for r in activity])
print(json.dumps({k:summary[k] for k in ['scope','cashflow','pair_totals','unclaimed_bot_residual_estimate','tp_sl_hold_comparison']},ensure_ascii=False,indent=2))
print('STRATEGIES',json.dumps({k:{a:b for a,b in v.items() if a!='curve'} for k,v in strategies.items()},ensure_ascii=False,indent=2))
