"""衡准 V1 的确定性模拟 FAK 场所；不接收私钥或真实交易接口。"""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR, localcontext
import json
import math
from .ledger import HengzhunLedger, LedgerError, canonical, decimal, digest, epoch_ms, money, setting
from .contracts import require_verified

SCENARIOS = {'S1': (1000, '0.50'), 'S2': (2000, '0.25'), 'S3': (5000, '0.25')}


class RealVenue:
    def __init__(self, *args, **kwargs):
        raise LedgerError('LIVE_EXECUTION_DISABLED')


def effective_delay(scenario_id, contract):
    if scenario_id not in SCENARIOS:
        raise LedgerError('UNREGISTERED_SCENARIO')
    assumed = SCENARIOS[scenario_id][0]
    trading = contract['trading']
    hold = trading.get('venue_min_arrival_delay_ms')
    status = trading.get('delay_status')
    if hold is not None:
        epoch_ms(hold)
        if status != 'VERIFIED':raise LedgerError('DELAY_CONTRACT_CONFLICT')
        delay = max(assumed, hold)
    else:
        if status != 'UNKNOWN':raise LedgerError('DELAY_CONTRACT_CONFLICT')
        delay = assumed
    return {'scenario_total_delay_ms': assumed, 'venue_hold_ms': hold, 'venue_hold_status': status,
            'effective_total_delay_ms': delay, 'execution_evidence': 'ASSUMED'}


def normalized_fee(quote, contract=None, price=None, quantity=None):
    """Accept the supplied CostQuote shape or the root FeeAdapter alias shape."""
    aliases={'gross_shares':'gross_qty','net_shares':'net_qty','all_in_cash':'cash_paid',
             'fee_collection_mode':'collection_mode','cash_before_fees':'notional'}
    output={k:quote.get(k,quote.get(alias)) for k,alias in aliases.items()}
    output['fee_cash']=quote.get('fee_cash')
    output['fee_shares']=quote.get('fee_shares')
    output['fee_model_id']=quote.get('fee_model_id')
    if contract is not None:
        fee=contract['trading']['fee_contract']
        output['fee_model_id']=output['fee_model_id'] or fee.get('fee_model_id')
        output['fee_collection_mode']=output['fee_collection_mode'] or fee.get('collection_mode')
        output['cash_before_fees']=output['cash_before_fees'] if output['cash_before_fees'] is not None else money(decimal(price)*decimal(quantity))
    if any(v is None for v in output.values()):raise LedgerError('INCOMPLETE_FEE_QUOTE')
    return output


class PaperVenue:
    def __init__(self, ledger: HengzhunLedger, fee_adapter, scenario_id=None):
        self.ledger=ledger
        self.fee_adapter=fee_adapter
        self.scenario_id=scenario_id or ledger.namespace['scenario_id']
        if self.scenario_id not in SCENARIOS or self.scenario_id!=ledger.namespace['scenario_id']:
            raise LedgerError('SCENARIO_NAMESPACE_MISMATCH')
        if fee_adapter is None:raise LedgerError('FEE_ADAPTER_REQUIRED')
        if setting(ledger.config,'paper.execution_evidence','ASSUMED')!='ASSUMED':raise LedgerError('EMPIRICAL_PROFILE_UNVERIFIED')
        self.clock_ms=0
        self.connected=True
        self._restore_connection()

    def _restore_connection(self):
        with self.ledger.lock:
            for row in self.ledger.conn.execute('SELECT payload FROM hz_venue_events WHERE space=? ORDER BY rowid',(self.ledger.space,)):
                e=json.loads(row[0]);p=e.get('payload',{});p=p.get('book',p)
                if p.get('fault_type')=='DISCONNECT':self.connected=False
                elif p.get('fault_type')=='RECONNECT':self.connected=True
                self.clock_ms=max(self.clock_ms,e['received_at'])

    def _task(self, intent_id):
        with self.ledger.lock:
            r=self.ledger.conn.execute('SELECT payload FROM hz_venue_tasks WHERE intent_id=?',(intent_id,)).fetchone()
            return json.loads(r[0]) if r else None

    def _save_task(self,task):
        with self.ledger.transaction():
            self.ledger.conn.execute('INSERT OR REPLACE INTO hz_venue_tasks VALUES(?,?)',(task['intent_id'],canonical(task)))

    def submit(self,intent_id,contract,at_ms,*,recheck=None,clock_bound_ms=None,selected_delay_profile_valid=True,response_lost=False):
        at_ms=epoch_ms(at_ms)
        order=self.ledger.intent(intent_id)
        if order['state']!='RESERVED' or order['send_attempts']!=0:raise LedgerError('INTENT_ALREADY_SENT_OR_TERMINAL')
        if self.ledger.namespace['mode']=='RECORD':raise LedgerError('RECORD_NO_ECONOMIC_ORDERS')
        if contract.get('verification_status')!='VERIFIED' or contract.get('trading',{}).get('fee_contract',{}).get('verification_status')!='VERIFIED':
            self.ledger.abort_before_send(intent_id,at_ms);raise LedgerError('CONTRACT_UNVERIFIED')
        if (not isinstance(clock_bound_ms,(int,float)) or isinstance(clock_bound_ms,bool)
                or not math.isfinite(clock_bound_ms) or clock_bound_ms<0 or selected_delay_profile_valid is not True):
            self.ledger.halt(order['wallet_id'],'TIME_BOUNDS_UNKNOWN',at_ms)
            self.ledger.abort_before_send(intent_id,at_ms);raise LedgerError('TIME_BOUNDS_UNKNOWN')
        if clock_bound_ms>int(setting(self.ledger.config,'data.clock_offset_max_ms',250)):
            self.ledger.halt(order['wallet_id'],'CLOCK_OFFSET',at_ms)
            self.ledger.abort_before_send(intent_id,at_ms);raise LedgerError('CLOCK_OFFSET')
        if contract['contract_sha256']!=order['plan']['contract_sha256']:
            self.ledger.abort_before_send(intent_id,at_ms);raise LedgerError('CONTRACT_CHANGED_BEFORE_SEND')
        try:require_verified(contract)
        except Exception as exc:
            self.ledger.abort_before_send(intent_id,at_ms)
            raise LedgerError('CONTRACT_UNVERIFIED') from exc
        trading=contract['trading']
        if not trading['accepting_orders'] or trading['book_state']!='READY':
            self.ledger.abort_before_send(intent_id,at_ms);raise LedgerError('MARKET_NOT_ACCEPTING')
        delay=effective_delay(self.scenario_id,contract)
        arrival=at_ms+delay['effective_total_delay_ms']
        remaining=(contract['window_close_at']-at_ms)/1000
        if not int(setting(self.ledger.config,'entry.submit_remaining_min_seconds',30))<=remaining<=int(setting(self.ledger.config,'entry.submit_remaining_max_seconds',90)):
            self.ledger.abort_before_send(intent_id,at_ms);raise LedgerError('SUBMIT_TIME_WINDOW')
        if contract['window_close_at']-arrival<int(setting(self.ledger.config,'entry.expected_arrival_remaining_min_seconds',25))*1000:
            self.ledger.abort_before_send(intent_id,at_ms);raise LedgerError('EXPECTED_ARRIVAL_TOO_LATE')
        qty=decimal(order['plan']['quantity_shares']);step=decimal(trading['quantity_step'])
        if qty%step!=0:
            self.ledger.abort_before_send(intent_id,at_ms);raise LedgerError('QUANTITY_PRECISION')
        minimum_basis={'SHARES':qty,'PRE_FEE_COLLATERAL':qty*decimal(order['plan']['limit_price']),
                       'ALL_IN_COLLATERAL':decimal(order['plan']['cost_quote']['all_in_cash'])}
        if minimum_basis[trading['min_order_size_unit']]<decimal(trading['min_order_size']):
            self.ledger.abort_before_send(intent_id,at_ms);raise LedgerError('BELOW_MARKET_MINIMUM')
        if decimal(order['plan']['limit_price'])%decimal(trading['tick_size'])!=0:
            self.ledger.abort_before_send(intent_id,at_ms);raise LedgerError('TICK_PRECISION')
        # Persist the task before the durable send marker. Recovery of RESERVED is
        # provably unsent; recovery after SUBMITTING never invokes this path again.
        request={k:order['plan'][k] for k in ('side','asset_id','limit_price','quantity_shares','contract_sha256')}
        request.update(intent_id=intent_id,order_type='FAK',order_action='BUY')
        task={'intent_id':intent_id,'contract':contract,'submitted_at':at_ms,'arrival_at':arrival,
              'effective_delay':delay,'depth_fraction':SCENARIOS[self.scenario_id][1],
              'phase':'SCHEDULED','fill_keys':[],'confirm_at':None,'faults':[],
              'submitted_request_json':canonical(request),'submitted_request_sha256':digest(request),
              'book_model':'LOCAL_OBSERVED_ASOF','execution_evidence':'ASSUMED'}
        self._save_task(task)
        result=self.ledger.submit_once(intent_id,at_ms,arrival,recheck)
        if result['state']=='ABORTED_BEFORE_SEND':
            task['phase']='ABORTED';self._save_task(task);return result
        if response_lost or not self.connected:self.ledger.mark_unknown(intent_id,at_ms,'SUBMISSION_RESPONSE_LOST')
        else:self.ledger.acknowledge(intent_id,at_ms)
        self.clock_ms=max(self.clock_ms,at_ms)
        return self.ledger.intent(intent_id)

    def on_event(self,event):
        return self.on_events([event])

    def ingest_observed_events(self,events):
        """Live capture may be drained after a wall-clock tick; never backfill.

        Original receive clocks remain unchanged. A late book that would change
        the as-of view of an already processed arrival latches DATA_GAP, leaving
        the recorded fill/reservation untouched for reconciliation. Harmless late
        observations do not pause the wallet. Offline replay remains strict.
        """
        events=list(events)
        if not events:return {'accepted_count':0,'late':False,'late_arrival_conflict':False}
        received=epoch_ms(events[0]['received_at'])
        if any(epoch_ms(e['received_at'])!=received for e in events):raise LedgerError('RECEIVE_BATCH_CLOCK_MISMATCH')
        if received>=self.clock_ms:
            accepted=self.on_events(events)
            return {'accepted_count':len(events) if accepted else 0,'late':False,'late_arrival_conflict':False}
        current=self.clock_ms;new=[];affected={};conflicts=[]
        with self.ledger.transaction():
            tasks=[json.loads(r[0]) for r in self.ledger.conn.execute(
                'SELECT v.payload FROM hz_venue_tasks v JOIN hz_intents i ON i.intent_id=v.intent_id WHERE i.space=?',(self.ledger.space,))]
            for event in events:
                old=self.ledger.conn.execute('SELECT payload FROM hz_venue_events WHERE space=? AND event_id=?',(self.ledger.space,event['event_id'])).fetchone()
                if old:
                    if json.loads(old[0])!=event:raise LedgerError('EVENT_ID_CONFLICT')
                    continue
                for task in tasks:
                    if task['phase'] in ('SCHEDULED','ABORTED') or not task['submitted_at']<received<=task['arrival_at']<=current:continue
                    order=self.ledger.intent(task['intent_id']);source=event.get('source_at')
                    if (event.get('source_kind')!='ORDER_BOOK' or event.get('asset_id')!=order['plan']['asset_id']
                            or (event.get('market') or {}).get('condition_id')!=order['condition_id']
                            or type(source) is not int or source>task['arrival_at']):continue
                    if order['intent_id'] not in affected:
                        before=self._asof_book(order,task['arrival_at'])
                        affected[order['intent_id']]=(order,task,before['event_id'] if before else None)
                self.ledger.conn.execute('INSERT INTO hz_venue_events VALUES(?,?,?,?,?,?)',
                    (self.ledger.space,event['event_id'],received,event['boot_id'],event['ingest_seq'],canonical(event)))
                new.append(event)
            for order,task,before in affected.values():
                after=self._asof_book(order,task['arrival_at'])
                if (after['event_id'] if after else None)==before:continue
                conflicts.append(order['intent_id'])
                self.ledger.halt(order['wallet_id'],'DATA_GAP',current,{'reason':'LATE_BOOK_FOR_PROCESSED_ARRIVAL',
                    'intent_id':order['intent_id'],'arrival_at':task['arrival_at'],'original_received_at':received,
                    'processed_at':current,'before_book_event_id':before,'after_book_event_id':after['event_id'] if after else None})
            for event in new:
                payload=event.get('payload',{})
                if event.get('source_kind')=='OFFICIAL_RESULT':self.ledger.record_result(payload.get('result',payload),current)
                if payload.get('fault_type'):
                    self._fault({**event,'received_at':current})
        return {'accepted_count':len(new),'late':True,'late_arrival_conflict':bool(conflicts),'affected_intents':conflicts}

    def on_events(self,events):
        """Persist one receive-clock batch before processing timers at that clock.

        Multiple normalized asset views may share the same received millisecond.
        Advancing after only the first view can otherwise match against a partial
        batch. Persisted ingest order breaks ties without lexical boot ordering.
        """
        events=list(events)
        if not events:return False
        received=epoch_ms(events[0]['received_at'])
        if any(epoch_ms(event['received_at'])!=received for event in events):raise LedgerError('RECEIVE_BATCH_CLOCK_MISMATCH')
        new=[]
        # Persist exactly the normalized RawEvent, retaining its original payload
        # digest provided by the capture layer. This is not a remote byte hash.
        with self.ledger.transaction():
            for event in events:
                old=self.ledger.conn.execute('SELECT payload FROM hz_venue_events WHERE space=? AND event_id=?',(self.ledger.space,event['event_id'])).fetchone()
                if old:
                    if json.loads(old[0])!=event:raise LedgerError('EVENT_ID_CONFLICT')
                    continue
                if received<self.clock_ms:raise LedgerError('RECEIVE_TIME_REGRESSION')
                self.ledger.conn.execute('INSERT INTO hz_venue_events VALUES(?,?,?,?,?,?)',
                    (self.ledger.space,event['event_id'],received,event['boot_id'],event['ingest_seq'],canonical(event)))
                new.append(event)
        if not new:return False
        if received>self.clock_ms:self.advance(received-1)
        for event in new:
            payload=event.get('payload',{})
            if event.get('source_kind')=='OFFICIAL_RESULT':
                result=payload.get('result',payload)
                self.ledger.record_result(result,received)
            if payload.get('fault_type'):self._fault(event)
        self.advance(received)
        return True

    def _fault(self,event):
        p=event['payload'];kind=p['fault_type'];at=event['received_at'];intent_id=p.get('intent_id')
        if kind=='DISCONNECT':self.connected=False
        elif kind=='RECONNECT':self.connected=True
        elif kind=='WALLET_OBSERVATION':self.ledger.reconcile_cash(p['wallet_id'],p['cash_confirmed'],at)
        elif kind=='CLOCK_UNKNOWN':self.ledger.halt(p['wallet_id'],'TIME_BOUNDS_UNKNOWN',at)
        elif kind in ('ORDER_UNKNOWN','RESPONSE_LOST'):
            self.ledger.mark_unknown(intent_id,at,kind)
        elif kind in ('FILL_RETRYING','FILL_UNKNOWN','FILL_FAILED_FINAL'):
            self.ledger.fill_status(p['venue_fill_key'],kind.removeprefix('FILL_'),at,p.get('permanent_no_payment_proven',False))
        elif kind=='DELAY_ARRIVAL':
            task=self._task(intent_id)
            if not task or task['phase']!='SCHEDULED':raise LedgerError('ARRIVAL_ALREADY_MATCHED')
            task['arrival_at']=epoch_ms(p['arrival_at']);task['faults'].append(event['event_id']);self._save_task(task)
        else:raise LedgerError('UNREGISTERED_FAULT_TYPE')

    def _asof_book(self,order,arrival):
        with self.ledger.lock:
            rows=self.ledger.conn.execute('SELECT payload FROM hz_venue_events WHERE space=? AND received_at<=? ORDER BY received_at,rowid',
                                          (self.ledger.space,arrival)).fetchall()
        candidates=[]
        for row in rows:
            e=json.loads(row[0]);p=e.get('payload',{});p=p.get('book',p)
            if e.get('source_kind')!='ORDER_BOOK' or not e.get('market') or e['market']['condition_id']!=order['condition_id']:continue
            if e.get('asset_id')!=order['plan']['asset_id']:continue
            candidates.append(e)
        # A later invalidation/full-resync marker must tombstone the old book.
        # Falling back to the last valid quote would create fictitious fills.
        return max(candidates,key=lambda e:(e['received_at'],e['ingest_seq'])) if candidates else None

    def _zero_fill(self,task,at,reason):
        task.update(phase='MATCHED_ZERO',match_reason=reason,matched_at=at)
        # This is the simulated venue's own definitive no-fill result, not a GET
        # NOT_FOUND inference. A lost acknowledgement stays UNKNOWN until reconnection.
        self._save_task(task)
        if self.connected:
            self.ledger.order_terminal(task['intent_id'],at,reconciled=True)
            task['phase']='DONE';self._save_task(task)
        else:self.ledger.mark_unknown(task['intent_id'],at,'NO_FILL_RESPONSE_LOST')

    def _arrive(self,task):
        current_task=self._task(task['intent_id'])
        if current_task is None or current_task['phase']!='SCHEDULED':return
        task=current_task
        at=task['arrival_at'];order=self.ledger.intent(task['intent_id']);plan=order['plan'];contract=task['contract']
        if order['state'] in ('ABORTED_BEFORE_SEND','REJECTED_CONFIRMED'):
            task['phase']='ABORTED';self._save_task(task);return
        if at>=contract['window_close_at']:
            self._zero_fill(task,at,'MARKET_CLOSED_AT_ARRIVAL');return
        breach=contract['window_close_at']-at<int(setting(self.ledger.config,'entry.expected_arrival_remaining_min_seconds',25))*1000
        if breach:self.ledger.halt(order['wallet_id'],'ARRIVAL_GUARD_BREACH',at)
        book_event=self._asof_book(order,at)
        if not book_event or book_event['received_at']<=task['submitted_at']:
            self._zero_fill(task,at,'NO_POST_SUBMIT_BOOK');return
        source=book_event.get('source_at')
        if isinstance(source,bool) or not isinstance(source,int) or source>at:
            self._zero_fill(task,at,'BOOK_INVALID');return
        if source<task['submitted_at']:
            self._zero_fill(task,at,'NO_POST_SUBMIT_BOOK');return
        if at-book_event['source_at']>int(setting(self.ledger.config,'data.book_source_max_age_ms',1000)) or at-book_event['received_at']>int(setting(self.ledger.config,'data.book_receive_max_age_ms',1000)):
            self._zero_fill(task,at,'BOOK_STALE_AT_ARRIVAL');return
        envelope=book_event['payload'];book=envelope.get('book',envelope);asks=book.get('asks') or [];bids=book.get('bids') or []
        if book.get('is_complete',book.get('complete')) is not True or not asks or not bids:
            self._zero_fill(task,at,'BOOK_INVALID');return
        for level in asks+bids:
            if not 0<decimal(level['price'])<1 or decimal(level.get('quantity_shares',level.get('size')))<0:
                self._zero_fill(task,at,'BOOK_INVALID');return
        asks=[x for x in asks if decimal(x.get('quantity_shares',x.get('size')))>0]
        bids=[x for x in bids if decimal(x.get('quantity_shares',x.get('size')))>0]
        if not asks or not bids:
            self._zero_fill(task,at,'BOOK_INVALID');return
        if max(decimal(x['price']) for x in bids)>min(decimal(x['price']) for x in asks):
            self._zero_fill(task,at,'BOOK_INVALID');return
        if envelope.get('contract_sha256',book.get('contract_sha256'))!=plan['contract_sha256']:
            # A changed local metadata snapshot cannot cancel a request that was
            # already sent. Its actual economics need independent reconciliation.
            self.ledger.halt(order['wallet_id'],'POST_SUBMIT_CONTRACT_UNRESOLVED',at)
            self.ledger.mark_unknown(order['intent_id'],at,'POST_SUBMIT_CONTRACT_UNRESOLVED')
            task.update(phase='RECONCILE_REQUIRED',match_reason='POST_SUBMIT_CONTRACT_UNRESOLVED')
            self._save_task(task);return
        remaining=decimal(plan['quantity_shares']);limit=decimal(plan['limit_price']);cash=Decimal('0');keys=[]
        fraction=decimal(task['depth_fraction']);book_hash=book.get('snapshot_sha256',book.get('book_hash'))
        if not book_hash:raise LedgerError('BOOK_HASH_MISSING')
        # Venue event/task updates and matches are repeatable with stable fill keys.
        # Per-book consumption is persisted separately and keyed by intent fill;
        # a retry finds recorded stable fills rather than double-consuming depth.
        for index,level in enumerate(sorted(asks,key=lambda x:decimal(x['price']))):
            price=decimal(level['price'])
            if price>limit:break
            fill_key=digest([self.ledger.space,order['intent_id'],book_hash,index])
            with self.ledger.lock:
                prior=self.ledger.conn.execute('SELECT payload FROM hz_fills WHERE space=? AND venue_fill_key=?',(self.ledger.space,fill_key)).fetchone()
                consumed=self.ledger.conn.execute('SELECT quantity FROM hz_consumed WHERE space=? AND wallet_id=? AND book_hash=? AND side=? AND price=?',
                    (self.ledger.space,order['wallet_id'],book_hash,plan['side'],money(price))).fetchone()
            if prior:
                f=json.loads(prior[0]);gross=decimal(f['economics']['gross_shares']);cash+=decimal(f['economics']['all_in_cash']);remaining-=gross;keys.append(fill_key);continue
            available=max(Decimal('0'),decimal(level.get('quantity_shares',level.get('size')))*fraction-(decimal(consumed[0]) if consumed else Decimal('0')))
            unit=Decimal(1).scaleb(-contract['trading']['quantity_decimals'])
            quantity=(min(remaining,available)/unit).to_integral_value(rounding=ROUND_FLOOR)*unit
            if quantity<=0:continue
            quote=normalized_fee(self.fee_adapter(contract,plan['side'],money(price),money(quantity)),contract,money(price),money(quantity))
            if cash+decimal(quote['all_in_cash'])>decimal(plan['budget_all_in']):
                self.ledger.halt(order['wallet_id'],'FEE_BUDGET_MISMATCH',at)
                self.ledger.mark_unknown(order['intent_id'],at,'FEE_BUDGET_MISMATCH')
                task.update(phase='RECONCILE_REQUIRED',match_reason='FEE_BUDGET_MISMATCH')
                self._save_task(task);return
            self.ledger.record_match(order['intent_id'],fill_key,money(price),quote,at)
            with self.ledger.transaction():
                self.ledger.conn.execute('INSERT OR REPLACE INTO hz_consumed VALUES(?,?,?,?,?,?)',
                    (self.ledger.space,order['wallet_id'],book_hash,plan['side'],money(price),money((decimal(consumed[0]) if consumed else Decimal('0'))+quantity)))
            cash+=decimal(quote['all_in_cash']);remaining-=quantity;keys.append(fill_key)
            if remaining<=0:break
        if not keys:
            self._zero_fill(task,at,'FAK_NO_MATCH');return
        task.update(phase='MATCHED',fill_keys=keys,matched_at=at,
                    confirm_at=at+int(setting(self.ledger.config,'paper.confirm_after_fill_ms',12000)),
                    matching_book_event=book_event['event_id'],matching_book_hash=book_hash,
                    cancelled_remainder=money(remaining),arrival_guard_breach=breach)
        self._save_task(task)
        if self.connected:self.ledger.order_terminal(order['intent_id'],at,reconciled=True)
        else:self.ledger.mark_unknown(order['intent_id'],at,'MATCH_RESPONSE_LOST')

    def advance(self,at_ms):
        at_ms=epoch_ms(at_ms)
        if at_ms<self.clock_ms:raise LedgerError('CLOCK_REGRESSION')
        with localcontext() as ctx:
            ctx.prec=60
            with self.ledger.lock:
                tasks=[json.loads(r[0]) for r in self.ledger.conn.execute('SELECT v.payload FROM hz_venue_tasks v JOIN hz_intents i ON i.intent_id=v.intent_id WHERE i.space=?',(self.ledger.space,))]
            for task in sorted(tasks,key=lambda x:(x['arrival_at'],x['intent_id'])):
                if task['phase']=='SCHEDULED' and task['arrival_at']<=at_ms:
                    with self.ledger.transaction():self._arrive(task)
                task=self._task(task['intent_id'])
                if task['phase']=='MATCHED_ZERO' and self.connected:
                    self.ledger.order_terminal(task['intent_id'],at_ms,reconciled=True);task['phase']='DONE';self._save_task(task)
                if task['phase']=='MATCHED' and task['confirm_at']<=at_ms and not self.connected:
                    task['confirmation_blocked']=True;self._save_task(task)
                if task['phase']=='MATCHED' and task['confirm_at']<=at_ms and self.connected:
                    order=self.ledger.intent(task['intent_id'])
                    for key in task['fill_keys']:
                        fill=next(x for x in self.ledger.fills(task['intent_id']) if x['venue_fill_key']==key)
                        if fill['state'] in ('MATCHED','RETRYING'):
                            self.ledger.confirm_fill(key,max(task['confirm_at'],at_ms if order['state']=='UNKNOWN' or task.get('confirmation_blocked') else task['confirm_at']))
                    if all(f['state'] in ('CONFIRMED','FAILED_FINAL') for f in self.ledger.fills(task['intent_id'])):
                        self.ledger.order_terminal(task['intent_id'],at_ms,reconciled=True);task['phase']='DONE';self._save_task(task)
            self.ledger.close_windows(at_ms)
            for order in self.ledger.intents():
                if order['position_state']=='CLAIM_PENDING' and order['claim_available_at']<=at_ms and self.connected:
                    self.ledger.claim(order['intent_id'],'paper-claim:'+order['intent_id'],order['payout'],at_ms)
            self.clock_ms=at_ms
        return self.snapshot(at_ms)

    def snapshot(self,at_ms=None):
        at=self.clock_ms if at_ms is None else at_ms
        with self.ledger.lock:
            wallets=[r[0] for r in self.ledger.conn.execute('SELECT wallet_id FROM hz_wallets WHERE space=? ORDER BY wallet_id',(self.ledger.space,))]
        return {'venue':'PAPER_VENUE','economic_space':dict(self.ledger.namespace),'execution_evidence':'ASSUMED',
                'book_model':'LOCAL_OBSERVED_ASOF','connected':self.connected,'clock_ms':at,
                'wallets':[self.ledger.snapshot(wallet,at) for wallet in wallets]}
