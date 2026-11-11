"""Offline entrypoint evidence; all files are temporary and no network adapter is loaded."""
from contextlib import redirect_stdout,redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from polybot2other.hengzhun.cli import main,golden_report,export_dataset,freeze_baseline,read_rows
from polybot2other.hengzhun.data import EventJournal


class HengzhunCliTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def invoke(self,args):
        out,err=io.StringIO(),io.StringIO()
        with redirect_stdout(out),redirect_stderr(err):code=main(args)
        return code,json.loads(out.getvalue() or err.getvalue())

    def test_supplied_golden_command_executes_all_actual_model_functions(self):
        report=golden_report();self.assertEqual(report['case_count'],27);self.assertEqual(report['passed'],27)
        path=self.root/'golden.json';code,out=self.invoke(['golden','--output',str(path)])
        self.assertEqual(code,0);self.assertEqual(json.loads(path.read_text()),out)
        code,out=self.invoke(['golden','--output',str(path)]);self.assertEqual(code,2)

    def test_live_config_rejected_and_default_contract_count_verified(self):
        code,out=self.invoke(['validate-config']);self.assertEqual(code,0);self.assertEqual(out['parameter_count'],94)
        path=self.root/'live.yaml';path.write_text('runtime:\n  mode: LIVE\n')
        code,out=self.invoke(['validate-config','--config',str(path)]);self.assertEqual(code,2)

    def test_export_snapshot_preserves_raw_bytes_and_actual_first_receive_time(self):
        path=self.root/'events.sqlite3';journal=EventJournal(path);self.addCleanup(journal.conn.close)
        raw=' { "value" : 101 } '
        first=journal.append(source_kind='OFFICIAL_SPOT',source_id='unit',stream_id='BTC',payload={'value':101},
            received_at=1001,source_at=1000,raw_text=raw)
        journal.append(source_kind='OFFICIAL_SPOT',source_id='unit',stream_id='BTC',payload={'value':102},received_at=2001,source_at=2000)
        destination=self.root/'export'
        report=export_dataset(path,destination,as_of_ms=1500)
        rows=read_rows(destination/'events.jsonl')
        self.assertEqual(rows,[first]);self.assertEqual(report['counts']['events'],1)
        self.assertEqual((destination/'raw'/(hashlib.sha256(raw.encode()).hexdigest()+'.bin')).read_bytes(),raw.encode())
        self.assertEqual(journal.counts()['raw_events'],2)
        self.assertEqual(report['complete_utc_days_status'],'NOT_ESTABLISHED')
        self.assertEqual(report['training_rows'],0)
        with self.assertRaises(FileExistsError):export_dataset(path,destination,as_of_ms=2000)

    def test_export_fails_on_corrupt_raw_archive_instead_of_claiming_integrity(self):
        path=self.root/'events.sqlite3';journal=EventJournal(path);self.addCleanup(journal.conn.close)
        event=journal.append(source_kind='OFFICIAL_SPOT',source_id='unit',stream_id='BTC',payload={'value':101},received_at=1,source_at=1)
        with journal.conn:journal.conn.execute('DELETE FROM raw_archives WHERE hash=?',(event['payload_sha256'],))
        with self.assertRaisesRegex(ValueError,'RAW_ARCHIVE_MISSING'):export_dataset(path,self.root/'export',as_of_ms=2)

    def test_unfitted_train_report_remains_insufficient_no_coefficients(self):
        day=86400000;request={'training_rows':[],'validation_rows':[],'complete_utc_days':[],
            'training_window':{'start_at':0,'end_at':28*day},'validation_window':{'start_at':28*day,'end_at':35*day},
            'evaluated_at':35*day,'rule_version':'fixture','rule_contract_hash':'a'*64,
            'code_revision':'unit','coverage_evidence':{}}
        path=self.root/'request.json';path.write_text(json.dumps(request));out=self.root/'model'
        code,result=self.invoke(['train','--request',str(path),'--output',str(out)])
        self.assertEqual(code,0);self.assertEqual(result['status'],'INSUFFICIENT')
        artifact=json.loads((out/'training-result.json').read_text())['artifact']
        self.assertEqual(artifact['artifact_status'],'UNFITTED');self.assertNotIn('fit',artifact)

    def test_baseline_freeze_is_exact_immutable_and_rejects_credentials(self):
        source=self.root/'source';(source/'src/polybot2other').mkdir(parents=True)
        (source/'pyproject.toml').write_text('[project]\nname="test"\n')
        (source/'src/polybot2other/strategy.py').write_text('threshold = 1\n')
        out=self.root/'baseline';result=freeze_baseline(source,out,{'stake_usd':'5','max_positions':5})
        self.assertEqual((out/'src/polybot2other/strategy.py').read_bytes(),(source/'src/polybot2other/strategy.py').read_bytes())
        self.assertFalse(result['historical_original_version_verified'])
        self.assertEqual(result['continuous_baseline_results'],'NOT_RUN')
        with self.assertRaises(FileExistsError):freeze_baseline(source,out,{})
        with self.assertRaisesRegex(ValueError,'CREDENTIALS'):freeze_baseline(source,self.root/'bad',{'api_key':'not-a-real-key'})
        self.assertFalse((self.root/'bad').exists())

    def test_missing_research_evidence_is_insufficient_not_success(self):
        evidence=self.root/'evidence.json';evidence.write_text('{}')
        code,result=self.invoke(['report','--evidence',str(evidence),'--output',str(self.root/'report.json')])
        self.assertEqual(code,0)
        self.assertTrue(all(result['gates'][g]['status']=='INSUFFICIENT' for g in ('G0','G1','G2','G3','G4')))
        self.assertEqual(result['gates']['execution_realism']['status'],'ASSUMED')


if __name__=='__main__':unittest.main()
