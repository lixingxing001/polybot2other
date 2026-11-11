"""Independent per-market counterfactual fills, never a pooled $100 return."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from . import STRATEGY_ID
from .config import config_hash, load_config
from .contracts import canonical, digest, require_verified, validate_record
from .execution import PaperVenue, effective_delay
from .ledger import HengzhunLedger, LedgerError
from .pricing import fee_adapter


class SignalShadowCoordinator:
    def __init__(self, path, namespace, config):
        self.config = load_config(config)
        self.namespace = {**namespace, 'ledger_scope': 'SIGNAL_SHADOW'}
        self.ledger = HengzhunLedger(Path(path), namespace=self.namespace, config=self.config)
        self.venue = PaperVenue(self.ledger, fee_adapter)
        self.error = None
        with self.ledger.lock, self.ledger.conn:
            self.ledger.conn.execute('CREATE TABLE IF NOT EXISTS hz_shadow_frames(space TEXT, frame_id TEXT, json TEXT, PRIMARY KEY(space,frame_id))')

    def close(self):
        self.ledger.close()

    def recover(self, at_ms):
        """Use the caller's event clock, including offline replay after restart."""
        return self.ledger.recover(at_ms)

    def on_events(self, events):
        if self.namespace['mode'] == 'RECORD':
            return
        if self.namespace['mode'] == 'PAPER':
            result = self.venue.ingest_observed_events(events)
            if result['late_arrival_conflict']:
                self.error = 'SHADOW_LATE_BOOK_FOR_PROCESSED_ARRIVAL'
        else:
            self.venue.on_events(events)

    def on_event(self, event):
        self.on_events([event])

    def advance(self, at_ms):
        if self.namespace['mode'] != 'RECORD':
            self.venue.advance(at_ms)

    def consider(self, frame, contract, at_ms, *, clock_bound_ms, pre_send_recheck):
        """Observe qualified signals even when the portfolio has no risk capacity.

        This method requires a fresh-input callback from the strategy runtime.
        Portfolio risk reasons are intentionally absent from the shadow wallet;
        signal/data/model/time rules are retained.
        """
        if self.namespace['mode'] == 'RECORD': return None
        if (frame.get('calibration_status') != 'CALIBRATED' or not frame.get('fitted')
                or frame.get('data_status') != 'READY' or frame.get('data_reasons')
                or (frame.get('prior') or {}).get('proxy_status') != 'READY'):
            return None
        validate_record(frame, definition='FeatureFrame')
        if frame['decision_at'] > at_ms or frame['contract_sha256'] != contract['contract_sha256']:
            raise ValueError('SHADOW_FRAME_NOT_VISIBLE_OR_CONTRACT_CHANGED')
        if frame['config_sha256'] != config_hash(self.config):
            raise ValueError('SHADOW_FRAME_CONFIG_CHANGED')
        require_verified(contract)
        left = (contract['window_close_at'] - at_ms) / 1000
        entry = self.config['entry']
        if not entry['submit_remaining_min_seconds'] <= left <= entry['submit_remaining_max_seconds']:
            return None
        delay = effective_delay(self.namespace['scenario_id'], contract)['effective_total_delay_ms']
        if contract['window_close_at'] - at_ms - delay < entry['expected_arrival_remaining_min_seconds'] * 1000:
            return None
        candidates = [x for x in frame.get('candidates', []) if x['passes_signal_gates']
                      and x['probability_basis'] == 'FITTED_CONSERVATIVE']
        value = lambda x: Decimal(x['cost_quote']['net_shares']) * Decimal(str(x['net_edge']))
        candidates.sort(key=value, reverse=True)
        if not candidates or (len(candidates) > 1 and abs(value(candidates[0])-value(candidates[1]))
                              <= Decimal(entry['value_tie_tolerance_usd'])):
            return None
        chosen = candidates[0]
        wallet = 'SHADOW:' + digest(contract['market'])[:32]
        self.ledger.ensure_wallet(wallet, at_ms, initial_cash=entry['budget_usd'])
        shadow_frame = deepcopy(frame)
        shadow_frame['frame_id'] = digest([frame['frame_id'], self.namespace, wallet])
        shadow_frame['decision_id'] = shadow_frame['frame_id']
        shadow_frame['identity'] = {'economic_space': self.namespace, 'wallet_id': wallet, 'strategy_id': STRATEGY_ID}
        shadow_frame['decision_reasons'] = []
        plan = {'wallet_id': wallet, 'strategy_id': STRATEGY_ID, 'market': contract['market'],
                'side': chosen['side'], 'asset_id': chosen['asset_id'],
                'collateral_asset_id': contract['collateral_asset_id'],
                'limit_price': chosen['limit_price'], 'quantity_shares': chosen['quantity_shares'],
                'budget_all_in': entry['budget_usd'], 'cost_quote': chosen['cost_quote'],
                'contract_sha256': contract['contract_sha256'], 'config_sha256': config_hash(self.config),
                'model_artifact_sha256': frame['fitted']['model_artifact_sha256'],
                'decision_frame_id': shadow_frame['frame_id'], 'calibration_status': 'CALIBRATED',
                'window_close_at': contract['window_close_at']}
        reasons = self.ledger.guard_reasons(plan, at_ms)
        if reasons:
            return None  # Each market consumes its one intent permanently.
        with self.ledger.transaction():
            order = self.ledger.reserve(plan, at_ms)
            shadow_frame.update(decision='INTENT_CREATED', intent_id=order['intent_id'])
            validate_record(shadow_frame, definition='FeatureFrame')
            self.ledger.conn.execute('INSERT INTO hz_shadow_frames VALUES(?,?,?)',
                (self.ledger.space, shadow_frame['frame_id'], canonical(shadow_frame)))
        try:
            recheck = pre_send_recheck(plan, at_ms)
            self.venue.submit(order['intent_id'], contract, at_ms, recheck=recheck, clock_bound_ms=clock_bound_ms)
        except Exception:
            # A failed fresh-input callback proves this path never started send.
            # Preserve the consumed market identity while releasing its budget.
            if self.ledger.intent(order['intent_id'])['state'] == 'RESERVED':
                self.ledger.abort_before_send(order['intent_id'], at_ms)
            raise
        return order['intent_id']

    def snapshot(self, at_ms):
        with self.ledger.lock:
            orders = self.ledger.intents()
            states = {}
            for order in orders:
                state = order['position_state'] or order['state']
                states[state] = states.get(state, 0) + 1
            outcomes = self.ledger.conn.execute('SELECT pnl FROM hz_outcomes WHERE space=?', (self.ledger.space,)).fetchall()
            fills = self.ledger.conn.execute("SELECT COUNT(DISTINCT intent_id) FROM hz_fills WHERE space=? AND state!='FAILED_FINAL'", (self.ledger.space,)).fetchone()[0]
            return {'channel': 'SIGNAL_SHADOW', 'economic_space': self.namespace,
                    'capital_model': 'INDEPENDENT_FIXED_BUDGET_PER_MARKET', 'account_return_valid': False,
                    'execution_evidence': 'ASSUMED', 'intent_count': len(orders), 'filled_markets': fills,
                    'settled_markets': len(outcomes), 'pending_markets': sum(o['recognized_at'] is None and o['send_attempts'] > 0 and o['state'] not in ('TERMINAL_NO_FILL', 'REJECTED_CONFIRMED', 'ABORTED_BEFORE_SEND') for o in orders),
                    'net_pnl': str(sum((Decimal(r[0]) for r in outcomes), Decimal(0))),
                    'hypothetical_pnl': str(sum((Decimal(r[0]) for r in outcomes), Decimal(0))),
                    'states': states, 'error': self.error, 'snapshot_at': at_ms,
                    'note': '独立市场假设成交，不能相加后当作一个 $100 账户的收益率。'}
