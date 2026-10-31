"""Isolated UI verification server. Fake exchange, temp ledgers, no trading worker."""
from dataclasses import replace
import importlib.util
import os
from pathlib import Path
import tempfile
import time

from polybot2other.bot import PaperTradingBot
from polybot2other.config import Settings
from polybot2other.managed_live import ManagedLiveAccounts
from polybot2other.managed_live_store import ManagedLiveStore
from polybot2other.models import MarketRound
from polybot2other.storage import TradeStore
from polybot2other.web import DashboardServer, Handler, STATIC_DIR

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location("live_test_fakes",ROOT/"tests/test_managed_live.py")
fakes=importlib.util.module_from_spec(spec);spec.loader.exec_module(fakes)
TMP=Path(tempfile.mkdtemp(prefix="polybot-live-ui-"));os.chdir(TMP)
settings=Settings(db_path=TMP/"paper.sqlite3",initial_balance=777,
                  live_trading_runtime_enabled=False,managed_live_enabled=False,
                  strategy_experiments_enabled=True,strategy_experiments_db_dir=TMP/"paper-variants",
                  btc_runtime_settings_path=TMP/"btc.json",market_scout_settings_path=TMP/"scout.json",
                  market_scout_enabled=False,llm_super_agent_enabled=False)
paper=TradeStore(settings.db_path,777);bot=PaperTradingBot(settings,paper)
now=time.time();start=int(now)//300*300
market=MarketRound(f"btc-updown-5m-{start}","BTC",start,start+300,100,
                   condition_id="fake-condition",up_token="up",down_token="down")
feed=fakes.FakeFeed(market);exchange=fakes.FakeExchange()
def no_real_post(*args,**kwargs):
    raise AssertionError("UI preview never sends an external order")
exchange.submit=no_real_post
store=ManagedLiveStore(TMP/"live/accounts.sqlite3")
recovery_order=None

class PreviewAccounts(ManagedLiveAccounts):
    def snapshot(self):
        now=time.time()
        self.wallet={"balance":42.771923,"allowance":1000,"portfolio_value":10,"total_equity":52.771923,
                     "ready":True,"checked_at":now,"read_started_at":now,"address_masked":"TEST…ONLY","currency":"测试单位"}
        self.geo={"ready":True,"blocked":False,"checked_at":now};self.open_orders_at=now
        if recovery_order is not None:
            self._last_tick_at=now;self._tick_wait=None
            self.reconcile();self.refresh_valuations()
        return super().snapshot()

managed=PreviewAccounts(replace(settings,managed_live_enabled=True,live_trading_runtime_enabled=True,
                                managed_live_dir=TMP/"live"),bot,exchange=exchange,feed=feed,store=store)
managed.process_lock.acquire();managed.snapshot()
store.record_wallet(42.771923,52.771923,now-30);store.record_wallet(42.771923,52.771923,now)
if os.environ.get("POLYBOT2OTHER_UI_VALUATION_TEST")=="1":
    # Isolated fixture only: the fake exchange cannot POST, no worker is started,
    # and every ledger lives under TMP. Let a cached quote age while the UI polls.
    market=replace(market,ends_at=now+3600);feed.market=market;store.save_market(market)
    order=store.prepare_order("0x"+"f"*64,"MAIN",market,"up","Up","BUY",.4,cash=5)
    store.apply_confirmed_fill(order["id"],trade_id="ui-fixture",shares=10,price=.4,
                               fee=.168,fee_source="test_fixture")
    store.set_order_state(order["id"],"FILLED")
    store.update_config({"enabled":True,"strategies":{"MAIN":{"enabled":True}}})
    feed.bid=.49;feed.ask=.5;managed.refresh_valuations()
    for quote in managed._valuation_books.values():
        quote["source_ms"]-=10000;quote["received_ms"]-=10000;quote["valid_until"]-=10
        store.save_valuation_book(quote)
if os.environ.get("POLYBOT2OTHER_UI_RECOVERY_TEST")=="1":
    market=replace(market,ends_at=now+3600);feed.market=market;store.save_market(market)
    recovery_order=store.prepare_order("0x"+"e"*64,"MAIN",market,"up","Up","BUY",.4,cash=5)
    store.set_order_state(recovery_order["id"],"PENDING")
    store.update_config({"enabled":True,"strategies":{"MAIN":{"enabled":True}}})
    recovery_started=time.monotonic()
    def delayed_read(order,market,info):
        if time.monotonic()-recovery_started<60:
            raise fakes.OrderConfirmationPending("官方订单暂未同步，等待确认（隔离验证）")
        return {"fills":[{"trade_id":"preview-confirmed","shares":10,"price":.4,"fee":.168,"fee_source":"test_fixture"}],"terminal":True,"no_fill":False}
    exchange.reconcile=delayed_read
    managed.last_signal["MAIN"]={"side":"NO_TRADE","state":"waiting","reason":"等待下一次入场信号（隔离验证）"}
bot.managed_live=managed
bot.current_market=market;bot.paper_price={"chainlink":100.2,"chainlink_updated_ms":now*1000}
bot.latest_price=dict(bot.paper_price);bot.paper_quotes=feed.observation()[2];bot.latest_quotes=dict(bot.paper_quotes)
bot.last_signal={"side":"NO_TRADE","reason":"隔离界面验证，不下真实订单"}

class PreviewHandler(Handler):
    def _send_static(self,name,include_body=True):
        if name=="index.html":
            text=(STATIC_DIR/name).read_text().replace("<title>EdgePulse</title>","<title>EdgePulse · 隔离验证</title>")
            text=text.replace("<h1>EdgePulse</h1>","<h1>隔离验证 · 无真实资金</h1>")
            return self._send_html(text,include_body=include_body)
        return super()._send_static(name,include_body=include_body)
    def log_message(self,format,*args):
        pass

server=DashboardServer(("127.0.0.1",0),PreviewHandler,bot)
print(f"UI_PREVIEW=http://127.0.0.1:{server.server_port}/#bot",flush=True)
print(f"TEMP_LEDGERS={TMP}",flush=True)
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    server.server_close();managed.process_lock.release()
