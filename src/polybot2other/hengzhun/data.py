"""独立追加事件日志和双时间 as-of 视图，不修改旧策略的数据或账本。"""
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
from functools import lru_cache
import json
import math
from pathlib import Path
import sqlite3
import threading
import uuid
import zlib
from contextlib import contextmanager, nullcontext

from .contracts import canonical, digest, validate_record, validator
from .model import PriceObservation


class EventJournal:
    def __init__(self, path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.boot_id=uuid.uuid4().hex
        self.conn=sqlite3.connect(self.path,check_same_thread=False)
        self.conn.row_factory=sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.executescript('''
          CREATE TABLE IF NOT EXISTS capture_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS raw_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE,
              received_at INTEGER,source_at INTEGER,kind TEXT,json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS raw_archives(hash TEXT PRIMARY KEY,compressed BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS market_contracts(hash TEXT PRIMARY KEY,json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS price_observations(channel TEXT,source_at INTEGER,received_at INTEGER,
              value TEXT,event_id TEXT,PRIMARY KEY(channel,source_at));
          CREATE TABLE IF NOT EXISTS features(id TEXT PRIMARY KEY,condition_id TEXT,at INTEGER,json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS landmarks(condition_id TEXT,rule_version TEXT,target INTEGER,
              at INTEGER,status TEXT,feature_id TEXT,PRIMARY KEY(condition_id,rule_version,target));
          CREATE TABLE IF NOT EXISTS official_results(id TEXT PRIMARY KEY,condition_id TEXT,available_at INTEGER,json TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS quality_events(id INTEGER PRIMARY KEY,at INTEGER,code TEXT,detail TEXT);
          CREATE TABLE IF NOT EXISTS proxy_samples(source_at INTEGER PRIMARY KEY,available_at INTEGER,error_bps REAL,json TEXT);
          CREATE TABLE IF NOT EXISTS condition_failures(code TEXT PRIMARY KEY,count INTEGER NOT NULL);
          CREATE INDEX IF NOT EXISTS raw_events_clock ON raw_events(received_at,seq);
          CREATE INDEX IF NOT EXISTS features_market ON features(condition_id,at);
          CREATE TABLE IF NOT EXISTS input_provenance_state(id INTEGER PRIMARY KEY CHECK(id=1),
              scanned_seq INTEGER NOT NULL,visible_count INTEGER NOT NULL,chain_hash TEXT,last_decision_at INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS input_provenance_pending(seq INTEGER PRIMARY KEY,received_at INTEGER NOT NULL);
          CREATE INDEX IF NOT EXISTS input_provenance_pending_clock ON input_provenance_pending(received_at,seq);
          CREATE TABLE IF NOT EXISTS input_provenance_manifests(hash TEXT PRIMARY KEY,raw_seq INTEGER UNIQUE NOT NULL,
              previous_hash TEXT,item_count INTEGER NOT NULL,cumulative_count INTEGER NOT NULL,at_ms INTEGER NOT NULL);
        ''')
        with self.conn:
            self.conn.execute('INSERT OR IGNORE INTO capture_meta VALUES(?,?)',('run_id',uuid.uuid4().hex))
            self.conn.execute('INSERT OR IGNORE INTO input_provenance_state VALUES(1,0,0,NULL,0)')
        self.run_id=self.conn.execute("SELECT value FROM capture_meta WHERE key='run_id'").fetchone()[0]
        self.last_event=None
        self._savepoint=0
        self.provenance_stats={'scanned_events':0,'new_visible_events':0,'manifests_written':0,'maximum_manifest_items':0}

    @contextmanager
    def transaction(self):
        """One durable commit per capture chunk; nested writes use savepoints.

        FULL synchronous/WAL remains enabled. No raw event is considered durable
        until the enclosing batch commits. Failed writes retain the prior head.
        """
        with self.lock:
            nested=self.conn.in_transaction;self._savepoint+=1;name='capture_'+str(self._savepoint)
            previous=self.last_event
            self.conn.execute('SAVEPOINT '+name if nested else 'BEGIN IMMEDIATE')
            try:
                yield
                self.conn.execute('RELEASE SAVEPOINT '+name if nested else 'COMMIT')
            except BaseException:
                if nested:
                    self.conn.execute('ROLLBACK TO SAVEPOINT '+name);self.conn.execute('RELEASE SAVEPOINT '+name)
                else:self.conn.execute('ROLLBACK')
                self.last_event=previous
                raise

    def quality(self, at, code, detail=''):
        with self.transaction():self.conn.execute('INSERT INTO quality_events(at,code,detail) VALUES(?,?,?)',(at,code,str(detail)[:500]))

    def append(self, *, source_kind, source_id, stream_id, payload, received_at, source_at=None,
               market=None, asset_id=None, full=False, sequence=None, connection_epoch=None, raw_text=None,
               _commit=True):
        if type(received_at) is not int or received_at<0:raise ValueError('INVALID_RECEIVE_TIME')
        source_status='MISSING'
        if source_at is not None:
            if isinstance(source_at,bool) or not isinstance(source_at,int) or source_at<0:source_at=None;source_status='MALFORMED'
            else:source_status='FUTURE' if source_at>received_at else 'VALID'
        raw=(raw_text if raw_text is not None else canonical(payload)).encode()
        raw_hash=sha256(raw).hexdigest();ident=uuid.uuid4().hex
        seq_contract=sequence or {'capability':'NONE','scope_id':None,'stream_seq':None,'check_status':'NOT_APPLICABLE','dropped_count':None}
        with (self.transaction() if _commit else nullcontext()):
            self.conn.execute('INSERT OR IGNORE INTO raw_archives VALUES(?,?)',(raw_hash,zlib.compress(raw)))
            cur=self.conn.execute('INSERT INTO raw_events(event_id,received_at,source_at,kind,json) VALUES(?,?,?,?,?)',
                                  (ident,received_at,source_at,source_kind,'{}'))
            record={'record_type':'RawEvent','schema_version':'1.0.0','event_id':ident,'capture_run_id':self.run_id,
                    'boot_id':self.boot_id,'ingest_seq':cur.lastrowid,'source_id':source_id,'stream_id':stream_id,
                    'source_kind':source_kind,'market':market,'asset_id':asset_id,'source_at':source_at,
                    'received_at':received_at,'source_time_status':source_status,'sequence':seq_contract,
                    'connection_epoch':connection_epoch or self.boot_id,'is_full_snapshot':bool(full),
                    'quality_reasons':[] if source_status=='VALID' else ['SOURCE_TIME_'+source_status],
                    'payload':payload,'payload_sha256':raw_hash,'raw_archive_locator':'sqlite:raw_archives/'+raw_hash,
                    'adapter_version':'hengzhun-observer-v1','raw_schema_hint':None}
            validate_record(record,definition='RawEvent')
            self.conn.execute('UPDATE raw_events SET json=? WHERE seq=?',(canonical(record),cur.lastrowid))
        self.last_event=record
        return record

    def provenance(self,now,*,chunk_size=512):
        """Seal only new causal observations into an append-only hash chain.

        The returned object is a genuine derived RawEvent whose payload hash can
        be put directly in FeatureFrame.input_event_hashes. Each bounded manifest
        links the prior manifest and raw payload hashes with original timestamps
        and capture sequence ranges. UUID/run/boot identifiers never enter the
        semantic payload. Valid time alone does not assert model inclusion.

        Future-received records wait in an indexed pending set. Future-source
        records received already remain in the audit chain, explicitly ineligible
        for model input. Repeated calls without new visible inputs reuse the head.
        """
        if type(now) is not int or now<0:raise ValueError('INVALID_DECISION_TIME')
        if type(chunk_size) is not int or not 1<=chunk_size<=512:raise ValueError('INVALID_PROVENANCE_CHUNK_SIZE')
        stats={'scanned_events':0,'new_visible_events':0,'manifests_written':0,'maximum_manifest_items':0}
        previous_last=self.last_event
        try:
            with self.transaction():
                state=self.conn.execute('SELECT * FROM input_provenance_state WHERE id=1').fetchone()
                if now<state['last_decision_at']:raise ValueError('PROVENANCE_CLOCK_REGRESSION')
                scanned=state['scanned_seq'];visible=state['visible_count'];head=state['chain_hash']
                high=self.conn.execute('SELECT coalesce(max(seq),0) FROM raw_events').fetchone()[0]
                def seal(rows):
                    nonlocal visible,head
                    if not rows:return
                    items=[]
                    for row in rows:
                        raw=json.loads(row['json'])
                        if raw['source_id']=='hengzhun-input-provenance':continue
                        source=raw.get('source_at');status=raw.get('source_time_status')
                        eligible=(status=='VALID' and type(source) is int and source<=raw['received_at']<=now)
                        items.append({'ingest_seq':row['seq'],'payload_sha256':raw['payload_sha256'],
                            'source_kind':raw['source_kind'],'source_id':raw['source_id'],'stream_id':raw['stream_id'],
                            'source_at':source,'received_at':raw['received_at'],'source_time_status':status,
                            'model_time_eligible':eligible,'audit_only_reason':None if eligible else 'SOURCE_TIME_'+str(status)})
                    if not items:return
                    body={'record_type':'InputProvenanceManifest','schema_version':'1.0.0',
                        'previous_manifest_sha256':head,'sequence_range':{'first':min(x['ingest_seq'] for x in items),
                            'last':max(x['ingest_seq'] for x in items)},'items':items,
                        'item_count':len(items),'cumulative_visible_events':visible+len(items),
                        'maximum_received_at':max(x['received_at'] for x in items),
                        'time_eligibility_is_not_model_inclusion':True,'derived':True}
                    validate_record(body,'input_provenance.schema.json')
                    event=self.append(source_kind='OTHER',source_id='hengzhun-input-provenance',stream_id='input-provenance',
                        payload=body,received_at=now,source_at=now,full=True,_commit=False)
                    self.conn.execute('INSERT INTO input_provenance_manifests VALUES(?,?,?,?,?,?)',
                        (event['payload_sha256'],event['ingest_seq'],head,len(items),visible+len(items),now))
                    head=event['payload_sha256'];visible+=len(items)
                    stats['new_visible_events']+=len(items);stats['manifests_written']+=1
                    stats['maximum_manifest_items']=max(stats['maximum_manifest_items'],len(items))
                # Records may have been loaded ahead of a replay clock. Their
                # later eligibility must not be lost when the append cursor moves.
                while True:
                    rows=self.conn.execute('SELECT r.* FROM input_provenance_pending p JOIN raw_events r ON r.seq=p.seq '
                        'WHERE p.received_at<=? ORDER BY p.seq LIMIT ?',(now,chunk_size)).fetchall()
                    if not rows:break
                    stats['scanned_events']+=len(rows);seal(rows)
                    self.conn.executemany('DELETE FROM input_provenance_pending WHERE seq=?',[(r['seq'],) for r in rows])
                while scanned<high:
                    rows=self.conn.execute('SELECT * FROM raw_events WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?',
                        (scanned,high,chunk_size)).fetchall()
                    if not rows:break
                    stats['scanned_events']+=len(rows);ready=[]
                    for row in rows:
                        raw=json.loads(row['json'])
                        if raw['source_id']=='hengzhun-input-provenance':continue
                        if row['received_at']<=now:ready.append(row)
                        else:self.conn.execute('INSERT OR IGNORE INTO input_provenance_pending VALUES(?,?)',(row['seq'],row['received_at']))
                    seal(ready);scanned=rows[-1]['seq']
                self.conn.execute('UPDATE input_provenance_state SET scanned_seq=?,visible_count=?,chain_hash=?,last_decision_at=? WHERE id=1',
                    (scanned,visible,head,now))
                result=self.conn.execute('SELECT r.json FROM input_provenance_manifests p JOIN raw_events r ON r.seq=p.raw_seq WHERE p.hash=?',
                    (head,)).fetchone() if head else None
            self.provenance_stats=stats
            return json.loads(result[0]) if result else None
        except Exception:
            self.last_event=previous_last
            raise

    def price(self, channel, source_at, received_at, value, event_id):
        number=Decimal(str(value))
        if not number.is_finite() or number<=0:raise ValueError('INVALID_PRICE')
        conflict=False
        with self.transaction():
            previous=self.conn.execute('SELECT * FROM price_observations WHERE channel=? AND source_at=?',(channel,source_at)).fetchone()
            if previous:
                if Decimal(previous['value'])!=number:
                    self.quality(received_at,'DATA_CONFLICT',f'{channel}:{source_at}');conflict=True
                else:return False
            else:self.conn.execute('INSERT INTO price_observations VALUES(?,?,?,?,?)',(channel,source_at,received_at,str(number),event_id))
        if conflict:raise ValueError('DATA_CONFLICT')
        return True

    def prices(self, channel, start, end, decision):
        with self.lock:
            rows=self.conn.execute('SELECT * FROM price_observations WHERE channel=? AND source_at>=? AND source_at<=? AND received_at<=? ORDER BY source_at',
                                   (channel,start,min(end,decision),decision)).fetchall()
        return [PriceObservation(r['source_at'],r['received_at'],float(r['value'])) for r in rows]

    def store_contract(self, contract):
        validate_record(contract,definition='MarketContractSnapshot')
        with self.transaction():self.conn.execute('INSERT OR IGNORE INTO market_contracts VALUES(?,?)',(contract['contract_sha256'],canonical(contract)))

    def feature(self, frame):
        validate_record(frame,definition='FeatureFrame')
        with self.transaction():
            self.conn.execute('INSERT INTO features VALUES(?,?,?,?)',(frame['frame_id'],frame['market']['condition_id'],frame['decision_at'],canonical(frame)))
            for code in set(frame['decision_reasons']+frame['data_reasons']):
                self.conn.execute('INSERT INTO condition_failures VALUES(?,1) ON CONFLICT(code) DO UPDATE SET count=count+1',(code,))

    def landmark(self, condition, rule, target, at, status, frame_id):
        with self.transaction():
            return bool(self.conn.execute('INSERT OR IGNORE INTO landmarks VALUES(?,?,?,?,?,?)',(condition,rule,target,at,status,frame_id)).rowcount)

    def counts(self):
        with self.lock:
            return {name:self.conn.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in
                    ('raw_events','market_contracts','price_observations','features','landmarks','official_results','proxy_samples')}


@lru_cache(maxsize=1)
def _book_header_validator():
    """Original BookView schema, with level checks factored into a bounded cache."""
    from jsonschema import Draft202012Validator
    schema=deepcopy(validator(definition='BookView').schema)
    for side in ('bids','asks'):schema['$defs']['BookView']['properties'][side].pop('items')
    return Draft202012Validator(schema)


@lru_cache(maxsize=32768)
def _validate_book_level(price,quantity):
    # Both strings are immutable and identify every field of the constructed
    # BookLevel. No mutation can turn a cached valid level into an invalid one.
    validate_record({'price':price,'quantity_shares':quantity},definition='BookLevel')


def validate_constructed_book(view):
    """Exactly the original structural checks without rewalking unchanged depth."""
    _book_header_validator().validate(view)
    for side in ('bids','asks'):
        for level in view[side]:
            if set(level)!={'price','quantity_shares'}:raise ValueError('BOOK_LEVEL_FIELDS_INVALID')
            _validate_book_level(level['price'],level['quantity_shares'])


class AsOfBooks:
    """从原始完整快照和增量独立重建，不使用旧系统截断的前 50 档。"""
    def __init__(self):
        self.depth={};self.clocks={};self.views=defaultdict(list)

    def ingest(self, message, source_at, received_at, event_id):
        tokens={str(message.get('asset_id',''))} if message.get('event_type')=='book' else {str(r.get('asset_id','')) for r in message.get('price_changes',[])}
        try:return self._ingest(message,source_at,received_at,event_id)
        except Exception as exc:
            for token in tokens:
                if not token:continue
                self.depth.pop(token,None)
                self.views[token].append(({'invalid':True,'source_at':min(source_at,received_at),'received_at':received_at},event_id))
            raise ValueError(str(exc) if isinstance(exc,ValueError) else 'BOOK_INVALID') from exc

    def _ingest(self, message, source_at, received_at, event_id):
        kind=message.get('event_type');changed=[]
        if kind=='book':
            token=str(message.get('asset_id',''))
            if not token:raise ValueError('BOOK_ASSET_MISSING')
            depth={}
            for side in ('bids','asks'):
                levels={}
                for level in message.get(side,[]):
                    p,q=Decimal(str(level['price'])),Decimal(str(level['size']))
                    if not p.is_finite() or not q.is_finite() or not 0<p<1 or q<0:raise ValueError('BOOK_INVALID')
                    if q:levels[p]=q
                depth[side]=levels
            if source_at<self.clocks.get(token,0):raise ValueError('BOOK_SOURCE_REGRESSION')
            if source_at==self.clocks.get(token) and token in self.depth and self.depth[token]!=depth:
                raise ValueError('BOOK_SOURCE_CONFLICT')
            self.depth[token]=depth;changed=[token]
        elif kind=='price_change':
            for level in message.get('price_changes',[]):
                token=str(level.get('asset_id',''))
                if token not in self.depth:raise ValueError('BOOK_RESYNC_REQUIRED')
                if source_at<self.clocks.get(token,0):raise ValueError('BOOK_SOURCE_REGRESSION')
                name={'BUY':'bids','SELL':'asks'}.get(level.get('side'))
                p,q=Decimal(str(level['price'])),Decimal(str(level['size']))
                if not name or not p.is_finite() or not q.is_finite() or not 0<p<1 or q<0:raise ValueError('BOOK_INVALID')
                if q:self.depth[token][name][p]=q
                else:self.depth[token][name].pop(p,None)
                changed.append(token)
        elif kind=='tick_size_change':raise ValueError('CONTRACT_TICK_CHANGED')
        for token in set(changed):
            self.clocks[token]=source_at;depth=self.depth[token]
            if not depth['bids'] or not depth['asks'] or max(depth['bids'])>min(depth['asks']):raise ValueError('BOOK_INVALID')
            view={'asset_id':token,'source_at':source_at,'received_at':received_at,
                  'bids':[{'price':str(p),'quantity_shares':str(depth['bids'][p])} for p in sorted(depth['bids'],reverse=True)],
                  'asks':[{'price':str(p),'quantity_shares':str(depth['asks'][p])} for p in sorted(depth['asks'])],
                  'is_complete':True,'observation_policy':'LOCAL_OBSERVED_ASOF'}
            previous=self.views[token][-1][0] if self.views[token] else None
            if previous and not previous.get('invalid') and all(view[k]==previous[k] for k in ('source_at','asset_id','bids','asks')):
                continue
            view['snapshot_sha256']=digest({k:v for k,v in view.items() if k!='received_at'})
            validate_constructed_book(view)
            # Every list/dict in view was newly built above; the depth map holds
            # Decimals only. No caller receives this stored mutable object.
            self.views[token].append((view,event_id))
            self.views[token]=self.views[token][-600:]

    def asof(self, token, at):
        eligible=[x for x in self.views.get(token,[]) if x[0]['source_at']<=at and x[0]['received_at']<=at]
        if not eligible:return None,None
        result=max(eligible,key=lambda x:(x[0]['received_at'],x[0]['source_at']))
        return (None,None) if result[0].get('invalid') else deepcopy(result)


def quality_reasons(reference, books, now, config, clock_bound_ms):
    reasons=[];rule=config['data']
    if not isinstance(clock_bound_ms,(int,float)) or isinstance(clock_bound_ms,bool) or not math.isfinite(clock_bound_ms) or clock_bound_ms<0:reasons.append('CLOCK_UNVERIFIED')
    elif clock_bound_ms>rule['clock_offset_max_ms']:reasons.append('CLOCK_OFFSET')
    for name in ('spot','twap'):
        value=reference.get(name)
        if not value:reasons.append('PRICE_MISSING');continue
        if not 0<=now-value['source_at']<=rule['price_source_max_age_ms']:reasons.append('PRICE_STALE')
        if not 0<=now-value['received_at']<=rule['price_receive_max_age_ms']:reasons.append('PRICE_STALE')
    for side in ('Up','Down'):
        book=books.get(side)
        if not book:reasons.append('BOOK_MISSING');continue
        if not 0<=now-book['source_at']<=rule['book_source_max_age_ms'] or not 0<=now-book['received_at']<=rule['book_receive_max_age_ms']:reasons.append('BOOK_STALE')
        if not book.get('is_complete'):reasons.append('BOOK_INVALID')
    if all(books.get(x) for x in ('Up','Down')) and abs(books['Up']['source_at']-books['Down']['source_at'])>rule['cross_book_max_skew_ms']:reasons.append('BOOK_SKEW')
    return list(dict.fromkeys(reasons))
