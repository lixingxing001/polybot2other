"""Compare unchanged live strategy behavior and aligned Paper inputs offline.
Historical prices/targets are real; the common fresh order book is synthetic.
This verifies input/strategy equivalence, not historical fills or profitability.
"""
from dataclasses import asdict
from pathlib import Path
from types import ModuleType,SimpleNamespace
from unittest.mock import patch
import json,sys,tempfile
from polybot2other.config import Settings
from polybot2other.models import MarketRound,Signal
from polybot2other.strategy import RealBtcFiveMinuteStrategy
from polybot2other.managed_live_signals import LiveSignals
from polybot2other.optimized_reference import SPOT,TWAP

ROOT=Path(__file__).resolve().parents[2]
REPORT=Path(__file__).resolve().parent
before=json.loads((REPORT/'before.json').read_text())
module=ModuleType('polybot2other._before_shared_input_verification');module.__package__='polybot2other'
sys.modules[module.__name__]=module
source=Path(before['backup'])/'src/polybot2other/managed_live_signals.py'
exec(compile(source.read_text(),str(source),'exec'),module.__dict__)
rows=json.loads((ROOT/'reports/paper-live-feed-review-20261105/comparison.json').read_text())['rows']
results=[];different=[];clock={}
with tempfile.TemporaryDirectory(prefix='polybot-input-replay-') as directory:
    tmp=Path(directory);settings=Settings(db_path=tmp/'paper.sqlite3',strategy_experiments_enabled=False,
        market_scout_enabled=False,live_trading_runtime_enabled=False,min_price_distance_bps=3.5)
    def store(name):
        return SimpleNamespace(path=tmp/name/'accounts.sqlite3',positions=lambda *a,**kw:[],
            daily_pnl=lambda *a:0,active_orders=lambda *a:[],risk_usage=lambda *a:{'slots':0})
    feed=SimpleNamespace(references=SimpleNamespace())
    aux=lambda:{'okx':clock['spot']*1.0001,'okx_updated_ms':clock['now']*1000,
                'binance_market':clock['spot']*1.0002,'binance_market_updated_ms':clock['now']*1000}
    previous=module.LiveSignals(settings,store('before'),feed,lambda:100,aux)
    current=LiveSignals(settings,store('after'),feed,lambda:100,aux)
    config={'stake_dollars':5,'max_open_trades':2,'max_daily_loss':21}
    info={'fee_rate':.07,'fee_exponent':1,'tick_size':'.01','neg_risk':False}
    for row in rows:
        now=row['at'];clock.update(now=now,spot=row['paper_btc']);start=int(row['round_id'].rsplit('-',1)[1])
        market=MarketRound(row['round_id'],'BTC',start,start+300,row['verified_official_target'],up_token='up',down_token='down')
        ref={'ready':True,'protocol_verified':True,'latest':{
            SPOT:{'price':row['paper_btc'],'source_ms':now*1000-100,'received_ms':now*1000-50},
            TWAP:{'price':row['verified_official_target'],'source_ms':now*1000-100,'received_ms':now*1000-50}}}
        quotes={side:{'token_id':token,'best_bid':ask-.01,'best_ask':ask,'bid_size':1000,'ask_size':1000,
                      'source_ms':now*1000-100,'received_ms':now*1000-50,'updated_at_ms':now*1000-100,
                      'bids':[{'price':ask-.01,'size':1000}],'asks':[{'price':ask,'size':1000}]}
                for side,token,ask in [('Up','up',.41),('Down','down',.61)]}
        with patch('time.time',return_value=now):
            old,old_limit=previous.evaluate('MAIN',config,market,ref,quotes,now,info)
            frame=current.market_inputs.build(market,ref,quotes,now)
            live,limit=current.evaluate('MAIN',config,market,ref,quotes,now,info,market_input=frame)
            paper=RealBtcFiveMinuteStrategy(settings).signal(frame.strategy_input())
            if frame.error:paper=Signal('BTC','NO_TRADE',0,0,0,frame.error)
            elif paper.side in ('Up','Down') and frame.quote_error(paper.side):
                paper=Signal('BTC','NO_TRADE',0,0,0,frame.quote_error(paper.side))
        result={'paper_trade_id':row['paper_id'],'round_id':row['round_id'],'signal':asdict(live),
                'paper_live_equal':asdict(live)==asdict(paper),'old_new_live_equal':asdict(old)==asdict(live) and old_limit==limit}
        results.append(result)
        if not result['paper_live_equal'] or not result['old_new_live_equal']:different.append(result)
report={'cases':len(results),'paper_live_equal':sum(r['paper_live_equal'] for r in results),
        'old_new_live_equal':sum(r['old_new_live_equal'] for r in results),'differences':different,
        'method':'Historical BTC prices and verified openings with an identical synthetic fresh book; frozen decision clocks. No execution, no financial writes.',
        'results':results}
(REPORT/'replay-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({k:v for k,v in report.items() if k!='results'},ensure_ascii=False,indent=2))
assert not different
