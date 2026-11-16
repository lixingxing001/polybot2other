from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from polybot2other.adaptive_model import AdaptiveModel, MODEL_VERSION
from polybot2other.models import MarketRound, Signal
from polybot2other.storage import TradeStore


class AdaptiveModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=time.time()
        self.seed=TradeStore(self.root/'main_optimized.sqlite3',100)
        self.adaptive=TradeStore(self.root/'main_adaptive.sqlite3',100)
        self.addCleanup(self.seed.conn.close);self.addCleanup(self.adaptive.conn.close)

    def trade(self,store,index,*,outcome='Up',side='Up',confidence=.8,settled=None):
        settled=self.now-600-index if settled is None else settled
        slug=f'btc-updown-5m-test-{index}'
        market=MarketRound(slug,'BTC',settled-301,settled-1,100,up_token=f'u-{index}',down_token=f'd-{index}')
        store.upsert_round(market)
        ident=store.place_trade(SimpleNamespace(market=market,signal=Signal('BTC',side,confidence,.4,20,'test'),stake_dollars=1.))
        with store.conn:store.conn.execute('UPDATE trades SET opened_at=? WHERE id=?',(settled-120,ident))
        store.settle_round_outcome(slug,outcome,settled)
        return ident,market,settled

    def model(self):
        result=AdaptiveModel(self.root/'model.sqlite3',self.seed.db_path,self.adaptive.db_path)
        self.addCleanup(result.close);return result

    def assessment(self,model,now=None):
        market=SimpleNamespace(round_id='current',target_price=100,ends_at=self.now+120)
        return model.assess_entry({'up_score':.8,'projected_final_twap':100.2},market,self.now if now is None else now,
            entry_price=.4,all_in_cost=.45,min_edge=.02)

    def attach_v2(self,trade_id,market,settled,**changes):
        decision={'adaptive_model_version':MODEL_VERSION,'base_selected_score':.81,'adaptive_score':.75,
            'candidate_side':'Up','round_id':market.round_id,'decision_at':settled-121,
            'time_left_seconds':120.,'entry_price':.4,'all_in_cost':.45}
        decision.update(changes)
        with self.adaptive.conn:
            self.adaptive.conn.execute('CREATE TABLE IF NOT EXISTS paper_sim_orders(order_id INTEGER PRIMARY KEY,phase TEXT,due_at REAL,payload TEXT)')
            # The real table's trade_id link is the provenance anchor.
            self.adaptive.conn.execute("""INSERT INTO paper_orders(round_id,symbol,side,order_type,status,requested_cash,
                trade_id,reason,created_at,updated_at) VALUES(?,'BTC','Up','FAK','FILLED',1,?,'test',?,?)""",
                (market.round_id,trade_id,settled-121,settled-120))
            order=self.adaptive.conn.execute('SELECT max(id) FROM paper_orders').fetchone()[0]
            self.adaptive.conn.execute('INSERT INTO paper_sim_orders VALUES(?,?,?,?)',
                (order,'FILLED',settled-120,json.dumps({'action':'BUY','adaptive_decision':decision})))

    def test_reads_source_without_writing_and_sparse_samples_fall_back(self):
        self.trade(self.seed,1)
        before={s.db_path:hashlib.sha256(s.db_path.read_bytes()).digest() for s in (self.seed,self.adaptive)}
        model=self.model();result=self.assessment(model)
        self.assertEqual(result['adaptive_state'],'BASELINE_FALLBACK')
        self.assertTrue(result['adaptive_allowed'])
        self.assertEqual(model.snapshot()['seed_samples'],1)
        for s in (self.seed,self.adaptive):self.assertEqual(hashlib.sha256(s.db_path.read_bytes()).digest(),before[s.db_path])

    def test_old_adjusted_adaptive_rows_preserved_but_excluded(self):
        for i in range(12):self.trade(self.adaptive,i,confidence=.76)
        model=self.model();s=model.snapshot()
        self.assertEqual(s['total_samples'],12)
        self.assertEqual(s['excluded_legacy_samples'],12)
        self.assertEqual(s['unique_markets'],0)
        self.assertEqual(self.assessment(model)['adaptive_score'],.8)

    def test_v2_raw_score_is_read_from_immutable_order_payload(self):
        ident,market,settled=self.trade(self.adaptive,1,confidence=.75)
        self.attach_v2(ident,market,settled)
        model=self.model();row=dict(model.conn.execute('SELECT * FROM training_samples').fetchone())
        self.assertEqual(row['confidence'],.75)
        self.assertEqual(row['base_score'],.81)
        self.assertEqual(row['provenance'],'adaptive_v2_raw')
        self.assertEqual(model.snapshot()['usable_samples'],1)
        self.assertEqual(model.snapshot()['recent_quality']['paired_prediction_samples'],1)

    def test_future_decision_payload_cannot_claim_valid_provenance(self):
        ident,market,settled=self.trade(self.adaptive,1)
        self.attach_v2(ident,market,settled,decision_at=settled+1)
        model=self.model()
        self.assertEqual(model.snapshot()['excluded_legacy_samples'],1)

    def test_asof_decision_does_not_see_later_settled_training_rows(self):
        for i in range(30):self.trade(self.seed,i,outcome='Down',settled=self.now+100+i)
        model=self.model();result=self.assessment(model)
        self.assertEqual(result['adaptive_samples'],0)
        self.assertEqual(result['adaptive_score'],.8)
        after=self.assessment(model,now=self.now+500)
        self.assertLess(after['adaptive_candidate_score'],.8)
        self.assertEqual(after['adaptive_score'],.8)
        self.assertEqual(after['adaptive_validation_state'],'BASELINE_SHADOW')

    def test_adjust_hook_preserves_raw_score_and_does_not_double_calibrate(self):
        model=self.model();market=SimpleNamespace(target_price=100)
        a=model.adjust_prediction({'up_score':.2,'projected_final_twap':99.8},market,self.now)
        b=model.adjust_prediction({**a,'up_score':.3},market,self.now)
        self.assertEqual(a['base_selected_score'],.8);self.assertEqual(b['base_selected_score'],.8)
        self.assertEqual(b['up_score'],.2);self.assertEqual(b['candidate_side'],'Down')

    def test_legacy_schema_migration_keeps_every_original_row(self):
        path=self.root/'model.sqlite3'
        c=sqlite3.connect(path)
        c.executescript('''CREATE TABLE training_samples(source_key TEXT PRIMARY KEY,source_name TEXT NOT NULL,
            source_trade_id INTEGER NOT NULL,round_id TEXT NOT NULL,side TEXT NOT NULL,confidence REAL NOT NULL,
            move_bps REAL NOT NULL,entry_price REAL NOT NULL,opened_at REAL NOT NULL,settled_at REAL NOT NULL,
            pnl REAL NOT NULL,won INTEGER NOT NULL,imported_at REAL NOT NULL);''')
        for name in ['MAIN_OPTIMIZED','MAIN_ADAPTIVE']:
            c.execute('INSERT INTO training_samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (name+':99',name,99,name+'-round','Up',.8,20,.4,self.now-200,self.now-100,1,1,self.now-99))
        c.commit();c.close();model=self.model();snapshot=model.snapshot()
        self.assertEqual(snapshot['total_samples'],2);self.assertEqual(snapshot['unique_markets'],1)
        self.assertEqual(snapshot['excluded_legacy_samples'],1)

    def test_shadow_observation_is_first_candidate_and_requires_official_label(self):
        _,market,settled=self.trade(self.seed,1)
        model=self.model()
        metadata={'round_id':market.round_id,'candidate_side':'Down','decision_at':settled-121,
            'market_ends_at':market.ends_at,'base_selected_score':.7,'adaptive_score':.66,
            'adaptive_model_version':MODEL_VERSION,'entry_price':.6,'all_in_cost':.63,
            'min_edge':.02,'time_left_seconds':120,'adaptive_allowed':False}
        with patch('polybot2other.adaptive_model.time.time',return_value=settled-119):
            self.assertTrue(model.record_observation(metadata))
            self.assertFalse(model.record_observation({**metadata,'candidate_side':'Up','decision_at':settled-120}))
        model.sync(force=True);snapshot=model.snapshot()
        self.assertEqual(snapshot['shadow_observations'],1);self.assertEqual(snapshot['shadow_resolved'],1)
        self.assertEqual(snapshot['total_samples'],1)
        self.assertEqual(snapshot['unique_markets'],1)
        # Seed wins; a shadow opposing vote does not double/count or override it.
        self.assertEqual(model.conn.execute('SELECT count(*) FROM training_samples').fetchone()[0],1)
        missing={**metadata,'round_id':'unresolved'}
        with patch('polybot2other.adaptive_model.time.time',return_value=settled-119):
            self.assertTrue(model.record_observation(missing))
        model.sync(force=True)
        self.assertEqual(model.snapshot()['shadow_resolved'],1)
        self.assertFalse(model.record_observation({**metadata,'round_id':'after-the-fact'}))


if __name__=='__main__':unittest.main()
