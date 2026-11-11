"""逐事件确定性重放；三个情景独立完整运行，不拼接利润。"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from .execution import PaperVenue, SCENARIOS
from .ledger import HengzhunLedger, LedgerError, canonical, digest


class ReplayRunner:
    def __init__(self, venue, decision_callback=None):
        self.venue=venue
        self.decision_callback=decision_callback

    def run(self, events, *, final_at=None):
        events=deepcopy(list(events))
        # Equal-clock order is its persisted capture order, not lexical boot IDs.
        # A dataset must supply that order; per-boot ingest regressions are rejected.
        last_received=-1;last_seq={}
        for event in events:
            received=event['received_at'];boot=event['boot_id'];seq=event['ingest_seq']
            if received<last_received or seq<=last_seq.get(boot,-1):raise LedgerError('REPLAY_ORDER_UNDETERMINED')
            self.venue.on_event(event)
            if self.decision_callback:self.decision_callback(event,self.venue)
            last_received=received;last_seq[boot]=seq
        if final_at is not None:self.venue.advance(final_at)
        result={'schema_version':'1.0.0','data_sha256':digest(events),'scenario_id':self.venue.scenario_id,
                'economic_space':self.venue.ledger.namespace,'execution_evidence':'ASSUMED','events_replayed':len(events),
                'wallets':self.venue.snapshot()['wallets'],'ledger_event_sha256':digest(self.venue.ledger.events()),
                'intents':[o['payload'] for o in self.venue.ledger.intents()]}
        result['result_sha256']=digest(result)
        return result


def run_scenarios(events, output_directory, fee_adapter, config, decision_factory=None, final_at=None):
    root=Path(output_directory);root.mkdir(parents=True,exist_ok=True);results={}
    data=list(events)
    for scenario in SCENARIOS:
        destination=root/scenario
        destination.mkdir(exist_ok=False)  # Never reuse or reset a prior research ledger.
        ns={'mode':'REPLAY','portfolio_run_id':'hengzhun-replay','scenario_id':scenario,'ledger_scope':'PAPER_PORTFOLIO'}
        ledger=HengzhunLedger(destination/'ledger.sqlite3',ns,config);ledger.ensure_wallet(at_ms=0)
        try:
            venue=PaperVenue(ledger,fee_adapter,scenario)
            callback=decision_factory(scenario) if decision_factory else None
            result=ReplayRunner(venue,callback).run(data,final_at=final_at)
            (destination/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
            results[scenario]=result
        finally:ledger.close()
    report={'execution_evidence':'ASSUMED','all_registered_scenarios':list(SCENARIOS),'scenarios':results,
            'economic_validation':'INSUFFICIENT','note':'工程重放结果，不构成已验证实盘收益；各情景独立，不合并净值。'}
    (root/'scenarios.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report


def main():
    parser=argparse.ArgumentParser(description='衡准 V1 只读事件结构检查；策略重放须传入已核验费用适配器与冻结模型')
    parser.add_argument('events',type=Path)
    args=parser.parse_args()
    rows=[json.loads(line) for line in args.events.read_text().splitlines() if line.strip()]
    print(json.dumps({'events':len(rows),'data_sha256':digest(rows),'execution':'NOT_RUN',
                      'reason':'使用 ReplayRunner 与已核验 FeeAdapter 和冻结决策回调运行经济回放'},ensure_ascii=False))


if __name__=='__main__':main()
