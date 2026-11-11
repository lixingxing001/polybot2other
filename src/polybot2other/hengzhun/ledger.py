"""衡准 V1 独立事件账本。金额只用 Decimal，所有写操作在 SQLite 原子事务内。

No network clients, keys, signers, or real-wallet operations are imported here.
The economic namespace and SQL constraints, not process-local locks, define ownership.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, localcontext
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any

ZERO = Decimal('0')
TERMINAL = {'TERMINAL_NO_FILL', 'TERMINAL_WITH_FILL', 'ABORTED_BEFORE_SEND', 'REJECTED_CONFIRMED'}
IN_FLIGHT = {'RESERVED', 'SUBMITTING', 'ACKNOWLEDGED', 'UNKNOWN'}


class LedgerError(ValueError):
    def __init__(self, *codes):
        self.codes = tuple(codes)
        super().__init__(','.join(codes))


def decimal(value: Any) -> Decimal:
    if isinstance(value, (float, bool)):
        raise LedgerError('DECIMAL_STRING_REQUIRED')
    try:
        number = Decimal(value)
    except Exception as exc:
        raise LedgerError('INVALID_DECIMAL') from exc
    if not number.is_finite():
        raise LedgerError('INVALID_DECIMAL')
    return number


def money(value: Any) -> str:
    value = decimal(value)
    result = format(value, 'f')
    if '.' in result:
        result = result.rstrip('0').rstrip('.')
    return '0' if result in ('-0', '') else result


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False,
                      default=lambda v: money(v) if isinstance(v, Decimal) else str(v))


def digest(value: Any) -> str:
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def epoch_ms(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 9007199254740991:
        raise LedgerError('UTC_EPOCH_MILLISECONDS_REQUIRED')
    return value


def setting(config, path, default):
    if path in config:
        return config[path]
    current = config
    for part in path.split('.'):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


class HengzhunLedger:
    def __init__(self, path, namespace=None, config=None, *, replay_baseline=None):
        self.path = Path(path)
        self.config = config or {}
        self.namespace = namespace or {'mode': 'RECORD', 'portfolio_run_id': 'record',
                                       'scenario_id': 'S1', 'ledger_scope': 'PAPER_PORTFOLIO'}
        if set(self.namespace) != {'mode', 'portfolio_run_id', 'scenario_id', 'ledger_scope'}:
            raise LedgerError('INVALID_ECONOMIC_SPACE')
        if (self.namespace['mode'] not in ('RECORD', 'REPLAY', 'PAPER')
                or setting(self.config,'runtime.mode',self.namespace['mode']) not in ('RECORD','REPLAY','PAPER')
                or setting(self.config, 'runtime.live_execution_enabled', False) is not False):
            raise LedgerError('LIVE_EXECUTION_DISABLED')
        if self.namespace['ledger_scope'] not in ('SIGNAL_SHADOW', 'PAPER_PORTFOLIO'):
            raise LedgerError('INVALID_LEDGER_SCOPE')
        if not all(isinstance(v, str) and v for v in self.namespace.values()):
            raise LedgerError('INVALID_ECONOMIC_SPACE')
        self.replay_baseline=None
        if replay_baseline is not None:
            required={'baseline_id','strategy_id','frozen_provenance_sha256','config_sha256','input_manifest_sha256','registered_at'}
            if self.namespace['mode']!='REPLAY' or self.namespace['ledger_scope']!='PAPER_PORTFOLIO':
                raise LedgerError('BASELINE_POLICY_REPLAY_ONLY')
            if (not isinstance(replay_baseline,dict) or set(replay_baseline)!=required
                    or replay_baseline['baseline_id'] not in ('B1_FROZEN_UNIFIED','B2_PRIOR')
                    or not isinstance(replay_baseline['strategy_id'],str) or not replay_baseline['strategy_id']):
                raise LedgerError('BASELINE_REGISTRATION_INVALID')
            for key in ('frozen_provenance_sha256','config_sha256','input_manifest_sha256'):
                if not isinstance(replay_baseline[key],str) or not re.fullmatch(r'[0-9a-f]{64}',replay_baseline[key]):
                    raise LedgerError('BASELINE_PROVENANCE_REQUIRED')
            epoch_ms(replay_baseline['registered_at'])
            if replay_baseline['config_sha256']!=digest(self.config):raise LedgerError('BASELINE_CONFIG_MISMATCH')
            self.replay_baseline=json.loads(canonical(replay_baseline))
        self.space = digest(self.namespace)
        self.lock = threading.RLock()
        self._savepoint = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=20, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript('''
          CREATE TABLE IF NOT EXISTS hz_spaces(space TEXT PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS hz_baseline_policies(space TEXT PRIMARY KEY,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS hz_wallets(space TEXT,wallet_id TEXT,initial_cash TEXT NOT NULL,
            cash TEXT NOT NULL,realized_pnl TEXT NOT NULL,high_water TEXT NOT NULL,nav TEXT,
            valuation_status TEXT NOT NULL,drawdown_halt INTEGER NOT NULL,halt_reasons TEXT NOT NULL,
            version INTEGER NOT NULL,PRIMARY KEY(space,wallet_id));
          CREATE TABLE IF NOT EXISTS hz_intents(intent_id TEXT PRIMARY KEY,space TEXT NOT NULL,wallet_id TEXT NOT NULL,
            strategy_id TEXT NOT NULL,condition_id TEXT NOT NULL,state TEXT NOT NULL,reserved TEXT NOT NULL,
            send_attempts INTEGER NOT NULL CHECK(send_attempts BETWEEN 0 AND 1),venue_terminal INTEGER NOT NULL,
            reconciled INTEGER NOT NULL,position_state TEXT,recognized_at INTEGER,claim_available_at INTEGER,
            payout TEXT NOT NULL,claim_received INTEGER NOT NULL,result_id TEXT,plan TEXT NOT NULL,payload TEXT NOT NULL,
            UNIQUE(space,wallet_id,strategy_id,condition_id),UNIQUE(space,wallet_id,condition_id));
          CREATE TABLE IF NOT EXISTS hz_fills(space TEXT,venue_fill_key TEXT,intent_id TEXT NOT NULL REFERENCES hz_intents(intent_id),
            state TEXT NOT NULL,cash TEXT NOT NULL,net_shares TEXT NOT NULL,payload TEXT NOT NULL,
            PRIMARY KEY(space,venue_fill_key));
          CREATE TABLE IF NOT EXISTS hz_ledger_events(event_id TEXT PRIMARY KEY,space TEXT NOT NULL,wallet_id TEXT NOT NULL,
            origin_key TEXT NOT NULL,kind TEXT NOT NULL,at_ms INTEGER NOT NULL,payload TEXT NOT NULL,
            UNIQUE(space,wallet_id,origin_key));
          CREATE TABLE IF NOT EXISTS hz_results(space TEXT,condition_id TEXT,result_id TEXT,payload TEXT NOT NULL,
            PRIMARY KEY(space,condition_id,result_id));
          CREATE TABLE IF NOT EXISTS hz_outcomes(intent_id TEXT PRIMARY KEY,space TEXT,wallet_id TEXT,at_ms INTEGER,pnl TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS hz_claim_receipts(space TEXT,receipt_id TEXT,intent_id TEXT,amount TEXT,
            PRIMARY KEY(space,receipt_id),UNIQUE(space,intent_id));
          CREATE TABLE IF NOT EXISTS hz_result_reconciliations(space TEXT,condition_id TEXT,result_id TEXT,payload TEXT NOT NULL,
            PRIMARY KEY(space,condition_id,result_id));
          CREATE TABLE IF NOT EXISTS hz_revision_cash_receipts(space TEXT,receipt_id TEXT,intent_id TEXT,amount TEXT,payload TEXT NOT NULL,
            PRIMARY KEY(space,receipt_id));
          CREATE TABLE IF NOT EXISTS hz_diagnostics(id INTEGER PRIMARY KEY,space TEXT,wallet_id TEXT,at_ms INTEGER,code TEXT,payload TEXT);
          CREATE TABLE IF NOT EXISTS hz_venue_tasks(intent_id TEXT PRIMARY KEY REFERENCES hz_intents(intent_id),payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS hz_venue_events(space TEXT,event_id TEXT,received_at INTEGER,boot_id TEXT,ingest_seq INTEGER,
            payload TEXT NOT NULL,PRIMARY KEY(space,event_id),UNIQUE(space,boot_id,ingest_seq));
          CREATE TABLE IF NOT EXISTS hz_consumed(space TEXT,wallet_id TEXT,book_hash TEXT,side TEXT,price TEXT,quantity TEXT,
            PRIMARY KEY(space,wallet_id,book_hash,side,price));
        ''')
        self.conn.execute('INSERT OR IGNORE INTO hz_spaces VALUES(?,?)', (self.space, canonical(self.namespace)))
        try:
            with self.transaction():
                registered=self.conn.execute('SELECT payload FROM hz_baseline_policies WHERE space=?',(self.space,)).fetchone()
                encoded=canonical(self.replay_baseline)
                if registered and registered[0]!=encoded:raise LedgerError('BASELINE_POLICY_IMMUTABLE_NEW_RUN_REQUIRED')
                if not registered:
                    if self.replay_baseline is not None and self.conn.execute('SELECT 1 FROM hz_wallets WHERE space=?',(self.space,)).fetchone():
                        raise LedgerError('BASELINE_MUST_BE_REGISTERED_BEFORE_WALLET')
                    self.conn.execute('INSERT INTO hz_baseline_policies VALUES(?,?)',(self.space,encoded))
        except Exception:
            self.conn.close()
            raise

    @contextmanager
    def transaction(self):
        with self.lock, localcontext() as ctx:
            ctx.prec = 60
            nested=self.conn.in_transaction
            self._savepoint+=1
            name='hz_'+str(self._savepoint)
            self.conn.execute('SAVEPOINT '+name if nested else 'BEGIN IMMEDIATE')
            try:
                yield
                self.conn.execute('RELEASE SAVEPOINT '+name if nested else 'COMMIT')
            except Exception:
                if nested:
                    self.conn.execute('ROLLBACK TO SAVEPOINT '+name)
                    self.conn.execute('RELEASE SAVEPOINT '+name)
                else:self.conn.execute('ROLLBACK')
                raise

    def close(self):
        self.conn.close()

    def _wallet(self, wallet_id):
        row = self.conn.execute('SELECT * FROM hz_wallets WHERE space=? AND wallet_id=?', (self.space, wallet_id)).fetchone()
        if row is None:
            raise LedgerError('WALLET_NOT_INITIALIZED')
        return dict(row)

    def ensure_wallet(self, wallet_id='HENGZHUN', at_ms=0, initial_cash=None):
        initial = decimal(initial_cash if initial_cash is not None else setting(self.config, 'risk.initial_paper_cash_usd', '100.00'))
        if initial < 0:
            raise LedgerError('INVALID_INITIAL_CASH')
        epoch_ms(at_ms)
        with self.transaction():
            old = self.conn.execute('SELECT * FROM hz_wallets WHERE space=? AND wallet_id=?', (self.space, wallet_id)).fetchone()
            if old:
                if decimal(old['initial_cash']) != initial:
                    raise LedgerError('RUN_INITIAL_CASH_IMMUTABLE')
                return self._snapshot(wallet_id, at_ms)
            self.conn.execute("INSERT INTO hz_wallets VALUES(?,?,?,?,?,?,?,'VERIFIED',0,'[]',0)",
                              (self.space, wallet_id, money(initial), money(initial), '0', money(initial), money(initial)))
            # RECORD maintains a non-economic UI baseline, never an economic event.
            if self.namespace['mode'] != 'RECORD':
                before=self._snapshot(wallet_id,at_ms);before['cash_confirmed']='0'
                self._event(wallet_id, 'initial', 'INITIAL_CASH', at_ms, None, {'CASH_CONFIRMED': initial}, before)
            return self._snapshot(wallet_id, at_ms)

    def intent(self, intent_id):
        with self.lock:
            row = self.conn.execute('SELECT * FROM hz_intents WHERE space=? AND intent_id=?', (self.space, intent_id)).fetchone()
            if row is None:
                raise LedgerError('INTENT_NOT_FOUND')
            result = dict(row)
            result['plan'] = json.loads(result['plan'])
            result['payload'] = json.loads(result['payload'])
            return result

    def intents(self, wallet_id=None):
        with self.lock:
            args = [self.space]
            sql = 'SELECT intent_id FROM hz_intents WHERE space=?'
            if wallet_id is not None:
                sql += ' AND wallet_id=?'; args.append(wallet_id)
            return [self.intent(r[0]) for r in self.conn.execute(sql+' ORDER BY intent_id', args).fetchall()]

    def fills(self, intent_id):
        with self.lock:
            return [{**dict(r), 'payload': json.loads(r['payload'])} for r in self.conn.execute(
                'SELECT * FROM hz_fills WHERE space=? AND intent_id=? ORDER BY venue_fill_key', (self.space, intent_id))]

    def _cost_qty(self, intent_id):
        rows = self.fills(intent_id)
        return (sum((decimal(x['cash']) for x in rows if x['state'] == 'CONFIRMED'), ZERO),
                sum((decimal(x['net_shares']) for x in rows if x['state'] == 'CONFIRMED'), ZERO))

    def _snapshot(self, wallet_id, at_ms):
        wallet = self._wallet(wallet_id)
        intents = self.intents(wallet_id)
        reserved = unresolved = claim_cost = claims = ZERO
        risk_markets, claim_markets, potential_markets = set(), set(), set()
        unknown = False
        for order in intents:
            cost, _ = self._cost_qty(order['intent_id'])
            reserved += decimal(order['reserved'])
            if order['recognized_at'] is None:
                unresolved += cost
                if order['state'] in IN_FLIGHT or cost > 0:
                    risk_markets.add(order['condition_id']); potential_markets.add(order['condition_id'])
            elif not order['claim_received'] and decimal(order['payout']) > 0:
                claim_cost += cost; claims += decimal(order['payout']); claim_markets.add(order['condition_id'])
            unknown |= order['state'] == 'UNKNOWN' or any(x['state'] == 'UNKNOWN' for x in self.fills(order['intent_id']))
        outcomes = self.conn.execute('SELECT pnl FROM hz_outcomes WHERE space=? AND wallet_id=? AND at_ms>? AND at_ms<=?',
                                     (self.space, wallet_id, at_ms-86400000, at_ms)).fetchall()
        g24 = sum((max(ZERO, -decimal(r[0])) for r in outcomes), ZERO)
        return {'scope': 'WALLET_GLOBAL_WITHIN_ECONOMIC_SPACE', 'open_risk_slot_limit': 1,
                'open_risk_slots_used': len(risk_markets), 'cash_confirmed': wallet['cash'], 'cash_reserved': money(reserved),
                'position_cost_unresolved': money(unresolved), 'claim_receivable': money(claims),
                'claim_cost_committed': money(claim_cost), 'committed_cost_total': money(reserved+unresolved+claim_cost),
                'rolling_24h_gross_loss': money(g24), 'unresolved_max_loss': money(reserved+unresolved),
                'realized_pnl': wallet['realized_pnl'], 'claim_pending_markets': len(claim_markets),
                'claim_reserved_markets': len(potential_markets), 'claim_slot_limit': 2, 'unknown_exposure': unknown,
                'valuation_status': wallet['valuation_status'], 'drawdown_halt': bool(wallet['drawdown_halt']), 'snapshot_at': at_ms}

    def snapshot(self, wallet_id='HENGZHUN', at_ms=0):
        epoch_ms(at_ms)
        with self.lock, localcontext() as ctx:
            ctx.prec = 60
            state = self._snapshot(wallet_id, at_ms); wallet = self._wallet(wallet_id)
            counts = {}
            for order in self.intents(wallet_id):
                status = order['position_state'] or order['state']
                counts[status] = counts.get(status, 0)+1
            return {**state, 'wallet_id': wallet_id, 'wallet_version': wallet['version'], 'economic_space': dict(self.namespace),
                    'cash_available': money(decimal(state['cash_confirmed'])-decimal(state['cash_reserved'])),
                    'initial_cash': wallet['initial_cash'], 'high_water_nav': wallet['high_water'],
                    'nav': wallet['nav'] if wallet['valuation_status'] == 'VERIFIED' else None,
                    'halt_reasons': json.loads(wallet['halt_reasons']), 'states': counts,
                    'replay_baseline':self.replay_baseline,
                    'account_return_valid': self.namespace['mode'] in ('REPLAY','PAPER') and self.namespace['ledger_scope'] == 'PAPER_PORTFOLIO'}

    def _diagnostic(self, wallet_id, code, at_ms, payload=None):
        self.conn.execute('INSERT INTO hz_diagnostics(space,wallet_id,at_ms,code,payload) VALUES(?,?,?,?,?)',
                          (self.space, wallet_id, at_ms, code, canonical(payload or {})))

    def _halt(self, wallet_id, reason, at_ms, details=None):
        wallet = self._wallet(wallet_id); reasons = set(json.loads(wallet['halt_reasons'])); reasons.add(reason)
        self.conn.execute('UPDATE hz_wallets SET halt_reasons=?,version=version+1 WHERE space=? AND wallet_id=?',
                          (canonical(sorted(reasons)), self.space, wallet_id))
        self._diagnostic(wallet_id, reason, at_ms, details)

    def halt(self, wallet_id, reason, at_ms, details=None):
        with self.transaction():self._halt(wallet_id, reason, epoch_ms(at_ms), details)

    def _event(self, wallet_id, origin, kind, at_ms, intent, deltas=None, before=None, **extra):
        ident = digest([self.space, wallet_id, origin])
        if self.conn.execute('SELECT 1 FROM hz_ledger_events WHERE event_id=?', (ident,)).fetchone():return ident
        plan = intent['plan'] if intent else {}
        payload = {'record_type': 'LedgerEvent', 'schema_version': '1.0.0', 'ledger_event_id': ident,
            'origin_key': origin, 'economic_space': dict(self.namespace), 'wallet_id': wallet_id,
            'strategy_id': intent['strategy_id'] if intent else None, 'kind': kind, 'effective_at': at_ms, 'recorded_at': at_ms,
            'market': plan.get('market'), 'intent_id': intent['intent_id'] if intent else None,
            'fill_id': None, 'official_result_id': None, 'contract_sha256': plan.get('contract_sha256'),
            'config_sha256': plan.get('config_sha256', digest(self.config)),
            'deltas': [{'account': k, 'asset_id': plan.get('asset_id', 'COLLATERAL') if k == 'POSITION_NET_SHARES' else plan.get('collateral_asset_id', 'COLLATERAL'),
                        'unit': 'SHARES' if k == 'POSITION_NET_SHARES' else 'COLLATERAL', 'delta': money(v)} for k,v in (deltas or {}).items()],
            'wallet_before': before or self._snapshot(wallet_id, at_ms), 'wallet_after': self._snapshot(wallet_id, at_ms),
            'settlement_gate': None, 'claim_available_at': None, 'cash_receipt_verified_at': None,
            'reverses_ledger_event_id': None, 'evidence_hashes': [], 'reason_codes': [], **extra}
        self.conn.execute('INSERT INTO hz_ledger_events VALUES(?,?,?,?,?,?,?)',
                          (ident, self.space, wallet_id, origin, kind, at_ms, canonical(payload)))
        return ident

    def _plan(self, plan):
        p = json.loads(canonical(plan))
        required = {'wallet_id','strategy_id','market','side','asset_id','limit_price','quantity_shares',
                    'budget_all_in','contract_sha256','config_sha256','model_artifact_sha256','decision_frame_id',
                    'calibration_status','cost_quote','window_close_at'}
        if not required <= p.keys():raise LedgerError('INCOMPLETE_ENTRY_PLAN')
        if p['side'] not in ('UP','DOWN'):raise LedgerError('INVALID_SIDE')
        if decimal(p['budget_all_in']) != decimal(setting(self.config, 'entry.budget_usd', '5')) or decimal(p['budget_all_in']) != 5:
            raise LedgerError('FIXED_BUDGET_REQUIRED')
        if decimal(p['quantity_shares']) <= 0 or not 0 < decimal(p['limit_price']) < 1:raise LedgerError('INVALID_QUANTITY_OR_PRICE')
        if decimal(p['cost_quote']['all_in_cash']) > decimal(p['budget_all_in']):raise LedgerError('BUDGET_EXCEEDED')
        if p['calibration_status'] not in ('CALIBRATED','UNCALIBRATED'):raise LedgerError('INVALID_CALIBRATION_STATUS')
        if self.replay_baseline is not None:
            if (p['strategy_id']!=self.replay_baseline['strategy_id'] or p['calibration_status']!='UNCALIBRATED'
                    or p['model_artifact_sha256'] is not None
                    or p.get('baseline_registration_sha256')!=digest(self.replay_baseline)
                    or p['config_sha256']!=self.replay_baseline['config_sha256']):
                raise LedgerError('BASELINE_PLAN_PROVENANCE_MISMATCH')
        elif self.namespace['ledger_scope'] == 'PAPER_PORTFOLIO' and p['calibration_status'] != 'CALIBRATED':raise LedgerError('MODEL_UNREADY')
        if p['calibration_status'] == 'CALIBRATED' and not p['model_artifact_sha256']:raise LedgerError('MODEL_UNREADY')
        return p

    def _guard_reasons(self, p, at_ms):
        reasons=[]
        if self.namespace['mode'] == 'RECORD':reasons.append('RECORD_NO_ECONOMIC_ORDERS')
        if self.replay_baseline is not None and self.replay_baseline['registered_at']>at_ms:reasons.append('BASELINE_NOT_YET_REGISTERED')
        wallet_id=p['wallet_id'];wallet=self._wallet(wallet_id);s=self._snapshot(wallet_id,at_ms);budget=decimal(p['budget_all_in'])
        reasons += json.loads(wallet['halt_reasons'])
        if wallet['drawdown_halt']:reasons.append('DRAWDOWN_HALT')
        if wallet['valuation_status'] != 'VERIFIED':reasons.append('VALUATION_UNCERTAIN')
        if s['unknown_exposure'] or any(o['state'] in IN_FLIGHT for o in self.intents(wallet_id)):reasons.append('UNKNOWN_ORDER_OR_WALLET_FLIGHT')
        if s['open_risk_slots_used'] >= int(setting(self.config,'risk.max_open_risk_positions',1)):reasons.append('WALLET_RISK_SLOT_FULL')
        if decimal(s['cash_confirmed'])-decimal(s['cash_reserved']) < budget:reasons.append('INSUFFICIENT_FULL_BUDGET')
        if decimal(s['committed_cost_total'])+budget > decimal(setting(self.config,'risk.max_committed_cost_usd','10')):reasons.append('COMMITTED_COST_LIMIT')
        if decimal(s['rolling_24h_gross_loss'])+decimal(s['unresolved_max_loss'])+budget > decimal(setting(self.config,'risk.gross_loss_24h_plus_risk_cap_usd','10')):reasons.append('GROSS_LOSS_RISK_LIMIT')
        if s['claim_pending_markets']+s['claim_reserved_markets']+1 > int(setting(self.config,'risk.max_claim_pending_markets',2)):reasons.append('CLAIM_SLOT_LIMIT')
        for old in self.intents(wallet_id):
            if old['condition_id']==p['market']['condition_id']:
                reasons.append('MARKET_INTENT_CONSUMED' if old['strategy_id']==p['strategy_id'] else 'WALLET_MARKET_OWNER_CONFLICT')
        return sorted(set(reasons))

    def guard_reasons(self, plan, at_ms):
        try:p=self._plan(plan)
        except LedgerError as exc:return list(exc.codes)
        with self.lock,localcontext() as ctx:
            ctx.prec=60
            return self._guard_reasons(p,epoch_ms(at_ms))

    def reserve(self, plan, at_ms, expected_wallet_version=None):
        p=self._plan(plan);epoch_ms(at_ms)
        with self.transaction():
            wallet=self._wallet(p['wallet_id']);reasons=self._guard_reasons(p,at_ms)
            if expected_wallet_version is not None and wallet['version']!=expected_wallet_version:reasons.append('WALLET_VERSION_CHANGED')
            if reasons:raise LedgerError(*reasons)
            before=self._snapshot(p['wallet_id'],at_ms)
            ident=digest([self.space,p['wallet_id'],p['strategy_id'],p['market']['condition_id']])
            payload={'record_type':'OrderIntent','schema_version':'1.0.0','intent_id':ident,
              'intent_key':{'wallet_id':p['wallet_id'],'strategy_id':p['strategy_id'],'condition_id':p['market']['condition_id']},
              'identity':{'economic_space':dict(self.namespace),'wallet_id':p['wallet_id'],'strategy_id':p['strategy_id']},
              'market':p['market'],'market_owner_wallet_id':p['wallet_id'],'singleflight_wallet_id':p['wallet_id'],
              'created_at':at_ms,'reserved_at':at_ms,'submitted_at':None,'acknowledged_at':None,'terminal_at':None,
              'state':'RESERVED','period_consumed':True,'send_attempts':0,'venue':'PAPER_VENUE','order_action':'BUY','order_type':'FAK',
              **{k:p[k] for k in ('side','asset_id','limit_price','quantity_shares','budget_all_in','cost_quote','calibration_status','model_artifact_sha256','contract_sha256','config_sha256','decision_frame_id')},
              'reservation_id':digest([ident,'reservation']),'risk_before':before,'risk_after':before,
              'expected_arrival_at':None,'venue_order_id':None,'terminal_reason':None,
              'evidence_hashes':([digest(self.replay_baseline),self.replay_baseline['frozen_provenance_sha256']] if self.replay_baseline else [])}
            if self.replay_baseline:
                from .contracts import validate_record
                payload.update(record_type='BaselineOrderIntent',schema_version='1.0.0-baseline.1',
                    baseline_id=self.replay_baseline['baseline_id'],baseline_registration_sha256=digest(self.replay_baseline),
                    frozen_provenance_sha256=self.replay_baseline['frozen_provenance_sha256'])
                validate_record(payload,'baseline_event_contracts.schema.json',definition='BaselineOrderIntent')
            self.conn.execute("INSERT INTO hz_intents VALUES(?,?,?,?,?,? ,?,0,0,0,NULL,NULL,NULL,'0',0,NULL,?,?)",
                (ident,self.space,p['wallet_id'],p['strategy_id'],p['market']['condition_id'],'RESERVED',money(p['budget_all_in']),canonical(p),canonical(payload)))
            self.conn.execute('UPDATE hz_wallets SET version=version+1 WHERE space=? AND wallet_id=?',(self.space,p['wallet_id']))
            payload['risk_after']=self._snapshot(p['wallet_id'],at_ms)
            self.conn.execute('UPDATE hz_intents SET payload=? WHERE intent_id=?',(canonical(payload),ident))
            self._event(p['wallet_id'],'reserve:'+ident,'RESERVE_BUDGET',at_ms,self.intent(ident),{'CASH_RESERVED':decimal(p['budget_all_in'])},before)
            return self.intent(ident)

    def _state(self, order, state, at_ms, **fields):
        payload={**order['payload'],'state':state,**fields}
        self.conn.execute('UPDATE hz_intents SET state=?,payload=? WHERE intent_id=?',(state,canonical(payload),order['intent_id']))
        self._diagnostic(order['wallet_id'],'ORDER_LIFECYCLE',at_ms,{'intent_id':order['intent_id'],'from':order['state'],'to':state})

    def submit_once(self, intent_id, at_ms, expected_arrival_at=None, recheck=None):
        epoch_ms(at_ms)
        with self.transaction():
            order=self.intent(intent_id)
            if order['state']!='RESERVED' or order['send_attempts']!=0:raise LedgerError('INTENT_ALREADY_SENT_OR_TERMINAL')
            wallet=self._wallet(order['wallet_id'])
            if json.loads(wallet['halt_reasons']) or wallet['drawdown_halt'] or wallet['valuation_status']!='VERIFIED':
                self._release(order,'ABORTED_BEFORE_SEND',at_ms,'WALLET_PAUSED_BEFORE_SEND')
                return self.intent(intent_id)
            if recheck is not None:
                immutable=('side','limit_price','quantity_shares','contract_sha256','config_sha256','model_artifact_sha256')
                if any(recheck.get(k)!=order['plan'][k] for k in immutable) or recheck.get('allow') is not True:
                    self._release(order,'ABORTED_BEFORE_SEND',at_ms,'PRE_SEND_RECHECK_FAILED');return self.intent(intent_id)
            self.conn.execute('UPDATE hz_intents SET send_attempts=1 WHERE intent_id=?',(intent_id,))
            self._state(order,'SUBMITTING',at_ms,send_attempts=1,submitted_at=at_ms,expected_arrival_at=expected_arrival_at)
            return self.intent(intent_id)

    def acknowledge(self,intent_id,at_ms,venue_order_id=None):
        with self.transaction():
            order=self.intent(intent_id)
            if order['state'] in TERMINAL:return order
            if order['state'] not in ('SUBMITTING','UNKNOWN','ACKNOWLEDGED'):raise LedgerError('INVALID_ORDER_TRANSITION')
            self._state(order,'ACKNOWLEDGED',epoch_ms(at_ms),acknowledged_at=at_ms,venue_order_id=venue_order_id or 'paper:'+intent_id)
            return self.intent(intent_id)

    def mark_unknown(self,intent_id,at_ms,reason='UNKNOWN_ORDER'):
        with self.transaction():
            order=self.intent(intent_id)
            if order['state'] in TERMINAL:return order
            if order['state']=='RESERVED':raise LedgerError('NOT_SUBMITTED')
            self._state(order,'UNKNOWN',epoch_ms(at_ms))
            self._event(order['wallet_id'],'unknown:'+intent_id+':'+str(at_ms),'RECOGNIZE_UNKNOWN_EXPOSURE',at_ms,order,reason_codes=[reason])
            return self.intent(intent_id)

    def _release(self,order,state,at_ms,reason):
        before=self._snapshot(order['wallet_id'],at_ms);amount=decimal(order['reserved'])
        self.conn.execute('UPDATE hz_intents SET reserved=? WHERE intent_id=?',('0',order['intent_id']))
        self._state(order,state,at_ms,terminal_at=at_ms,terminal_reason=reason)
        self._refresh_cash_nav(order['wallet_id'],at_ms)
        self._event(order['wallet_id'],'release:'+order['intent_id'],'RELEASE_UNUSED_RESERVATION',at_ms,self.intent(order['intent_id']),{'CASH_RESERVED':-amount},before,reason_codes=[reason])

    def abort_before_send(self,intent_id,at_ms):
        with self.transaction():
            order=self.intent(intent_id)
            if order['state']=='ABORTED_BEFORE_SEND':return order
            if order['state']!='RESERVED' or order['send_attempts']:raise LedgerError('CANNOT_PROVE_UNSENT')
            self._release(order,'ABORTED_BEFORE_SEND',epoch_ms(at_ms),'DURABLE_UNSENT_MARKER')
            return self.intent(intent_id)

    def reject_confirmed(self,intent_id,at_ms,evidence_verified=False):
        with self.transaction():
            order=self.intent(intent_id)
            if order['state']=='REJECTED_CONFIRMED':return order
            if not evidence_verified or order['state'] not in ('SUBMITTING','ACKNOWLEDGED','UNKNOWN') or self.fills(intent_id):raise LedgerError('NO_DEFINITE_ZERO_FILL_PROOF')
            self._release(order,'REJECTED_CONFIRMED',epoch_ms(at_ms),'VERIFIED_VENUE_REJECTION')
            return self.intent(intent_id)

    def record_match(self,intent_id,venue_fill_key,price,economics,at_ms):
        epoch_ms(at_ms)
        with self.transaction():
            order=self.intent(intent_id);existing=self.conn.execute('SELECT * FROM hz_fills WHERE space=? AND venue_fill_key=?',(self.space,venue_fill_key)).fetchone()
            normalized={k:money(economics[k]) for k in ('gross_shares','net_shares','cash_before_fees','fee_cash','fee_shares','all_in_cash')}
            if any(decimal(v)<0 for v in normalized.values()) or decimal(normalized['gross_shares'])<=0:raise LedgerError('INVALID_FILL_ECONOMICS')
            mode=economics.get('fee_collection_mode')
            if mode not in ('CASH_ADDED','SHARES_DEDUCTED','NONE') or not economics.get('fee_model_id'):
                raise LedgerError('FEE_CONTRACT_UNVERIFIED')
            if ((mode=='CASH_ADDED' and decimal(normalized['fee_shares']))
                    or (mode=='SHARES_DEDUCTED' and decimal(normalized['fee_cash']))
                    or (mode=='NONE' and (decimal(normalized['fee_cash']) or decimal(normalized['fee_shares'])))):
                raise LedgerError('FEE_COLLECTION_MISMATCH')
            if decimal(normalized['net_shares'])!=decimal(normalized['gross_shares'])-decimal(normalized['fee_shares']):raise LedgerError('NET_SHARES_MISMATCH')
            if decimal(normalized['all_in_cash'])!=decimal(normalized['cash_before_fees'])+decimal(normalized['fee_cash']):raise LedgerError('CASH_FEE_MISMATCH')
            if existing:
                previous=json.loads(existing['payload'])
                if existing['intent_id']!=intent_id or previous['price']!=money(price) or any(previous['economics'][k]!=v for k,v in normalized.items()):raise LedgerError('FILL_ID_CONFLICT')
                return previous
            if order['state'] in TERMINAL or order['state']=='RESERVED':raise LedgerError('LATE_FILL_RECONCILIATION_REQUIRED')
            allfills=self.fills(intent_id)
            used=sum((decimal(f['cash']) for f in allfills if f['state']!='FAILED_FINAL'),ZERO)
            gross=sum((decimal(f['payload']['economics']['gross_shares']) for f in allfills if f['state']!='FAILED_FINAL'),ZERO)
            if used+decimal(normalized['all_in_cash'])>decimal(order['plan']['budget_all_in']):raise LedgerError('BUDGET_EXCEEDED')
            if gross+decimal(normalized['gross_shares'])>decimal(order['plan']['quantity_shares']):raise LedgerError('OVERFILL')
            if decimal(price)>decimal(order['plan']['limit_price']):raise LedgerError('LIMIT_EXCEEDED')
            record={'record_type':'Fill','schema_version':'1.0.0','fill_id':digest([self.space,venue_fill_key]),'venue_fill_key':venue_fill_key,
                'venue_order_id':order['payload']['venue_order_id'] or 'paper:'+intent_id,'intent_id':intent_id,
                'identity':order['payload']['identity'],'market':order['plan']['market'],'side':order['plan']['side'],'asset_id':order['plan']['asset_id'],
                'contract_sha256':order['plan']['contract_sha256'],'config_sha256':order['plan']['config_sha256'],'price':money(price),
                'status':'MATCHED','source_at':at_ms,'received_at':at_ms,'matched_at':at_ms,'confirmed_at':None,'terminal_at':None,
                'failure_reason':None,'economics':{**normalized,'fee_collection_mode':economics['fee_collection_mode'],
                'fee_model_id':economics['fee_model_id'],'economics_confirmed':False},'evidence_hashes':[]}
            self.conn.execute('INSERT INTO hz_fills VALUES(?,?,?,?,?,?,?)',(self.space,venue_fill_key,intent_id,'MATCHED',normalized['all_in_cash'],normalized['net_shares'],canonical(record)))
            self.conn.execute("UPDATE hz_wallets SET valuation_status='UNCERTAIN',nav=NULL WHERE space=? AND wallet_id=?",(self.space,order['wallet_id']))
            return record

    def confirm_fill(self,venue_fill_key,at_ms,evidence_verified=True):
        epoch_ms(at_ms)
        with self.transaction():
            row=self.conn.execute('SELECT * FROM hz_fills WHERE space=? AND venue_fill_key=?',(self.space,venue_fill_key)).fetchone()
            if not row:raise LedgerError('UNKNOWN_FILL_ID')
            if row['state']=='CONFIRMED':return json.loads(row['payload'])
            if row['state']=='FAILED_FINAL' or not evidence_verified:raise LedgerError('FILL_CONFIRMATION_NOT_VERIFIED')
            order=self.intent(row['intent_id']);wallet=self._wallet(order['wallet_id']);before=self._snapshot(order['wallet_id'],at_ms)
            paid=decimal(row['cash']);reserve=decimal(order['reserved'])
            if paid>reserve or paid>decimal(wallet['cash']):raise LedgerError('BUDGET_OR_CASH_MISMATCH')
            record=json.loads(row['payload']);record.update(status='CONFIRMED',confirmed_at=at_ms,terminal_at=at_ms)
            record['economics']['economics_confirmed']=True
            self.conn.execute("UPDATE hz_fills SET state='CONFIRMED',payload=? WHERE space=? AND venue_fill_key=?",(canonical(record),self.space,venue_fill_key))
            self.conn.execute('UPDATE hz_wallets SET cash=?,version=version+1 WHERE space=? AND wallet_id=?',(money(decimal(wallet['cash'])-paid),self.space,order['wallet_id']))
            self.conn.execute("UPDATE hz_intents SET reserved=?,position_state='OPEN_UNRESOLVED' WHERE intent_id=?",(money(reserve-paid),order['intent_id']))
            self._event(order['wallet_id'],'fill:'+venue_fill_key,'CONFIRM_FILL',at_ms,order,
                {'CASH_CONFIRMED':-paid,'CASH_RESERVED':-paid,'POSITION_COST_UNRESOLVED':paid,'POSITION_NET_SHARES':decimal(row['net_shares'])},before,fill_id=record['fill_id'])
            self._try_finalize(order['intent_id'],at_ms)
            self._try_settle_market(order['condition_id'],at_ms)
            return record

    def fill_status(self,venue_fill_key,status,at_ms,permanent_no_payment_proven=False):
        if status not in ('RETRYING','UNKNOWN','FAILED_FINAL'):raise LedgerError('INVALID_FILL_STATUS')
        with self.transaction():
            row=self.conn.execute('SELECT * FROM hz_fills WHERE space=? AND venue_fill_key=?',(self.space,venue_fill_key)).fetchone()
            if not row:raise LedgerError('UNKNOWN_FILL_ID')
            if row['state'] in ('CONFIRMED','FAILED_FINAL'):return json.loads(row['payload'])
            if status=='FAILED_FINAL' and not permanent_no_payment_proven:raise LedgerError('FAILURE_NOT_FINAL')
            record=json.loads(row['payload']);record['status']=status
            if status=='FAILED_FINAL':record.update(terminal_at=epoch_ms(at_ms),failure_reason='VERIFIED_NO_PAYMENT')
            self.conn.execute('UPDATE hz_fills SET state=?,payload=? WHERE space=? AND venue_fill_key=?',(status,canonical(record),self.space,venue_fill_key))
            if status=='UNKNOWN':self._state(self.intent(row['intent_id']),'UNKNOWN',at_ms)
            self._try_finalize(row['intent_id'],at_ms)
            return record

    def order_terminal(self,intent_id,at_ms,reconciled=False):
        with self.transaction():
            order=self.intent(intent_id)
            if order['state'] in TERMINAL:return order
            if not reconciled or order['state']=='RESERVED':raise LedgerError('TERMINAL_NOT_RECONCILED')
            self.conn.execute('UPDATE hz_intents SET venue_terminal=1,reconciled=1 WHERE intent_id=?',(intent_id,))
            self._try_finalize(intent_id,epoch_ms(at_ms))
            self._try_settle_market(order['condition_id'],at_ms)
            return self.intent(intent_id)

    def _try_finalize(self,intent_id,at_ms):
        order=self.intent(intent_id)
        if order['state'] in TERMINAL or not order['venue_terminal'] or not order['reconciled']:return
        fills=self.fills(intent_id)
        if any(f['state'] not in ('CONFIRMED','FAILED_FINAL') for f in fills):return
        cost,qty=self._cost_qty(intent_id)
        state='TERMINAL_WITH_FILL' if qty>0 or cost>0 else 'TERMINAL_NO_FILL'
        self._release(order,state,at_ms,'WHOLE_ORDER_AND_ALL_CHILD_FILLS_RECONCILED')

    def record_result(self,result,at_ms):
        """OfficialResult contract; label time never substitutes for economic recognition."""
        from .contracts import validate_record
        validate_record(result,definition='OfficialResult')
        epoch_ms(at_ms)
        condition=result['market']['condition_id'];rid=result['result_id']
        if ((result['source_at'] is not None and result['source_at']>result['received_at']) or result['received_at']>at_ms
                or (result.get('verified_at') is not None and not result['received_at']<=result['verified_at']<=at_ms)
                or (result.get('label_available_at') is not None and (result.get('verified_at') is None
                    or not result['verified_at']<=result['label_available_at']<=at_ms))):
            raise LedgerError('FUTURE_RESULT')
        with self.transaction():
            old=self.conn.execute('SELECT payload FROM hz_results WHERE space=? AND condition_id=? AND result_id=?',(self.space,condition,rid)).fetchone()
            if old:
                if json.loads(old[0])!=result:raise LedgerError('RESULT_ID_CONFLICT')
                return False
            previous=[json.loads(r[0]) for r in self.conn.execute('SELECT payload FROM hz_results WHERE space=? AND condition_id=?',(self.space,condition))]
            self.conn.execute('INSERT INTO hz_results VALUES(?,?,?,?)',(self.space,condition,rid,canonical(result)))
            final=result.get('status') in ('VERIFIED_FINAL','SPECIAL_FINAL') and result.get('official_final_verified') is True
            previous_final=[p for p in previous if p.get('official_final_verified')]
            latest=max(previous_final,key=lambda p:p['revision']) if previous_final else None
            conflicting=(latest is not None and (latest.get('payouts')!=result.get('payouts')
                         or (final and result['revision']>=latest['revision']))) or result.get('status')=='CONFLICT'
            if conflicting:
                for o in self.intents():
                    if o['condition_id']==condition:
                        self._halt(o['wallet_id'],'SETTLEMENT_EXCEPTION',at_ms,{'result_id':rid})
                        self.conn.execute("UPDATE hz_intents SET position_state='SETTLEMENT_EXCEPTION' WHERE intent_id=?",(o['intent_id'],))
                        self.conn.execute("UPDATE hz_wallets SET valuation_status='UNCERTAIN',nav=NULL WHERE space=? AND wallet_id=?",
                            (self.space,o['wallet_id']))
                return False
            if final:self._try_settle_market(condition,at_ms)
            else:
                self.conn.execute("UPDATE hz_intents SET position_state='RESULT_PENDING' WHERE space=? AND condition_id=? AND recognized_at IS NULL",(self.space,condition))
            return final

    def reconcile_result_revision(self,condition_id,result_id,at_ms,*,evidence_hashes,cash_adjustments=None):
        """Apply a separately verified revision, never merely a conflicting callback.

        ``cash_adjustments`` is keyed by intent ID. A previously claimed payout
        requires a verified cash receipt for exactly new payout minus old payout;
        its fields are receipt_id, cash_delta, received_at, verified_at,
        evidence_hashes and verified=True. No payment is inferred or sent here.

        Reversal and replacement entries form one atomic journal batch. Every
        entry uses the batch's real before/after wallet snapshots, avoiding a
        fictitious interim risk position or drawdown while reversing old entries.
        """
        at_ms=epoch_ms(at_ms);cash_adjustments=cash_adjustments or {}
        def hashes(values):
            return (isinstance(values,list) and bool(values) and len(set(values))==len(values)
                    and all(isinstance(v,str) and re.fullmatch(r'[0-9a-f]{64}',v) for v in values))
        if not hashes(evidence_hashes):raise LedgerError('REVISION_VERIFICATION_EVIDENCE_REQUIRED')
        request={'condition_id':condition_id,'result_id':result_id,'evidence_hashes':evidence_hashes,'cash_adjustments':cash_adjustments}
        with self.transaction():
            applied=self.conn.execute('SELECT payload FROM hz_result_reconciliations WHERE space=? AND condition_id=? AND result_id=?',
                (self.space,condition_id,result_id)).fetchone()
            if applied:
                if json.loads(applied[0])!=request:raise LedgerError('REVISION_RECONCILIATION_CONFLICT')
                return False
            rows=[json.loads(r[0]) for r in self.conn.execute('SELECT payload FROM hz_results WHERE space=? AND condition_id=?',
                (self.space,condition_id))]
            result=next((r for r in rows if r['result_id']==result_id),None)
            if (not result or not result.get('official_final_verified') or result.get('status') not in ('VERIFIED_FINAL','SPECIAL_FINAL')
                    or result.get('verified_at') is None or result['verified_at']>at_ms or result['received_at']>at_ms):
                raise LedgerError('REVISION_NOT_VERIFIED_FINAL')
            predecessor=next((r for r in rows if r['result_id']==result.get('supersedes_result_id')),None)
            if (not predecessor or result['revision']<=predecessor['revision']
                    or any(r['revision']>result['revision'] for r in rows)):
                raise LedgerError('REVISION_CHAIN_UNVERIFIED')
            orders=[o for o in self.intents() if o['condition_id']==condition_id]
            if set(cash_adjustments)-{o['intent_id'] for o in orders}:raise LedgerError('UNRELATED_REVISION_CASH_RECEIPT')
            changes=[]
            # Validate the entire batch before changing any projection or receipt.
            for order in orders:
                if order['plan']['contract_sha256']!=result['contract_sha256']:raise LedgerError('SETTLEMENT_CONTRACT_CONFLICT')
                if order['recognized_at'] is None:continue
                if order['result_id']!=predecessor['result_id']:raise LedgerError('REVISION_CHAIN_UNVERIFIED')
                fills=self.fills(order['intent_id'])
                if (order['state']!='TERMINAL_WITH_FILL' or not order['venue_terminal'] or not order['reconciled']
                        or any(f['state'] not in ('CONFIRMED','FAILED_FINAL') for f in fills)):
                    raise LedgerError('REVISION_SETTLEMENT_NOT_RECONCILED')
                cost,qty=self._cost_qty(order['intent_id']);payout=qty*decimal(result['payouts'][order['plan']['side'].lower()])
                old_payout=decimal(order['payout']);delta=payout-old_payout;receipt=cash_adjustments.get(order['intent_id'])
                if order['claim_received'] and delta:
                    if (not isinstance(receipt,dict) or receipt.get('verified') is not True or not receipt.get('receipt_id')
                            or not hashes(receipt.get('evidence_hashes')) or decimal(receipt.get('cash_delta'))!=delta):
                        raise LedgerError('REVISION_CASH_DELTA_NOT_VERIFIED')
                    received=epoch_ms(receipt.get('received_at'));verified=epoch_ms(receipt.get('verified_at'))
                    if not result['received_at']<=received<=verified<=at_ms:raise LedgerError('REVISION_CASH_DELTA_NOT_VERIFIED')
                    old_receipt=self.conn.execute('SELECT 1 FROM hz_revision_cash_receipts WHERE space=? AND receipt_id=?',
                        (self.space,receipt['receipt_id'])).fetchone()
                    if old_receipt or self.conn.execute('SELECT 1 FROM hz_claim_receipts WHERE space=? AND receipt_id=?',
                        (self.space,receipt['receipt_id'])).fetchone():raise LedgerError('CLAIM_RECEIPT_CONFLICT')
                    if decimal(self._wallet(order['wallet_id'])['cash'])+delta<0:raise LedgerError('REVISION_CASH_MISMATCH')
                elif receipt is not None:raise LedgerError('UNEXPECTED_REVISION_CASH_RECEIPT')
                history=self.events(order['wallet_id'])
                old_settlement=next((e for e in reversed(history) if e['intent_id']==order['intent_id']
                    and e['official_result_id']==order['result_id'] and e['kind'] in ('SETTLEMENT_RECOGNIZED_CLAIM','SETTLEMENT_RECOGNIZED_LOSS','SETTLEMENT_SPECIAL')),None)
                old_claim=next((e for e in reversed(history) if e['intent_id']==order['intent_id']
                    and e['official_result_id']==order['result_id'] and e['kind']=='CLAIM_RECEIVED'),None)
                if not old_settlement or (order['claim_received'] and not old_claim):raise LedgerError('REVISION_ORIGINAL_ENTRIES_MISSING')
                changes.append((order,cost,payout,delta,receipt,old_settlement,old_claim))
            for order,cost,payout,delta,receipt,old_settlement,old_claim in changes:
                wallet_id=order['wallet_id'];wallet=self._wallet(wallet_id);before=self._snapshot(wallet_id,at_ms)
                claimed=bool(order['claim_received']);new_pnl=payout-cost
                available=None if claimed or payout==0 else at_ms+int(setting(self.config,'paper.claim_after_final_ms',60000))
                state='CLOSED_CLAIMED' if claimed else ('CLAIM_PENDING' if payout else 'RESOLVED_LOSS')
                self.conn.execute('UPDATE hz_intents SET recognized_at=?,claim_available_at=?,payout=?,result_id=?,position_state=? WHERE intent_id=?',
                    (at_ms,available,money(payout),result_id,state,order['intent_id']))
                self.conn.execute('UPDATE hz_wallets SET cash=?,realized_pnl=?,version=version+1 WHERE space=? AND wallet_id=?',
                    (money(decimal(wallet['cash'])+(delta if claimed else ZERO)),money(decimal(wallet['realized_pnl'])+delta),self.space,wallet_id))
                self.conn.execute('UPDATE hz_outcomes SET at_ms=?,pnl=? WHERE intent_id=?',(at_ms,money(new_pnl),order['intent_id']))
                if receipt:self.conn.execute('INSERT INTO hz_revision_cash_receipts VALUES(?,?,?,?,?)',
                    (self.space,receipt['receipt_id'],order['intent_id'],money(delta),canonical(receipt)))
                self._refresh_cash_nav(wallet_id,at_ms)
                after=self._snapshot(wallet_id,at_ms);audit=list(dict.fromkeys(evidence_hashes+[digest(result)]+(receipt['evidence_hashes'] if receipt else [])))
                prefix='revision:'+result_id+':'+order['intent_id']
                common={'official_result_id':result_id,'evidence_hashes':audit,'wallet_after':after,
                        'reason_codes':['VERIFIED_RESULT_REVISION','ATOMIC_REVISION_BATCH']}
                # Preserve and exactly reverse original accounting entries. Any
                # claimed cash difference has already been independently proven.
                for label,old in (('claim',old_claim),('settlement',old_settlement)):
                    if old is None:continue
                    self._event(wallet_id,prefix+':reverse-'+label,'REVERSAL',at_ms,order,
                        {d['account']:-decimal(d['delta']) for d in old['deltas']},before,
                        reverses_ledger_event_id=old['ledger_event_id'],**common)
                gate={**old_settlement['settlement_gate'],'checked_at':at_ms,'settlement_recognized_at':at_ms,
                      'official_result_id':result_id,'evidence_hashes':audit}
                self._event(wallet_id,prefix+':settlement','SETTLEMENT_RECOGNIZED_CLAIM' if payout else 'SETTLEMENT_RECOGNIZED_LOSS',at_ms,order,
                    {'POSITION_COST_UNRESOLVED':-cost,'CLAIM_RECEIVABLE':payout,'CLAIM_COST_COMMITTED':cost if payout else ZERO,'REALIZED_PNL':new_pnl},before,
                    settlement_gate=gate,claim_available_at=available,**common)
                if claimed:
                    # Replacement claim includes zero payouts so the chain can
                    # be reversed again without inventing a new economic claim.
                    self._event(wallet_id,prefix+':claim','CLAIM_RECEIVED',at_ms,order,
                        {'CASH_CONFIRMED':payout,'CLAIM_RECEIVABLE':-payout,'CLAIM_COST_COMMITTED':-cost if payout else ZERO},before,
                        settlement_gate=gate,claim_available_at=at_ms,cash_receipt_verified_at=at_ms,**common)
            for order in orders:
                if order['recognized_at'] is None and order['position_state']=='SETTLEMENT_EXCEPTION':
                    self.conn.execute("UPDATE hz_intents SET position_state='RESULT_PENDING' WHERE intent_id=?",(order['intent_id'],))
            self.conn.execute('INSERT INTO hz_result_reconciliations VALUES(?,?,?,?)',(self.space,condition_id,result_id,canonical(request)))
            # Clear only this class of halt once every affected market on the
            # wallet has been reconciled; drawdown and unrelated halts persist.
            for wallet_id in {o['wallet_id'] for o in orders}:
                if any(o['position_state']=='SETTLEMENT_EXCEPTION' for o in self.intents(wallet_id)):continue
                wallet=self._wallet(wallet_id);reasons=set(json.loads(wallet['halt_reasons']))
                reasons.discard('SETTLEMENT_EXCEPTION')
                self.conn.execute('UPDATE hz_wallets SET halt_reasons=?,version=version+1 WHERE space=? AND wallet_id=?',
                    (canonical(sorted(reasons)),self.space,wallet_id))
                self._diagnostic(wallet_id,'RESULT_REVISION_RECONCILED',at_ms,request)
                self._refresh_cash_nav(wallet_id,at_ms)
            self._try_settle_market(condition_id,at_ms)
            return True

    def _try_settle_market(self,condition,at_ms):
        results=[json.loads(r[0]) for r in self.conn.execute('SELECT payload FROM hz_results WHERE space=? AND condition_id=?',(self.space,condition))]
        valid=[r for r in results if r.get('official_final_verified') is True and r.get('status') in ('VERIFIED_FINAL','SPECIAL_FINAL')
               and r.get('verified_at') is not None and r['verified_at']<=at_ms and r['received_at']<=at_ms and r.get('payouts') is not None]
        if not valid:return
        result=max(valid,key=lambda x:x['revision'])
        for order in self.intents():
            if order['condition_id']!=condition or order['recognized_at'] is not None or order['state']!='TERMINAL_WITH_FILL':continue
            if 'SETTLEMENT_EXCEPTION' in json.loads(self._wallet(order['wallet_id'])['halt_reasons']):continue
            fills=self.fills(order['intent_id'])
            if any(f['state'] not in ('CONFIRMED','FAILED_FINAL') for f in fills):continue
            if order['plan']['contract_sha256']!=result['contract_sha256']:
                self._halt(order['wallet_id'],'SETTLEMENT_CONTRACT_CONFLICT',at_ms);continue
            cost,qty=self._cost_qty(order['intent_id']);unit=decimal(result['payouts'][order['plan']['side'].lower()])
            if not 0<=unit<=1:raise LedgerError('INVALID_PAYOUT_VECTOR')
            payout=qty*unit;pnl=payout-cost;wallet=self._wallet(order['wallet_id']);before=self._snapshot(order['wallet_id'],at_ms)
            available=at_ms+int(setting(self.config,'paper.claim_after_final_ms',60000)) if payout>0 else None
            state='CLAIM_PENDING' if payout>0 else 'RESOLVED_LOSS'
            self.conn.execute('UPDATE hz_intents SET recognized_at=?,claim_available_at=?,payout=?,result_id=?,position_state=? WHERE intent_id=?',
                (at_ms,available,money(payout),result['result_id'],state,order['intent_id']))
            self.conn.execute('UPDATE hz_wallets SET realized_pnl=?,version=version+1 WHERE space=? AND wallet_id=?',(money(decimal(wallet['realized_pnl'])+pnl),self.space,order['wallet_id']))
            self.conn.execute('INSERT INTO hz_outcomes VALUES(?,?,?,?,?)',(order['intent_id'],self.space,order['wallet_id'],at_ms,money(pnl)))
            self._refresh_cash_nav(order['wallet_id'],at_ms)
            gate={'official_final_result_verified':True,'whole_order_terminal_and_reconciled':True,
                  'all_child_fill_economics_confirmed':True,'no_related_unknown_exposure':True,'checked_at':at_ms,
                  'settlement_recognized_at':at_ms,'official_result_id':result['result_id'],'intent_ids':[order['intent_id']],
                  'fill_ids':[f['payload']['fill_id'] for f in fills],'evidence_hashes':[digest(result)]}
            self._event(order['wallet_id'],'settlement:'+order['intent_id'],'SETTLEMENT_RECOGNIZED_CLAIM' if payout>0 else 'SETTLEMENT_RECOGNIZED_LOSS',at_ms,order,
                {'POSITION_COST_UNRESOLVED':-cost,'CLAIM_RECEIVABLE':payout,'CLAIM_COST_COMMITTED':cost if payout>0 else ZERO,'REALIZED_PNL':pnl},before,
                official_result_id=result['result_id'],settlement_gate=gate,claim_available_at=available,evidence_hashes=[digest(result)])

    def claim(self,intent_id,receipt_id,amount,at_ms,evidence_verified=True):
        epoch_ms(at_ms);amount=decimal(amount)
        with self.transaction():
            order=self.intent(intent_id)
            old=self.conn.execute('SELECT * FROM hz_claim_receipts WHERE space=? AND receipt_id=?',(self.space,receipt_id)).fetchone()
            if old:
                if old['intent_id']!=intent_id or decimal(old['amount'])!=amount:raise LedgerError('CLAIM_RECEIPT_CONFLICT')
                return False
            if order['claim_received']:return False
            if (not evidence_verified or order['position_state']!='CLAIM_PENDING' or order['recognized_at'] is None
                    or at_ms<order['claim_available_at'] or amount!=decimal(order['payout'])):raise LedgerError('CLAIM_NOT_VERIFIED_OR_NOT_DUE')
            wallet=self._wallet(order['wallet_id']);before=self._snapshot(order['wallet_id'],at_ms);cost,_=self._cost_qty(intent_id)
            self.conn.execute('INSERT INTO hz_claim_receipts VALUES(?,?,?,?)',(self.space,receipt_id,intent_id,money(amount)))
            self.conn.execute("UPDATE hz_intents SET claim_received=1,position_state='CLOSED_CLAIMED' WHERE intent_id=?",(intent_id,))
            self.conn.execute('UPDATE hz_wallets SET cash=?,version=version+1 WHERE space=? AND wallet_id=?',(money(decimal(wallet['cash'])+amount),self.space,order['wallet_id']))
            self._refresh_cash_nav(order['wallet_id'],at_ms)
            previous=next(e for e in reversed(self.events(order['wallet_id'])) if e['intent_id']==intent_id
                and e['official_result_id']==order['result_id'] and e['kind'] in ('SETTLEMENT_RECOGNIZED_CLAIM','SETTLEMENT_SPECIAL'))
            self._event(order['wallet_id'],'claim:'+receipt_id,'CLAIM_RECEIVED',at_ms,order,
                {'CASH_CONFIRMED':amount,'CLAIM_RECEIVABLE':-amount,'CLAIM_COST_COMMITTED':-cost},before,
                official_result_id=order['result_id'],settlement_gate=previous['settlement_gate'],claim_available_at=order['claim_available_at'],
                cash_receipt_verified_at=at_ms,evidence_hashes=[digest({'receipt_id':receipt_id,'amount':money(amount),'at_ms':at_ms})])
            return True

    def _refresh_cash_nav(self,wallet_id,at_ms):
        state=self._snapshot(wallet_id,at_ms)
        if (decimal(state['position_cost_unresolved']) or state['unknown_exposure']
                or any(o['position_state']=='SETTLEMENT_EXCEPTION' for o in self.intents(wallet_id))):
            return
        if any(f['state'] not in ('CONFIRMED','FAILED_FINAL') for o in self.intents(wallet_id) for f in self.fills(o['intent_id'])):
            return
        wallet=self._wallet(wallet_id);nav=decimal(wallet['cash'])+decimal(state['claim_receivable'])
        high=max(decimal(wallet['high_water']),nav)
        stop=high>0 and (high-nav)/high>=decimal(setting(self.config,'risk.maximum_high_water_drawdown','0.15'))
        self.conn.execute("UPDATE hz_wallets SET nav=?,high_water=?,valuation_status='VERIFIED',drawdown_halt=max(drawdown_halt,?) WHERE space=? AND wallet_id=?",
                          (money(nav),money(high),int(stop),self.space,wallet_id))
        if stop and not wallet['drawdown_halt']:
            self._event(wallet_id,'drawdown:'+str(at_ms),'DRAWDOWN_HALT',at_ms,None,before=state,reason_codes=['DRAWDOWN_HALT'])

    def mark_equity(self,wallet_id,liquidation_value,at_ms,complete=True,quality_valid=True):
        with self.transaction():
            wallet=self._wallet(wallet_id);before=self._snapshot(wallet_id,epoch_ms(at_ms))
            uncertain=(before['unknown_exposure'] or any(o['position_state']=='SETTLEMENT_EXCEPTION' for o in self.intents(wallet_id))
                or any(f['state'] not in ('CONFIRMED','FAILED_FINAL') for o in self.intents(wallet_id) for f in self.fills(o['intent_id'])))
            if not complete or not quality_valid or liquidation_value is None or uncertain:
                self.conn.execute("UPDATE hz_wallets SET valuation_status='UNCERTAIN',nav=NULL WHERE space=? AND wallet_id=?",(self.space,wallet_id))
                self._event(wallet_id,'valuation-missing:'+str(at_ms),'VALUATION_PAUSE',at_ms,None,before=before,reason_codes=['VALUATION_UNCERTAIN'])
                return None
            liquidation=decimal(liquidation_value)
            if decimal(before['position_cost_unresolved'])==0 and liquidation!=0:
                raise LedgerError('NO_POSITION_FOR_LIQUIDATION_VALUE')
            if liquidation<0:raise LedgerError('INVALID_LIQUIDATION_VALUE')
            nav=decimal(wallet['cash'])+liquidation+decimal(before['claim_receivable'])
            high=max(decimal(wallet['high_water']),nav);stop=high>0 and (high-nav)/high>=decimal(setting(self.config,'risk.maximum_high_water_drawdown','0.15'))
            self.conn.execute("UPDATE hz_wallets SET nav=?,high_water=?,valuation_status='VERIFIED',drawdown_halt=max(drawdown_halt,?) WHERE space=? AND wallet_id=?",
                              (money(nav),money(high),int(stop),self.space,wallet_id))
            if stop and not wallet['drawdown_halt']:self._event(wallet_id,'drawdown:'+str(at_ms),'DRAWDOWN_HALT',at_ms,None,before=before,reason_codes=['DRAWDOWN_HALT'])
            return money(nav)

    def reconcile_cash(self,wallet_id,observed_cash,at_ms,registered_delta='0'):
        with self.transaction():
            wallet=self._wallet(wallet_id);expected=decimal(wallet['cash'])+decimal(registered_delta)
            if decimal(observed_cash)!=expected:
                self._halt(wallet_id,'WALLET_RECONCILIATION_MISMATCH',epoch_ms(at_ms),{'expected':money(expected),'observed':money(observed_cash)})
                return False
            return True

    def close_windows(self,at_ms):
        epoch_ms(at_ms)
        with self.transaction():
            for order in self.intents():
                if order['position_state']=='OPEN_UNRESOLVED' and order['plan']['window_close_at']<=at_ms:
                    self.conn.execute("UPDATE hz_intents SET position_state='WINDOW_CLOSED' WHERE intent_id=?",(order['intent_id'],))
                    self._diagnostic(order['wallet_id'],'WINDOW_CLOSED',at_ms,{'intent_id':order['intent_id']})

    def recover_data_health(self,wallet_id,at_ms,healthy_since,*,full_resync,wallet_reconciled):
        epoch_ms(at_ms);epoch_ms(healthy_since)
        if not full_resync or not wallet_reconciled or at_ms-healthy_since<int(setting(self.config,'data.recovery_healthy_seconds',10))*1000:
            return False
        with self.transaction():
            wallet=self._wallet(wallet_id)
            transient={'CLOCK_OFFSET','TIME_BOUNDS_UNKNOWN','DATA_GAP','STREAM_RESYNC_REQUIRED'}
            reasons=set(json.loads(wallet['halt_reasons']))
            self.conn.execute('UPDATE hz_wallets SET halt_reasons=?,version=version+1 WHERE space=? AND wallet_id=?',
                (canonical(sorted(reasons-transient)),self.space,wallet_id))
            self._diagnostic(wallet_id,'DATA_HEALTH_RECOVERED',at_ms,{'healthy_since':healthy_since})
            return True

    def recover(self,at_ms):
        recovered=[]
        for order in self.intents():
            if order['state']=='RESERVED':self.abort_before_send(order['intent_id'],at_ms);recovered.append(order['intent_id'])
            elif order['state']=='SUBMITTING':self.mark_unknown(order['intent_id'],at_ms,'RESTART_AFTER_SEND_MARKER');recovered.append(order['intent_id'])
        return recovered

    def events(self,wallet_id=None):
        with self.lock:
            if wallet_id is None:rows=self.conn.execute('SELECT payload FROM hz_ledger_events WHERE space=? ORDER BY rowid',(self.space,))
            else:rows=self.conn.execute('SELECT payload FROM hz_ledger_events WHERE space=? AND wallet_id=? ORDER BY rowid',(self.space,wallet_id))
            return [json.loads(r[0]) for r in rows]
