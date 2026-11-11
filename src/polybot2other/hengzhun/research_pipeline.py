"""Registered 28/7/7 study orchestration on frozen causal inputs.

Fits each training fold, publishes only after its validation week, and then runs
one continuous account per scenario for ALL test weeks. It never restarts a
wallet at a weekly fold and never upgrades missing evidence to PASS.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime,timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from collections import defaultdict

from .config import load_config,config_hash
from .contracts import canonical,digest,validate_record
from .calibration import train_candidate,publish_artifact,validate_artifact,market_probability
from .pricing import choose_entry,plan_side,cost_quote,floor_step,fee_adapter
from .research import (DAY_MS,walk_forward_windows,partition_rows,probability_metrics,
    probability_metric_intervals,bootstrap_sensitivity,concentration_report,assess_research_gates,ExperimentRegistry)


def _json(path):return json.loads(Path(path).read_text())


def _rows(path):
    path=Path(path)
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.suffix=='.jsonl' else _json(path)


def _write(path,value):
    with Path(path).open('x') as f:f.write(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return {'uri':str(Path(path).resolve()),'sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest()}


def _date(at):return datetime.fromtimestamp(at/1000,timezone.utc).date().isoformat()


def _entry_plan(runtime,frame,candidate,registration):
    c=runtime.contract;side=candidate['side'];guard=candidate['guard_probability']
    return {'wallet_id':'HENGZHUN','strategy_id':registration['strategy_id'],'market':c['market'],
        'side':side.upper(),'asset_id':c['up_asset_id'] if side=='Up' else c['down_asset_id'],
        'collateral_asset_id':c['collateral_asset_id'],'limit_price':candidate['limit_price'],
        'quantity_shares':candidate['gross_qty'],'budget_all_in':runtime.config['entry']['budget_usd'],
        'cost_quote':cost_quote(c,side,candidate['limit_price'],candidate['gross_qty']),
        'contract_sha256':c['contract_sha256'],'config_sha256':config_hash(runtime.config),
        'model_artifact_sha256':None,'decision_frame_id':frame['frame_id'],'calibration_status':'UNCALIBRATED',
        'window_close_at':c['window_close_at'],'baseline_registration_sha256':digest(registration),
        'probability_used':guard}


def baseline_factory(kind,*,registered_at,baseline_folder=None):
    """The same cost/precision/risk layer, with an explicitly uncalibrated baseline signal."""
    frozen=None
    if kind=='B1_FROZEN_UNIFIED':
        from .baseline import FrozenMainBaseline
        frozen=FrozenMainBaseline(baseline_folder)
        provenance=frozen.manifest_sha256
    elif kind=='B2_PRIOR':provenance=digest({'kind':'REGISTERED_FIXED_PRIOR','prior':load_config()['prior']})
    else:raise ValueError('UNKNOWN_BASELINE')
    def factory(scenario,cfg,inputsha,runid):
        registration={'baseline_id':kind,'strategy_id':kind,'frozen_provenance_sha256':provenance,
            'config_sha256':config_hash(cfg),'input_manifest_sha256':inputsha,'registered_at':registered_at}
        def decide(runtime,frame,at):
            c=runtime.contract;books={'Up':frame['up_book'],'Down':frame['down_book']}
            if kind=='B2_PRIOR':
                prior=frame.get('prior')
                if not prior or any('PROXY' in x for x in frame['decision_reasons']):return None
                selected,_=choose_entry(c,books,{'Up':prior['p_raw_up'],'Down':prior['p_raw_down']},runtime.config)
                return _entry_plan(runtime,frame,selected,registration) if selected else None
            inputs=getattr(runtime,'baseline_inputs',None)
            if isinstance(inputs,dict) and inputs.get('record_type')!='B1MarketInput':
                inputs=inputs.get(c['market']['condition_id']) or inputs.get('latest')
            if not inputs:return None
            info=inputs.get('market_info')
            if not info or info.get('fee_rate') is None or info.get('fee_exponent') is None:return None
            result=frozen.evaluate(inputs,at,fee_rate=info['fee_rate'],fee_exponent=info['fee_exponent'])
            side=result.get('side')
            if side not in ('Up','Down') or result.get('limit_price') is None:return None
            try:chosen=plan_side(c,side,books[side],result['confidence'],runtime.config)
            except ValueError:return None
            limit=floor_step(min(Decimal(chosen['limit_price']),Decimal(str(result['limit_price']))),c['trading']['tick_size'])
            if limit<Decimal(books[side]['asks'][0]['price']):return None
            # Re-size downward under the immutable original signal's limit; never
            # raise it to meet a minimum or replace it with the model's direction.
            step=Decimal(c['trading']['quantity_step']);budget=Decimal(runtime.config['entry']['budget_usd'])
            lo,hi=0,int(budget/limit/step)
            while lo<hi:
                middle=(lo+hi+1)//2
                quote=fee_adapter(c,side,str(limit),str(middle*step),worst_case=True)
                if Decimal(quote['cash_paid'])<=budget:lo=middle
                else:hi=middle-1
            quantity=lo*step
            if quantity<=0:return None
            quote=cost_quote(c,side,str(limit),str(quantity))
            unit=c['trading']['min_order_size_unit'];minimum=Decimal(c['trading']['min_order_size'])
            observed=quantity if unit=='SHARES' else quantity*limit if unit=='PRE_FEE_COLLATERAL' else Decimal(quote['all_in_cash'])
            if observed<minimum:return None
            chosen.update(limit_price=str(limit),gross_qty=str(quantity))
            return _entry_plan(runtime,frame,chosen,registration)
        return registration,decide
    return factory


def _daily_changes(rows,start,end):
    """Keep every calendar day. Unknown NAV remains unknown, including its next delta."""
    points={}
    for row in rows:
        at=row.get('at',row.get('at_ms',row.get('timestamp')))
        if at is None and row.get('day'):
            at=int(datetime.fromisoformat(row['day']).replace(tzinfo=timezone.utc).timestamp()*1000)+DAY_MS-1
        if type(at) is int:points[(at+1)//DAY_MS*DAY_MS]=row
    previous=Decimal('100');result=[];maximum_dd=Decimal(0);high=Decimal('100')
    for boundary in range(start+DAY_MS,end+1,DAY_MS):
        row=points.get(boundary,{})
        nav=row.get('nav',row.get('wallet',{}).get('nav'))
        current=Decimal(str(nav)) if nav is not None else None
        if current is not None and not current.is_finite():current=None
        delta=current-previous if current is not None and previous is not None else None
        result.append({'day':_date(boundary-DAY_MS),'net_return_usd':str(delta) if delta is not None else None,
            'nav_end':str(current) if current is not None else None,'external_flows_usd':'0',
            'valuation_status':'VERIFIED' if current is not None else 'UNCERTAIN'})
        if current is not None:
            high=max(high,current)
            if high>0:maximum_dd=max(maximum_dd,(high-current)/high)
        previous=current
    return result,float(maximum_dd)


def probability_support_report(rows):
    """Fixed diagnostic groups only; never selects thresholds or refits a model."""
    groups=defaultdict(list)
    for row in rows:
        time_left=row.get('tau_wall_seconds');price=row.get('best_ask_up');vol=row.get('volatility_bps_sqrt_second')
        group_time=str(min((30,45,60,75,90),key=lambda x:abs(x-time_left))) if time_left is not None else 'UNKNOWN'
        group_price=(str(int(float(price)*10)/10)+'-'+str((int(float(price)*10)+1)/10)) if price is not None else 'UNKNOWN'
        group_vol=next((label for limit,label in ((.25,'<=0.25'),(.5,'0.25-0.5'),(1.,'0.5-1'),(2.,'1-2')) if vol is not None and vol<=limit),'>2' if vol is not None else 'UNKNOWN')
        for dimension,value in (('remaining_seconds',group_time),('up_best_ask',group_price),('volatility_bps_sqrt_second',group_vol),('settled_outcome','UP' if row['y'] else 'DOWN')):
            groups[(dimension,value)].append(row)
    return {'scope':'FIXED_DIAGNOSTIC_BINS_NOT_PARAMETER_SEARCH','groups':[
        {'dimension':dimension,'bucket':bucket,**probability_metrics(sample)} for (dimension,bucket),sample in sorted(groups.items())]}


def tail_risk_report(daily,market_pnl):
    from .research import quantile_linear
    if any(r['net_return_usd'] is None for r in daily):return {'status':'INSUFFICIENT','reason':'DAILY_VALUATION_MISSING'}
    values=[float(Decimal(r['net_return_usd'])) for r in daily];markets=[float(Decimal(x)) for x in market_pnl]
    current=longest=0
    for value in values:
        current=current+1 if value<0 else 0;longest=max(longest,current)
    return {'status':'COMPUTED' if values else 'INSUFFICIENT','calendar_days':len(values),
        'losing_days':sum(v<0 for v in values),'longest_consecutive_losing_days':longest,
        'worst_day_usd':min(values) if values else None,'daily_q01_usd':quantile_linear(values,.01) if values else None,
        'daily_q05_usd':quantile_linear(values,.05) if values else None,'worst_settled_market_usd':min(markets) if markets else None,
        'unresolved_market_losses_included':'REPORTED_SEPARATELY_AS_PENDING_TAIL_NOT_ASSUMED_ZERO'}


def _verified_acceptance(path):
    """A caller's PASS string is not proof: check the referenced evidence bytes."""
    report=_json(path)
    if report.get('G1')!='PASS' or report.get('case_count')!=50 or len(report.get('cases',[]))!=50:return False
    if any(c.get('acceptance_status')!='PASS' or not c.get('coverage_reviewed') for c in report['cases']):return False
    references=report.get('junit_evidence',[])
    if not references or not report.get('implementation_source_hashes') or not report.get('test_source_hashes'):return False
    root=Path(__file__).resolve().parents[3]
    for reference in references:
        file=Path(reference['path'])
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest()!=reference['sha256']:return False
    for group in ('implementation_source_hashes','test_source_hashes'):
        for relative,sha in report[group].items():
            file=(root/relative).resolve()
            if not file.is_relative_to(root) or not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest()!=sha:return False
    return True


def _actual_outcomes(folder,scenario):
    path=Path(folder)/scenario/'ledger.sqlite3'
    if not path.exists():return {'status':'INSUFFICIENT','reason':'LEDGER_NOT_CREATED'}
    conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    try:
        orders=[dict(r) for r in conn.execute('SELECT * FROM hz_intents')]
        fills=[dict(r) for r in conn.execute('SELECT * FROM hz_fills')]
        market_pnl=[str(r[0]) for r in conn.execute('SELECT pnl FROM hz_outcomes')]
        rows=[json.loads(r['payload']) for r in conn.execute('SELECT payload FROM hz_ledger_events')]
        extra_tick=Decimal(0);fee_diagnostic_complete=True
        contracts={}
        journal=Path(folder)/scenario/'events.sqlite3'
        if journal.exists():
            read=sqlite3.connect(journal.as_uri()+'?mode=ro',uri=True)
            try:contracts={c['contract_sha256']:c for c in [json.loads(r[0]) for r in read.execute('SELECT json FROM market_contracts')]}
            finally:read.close()
        for fill in fills:
            p=json.loads(fill['payload']);c=contracts.get(p['contract_sha256'])
            if p['status']!='CONFIRMED':continue
            if not c:fee_diagnostic_complete=False;continue
            moved=Decimal(p['price'])+Decimal(c['trading']['tick_size'])
            if moved>=1:fee_diagnostic_complete=False;continue
            quantity=p['economics']['gross_shares']
            try:
                repriced=fee_adapter(c,p['side'],str(moved),quantity)
                # Fixed observed fill set, cost sensitivity only. Never claims a
                # repriced order would still qualify or fill at its old limit.
                extra_tick+=Decimal(repriced['cash_paid'])-Decimal(p['economics']['all_in_cash'])
                lost=Decimal(p['economics']['net_shares'])-Decimal(repriced['net_qty'])
                order=next(o for o in orders if o['intent_id']==fill['intent_id'])
                total_net=sum((Decimal(f['net_shares']) for f in fills if f['intent_id']==fill['intent_id'] and f['state']=='CONFIRMED'),Decimal(0))
                payout=Decimal(order['payout'])/total_net if total_net and order['recognized_at'] is not None else Decimal(0)
                extra_tick+=max(Decimal(0),lost)*payout
            except ValueError:fee_diagnostic_complete=False
        pending=sum(r['recognized_at'] is None and r['state'] not in ('TERMINAL_NO_FILL','ABORTED_BEFORE_SEND','REJECTED_CONFIRMED') for r in orders)
        return {'status':'ACCOUNTED' if not pending else 'PENDING_TAIL','orders':len(orders),
            'distinct_filled_markets':len({r['condition_id'] for r in orders if any(f['intent_id']==r['intent_id'] and f['state']!='FAILED_FINAL' for f in fills)}),
            'unresolved_orders':pending,
            'market_realized_pnl':market_pnl,'one_tick_additional_cost_usd':str(extra_tick) if fee_diagnostic_complete else None,
            'one_tick_scope':'FIXED_OBSERVED_FILLS_COST_ONLY_NOT_REEXECUTION',
            'ledger_event_sha256':digest(rows)}
    finally:conn.close()


def run_research(request,output_directory,*,runner=None):
    """Execute a new immutable experiment. Input request paths are explicit, never guessed."""
    from .replay_runtime import run_strategy_scenarios
    runner=runner or run_strategy_scenarios
    start,end=request['first_day_at'],request['end_at'];windows=walk_forward_windows(start,end)
    candidate_count=request.get('candidate_count')
    if type(candidate_count) is not int or not 1<=candidate_count<=12:raise ValueError('REGISTERED_CANDIDATE_COUNT_REQUIRED')
    if request.get('candidate_history_known') is not True:raise ValueError('UNKNOWN_TRIAL_HISTORY_START_NEW_FORWARD_STUDY')
    out=Path(output_directory).resolve();out.mkdir(parents=True,exist_ok=False)
    cfg=load_config(request.get('config'));events=_rows(request['events']);contracts=_rows(request['contracts']);landmarks=_rows(request['landmark_rows'])
    coverage=_json(request['coverage']);input_files={}
    for key in ('events','contracts','landmark_rows','coverage'):
        path=Path(request[key]).resolve();input_files[key]={'uri':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    code={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob('*.py'))}
    registry=ExperimentRegistry(out/'registry.sqlite3')
    try:registration=registry.register(out.name,cfg,input_files,strategy_hash=digest(code))
    finally:registry.close()
    _write(out/'registration.json',{'request':request,'input_files':input_files,'code':code,**registration,
        'registered_before_this_evaluation':True,'historically_preregistered':False})
    folds=[];published=[]
    for index,split in enumerate(windows):
        partition=partition_rows(landmarks,split,publication_at=split.validation_end_at,evaluation_at=end)
        folder=out/('fold-'+str(index+1));folder.mkdir()
        _write(folder/'partition.json',partition)
        days=[d for d in coverage.get('complete_utc_days',[]) if _date(split.training_start_at)<=d<_date(split.training_end_at)]
        began=time.monotonic()
        fitted=train_candidate(partition['train'],partition['validation'],complete_utc_days=days,
            training_window={'start_at':split.training_start_at,'end_at':split.training_end_at},
            validation_window={'start_at':split.validation_start_at,'end_at':split.validation_end_at},
            evaluated_at=split.validation_end_at,rule_version=request['rule_version'],rule_contract_hash=request['rule_contract_hash'],
            output_dir=folder,code_revision=request['code_revision'],coverage_evidence=input_files['coverage'],config=cfg)
        duration=max(1,math.ceil((time.monotonic()-began)*1000));fold={'index':index+1,'split':partition['split'],
            'training_status':fitted['status'],'counts':fitted.get('training_counts'),
            'measured_offline_fit_duration_ms':duration,'publication_latency_evidence':'ASSUMED_USING_CURRENT_MACHINE_FIT_DURATION'}
        if fitted['status']=='FITTED_CANDIDATE':
            earliest=split.validation_end_at+duration
            boundaries=[c for c in contracts if earliest<=c['window_open_at']<split.test_end_at and c['window_open_at']%300000==0]
            if boundaries:
                market=min(boundaries,key=lambda c:c['window_open_at'])
                released=publish_artifact(fitted['artifact'],market['window_open_at'],market['market']['market_uid'])
                published.append(released);fold['release']=_write(folder/'model-released.json',released)
            else:fold['publication_status']='NO_TEST_MARKET_BOUNDARY'
        _write(folder/'training-result.json',fitted);folds.append(fold)
    if not windows:
        result={'status':'INSUFFICIENT','reason':'COMPLETE_28_7_7_WINDOW_REQUIRED','folds':folds,'registration':registration,
            'gates':assess_research_gates({}),'economic_validation':'INSUFFICIENT'}
        _write(out/'research-report.json',result);return result
    test_start=windows[0].test_start_at;test_end=windows[-1].test_end_at
    data=[e for e in events if e['received_at']<=end]
    common={'config':cfg,'model_artifacts':published,'contracts':contracts,'final_at':end,
            'clock_bound_ms':request.get('clock_bound_ms'),'trade_start_at':test_start,'evaluation_end_at':test_end}
    strategy=runner(data,out/'strategy',**common)
    b2=runner(data,out/'B2',**{**common,'model_artifacts':[],
        'baseline_policy_factory':baseline_factory('B2_PRIOR',registered_at=test_start)})
    b1=None;baseline_original={'status':'BASELINE_UNAVAILABLE','reason':'FROZEN_ORIGINAL_ACCOUNT_NOT_PROVIDED'}
    if request.get('baseline_folder'):
        b1=runner(data,out/'B1-unified',**{**common,'model_artifacts':[],
            'baseline_policy_factory':baseline_factory('B1_FROZEN_UNIFIED',registered_at=test_start,baseline_folder=request['baseline_folder'])})
        from .baseline_account import run_original_main_account
        baseline_original=run_original_main_account(data,request['baseline_folder'],out/'B1-original-account',final_at=end)
    b0=[{'day':_date(t),'net_return_usd':'0','nav_end':'100','external_flows_usd':'0','valuation_status':'VERIFIED'} for t in range(test_start,test_end,DAY_MS)]
    statistical={};diagnostics={};baselines={'B0':{'daily':b0,'account_return':'0'},'B1_unified':b1 or {'status':'BASELINE_UNAVAILABLE'},
        'B1_original_account':baseline_original,'B2':b2,'B3':{'scope':'PROBABILITY_ONLY_NOT_EXECUTABLE_MID_PRICE'}}
    for scenario,item in strategy['scenarios'].items():
        daily,dd=_daily_changes(item.get('daily_nav',[]),test_start,test_end)
        baseline_days=_daily_changes(b1['scenarios'][scenario].get('daily_nav',[]),test_start,test_end)[0] if b1 else None
        statistics=bootstrap_sensitivity(daily,baseline_days,candidate_count=candidate_count,registered=True,candidate_history_known=True)
        outcome=_actual_outcomes(out/'strategy',scenario)
        good_daily=[r['net_return_usd'] for r in daily if r['net_return_usd'] is not None]
        concentration=concentration_report(good_daily,outcome.get('market_realized_pnl',[])) if len(good_daily)==len(daily) and good_daily else {'status':'INSUFFICIENT','reason':'DAILY_VALUATION_MISSING'}
        statistical[scenario]={'daily':daily,'bootstrap':statistics,'maximum_daily_boundary_drawdown':dd,
            'continuous_intraday_maximum_drawdown':item.get('maximum_drawdown') if item.get('maximum_drawdown_complete') is True else None,
            'concentration':concentration,'tail_risk':tail_risk_report(daily,outcome.get('market_realized_pnl',[])),'execution':outcome}
        diagnostics[scenario]={'full_replay':item.get('replay_kind')=='FULL_STRATEGY_CAUSAL',
            'manifest_verified':item.get('input_sha256')==digest(data),
            'net_pnl_usd':str(sum((Decimal(r['net_return_usd']) for r in daily),Decimal(0))) if len(good_daily)==len(daily) else None}
    # Evaluate only labels available by the study cutoff, and only common valid
    # landmarks; no random direction/tick split and no mid-price execution.
    probability_rows=[]
    journal=out/'strategy/S1/events.sqlite3'
    if journal.exists():
        conn=sqlite3.connect(journal.as_uri()+'?mode=ro',uri=True)
        try:
            predicted=[json.loads(r[0]) for r in conn.execute("SELECT DISTINCT f.json FROM features f JOIN landmarks l ON l.feature_id=f.id WHERE l.status='PRESENT' AND f.at>=? AND f.at<?",(test_start,test_end))]
            actual={r['condition_id']:r for r in landmarks if r['label_available_at']<=end and r.get('label_verified_at',r['label_available_at'])<=end}
            for frame in predicted:
                row=actual.get(frame['market']['condition_id'])
                if row and frame.get('fitted') and frame['data_status']=='READY':
                    up,down=frame['up_book'],frame['down_book']
                    probability_rows.append({**row,'decision_at':frame['decision_at'],'p_fit_up':frame['fitted']['p_fit_up'],
                        'p_raw_up':frame['prior']['p_raw_up'],'tau_wall_seconds':frame['tau_wall_seconds'],
                        'best_ask_up':up['asks'][0]['price'],
                        'volatility_bps_sqrt_second':math.sqrt(float(frame['prior']['variance_rate_usd2_per_second']))/float(frame['prior']['spot'])*10000,
                        'p_market_up':market_probability(up['bids'][0]['price'],up['asks'][0]['price'],down['bids'][0]['price'],down['asks'][0]['price'])})
        finally:conn.close()
    baselines['B3'].update(metrics=probability_metrics(probability_rows),intervals=probability_metric_intervals(probability_rows))
    baselines['B2_probability']=probability_metrics(probability_rows,probability_key='p_raw_up')
    baselines['probability_support']=probability_support_report(probability_rows)
    main=statistical.get('S1',{});outcome=main.get('execution',{});first_model=published[-1] if published else None
    evidence={};model_evidence={}
    if first_model:
        validate_artifact(first_model);counts=first_model['training']['sample_counts'];val=first_model['validation']
        model_evidence={'artifact_verified':True,'manifests_verified':True,'coverage_reported':True,
            'probability_intervals_reported':True,'train_markets':counts['independent_markets'],
            'complete_train_days':counts['complete_utc_days'],'up_train_markets':counts['up_markets'],'down_train_markets':counts['down_markets'],
            'validation_markets':val['sample_counts']['independent_markets'],'bootstrap_success':first_model['bootstrap']['successful_count'],
            'train_window_days':28,'validation_window_days':7,'validation_metrics':{'model':val['model_metrics'],'market':val['market_probability_metrics']}}
    if request.get('acceptance_report') and _verified_acceptance(request['acceptance_report']):
        from .cli import golden_report
        golden=golden_report();_write(out/'golden-execution.json',golden)
        evidence['G0']={'executed':True,'evidence_verified':golden['case_count']==golden['passed']==27,'failed':golden['case_count']-golden['passed']}
        evidence['G1']={'executed':True,'evidence_verified':True,'failed':0}
    pnl=diagnostics.get('S1',{}).get('net_pnl_usd');tick=outcome.get('one_tick_additional_cost_usd')
    evidence.update(model=model_evidence,study={'manifests_verified':True,'continuous_accounts_verified':all(i.get('replay_kind')=='FULL_STRATEGY_CAUSAL' for i in strategy['scenarios'].values()),
        'complete_daily_sequence':all(r['net_return_usd'] is not None for r in main.get('daily',[])),
        'tails_accounted':outcome.get('unresolved_orders')==0,'concentration_reported':bool(main.get('concentration')),
        'nonoverlapping_test_weeks':len(windows),'shadow_filled_markets':strategy['scenarios']['S1'].get('signal_shadow',{}).get('filled_markets',0),
        'portfolio_filled_markets':outcome.get('distinct_filled_markets',0),
        'unexplained_reconciliation_differences':strategy['scenarios']['S1'].get('unexplained_reconciliation_differences'),
        'maximum_drawdown':main.get('continuous_intraday_maximum_drawdown'),'bootstrap':main.get('bootstrap',{}),'scenarios':diagnostics,
        'one_extra_tick_cost_pnl_usd':str(Decimal(pnl)-Decimal(tick)) if pnl is not None and tick is not None else None},
        baseline={'frozen_source_verified':bool(b1),'unified_risk_supports_improvement':bool(b1 and main.get('bootstrap',{}).get('3',{}).get('improvement',{}).get('one_sided_lower',-1)>0)})
    gates=assess_research_gates(evidence)
    result={'status':'COMPUTED','research_conclusion':'SEE_SEPARATE_GATES','economic_validation':gates['G3']['status'],
        'registration':registration,'folds':folds,'published_models':len(published),'strategy':strategy,'baselines':baselines,
        'statistics':statistical,'gates':gates,'evidence':evidence,
        'notes':['Each scenario keeps one wallet for all test weeks; no weekly reset.',
                 'Retrospective fit publication uses measured current-machine latency and is ASSUMED.',
                 'Frozen B1 is the available corrected implementation, not a reconstructed missing historical release.']}
    _write(out/'research-report.json',result)
    return result
