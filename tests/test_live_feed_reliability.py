"""Transport, data recovery and power guards; all order clients stay fake."""
import base64
from dataclasses import replace
import hashlib
import json
import socket
import struct
import threading
import time
import unittest
from unittest.mock import Mock,patch

from polybot2other.buffered_ws import BufferedWebSocket
from polybot2other.clob_ws import WebSocketProtocolError,_WEBSOCKET_GUID
from polybot2other.managed_live_book import LiveDepthBook,ReliableLiveBooks,fresh_book
from polybot2other.managed_live_signals import LiveMarketFeed
from polybot2other.models import MarketRound
from polybot2other.service_continuity import ServiceContinuity
import test_managed_live as fixtures


def frame(payload,opcode=1,fin=True):
    if isinstance(payload,str):payload=payload.encode()
    first=(128 if fin else 0)|opcode
    return (bytes([first,len(payload)]) if len(payload)<126 else struct.pack('!BBH',first,126,len(payload)))+payload


class FakeSocket:
    def __init__(self,chunks):self.chunks=list(chunks);self.reads=0;self.sent=[];self.closed=False
    def settimeout(self,value):pass
    def recv(self,size):
        self.reads+=1
        if not self.chunks:raise socket.timeout()
        item=self.chunks.pop(0)
        if isinstance(item,Exception):raise item
        if len(item)>size:self.chunks.insert(0,item[size:])
        return item[:size]
    def sendall(self,value):self.sent.append(value)
    def close(self):self.closed=True


class BufferedTransportTests(unittest.TestCase):
    def test_partial_header_payload_and_fragments_survive_timeouts(self):
        message=frame('hello')
        sock=FakeSocket([message[:1],socket.timeout(),message[1:4],socket.timeout(),message[4:]])
        ws=BufferedWebSocket(sock)
        self.assertIsNone(ws.read_text());self.assertIsNone(ws.read_text());self.assertEqual(ws.read_text(),'hello')
        sock=FakeSocket([frame('part ',fin=False),socket.timeout(),frame('ping',opcode=9)+frame('two',opcode=0)])
        ws=BufferedWebSocket(sock);ws._send_pong=Mock()
        self.assertIsNone(ws.read_text());self.assertEqual(ws.read_text(),'part two');ws._send_pong.assert_called_once_with(b'ping')

    def test_multiple_frames_use_one_buffered_socket_read(self):
        sock=FakeSocket([frame('one')+frame('two')+frame('three')]);ws=BufferedWebSocket(sock)
        self.assertEqual([ws.read_text(),ws.read_text(),ws.read_text()],['one','two','three'])
        self.assertEqual(sock.reads,1)

    def test_first_frame_coalesced_with_handshake_is_not_lost(self):
        nonce=b'a'*16;key=base64.b64encode(nonce).decode()
        accept=base64.b64encode(hashlib.sha1((key+_WEBSOCKET_GUID).encode()).digest()).decode()
        header=f'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n'.encode()
        sock=FakeSocket([header+frame('initial-book')]);context=Mock();context.wrap_socket.return_value=sock
        with patch('polybot2other.buffered_ws.socket.create_connection',return_value=sock),patch('polybot2other.buffered_ws.ssl.create_default_context',return_value=context),patch('polybot2other.buffered_ws.os.urandom',return_value=nonce):
            ws=BufferedWebSocket.connect('wss://example.invalid/stream',8)
        self.assertEqual(ws.read_text(),'initial-book');self.assertEqual(sock.reads,1)
        context.wrap_socket.assert_called_once_with(sock,server_hostname='example.invalid')

    def test_missing_accept_and_oversized_frames_fail_closed(self):
        sock=FakeSocket([b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n']);context=Mock();context.wrap_socket.return_value=sock
        with patch('polybot2other.buffered_ws.socket.create_connection',return_value=sock),patch('polybot2other.buffered_ws.ssl.create_default_context',return_value=context),self.assertRaises(WebSocketProtocolError):
            BufferedWebSocket.connect('wss://example.invalid/',8)
        self.assertTrue(sock.closed)
        ws=BufferedWebSocket(FakeSocket([struct.pack('!BBQ',129,127,BufferedWebSocket.MAX_MESSAGE+1)]))
        with self.assertRaises(WebSocketProtocolError):ws.read_text()


class BookRecoveryTests(unittest.TestCase):
    def setUp(self):
        now=time.time();self.market=MarketRound('round','BTC',now-30,now+240,100,up_token='up',down_token='down')
        self.feed=object.__new__(LiveMarketFeed);self.feed._lock=threading.RLock();self.feed._stop=threading.Event()
        self.feed.current_market=self.market;self.feed.paper_quotes={};self.feed.execution_quotes={};self.feed.latest_quotes={}

    def quote(self,token,age=0):
        now=time.time()*1000
        return {'token_id':token,'best_bid':.4,'best_ask':.41,'source_ms':now-age,'received_ms':now,
                'bids':[{'price':.4,'size':100}],'asks':[{'price':.41,'size':100}]}

    def reader(self,callback):
        return ReliableLiveBooks(self.feed._current_book_market,self.feed._publish_book_quotes,self.feed._book_quotes,callback)

    def test_full_depth_survives_removal_of_more_than_fifty_levels(self):
        book=LiveDepthBook(self.market)
        bids=[{'price':round(.5-i*.001,3),'size':10} for i in range(100)]
        book.apply({'event_type':'book','asset_id':'up','bids':bids,'asks':[{'price':.6,'size':10}]},1000,1100)
        for i in range(60):book.apply({'event_type':'price_change','price_changes':[{'asset_id':'up','side':'BUY','price':bids[i]['price'],'size':0}]},1100+i,1200+i)
        quote=book.quote('up');self.assertEqual(quote['best_bid'],.44);self.assertEqual(len(quote['bids']),40)

    def test_delta_needs_snapshot_and_top_only_cannot_refresh_depth(self):
        book=LiveDepthBook(self.market);delta={'event_type':'price_change','price_changes':[{'asset_id':'up','side':'BUY','price':.4,'size':10}]}
        with self.assertRaisesRegex(ValueError,'INITIAL_BOOK'):book.apply(delta,1000,1000)
        book.apply({'event_type':'book','asset_id':'up','bids':[{'price':.4,'size':10}],'asks':[{'price':.6,'size':10}]},2000,2100)
        self.assertFalse(book.apply({'event_type':'best_bid_ask','asset_id':'up','best_bid':.5,'best_ask':.6},3000,3000))
        self.assertEqual(book.quote('up')['source_ms'],2000)
        with self.assertRaisesRegex(ValueError,'OUT_OF_ORDER'):book.apply(delta,1000,4000)
        delta['price_changes'][0]['best_bid']=.5
        book.apply(delta,4000,4100)
        with self.assertRaisesRegex(ValueError,'DEPTH_MISMATCH'):book.quote('up')

    def test_split_exchange_change_is_not_published_until_final_bbo_matches(self):
        book=LiveDepthBook(self.market)
        book.apply({'event_type':'book','asset_id':'up','bids':[{'price':.37,'size':10},{'price':.36,'size':20}],
                    'asks':[{'price':.38,'size':10}]},2000,2100)
        # Both frames carry the final best bid (.36), including the first one
        # which has not yet removed the old .37 level.
        book.apply({'event_type':'price_change','price_changes':[{'asset_id':'up','side':'BUY','price':.36,'size':30,
                    'best_bid':.36,'best_ask':.38}]},2200,2300)
        self.assertFalse(book.synchronized('up'))
        with self.assertRaisesRegex(ValueError,'DEPTH_MISMATCH'):book.quote('up')
        book.apply({'event_type':'price_change','price_changes':[{'asset_id':'up','side':'BUY','price':.37,'size':0,
                    'best_bid':.36,'best_ask':.38}]},2200,2310)
        self.assertTrue(book.synchronized('up'));self.assertEqual(book.quote('up')['best_bid'],.36)
        self.assertEqual(book.quote('up')['bid_size'],30)

    def test_incomplete_split_change_reconnects_without_publishing_intermediate_depth(self):
        stop=threading.Event();clock=[100.];connections=[];published=[]
        snapshot={'event_type':'book','asset_id':'up','timestamp':time.time()*1000,
                  'bids':[{'price':.37,'size':10},{'price':.36,'size':20}],'asks':[{'price':.38,'size':10}]}
        delta={'event_type':'price_change','timestamp':snapshot['timestamp'],'price_changes':[
            {'asset_id':'up','side':'BUY','price':.36,'size':30,'best_bid':.36,'best_ask':.38}]}
        messages=[json.dumps(snapshot),json.dumps(delta)]
        ws=Mock()
        def read(_):clock[0]+=.3;return messages.pop(0) if messages else None
        ws.read_text.side_effect=read
        def connect(*args):
            connections.append(1)
            if len(connections)>1:stop.set()
            return ws
        stream=self.reader(None);stream.connector=connect;stream.publish=lambda m,q:published.append(q) or True
        with patch('time.monotonic',side_effect=lambda:clock[0]):stream.run(stop)
        self.assertEqual(stream.stats['last_reason'],'BOOK_DEPTH_MISMATCH')
        self.assertEqual(len(published),1);self.assertEqual(published[0]['Up']['bid_size'],10)

    def test_rest_repairs_both_sides_without_rewriting_source_time(self):
        reader=Mock(side_effect=lambda token:self.quote(token,500));stream=self.reader(reader)
        stream.repair_once(threading.Event())
        self.assertEqual(reader.call_count,2)
        for side,token in [('Up','up'),('Down','down')]:
            quote=self.feed.paper_quotes[side]
            self.assertTrue(fresh_book(quote,token,time.time()));self.assertEqual(quote['source'],'clob-rest-recovery')
            self.assertEqual(quote['source_ms'],quote['updated_at_ms']);self.assertEqual(quote['ask_size'],100)
        stream.repair_once(threading.Event());self.assertEqual(reader.call_count,2)

    def test_stale_rest_wrong_token_and_paused_feed_are_never_published(self):
        for callback in (lambda token:self.quote(token,5000),lambda token:self.quote('wrong')):
            stream=self.reader(callback);stream.repair_once(threading.Event());self.assertEqual(self.feed.paper_quotes,{})
        self.feed._stop.set();stream=self.reader(lambda token:self.quote(token));stream.repair_once(threading.Event())
        self.assertEqual(self.feed.paper_quotes,{})

    def test_round_rollover_discards_rest_in_flight(self):
        def read(token):
            self.feed.current_market=replace(self.market,round_id='next',up_token='next-up',down_token='next-down')
            return self.quote(token)
        stream=self.reader(read);stream.repair_once(threading.Event());self.assertEqual(self.feed.paper_quotes,{})

    def test_late_rest_cannot_replace_newer_websocket_quote(self):
        def read(token):
            side='Up' if token=='up' else 'Down'
            self.feed._publish_book_quotes(self.market,{side:{**self.quote(token),'source':'clob-ws-live'}})
            return self.quote(token,500)
        stream=self.reader(read);stream.repair_once(threading.Event())
        self.assertTrue(all(q['source']=='clob-ws-live' for q in self.feed.paper_quotes.values()))

    def test_backlogged_stream_is_rebuilt_without_publishing_old_delta(self):
        stop=threading.Event();clock=[100.];calls=[]
        snapshot={'event_type':'book','asset_id':'up','timestamp':time.time()*1000-6000,'bids':[{'price':.4,'size':10}],'asks':[{'price':.6,'size':10}]}
        delta={'event_type':'price_change','timestamp':time.time()*1000-5000,'price_changes':[{'asset_id':'up','side':'SELL','price':.6,'size':20}]}
        messages=[json.dumps(snapshot),json.dumps(delta)]
        ws=Mock()
        def read(_):clock[0]+=1.1;return messages.pop(0)
        ws.read_text.side_effect=read
        def connect(*args):
            calls.append(1)
            if len(calls)>1:stop.set()
            return ws
        stream=self.reader(None);stream.connector=connect
        with patch('time.monotonic',side_effect=lambda:clock[0]):stream.run(stop)
        self.assertEqual(stream.stats['reconnections'],1);self.assertEqual(stream.stats['last_reason'],'BOOK_STREAM_BACKLOG')
        self.assertEqual(self.feed.paper_quotes,{})


class ContinuityTests(unittest.TestCase):
    def test_guard_is_scoped_to_this_process_and_released_on_stop(self):
        process=Mock();process.poll.return_value=None;process.pid=12345
        with patch('polybot2other.service_continuity.subprocess.Popen',return_value=process) as launch:
            guard=ServiceContinuity(True,platform_name='darwin')
            try:
                guard.start();self.assertTrue(guard.snapshot()['keep_awake_active'])
                command=launch.call_args.args[0]
                self.assertEqual(command[:4],['/usr/bin/caffeinate','-i','-s','-w'])
                self.assertNotIn('-d',command)
            finally:guard.stop()
        process.terminate.assert_called_once();self.assertFalse(guard.snapshot()['keep_awake_active'])

    def test_sleep_gap_triggers_one_recovery_and_non_mac_never_launches_tool(self):
        callback=Mock();guard=ServiceContinuity(True,callback,platform_name='linux')
        with patch('polybot2other.service_continuity.subprocess.Popen') as launch:
            guard._ensure_guard();launch.assert_not_called()
        guard.observe(100,100);guard.observe(102,102);callback.assert_not_called()
        guard.observe(702,104);callback.assert_called_once_with(600)
        guard.observe(704,106);self.assertEqual(callback.call_count,1)


class ResumeAccountTests(unittest.TestCase):
    setUp=fixtures.ManagedLiveTests.setUp

    def test_resume_rechecks_funds_preserves_orders_and_rejects_pre_sleep_results(self):
        self.manager.feed.recover_after_interruption=Mock();before=self.store.config();now=time.time()
        self.manager.recover_after_interruption(7200)
        self.assertFalse(self.manager.wallet['ready']);self.assertFalse(self.manager.geo['ready'])
        self.assertEqual(self.manager.open_orders_at,0);self.assertTrue(self.manager._wallet_wake.is_set())
        self.assertFalse(self.manager._wallet_result_fresh({'ready':True,'read_started_at':now-7200}))
        self.assertTrue(self.manager._wallet_result_fresh({'ready':True,'read_started_at':time.time()}))
        self.assertEqual(self.store.config(),before);self.assertEqual(self.exchange.submitted,[])
        self.manager.feed.recover_after_interruption.assert_called_once()


if __name__=='__main__':unittest.main()
