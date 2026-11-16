"""V3 persistence/migration tests use independent disposable SQLite sources."""
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from polybot2other.adaptive_features import FEATURE_VERSION
from polybot2other.adaptive_v3 import MODEL_VERSION
from polybot2other.adaptive_v3_model import AdaptiveV3Model, normalize_trade

NOW=2_000_000.
V2='main-adaptive-calibration-v2'


def source_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE trades(id INTEGER PRIMARY KEY,round_id TEXT,side TEXT,confidence REAL,move_bps REAL,
                entry_price REAL,opened_at REAL,settled_at REAL,pnl REAL,status TEXT,settlement_source TEXT);
            CREATE TABLE market_rounds(round_id TEXT PRIMARY KEY,outcome TEXT,ends_at REAL,settled_at REAL,settlement_source TEXT);
            CREATE TABLE paper_orders(id INTEGER PRIMARY KEY,trade_id INTEGER);
            CREATE TABLE paper_sim_orders(order_id INTEGER PRIMARY KEY,payload TEXT);
        ''')


def trade_record(ident, **changes):
    ends=NOW-1000-ident*500
    payload={'submitted_at':ends-200,'signal':{'entry_price':.6,'side':'Up'},
             'market':{'ends_at':ends,'round_id':f'round-{ident}'}}
    row={'id':ident,'round_id':f'round-{ident}','side':'Up','confidence':.7,'move_bps':10.,
         'entry_price':.61,'opened_at':ends-190,'settled_at':ends+20,'pnl':1.,'status':'SETTLED',
         'settlement_source':'polymarket_official','outcome':'Up','ends_at':ends,
         'outcome_source':'polymarket_official','order_payload':payload}
    row.update(changes)
    return row


def metadata(ident, **changes):
    value={'round_id':f'new-{ident}','candidate_side':'Up','decision_at':NOW,
        'market_ends_at':NOW+300,'base_selected_score':.7,'adaptive_score':.7,'adaptive_delta':0.,
        'adaptive_candidate_score':.72,'adaptive_candidate_delta':.02,'adaptive_allowed':True,
        'adaptive_candidate_allowed':True,'adaptive_model_version':MODEL_VERSION,
        'adaptive_training_cutoff':NOW-1,'adaptive_validation_state':'BASELINE_SHADOW',
        'entry_price':.6,'all_in_cost':.62,'min_edge':.02,'time_left_seconds':300.,
        'fee_source':'official-market','v3_training_fingerprint':'a'*64,'v3_model_state':'READY',
        'v3_feature_version':FEATURE_VERSION,'v3_feature_status':'MISSING_HISTORY'}
    value.update(changes)
    when=value['decision_at']
    if 'v3_feature_source_ms' not in changes:value['v3_feature_source_ms']=(when-1)*1000
    if 'v3_feature_received_ms' not in changes:value['v3_feature_received_ms']=when*1000
    if 'adaptive_training_cutoff' not in changes:value['adaptive_training_cutoff']=when-1
    return value


class AdaptiveV3PersistenceTests(unittest.TestCase):
    def setUp(self):
        temporary=TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.seed=self.root/'optimized.sqlite3';self.own=self.root/'adaptive.sqlite3'
        self.model_path=self.root/'model.sqlite3';source_db(self.seed);source_db(self.own)
        self.clock=patch('polybot2other.adaptive_v3_model.time.time',return_value=NOW)
        self.clock.start();self.addCleanup(self.clock.stop)

    def insert_trade(self,path,row):
        with sqlite3.connect(path) as conn:
            conn.execute('INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?)',tuple(row[k] for k in
                ('id','round_id','side','confidence','move_bps','entry_price','opened_at','settled_at','pnl','status','settlement_source')))
            conn.execute('INSERT INTO market_rounds VALUES(?,?,?,?,?)',(row['round_id'],row['outcome'],row['ends_at'],row['settled_at'],row['outcome_source']))
            conn.execute('INSERT INTO paper_orders VALUES(?,?)',(row['id'],row['id']))
            conn.execute('INSERT INTO paper_sim_orders VALUES(?,?)',(row['id'],json.dumps(row['order_payload'])))

    def model(self):
        model=AdaptiveV3Model(self.model_path,self.seed,self.own,reference_path=self.root/'absent-references.sqlite3')
        self.addCleanup(model.close);return model

    def test_optimized_decision_uses_submission_not_later_fill(self):
        row=trade_record(1);result=normalize_trade(row,'MAIN_OPTIMIZED')
        self.assertEqual(result['decision_at'],row['order_payload']['submitted_at'])
        self.assertNotEqual(result['decision_at'],row['opened_at'])
        self.assertEqual(result['entry_price'],.6)
        self.assertEqual(result['time_left_seconds'],200)
        missing=normalize_trade({**row,'order_payload':{}},'MAIN_OPTIMIZED')
        self.assertEqual(missing['exclusion_reason'],'missing_valid_pre_fill_decision')
        self.assertIsNone(missing['decision_at'])

    def test_legacy_adjusted_scores_and_nonofficial_labels_are_excluded(self):
        old=normalize_trade(trade_record(1),'MAIN_ADAPTIVE')
        self.assertEqual(old['provenance'],'legacy_adaptive')
        self.assertEqual(old['exclusion_reason'],'missing_raw_base_score')
        bad=normalize_trade(trade_record(1,outcome_source='inferred'),'MAIN_OPTIMIZED')
        self.assertEqual(bad['exclusion_reason'],'nonofficial_or_nonterminal_label')
        self.assertIsNone(bad['won'])

    def test_v2_database_migration_preserves_tables_and_source_ledgers(self):
        self.insert_trade(self.seed,trade_record(1))
        with sqlite3.connect(self.model_path) as conn:
            conn.executescript('''CREATE TABLE model_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO model_meta VALUES('model_version','main-adaptive-calibration-v2');
                CREATE TABLE training_samples(source_key TEXT PRIMARY KEY,payload TEXT);
                INSERT INTO training_samples VALUES('kept','historical-data');
                CREATE TABLE calibration_observations(round_id TEXT PRIMARY KEY,side TEXT,decision_at REAL,
                    ends_at REAL,payload TEXT,outcome TEXT,settled_at REAL,label_source TEXT,recorded_at REAL);''')
            old=metadata(1,round_id='old',decision_at=NOW-2000,market_ends_at=NOW-1700,
                         adaptive_model_version=V2,time_left_seconds=300.)
            conn.execute('INSERT INTO calibration_observations VALUES(?,?,?,?,?,?,?,?,?)',
                ('old','Up',NOW-2000,NOW-1700,json.dumps(old),'Up',NOW-1650,'polymarket_official',NOW-1999))
        source_hashes={p:sha256(p.read_bytes()).digest() for p in (self.seed,self.own)}
        model=self.model();snapshot=model.snapshot()
        self.assertEqual(model.conn.execute('SELECT * FROM training_samples').fetchall()[0]['payload'],'historical-data')
        self.assertEqual(model.conn.execute('SELECT count(*) FROM calibration_observations').fetchone()[0],1)
        self.assertEqual(model.conn.execute("SELECT value FROM model_meta WHERE key='model_version'").fetchone()[0],V2)
        self.assertEqual(snapshot['model_version'],MODEL_VERSION)
        self.assertEqual(snapshot['shadow_observations'],0)
        self.assertEqual(snapshot['validation']['samples'],0)
        self.assertFalse(snapshot['validation']['passed'])
        self.assertTrue(snapshot['v3']['historical_seed']['loaded'])
        self.assertGreaterEqual(snapshot['unique_markets'],2)
        for path,digest in source_hashes.items():self.assertEqual(sha256(path.read_bytes()).digest(),digest)

    def test_first_candidate_commits_once_and_never_after_market_end(self):
        model=self.model();first=metadata(1)
        self.assertTrue(model.record_observation(first))
        self.assertFalse(model.record_observation({**first,'adaptive_candidate_score':.64}))
        stored=json.loads(model.conn.execute('SELECT payload FROM context_observations_v3').fetchone()[0])
        self.assertEqual(stored['adaptive_candidate_score'],.72)
        self.assertFalse(model.record_observation(metadata(2,decision_at=NOW+1)))
        self.assertFalse(model.record_observation(metadata(3,market_ends_at=NOW)))
        self.assertFalse(model.record_observation(metadata(4,decision_at=NOW-31)))

    def test_invalid_first_observation_cannot_reserve_market(self):
        model=self.model()
        for index,change in enumerate(({'base_selected_score':None},{'base_selected_score':.2},
            {'adaptive_candidate_score':float('nan')},{'adaptive_candidate_score':1.1},
            {'entry_price':0},{'all_in_cost':.4},{'all_in_cost':1.1},{'min_edge':-1},
            {'adaptive_training_cutoff':NOW+1})):
            with self.subTest(change=change):
                self.assertFalse(model.record_observation(metadata(index,**change)))
                self.assertTrue(model.record_observation(metadata(index)))

    def test_unsettled_and_nonofficial_labels_never_enter_training_or_validation(self):
        model=self.model();self.assertTrue(model.record_observation(metadata(1)))
        with sqlite3.connect(self.seed) as conn:
            conn.execute('INSERT INTO market_rounds VALUES(?,?,?,?,?)',('new-1','Up',NOW+300,NOW+310,'inferred'))
        model.sync(force=True)
        self.assertEqual(model.snapshot()['shadow_resolved'],0)
        # Future official outcomes are retained as raw observations but are not
        # available for today's training or promotion.
        with sqlite3.connect(self.seed) as conn:
            conn.execute("UPDATE market_rounds SET settlement_source='polymarket_official'")
        model.sync(force=True);snapshot=model.snapshot()
        self.assertEqual(snapshot['validation']['samples'],0)
        self.assertEqual(snapshot['unique_markets'],0)
        self.assertEqual(model._model(NOW)['counts']['unique_markets'],0)
        with patch('polybot2other.adaptive_v3_model.time.time',return_value=NOW+400):
            model.sync(force=True);later=model.snapshot()
        self.assertEqual(later['unique_markets'],1)
        self.assertEqual(later['validation']['samples'],1)

    def test_v3_immutable_features_and_actual_score_corrections_are_counted(self):
        row=trade_record(1)
        d=metadata(1,round_id=row['round_id'],decision_at=row['opened_at']-1,market_ends_at=row['ends_at'],
            time_left_seconds=191,adaptive_score=.72,adaptive_delta=.02,
            v3_momentum_10_bps=3.2,v3_feature_status='PARTIAL_HISTORY')
        row['order_payload']['adaptive_decision']=d
        self.insert_trade(self.own,row)
        model=self.model();snapshot=model.snapshot()
        self.assertEqual(snapshot['v3']['execution']['v3_trades'],1)
        self.assertEqual(snapshot['v3']['execution']['actual_adjusted_trades'],1)
        self.assertEqual(model._rows[0]['v3_momentum_10_bps'],3.2)
        self.assertEqual(model._rows[0]['base_score'],.7)
        self.assertEqual(model._rows[0]['applied_score'],.72)
        self.assertEqual(model._rows[0]['provenance'],'adaptive_v3_raw')
        self.assertEqual(snapshot['validation']['samples'],0)

    def test_future_imported_feature_receipts_or_training_times_are_quarantined(self):
        for ident,change in ((1,'future_receipt'),(2,'future_training'),(3,'missing_clock')):
            row=trade_record(ident);when=row['opened_at']-1
            d=metadata(ident,round_id=row['round_id'],decision_at=when,market_ends_at=row['ends_at'],
                v3_momentum_10_bps=3.2,v3_volatility_bps=1.,v3_feature_status='PARTIAL_HISTORY')
            if change=='future_receipt':d['v3_feature_received_ms']=(when+1)*1000
            elif change=='future_training':d['adaptive_training_cutoff']=when
            else:d['v3_feature_source_ms']=None
            row['order_payload']['adaptive_decision']=d
            self.insert_trade(self.own,row)
        model=self.model();snapshot=model.snapshot()
        self.assertEqual(snapshot['unique_markets'],3)
        for row in model._rows:
            self.assertEqual(row['base_score'],.7)
            self.assertIsNone(row['exclusion_reason'])
            self.assertIsNone(row['v3_momentum_10_bps'])
            self.assertIsNone(row['v3_volatility_bps'])
            self.assertEqual(row['v3_feature_status'],'INVALID_CAUSAL_PROVENANCE')
            self.assertEqual(row['v3_feature_origin'],'decision-snapshot')

    def test_optimized_order_side_or_market_mismatch_is_excluded(self):
        for part,field,value in (('signal','side','Down'),('market','round_id','another-round')):
            with self.subTest(field=field):
                row=trade_record(1);row['order_payload'][part][field]=value
                normalized=normalize_trade(row,'MAIN_OPTIMIZED')
                self.assertEqual(normalized['exclusion_reason'],'mismatched_order_provenance')
                self.assertEqual(normalized['base_score'],.7)

    def test_future_live_feature_metadata_does_not_reserve_observation(self):
        model=self.model()
        bad=metadata(1,v3_momentum_10_bps=2.,v3_feature_received_ms=(NOW+1)*1000)
        self.assertFalse(model.record_observation(bad))
        self.assertTrue(model.record_observation(metadata(1,v3_momentum_10_bps=2.)))

    def test_candidate_errors_fall_back_to_baseline_without_blocking(self):
        model=self.model();market=SimpleNamespace(round_id='current',ends_at=NOW+120)
        with patch('polybot2other.adaptive_v3_model.predict',side_effect=ValueError('bad candidate')):
            result=model.assess_entry({'up_score':.8},market,NOW,entry_price=.6,all_in_cost=.62,min_edge=.02)
        self.assertEqual(result['adaptive_score'],.8)
        self.assertEqual(result['adaptive_delta'],0)
        self.assertTrue(result['adaptive_allowed'])
        self.assertEqual(model.snapshot()['v3']['learner_error'],'ValueError')


if __name__=='__main__':unittest.main()
