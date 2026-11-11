"""Isolated original MAIN Paper account using the exact frozen existing engine.

This reproduces original sizing/risk/accounting/confirmation rules. It is an
offline experiment on captured input frames; public-read latency and historical
thread scheduling are not recorded and are explicitly ASSUMED, never empirical.
No exchange, signer, real ledger or network callback is constructed.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields, replace
from datetime import datetime, timezone
import importlib
import json
import math
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

from .baseline import FrozenMainBaseline, SIGNAL_FIELDS
from .contracts import canonical, digest, validate_record


def _event_time(event):
    stamp=event.get('received_at',event.get('observed_at'))
    if type(stamp) is not int or stamp<0:raise ValueError('BASELINE_EVENT_CLOCK_REQUIRED')
    return stamp


def _payload(event):
    payload=event.get('payload',event)
    return payload.get('result',payload)


class OriginalMainAccountReplay:
    def __init__(self,baseline_folder,output_directory,*,started_at=0):
        # Full manifest/content verification precedes all module imports.
        frozen=FrozenMainBaseline(baseline_folder);self.frozen=frozen
        settings=json.loads((frozen.folder/'baseline-settings.json').read_text())
        if not all(key in settings for key in ('signal_settings','main_rule','execution_policy')):
            raise ValueError('BASELINE_ORIGINAL_SETTINGS_UNAVAILABLE')
        self.main_rule=deepcopy(settings['main_rule']);self.policy_settings=deepcopy(settings['execution_policy'])
        if any(key not in self.main_rule for key in ('stake_dollars','max_open_trades','max_daily_loss')):
            raise ValueError('BASELINE_ORIGINAL_RISK_UNAVAILABLE')
        if not all(isinstance(self.main_rule[k],(int,float)) and not isinstance(self.main_rule[k],bool)
                   and math.isfinite(self.main_rule[k]) and self.main_rule[k]>=0
                   for k in ('stake_dollars','max_open_trades','max_daily_loss')):
            raise ValueError('BASELINE_ORIGINAL_RISK_INVALID')
        if self.main_rule['stake_dollars']<=0 or self.main_rule['max_open_trades']<1:
            raise ValueError('BASELINE_ORIGINAL_RISK_INVALID')
        self.destination=Path(output_directory);self.destination.mkdir(parents=True,exist_ok=False)
        # The module namespace is per account. Injecting its module-local clock
        # cannot change time.time or another running strategy's module globals.
        self.package='_hengzhun_original_account_'+digest([str(self.destination.resolve()),frozen.manifest_sha256])[:24]
        package=ModuleType(self.package);package.__path__=[str(frozen.folder/'src/polybot2other')]
        sys.modules[self.package]=package
        self.storage=importlib.import_module(self.package+'.storage')
        self.execution=importlib.import_module(self.package+'.paper_execution')
        self.shared=importlib.import_module(self.package+'.shared_market_inputs')
        self.models=importlib.import_module(self.package+'.models')
        config=importlib.import_module(self.package+'.config')
        if set(self.policy_settings)!={f.name for f in fields(self.execution.ExecutionPolicy)}:
            raise ValueError('BASELINE_ORIGINAL_EXECUTION_POLICY_INCOMPLETE')
        self.now=started_at/1000;self.storage.time=SimpleNamespace(time=lambda:self.now)
        self.current=None;self.current_info=None;self.results={};self.result_revisions={}
        self.markets={};self.timeline=[];self.diagnostics=[];self.frame_count=0;self.complete_frame_count=0
        self.settings=config.Settings(**frozen.signal_settings)
        self.store=self.storage.TradeStore(self.destination/'original-main.sqlite3',100)
        bot=SimpleNamespace(store=self.store,settings=self.settings,_lock=threading.RLock(),
            paper_trading_paused=False,last_signal=None,_sync_shared_market_inputs=self.frame)
        feed=SimpleNamespace(references=SimpleNamespace(points=lambda *args:[]))
        self.bot=bot
        self.engine=self.execution.PaperExecution(bot,'MAIN',self.settings,frame_reader=self.frame,
            info_reader=self.info,book_reader=self.book,config_reader=self.config,feed=feed,
            policy=self.execution.ExecutionPolicy(**self.policy_settings),clock=lambda:self.now)
        self._sample()

    def close(self):
        self.store.conn.close()

    def config(self):
        # Old MAIN Paper intentionally ignores real-trading switches. The frozen
        # values remain evidence; no live manager is present in this process.
        return {'enabled':False,'revision':1,'strategies':{'MAIN':deepcopy(self.main_rule)}}

    def frame(self):
        if self.current is None:
            return self.shared.MarketInputs(None,{}, {},{},self.now,'BASELINE_NO_FRAME','BASELINE_INPUT_UNAVAILABLE')
        row=self.current['input'];market=self.models.MarketRound(**row['market']) if row['market'] else None
        return self.shared.MarketInputs(market,deepcopy(row['reference']),deepcopy(row['price']),deepcopy(row['quotes']),
            self.now,row['snapshot_id'],row['error'])

    def info(self,market,**kwargs):
        if not self.current or self.current['input']['market']['round_id']!=market.round_id or self.current_info is None:
            raise ValueError('BASELINE_MARKET_INFO_UNAVAILABLE')
        return self.execution.checked_info(deepcopy(self.current_info))

    def book(self,token):
        frame=self.frame()
        for quote in frame.quotes.values():
            if quote.get('token_id')==token:return deepcopy(quote)
        raise ValueError('BASELINE_BOOK_UNAVAILABLE')

    def _settle(self):
        for condition,result in self.results.items():
            market=self.markets.get(condition)
            if market is None:continue
            self.store.settle_round_outcome(market.round_id,result['outcome'].title(),self.now,
                final_price=None,target_price=market.target_price,settlement_source='polymarket_official')

    def _poll(self):
        self.engine.poll();self._settle();self._sample()

    def _sample(self):
        account=self.store.account();reserved=self.store.reserved_cash();receivable=self.engine.pending_cash() if hasattr(self,'engine') else 0
        positions=self.store.open_trades();frame=self.frame();liquidation=0.;valid=True
        pending=self.engine.pending() if hasattr(self,'engine') else []
        if any(order['phase']=='CONFIRMING' for order in pending):valid=False
        for position in positions:
            quote=frame.quotes.get(position['side'],{})
            mark=self.engine.mark(position,quote,{})
            if mark['valuation_status']!='fresh':valid=False
            else:liquidation+=mark['exit_value']
        nav=round(account['cash_balance']+reserved+receivable+liquidation,6) if valid else None
        row={'at_ms':round(self.now*1000),'cash':account['cash_balance'],'reserved':reserved,'receivable':receivable,
             'open_cost':sum(p['stake'] for p in positions),'realized_pnl':account['realized_pnl'],
             'conservative_nav':nav,'valuation_status':'VERIFIED' if valid else 'UNCERTAIN',
             'open_positions':len(positions),'pending_orders':len(pending)}
        if self.timeline and self.timeline[-1]['at_ms']==row['at_ms']:self.timeline[-1]=row
        else:self.timeline.append(row)

    def advance(self,at_ms,*,inclusive=True):
        if at_ms<round(self.now*1000):raise ValueError('BASELINE_RECEIVE_TIME_REGRESSION')
        # Process known timers using the then-visible frame, never the future
        # frame that caused this advancement. Blocked confirmations wait for new
        # evidence, rather than looping or fabricating a fresh quote.
        attempted=set();end=at_ms/1000
        while True:
            due={o['due_at'] for o in self.engine.pending() if o['due_at']>=self.now}
            due.update(r[0] for r in self.store.conn.execute('SELECT available_at FROM paper_sim_claims WHERE released_at IS NULL'))
            boundary=(int(self.now)//86400+1)*86400
            due.add(float(boundary))
            candidates=[t for t in due if t not in attempted and (t<=end if inclusive else t<end)]
            if not candidates:break
            moment=min(candidates);attempted.add(moment);self.now=moment;self._poll()
        self.now=end

    def on_event(self,event):
        at=_event_time(event);payload=_payload(event);self.advance(at,inclusive=False)
        kind=payload.get('record_type')
        if kind=='B1MarketInput':
            self.frame_count+=1
            if payload.get('derived') is not True or payload.get('observed_at',at)>at:raise ValueError('BASELINE_INPUT_NOT_VISIBLE')
            if payload.get('signal_settings')!=self.frozen.signal_settings:raise ValueError('BASELINE_CONFIG_CHANGED')
            if payload.get('main_rule')!=self.main_rule or payload.get('execution_policy')!=self.policy_settings:
                raise ValueError('BASELINE_ORIGINAL_POLICY_CHANGED_OR_MISSING')
            self.current=deepcopy(payload);self.current_info=deepcopy(payload.get('market_info'))
            frame=self.frame()
            if frame.market is not None:
                self.markets[frame.market.condition_id]=frame.market;self.store.upsert_round(frame.market)
            if self.current_info is not None:self.complete_frame_count+=1
            else:self.diagnostics.append({'at_ms':at,'reason':'BASELINE_MARKET_INFO_UNAVAILABLE'})
            self._poll();self.engine.decide();self._settle();self._sample()
        elif kind=='OfficialResult':
            validate_record(payload,definition='OfficialResult')
            if any(payload.get(k) is not None and payload[k]>at for k in ('received_at','verified_at','label_available_at')):
                raise ValueError('BASELINE_FUTURE_RESULT')
            if payload.get('official_final_verified') is True:
                condition=payload['market']['condition_id'];old=self.results.get(condition)
                if old and old['payouts']!=payload['payouts']:raise ValueError('BASELINE_ORIGINAL_RESULT_REVISION_UNSUPPORTED')
                if payload['outcome'] not in ('UP','DOWN'):
                    self.diagnostics.append({'at_ms':at,'reason':'BASELINE_SPECIAL_PAYOUT_UNSUPPORTED'})
                else:self.results[condition]=deepcopy(payload)
            self._poll()
        else:self._poll()

    def report(self,events):
        self._sample();tables={}
        for name in ('account','trades','paper_orders','paper_fills','paper_sim_orders','paper_sim_claims','paper_sim_events'):
            tables[name]=[dict(row) for row in self.store.conn.execute('SELECT * FROM '+name+' ORDER BY rowid')]
        pending=len(self.engine.pending());open_positions=len(self.store.open_trades());claims=len([r for r in tables['paper_sim_claims'] if r['released_at'] is None])
        missing=[]
        if not self.frame_count:missing.append('BASELINE_INPUT_UNAVAILABLE')
        if not self.complete_frame_count:missing.append('BASELINE_MARKET_INFO_UNAVAILABLE')
        if self.diagnostics:missing.extend(sorted({r['reason'] for r in self.diagnostics}))
        status='BASELINE_UNAVAILABLE' if missing else 'PENDING_TAIL' if pending or open_positions or claims else 'COMPLETED'
        data_hash=digest(events)
        return {'baseline':'B1_CURRENT_FROZEN_ORIGINAL_ACCOUNT','status':status,'missing':sorted(set(missing)),
            'source_scope':'CURRENT_AVAILABLE_CODE_SNAPSHOT','historical_original_version_verified':False,
            'baseline_manifest_sha256':self.frozen.manifest_sha256,'data_sha256':data_hash,
            'economic_space':{'mode':'REPLAY','portfolio_run_id':'B1-original-'+data_hash[:20],
                'scenario_id':'ORIGINAL_FROZEN_POLICY','ledger_scope':'PAPER_PORTFOLIO'},
            'initial_cash':100,'main_rule':self.main_rule,'execution_policy':self.policy_settings,
            'execution_evidence':'ASSUMED','decision_schedule':'RECORDED_FRAME_EVENTS',
            'latency_assumption':'Frozen arrival floor; public-read latency and original thread interleaving were not recorded.',
            'engine':'EXACT_FROZEN_PAPER_EXECUTION_AND_TRADE_STORE','new_hengzhun_risk_rules_applied':False,
            'arithmetic':'ORIGINAL_ENGINE_FLOAT_PRECISION_UNCHANGED','frames':self.frame_count,
            'complete_frames':self.complete_frame_count,'account':self.store.account(),'original_engine_metrics':self.store.metrics(),
            'economic_tail':{'pending_orders':pending,'open_positions':open_positions,'pending_claims':claims},
            'conservative_nav':self.timeline[-1]['conservative_nav'],'timeline':self.timeline,
            'financial_tables':tables,'financial_ledger_sha256':digest(tables),
            'diagnostics':self.diagnostics,'full_strategy_replay':True,'research_validation':'INSUFFICIENT'}


def run_original_main_account(events,baseline_folder,output_directory,*,final_at=None):
    events=deepcopy(list(events));last=-1
    for event in events:
        at=_event_time(event)
        if at<last:raise ValueError('BASELINE_RECEIVE_TIME_REGRESSION')
        last=at
    replay=OriginalMainAccountReplay(baseline_folder,output_directory,started_at=_event_time(events[0]) if events else 0)
    try:
        for event in events:replay.on_event(event)
        if final_at is not None:replay.advance(final_at);replay._poll()
        report=replay.report(events)
        (replay.destination/'report.json').write_text(canonical(report)+'\n')
        return report
    finally:replay.close()
