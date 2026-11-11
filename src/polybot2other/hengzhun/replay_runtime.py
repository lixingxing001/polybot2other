"""完整策略因果重放：同一冻结数据分别运行三个独立账户。

不读取网络/密钥。未提供模型、已核验合同或时钟证据时保留拒绝记录，
不会用空策略或零系数伪造收益。原始字节是否核验与归一化事件重放分开标注。
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from itertools import groupby
import json
import math
from pathlib import Path
from types import SimpleNamespace

from .config import load_config,config_hash
from .contracts import canonical,digest,validate_record,contract_hash
from .execution import SCENARIOS
from .runtime import HengzhunRuntime


@dataclass
class ReplayMarket:
    round_id:str
    condition_id:str
    slug:str
    started_at:float
    ends_at:float
    up_token:str
    down_token:str
    target_price:float
    question:str='FROZEN_CONTRACT_REPLAY'


class ReplayClock:
    def __init__(self,now,override=None):
        if override is not None and (isinstance(override,bool) or not isinstance(override,(int,float)) or not math.isfinite(override) or override<0):
            raise ValueError('INVALID_REPLAY_CLOCK_BOUND')
        self.now=now;self.override=override;self.evidence=None
    def snapshot(self):
        if self.override is not None:return {'offset_bound_ms':self.override,'status':'ASSUMED_REPLAY_CLOCK_BOUND'}
        value=self.evidence or {};at=value.get('checked_at')
        if type(at) is not int or not 0<=self.now()-at<=120000:return {'offset_bound_ms':None,'status':'CLOCK_UNVERIFIED'}
        bound=value.get('offset_bound_ms')
        if isinstance(bound,bool) or not isinstance(bound,(int,float)) or not math.isfinite(bound) or bound<0:return {'offset_bound_ms':None,'status':'CLOCK_UNVERIFIED'}
        return {**value,'offset_bound_ms':bound+(self.now()-at)*15/1000000}


def _contracts(values):
    if values is None:return []
    if isinstance(values,dict):
        if values.get('record_type')=='MarketContractSnapshot':return [values]
        return list(values.values())
    return list(values)


def _book_payload(event):
    payload=deepcopy(event['payload']);book=payload.get('book',payload)
    if 'event_type' in payload:return payload
    if not event.get('asset_id'):raise ValueError('REPLAY_BOOK_ASSET_MISSING')
    if book.get('is_complete',book.get('complete')) is not True:
        # Empty sides create an explicit invalid reconstruction tombstone.
        return {'event_type':'book','timestamp':event['source_at'],'asset_id':event['asset_id'],'bids':[],'asks':[]}
    return {'event_type':'book','timestamp':event['source_at'],'asset_id':event['asset_id'],
        **{side:[{'price':x['price'],'size':x.get('quantity_shares',x.get('size'))} for x in book[side]] for side in ('bids','asks')}}


def run_strategy_scenarios(events,output_directory,config=None,model_artifact=None,contracts=None,final_at=None,clock_bound_ms=None,*,model_artifacts=None,trade_start_at=None,evaluation_end_at=None,baseline_policy_factory=None):
    data=deepcopy(list(events));last=-1;sequences={}
    for event in data:
        validate_record(event,definition='RawEvent')
        at=event['received_at'];boot=event['boot_id'];seq=event['ingest_seq']
        if at<last or seq<=sequences.get(boot,-1):raise ValueError('REPLAY_ORDER_UNDETERMINED')
        sequences[boot]=seq;last=at
    if not data:raise ValueError('REPLAY_DATA_EMPTY')
    if final_at is not None and (type(final_at) is not int or final_at<last):raise ValueError('REPLAY_FINAL_TIME_REGRESSION')
    frozen=_contracts(contracts);available=[]
    for c in frozen:
        validate_record(c,definition='MarketContractSnapshot')
        if c['contract_sha256']!=contract_hash(c):raise ValueError('CONTRACT_HASH_MISMATCH')
        available.append((c['captured_at'],deepcopy(c)))
    if model_artifact is not None:validate_record(model_artifact,'model_artifact.schema.json')
    schedule=deepcopy(list(model_artifacts or []))
    for artifact in schedule:validate_record(artifact,'model_artifact.schema.json')
    root=Path(output_directory);root.mkdir(parents=True,exist_ok=True)
    data_sha=digest(data);model_sha=digest(schedule) if schedule else digest(model_artifact) if model_artifact is not None else None
    base=load_config(config);base['runtime']['mode']='REPLAY';results={}
    for scenario in SCENARIOS:
        folder=root/scenario;folder.mkdir(exist_ok=False)
        cfg=load_config(base,scenario=scenario)
        run_id=digest({'input':data_sha,'config':config_hash(cfg),'model':model_sha,'contracts':frozen,'scenario':scenario})
        policy=baseline_policy_factory(scenario,cfg,data_sha,run_id) if baseline_policy_factory else (None,None)
        registration,decider=policy
        if registration:run_id=digest([run_id,registration])
        now=[data[0]['received_at']];clock=ReplayClock(lambda:now[0],clock_bound_ms)
        feed=SimpleNamespace(market=None,references=SimpleNamespace(observer=None),books=SimpleNamespace(observer=None),research_observer=None)
        feed.observation=lambda:(feed.market,None,None)
        (folder/'run.json').write_text(canonical({'run_id':run_id,'started_at':now[0],'config_sha256':config_hash(cfg),
            'source':'CAUSAL_FULL_STRATEGY_REPLAY','input_sha256':data_sha,'model_sha256':model_sha}))
        if model_artifact is not None:
            (folder/'models').mkdir();(folder/'models/current.json').write_text(canonical(model_artifact))
        contract_dir=folder/'verified-contracts';contract_dir.mkdir()
        runtime=HengzhunRuntime(folder,feed,config=cfg,clock=clock,event_clock=lambda:now[0],
            replay_baseline=registration,baseline_decider=decider,trade_start_at=trade_start_at,evaluation_end_at=evaluation_end_at)
        observed=deepcopy(available);known_hashes=set();pending_venue=[]
        original_venue_events=runtime._venue_events
        runtime._venue_event=pending_venue.append
        runtime._venue_events=pending_venue.extend
        tick_at=now[0];daily_nav=[];pending_decision=None;last_selected_market=None;maximum_drawdown=Decimal(0);valuation_complete=True

        def select_market(at):
            nonlocal last_selected_market
            eligible=[(seen,c) for seen,c in observed if seen<=at and c['captured_at']<=at and c['window_open_at']<=at]
            if not eligible:feed.market=None;return
            seen,c=max(eligible,key=lambda row:(row[1]['window_open_at'],row[0]))
            if c['contract_sha256'] not in known_hashes:
                (contract_dir/(c['market']['condition_id']+'.json')).write_text(canonical(c));known_hashes.add(c['contract_sha256'])
            if schedule and last_selected_market!=c['market']['market_uid']:
                usable=[a for a in schedule if a['release'].get('status')=='RELEASED' and a['release'].get('available_at') is not None
                        and a['release']['available_at']<=min(at,c['window_open_at'])]
                if usable:
                    selected=max(usable,key=lambda a:a['release']['available_at'])
                    runtime.model_path.write_text(canonical(selected))
                last_selected_market=c['market']['market_uid']
            feed.market=ReplayMarket(c['market']['market_uid'],c['market']['condition_id'],c['slug'],c['window_open_at']/1000,
                c['window_close_at']/1000,c['up_asset_id'],c['down_asset_id'],float(c['baseline']['value'] or 0))
            runtime.contract=runtime._contract(feed.market,at)

        def flush_venue():
            if not pending_venue:return
            events_now=list(pending_venue);pending_venue.clear()
            original_venue_events(events_now)

        def decide(at):
            nonlocal pending_decision,maximum_drawdown,valuation_complete
            now[0]=at;select_market(at);flush_venue();runtime.tick(at)
            valued=runtime.ledger.snapshot('HENGZHUN',at)
            if valued['nav'] is not None and Decimal(valued['high_water_nav'])>0:
                maximum_drawdown=max(maximum_drawdown,(Decimal(valued['high_water_nav'])-Decimal(valued['nav']))/Decimal(valued['high_water_nav']))
            elif valued['open_risk_slots_used'] or valued['unknown_exposure']:valuation_complete=False
            pending_decision=runtime.last_decision_at+cfg['runtime']['decision_interval_ms'] if at<runtime.last_decision_at+cfg['runtime']['decision_interval_ms'] and runtime.last_decision_at!=at else None
            if (at+1)%86400000==0 and (trade_start_at is None or at>=trade_start_at) and (evaluation_end_at is None or at<evaluation_end_at):
                wallet=runtime.ledger.snapshot('HENGZHUN',at)
                daily_nav.append({'at':at,'nav':wallet.get('nav'),'valuation_status':wallet.get('valuation_status'),
                    'complete':wallet.get('valuation_status')=='VERIFIED','open_risk_slots_used':wallet.get('open_risk_slots_used'),
                    'high_water_nav':wallet.get('high_water_nav'),'drawdown_halt':wallet.get('drawdown_halt')})

        def next_timer(after):
            times=[]
            if pending_decision is not None:times.append(pending_decision)
            if trade_start_at is not None:times.append(trade_start_at)
            if evaluation_end_at is not None:times.append(evaluation_end_at)
            for seen,c in observed:
                times.extend((max(seen,c['captured_at'],c['window_open_at']),c['window_close_at']))
                times.extend(c['window_close_at']-target*1000 for target in cfg['calibration']['landmarks_seconds'])
                times.extend((c['window_close_at']-cfg['entry']['submit_remaining_max_seconds']*1000,
                              c['window_close_at']-cfg['entry']['submit_remaining_min_seconds']*1000+1))
            for ref in runtime.reference.values():
                times.extend((ref['source_at']+cfg['data']['price_source_max_age_ms']+1,ref['received_at']+cfg['data']['price_receive_max_age_ms']+1))
            for views in runtime.books.views.values():
                if views:
                    book=views[-1][0];times.extend((book['source_at']+cfg['data']['book_source_max_age_ms']+1,book['received_at']+cfg['data']['book_receive_max_age_ms']+1))
            if runtime.healthy_since is not None:times.append(runtime.healthy_since+cfg['data']['recovery_healthy_seconds']*1000)
            checked=(clock.evidence or {}).get('checked_at')
            if type(checked) is int:times.append(checked+120001)
            with runtime.ledger.lock:
                for row in runtime.ledger.conn.execute('SELECT payload FROM hz_venue_tasks'):
                    task=json.loads(row[0])
                    if task['phase']=='SCHEDULED':times.append(task['arrival_at'])
                    if task['phase']=='MATCHED' and task['confirm_at'] is not None:times.append(task['confirm_at'])
            for order in runtime.ledger.intents():
                if order['position_state']=='CLAIM_PENDING':times.append(order['claim_available_at'])
            times.append(((after+1)//86400000+1)*86400000-1)
            eligible=[at for at in times if at>after]
            return min(eligible) if eligible else None

        def before(at):
            while True:
                timer=next_timer(now[0])
                if timer is None or timer>=at:break
                decide(timer)


        try:
            for at,group in groupby(data,key=lambda e:e['received_at']):
                before(at)
                now[0]=at
                group=list(group)
                # Contract knowledge becomes visible only at the recorded receipt.
                for event in group:
                    p=event['payload'];candidate=p.get('contract',p) if isinstance(p,dict) else None
                    if candidate and candidate.get('record_type')=='MarketContractSnapshot':
                        validate_record(candidate,definition='MarketContractSnapshot')
                        if candidate['contract_sha256']!=contract_hash(candidate):raise ValueError('CONTRACT_HASH_MISMATCH')
                        observed.append((at,candidate))
                select_market(at)
                for event in group:
                    if event.get('source_id')=='hengzhun-input-provenance':continue
                    kind=event['source_kind'];p=event['payload']
                    if isinstance(p,dict) and p.get('record_type')=='B1MarketInput':runtime.baseline_inputs=deepcopy(p)
                    if kind in ('OFFICIAL_SPOT','OFFICIAL_TWAP'):
                        if not isinstance(p,dict) or 'channel' not in p or 'payload' not in p:raise ValueError('REPLAY_REFERENCE_PAYLOAD_UNSUPPORTED')
                        runtime.ingest('REFERENCE',p,at)
                    elif kind=='ORDER_BOOK':runtime.ingest('BOOK',_book_payload(event),at)
                    elif kind=='CLOCK':clock.evidence=deepcopy(p);runtime.ingest('CLOCK',p,at)
                    elif kind=='OFFICIAL_RESULT':
                        result=p.get('result',p)
                        if result.get('record_type')=='OfficialResult':runtime.ingest('RESULT',result,at)
                        else:runtime.ingest('RESULT',p,at)
                    elif kind=='OTHER' and event.get('source_id')=='collector-health':
                        stream=event.get('stream_id');runtime.ingest('REFERENCE_HEALTH' if stream=='reference' else 'BOOK_HEALTH',p,at)
                    elif kind=='OTHER' and p.get('fault_type'):
                        # Explicit preregistered venue faults retain their input event clocks.
                        pending_venue.append(event)
                    else:runtime.ingest('METADATA',p,at)
                decide(at)
            end=last if final_at is None else final_at
            before(end)
            if now[0]<end:decide(end)
            rows=runtime.ledger.intents('HENGZHUN');snapshot=runtime.snapshot()
            # Hash stable economic fields; capture run/boot IDs are evidence, not PnL inputs.
            ledger_events=runtime.ledger.events();wallet=snapshot['wallet']
            balances={key:Decimal(0) for key in ('CASH_CONFIRMED','CASH_RESERVED','CLAIM_COST_COMMITTED','CLAIM_RECEIVABLE','POSITION_COST_UNRESOLVED','REALIZED_PNL')}
            for event in ledger_events:
                for delta in event['deltas']:
                    if delta['account'] in balances:balances[delta['account']]+=Decimal(delta['delta'])
            differences=[{'account':key,'ledger_sum':str(total),'wallet_value':wallet[key.lower()]} for key,total in balances.items() if total!=Decimal(wallet[key.lower()])]
            financial={'wallet':snapshot['wallet'],'intents':[row['payload'] for row in rows],
                       'ledger_events':runtime.ledger.events()}
            skipped=sum(event.get('source_id')=='hengzhun-input-provenance' for event in data)
            result={'scenario_id':scenario,'economic_space':runtime.namespace,'events_replayed':len(data)-skipped,
                'input_event_count':len(data),'derived_provenance_events_skipped':skipped,
                'input_sha256':data_sha,'model_sha256':model_sha,'execution_evidence':'ASSUMED',
                'strategy_id':runtime.strategy_id,'config_sha256':runtime.config_sha,'code_manifest_sha256':runtime.code_sha,
                'fixed_seed':cfg['runtime']['seed'],'maximum_drawdown':str(maximum_drawdown),
                'maximum_drawdown_complete':valuation_complete,'unexplained_reconciliation_differences':len(differences),
                'reconciliation':{'kind':'INTERNAL_LEDGER_CONSERVATION','checked_accounts':list(balances),'differences':differences},
                'baseline_registration':registration,'daily_nav':daily_nav,
                'feature_database':str(folder/'events.sqlite3'),'ledger_database':str(folder/'ledger.sqlite3'),
                'ledger_event_sha256':digest(runtime.ledger.events()),
                'replay_kind':'FULL_STRATEGY_CAUSAL','raw_source_bytes_verified':False,
                'clock_evidence':'ASSUMED_OVERRIDE' if clock_bound_ms is not None else 'RECORDED_CLOCK_EVENTS',
                'counts':runtime.journal.counts(),'reason_codes':snapshot['reason_codes'],
                'model_status':snapshot['model_status'],'wallets':[snapshot['wallet']],
                'financial_stats':snapshot['financial_stats'],'signal_shadow':snapshot['signal_shadow'],'intents':[row['payload'] for row in rows],
                'economic_result_sha256':digest(financial),'economic_validation':'INSUFFICIENT'}
            (folder/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));results[scenario]=result
        finally:
            if runtime.shadow:runtime.shadow.close()
            runtime.journal.conn.close();runtime.ledger.close()
    report={'replay_kind':'FULL_STRATEGY_CAUSAL','execution_evidence':'ASSUMED','economic_validation':'INSUFFICIENT',
        'all_registered_scenarios':list(SCENARIOS),'scenarios':results,
        'note':'每个情景独立重新计算特征、概率、意图、风险和资金路径。未核验原始字节或合格研究数据时不得作收益改善声明。'}
    (root/'scenarios.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report
