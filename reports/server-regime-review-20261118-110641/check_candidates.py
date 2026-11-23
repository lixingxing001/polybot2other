from pathlib import Path
import collections
import datetime as dt
import json
import math
import statistics as st
import random

ROOT=Path(__file__).resolve().parent
rows=json.loads((ROOT/'trade-features.json').read_text())
rows.sort(key=lambda r:(r['decision_at'],r['strategy'],r['id']))
groups=collections.defaultdict(list)
for r in rows:groups[r['strategy']].append(r)
# Fixed simple hypotheses; no threshold search or best-rule promotion.
for key, group in groups.items():
    first={}
    for r in group:
        first.setdefault(r['round_id'],r)
    observations=sorted(first.values(),key=lambda r:(r['settled_at'],r['id']))
    for r in group:
        known=[q for q in observations if q['settled_at']<r['decision_at'] and q['round_id']!=r['round_id']]
        recent=known[-20:]
        r['prior20_n']=len(recent)
        r['prior20_roi']=st.mean(q['roi'] for q in recent) if len(recent)==20 else None
        r['prior20_win_rate']=st.mean(q['won'] for q in recent) if len(recent)==20 else None
        r['prior20_score_error']=st.mean(q['won']-q['confidence'] for q in recent) if len(recent)==20 else None

RULES={
    'baseline':lambda r:True,
    'momentum60_same_direction':lambda r:r['momentum_60_aligned'] is not None and r['momentum_60_aligned']>0,
    'short_vol_not_above_15min':lambda r:r['sigma_ratio_120_900'] is not None and r['sigma_ratio_120_900']<=1,
    'round_crossings_at_most_one':lambda r:r['known_round_crossings'] is not None and r['known_round_crossings']<=1,
    'score_at_least_075':lambda r:r['confidence']>=.75,
    'submission_edge_at_least_005':lambda r:r['submission_edge']>=.05,
    'last20_settled_rounds_profitable':lambda r:r['prior20_roi'] is not None and r['prior20_roi']>0,
}
def stats(q):
    return {'n':len(q),'markets':len({r['round_id'] for r in q}),'wins':sum(r['won'] for r in q),
            'win_rate':st.mean(r['won'] for r in q) if q else None,
            'pnl':sum(r['pnl'] for r in q),'normalized_5':sum(r['normalized_5'] for r in q),
            'mean_roi':st.mean(r['roi'] for r in q) if q else None}
result={'caveat':'Retrospective deletion of already executed baseline trades, not a full order-book replay or untouched prospective validation. Previously inspected outcomes; no rule selected for deployment. Missing input blocks that rule. Five accounts overlap in market exposure.',
        'rules':{},'rolling_detection':{},'market_group_summary':{}}
for name, rule in RULES.items():
    result['rules'][name]={}
    for phase in ['earlier','evening','night','morning']:
        baseline=[r for r in rows if r['period']==phase]
        accepted=[r for r in baseline if rule(r)]
        result['rules'][name][phase]={'all':stats(accepted),**{k:stats([r for r in accepted if r['strategy']==k]) for k in groups},
                                    'missed_positive_pnl':sum(r['pnl'] for r in baseline if not rule(r) and r['pnl']>0),
                                    'avoided_negative_pnl':-sum(r['pnl'] for r in baseline if not rule(r) and r['pnl']<0)}
    print(name,json.dumps({phase:{k:v['all'][k] for k in ['n','mean_roi','normalized_5']} for phase,v in result['rules'][name].items()},ensure_ascii=False))
for key, group in groups.items():
    q=[r for r in group if r['period']=='night']
    first=next((r for r in q if RULES['last20_settled_rounds_profitable'](r)),None)
    result['rolling_detection'][key]={'first_positive_night_decision':first['decision_cst'] if first else None,
                                     'prior20_roi_at_first':first['prior20_roi'] if first else None,
                                     'night_pnl_before_that':sum(r['pnl'] for r in q if first and r['decision_at']<first['decision_at']),
                                     'night_total_pnl':sum(r['pnl'] for r in q)}
print('rolling_detection',json.dumps(result['rolling_detection'],ensure_ascii=False))

# Group correlated accounts and reverse entries within a single market.
# This diagnostic weights each observed market once; it is not a deployable portfolio.
for phase in ['earlier','evening','night','morning']:
    market_rows=collections.defaultdict(list)
    for r in rows:
        if r['period']==phase:market_rows[r['round_id']].append(r)
    observations=[{'round_id':k,'mean_roi':st.mean(r['roi'] for r in v),'win_share':st.mean(r['won'] for r in v),
                  'score':st.mean(r['confidence'] for r in v),'cost':st.mean(r['all_in_cost'] for r in v)} for k,v in market_rows.items()]
    result['market_group_summary'][phase]={'n':len(observations),'mean_roi':st.mean(r['mean_roi'] for r in observations) if observations else None,
                                         'mean_win_share':st.mean(r['win_share'] for r in observations) if observations else None,
                                         'mean_score':st.mean(r['score'] for r in observations) if observations else None,
                                         'mean_cost':st.mean(r['cost'] for r in observations) if observations else None,
                                         'positive_markets':sum(r['mean_roi']>0 for r in observations)}
print('market_group_summary',json.dumps(result['market_group_summary'],ensure_ascii=False))
(ROOT/'candidate-checks.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
(ROOT/'trade-features-with-rolling.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
