"""仅人工合同，EXACT 费率只作数学夹具，不声称已核验任何远程市场。"""
from copy import deepcopy
from decimal import Decimal as D
import unittest

from polybot2other.hengzhun.config import load_config
from polybot2other.hengzhun.contracts import contract_hash,digest,require_verified,validate_record
from polybot2other.hengzhun.pricing import fee_adapter,plan_side,choose_entry,cost_quote

ACCEPTANCE_CASES={'EX-011':'test_ex011_unknown_fee_or_contract_blocks_all_scenarios',
                  'EX-012':'test_ex012_semantic_changes_produce_new_contract_hash',
                  'EX-015':'test_ex015_budget_and_minimum_never_round_up',
                  'EX-016':'test_ex016_cash_fee_inside_five_dollar_cap',
                  'EX-017':'test_ex017_share_fee_not_charged_again_as_cash'}


def verified_contract(*,market='SYNTHETIC-M1',opened=1800000000000,mode='CASH_ADDED',rounding='EXACT'):
    evidence={'source_label':'SYNTHETIC UNIT FIXTURE','source_locator':None,
        'raw_payload_sha256':digest({'synthetic_fixture':market}), 'observed_at':opened,
        'verification_method':'artificial numeric contract, never a remote acceptance claim','verified_at':opened}
    c={'record_type':'MarketContractSnapshot','schema_version':'1.0.0','snapshot_id':market+'-snapshot',
       'captured_at':opened,'contract_sha256':'0'*64,'market':{'market_uid':market,'condition_id':market},
       'slug':market,'market_protocol_version':'SYNTHETIC','up_asset_id':market+'-UP','down_asset_id':market+'-DOWN',
       'collateral_asset_id':'SYNTHETIC_USD','collateral_decimals':6,'listed_at':opened-60000,
       'window_open_at':opened,'window_close_at':opened+300000,'window_duration_seconds':300,
       'rules':{'rule_version':'synthetic-rule-v1','rule_text_sha256':digest({'rule':'synthetic-twap60'}),
          'resolution_feed_id':'SYNTHETIC_CHAINLINK','price_kind':'CHAINLINK_TWAP','symbol':'BTC/USD',
          'twap_window_seconds':60,'comparison':'END_GREATER_THAN_OR_EQUAL_TO_START_UP','tie_policy':'UP',
          'result_verification_rule_id':'synthetic-final'},
       'baseline':{'value':'100','source_at':opened,'received_at':opened,'verified_at':opened,'status':'VERIFIED',
          'resolution_feed_id':'SYNTHETIC_CHAINLINK','boundary_rule_id':'synthetic-exact-source-boundary',
          'verification_method':'synthetic fixture','evidence':[deepcopy(evidence)]},
       'trading':{'accepting_orders':True,'book_state':'READY','tick_size':'0.01','tick_size_unit':'COLLATERAL_PER_SHARE',
          'min_order_size':'5','min_order_size_unit':'SHARES','quantity_step':'0.01','quantity_unit':'SHARES',
          'quantity_decimals':6,'supported_order_types':['FAK'],'fee_contract':{'verification_status':'VERIFIED',
             'fee_model_id':'synthetic-cash-or-share-fee','fee_formula_id':'CRYPTO_P_ONE_MINUS_P','coefficient':'0.07',
             'collection_mode':mode,'amount_semantics':'GROSS_SHARES','rounding_mode':rounding,
             'rounding_increment':'0.000001','minimum_fee':'0','evidence':[deepcopy(evidence)]},
          'delay_status':'VERIFIED','venue_min_arrival_delay_ms':0,'delay_included_in_scenario':True,'delay_evidence':[deepcopy(evidence)]},
       'versions':{'adapter_version':'synthetic-fixture-1','adapter_code_sha256':digest({'adapter':'synthetic'}),
          'rest_schema_version':'synthetic-rest-1','websocket_schema_version':'synthetic-ws-1',
          'sdk_locked_version':'SYNTHETIC-NO-SDK','signature_domain_version':None,'data_api_version':None},
       'verification_status':'VERIFIED','verification_reasons':[],'evidence':[deepcopy(evidence)]}
    c['contract_sha256']=contract_hash(c);validate_record(c,definition='MarketContractSnapshot')
    return c


def book(ask='0.60',bid='0.59',size='100'):
    return {'is_complete':True,'asks':[{'price':ask,'size':size}],'bids':[{'price':bid,'size':size}]}


class HengzhunPricingTests(unittest.TestCase):
    def test_ex011_unknown_fee_or_contract_blocks_all_scenarios(self):
        c=verified_contract();unknown=deepcopy(c);unknown['verification_status']='UNVERIFIED';unknown['trading']['fee_contract']['verification_status']='UNVERIFIED'
        unknown['contract_sha256']=contract_hash(unknown)
        for scenario in ('S1','S2','S3'):
            with self.subTest(scenario=scenario),self.assertRaisesRegex(ValueError,'CONTRACT_UNVERIFIED'):
                plan_side(unknown,'Up',book(),.66,load_config(scenario=scenario))
        unsupported=deepcopy(c);unsupported['trading']['fee_contract']['fee_formula_id']='UNKNOWN'
        unsupported['contract_sha256']=contract_hash(unsupported)
        with self.assertRaisesRegex(ValueError,'FEE_FORMULA_UNSUPPORTED'):fee_adapter(unsupported,'Up','.6','5')

    def test_ex012_semantic_changes_produce_new_contract_hash(self):
        original=verified_contract()
        for section,key,value in [('trading','tick_size','0.001'),('baseline','value','101'),('rules','rule_version','synthetic-v2')]:
            changed=deepcopy(original);changed[section][key]=value
            self.assertNotEqual(contract_hash(changed),original['contract_sha256'])
            with self.assertRaisesRegex(ValueError,'HASH_MISMATCH'):require_verified(changed)
        audit=deepcopy(original);audit['captured_at']+=1000;audit['baseline']['received_at']+=1000;audit['baseline']['verified_at']+=1000
        self.assertEqual(contract_hash(audit),original['contract_sha256'])

    def test_baseline_same_feed_and_exact_open_boundary_are_required(self):
        for key,value in [('source_at',1800000000001),('resolution_feed_id','WRONG_FEED')]:
            c=verified_contract();c['baseline'][key]=value;c['contract_sha256']=contract_hash(c)
            with self.subTest(key=key),self.assertRaises(ValueError):require_verified(c)

    def test_ex015_budget_and_minimum_never_round_up(self):
        c=verified_contract();plan=plan_side(c,'Up',book(),.66,load_config())
        self.assertEqual(D(plan['limit_price']),D('.61'))
        self.assertEqual(D(plan['gross_qty'])%D('.01'),0)
        self.assertLessEqual(D(plan['cost']['cash_paid']),D('5'))
        c['trading']['min_order_size']='10';c['contract_sha256']=contract_hash(c)
        with self.assertRaisesRegex(ValueError,'BELOW_MARKET_MINIMUM'):plan_side(c,'Up',book(),.99,load_config())

    def test_ex016_cash_fee_inside_five_dollar_cap(self):
        c=verified_contract();quote=fee_adapter(c,'Up','.6','5')
        self.assertEqual(D(quote['cash_paid']),D('3.084'))
        self.assertEqual(D(quote['net_qty']),D('5'))
        self.assertEqual(D(quote['fee_cash']),D('.084'))
        plan=plan_side(c,'Up',book(),.66,load_config())
        quote=cost_quote(c,'Up',plan['limit_price'],plan['gross_qty'])
        self.assertLessEqual(D(quote['all_in_cash']),D('5'))

    def test_ex017_share_fee_not_charged_again_as_cash(self):
        c=verified_contract(mode='SHARES_DEDUCTED');quote=fee_adapter(c,'Up','.6','5')
        self.assertEqual(D(quote['cash_paid']),D('3'))
        self.assertEqual(D(quote['fee_cash']),D('0'))
        self.assertEqual(D(quote['fee_shares']),D('.14'))
        self.assertEqual(D(quote['net_qty']),D('4.86'))
        self.assertAlmostEqual(float(quote['cost_per_share']),3/4.86,places=12)

    def test_two_sides_choose_value_and_tie_abstains(self):
        c=verified_contract();books={'Up':book('.20','.19'),'Down':book('.20','.19')}
        plan,reasons=choose_entry(c,books,{'Up':.5,'Down':.5},load_config())
        self.assertIsNone(plan);self.assertEqual(reasons,['VALUE_TIE'])
        plan,_=choose_entry(c,books,{'Up':.6,'Down':.4},load_config());self.assertEqual(plan['side'],'Up')

    def test_empty_zero_and_negative_depth_cannot_create_a_plan(self):
        for invalid in [book(size='0'),book(size='-1'),{'is_complete':False,'asks':[{'price':'.6','size':'10'}],'bids':[{'price':'.59','size':'10'}]}]:
            with self.subTest(book=invalid),self.assertRaises(ValueError):plan_side(verified_contract(),'Up',invalid,.66,load_config())


if __name__=='__main__':unittest.main()
