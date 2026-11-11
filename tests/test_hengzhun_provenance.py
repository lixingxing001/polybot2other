"""Incremental raw-input evidence: causal, bounded, atomic and restart-safe."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib

from polybot2other.hengzhun.contracts import canonical,digest,validate_record
from polybot2other.hengzhun.data import EventJournal


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'events.sqlite3';self.journal=EventJournal(self.path)
        self.addCleanup(lambda:self.journal.conn.close())

    @staticmethod
    def append(journal,at,source=None,value=100):
        return journal.append(source_kind='OFFICIAL_SPOT',source_id='test-feed',stream_id='spot',
            payload={'value':value,'timestamp':at if source is None else source},received_at=at,
            source_at=at if source is None else source)

    def chain(self,journal,head):
        result=[];current=head['payload_sha256']
        while current:
            row=journal.conn.execute('SELECT r.json FROM input_provenance_manifests p JOIN raw_events r ON r.seq=p.raw_seq WHERE p.hash=?',(current,)).fetchone()
            self.assertIsNotNone(row);record=json.loads(row[0]);validate_record(record,definition='RawEvent')
            validate_record(record['payload'],'input_provenance.schema.json')
            self.assertEqual(digest(record['payload']),record['payload_sha256'])
            archive=journal.conn.execute('SELECT compressed FROM raw_archives WHERE hash=?',(current,)).fetchone()[0]
            self.assertEqual(zlib.decompress(archive).decode(),canonical(record['payload']))
            result.append(record);current=record['payload']['previous_manifest_sha256']
        return list(reversed(result))

    def test_manifest_is_a_real_raw_event_and_resolves_all_original_payloads(self):
        raw=[self.append(self.journal,1000+i) for i in range(5)]
        head=self.journal.provenance(2000,chunk_size=2);chain=self.chain(self.journal,head)
        self.assertEqual(len(chain),3)
        items=[item for record in chain for item in record['payload']['items']]
        self.assertEqual([x['payload_sha256'] for x in items],[r['payload_sha256'] for r in raw])
        self.assertTrue(all(item['received_at']<=2000 and item['model_time_eligible'] for item in items))
        self.assertEqual(head['source_id'],'hengzhun-input-provenance')
        self.assertEqual(head['payload']['cumulative_visible_events'],5)
        text=canonical(head['payload'])
        self.assertNotIn(self.journal.boot_id,text);self.assertNotIn(self.journal.run_id,text)

    def test_future_source_is_audit_only_and_future_receipt_waits_without_being_lost(self):
        self.append(self.journal,1000);future_source=self.append(self.journal,1001,source=5000)
        future_received=self.append(self.journal,4000);late_visible=self.append(self.journal,1500,value=99)
        head=self.journal.provenance(2000)
        items=[i for r in self.chain(self.journal,head) for i in r['payload']['items']]
        self.assertEqual(len(items),3)
        audited=next(x for x in items if x['payload_sha256']==future_source['payload_sha256'])
        self.assertFalse(audited['model_time_eligible']);self.assertEqual(audited['audit_only_reason'],'SOURCE_TIME_FUTURE')
        self.assertNotIn(future_received['payload_sha256'],[x['payload_sha256'] for x in items])
        self.assertIn(late_visible['payload_sha256'],[x['payload_sha256'] for x in items])
        later=self.journal.provenance(4000)
        self.assertEqual(later['payload']['items'][0]['payload_sha256'],future_received['payload_sha256'])
        self.assertEqual(later['payload']['cumulative_visible_events'],4)
        self.assertEqual(self.journal.conn.execute('SELECT count(*) FROM input_provenance_pending').fetchone()[0],0)

    def test_future_tail_variants_and_random_capture_identity_do_not_change_past_head(self):
        heads=[]
        for index,tail in enumerate((1000,-1000,None)):
            journal=EventJournal(Path(self.temp.name)/('variant-'+str(index)+'.sqlite3'));self.addCleanup(journal.conn.close)
            self.append(journal,1000,value=100);self.append(journal,1500,value=101)
            if tail is not None:self.append(journal,5000,value=tail)
            heads.append(journal.provenance(2000)['payload_sha256'])
        self.assertEqual(len(set(heads)),1)

    def test_restart_continues_prefix_and_repeated_calls_do_not_duplicate_evidence(self):
        self.append(self.journal,1000);first=self.journal.provenance(2000)
        count=self.journal.counts()['raw_events'];self.journal.conn.close();self.journal=EventJournal(self.path)
        same=self.journal.provenance(2100)
        self.assertEqual(same['payload_sha256'],first['payload_sha256']);self.assertEqual(self.journal.counts()['raw_events'],count)
        next_raw=self.append(self.journal,2200);second=self.journal.provenance(2300)
        self.assertEqual(second['payload']['previous_manifest_sha256'],first['payload_sha256'])
        self.assertEqual(second['payload']['items'][0]['payload_sha256'],next_raw['payload_sha256'])
        self.assertEqual(second['payload']['cumulative_visible_events'],2)

    def test_append_and_cursor_are_one_transaction_when_manifest_write_fails(self):
        self.append(self.journal,1000);before=self.journal.counts()['raw_events']
        original=self.journal.append
        def fail(**kwargs):
            record=original(**kwargs)
            if kwargs.get('source_id')=='hengzhun-input-provenance':raise RuntimeError('injected disk transaction failure')
            return record
        with patch.object(self.journal,'append',side_effect=fail),self.assertRaisesRegex(RuntimeError,'injected'):
            self.journal.provenance(2000)
        self.assertEqual(self.journal.counts()['raw_events'],before)
        self.assertEqual(self.journal.conn.execute('SELECT visible_count FROM input_provenance_state').fetchone()[0],0)
        self.assertEqual(self.journal.conn.execute('SELECT count(*) FROM input_provenance_manifests').fetchone()[0],0)
        self.assertEqual(self.journal.provenance(2000)['payload']['cumulative_visible_events'],1)

    def test_work_after_large_history_is_proportional_to_new_events_and_chunks_are_bounded(self):
        for index in range(1200):self.append(self.journal,index+1)
        first=self.journal.provenance(2000)
        self.assertEqual(self.journal.provenance_stats['new_visible_events'],1200)
        self.assertLessEqual(self.journal.provenance_stats['maximum_manifest_items'],512)
        self.assertEqual(self.journal.provenance_stats['manifests_written'],3)
        for index in range(3):self.append(self.journal,2100+index)
        next_head=self.journal.provenance(2200)
        self.assertEqual(self.journal.provenance_stats['new_visible_events'],3)
        self.assertLessEqual(self.journal.provenance_stats['scanned_events'],6)
        self.assertEqual(next_head['payload']['previous_manifest_sha256'],first['payload_sha256'])
        self.assertEqual(next_head['payload']['item_count'],3)
        self.journal.provenance(2300)
        self.assertLessEqual(self.journal.provenance_stats['scanned_events'],1)
        self.assertEqual(self.journal.provenance_stats['manifests_written'],0)
