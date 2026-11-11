"""Read-only exchange verification against a disposable copy of the live ledger."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3
import tempfile
import time

from polybot2other.config import load_settings
from polybot2other.managed_live import ManagedLiveAccounts
from polybot2other.managed_live_client import ManagedExchange
from polybot2other.managed_live_store import ManagedLiveStore
from polybot2other.optimized_main import StrictTwapMarketClient

ROOT=Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix="polybot-finalization-readonly-") as directory:
    temp=Path(directory)
    with sqlite3.connect(f"file:{ROOT/'data/live-accounts/accounts.sqlite3'}?mode=ro",uri=True) as source,sqlite3.connect(temp/'accounts.sqlite3') as copy:
        source.backup(copy)
    settings=replace(load_settings(),live_trading_runtime_enabled=False,managed_live_dir=temp,db_path=temp/'unused-paper.sqlite3')
    exchange=ManagedExchange(settings)
    request=exchange.http.request
    def only_get(method,*args,**kwargs):
        if method.upper()!="GET":raise AssertionError("Dry run forbids every exchange mutation")
        return request(method,*args,**kwargs)
    exchange.http.request=only_get
    market_client=StrictTwapMarketClient(settings,None)
    def get_json(url,params=None):
        response=exchange.http.get(url,params=params)
        if response.status_code==404:return {}
        response.raise_for_status();return response.json()
    market_client._get_json=get_json
    feed=SimpleNamespace(polymarket=market_client,references=SimpleNamespace())
    store=ManagedLiveStore(temp/'accounts.sqlite3')
    manager=ManagedLiveAccounts(settings,exchange=exchange,feed=feed,store=store)
    assert manager.process_lock.acquire() is None
    original=[o['id'] for o in store.active_orders() if o['state']=='QUARANTINED']
    samples=[]
    try:
        for attempt in range(6):
            manager.wallet=exchange.read_wallet()
            manager.open_orders=exchange.read_open_orders();manager.open_orders_at=time.time()
            manager.reconcile()
            rows=[{k:store.order(ident).get(k) for k in ['id','strategy_id','state','closure_checks','finalized_at','error']} for ident in original]
            samples.append(rows);print(json.dumps({'attempt':attempt+1,'orders':rows},ensure_ascii=False),flush=True)
            if all(row['state']=='EXPIRED_UNFILLED' for row in rows):break
            if attempt<5:time.sleep(16)
        report={'checked_at':time.time(),'network_methods_allowed':['GET'],'live_db_opened_read_only':True,'samples':samples,
                'orders':[store.order(ident) for ident in original],
                'all_original_quarantines_finalized':all(store.order(ident)['state']=='EXPIRED_UNFILLED' for ident in original)}
        (ROOT/'reports/live-finalization-20261102/dry-run-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print('DRY_RUN_FINALIZED',report['all_original_quarantines_finalized'],flush=True)
    finally:
        manager.process_lock.release();store.conn.close();exchange.http.close()
