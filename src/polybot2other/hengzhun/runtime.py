"""衡准 V1 独立研究运行器：原始采集、landmark、影子预测与隔离 Paper。

唯一借用对象为只读行情；不接受 ManagedExchange、真实钱包或任何 signer。
"""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
import json
import os
from itertools import groupby
from pathlib import Path
import queue
import threading
import time
import uuid

from . import STRATEGY_ID, DISPLAY_NAME
from .config import load_config, config_hash
from .contracts import SPEC_DIR, canonical, digest, contract_hash, validate_record
from .data import EventJournal, AsOfBooks, quality_reasons
from .clock import ClockMonitor
from .model import (causal_grid,estimate_volatility,integrate_spot,proxy_error_stats,
                    ProxyErrorObservation,predict_terminal_twap,ModelUnavailable)
from .calibration import predict_calibrated, raw_features, FEATURE_ORDER
from .pricing import choose_entry,cost_quote,fee_adapter,plan_side
from .ledger import HengzhunLedger
from .execution import PaperVenue,effective_delay

CODE_VERSION='hengzhun-v1-python-1'


def epoch_ms():return int(time.time()*1000)


def unknown_contract(market,now,adapter_code_sha256=None):
    """保留已经观察的事实；旧模块的ready不等于本策略IV01-03全部已核验。"""
    opened=int(market.started_at*1000)
    evidence={'source_label':'existing verified opening collector','source_locator':'local:managed-live/reference',
              'raw_payload_sha256':digest(asdict(market)),'observed_at':now,
              'verification_method':'旧collector可供对照；待本策略逐项合同审计','verified_at':None}
    result={'record_type':'MarketContractSnapshot','schema_version':'1.0.0','snapshot_id':uuid.uuid4().hex,
        'captured_at':now,'contract_sha256':'0'*64,'market':{'market_uid':market.round_id,'condition_id':market.condition_id},
        'slug':market.slug or market.round_id,'market_protocol_version':None,'up_asset_id':market.up_token,
        'down_asset_id':market.down_token,'collateral_asset_id':None,'collateral_decimals':None,'listed_at':None,
        'window_open_at':opened,'window_close_at':int(market.ends_at*1000),'window_duration_seconds':300,
        'rules':{'rule_version':'UNVERIFIED-BTC5M','rule_text_sha256':digest({'question':market.question}),
            'resolution_feed_id':None,'price_kind':'UNVERIFIED','symbol':'BTC/USD','twap_window_seconds':None,
            'comparison':'UNVERIFIED','tie_policy':'UNVERIFIED','result_verification_rule_id':None},
        'baseline':{'value':str(market.target_price) if market.target_price>0 else None,'source_at':opened,
            'received_at':None,'verified_at':None,'status':'UNVERIFIED','resolution_feed_id':None,'boundary_rule_id':None,
            'verification_method':None,'evidence':[evidence]},
        'trading':{'accepting_orders':False,'book_state':'UNVERIFIED','tick_size':None,'tick_size_unit':'COLLATERAL_PER_SHARE',
            'min_order_size':None,'min_order_size_unit':'UNVERIFIED','quantity_step':None,'quantity_unit':'SHARES',
            'quantity_decimals':None,'supported_order_types':['FAK'],
            'fee_contract':{'verification_status':'UNVERIFIED','fee_model_id':None,'fee_formula_id':None,'coefficient':None,
                'collection_mode':'UNVERIFIED','amount_semantics':'UNVERIFIED','rounding_mode':None,'rounding_increment':None,
                'minimum_fee':None,'evidence':[]},'delay_status':'UNKNOWN','venue_min_arrival_delay_ms':None,
            'delay_included_in_scenario':True,'delay_evidence':[]},
        'versions':{'adapter_version':CODE_VERSION,'adapter_code_sha256':adapter_code_sha256 or digest(Path(__file__).read_text()),
            'rest_schema_version':'legacy-gamma-observed','websocket_schema_version':'observed-clob-polybolt',
            'sdk_locked_version':'py_clob_client_v2-1.0.1','signature_domain_version':None,'data_api_version':None},
        'verification_status':'UNVERIFIED','verification_reasons':['IV01_BOUNDARY_AUDIT','IV02_FEE_PRECISION_AUDIT','IV03_PROTOCOL_MAPPING_AUDIT'],
        'evidence':[evidence]}
    result['contract_sha256']=contract_hash(result)
    return validate_record(result,definition='MarketContractSnapshot')


class HengzhunRuntime:
    def __init__(self,path,feed,*,config=None,clock=None,event_clock=None,replay_baseline=None,baseline_decider=None,trade_start_at=None,evaluation_end_at=None):
        self.path=Path(path);self.path.mkdir(parents=True,exist_ok=True)
        self.config=load_config(config);validate_record(self.config,'config.schema.json')
        self.mode=self.config['runtime']['mode']
        if self.mode=='REPLAY' and not callable(event_clock):raise ValueError('REPLAY_REQUIRES_OFFLINE_EVENT_CLOCK')
        self.now=event_clock or epoch_ms
        self.economic=self.mode in ('PAPER','REPLAY')
        if (replay_baseline is not None or baseline_decider is not None) and self.mode!='REPLAY':raise ValueError('BASELINE_POLICY_REPLAY_ONLY')
        self.replay_baseline=replay_baseline;self.baseline_decider=baseline_decider;self.baseline_inputs=None
        self.trade_start_at=trade_start_at;self.evaluation_end_at=evaluation_end_at
        self.strategy_id=replay_baseline['strategy_id'] if replay_baseline else STRATEGY_ID
        self.config_sha=config_hash(self.config);self.feed=feed
        self.code_manifest={p.name:digest(p.read_text()) for p in sorted(Path(__file__).parent.glob('*.py'))}
        self.code_sha=digest(self.code_manifest)
        self._contract_event_hashes={};self._last_model_market=None
        self.journal=EventJournal(self.path/'events.sqlite3');self.books=AsOfBooks()
        self.clock=clock or ClockMonitor(offset_limit_ms=self.config['data']['clock_offset_max_ms'])
        self.processing_backlog=False;self._retry_items=[];self._deferred_venue_events=None
        self.processing_stats={'processed_events':0,'batch_count':0,'last_batch_ms':0.,'max_batch_ms':0.,'oldest_pending_age_ms':0,'last_decision_ms':0.,'stop_spooled_events':0}
        self.pending=queue.Queue(maxsize=10000);self.stop_event=threading.Event();self.thread=None;self.aux_thread=None
        self.lock=threading.RLock();self.fault=None;self.drop_count=0;self.reference={};self.markets={};self.contract=None
        self.last_frame=None;self.last_decision_at=0;self.last_input=None;self.last_heartbeat=0
        self.recovery_fault_at=None;self.full_resync_at={};self.recovery_healthy_since=None
        self.healthy_since=None;self.channel_epoch=default_epochs();self.sequence={};self.proxy_last=0
        self.snapshot_cache={};self._last_cache_at=0
        self.model_path=self.path/'models/current.json';self.model_path.parent.mkdir(exist_ok=True)
        if not self.model_path.exists():self.model_path.write_bytes((SPEC_DIR/'model_unfitted.json').read_bytes())
        self.model=json.loads(self.model_path.read_text());validate_record(self.model,'model_artifact.schema.json')
        manifest=self.path/'run.json'
        if manifest.exists():
            run=json.loads(manifest.read_text())
            if run['config_sha256']!=self.config_sha:raise ValueError('CONFIG_CHANGED_CREATE_NEW_EXPERIMENT')
        else:
            run={'run_id':self.journal.run_id,'started_at':self.now(),'config_sha256':self.config_sha,
                 'config':self.config,'strategy_id':STRATEGY_ID,'spec_version':'1.0.0','language_decision':'USER_APPROVED_PYTHON',
                 'code_manifest':self.code_manifest,'code_manifest_sha256':self.code_sha}
            manifest.write_text(canonical(run))
        boots=self.path/'boots';boots.mkdir(exist_ok=True)
        boot={'boot_id':self.journal.boot_id,'run_id':run['run_id'],'started_at':self.now(),
              'config_sha256':self.config_sha,'code_manifest_sha256':self.code_sha,'code_manifest':self.code_manifest}
        with (boots/(self.journal.boot_id+'.json')).open('x') as out:out.write(canonical(boot))
        self.run=run;self.namespace={'mode':self.mode,'portfolio_run_id':run['run_id'],'scenario_id':self.config['paper']['default_scenario'],'ledger_scope':'PAPER_PORTFOLIO'}
        self.ledger=HengzhunLedger(self.path/'ledger.sqlite3',namespace=self.namespace,config=self.config,replay_baseline=replay_baseline)
        self.ledger.ensure_wallet('HENGZHUN',self.now())
        self.ledger.recover(self.now())
        self.venue=PaperVenue(self.ledger,fee_adapter)
        self.shadow=None
        if self.economic and not self.replay_baseline:
            from .shadow import SignalShadowCoordinator
            self.shadow=SignalShadowCoordinator(self.path/'signal-shadow.sqlite3',self.namespace,self.config)
            self.shadow.recover(self.now())
        self._load_markets()
        self._initialize_result_polling()
        with self.journal.transaction():
            self.journal.conn.execute('CREATE TABLE IF NOT EXISTS prior_shadow_frames(frame_id TEXT PRIMARY KEY,at INTEGER,json TEXT NOT NULL)')

    def _load_markets(self):
        with self.journal.lock:
            for row in self.journal.conn.execute('SELECT json FROM market_contracts'):
                c=json.loads(row[0]);self.markets[c['market']['condition_id']]=c

    def capture(self,kind,payload,received_ms,raw_text=None):
        """采集线程只复制并入有界队列；任何故障都只暂停本策略。"""
        if self.stop_event.is_set():return
        try:self.pending.put_nowait((kind,deepcopy(payload),int(received_ms),raw_text))
        except Exception as exc:
            self.drop_count+=1;self.fault='QUEUE_OVERFLOW' if isinstance(exc,queue.Full) else 'CAPTURE_FAILED'
            self.recovery_fault_at=self.now();self.recovery_healthy_since=None

    def start(self):
        if self.mode=='REPLAY':raise ValueError('REPLAY_NETWORK_START_FORBIDDEN')
        if self.thread and self.thread.is_alive():return
        self.stop_event.clear()
        self.feed.references.observer=self.capture;self.feed.books.observer=self.capture;self.feed.research_observer=self.capture
        self.thread=threading.Thread(target=self._run,name='hengzhun-record',daemon=True);self.thread.start()
        self.aux_thread=threading.Thread(target=self._aux_loop,name='hengzhun-clock-results',daemon=True);self.aux_thread.start()

    def _archive_captured(self,item):
        """Shutdown fallback preserves raw bytes without reconstructing past decisions."""
        kind,payload,received,raw_text=item
        source=None;asset=None;stream=kind
        source_kind={'BOOK':'ORDER_BOOK','REST_BOOK':'ORDER_BOOK','CLOCK':'CLOCK','RESULT':'OFFICIAL_RESULT'}.get(kind,'MARKET_METADATA')
        source_id={'BOOK':'clob-websocket','REST_BOOK':'legacy-normalized-rest','CLOCK':'ntp-read-only','RESULT':'shutdown-result-archive'}.get(kind,'gamma-read-only')
        if kind=='REFERENCE':
            stream=payload.get('channel') or 'unknown';source_id='polybolt-chainlink'
            source_kind={'price.crypto':'OFFICIAL_SPOT','price.crypto.twap':'OFFICIAL_TWAP'}.get(stream,'OTHER')
            stamp=(payload.get('payload') or {}).get('timestamp')
        elif kind in ('BOOK','REST_BOOK'):
            stamp=payload.get('timestamp',payload.get('source_ms'));asset=payload.get('asset_id',payload.get('token_id'));stream=asset or 'market'
        else:
            stamp=payload.get('source_at') if isinstance(payload,dict) else None
            if kind.endswith('_HEALTH'):source_kind='OTHER';source_id='collector-health'
        try:
            if stamp is not None and not isinstance(stamp,bool):source=int(stamp)
        except (ValueError,TypeError,OverflowError):source=None
        return self.journal.append(source_kind=source_kind,source_id=source_id,stream_id=stream,payload=payload,
            received_at=received,source_at=source,asset_id=asset,raw_text=raw_text,
            full=isinstance(payload,dict) and (bool(payload.get('snapshot')) or payload.get('event_type')=='book'))

    def _drain_capture_batch(self,*,limit=256,max_seconds=.025,archive_only=False):
        """One durable raw batch; venue consequences occur only after its commit."""
        began=time.perf_counter();items=[];deferred=[]
        if not archive_only:self._deferred_venue_events=deferred
        try:
            with self.journal.transaction():
                while len(items)<limit and (not items or time.perf_counter()-began<max_seconds):
                    if self._retry_items:item=self._retry_items.pop(0)
                    else:
                        try:item=self.pending.get_nowait()
                        except queue.Empty:break
                    items.append(item)
                    if archive_only:self._archive_captured(item)
                    else:self.ingest(*item)
        except Exception:
            self._retry_items=items+self._retry_items
            self.reference={};self.books=AsOfBooks();self.healthy_since=None
            raise
        finally:self._deferred_venue_events=None
        # Raw capture is already durable before any simulated acknowledgement.
        for _,group in groupby(deferred,key=lambda event:event['received_at']):self._venue_events(list(group))
        elapsed=(time.perf_counter()-began)*1000
        if items:
            self.processing_stats['processed_events']+=len(items);self.processing_stats['batch_count']+=1
            self.processing_stats['last_batch_ms']=elapsed
            self.processing_stats['max_batch_ms']=max(self.processing_stats['max_batch_ms'],elapsed)
        return len(items)

    def _pending_clock(self):
        if self._retry_items:return self._retry_items[0][2]
        with self.pending.mutex:return self.pending.queue[0][2] if self.pending.queue else None

    def _spool_pending(self):
        """Rare bounded-stop spill: immutable append-only capture file, never dropped."""
        items=self._retry_items;self._retry_items=[]
        while True:
            try:items.append(self.pending.get_nowait())
            except queue.Empty:break
        if not items:return None
        folder=self.path/'capture-spool';folder.mkdir(exist_ok=True)
        ident=self.journal.boot_id+'-'+uuid.uuid4().hex;temporary=folder/(ident+'.tmp');destination=folder/(ident+'.jsonl')
        try:
            with temporary.open('x') as stream:
                for kind,payload,received,raw_text in items:
                    stream.write(canonical({'kind':kind,'payload':payload,'received_at':received,'raw_text':raw_text,'capture_boot_id':self.journal.boot_id})+'\n')
                stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,destination)
            self.processing_stats['stop_spooled_events']+=len(items)
            (folder/(ident+'.manifest.json')).write_text(canonical({'file':destination.name,'event_count':len(items),
                'sha256':__import__('hashlib').sha256(destination.read_bytes()).hexdigest(),'status':'RAW_PRESERVED_NOT_MATERIALIZED',
                'first_received_at':min(item[2] for item in items),'last_received_at':max(item[2] for item in items)}))
            return destination
        except Exception:
            self._retry_items=items+self._retry_items
            raise

    def stop(self):
        self.stop_event.set()
        for owner,field in ((self.feed.references,'observer'),(self.feed.books,'observer'),(self.feed,'research_observer')):
            if getattr(owner,field,None)==self.capture:setattr(owner,field,None)
        for thread in (self.thread,self.aux_thread):
            if thread and thread is not threading.current_thread():thread.join(timeout=2)
        worker_done=not self.thread or not self.thread.is_alive();archived=0;deadline=time.perf_counter()+2
        if worker_done:
            try:
                while (self._retry_items or not self.pending.empty()) and time.perf_counter()<deadline:
                    archived+=self._drain_capture_batch(limit=256,max_seconds=.05,archive_only=True)
            except Exception as exc:self.fault='STOP_DRAIN_FAILED:'+type(exc).__name__
        spool=self._spool_pending()
        if worker_done:
            self.journal.quality(self.now(),'CAPTURE_STOPPED',canonical({'archived_without_decisions':archived,
                'spool_file':spool.name if spool else None,'queued':self.pending.qsize()}))

    def _worker_cycle(self):
        count=self._drain_capture_batch()
        now=self.now();oldest=self._pending_clock();self.processing_backlog=oldest is not None
        self.processing_stats['oldest_pending_age_ms']=max(0,now-oldest) if oldest is not None else 0
        if now-self.last_decision_at>=self.config['runtime']['decision_interval_ms']:
            started=time.perf_counter();self.tick(now)
            self.processing_stats['last_decision_ms']=(time.perf_counter()-started)*1000
        return count

    def _run(self):
        while not self.stop_event.is_set():
            try:self._worker_cycle()
            except Exception as exc:
                self.fault='PERSISTENCE_OR_PROCESSING_FAILED:'+type(exc).__name__
                try:self.journal.quality(self.now(),self.fault,str(exc)[:160])
                except Exception:pass
            self.stop_event.wait(.002 if self._retry_items or not self.pending.empty() else .01)

    def ingest(self,kind,payload,received,raw_text=None):
        market=None
        current=self.feed.observation()[0]
        if current:market={'market_uid':current.round_id,'condition_id':current.condition_id}
        if kind=='REFERENCE':
            channel=payload.get('channel');body=payload.get('payload') or {}
            source_kind={'price.crypto':'OFFICIAL_SPOT','price.crypto.twap':'OFFICIAL_TWAP'}.get(channel,'OTHER')
            stamp=body.get('timestamp');stamp=int(stamp) if isinstance(stamp,(int,float)) else None
            seq=payload.get('seq');seq_contract={'capability':'UNKNOWN' if seq is not None else 'NONE',
                'scope_id':channel if seq is not None else None,'stream_seq':seq if isinstance(seq,int) and seq>=0 else None,
                'check_status':'NOT_CHECKED' if seq is not None else 'NOT_APPLICABLE','dropped_count':int(payload.get('dropped') or 0)}
            event=self.journal.append(source_kind=source_kind,source_id='polybolt-chainlink',stream_id=channel or 'unknown',
                payload=payload,received_at=received,source_at=stamp,market=market,full=bool(payload.get('snapshot')),
                sequence=seq_contract,connection_epoch=self.channel_epoch['reference'],raw_text=raw_text)
            if payload.get('dropped'):
                self.reference={};self.healthy_since=None;self.journal.quality(received,'REFERENCE_DROPPED');return
            if body.get('symbol')!='btcusd' or body.get('source')!='chainlink' or (channel=='price.crypto.twap' and body.get('window_seconds')!=60):return
            if payload.get('snapshot'):self.full_resync_at[channel]=received
            for row in body.get('data',[]) if payload.get('snapshot') else [body]:
                if not isinstance(row,dict):continue
                ts=row.get('timestamp');value=row.get('full_accuracy_value',row.get('value'))
                if not isinstance(ts,(int,float)) or ts<0 or ts>received:
                    self.journal.quality(received,'PRICE_TIME_INVALID');continue
                ts=int(ts)
                try:self.journal.price(channel,ts,received,value,event['event_id'])
                except ValueError as exc:self.reference={};self.healthy_since=None;self.journal.quality(received,str(exc));continue
                name='spot' if channel=='price.crypto' else 'twap'
                if ts>self.reference.get(name,{}).get('source_at',0):
                    self.reference[name]={'source_at':ts,'received_at':received,'value':str(value),'event_id':event['event_id']}
        elif kind in ('BOOK','REST_BOOK'):
            raw=payload
            if kind=='REST_BOOK':
                # Legacy recovery response is normalized, not original bytes.
                # Archive honestly; it cannot establish the raw-feed contract.
                event=self.journal.append(source_kind='ORDER_BOOK',source_id='legacy-normalized-rest',stream_id=str(payload.get('token_id','unknown')),
                    payload=raw,received_at=received,source_at=int(payload.get('source_ms') or 0),market=market,full=True)
                return
            source=raw.get('timestamp');source=int(float(source)) if source is not None else None
            event=self.journal.append(source_kind='ORDER_BOOK',source_id='clob-websocket',stream_id='market',payload=raw,
                received_at=received,source_at=source,market=market,asset_id=raw.get('asset_id'),full=raw.get('event_type')=='book',
                connection_epoch=self.channel_epoch['book'],raw_text=raw_text)
            if source is not None and source<=received:
                try:
                    self.books.ingest(raw,source,received,event['event_id'])
                    if raw.get('event_type')=='book':self.full_resync_at[str(raw.get('asset_id'))]=received
                    self._forward_books(event)
                except ValueError as exc:
                    self.healthy_since=None;self.journal.quality(received,str(exc))
                    self._forward_books(event,include_invalid=True)
        elif kind.endswith('_HEALTH'):
            key='reference' if kind.startswith('REFERENCE') else 'book'
            self.channel_epoch[key]=uuid.uuid4().hex;self.healthy_since=None
            if key=='book':self.books=AsOfBooks()
            else:self.reference={}
            self.journal.append(source_kind='OTHER',source_id='collector-health',stream_id=key,payload=payload,received_at=received,source_at=received)
        elif kind=='CLOCK':
            self.journal.append(source_kind='CLOCK',source_id='ntp-read-only',stream_id='clock',payload=payload,received_at=received,source_at=received)
        elif kind=='RESULT':
            self._record_result(payload,received)
        else:
            self.journal.append(source_kind='MARKET_METADATA',source_id='gamma-read-only',stream_id='metadata',payload=payload,
                received_at=received,market=market,raw_text=raw_text)

    def _contract(self,market,now):
        file=self.path/'verified-contracts'/(market.condition_id+'.json')
        if file.exists():
            c=json.loads(file.read_text());validate_record(c,definition='MarketContractSnapshot')
            if c['market']['condition_id']!=market.condition_id or c['window_open_at']!=int(market.started_at*1000):raise ValueError('CONTRACT_IDENTITY_MISMATCH')
        else:c=self.markets.get(market.condition_id) or unknown_contract(market,now,self.code_sha)
        self.journal.store_contract(c);self.markets[market.condition_id]=c
        if c['contract_sha256'] not in self._contract_event_hashes:
            event=self.journal.append(source_kind='MARKET_METADATA',source_id='hengzhun-contract-snapshot',stream_id=market.condition_id,
                payload=c,received_at=now,source_at=now,market=c['market'],full=True)
            self._contract_event_hashes[c['contract_sha256']]=event['payload_sha256']
        return c

    def _prior(self,c,now):
        spot=self.reference.get('spot')
        if not spot:raise ModelUnavailable('PRICE_MISSING')
        if c['baseline']['value'] is None:raise ModelUnavailable('BASELINE_UNVERIFIED')
        obs=self.journal.prices('price.crypto',now-660000,now,now)
        grid_end=spot['source_at'];grid_start=grid_end//5000*5000-600000
        grid=causal_grid(obs,grid_start,grid_end)
        vol=estimate_volatility(grid,spot['source_at'],float(spot['value']),self.config)
        if vol.shock_cooldown_until_ms and now<vol.shock_cooldown_until_ms:raise ModelUnavailable('SHOCK_COOLDOWN')
        end=c['window_close_at'];t_obs=spot['source_at'];known=0.
        if end-t_obs<60000:known=integrate_spot(obs,end-60000,t_obs,now,max_gap_ms=self.config['data']['integral_max_gap_ms'])
        self._update_proxy(obs,now,c)
        with self.journal.lock:
            records=[ProxyErrorObservation(x['source_at'],x['available_at'],x['error_bps']) for x in self.journal.conn.execute('SELECT * FROM proxy_samples WHERE source_at>?',(now-86400000,))]
        proxy=proxy_error_stats(records,now,self.config)
        p=predict_terminal_twap(float(spot['value']),float(c['baseline']['value']),end,t_obs,now,vol.sigma_squared,known,proxy)
        prior={'spot':spot['value'],'baseline':c['baseline']['value'],'known_integral_usd_seconds':str(known),
            'known_seconds':max(0.,60-p.tau_model_seconds),'mean_unadjusted_usd':str(p.mean-float(spot['value'])*proxy.bias_bps/10000),
            'mean_adjusted_usd':str(p.mean),'variance_rate_usd2_per_second':str(vol.sigma_squared),
            'process_variance_usd2':str(p.process_variance),'total_variance_usd2':str(p.total_variance),
            'proxy_bias_bps':proxy.bias_bps,'proxy_sd_bps':proxy.sd_bps,'proxy_sample_count':proxy.sample_count,
            'proxy_p95_abs_error_bps':proxy.absolute_error_p95_bps,'proxy_status':'READY' if proxy.calibrated else 'WARMUP_FLOOR',
            'fast_return_count':vol.fast_count,'slow_return_count':vol.slow_count,'volatility_span_seconds':vol.history_span_ms/1000,
            'p_raw_up':p.p_raw_up,'p_raw_down':p.p_raw_down}
        return prior,proxy

    def _update_proxy(self,obs,now,contract):
        if contract['verification_status']!='VERIFIED':return
        twap=self.reference.get('twap');spot=self.reference.get('spot')
        if not twap or not spot or twap['source_at']-self.proxy_last<60000:return
        stamp=twap['source_at']
        try:
            known=integrate_spot(obs,stamp-60000,stamp,now,max_gap_ms=2000)
            same=[x for x in obs if x.source_at_ms<=stamp and x.received_at_ms<=now]
            anchor=same[-1]
            if stamp-anchor.source_at_ms>1500:return
            error=(float(twap['value'])-known/60)/anchor.value*10000
            with self.journal.transaction():
                last=self.journal.conn.execute('SELECT max(source_at) FROM proxy_samples').fetchone()[0]
                if last and stamp-last<60000:return
                self.journal.conn.execute('INSERT OR IGNORE INTO proxy_samples VALUES(?,?,?,?)',(stamp,now,error,canonical({'official':twap,'local_proxy':known/60})))
            self.proxy_last=stamp
        except (ValueError,IndexError):pass

    def _forward_books(self,event,include_invalid=False):
        if not self.economic:return
        derived_events=[]
        for asset in tuple(self.books.views):
            view,origin=self.books.asof(asset,event['received_at'])
            if include_invalid and self.books.views[asset] and self.books.views[asset][-1][1]==event['event_id']:
                latest=self.books.views[asset][-1][0]
                if latest.get('invalid'):
                    view={'is_complete':False,'source_at':latest['source_at'],'received_at':latest['received_at'],
                          'snapshot_sha256':digest([event['event_id'],asset,'INVALID'])};origin=event['event_id']
            if not view or origin!=event['event_id']:continue
            contract=next((c for c in self.markets.values() if asset in (c['up_asset_id'],c['down_asset_id'])),None)
            if not contract:continue
            derived={**event,'event_id':digest([event['event_id'],asset,view['snapshot_sha256'],contract['contract_sha256']]),
                'market':contract['market'],'asset_id':asset,'is_full_snapshot':view.get('is_complete',False),
                'boot_id':event['boot_id']+':book-view:'+asset,
                'payload':{'book':view,'contract_sha256':contract['contract_sha256'],
                           'origin_event_id':event['event_id'],'origin_payload_sha256':event['payload_sha256']}}
            derived_events.append(derived)
        if derived_events:self._venue_events(derived_events)

    def _venue_event(self,event):
        self._venue_events([event])

    def _venue_events(self,events):
        if not self.economic or not events:return
        if self._deferred_venue_events is not None:
            self._deferred_venue_events.extend(events);return
        if self.mode=='PAPER':
            result=self.venue.ingest_observed_events(events)
            if result['late_arrival_conflict']:
                self.fault='RECEIVE_ORDER_UNCERTAIN';self.recovery_fault_at=self.now();self.recovery_healthy_since=None
                self.journal.quality(self.now(),self.fault,canonical(result))
        else:self.venue.on_events(events)
        if self.shadow:
            try:
                if self.mode=='PAPER':
                    result=self.shadow.venue.ingest_observed_events(events)
                    if result['late_arrival_conflict']:self.shadow.error='SHADOW_RECEIVE_ORDER_UNCERTAIN'
                else:self.shadow.on_events(events)
            except Exception as exc:self.shadow.error='SHADOW_EVENT_FAILED:'+type(exc).__name__

    def _recover_transient_data(self,c,now,base_reasons):
        transient={'RECEIVE_ORDER_UNCERTAIN','QUEUE_OVERFLOW','CAPTURE_FAILED'}
        if self.fault not in transient or self.recovery_fault_at is None:return
        required=('price.crypto','price.crypto.twap',c['up_asset_id'],c['down_asset_id'])
        full=all(self.full_resync_at.get(key,-1)>=self.recovery_fault_at for key in required)
        if base_reasons or not full or not self.pending.empty():self.recovery_healthy_since=None;return
        if self.recovery_healthy_since is None:self.recovery_healthy_since=now
        if now-self.recovery_healthy_since<self.config['data']['recovery_healthy_seconds']*1000:return
        if not self.economic:
            self.journal.quality(now,'DATA_HEALTH_RECOVERED',self.fault);self.fault=None;self.recovery_fault_at=None
            self.healthy_since=self.recovery_healthy_since;return
        cash=sum((Decimal(delta['delta']) for event in self.ledger.events('HENGZHUN') for delta in event['deltas'] if delta['account']=='CASH_CONFIRMED'),Decimal(0))
        reconciled=self.ledger.reconcile_cash('HENGZHUN',str(cash),now)
        if self.ledger.recover_data_health('HENGZHUN',now,self.recovery_healthy_since,full_resync=full,wallet_reconciled=reconciled):
            self.journal.quality(now,'DATA_HEALTH_RECOVERED',self.fault);self.fault=None;self.recovery_fault_at=None
            self.healthy_since=self.recovery_healthy_since

    def _advance(self,now):
        if not self.economic:return
        self.venue.advance(max(now,self.venue.clock_ms))
        if self.shadow:
            try:self.shadow.advance(max(now,self.shadow.venue.clock_ms))
            except Exception as exc:self.shadow.error='SHADOW_ADVANCE_FAILED:'+type(exc).__name__
        self._mark_nav(now)

    def _mark_nav(self,now):
        liquidation=Decimal(0);complete=True
        for order in self.ledger.intents('HENGZHUN'):
            if order['recognized_at'] is not None:continue
            fills=[f for f in self.ledger.fills(order['intent_id']) if f['state'] not in ('FAILED_FINAL',)]
            if not fills:continue
            if any(f['state']!='CONFIRMED' for f in fills):complete=False;continue
            qty=sum((Decimal(f['net_shares']) for f in fills),Decimal(0))
            c=self.markets.get(order['condition_id'])
            book,_=self.books.asof(order['plan']['asset_id'],now)
            if (not c or c['verification_status']!='VERIFIED' or not book or now>=c['window_close_at']
                    or now-book['source_at']>self.config['data']['book_source_max_age_ms']
                    or now-book['received_at']>self.config['data']['book_receive_max_age_ms']):complete=False;continue
            remaining=qty
            for level in book['bids']:
                take=min(remaining,Decimal(level['quantity_shares']))
                if take<=0:continue
                price=Decimal(level['price'])
                fee=fee_adapter(c,order['plan']['side'],str(price),str(take),worst_case=True)
                cost=Decimal(fee['fee_cash'])+Decimal(fee['fee_shares'])*price
                liquidation+=max(Decimal(0),take*price-cost);remaining-=take
                if remaining<=0:break
            if remaining>0:complete=False
        self.ledger.mark_equity('HENGZHUN',str(liquidation) if complete else None,now,complete=complete,quality_valid=complete)

    def _books_at(self,c,now):
        return {side:self.books.asof(asset,now)[0] for side,asset in (('Up',c['up_asset_id']),('Down',c['down_asset_id']))}

    def _input_hashes(self,c,now):
        hashes={self._contract_event_hashes[c['contract_sha256']]}
        # The bounded manifest chain resolves every observed payload without
        # rescanning/repeating the previous 24 hours on each 250 ms decision.
        manifest=self.journal.provenance(now)
        if manifest:hashes.add(manifest['payload_sha256'])
        return sorted(hashes)

    @staticmethod
    def _rule_hash(c):
        # Rule compatibility excludes each period's price, clock and asset IDs.
        return digest({'version':'hengzhun-rule-projection-v1','rules':c['rules'],
                       'baseline_feed':c['baseline']['resolution_feed_id'],
                       'baseline_boundary':c['baseline']['boundary_rule_id']})

    def _fitted(self,c,books,prior,now):
        mids=[(float(books[s]['bids'][0]['price'])+float(books[s]['asks'][0]['price']))/2 for s in ('Up','Down')]
        market_p=mids[0]/sum(mids);left=(c['window_close_at']-now)/1000
        result=predict_calibrated(self.model,prior['p_raw_up'],market_p,left,now,c['rules']['rule_version'],self._rule_hash(c))
        features=raw_features(prior['p_raw_up'],market_p,left)
        transform=self.model['fit']['transform']
        fitted={'model_artifact_sha256':digest(self.model),'model_version':result['model_version'],
            'feature_order':list(FEATURE_ORDER),'features':list(features),
            'standardized_features':[(x-mean)/scale for x,mean,scale in zip(features,transform['mean'],transform['scale'])],
            'p_fit_up':result['p_fit_up'],'p10_up':result['bootstrap_p10_up'],'p90_up':result['bootstrap_p90_up'],
            'guard_up':result['guard_up'],'guard_down':result['guard_down'],
            'bootstrap_success_count':result['bootstrap_successful_count'],'inference_success_count':result['bootstrap_successful_count'],
            'model_available_at':self.model['release']['available_at'],'model_expires_at':self.model['release']['expires_at']}
        return validate_record(fitted,definition='FittedPrediction')

    def _candidate(self,c,side,book,guard,basis):
        p=plan_side(c,side,book,guard,self.config)
        return {'side':side.upper(),'asset_id':c['up_asset_id'] if side=='Up' else c['down_asset_id'],
            'best_bid':book['bids'][0]['price'],'best_ask':book['asks'][0]['price'],'limit_price':p['limit_price'],
            'quantity_shares':p['gross_qty'],'probability_basis':basis,'probability_used':guard,
            'cost_quote':cost_quote(c,side,p['limit_price'],p['gross_qty']),
            'net_edge':float(p['guard_edge']),'conservative_roi':float(p['guard_roi']),
            'passes_signal_gates':True,'rejection_reasons':[]}

    def _entry_plan(self,frame,c,chosen):
        return {'wallet_id':'HENGZHUN','strategy_id':STRATEGY_ID,'market':c['market'],'side':chosen['side'],
            'asset_id':chosen['asset_id'],'collateral_asset_id':c['collateral_asset_id'],
            'limit_price':chosen['limit_price'],'quantity_shares':chosen['quantity_shares'],
            'budget_all_in':self.config['entry']['budget_usd'],'cost_quote':chosen['cost_quote'],
            'contract_sha256':c['contract_sha256'],'config_sha256':self.config_sha,
            'model_artifact_sha256':frame['fitted']['model_artifact_sha256'],'decision_frame_id':frame['frame_id'],
            'calibration_status':'CALIBRATED','window_close_at':c['window_close_at']}

    def _pre_send_recheck(self,plan,now):
        current=self.feed.observation()[0]
        if current is None or current.condition_id!=plan['market']['condition_id']:return {'allow':False}
        c=self._contract(current,now);books=self._books_at(c,now)
        reasons=quality_reasons(self.reference,books,now,self.config,self.clock.snapshot().get('offset_bound_ms'))
        if self.replay_baseline:
            if reasons or self.fault or c['contract_sha256']!=plan['contract_sha256'] or config_hash(self.config)!=plan['config_sha256']:return {'allow':False}
            frame=deepcopy(self.last_frame or {});frame.update(decision_at=now,tau_wall_seconds=(c['window_close_at']-now)/1000,up_book=books['Up'],down_book=books['Down'])
            try:
                frame['prior'],_=self._prior(c,now)
            except ValueError:frame['prior']=None
            proposed=self.baseline_decider(self,frame,now) if self.baseline_decider else None
            if not proposed or proposed['side']!=plan['side']:return {'allow':False}
            try:
                side='Up' if plan['side']=='UP' else 'Down';book=books[side]
                ask=Decimal(book['asks'][0]['price']);limit=Decimal(plan['limit_price'])
                if not ask<=limit<=min(Decimal(self.config['entry']['hard_limit_price_cap']),ask+Decimal(c['trading']['tick_size'])*self.config['entry']['max_slippage_ticks']):return {'allow':False}
                quote=cost_quote(c,side,plan['limit_price'],plan['quantity_shares']);unit=Decimal(quote['all_in_cost_per_net_share'])
                probability=Decimal(str(proposed['probability_used']))
                if not probability.is_finite() or not 0<=probability<=1:return {'allow':False}
                if Decimal(quote['all_in_cash'])>Decimal(plan['budget_all_in']) or probability-unit<Decimal(self.config['entry']['minimum_guard_edge']) or probability/unit-1<Decimal(self.config['entry']['minimum_guard_roi']):return {'allow':False}
            except (ValueError,KeyError):return {'allow':False}
            return {**{k:plan[k] for k in ('side','limit_price','quantity_shares','contract_sha256','config_sha256','model_artifact_sha256')},'allow':True}
        if (reasons or self.fault or c['contract_sha256']!=plan['contract_sha256']
                or digest(self.model)!=plan['model_artifact_sha256'] or config_hash(self.config)!=plan['config_sha256']):return {'allow':False}
        try:
            prior,proxy=self._prior(c,now)
            if not proxy.calibrated:return {'allow':False}
            fitted=self._fitted(c,books,prior,now)
            selected,_=choose_entry(c,books,{'Up':fitted['guard_up'],'Down':fitted['guard_down']},self.config)
            if not selected or selected['side'].upper()!=plan['side']:return {'allow':False}
            side='Up' if plan['side']=='UP' else 'Down';book=books[side]
            limit=Decimal(plan['limit_price']);ask=Decimal(book['asks'][0]['price'])
            if not ask<=limit<=min(Decimal(self.config['entry']['hard_limit_price_cap']),ask+Decimal(c['trading']['tick_size'])*self.config['entry']['max_slippage_ticks']):return {'allow':False}
            quote=cost_quote(c,side,plan['limit_price'],plan['quantity_shares'])
            cost=Decimal(quote['all_in_cost_per_net_share']);guard=Decimal(str(fitted['guard_up' if side=='Up' else 'guard_down']))
            if Decimal(quote['all_in_cash'])>Decimal(plan['budget_all_in']) or guard-cost<Decimal(self.config['entry']['minimum_guard_edge']) or guard/cost-1<Decimal(self.config['entry']['minimum_guard_roi']):return {'allow':False}
        except (ValueError,KeyError):return {'allow':False}
        return {**{k:plan[k] for k in ('side','limit_price','quantity_shares','contract_sha256','config_sha256','model_artifact_sha256')},'allow':True}

    def tick(self,now):
        self.last_heartbeat=now
        oldest=self._pending_clock() if self.processing_backlog else None
        advance_at=min(now,max(0,oldest-1)) if oldest is not None else now
        self._advance(advance_at)  # Keep actual receipt watermark while capture is backlogged.
        if now-self.last_decision_at<self.config['runtime']['decision_interval_ms']:return
        self.last_decision_at=now
        market=self.feed.observation()[0]
        if market is None:return
        c=self._contract(market,now);self.contract=c
        if self._last_model_market!=c['market']['market_uid']:
            candidate=json.loads(self.model_path.read_text());validate_record(candidate,'model_artifact.schema.json')
            self.model=candidate;self._last_model_market=c['market']['market_uid']
        left=(c['window_close_at']-now)/1000;books=self._books_at(c,now)
        clock=self.clock.snapshot();reasons=quality_reasons(self.reference,books,now,self.config,clock.get('offset_bound_ms'))
        if self.processing_backlog:reasons.append('PROCESSING_BACKLOG')
        self._recover_transient_data(c,now,reasons)
        if self.fault:reasons.append(self.fault.split(':')[0])
        if c['verification_status']!='VERIFIED':reasons.append('CONTRACT_UNVERIFIED')
        if c['captured_at']>now:reasons.append('CONTRACT_FUTURE')
        if not reasons:
            if self.healthy_since is None:self.healthy_since=now
            if now-self.healthy_since<self.config['data']['recovery_healthy_seconds']*1000:reasons.append('RECOVERY_WARMUP')
        else:self.healthy_since=None
        prior=None;proxy=None;fitted=None
        try:
            if self.processing_backlog:raise ModelUnavailable('PROCESSING_BACKLOG')
            prior,proxy=self._prior(c,now);validate_record(prior,definition='PriorPrediction')
        except (ValueError,KeyError) as exc:reasons.append(getattr(exc,'reason_code',str(exc)).split(':')[0]);prior=None
        decision_reasons=[];calibration_status='UNCALIBRATED';candidates=[];entry=None
        if self.model.get('artifact_status')=='UNFITTED':decision_reasons.append('MODEL_UNFITTED')
        elif prior and all(books.values()) and not reasons:
            try:fitted=self._fitted(c,books,prior,now);calibration_status='CALIBRATED'
            except ValueError as exc:decision_reasons.append(getattr(exc,'reason_code',str(exc)).split(':')[0])
        if proxy and not proxy.calibrated:decision_reasons.append(proxy.reason_code or 'PROXY_UNCALIBRATED')
        due=[]
        for known in list(self.markets.values()):
            for target in self.config['calibration']['landmarks_seconds']:
                when=known['window_close_at']-target*1000
                if now>=when:
                    with self.journal.lock:
                        exists=self.journal.conn.execute('SELECT 1 FROM landmarks WHERE condition_id=? AND rule_version=? AND target=?',
                            (known['market']['condition_id'],known['rules']['rule_version'],target)).fetchone()
                    if not exists:due.append((known,target,when))
        reference_inputs={name:{key:value for key,value in self.reference.get(name,{}).items() if key!='event_id'} for name in ('spot','twap')}
        signature=digest({'market':c['contract_sha256'],'spot':reference_inputs['spot'],'twap':reference_inputs['twap'],
            'books':{s:b['snapshot_sha256'] if b else None for s,b in books.items()},'reasons':reasons,'model':digest(self.model),
            'baseline_input':digest(self.baseline_inputs) if self.replay_baseline and self.baseline_inputs else None,
            'entry_window':self.config['entry']['submit_remaining_min_seconds']<=left<=self.config['entry']['submit_remaining_max_seconds']})
        if signature==self.last_input and not due:return
        self.last_input=signature
        input_hashes=self._input_hashes(c,now)
        frame_id=digest([self.run['run_id'],now,signature]) if self.mode=='REPLAY' else uuid.uuid4().hex;last=self.journal.last_event or {'ingest_seq':0,'boot_id':self.journal.boot_id}
        frame={'record_type':'FeatureFrame','schema_version':'1.0.0','frame_id':frame_id,'decision_id':frame_id,
            'identity':{'economic_space':self.namespace,'wallet_id':'HENGZHUN','strategy_id':self.strategy_id},
            'market':c['market'],'contract_sha256':c['contract_sha256'],'config_sha256':self.config_sha,
            'feature_code_sha256':self.code_sha,'input_event_hashes':input_hashes,
            'decision_at':now,'t_obs':self.reference.get('spot',{}).get('source_at'),'tau_wall_seconds':left,
            'tau_model_seconds':(c['window_close_at']-self.reference['spot']['source_at'])/1000 if 'spot'in self.reference else None,
            'ingest_cutoff':{'boot_id':last['boot_id'],'ingest_seq':last['ingest_seq']},'landmark_target_seconds':None,
            'landmark_status':'NOT_A_LANDMARK','data_status':'BLOCKED' if reasons else 'READY','data_reasons':list(dict.fromkeys(reasons)),
            'calibration_status':calibration_status,'up_book':books['Up'],'down_book':books['Down'],'prior':prior,'fitted':fitted,
            'candidates':[],'decision':'RECORD_ONLY' if self.mode=='RECORD' else 'BLOCKED','intent_id':None,'decision_reasons':[]}
        current_landmarks=[(target,when) for known,target,when in due if known['market']==c['market']]
        if current_landmarks:
            target,when=max(current_landmarks,key=lambda item:item[1])
            present=now-when<=self.config['calibration']['landmark_max_lateness_ms'] and not reasons and prior is not None
            frame['landmark_target_seconds']=target
            frame['landmark_status']='PRESENT' if present else 'MISSING_OR_TOO_LATE'
        if self.mode=='RECORD':decision_reasons.insert(0,'RECORD_ONLY')
        if prior and not reasons:
            guards={'Up':fitted['guard_up'],'Down':fitted['guard_down']} if fitted else {'Up':prior['p_raw_up'],'Down':prior['p_raw_down']}
            for side in ('Up','Down'):
                try:candidates.append(self._candidate(c,side,books[side],guards[side],'FITTED_CONSERVATIVE' if fitted else 'PRIOR_UNCALIBRATED'))
                except ValueError as exc:decision_reasons.append(side.upper()+':'+str(exc))
            frame['candidates']=candidates
            if self.economic and not self.replay_baseline and fitted and proxy.calibrated:
                if not self.config['entry']['submit_remaining_min_seconds']<=left<=self.config['entry']['submit_remaining_max_seconds']:decision_reasons.append('SUBMIT_TIME_WINDOW')
                elif c['window_close_at']-now-effective_delay(self.namespace['scenario_id'],c)['effective_total_delay_ms']<self.config['entry']['expected_arrival_remaining_min_seconds']*1000:decision_reasons.append('EXPECTED_ARRIVAL_TOO_LATE')
                elif candidates:
                    candidates.sort(key=lambda item:Decimal(item['cost_quote']['net_shares'])*Decimal(str(item['net_edge'])),reverse=True)
                    if len(candidates)==2 and abs(Decimal(candidates[0]['cost_quote']['net_shares'])*Decimal(str(candidates[0]['net_edge']))-Decimal(candidates[1]['cost_quote']['net_shares'])*Decimal(str(candidates[1]['net_edge'])))<=Decimal(self.config['entry']['value_tie_tolerance_usd']):decision_reasons.append('VALUE_TIE')
                    else:
                        entry=self._entry_plan(frame,c,candidates[0]);risk=self.ledger.guard_reasons(entry,now)
                        decision_reasons.extend(risk)
                        if risk:frame['decision']='QUALIFIED_NO_INTENT';entry=None
                else:frame['decision']='NO_SIGNAL'
        if self.replay_baseline and self.baseline_decider:
            baseline_quality=quality_reasons(self.reference,books,now,self.config,clock.get('offset_bound_ms'))
            if c['verification_status']!='VERIFIED':baseline_quality.append('CONTRACT_UNVERIFIED')
            if self.fault:baseline_quality.append(self.fault.split(':')[0])
            if self.healthy_since is None or now-self.healthy_since<self.config['data']['recovery_healthy_seconds']*1000:baseline_quality.append('RECOVERY_WARMUP')
            if not self.config['entry']['submit_remaining_min_seconds']<=left<=self.config['entry']['submit_remaining_max_seconds']:baseline_quality.append('SUBMIT_TIME_WINDOW')
            if c['window_close_at']-now-effective_delay(self.namespace['scenario_id'],c)['effective_total_delay_ms']<self.config['entry']['expected_arrival_remaining_min_seconds']*1000:baseline_quality.append('EXPECTED_ARRIVAL_TOO_LATE')
            if not baseline_quality:
                entry=self.baseline_decider(self,deepcopy(frame),now)
                if entry:
                    entry=deepcopy(entry);risk=self.ledger.guard_reasons(entry,now);decision_reasons.extend(risk)
                    if risk:frame['decision']='QUALIFIED_NO_INTENT';entry=None
            decision_reasons.extend(baseline_quality)
            decision_reasons.append('REGISTERED_REPLAY_BASELINE')
        if entry and ((self.trade_start_at is not None and now<self.trade_start_at) or (self.evaluation_end_at is not None and now>=self.evaluation_end_at)):
            decision_reasons.append('OUTSIDE_EVALUATION_WINDOW');entry=None;frame['decision']='QUALIFIED_NO_INTENT'
        frame['decision_reasons']=list(dict.fromkeys(decision_reasons+reasons))
        if entry:
            # Freeze the actual decision only after the wallet transaction accepts
            # the unique intent. Failure to persist a frame rolls back reservation.
            with self.ledger.transaction():
                reservation=self.ledger.reserve(entry,now)
                frame.update(intent_id=reservation['intent_id'],decision='INTENT_CREATED')
                self.journal.feature(frame)
        else:self.journal.feature(frame)
        self.last_frame=deepcopy(frame)
        if self.shadow and not (self.trade_start_at is not None and now<self.trade_start_at) and not (self.evaluation_end_at is not None and now>=self.evaluation_end_at):
            try:
                shadow_at=max(now,self.now())
                self.shadow.consider(frame,c,shadow_at,clock_bound_ms=self.clock.snapshot().get('offset_bound_ms'),pre_send_recheck=self._pre_send_recheck)
            except Exception as exc:self.shadow.error='SHADOW_SIGNAL_FAILED:'+type(exc).__name__
        if prior and calibration_status=='UNCALIBRATED':
            with self.journal.transaction():
                self.journal.conn.execute('INSERT INTO prior_shadow_frames VALUES(?,?,?)',(frame_id,now,canonical({
                    'channel':'SHADOW_PRIOR','calibration_status':'UNCALIBRATED','account_return_valid':False,
                    'execution_evidence':'ASSUMED','frame_id':frame_id,'prior':prior,'candidates':candidates,'reasons':frame['decision_reasons']})))
        for known,target,when in due:
            eligible=known['market']==c['market'] and now-when<=self.config['calibration']['landmark_max_lateness_ms'] and not reasons and prior is not None
            self.journal.landmark(known['market']['condition_id'],known['rules']['rule_version'],target,now,'PRESENT' if eligible else 'MISSING_OR_TOO_LATE',frame_id if eligible else None)
        if entry:
            # No post-send score rechecks: after SUBMITTING only venue lifecycle acts.
            send_at=max(now,self.now());recheck=self._pre_send_recheck(entry,send_at)
            self.venue.submit(frame['intent_id'],c,send_at,recheck=recheck,clock_bound_ms=self.clock.snapshot().get('offset_bound_ms'))

    def _record_result(self,payload,received):
        if payload.get('record_type')=='OfficialResult':
            result=deepcopy(payload);validate_record(result,definition='OfficialResult')
            condition=result['market']['condition_id'];contract=self.markets.get(condition)
            if not contract or contract['contract_sha256']!=result['contract_sha256']:raise ValueError('RESULT_CONTRACT_MISMATCH')
            if result['received_at']!=received:raise ValueError('RESULT_RECEIPT_MISMATCH')
            raw_event=self.journal.append(source_kind='OFFICIAL_RESULT',source_id='normalized-official-result',stream_id=condition,
                payload={'result':result},received_at=received,source_at=result['source_at'],market=result['market'])
        else:
            condition=payload['condition_id'];contract=self.markets.get(condition);raw=payload['result']
            raw_event=self.journal.append(source_kind='OFFICIAL_RESULT',source_id='existing-strict-twap-parser',stream_id=condition,
                payload=payload,received_at=received,source_at=None,market=contract['market'] if contract else None)
            if not contract or contract['verification_status']!='VERIFIED' or raw.get('outcome') not in ('Up','Down'):return
            if raw.get('target_price') is None or Decimal(str(raw['target_price']))!=Decimal(contract['baseline']['value']):
                self.journal.quality(received,'RESULT_BASELINE_MISMATCH');return
            label=1 if raw['outcome']=='Up' else 0;result_id=digest({'condition':condition,'result':raw})
            result={'record_type':'OfficialResult','schema_version':'1.0.0','result_id':result_id,'market':contract['market'],
                'contract_sha256':contract['contract_sha256'],'revision':1,'supersedes_result_id':None,'status':'VERIFIED_FINAL',
                'source_at':None,'received_at':received,'verified_at':received,'label_available_at':received,
                'official_final_verified':True,'outcome':'UP' if label else 'DOWN','payouts':{'up':str(label),'down':str(1-label)},
                'classification_label_up':label,'baseline_value':str(raw['target_price']),
                'final_twap_value':str(raw['final_price']) if raw.get('final_price') is not None else None,
                'resolution_feed_id':contract['rules']['resolution_feed_id'],'verification_rule_id':contract['rules']['result_verification_rule_id'],
                'evidence':[{'source_label':'strict TWAP parser','source_locator':'local:raw_events/'+raw_event['event_id'],
                    'raw_payload_sha256':raw_event['payload_sha256'],'observed_at':received,
                    'verification_method':'已核验合同下官方最终结果解析','verified_at':received}],'reason_codes':[]}
            validate_record(result,definition='OfficialResult')
        with self.journal.transaction():
            prior=self.journal.conn.execute('SELECT json FROM official_results WHERE id=?',(result['result_id'],)).fetchone()
            if prior:
                previous=json.loads(prior[0])
                # Repeated read is not a new label availability timestamp.
                result=previous
            else:self.journal.conn.execute('INSERT INTO official_results VALUES(?,?,?,?)',
                (result['result_id'],condition,result['label_available_at'],canonical(result)))
        if prior:return
        if self.economic:
            self._venue_event({**raw_event,'payload':{'result':result,'origin_payload_sha256':raw_event['payload_sha256']}})

    def _initialize_result_polling(self):
        with self.journal.transaction():
            self.journal.conn.execute('CREATE TABLE IF NOT EXISTS result_poll_state(condition_id TEXT PRIMARY KEY,last_polled_at INTEGER,next_poll_at INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,completion_status TEXT,completion_contract_sha256 TEXT,result_sha256 TEXT,completed_at INTEGER,last_error TEXT)')
            self.journal.conn.execute('CREATE INDEX IF NOT EXISTS raw_events_result_kind ON raw_events(kind,seq)')
            self.journal.conn.execute("INSERT OR IGNORE INTO capture_meta VALUES('result_poll_scan_seq','0')")
            for row in self.journal.conn.execute('SELECT condition_id,json FROM official_results').fetchall():
                result=json.loads(row['json'])
                if result.get('official_final_verified') is True and result.get('status')=='VERIFIED_FINAL':
                    self._complete_result_poll(row['condition_id'],'VERIFIED_FINAL',result['contract_sha256'],digest(result),result['label_available_at'])

    def _complete_result_poll(self,condition,status,contract_sha,result_sha,at):
        self.journal.conn.execute('INSERT OR IGNORE INTO result_poll_state(condition_id) VALUES(?)',(condition,))
        previous=self.journal.conn.execute('SELECT completion_status FROM result_poll_state WHERE condition_id=?',(condition,)).fetchone()
        if previous['completion_status']=='VERIFIED_FINAL' and status!='VERIFIED_FINAL':return
        self.journal.conn.execute('UPDATE result_poll_state SET completion_status=?,completion_contract_sha256=?,result_sha256=?,completed_at=?,last_error=NULL WHERE condition_id=?',
            (status,contract_sha,result_sha,at,condition))

    def _refresh_result_completions(self):
        # Incremental indexed scan, including raw finals archived during shutdown.
        # Completion is recorded only after the captured result became durable.
        with self.journal.transaction():
            last=int(self.journal.conn.execute("SELECT value FROM capture_meta WHERE key='result_poll_scan_seq'").fetchone()[0])
            rows=self.journal.conn.execute("SELECT seq,json FROM raw_events WHERE kind='OFFICIAL_RESULT' AND seq>? ORDER BY seq LIMIT 128",(last,)).fetchall()
            for row in rows:
                event=json.loads(row['json']);payload=event['payload'];result=payload.get('result',payload)
                condition=payload.get('condition_id') or (result.get('market') or event.get('market') or {}).get('condition_id')
                status=None;contract_sha=None
                if result.get('record_type')=='OfficialResult':
                    if result.get('official_final_verified') is True and result.get('status')=='VERIFIED_FINAL':
                        status='VERIFIED_FINAL';contract_sha=result.get('contract_sha256')
                elif result.get('outcome') in ('Up','Down') and result.get('target_price') is not None:
                    status='RAW_FINAL_OBSERVED'
                    state=self.journal.conn.execute('SELECT last_polled_at FROM result_poll_state WHERE condition_id=?',(condition,)).fetchone() if condition else None
                    contract=self.markets.get(condition)
                    if state and state['last_polled_at'] is not None and event['received_at']>=state['last_polled_at'] and contract:
                        contract_sha=contract['contract_sha256']
                if condition and status:self._complete_result_poll(condition,status,contract_sha,digest(result),event['received_at'])
                last=row['seq']
            if rows:self.journal.conn.execute("UPDATE capture_meta SET value=? WHERE key='result_poll_scan_seq'",(str(last),))
            # Normalized official_results may have been derived from a raw final.
            for row in self.journal.conn.execute("SELECT o.condition_id,o.json FROM official_results o LEFT JOIN result_poll_state p ON p.condition_id=o.condition_id WHERE p.completion_status IS NULL OR p.completion_status!='VERIFIED_FINAL'").fetchall():
                result=json.loads(row['json'])
                if result.get('official_final_verified') is True and result.get('status')=='VERIFIED_FINAL':
                    self._complete_result_poll(row['condition_id'],'VERIFIED_FINAL',result['contract_sha256'],digest(result),result['label_available_at'])

    def _result_poll_candidates(self,now,limit=2):
        if type(limit) is not int or not 1<=limit<=2:raise ValueError('RESULT_POLL_BATCH_LIMIT')
        self._refresh_result_completions()
        pending={order['condition_id'] for order in self.ledger.intents('HENGZHUN') if order['recognized_at'] is None and order['state'] not in ('ABORTED_BEFORE_SEND','REJECTED_CONFIRMED','TERMINAL_NO_FILL') and
                 (order['send_attempts']>0 or self.ledger.fills(order['intent_id']))}
        with self.journal.lock:states={row['condition_id']:dict(row) for row in self.journal.conn.execute('SELECT * FROM result_poll_state')}
        candidates=[]
        for condition,c in list(self.markets.items()):
            state=states.get(condition,{})
            if now<c['window_close_at'] or now<state.get('next_poll_at',0):continue
            status=state.get('completion_status');same=state.get('completion_contract_sha256')==c['contract_sha256']
            if status=='VERIFIED_FINAL' and same:continue
            if status=='RAW_FINAL_OBSERVED' and condition not in pending and (same or c['verification_status']!='VERIFIED'):continue
            candidates.append((state.get('last_polled_at') if state.get('last_polled_at') is not None else -1,c['window_close_at'],condition,c))
        candidates.sort(key=lambda item:item[:3])
        # One priority request plus one fair request prevents both position stalls
        # and starvation of older untraded markets needed for model labels.
        urgent=next((item for item in candidates if item[2] in pending),None)
        selected=[urgent] if urgent else []
        selected.extend(item for item in candidates if item is not urgent)
        return [(item[2],deepcopy(item[3])) for item in selected[:limit]]

    def _mark_result_poll_attempt(self,condition,now):
        with self.journal.transaction():
            self.journal.conn.execute('INSERT OR IGNORE INTO result_poll_state(condition_id) VALUES(?)',(condition,))
            self.journal.conn.execute('UPDATE result_poll_state SET last_polled_at=?,next_poll_at=?,attempts=attempts+1 WHERE condition_id=?',(now,now+30000,condition))

    def _aux_loop(self):
        last_clock=0
        while not self.stop_event.is_set():
            now=self.now()
            if now-last_clock>=60000:
                self.clock.probe(timeout=2);self.capture('CLOCK',self.clock.snapshot(),self.now());last_clock=now
            for condition,c in self._result_poll_candidates(self.now()):
                if self.stop_event.is_set():break
                # Recheck clock work between bounded requests, never after an
                # unbounded scan of every historical market.
                if self.now()-last_clock>=60000:
                    self.clock.probe(timeout=2);self.capture('CLOCK',self.clock.snapshot(),self.now());last_clock=self.now()
                self._mark_result_poll_attempt(condition,self.now())
                try:
                    result=self.feed.polymarket.get_resolution(c['slug'])
                    if result:self.capture('RESULT',{'condition_id':condition,'result':result},self.now())
                except Exception as exc:
                    with self.journal.transaction():self.journal.conn.execute('UPDATE result_poll_state SET last_error=? WHERE condition_id=?',(type(exc).__name__,condition))
            self.stop_event.wait(1)

    def snapshot(self):
        now=self.now()
        if now-self._last_cache_at<1000 and self.snapshot_cache:return deepcopy(self.snapshot_cache)
        counts=self.journal.counts()
        with self.journal.lock:
            failures=[dict(r) for r in self.journal.conn.execute('SELECT code,count FROM condition_failures ORDER BY count DESC LIMIT 10')]
            landmarks=dict(self.journal.conn.execute('SELECT status,count(*) FROM landmarks GROUP BY status').fetchall())
            markets=self.journal.conn.execute('SELECT count(DISTINCT condition_id) FROM features').fetchone()[0]
        frame=self.last_frame or {};wallet=self.ledger.snapshot('HENGZHUN',now)
        orders=sorted(self.ledger.intents('HENGZHUN'),key=lambda item:item['payload']['created_at'],reverse=True)
        with self.ledger.lock:
            outcomes=[dict(row) for row in self.ledger.conn.execute(
                'SELECT intent_id,pnl FROM hz_outcomes WHERE space=? AND wallet_id=?',(self.ledger.space,'HENGZHUN'))]
        pnl_by_intent={row['intent_id']:row['pnl'] for row in outcomes}
        recent=[{**order,'realized_pnl':pnl_by_intent.get(order['intent_id']),
                 'fills':self.ledger.fills(order['intent_id'])} for order in orders[:100]]
        financial_stats={'intent_count':len(orders),'settled_count':len(outcomes),
            'win_rate':sum(Decimal(row['pnl'])>0 for row in outcomes)/len(outcomes) if outcomes else None}
        payload={'enabled':True,'strategy_id':STRATEGY_ID,'name':DISPLAY_NAME,'spec_version':'1.0.0','mode':self.mode,
            'live_execution_enabled':False,'running':bool(self.thread and self.thread.is_alive()),'last_heartbeat':self.last_heartbeat,
            'run_id':self.run['run_id'],'started_at':self.run['started_at'],'config_sha256':self.config_sha,'config':self.config,
            'counts':counts,'independent_markets':markets,'landmarks':landmarks,'model_status':self.model.get('artifact_status','UNFITTED'),
            'research_status':'INSUFFICIENT','execution_evidence':'ASSUMED','clock':self.clock.snapshot(),
            'signal_shadow':self.shadow.snapshot(now) if self.shadow else None,
            'prior_shadow_count':self.journal.conn.execute('SELECT count(*) FROM prior_shadow_frames').fetchone()[0],
            'reason_codes':frame.get('decision_reasons') or ['RECORD_ONLY','MODEL_UNFITTED'],
            'last_decision':frame,'failure_counts':failures,'queue_size':self.pending.qsize(),'dropped_events':self.drop_count,
            'processing':deepcopy(self.processing_stats),'processing_backlog':self.processing_backlog,
            'error':self.fault,'wallet':wallet,'recent_intents':recent,'financial_stats':financial_stats,'trade_stats':financial_stats,'contract_status':self.contract['verification_status'] if self.contract else 'UNVERIFIED',
            'adapter_blockers':['IV01_BOUNDARY_AUDIT','IV02_FEE_PRECISION_AUDIT','IV03_PROTOCOL_MAPPING_AUDIT'] if not self.contract or self.contract['verification_status']!='VERIFIED' else [],
            'note':'按设计先采集；模型和合同未通过前不进入正式 Paper。独立账本，不读取实盘资金或交易密钥。'}
        self.snapshot_cache=payload;self._last_cache_at=now;return deepcopy(payload)


def default_epochs():return {'reference':uuid.uuid4().hex,'book':uuid.uuid4().hex}
