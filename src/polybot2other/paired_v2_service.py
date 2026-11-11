"""Two isolated Paper ledgers share public observations, never real order keys."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import queue
import sqlite3
import threading
import time

import httpx

from .paired_paper import dec, money, canonical, digest
from .paired_paper_service import PairedPaperService, now_ms
from .paired_v2 import PairV2Engine


class ExitContractError(ValueError):
    pass


def exit_contract(group,raw,book_payloads,received,*,metadata_only=False,received_by_side=None):
    """Exit terms rely on the CLOB itself, not a possibly unavailable Gamma page."""
    if raw.get('c')!=group['id'] or raw.get('v')!='v1' or raw.get('ao') is not True:raise ExitContractError('退出市场身份或交易状态无效')
    tokens={t.get('o'):str(t.get('t')) for t in raw.get('t',[])}
    if len(raw.get('t',[]))!=2 or tokens!=group['tokens']:raise ExitContractError('退出资产标识不匹配')
    fd=raw.get('fd')
    if not isinstance(fd,dict) or fd.get('to') is not True:raise ExitContractError('退出费用合同缺失')
    rate,exponent=dec(fd['r']),dec(fd['e'])
    if not 0<=rate<=1 or exponent!=1:raise ExitContractError('退出费用公式不支持')
    ticks=[dec(raw['mts'])];sizes=[dec(raw['mos'])];views={}
    for side,b in book_payloads.items():
        if b.get('market')!=group['id'] or str(b.get('asset_id'))!=tokens[side] or b.get('version') not in (None,'v1','ctf'):
            raise ExitContractError('退出盘口属于其他资产或协议')
        tick,size=dec(b['tick_size']),dec(b['min_order_size']);ticks.append(tick);sizes.append(size)
        stamp=int(b['timestamp'])
        first_received=(received_by_side or {}).get(side,received)
        if not 0<=received-stamp<=3000 or stamp>first_received or first_received>received:raise ValueError('退出盘口源时间过期或接收时刻无效')
        bid=[{'price':money(x['price']),'size':money(x['size'])} for x in b.get('bids',[]) if dec(x['size'])>0]
        ask=[{'price':money(x['price']),'size':money(x['size'])} for x in b.get('asks',[]) if dec(x['size'])>0]
        if any(not 0<dec(x['price'])<1 for x in bid+ask):raise ValueError('退出盘口价格无效')
        views[side]={'token_id':tokens[side],'source_ms':stamp,'received_ms':first_received,'source':'paired-v2-exit-rest',
            'bids':bid,'asks':ask,'best_bid':money(max((dec(x['price']) for x in bid),default=dec(0))) if bid else None,
            'best_ask':money(min((dec(x['price']) for x in ask),default=dec(1))) if ask else None,
            'tick_size':money(tick),'min_order_size':money(size)}
    if (not views and not metadata_only) or any(t not in (dec('.1'),dec('.01'),dec('.001'),dec('.0001')) for t in ticks) or min(sizes)<=0:
        raise ExitContractError('退出精度或最低份额无效')
    rules={'market_protocol':'v1','fee_rate':money(rate),'fee_exponent':money(exponent),'tick_size':money(max(ticks)),
        'min_order_size':money(max(sizes)),'maker_fee':'0','checked_at':received,
        'fee_rounding':'ROUND_UP_5DP_CONSERVATIVE','evidence_hash':digest({'clob':raw,'books':book_payloads}),
        'verification_scope':'CURRENT_CLOB_EXIT_ONLY'}
    return rules,views


class PairedV2Service(PairedPaperService):
    def __init__(self,path,feed,*,comparison_db=None,**kwargs):
        super().__init__(path,feed,engine_factory=PairV2Engine,listener_key='paired-paper-v2',**kwargs)
        self.stress=PairV2Engine(Path(path)/'stress.sqlite3',at_ms=now_ms(),scenario='STRESS')
        # Same preregistered rules, but independent capital and scenario fills.
        with self.stress.tx():
            self.stress.config=deepcopy(self.engine.config)
            self.stress.conn.execute('UPDATE pair_settings SET json=? WHERE id=1',(canonical(self.stress.config),))
        self.exit_http=httpx.Client(timeout=httpx.Timeout(3,connect=2),follow_redirects=False)
        self.exit_queue=queue.Queue(maxsize=100);self.exit_worker=None;self.exit_errors={};self.last_exit_check={}
        self._fee_cache={}
        self.comparison_db=Path(comparison_db) if comparison_db else None
        self.comparison_cache=None;self.comparison_at=0
        self.view_lock=threading.RLock()

    def start(self):
        super().start()
        if self.exit_worker and self.exit_worker.is_alive():return
        self.exit_worker=threading.Thread(target=self._exit_loop,name='paired-v2-exit-data',daemon=True);self.exit_worker.start()

    def stop(self):
        super().stop()
        if self.exit_worker:self.exit_worker.join(timeout=7)
        self.exit_http.close()
        with self.stress.tx():
            for g in self.stress.groups():self.stress.cancel(g,now_ms(),'服务停止，保留已知模拟成交');self.stress.save_group(g)

    def _engines(self):return (self.engine,self.stress)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                now=now_ms();market,_,books,_=self.feed.observation();m=None
                if market:
                    m=asdict(market);m['started_at']=int(market.started_at*1000);m['ends_at']=int(market.ends_at*1000)
                with self.lock:
                    changed=(m or {}).get('condition_id')!=(self.market or {}).get('condition_id');self.market=m
                    rules=deepcopy(self.rules.get(m['condition_id'])) if m else None
                if changed:self.rule_wake.set()
                events=[]
                for _ in range(1000):
                    try:events.append(self.pending.get_nowait())
                    except queue.Empty:break
                trades=[received for kind,payload,received in events if kind=='BOOK' and payload.get('event_type')=='last_trade_price']
                if trades:self.last_event_receive=max(trades)
                exits=[]
                for _ in range(20):
                    try:exits.append(self.exit_queue.get_nowait())
                    except queue.Empty:break
                pause='全局 Paper 或行情已暂停；退出通道继续管理持仓' if self.entries_paused() else None
                for engine in self._engines():
                    with engine.tx():
                        if self.drops!=self.observed_drops:engine.gap(now,'公共成交流丢弃，受影响市场已标记')
                        for item in exits:
                            if item['kind']=='rules':
                                engine.set_exit_rules(item['cid'],item['rules'],item['books'],now)
                                engine.event('EXIT_TERMS',item['at'],item['cid'],rules=item['rules'],books=item['books'])
                            elif item['kind']=='invalid':engine.invalidate_exit_rules(item['cid'],now,item['reason'])
                            elif item['kind']=='result':engine.official_result(item['cid'],item['outcome'],item['at'],item['evidence'])
                        for b in books.values():engine.add_book(b,now)
                        for kind,payload,received in events:
                            if kind=='BOOK_HEALTH':engine.gap(now,'公共盘口连接重新同步')
                            elif payload.get('event_type')=='tick_size_change':engine.gap(now,'价格精度变化，重新核验退出与新报价')
                            elif now-received>3000:engine.gap(now,'成交消息处理过迟，未补记成交')
                            else:engine.tape(payload,received)
                        engine.tick(now,m,books,rules,entry_pause_reason=pause)
                self.observed_drops=self.drops;self.error=None
                for kind,payload,_ in events:
                    if kind=='BOOK' and payload.get('event_type')=='tick_size_change':
                        with self.lock:
                            if m:self.rules.pop(m['condition_id'],None);self.rule_at.pop(m['condition_id'],None)
                        self.rule_wake.set()
            except Exception as exc:
                self.error=f'V2 模拟处理异常（{type(exc).__name__}），正在恢复'
                for engine in self._engines():
                    try:engine.gap(now_ms(),self.error)
                    except Exception:pass
            self.stop_event.wait(.1)

    def _exit_get(self,url,params=None):
        response=self.exit_http.get(url,params=params);response.raise_for_status();return response.json(),now_ms()

    def _exit_loop(self):
        last={};result_retry={}
        while not self.stop_event.is_set():
            now=now_ms();groups={}
            for engine in self._engines():
                with engine.lock:
                    for g in engine.groups():
                        if g['first_fill'] is not None and any(dec(v)>0 for v in g['qty'].values()):
                            previous=groups.get(g['id'])
                            if previous:
                                for side in ('Up','Down'):previous['qty'][side]=money(max(dec(previous['qty'][side]),dec(g['qty'][side])))
                            else:groups[g['id']]=deepcopy(g)
            self.exit_errors={cid:message for cid,message in self.exit_errors.items() if cid in groups}
            self._fee_cache={cid:value for cid,value in self._fee_cache.items() if cid in groups}
            for cid,g in sorted(groups.items(),key=lambda item:last.get(item[0],0))[:2]:
                if self.stop_event.is_set():break
                if now>=g['ends_at']:
                    if now<result_retry.get(cid,0):continue
                    result_retry[cid]=now+15000
                    try:
                        from .paired_paper_service import verified_result
                        raw,received=self._exit_get(self.gamma_url+'/markets/slug/'+g['market'])
                        outcome=verified_result(g,raw)
                        if outcome:self.exit_queue.put_nowait({'kind':'result','cid':cid,'outcome':outcome,'at':received,'evidence':raw})
                    except Exception as exc:self.exit_errors[cid]=f'终局核验重试中（{type(exc).__name__}）'
                    continue
                if now-last.get(cid,0)<700:continue
                last[cid]=now
                try:
                    cached=self._fee_cache.get(cid)
                    if not cached or now-cached[0]>8000:
                        raw,checked=self._exit_get(self.clob_url+'/clob-markets/'+cid);self._fee_cache[cid]=(checked,raw)
                    else:checked,raw=cached
                    metadata,_=exit_contract(g,raw,{},checked,metadata_only=True)
                    self.exit_queue.put_nowait({'kind':'rules','cid':cid,'rules':metadata,'books':{},'at':checked})
                    raw_books={};receipts={}
                    # Fetch the larger leg first; the exit feed has no dependency
                    # on Gamma or on the new-entry discovery worker.
                    sides=sorted(g['tokens'],key=lambda s:dec(g['qty'][s]),reverse=True)
                    for side in sides:
                        if dec(g['qty'][side])<=0 and len(raw_books)>0:continue
                        b,received=self._exit_get(self.clob_url+'/book',{'token_id':g['tokens'][side]})
                        raw_books[side]=b;receipts[side]=received
                    latest=max(receipts.values())
                    rule,views=exit_contract(g,raw,raw_books,latest,received_by_side=receipts)
                    # Fee age is its own clock; a new book cannot refresh it.
                    rule['checked_at']=min(checked,latest)
                    self.exit_queue.put_nowait({'kind':'rules','cid':cid,'rules':rule,'books':views,'at':latest})
                    self.last_exit_check[cid]=latest;self.exit_errors.pop(cid,None)
                except ExitContractError as exc:
                    self._fee_cache.pop(cid,None)
                    self.exit_errors[cid]=str(exc)[:160]
                    try:self.exit_queue.put_nowait({'kind':'invalid','cid':cid,'reason':str(exc)[:160]})
                    except queue.Full:pass
                except ValueError as exc:self.exit_errors[cid]=str(exc)[:160]
                except Exception as exc:
                    # A transient fetch failure does not delete the last proved
                    # exit terms. They retain their original, bounded validity.
                    self.exit_errors[cid]=f'独立退出数据重试中（{type(exc).__name__}）'
            self.stop_event.wait(.2)

    def snapshot(self):
        with self.view_lock:return self._snapshot_locked()

    def _snapshot_locked(self):
        now=now_ms()
        if self.snapshot_cache is not None and now-self.snapshot_at<1000:return deepcopy(self.snapshot_cache)
        result=super().snapshot();stress=self.stress.snapshot(now)
        result.update(stress=stress,exit_errors=deepcopy(self.exit_errors),exit_worker_running=bool(self.exit_worker and self.exit_worker.is_alive()),
            comparison_note='两个独立 $100 模拟情景，收益不相加；压力情景未通过时不能宣称收益改善',
            source_scope='TAPE 主账本 + ASK_THROUGH 压力账本',frozen=False)
        if self.comparison_cache is None or now-self.comparison_at>=10000:
            rows=[]
            for label,engine in [('V2 成交带',self.engine),('V2 穿价压力',self.stress)]:
                with engine.lock:
                    groups=[g for g in engine.groups(False) if g['closed'] and g['first_fill'] is not None]
                    pnl=sum((dec(g['pnl']) for g in groups),dec(0));spent=sum((dec(g['spent']) for g in groups),dec(0))
                    rows.append({'name':label,'available':True,'completed':len(groups),'pnl':money(pnl),'spent':money(spent),'roi':money(pnl/spent) if spent else None})
            baseline={'name':'主账户优化版','available':False,'reason':'对照数据不可用'}
            if self.comparison_db:
                try:
                    connection=sqlite3.connect(self.comparison_db.resolve().as_uri()+'?mode=ro',uri=True)
                    try:
                        trades=connection.execute('SELECT stake,pnl FROM trades WHERE opened_at>=? AND settled_at<=? AND pnl IS NOT NULL',
                            (self.engine.identity['started_at']/1000,now/1000)).fetchall()
                    finally:connection.close()
                    pnl=sum((dec(p) for _,p in trades),dec(0));spent=sum((dec(s) for s,_ in trades),dec(0))
                    baseline.update(available=True,completed=len(trades),pnl=money(pnl),spent=money(spent),roi=money(pnl/spent) if spent else None,reason=None)
                except (OSError,sqlite3.Error,ValueError):pass
            rows.append(baseline)
            self.comparison_cache={'started_at':self.engine.identity['started_at'],'rows':rows,
                'scope':'仅计 V2 启动之后新开仓且已完成的记录；每笔金额和执行模型不同，需同时看投入和收益率'}
            self.comparison_at=now
        result['comparison']=deepcopy(self.comparison_cache)
        self.snapshot_cache=deepcopy(result);self.snapshot_at=now;return result

    def update_settings(self,payload):
        with self.engine.lock,self.stress.lock:
            old={k:self.engine.config[k] for k in ('enabled','budget','max_markets','daily_loss_limit')}
            self.engine.settings(payload,now_ms())
            try:self.stress.settings(payload,now_ms())
            except Exception:
                self.engine.settings(old,now_ms());raise
        self.snapshot_at=0;return self.snapshot()
