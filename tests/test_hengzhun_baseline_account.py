"""Exact frozen legacy account, synthetic frames and isolated SQLite only."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from polybot2other.config import Settings
from polybot2other.models import MarketRound
from polybot2other.paper_execution import ExecutionPolicy
from polybot2other.shared_market_inputs import MarketInputs
from polybot2other.hengzhun.baseline import SIGNAL_FIELDS,capture_main_frame
from polybot2other.hengzhun.baseline_account import OriginalMainAccountReplay,run_original_main_account
from polybot2other.hengzhun.cli import freeze_baseline
import test_hengzhun_execution as fixture


class OriginalAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.settings=Settings();self.rule={'enabled':False,'stake_dollars':3.,'max_open_trades':5,
            'max_daily_loss':21.,'take_profit_pct':20.,'stop_loss_pct':40.}
        self.policy=asdict(ExecutionPolicy(confirmation_seconds=10,depth_fraction=.5,redemption_seconds=60))
        self.info={'fee_rate':.07,'fee_exponent':1,'tick_size':'0.01','neg_risk':False}
        self.frozen=self.root/'frozen'
        freeze_baseline(Path(__file__).parents[1],self.frozen,{'signal_settings':{k:getattr(self.settings,k) for k in SIGNAL_FIELDS},
            'main_rule':self.rule,'execution_policy':self.policy})

    def frame(self,at,market='M1',opened=0):
        round=self.market=MarketRound(market,'BTC',opened/1000,(opened+300000)/1000,100.,
            condition_id=market,up_token=market+'-UP',down_token=market+'-DOWN')
        reference={'ready':True,'protocol_verified':True,
            'latest':{'price.crypto':{'price':100.2,'source_ms':at-100}}}
        price={'chainlink':100.2,'chainlink_updated_ms':at-100}
        quotes={side:{'best_bid':bid,'best_ask':ask,'bid_size':100,'ask_size':100,
            'source_ms':at-100,'received_ms':at-90,'updated_at_ms':at-100,'token_id':market+'-'+side.upper(),
            'tick_size':'0.01','min_order_size':1,'bids':[{'price':bid,'size':100}],
            'asks':[{'price':ask,'size':100}]} for side,bid,ask in [('Up',.39,.4),('Down',.59,.6)]}
        frame=MarketInputs(round,reference,price,quotes,at/1000,'frame-'+str(at),None)
        record=capture_main_frame(frame,self.settings)
        record.update(main_rule=deepcopy(self.rule),execution_policy=deepcopy(self.policy),market_info=deepcopy(self.info))
        return record

    def events(self):
        return [self.frame(at) for at in (240000,241000,249999,250000)]+[fixture.result()]

    def test_original_engine_keeps_three_dollar_budget_and_submit_anchored_confirmation(self):
        original_clock=time.time
        with patch('httpx.Client.get',side_effect=AssertionError('NO NETWORK')) as get,\
             patch('httpx.Client.post',side_effect=AssertionError('NO NETWORK')) as post:
            report=run_original_main_account(self.events(),self.frozen,self.root/'run',final_at=370000)
        get.assert_not_called();post.assert_not_called();self.assertIs(time.time,original_clock)
        self.assertEqual(report['status'],'COMPLETED');self.assertFalse(report['new_hengzhun_risk_rules_applied'])
        orders=report['financial_tables']['paper_orders'];self.assertEqual(len(orders),1)
        self.assertEqual(orders[0]['requested_cash'],3)
        import json
        simulated=json.loads(report['financial_tables']['paper_sim_orders'][0]['payload'])
        self.assertEqual(simulated['matched_at'],241);self.assertEqual(simulated['confirmed_at'],250)
        self.assertEqual(report['execution_policy'],self.policy);self.assertEqual(report['main_rule'],self.rule)
        self.assertEqual(report['economic_tail'],{'pending_orders':0,'open_positions':0,'pending_claims':0})
        self.assertAlmostEqual(report['account']['cash_balance'],100+report['account']['realized_pnl'],places=6)
        self.assertEqual(report['execution_evidence'],'ASSUMED')

    def test_original_slot_limit_remains_five_and_accounts_do_not_reset_between_markets(self):
        events=[self.frame(at) for at in (240000,241000,250000)]
        events += [self.frame(at,'M2',300000) for at in (540000,541000,550000)]
        report=run_original_main_account(events,self.frozen,self.root/'run',final_at=550000)
        self.assertEqual(report['status'],'PENDING_TAIL')
        self.assertEqual(report['economic_tail']['open_positions'],2)
        self.assertEqual(len(report['financial_tables']['paper_orders']),2)
        self.assertEqual(report['initial_cash'],100);self.assertLess(report['account']['cash_balance'],95)
        self.assertIsNone(report['conservative_nav'])

    def test_repeated_full_account_replay_has_same_economic_hash(self):
        a=run_original_main_account(self.events(),self.frozen,self.root/'a',final_at=370000)
        b=run_original_main_account(self.events(),self.frozen,self.root/'b',final_at=370000)
        self.assertEqual(a,b)

    def test_missing_inputs_and_missing_actual_fee_response_stay_unavailable(self):
        absent=run_original_main_account([],self.frozen,self.root/'absent')
        self.assertEqual(absent['status'],'BASELINE_UNAVAILABLE')
        self.assertIn('BASELINE_INPUT_UNAVAILABLE',absent['missing'])
        frame=self.frame(240000);frame.pop('market_info')
        missing=run_original_main_account([frame],self.frozen,self.root/'missing',final_at=250000)
        self.assertIn('BASELINE_MARKET_INFO_UNAVAILABLE',missing['missing'])
        self.assertEqual(missing['financial_tables']['paper_orders'],[])

    def test_policy_change_is_not_silently_rewritten_to_frozen_or_hengzhun_limits(self):
        changed=self.frame(240000);changed['main_rule']['max_open_trades']=1
        with self.assertRaisesRegex(ValueError,'POLICY_CHANGED_OR_MISSING'):
            run_original_main_account([changed],self.frozen,self.root/'changed')

    def test_future_favorable_quote_cannot_fill_the_original_arrival(self):
        first=self.frame(240000);expensive=self.frame(240900)
        expensive['input']['quotes']['Up'].update(best_bid=.79,best_ask=.8,
            bids=[{'price':.79,'size':100}],asks=[{'price':.8,'size':100}])
        later=self.frame(241100)
        report=run_original_main_account([first,expensive,later],self.frozen,self.root/'run',final_at=242000)
        self.assertEqual(report['financial_tables']['paper_fills'],[])
        self.assertEqual(report['financial_tables']['paper_orders'][0]['status'],'CANCELED')
        self.assertEqual(report['account']['cash_balance'],100)

    def test_output_folder_cannot_reuse_or_reset_old_account(self):
        run_original_main_account([],self.frozen,self.root/'run')
        with self.assertRaises(FileExistsError):run_original_main_account([],self.frozen,self.root/'run')


if __name__=='__main__':unittest.main()
