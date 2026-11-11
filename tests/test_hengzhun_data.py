"""衡准 V1 数据可见性和旁路 observer；人工数据，不触网。"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock,patch
import zlib
from jsonschema import ValidationError

from polybot2other.hengzhun.config import load_config
from polybot2other.hengzhun.data import EventJournal,AsOfBooks,quality_reasons
from polybot2other.hengzhun.contracts import validate_record
from polybot2other.hengzhun.clock import ClockMonitor
from polybot2other.buffered_ws import BufferedWebSocket
from polybot2other.optimized_reference import ReferenceCache,SPOT
from polybot2other.managed_live_book import ReliableLiveBooks


def full_book(asset='up',levels=80):
    return {'event_type':'book','asset_id':asset,
        'bids':[{'price':f'{.4-i*.001:.3f}','size':'10'} for i in range(levels)],
        'asks':[{'price':f'{.5+i*.001:.3f}','size':'10'} for i in range(levels)]}


def ws_frame(text,opcode=1,fin=True):
    data=text.encode();return bytes([(128 if fin else 0)|opcode,len(data)])+data


class JournalAndVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.journal=EventJournal(self.root/'events.sqlite3');self.addCleanup(self.journal.conn.close)

    def append(self,payload,at=1000,source=900,raw=None):
        return self.journal.append(source_kind='OFFICIAL_SPOT',source_id='official-test',stream_id='spot',
            payload=payload,source_at=source,received_at=at,raw_text=raw)

    def test_raw_bytes_are_archived_and_hashed_before_json_normalization(self):
        raw=' { "z": 1,  "a": "100.00" }\n';payload=json.loads(raw)
        event=self.append(payload,raw=raw)
        validate_record(event,definition='RawEvent')
        self.assertEqual(event['payload_sha256'],sha256(raw.encode()).hexdigest())
        stored=self.journal.conn.execute('SELECT compressed FROM raw_archives WHERE hash=?',(event['payload_sha256'],)).fetchone()[0]
        self.assertEqual(zlib.decompress(stored),raw.encode())
        self.assertNotEqual(event['payload_sha256'],sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest())
        second=self.append(payload,at=1100,raw=raw)
        self.assertGreater(second['ingest_seq'],event['ingest_seq']);self.assertNotEqual(second['event_id'],event['event_id'])
        self.assertEqual(self.journal.counts()['raw_events'],2)
        self.assertEqual(self.journal.conn.execute('SELECT count(*) FROM raw_archives').fetchone()[0],1)

    def test_ex006_future_source_and_late_receipt_never_leak_into_past_asof(self):
        self.journal.price('spot',900,950,'100','visible')
        self.journal.price('spot',1000,2000,'101','late')
        self.journal.price('spot',2000,1000,'102','future-source')
        rows=self.journal.prices('spot',0,9999,1500)
        self.assertEqual([(r.source_at_ms,r.received_at_ms) for r in rows],[(900,950)])
        later=self.journal.prices('spot',0,9999,2500)
        self.assertEqual(len(later),3)

    def test_duplicates_keep_first_received_and_conflicts_are_append_only(self):
        first=self.append({'value':'100'},at=1000,source=900)
        self.assertTrue(self.journal.price('spot',900,1000,'100',first['event_id']))
        duplicate=self.append({'value':'100'},at=1500,source=900)
        self.assertFalse(self.journal.price('spot',900,1500,'100',duplicate['event_id']))
        conflict=self.append({'value':'101'},at=1600,source=900)
        with self.assertRaisesRegex(ValueError,'CONFLICT'):
            self.journal.price('spot',900,1600,'101',conflict['event_id'])
        row=self.journal.conn.execute('SELECT * FROM price_observations').fetchone()
        self.assertEqual((row['received_at'],row['value'],row['event_id']),(1000,'100',first['event_id']))
        self.assertEqual(self.journal.counts()['raw_events'],3)
        self.assertEqual(self.journal.conn.execute("SELECT count(*) FROM quality_events WHERE code='DATA_CONFLICT'").fetchone()[0],1)
        self.assertTrue(self.journal.price('spot',1100,1700,'100','same-value-new-source'))

    def test_ex003_restart_preserves_capture_run_but_changes_boot_id(self):
        run,boot=self.journal.run_id,self.journal.boot_id
        self.append({'x':1});self.journal.conn.close()
        self.journal=EventJournal(self.root/'events.sqlite3')
        self.assertEqual(self.journal.run_id,run);self.assertNotEqual(self.journal.boot_id,boot)
        self.assertEqual(self.journal.counts()['raw_events'],1)

    def test_malformed_future_source_timestamps_remain_diagnostic_raw_events(self):
        future=self.append({'value':'100'},source=2000,at=1000)
        self.assertEqual(future['source_time_status'],'FUTURE')
        missing=self.append({'value':'100'},source=None)
        self.assertEqual(missing['source_time_status'],'MISSING')
        malformed=self.append({'value':'100'},source=True)
        self.assertEqual(malformed['source_time_status'],'MALFORMED');self.assertIsNone(malformed['source_at'])
        with self.assertRaises((ValueError,ValidationError)):self.append({},at=True)


class BookAsOfTests(unittest.TestCase):
    def test_complete_depth_is_not_truncated_to_50_levels(self):
        books=AsOfBooks();books.ingest(full_book(),1000,1100,'b1')
        view,event=books.asof('up',1100)
        self.assertEqual(len(view['asks']),80);self.assertEqual(len(view['bids']),80)
        self.assertIn('quantity_shares',view['asks'][0]);self.assertNotIn('size',view['asks'][0])
        validate_record(view,definition='BookView');self.assertEqual(event,'b1')
        self.assertEqual(books.asof('up',1099),(None,None))

    def test_ex006_ex009_dual_clock_and_future_better_book_preserve_old_view(self):
        books=AsOfBooks();books.ingest(full_book(),1000,1000,'old')
        newer=full_book();newer['asks'][0]['size']='100'
        books.ingest(newer,1200,2000,'late-better')
        self.assertEqual(books.asof('up',1500)[1],'old')
        self.assertEqual(books.asof('up',2000)[1],'late-better')
        books2=AsOfBooks();books2.ingest(full_book(),2000,1000,'future')
        self.assertEqual(books2.asof('up',1500),(None,None))

    def test_duplicate_full_snapshot_does_not_refresh_first_receive_clock(self):
        books=AsOfBooks();books.ingest(full_book(),1000,1100,'first')
        books.ingest(full_book(),1000,1500,'duplicate')
        view,event=books.asof('up',1500)
        self.assertEqual(view['received_at'],1100);self.assertEqual(event,'first')
        books.ingest(full_book(),1600,1700,'same-value-new-time')
        self.assertEqual(books.asof('up',1700)[0]['source_at'],1600)

    def test_increment_without_snapshot_requires_resync(self):
        with self.assertRaisesRegex(ValueError,'RESYNC'):
            AsOfBooks().ingest({'event_type':'price_change','price_changes':[{'asset_id':'up','side':'BUY','price':'.4','size':'10'}]},1000,1100,'delta')

    def test_conflicting_same_source_or_crossed_state_is_not_masked_by_old_book(self):
        for corrupt in ('same-source','crossed','empty'):
            with self.subTest(corrupt=corrupt):
                books=AsOfBooks();books.ingest(full_book(),1000,1100,'valid')
                changed=full_book()
                if corrupt=='same-source':changed['asks'][0]['size']='999'
                elif corrupt=='crossed':changed['bids'][0]['price']='.8'
                else:changed['asks']=[]
                with self.assertRaises(ValueError):books.ingest(changed,1000 if corrupt=='same-source' else 1200,1300,'bad')
                self.assertEqual(books.asof('up',1400),(None,None))
                self.assertEqual(books.asof('up',1150)[1],'valid')
                books.ingest(full_book(),1500,1600,'resync')
                self.assertEqual(books.asof('up',1600)[1],'resync')

    def test_increment_updates_all_depth_without_altering_prior_snapshot(self):
        books=AsOfBooks();books.ingest(full_book(),1000,1100,'b1')
        books.ingest({'event_type':'price_change','price_changes':[{'asset_id':'up','side':'SELL','price':'.500','size':'0'}]},1200,1300,'delta')
        self.assertEqual(books.asof('up',1150)[0]['asks'][0]['price'],'0.500')
        self.assertEqual(books.asof('up',1400)[0]['asks'][0]['price'],'0.501')


class DataQualityTests(unittest.TestCase):
    def setUp(self):
        self.config=load_config();self.now=10000
        self.ref={name:{'source_at':9500,'received_at':9600} for name in ('spot','twap')}
        self.books={side:{'is_complete':True,'source_at':9500,'received_at':9600} for side in ('Up','Down')}

    def test_ex010_new_receipt_cannot_disguise_stale_source(self):
        self.assertEqual(quality_reasons(self.ref,self.books,self.now,self.config,10),[])
        self.ref['spot'].update(source_at=8000,received_at=10000)
        self.assertIn('PRICE_STALE',quality_reasons(self.ref,self.books,self.now,self.config,10))
        self.books['Up'].update(source_at=8000,received_at=10000)
        self.assertIn('BOOK_STALE',quality_reasons(self.ref,self.books,self.now,self.config,10))

    def test_ex034_unknown_invalid_or_overbound_clock_blocks(self):
        for bound in (None,float('nan'),float('inf'),True,-1,251):
            with self.subTest(bound=bound):
                self.assertTrue(any(r.startswith('CLOCK_') for r in quality_reasons(self.ref,self.books,self.now,self.config,bound)))
        self.assertEqual(quality_reasons(self.ref,self.books,self.now,self.config,250),[])

    def test_cross_book_skew_500ms_boundary_and_future_time_rejection(self):
        self.books['Up']['source_at']=10000;self.books['Down']['source_at']=9500
        self.assertNotIn('BOOK_SKEW',quality_reasons(self.ref,self.books,self.now,self.config,0))
        self.books['Down']['source_at']=9499
        self.assertIn('BOOK_SKEW',quality_reasons(self.ref,self.books,self.now,self.config,0))
        self.books['Up']['source_at']=10001
        self.assertIn('BOOK_STALE',quality_reasons(self.ref,self.books,self.now,self.config,0))
        self.ref['twap']['received_at']=10001
        self.assertIn('PRICE_STALE',quality_reasons(self.ref,self.books,self.now,self.config,0))

    def test_clock_evidence_expires_without_silently_becoming_zero(self):
        wall=[100.];mono=[0.]
        monitor=ClockMonitor(wall_clock=lambda:wall[0],monotonic=lambda:mono[0],max_age_ms=1000,
            probe_fn=lambda **kw:{'status':'VERIFIED','offset_bound_ms':20.,'checked_at':100000})
        self.assertIsNone(monitor.snapshot()['offset_bound_ms'])
        self.assertEqual(monitor.probe()['status'],'VERIFIED')
        wall[0]=102.;mono[0]=2.
        expired=monitor.snapshot()
        self.assertEqual(expired['reason'],'CLOCK_EVIDENCE_STALE');self.assertIsNone(expired['offset_bound_ms'])


class BufferedReceiveTimestampTests(unittest.TestCase):
    class Sock:
        def __init__(self,chunks,clock):self.chunks=list(chunks);self.clock=clock;self.reads=0
        def settimeout(self,value):pass
        def recv(self,size):
            self.reads+=1
            if not self.chunks:raise socket.timeout()
            stamp,value=self.chunks.pop(0);self.clock[0]=stamp;return value
        def close(self):pass
        def sendall(self,value):pass

    def test_coalesced_buffered_frames_keep_recv_timestamp_when_consumed_later(self):
        clock=[0.];sock=self.Sock([(100.,ws_frame('first')+ws_frame('second'))],clock)
        with patch('polybot2other.buffered_ws.time.time',side_effect=lambda:clock[0]):
            ws=BufferedWebSocket(sock)
            self.assertEqual(ws.read_text(),'first');self.assertEqual(ws.last_received_ms,100000)
            clock[0]=999.
            self.assertEqual(ws.read_text(),'second');self.assertEqual(ws.last_received_ms,100000)
            self.assertEqual(sock.reads,1)

    def test_payload_tail_recv_marks_complete_frame_and_fragments(self):
        encoded=ws_frame('hello');clock=[0.]
        chunks=[(100.,encoded[:3]),(101.,encoded[3:])]
        with patch('polybot2other.buffered_ws.time.time',side_effect=lambda:clock[0]):
            ws=BufferedWebSocket(self.Sock(chunks,clock));self.assertEqual(ws.read_text(),'hello');self.assertEqual(ws.last_received_ms,101000)
            ws=BufferedWebSocket(self.Sock([(200.,ws_frame('part',fin=False)),(201.,ws_frame('ping',opcode=9)),(202.,ws_frame('two',opcode=0))],clock))
            ws._send_pong=Mock();self.assertEqual(ws.read_text(),'parttwo');self.assertEqual(ws.last_received_ms,202000)
            ws._send_pong.assert_called_once_with(b'ping')

    def test_handshake_tail_has_original_receive_time(self):
        clock=[100.]
        with patch('polybot2other.buffered_ws.time.time',side_effect=lambda:clock[0]):
            ws=BufferedWebSocket(self.Sock([],clock),initial=ws_frame('tail'))
            clock[0]=999.;self.assertEqual(ws.read_text(),'tail');self.assertEqual(ws.last_received_ms,100000)


class ObserverIsolationTests(unittest.TestCase):
    def test_reference_observer_clock_does_not_change_legacy_receive_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=ReferenceCache(Path(directory)/'reference.sqlite3')
            try:
                observer=Mock();cache.observer=observer
                f={'channel':SPOT,'seq':1,'payload':{'source':'chainlink','symbol':'btcusd','timestamp':4000,'value':'100'}}
                self.assertTrue(cache.ingest(f,5000,observer_received_ms=4500,raw_text='original bytes'))
                observer.assert_called_once_with('REFERENCE',f,4500,raw_text='original bytes')
                self.assertEqual(cache.latest[SPOT]['received_ms'],5000)
                cache.observer=Mock(side_effect=RuntimeError('observer failed'))
                f['seq']=2;f['payload']['timestamp']=6000
                self.assertTrue(cache.ingest(f,6100,observer_received_ms=6050))
                self.assertEqual(cache.latest[SPOT]['received_ms'],6100)
            finally:cache.conn.close()

    def test_optional_absent_or_none_observer_has_no_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            cache=ReferenceCache(Path(directory)/'reference.sqlite3')
            try:
                cache._observe('REFERENCE',{},1000)
                del cache.observer
                cache._observe('REFERENCE',{},1000)
            finally:cache.conn.close()
        books=ReliableLiveBooks(lambda:None,Mock(),lambda:{},None)
        books._observe('BOOK',{},1000);del books.observer
        books._observe('BOOK',{},1000)
        observer=Mock(side_effect=ValueError('ignored by old strategy'));books.observer=observer
        books._observe('BOOK',{'x':1},1000,raw_text='raw')
        observer.assert_called_once_with('BOOK',{'x':1},1000,raw_text='raw')
