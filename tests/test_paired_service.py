from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import Mock, patch

from polybot2other.managed_live_book import ReliableLiveBooks
from polybot2other.paired_paper_service import PairedPaperService
from polybot2other.web import DashboardServer, Handler


class PairServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.books=ReliableLiveBooks(lambda:None,lambda *a:None,lambda:{},None)
        self.feed=SimpleNamespace(books=self.books,observation=lambda:(None,{}, {},0))
        self.service=PairedPaperService(Path(self.tmp.name),self.feed)
        self.addCleanup(self.service.engine.close);self.addCleanup(self.service.http.close)

    def test_selective_observer_preserves_original_observer_and_drops_no_quote_stream(self):
        original=Mock();self.books.observer=original
        self.books.add_listener('paired-paper',self.service.capture)
        for _ in range(200):self.books._observe('BOOK',{'event_type':'price_change'},1000)
        self.assertEqual(self.service.pending.qsize(),0);self.assertEqual(original.call_count,200)
        self.books._observe('BOOK',{'event_type':'last_trade_price','price':'.5'},1000)
        self.assertEqual(self.service.pending.qsize(),1)
        self.books.remove_listener('paired-paper');self.books._observe('BOOK',{'event_type':'last_trade_price'},1001)
        self.assertEqual(self.service.pending.qsize(),1)

    def test_listener_failure_never_changes_old_feed(self):
        original=Mock();self.books.observer=original
        self.books.add_listener('broken',Mock(side_effect=OSError('fixture')))
        self.books._observe('BOOK',{'event_type':'book'},1000)
        original.assert_called_once()

    def test_gamma_lookup_can_find_closed_markets_and_never_uses_active_only_collection(self):
        self.service._get=Mock(return_value=({'slug':'btc-updown-5m-123','closed':True},{'sha256':'proof'}))
        result,_=self.service._gamma('btc-updown-5m-123')
        self.assertTrue(result['closed']);self.assertTrue(self.service._get.call_args.args[0].endswith('/markets/slug/btc-updown-5m-123'))

    def test_controller_requires_local_origin_and_token_and_never_changes_live(self):
        bot=SimpleNamespace(paired_paper=self.service,paired_paper_snapshot=self.service.snapshot)
        server=DashboardServer(('127.0.0.1',0),Handler,bot)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}';opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(base+'/api/paired-paper') as response:body=json.load(response)
            self.assertEqual(body['mode'],'PAPER');self.assertFalse(body['live_enabled'])
            def post(payload,token='',origin=base):
                req=urllib.request.Request(base+'/api/paired-paper/settings',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Paired-Control':token,'Origin':origin})
                return opener.open(req)
            with self.assertRaises(urllib.error.HTTPError) as denied:post({'enabled':False})
            self.assertEqual(denied.exception.code,403)
            with self.assertRaises(urllib.error.HTTPError):post({'enabled':False},body['control_token'],'https://foreign.invalid')
            with self.assertRaises(urllib.error.HTTPError) as invalid:post({'live_enabled':True},body['control_token'])
            self.assertEqual(invalid.exception.code,400)
            with post({'budget':'6','enabled':False},body['control_token']) as response:updated=json.load(response)
            self.assertEqual(updated['settings']['budget'],'6.00');self.assertFalse(updated['settings']['enabled'])
            self.assertEqual(updated['cash'],'100')
        finally:server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()
