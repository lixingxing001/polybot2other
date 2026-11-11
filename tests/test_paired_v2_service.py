from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock,patch
import json
import threading
import unittest
import urllib.request
import urllib.error

from polybot2other.paired_v2_service import PairedV2Service
from polybot2other.paired_paper_service import PairedPaperService
from polybot2other.web import DashboardServer,Handler
from test_paired_paper import books,market,print_
from test_paired_v2 import terms


@dataclass
class Market:
    round_id:str='m';condition_id:str='m';slug:str='m'
    started_at:float=0;ends_at:float=300;up_token:str='mu';down_token:str='md';target_price:float=100


class OneCycle:
    def __init__(self):self.calls=0
    def is_set(self):self.calls+=1;return self.calls>1
    def wait(self,value):pass


class V2ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.feed=SimpleNamespace(books=SimpleNamespace(add_listener=Mock(),remove_listener=Mock()),observation=lambda:(None,{}, {},0))
        with patch('polybot2other.paired_paper_service.now_ms',return_value=0),patch('polybot2other.paired_v2_service.now_ms',return_value=0):
            self.s=PairedV2Service(Path(self.tmp.name)/'v2',self.feed)
        self.addCleanup(self.s.engine.close);self.addCleanup(self.s.stress.close)
        self.addCleanup(self.s.http.close);self.addCleanup(self.s.exit_http.close)

    def test_one_cycle_shares_observation_but_not_cash_and_posts_no_network_orders(self):
        for e in (self.s.engine,self.s.stress):
            for at in (30000,30500,31000,31500,32000,33900,34000):e.tick(at,market(),books(at),terms(at))
        self.feed.observation=lambda:(Market(),{},books(35000,up='.45'),35)
        self.s.rules['m']=terms(35000);self.s.stop_event=OneCycle()
        with patch('polybot2other.paired_v2_service.now_ms',return_value=35000),patch('httpx.Client.post',side_effect=AssertionError('NO LIVE')):
            self.s._run()
        self.assertIsNone(self.s.error)
        self.assertEqual(self.s.engine.snapshot(35000)['fills'],[])
        self.assertGreater(len(self.s.stress.snapshot(35000)['fills']),0)

    def test_independent_exit_cache_survives_entry_metadata_removed(self):
        self.s.rules['m']=terms(30000)
        self.s.engine.set_exit_rules('m',terms(30000),books(30000),30000)
        self.s.rules.pop('m');self.s.engine.rules.pop('m',None)
        self.assertEqual(self.s.engine.exit_rules['m']['checked_at'],30000)

    def test_both_scenarios_receive_results_and_rule_events(self):
        self.s.exit_queue.put({'kind':'rules','cid':'m','rules':terms(34000),'books':books(34000),'at':34000})
        self.s.stop_event=OneCycle()
        with patch('polybot2other.paired_v2_service.now_ms',return_value=35000):self.s._run()
        self.assertIsNone(self.s.error)
        self.assertEqual(self.s.engine.exit_rules['m'],self.s.stress.exit_rules['m'])

    def test_new_controller_applies_to_both_paper_scenarios_without_live_fields(self):
        bot=SimpleNamespace(paired_v2=self.s,paired_v2_snapshot=self.s.snapshot)
        server=DashboardServer(('127.0.0.1',0),Handler,bot)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        op=urllib.request.build_opener(urllib.request.ProxyHandler({}));url=f'http://127.0.0.1:{server.server_port}'
        try:
            with op.open(url+'/api/paired-v2') as response:data=json.load(response)
            self.assertEqual(data['scenario'],'TAPE');self.assertEqual(data['stress']['scenario'],'STRESS')
            self.assertEqual(data['cash'],'100');self.assertEqual(data['stress']['cash'],'100')
            def post(payload,token=''):
                request=urllib.request.Request(url+'/api/paired-v2/settings',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Origin':url,'X-Paired-Control':token})
                return op.open(request)
            with self.assertRaises(urllib.error.HTTPError) as err:post({'enabled':False})
            self.assertEqual(err.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError):post({'live_enabled':True},data['control_token'])
            with post({'budget':'6','enabled':False},data['control_token']) as response:changed=json.load(response)
            self.assertEqual(changed['settings']['budget'],'6.00');self.assertEqual(changed['stress']['settings']['budget'],'6.00')
            self.assertFalse(changed['live_enabled']);self.assertFalse(changed['settings']['enabled'])
        finally:server.shutdown();server.server_close();thread.join(timeout=2)

    def test_frozen_v1_remains_visible_read_only_and_does_not_start_collector(self):
        folder=Path(self.tmp.name)/'v1'
        old=PairedPaperService(folder,self.feed)
        with old.engine.tx():old.engine.conn.execute("UPDATE pair_wallet SET cash='87',realized='-13'")
        old.http.close();old.engine.close()
        frozen=PairedPaperService(folder,self.feed,frozen=True)
        try:
            frozen.start();s=frozen.snapshot()
            self.assertTrue(s['frozen']);self.assertFalse(s['running']);self.assertEqual(s['cash'],'87')
            self.feed.books.add_listener.assert_not_called()
            with self.assertRaises(ValueError):frozen.update_settings({'enabled':True})
        finally:frozen.http.close();frozen.engine.close()


if __name__=='__main__':unittest.main()
