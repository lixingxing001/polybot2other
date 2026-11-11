"""Batch durability and cached schema validation retain the original contracts."""
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest

from polybot2other.hengzhun.contracts import digest,validate_record
from polybot2other.hengzhun.data import EventJournal,validate_constructed_book,_validate_book_level


class IngestPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'events.sqlite3';self.journal=EventJournal(self.path)
        self.addCleanup(self.journal.conn.close)

    def append(self,at,**changes):
        args={'source_kind':'ORDER_BOOK','source_id':'synthetic','stream_id':'book','payload':{'timestamp':at},
              'source_at':at,'received_at':at}
        args.update(changes);return self.journal.append(**args)

    def test_capture_batch_has_one_atomic_durable_commit_and_keeps_all_raw_bytes(self):
        reader=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True);self.addCleanup(reader.close)
        trace=[];self.journal.conn.set_trace_callback(trace.append)
        with self.journal.transaction():
            events=[self.append(1000+i,raw_text='{"timestamp": '+str(1000+i)+'}') for i in range(100)]
            self.assertEqual(reader.execute('SELECT count(*) FROM raw_events').fetchone()[0],0)
            self.assertEqual(self.journal.counts()['raw_events'],100)
        self.assertEqual(reader.execute('SELECT count(*) FROM raw_events').fetchone()[0],100)
        self.assertEqual(sum(command=='COMMIT' for command in trace),1)
        self.assertEqual(self.journal.conn.execute('PRAGMA synchronous').fetchone()[0],2)
        self.assertEqual([row[0] for row in self.journal.conn.execute('SELECT received_at FROM raw_events ORDER BY seq')],list(range(1000,1100)))
        for event in events:validate_record(event,definition='RawEvent')

    def test_failed_outer_batch_rolls_back_events_archives_price_and_provenance_together(self):
        with self.assertRaisesRegex(RuntimeError,'simulated failure'):
            with self.journal.transaction():
                event=self.append(1000)
                self.journal.price('spot',1000,1000,'100',event['event_id'])
                self.journal.provenance(1000)
                raise RuntimeError('simulated failure')
        for table in ('raw_events','raw_archives','price_observations','input_provenance_manifests'):
            self.assertEqual(self.journal.conn.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        self.assertIsNone(self.journal.last_event)
        self.assertEqual(self.journal.conn.execute('SELECT visible_count FROM input_provenance_state').fetchone()[0],0)

    def test_rejected_nested_event_does_not_poison_other_events_or_commit_early(self):
        with self.journal.transaction():
            self.append(1000)
            with self.assertRaises(Exception):self.append(1001,source_kind='INVALID_SCHEMA_KIND')
            self.append(1002)
        self.assertEqual(self.journal.counts()['raw_events'],2)
        self.assertEqual(self.journal.conn.execute("SELECT count(*) FROM raw_events WHERE json='{}'").fetchone()[0],0)
        self.assertEqual(self.journal.conn.execute('SELECT count(*) FROM raw_archives').fetchone()[0],2)

    @staticmethod
    def view():
        return {'asset_id':'synthetic','source_at':1000,'received_at':1000,'snapshot_sha256':digest('book'),
            'bids':[{'price':'0.49','quantity_shares':'100'}],'asks':[{'price':'0.5','quantity_shares':'100'}],
            'is_complete':True,'observation_policy':'LOCAL_OBSERVED_ASOF'}

    def test_factored_validator_agrees_with_original_schema_for_valid_and_invalid_shapes(self):
        base=self.view();variants=[base]
        for field,value in (('asset_id',''),('source_at',-1),('received_at',True),('snapshot_sha256','bad'),
                            ('is_complete',False),('observation_policy','FAKE'),('bids',[]),('asks',[])):
            view=deepcopy(base);view[field]=value;variants.append(view)
        for key,value in (('price','-1'),('price','1e-3'),('price',.5),('price','0'*81),
                          ('quantity_shares','0'),('quantity_shares','NaN'),('quantity_shares','-1'),('extra','x')):
            view=deepcopy(base);view['asks'][0][key]=value;variants.append(view)
        extra=deepcopy(base);extra['extra']=True;variants.append(extra)
        for index,view in enumerate(variants):
            def accepted(check):
                try:check(view);return True
                except Exception:return False
            with self.subTest(index=index):
                self.assertEqual(accepted(validate_constructed_book),accepted(lambda item:validate_record(item,definition='BookView')))

    def test_unchanged_depth_uses_cached_complete_level_validation_and_mutations_are_rechecked(self):
        view=self.view();validate_constructed_book(view)
        misses=_validate_book_level.cache_info().misses
        for at in range(1001,1101):
            view['source_at']=view['received_at']=at;validate_constructed_book(view)
        self.assertEqual(_validate_book_level.cache_info().misses,misses)
        changed=deepcopy(view);changed['asks'][0]['quantity_shares']='-1'
        with self.assertRaises(Exception):validate_constructed_book(changed)
        validate_constructed_book(view)


if __name__=='__main__':unittest.main()
