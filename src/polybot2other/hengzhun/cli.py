"""Offline operations. Never imports the old live executor or reads credentials."""
from __future__ import annotations

import argparse
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import zlib

import yaml

from . import model
from .config import load_config, config_hash
from .contracts import SPEC_DIR, canonical, digest, validate_record, require_verified
from .calibration import train_candidate, market_probability
from .pricing import fee_adapter
from .replay import run_scenarios
from .research import assess_research_gates


def read_json(path):
    return json.loads(Path(path).read_text(), parse_constant=lambda x: (_ for _ in ()).throw(ValueError('NONFINITE_JSON')))


def read_rows(path):
    path = Path(path)
    if path.suffix == '.jsonl':
        return [json.loads(line, parse_constant=lambda x: (_ for _ in ()).throw(ValueError('NONFINITE_JSON')))
                for line in path.read_text().splitlines() if line.strip()]
    rows = read_json(path)
    if not isinstance(rows, list):
        raise ValueError('JSON_ARRAY_OR_JSONL_REQUIRED')
    return rows


def write_json(path, value):
    path = Path(path)
    with path.open('x') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def config_from(path=None):
    return load_config(yaml.safe_load(Path(path).read_text()) if path else None)


def golden_report():
    """Evaluate the original fixture inputs against this implementation, not its reference copy."""
    fixture = read_json(SPEC_DIR/'golden_cases.json')
    names = ('twap_prior','forecast_from_timestamps','source_timing','previous_tick_integral',
             'effective_cost_cash','conservative_guards','cost_cap_from_guard','maximum_price_for_cash_cost',
             'floor_to_tick','value_gates','budget_to_shares','equal_budget_pair_math','settlement_label','reference_trade_example')
    operations = {name: getattr(model, name) for name in names}
    operations['realized_variance'] = model.realized_variance_from_log_returns
    def equal(actual, expected, tolerance):
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(k in actual and equal(actual[k], v, tolerance) for k,v in expected.items())
        if isinstance(expected, bool): return actual is expected
        if isinstance(actual, Decimal) and isinstance(expected, str): return actual == Decimal(expected)
        if isinstance(expected, (int,float)):
            return math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=tolerance)
        return actual == expected
    results = []
    for case in fixture['cases']:
        try:
            actual = operations[case['operation']](**case['inputs'])
            passed = not case.get('expected_error') and equal(actual,case['expected'],case.get('abs_tol',1e-9))
        except ValueError:
            passed = bool(case.get('expected_error'))
        results.append({'id':case['id'], 'status':'PASS' if passed else 'FAIL'})
    return {'scope':'SYNTHETIC_MATH_ONLY_NOT_TRAINED', 'fixture_sha256':hashlib.sha256((SPEC_DIR/'golden_cases.json').read_bytes()).hexdigest(),
            'case_count':len(results), 'passed':sum(r['status']=='PASS' for r in results), 'cases':results}


def export_dataset(database, destination, *, as_of_ms):
    """Consistent read-only snapshot; first receive clocks and raw byte hashes are preserved."""
    if type(as_of_ms) is not int or as_of_ms < 0: raise ValueError('AS_OF_TIME_REQUIRED')
    source = Path(database).resolve()
    if not source.is_file(): raise ValueError('EVENT_DATABASE_NOT_FOUND')
    out = Path(destination); out.mkdir(parents=True, exist_ok=False)
    snapshot = sqlite3.connect(':memory:'); snapshot.row_factory = sqlite3.Row
    reader = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)
    try: reader.backup(snapshot)
    finally: reader.close()
    files = {}; counts = {}; exported = {}
    queries = {
        'events':('SELECT json FROM raw_events WHERE received_at<=? ORDER BY seq',(as_of_ms,)),
        'features':('SELECT json FROM features WHERE at<=? ORDER BY at,id',(as_of_ms,)),
        'contracts':('SELECT json FROM market_contracts ORDER BY hash',()),
        'results':('SELECT json FROM official_results WHERE available_at<=? ORDER BY available_at,id',(as_of_ms,)),
        'landmarks':('SELECT * FROM landmarks WHERE at<=? ORDER BY at,condition_id,target',(as_of_ms,)),
        'quality':('SELECT * FROM quality_events WHERE at<=? ORDER BY id',(as_of_ms,)),
    }
    try:
        for name,(sql,params) in queries.items():
            rows = [json.loads(r['json']) if 'json' in r.keys() else dict(r) for r in snapshot.execute(sql,params)]
            if name=='contracts': rows=[r for r in rows if r['captured_at']<=as_of_ms]
            exported[name]=rows; counts[name]=len(rows)
            path=out/(name+'.jsonl')
            with path.open('x') as handle:
                for row in rows: handle.write(canonical(row)+'\n')
            files[name]={'path':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'rows':len(rows)}
        archives=out/'raw'; archives.mkdir()
        hashes=sorted({r['payload_sha256'] for r in exported['events']})
        for sha in hashes:
            row=snapshot.execute('SELECT compressed FROM raw_archives WHERE hash=?',(sha,)).fetchone()
            if row is None: raise ValueError('RAW_ARCHIVE_MISSING')
            raw=zlib.decompress(row[0])
            if hashlib.sha256(raw).hexdigest()!=sha: raise ValueError('RAW_ARCHIVE_DIGEST_MISMATCH')
            (archives/(sha+'.bin')).write_bytes(raw)
        contracts={r['contract_sha256']:r for r in exported['contracts']}
        frames={r['frame_id']:r for r in exported['features']}; latest={}
        for row in exported['results']:
            if max(row['received_at'],row['verified_at'] or 0,row['label_available_at'] or 0)>as_of_ms: continue
            key=row['market']['condition_id']; old=latest.get(key)
            if old is None or row['revision']>old['revision']:latest[key]=row
            elif old['revision']==row['revision'] and old!=row:raise ValueError('RESULT_REVISION_CONFLICT')
        training=[]; excluded=[]; seen=set()
        for landmark in exported['landmarks']:
            frame=frames.get(landmark['feature_id']); reason=None
            contract=contracts.get(frame['contract_sha256']) if frame else None
            result=latest.get(landmark['condition_id'])
            if landmark['status']!='PRESENT' or not frame:reason='LANDMARK_MISSING'
            elif frame['frame_id'] in seen:continue
            elif frame['data_status']!='READY' or not frame['prior']:reason='FEATURE_NOT_READY'
            elif not contract or contract['verification_status']!='VERIFIED':reason='CONTRACT_UNVERIFIED'
            elif not result or result['official_final_verified'] is not True or type(result['classification_label_up']) is not int or result['classification_label_up'] not in (0,1):reason='VERIFIED_BINARY_LABEL_UNAVAILABLE'
            elif result['contract_sha256']!=frame['contract_sha256']:reason='RESULT_CONTRACT_MISMATCH'
            if reason:
                excluded.append({'condition_id':landmark['condition_id'],'target':landmark['target'],'reason':reason});continue
            seen.add(frame['frame_id'])
            up,down=frame['up_book'],frame['down_book']
            training.append({'condition_id':landmark['condition_id'],'rule_version':contract['rules']['rule_version'],
                'window_open_at':contract['window_open_at'],'decision_at':frame['decision_at'],
                'label_available_at':result['label_available_at'],'label_verified_at':result['verified_at'],
                'y':result['classification_label_up'],'p_raw_up':frame['prior']['p_raw_up'],
                'p_market_up':market_probability(up['bids'][0]['price'],up['asks'][0]['price'],down['bids'][0]['price'],down['asks'][0]['price']),
                'tau_wall_seconds':frame['tau_wall_seconds'],'feature_frame_id':frame['frame_id'],
                'landmark_target_seconds':landmark['target'],
                'result_id':result['result_id'],'contract_sha256':frame['contract_sha256']})
        files['training_rows']=write_json(out/'training-rows.json',training)
        files['excluded_landmarks']=write_json(out/'excluded-landmarks.json',excluded)
        manifest={'schema_version':'1.0.0','kind':'HENGZHUN_DATASET_EXPORT','source_database':str(source),
            'as_of_ms':as_of_ms,'files':files,'counts':counts,'raw_archives':hashes,'raw_archives_verified':True,
            'training_rows':len(training),'independent_labeled_markets':len({r['condition_id'] for r in training}),
            'complete_utc_days_status':'NOT_ESTABLISHED','economic_validation':'INSUFFICIENT',
            'note':'An export is not a proof of complete UTC-day coverage. Revisions retain actual availability.'}
        manifest['manifest_sha256']=digest(manifest)
        write_json(out/'manifest.json',manifest)
        return manifest
    finally:snapshot.close()


def freeze_baseline(source_root, destination, settings):
    """Freeze the available current B1 code; never claim it is a missing historical version."""
    def secret_check(value):
        if isinstance(value,dict):
            for key,child in value.items():
                if re.search(r'password|secret|private|credential|authorization|api.?key|access.?token',key,re.I):
                    raise ValueError('BASELINE_SETTINGS_MUST_NOT_CONTAIN_CREDENTIALS')
                secret_check(child)
        elif isinstance(value,list):
            for child in value:secret_check(child)
    secret_check(settings)
    root=Path(source_root).resolve(); out=Path(destination); out.mkdir(parents=True,exist_ok=False)
    paths=['pyproject.toml']+[str(p.relative_to(root)) for p in sorted((root/'src/polybot2other').glob('*.py'))]
    files=[]
    for relative in paths:
        source=root/relative
        if not source.is_file():raise ValueError('BASELINE_SOURCE_MISSING')
        raw=source.read_bytes();target=out/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
        files.append({'path':relative,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)})
    config_file=write_json(out/'baseline-settings.json',settings)
    def git_value(arguments):
        result=subprocess.run(['git','-C',str(root),*arguments],capture_output=True,text=True,check=False)
        return result.stdout.strip() if result.returncode==0 else None
    git_status=git_value(['status','--porcelain'])
    manifest={'baseline':'B1','source_scope':'CURRENT_AVAILABLE_CODE_SNAPSHOT',
        'display_label':'B1_CURRENT_CORRECTED_FROZEN','git_commit':git_value(['rev-parse','HEAD']),
        'git_worktree_dirty':None if git_status is None else bool(git_status),
        'source_of_truth':'EXACT_FILE_BYTES_AND_CONFIG_HASHES_NOT_GIT_COMMIT_ALONE',
        'historical_original_version_verified':False,'files':files,'settings':config_file,
        'code_config_frozen':True,'continuous_baseline_results':'NOT_RUN',
        'G4':'BASELINE_COMPARISON_NOT_ESTABLISHED'}
    manifest['manifest_sha256']=digest(manifest);write_json(out/'manifest.json',manifest)
    return manifest


def execution_replay(events, intents, destination, config, final_at):
    """Replay explicitly frozen decisions only; never label this full strategy research."""
    events=list(events); intents=deepcopy(list(intents))
    for event in events:validate_record(event,definition='RawEvent')
    for item in intents:
        require_verified(item['contract'])
        if type(item['submitted_at']) is not int:raise ValueError('SUBMIT_TIME_REQUIRED')
        if item['plan']['contract_sha256']!=item['contract']['contract_sha256']:raise ValueError('CONTRACT_HASH_MISMATCH')
        if type(item['clock_bound_ms']) not in (int,float) or not math.isfinite(item['clock_bound_ms']) or item['clock_bound_ms']<0:raise ValueError('CLOCK_BOUND_REQUIRED')
        # Freeze decisions on an actual input clock; do not silently move them to a later tick.
        if not any(e['event_id']==item['decision_event_id'] and e['received_at']==item['submitted_at'] for e in events):
            raise ValueError('DECISION_EVENT_CLOCK_MISSING')
    def factory(scenario):
        pending=deepcopy(intents)
        def decide(event,venue):
            for item in list(pending):
                if event['event_id']!=item['decision_event_id']:continue
                venue.ledger.ensure_wallet(item['plan']['wallet_id'],event['received_at'])
                intent=venue.ledger.reserve(item['plan'],event['received_at'])
                venue.submit(intent['intent_id'],item['contract'],event['received_at'],clock_bound_ms=item['clock_bound_ms'])
                pending.remove(item)
        return decide
    report=run_scenarios(events,destination,fee_adapter,config,decision_factory=factory,final_at=final_at)
    report.update(replay_scope='FROZEN_INTENT_EXECUTION_ONLY',full_strategy_replay=False,
                  intent_manifest_sha256=digest(intents),economic_validation='INSUFFICIENT')
    write_json(Path(destination)/'execution-only-scope.json',report)
    return report


def parser():
    p=argparse.ArgumentParser(description='衡准 V1 离线工具；不会签名、实盘下单或改动既有账户。')
    sub=p.add_subparsers(dest='command',required=True)
    v=sub.add_parser('validate-config');v.add_argument('--config',type=Path)
    g=sub.add_parser('golden');g.add_argument('--output',type=Path)
    x=sub.add_parser('export');x.add_argument('--database',type=Path,required=True);x.add_argument('--output',type=Path,required=True);x.add_argument('--as-of-ms',type=int,required=True)
    t=sub.add_parser('train');t.add_argument('--request',type=Path,required=True);t.add_argument('--config',type=Path);t.add_argument('--output',type=Path,required=True)
    b=sub.add_parser('freeze-baseline');b.add_argument('--source-root',type=Path,required=True);b.add_argument('--settings',type=Path,required=True);b.add_argument('--output',type=Path,required=True)
    r=sub.add_parser('execution-replay');r.add_argument('--events',type=Path,required=True);r.add_argument('--intents',type=Path,required=True);r.add_argument('--config',type=Path);r.add_argument('--output',type=Path,required=True);r.add_argument('--final-at-ms',type=int)
    r=sub.add_parser('replay');r.add_argument('--events',type=Path,required=True);r.add_argument('--contracts',type=Path,required=True);r.add_argument('--model',type=Path,required=True);r.add_argument('--config',type=Path);r.add_argument('--output',type=Path,required=True);r.add_argument('--final-at-ms',type=int);r.add_argument('--clock-bound-ms',type=float,required=True)
    r=sub.add_parser('report');r.add_argument('--evidence',type=Path,required=True);r.add_argument('--output',type=Path,required=True)
    r=sub.add_parser('research-run');r.add_argument('--request',type=Path,required=True);r.add_argument('--output',type=Path,required=True)
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        if args.command=='validate-config':
            c=config_from(args.config);validate_record(c,'config.schema.json')
            result={'status':'VALID','config_sha256':config_hash(c),'mode':c['runtime']['mode'],'live_execution_enabled':False,'parameter_count':sum(len(v) for v in c.values())-1}
        elif args.command=='golden':
            result=golden_report()
            if args.output:write_json(args.output,result)
        elif args.command=='export':result=export_dataset(args.database,args.output,as_of_ms=args.as_of_ms)
        elif args.command=='freeze-baseline':result=freeze_baseline(args.source_root,args.output,read_json(args.settings))
        elif args.command=='train':
            request=read_json(args.request);args.output.mkdir(parents=True,exist_ok=False)
            result=train_candidate(**request,output_dir=args.output,config=config_from(args.config))
            write_json(args.output/'training-result.json',result)
            result={k:v for k,v in result.items() if k!='artifact'}
        elif args.command=='execution-replay':result=execution_replay(read_rows(args.events),read_rows(args.intents),args.output,config_from(args.config),args.final_at_ms)
        elif args.command=='replay':
            from .replay_runtime import run_strategy_scenarios
            result=run_strategy_scenarios(read_rows(args.events),args.output,config=config_from(args.config),
                model_artifact=read_json(args.model),contracts=read_rows(args.contracts),final_at=args.final_at_ms,clock_bound_ms=args.clock_bound_ms)
        elif args.command=='research-run':
            from .research_pipeline import run_research
            result=run_research(read_json(args.request),args.output)
        else:
            evidence=read_json(args.evidence)
            result={'gates':assess_research_gates(evidence),'input_evidence_sha256':digest(evidence),
                    'provenance_scope':'CALLER_SUPPLIED_EVIDENCE_NOT_INDEPENDENTLY_VERIFIED'}
            write_json(args.output,result)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False))
        return 1 if result.get('passed',result.get('case_count'))!=result.get('case_count') else 0
    except (ValueError,KeyError,TypeError,OSError,sqlite3.Error) as exc:
        print(json.dumps({'status':'ERROR','reason':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2
