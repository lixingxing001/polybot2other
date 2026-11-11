"""Map original 50 cases to actual pytest JUnit evidence; never infer execution from source alone."""
import argparse,ast,hashlib,json,re,xml.etree.ElementTree as E
from pathlib import Path

# Additional integration evidence whose descriptive test names predate the EX
# mapping. These are actual collected nodes, not assumed results.
INTEGRATION_NODES={
    'EX-002':['tests/test_hengzhun_runtime.py::RuntimeTests::test_record_records_raw_and_feature_but_no_economic_events'],
    'EX-004':['tests/test_hengzhun_runtime.py::RuntimeTests::test_record_records_raw_and_feature_but_no_economic_events'],
    'EX-006':['tests/test_hengzhun_replay_runtime.py::ReplayRuntimeTests::test_full_strategy_future_tails_do_not_rewrite_past_actions_or_ledger'],
    'EX-010':['tests/test_hengzhun_runtime.py::RuntimeTests::test_repeat_source_receipt_cannot_refresh_stale_price'],
    'EX-012':['tests/test_hengzhun_runtime.py::RuntimeTests::test_presend_contract_change_aborts_original_plan'],
    'EX-022':['tests/test_hengzhun_execution.py::HengzhunExecutionTests::test_ex027_ex028_duplicate_and_out_of_order_callbacks_do_not_regress',
              'tests/test_hengzhun_acceptance_extra.py::AcceptanceExtraTests::test_ex027_committed_fill_survives_real_connection_restart_and_duplicate_delivery'],
    'EX-035':['tests/test_hengzhun_runtime.py::RuntimeTests::test_normalized_book_reaches_venue_and_confirmation_uses_match_clock'],
    'EX-038':['tests/test_hengzhun_runtime.py::RuntimeTests::test_verified_result_and_claim_continue_without_current_market'],
    'EX-050':['tests/test_hengzhun_replay_runtime.py::ReplayRuntimeTests::test_cold_paper_replay_all_scenarios_independent_and_unfitted',
              'tests/test_hengzhun_replay_runtime.py::ReplayRuntimeTests::test_full_strategy_future_tails_do_not_rewrite_past_actions_or_ledger'],
}

def build(junit_paths, reviewed=()):
    root=Path.cwd();meta=json.loads((root/'reports/hengzhun-current.json').read_text());out=Path(meta['report'])
    matrix=json.loads((out/'supplied-package/Hengzhun_V1/acceptance_matrix.json').read_text())
    mapping={row['id']:[] for row in matrix['cases']};sources={}
    for path in sorted((root/'tests').glob('test_hengzhun*.py')):
        tree=ast.parse(path.read_text());rel=str(path.relative_to(root));sources[rel]=hashlib.sha256(path.read_bytes()).hexdigest()
        methods={}
        for owner in tree.body:
            candidates=owner.body if isinstance(owner,ast.ClassDef) else [owner]
            for node in candidates:
                if isinstance(node,ast.FunctionDef) and node.name.startswith('test_'):
                    classname=owner.name if isinstance(owner,ast.ClassDef) else ''
                    nodeid=rel+'::'+(classname+'::' if classname else '')+node.name
                    methods.setdefault(node.name,[]).append(nodeid)
                    for number in re.findall(r'ex(\d{3})',node.name):
                        key='EX-'+number
                        if key in mapping:mapping[key].append(nodeid)
        for node in tree.body:
            if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='ACCEPTANCE_CASES' for t in node.targets):
                extra=ast.literal_eval(node.value)
                for key,names in extra.items():
                    if key not in mapping:raise ValueError('unknown acceptance ID: '+key)
                    if isinstance(names,str):names=[names]
                    for name in names:
                        if name.startswith('tests/'):mapping[key].append(name)
                        elif '::' in name:mapping[key].append(rel+'::'+name)
                        else:mapping[key].extend(methods.get(name,[]))
    for key,nodes in INTEGRATION_NODES.items():mapping[key].extend(nodes)
    outcomes={};junit=[]
    for path in junit_paths:
        path=Path(path)
        junit.append({'path':str(path.resolve()),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        for case in E.parse(path).getroot().iter('testcase'):
            cls=case.get('classname','').split('.');module=next((x for x in cls if x.startswith('test_hengzhun')),None)
            if not module:continue
            position=cls.index(module);rest=cls[position+1:]
            nodeid='tests/'+module+'.py::'+('::'.join(rest)+'::' if rest else '')+case.get('name','')
            status='FAIL' if case.find('failure') is not None or case.find('error') is not None else 'SKIPPED' if case.find('skipped') is not None else 'PASS'
            detail=next((n.text for n in list(case) if n.tag in ('failure','error','skipped')),None)
            outcomes[nodeid]={'status':status,'duration_seconds':float(case.get('time',0)),'detail':detail,'junit':str(path)}
    cases=[]
    for original in matrix['cases']:
        ids=sorted(set(mapping[original['id']]));states=[outcomes.get(node,{'status':'NOT_RUN'})['status'] for node in ids]
        status='FAIL' if 'FAIL' in states else 'NOT_RUN' if not ids or any(s in ('NOT_RUN','SKIPPED') for s in states) else 'PASS'
        accepted=status if status!='PASS' or original['id'] in reviewed else 'REVIEW_REQUIRED'
        cases.append({'id':original['id'],'title':original['title'],'test_status':status,'acceptance_status':accepted,
                      'coverage_reviewed':original['id'] in reviewed,
                      'tests':[{'nodeid':node,**outcomes.get(node,{'status':'NOT_RUN'})} for node in ids],
                      'original_required_evidence':original['required_evidence']})
    report={'source_matrix_sha256':hashlib.sha256((out/'supplied-package/Hengzhun_V1/acceptance_matrix.json').read_bytes()).hexdigest(),
            'case_count':len(cases),'junit_evidence':junit,'test_source_hashes':sources,'cases':cases,
            'implementation_source_hashes':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root/'src/polybot2other/hengzhun').glob('*.py'))},
            'summary':{k:sum(c['acceptance_status']==k for c in cases) for k in ('PASS','FAIL','NOT_RUN','REVIEW_REQUIRED')},
            'G1':'PASS' if all(c['acceptance_status']=='PASS' for c in cases) else 'NOT_ESTABLISHED',
            'scope':'Synthetic engineering cases only; no model-profit, forward profitability or real-execution claim.'}
    (out/'acceptance-execution-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    rows=['# 衡准 V1 系统验收实际执行索引','',f"G1: {report['G1']}。测试通过与完整场景覆盖审查分开，不以测试总数替代原始50场景。",'',
          '| 用例 | 题目 | 测试执行 | 完整场景审查 | 实际节点 |','|---|---|---|---|---|']
    rows += [f"| {c['id']} | {c['title']} | {c['test_status']} | {c['acceptance_status']} | "+'<br>'.join('`'+t['nodeid']+'`' for t in c['tests'])+' |' for c in cases]
    (root/'doc/hengzhun-v1-acceptance-execution.md').write_text('\n'.join(rows)+'\n')
    print(json.dumps(report['summary'],ensure_ascii=False));print('G1',report['G1'])
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--junit',action='append',required=True);parser.add_argument('--reviewed-cases')
    args=parser.parse_args();reviewed=json.loads(Path(args.reviewed_cases).read_text()) if args.reviewed_cases else []
    build(args.junit,reviewed)
