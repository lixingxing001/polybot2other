"""Read-only public data adapter for 双翼配对 V1. Never imports a trading SDK."""
from collections import deque
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import queue
import secrets
import threading
import time
from urllib.parse import quote

import httpx

from .paired_paper import PairedPaperEngine, dec, money, canonical, digest


def now_ms(): return int(time.time()*1000)


def verify_rules(market, raw, gamma, book_contracts=None):
    """Map facts from current public responses, with no unknown-as-zero defaults."""
    cid=market['condition_id']
    if raw.get('c')!=cid or raw.get('v')!='v1' or gamma.get('version')!='v1':
        raise ValueError('市场协议或 condition 标识不匹配')
    expected={'Up':str(market['up_token']),'Down':str(market['down_token'])}
    tokens={t.get('o'):str(t.get('t')) for t in raw.get('t',[])}
    sides=json.loads(gamma['outcomes']) if isinstance(gamma.get('outcomes'),str) else gamma.get('outcomes')
    assets=json.loads(gamma['clobTokenIds']) if isinstance(gamma.get('clobTokenIds'),str) else gamma.get('clobTokenIds')
    if len(raw.get('t',[]))!=2 or len(sides or [])!=2 or len(assets or [])!=2 or tokens!=expected or dict(zip(sides or [],assets or []))!=expected or set(sides or [])!={'Up','Down'}:
        raise ValueError('不是已核对的同一期互补 Up/Down 份额')
    if gamma.get('conditionId')!=cid or gamma.get('slug')!=market['round_id'] or raw.get('ao') is not True or gamma.get('acceptingOrders') is not True:
        raise ValueError('市场未开放新订单')
    if market['ends_at']-market['started_at']!=300000:
        raise ValueError('市场窗口不是五分钟')
    if 'btc-usd-twap-60s' not in str(gamma.get('resolutionSource','')):
        raise ValueError('结算规则不属于 BTC 同一期 Up/Down')
    fd=raw.get('fd');gs=gamma.get('feeSchedule')
    if not isinstance(fd,dict) or not isinstance(gs,dict) or fd.get('to') is not True or gs.get('takerOnly') is not True:
        raise ValueError('缺少可核对的 maker/taker 费用规则')
    rate,exponent=dec(fd['r']),dec(fd['e']);tick=dec(raw['mts']);minimum=dec(raw['mos'])
    if rate!=dec(gs['rate']) or exponent!=dec(gs['exponent']) or not 0<=rate<=1 or exponent!=1:
        raise ValueError('费用参数不一致或暂不支持')
    if rate>0 and gamma.get('feesEnabled') is not True:raise ValueError('费用启用标记未核对')
    if tick not in [dec('.1'),dec('.01'),dec('.001'),dec('.0001')] or minimum<=0:
        raise ValueError('价格精度或最低份额无效')
    metadata_note=None
    if tick!=dec(gamma['orderPriceMinTickSize']) or minimum!=dec(gamma['orderMinSize']):
        # Gamma can lag dynamic CLOB precision changes. Prove the executable
        # values using BOTH token books; no invented default or silent bypass.
        if not book_contracts or set(book_contracts)!=set(expected):raise ValueError('市场精度变化，正在核对两侧实际盘口')
        ticks=[tick];sizes=[minimum]
        for side,asset in expected.items():
            b=book_contracts[side]
            if str(b.get('asset_id'))!=asset or b.get('market')!=cid or b.get('version') not in (None,'v1','ctf'):
                raise ValueError('精度核验盘口身份不匹配')
            bt,bs=dec(b['tick_size']),dec(b['min_order_size'])
            if bt not in [dec('.1'),dec('.01'),dec('.001'),dec('.0001')] or bs<=0:raise ValueError('盘口精度无效')
            ticks.append(bt);sizes.append(bs)
        tick=max(ticks);minimum=max(sizes)
        metadata_note='Gamma 精度滞后；已按两侧 CLOB 盘口共同有效的精度/最低量核对'
    return {'market_protocol':'v1','fee_rate':money(rate),'fee_exponent':money(exponent),
            'tick_size':money(tick),'min_order_size':money(minimum),'maker_fee':'0',
            'fee_rounding':'ROUND_UP_5DP_CONSERVATIVE','metadata_note':metadata_note,
            'evidence_hash':digest({'clob':raw,'gamma':gamma,'book_contracts':book_contracts})}


def verified_result(g, raw):
    if (raw.get('conditionId')!=g['id'] or raw.get('slug')!=g['market'] or raw.get('closed') is not True
            or raw.get('umaResolutionStatus')!='resolved'
            or 'btc-usd-twap-60s' not in str(raw.get('resolutionSource',''))): return None
    sides=json.loads(raw['outcomes']) if isinstance(raw.get('outcomes'),str) else raw.get('outcomes')
    prices=json.loads(raw['outcomePrices']) if isinstance(raw.get('outcomePrices'),str) else raw.get('outcomePrices')
    tokens=json.loads(raw['clobTokenIds']) if isinstance(raw.get('clobTokenIds'),str) else raw.get('clobTokenIds')
    if dict(zip(sides or [],tokens or []))!=g['tokens'] or len(prices or [])!=2: return None
    payout=dict(zip(sides,map(dec,prices)))
    # closed or an estimated end time alone is insufficient.
    if set(payout.values())!={dec('0'),dec('1')}: return None
    return next(s for s,p in payout.items() if p==1)


class PairedPaperService:
    def __init__(self, path, feed, *, gamma_url='https://gamma-api.polymarket.com', clob_url='https://clob.polymarket.com', entries_paused=None,
                 engine_factory=PairedPaperEngine, listener_key='paired-paper', frozen=False):
        self.path=Path(path);self.path.mkdir(parents=True,exist_ok=True)
        self.engine=engine_factory(self.path/'paper.sqlite3',at_ms=now_ms())
        self.listener_key=listener_key;self.frozen=frozen
        if frozen and self.engine.config['enabled']:self.engine.settings({'enabled':False},now_ms())
        self.feed=feed;self.gamma_url=gamma_url.rstrip('/');self.clob_url=clob_url.rstrip('/')
        self.entries_paused=entries_paused or (lambda:False)
        self.http=httpx.Client(timeout=httpx.Timeout(5,connect=3),follow_redirects=False)
        self.control_token=secrets.token_urlsafe(32)
        self.pending=queue.Queue(maxsize=10000);self.stop_event=threading.Event();self.thread=None;self.aux=None
        self.lock=threading.RLock();self.rules={};self.rule_at={};self.rule_errors={};self.market=None
        self.error=None;self.drops=0;self.observed_drops=0;self.last_event_receive=None;self.latency_samples=deque(maxlen=1000)
        self.snapshot_cache=None;self.snapshot_at=0;self.rule_wake=threading.Event()

    def capture(self,kind,payload,received_ms,raw_text=None):
        if self.stop_event.is_set(): return
        if kind!='BOOK_HEALTH' and (kind!='BOOK' or payload.get('event_type') not in ('last_trade_price','tick_size_change')): return
        try:self.pending.put_nowait((kind,dict(payload),int(received_ms)))
        except queue.Full:self.drops+=1

    def start(self):
        if self.thread and self.thread.is_alive(): return
        if self.frozen and not self.engine.groups():return
        self.stop_event.clear();self.feed.books.add_listener(self.listener_key,self.capture)
        self.thread=threading.Thread(target=self._run,name='paired-paper',daemon=True)
        self.aux=threading.Thread(target=self._public_loop,name='paired-public-read',daemon=True)
        self.thread.start();self.aux.start()

    def stop(self):
        self.stop_event.set();self.rule_wake.set();self.feed.books.remove_listener(self.listener_key)
        for worker in (self.thread,self.aux):
            if worker and worker is not threading.current_thread():worker.join(timeout=6)
        with self.engine.tx():
            for g in self.engine.groups():
                self.engine.cancel(g,now_ms(),'服务停止；重启继续管理已知持仓');self.engine.save_group(g)
        self.http.close()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                now=now_ms();market,_,books,_=self.feed.observation()
                m=None
                if market:
                    m=asdict(market);m['started_at']=int(market.started_at*1000);m['ends_at']=int(market.ends_at*1000)
                with self.lock:
                    changed=(m or {}).get('condition_id')!=(self.market or {}).get('condition_id');self.market=m
                    rules=deepcopy(self.rules.get(m['condition_id'])) if m else None
                if changed:self.rule_wake.set()
                pause_reason='全局 Paper 或行情已暂停；已有份额继续管理' if self.entries_paused() else None
                with self.engine.tx():
                    if self.drops!=self.observed_drops:
                        self.engine.gap(now,f'成交事件队列丢弃 {self.drops-self.observed_drops} 条；受影响记录已标记')
                        self.observed_drops=self.drops
                    # Books are timestamped evidence, never passive fills. Consume
                    # already-received prints before releasing cancelled reserves.
                    for b in books.values():self.engine.add_book(b,now)
                    for _ in range(1000):
                        try:kind,payload,received=self.pending.get_nowait()
                        except queue.Empty:break
                        if kind=='BOOK_HEALTH':self.engine.gap(now,'公共盘口连接重新同步')
                        elif payload.get('event_type')=='tick_size_change':
                            self.engine.gap(now,'市场 tick 变化，重新核对公开规则')
                            with self.lock:
                                if m:self.rules.pop(m['condition_id'],None);self.rule_at.pop(m['condition_id'],None)
                            self.rule_wake.set()
                        else:
                            self.last_event_receive=received
                            if now-received>3000:
                                self.engine.gap(now,'成交消息处理延迟，未补记历史成交');continue
                            self.engine.tape(payload,received)
                    self.engine.tick(now,m,books,rules,entry_pause_reason=pause_reason)
                self.error=None
            except Exception as exc:
                self.error=f'模拟处理异常（{type(exc).__name__}），自动重试'
                try:self.engine.gap(now_ms(),self.error)
                except Exception:pass
            self.stop_event.wait(.1)

    def _get(self,url,params=None):
        started=time.monotonic();response=self.http.get(url,params=params);received=now_ms()
        response.raise_for_status();raw=response.json();elapsed=(time.monotonic()-started)*1000
        self.latency_samples.append(round(elapsed,1))
        proof={'url':str(response.url),'received_at':received,'sha256':hashlib.sha256(response.content).hexdigest(),'latency_ms':round(elapsed,1)}
        return raw,proof

    def _gamma(self,slug):
        # The collection endpoint defaults to active markets and omits closed
        # ones. The slug endpoint returns terminal markets for reconciliation.
        raw,proof=self._get(self.gamma_url+'/markets/slug/'+quote(str(slug),safe=''))
        rows=raw if isinstance(raw,list) else [raw]
        selected=next((r for r in rows if isinstance(r,dict) and r.get('slug')==slug),None)
        if selected is None:raise ValueError('当期市场元数据暂不可用')
        return selected,proof

    def _public_loop(self):
        result_retry={}
        while not self.stop_event.is_set():
            now=now_ms()
            with self.lock:m=deepcopy(self.market)
            if m and now-self.rule_at.get(m['condition_id'],0)>=30000:
                cid=m['condition_id']
                try:
                    raw,p1=self._get(self.clob_url+'/clob-markets/'+cid);gamma,p2=self._gamma(m['round_id'])
                    book_contracts=None;extra_proofs=[]
                    if dec(raw['mts'])!=dec(gamma['orderPriceMinTickSize']) or dec(raw['mos'])!=dec(gamma['orderMinSize']):
                        book_contracts={}
                        for side,token in (('Up',m['up_token']),('Down',m['down_token'])):
                            book,proof=self._get(self.clob_url+'/book',{'token_id':token})
                            book_contracts[side]=book;extra_proofs.append(proof)
                    rule=verify_rules(m,raw,gamma,book_contracts);rule['checked_at']=now_ms()
                    with self.lock:self.rules[cid]=rule;self.rule_errors.pop(cid,None)
                    with self.engine.tx():self.engine.event('RULES',now_ms(),cid,rules=rule,clob=raw,gamma=gamma,book_contracts=book_contracts,evidence=[p1,p2]+extra_proofs)
                except Exception as exc:
                    text=str(exc) if isinstance(exc,ValueError) else '公开市场规则暂时不可用，自动重试'
                    with self.lock:self.rules.pop(cid,None);self.rule_errors[cid]=text[:180]
                self.rule_at[cid]=now_ms()-20000 if cid in self.rule_errors else now_ms()
            with self.engine.lock:
                pending=[g for g in self.engine.groups() if not g['result'] and now>=g['ends_at'] and now>=result_retry.get(g['id'],0)]
            for g in sorted(pending,key=lambda x:result_retry.get(x['id'],0))[:2]:
                result_retry[g['id']]=now+15000
                try:
                    raw,proof=self._gamma(g['market']);outcome=verified_result(g,raw)
                    if outcome:self.engine.official_result(g['id'],outcome,now_ms(),{'response':raw,'fetch':proof})
                except Exception as exc:
                    with self.engine.tx():self.engine.event('RESULT_WAIT',now_ms(),g['id'],message=f'等待官方最终结果（{type(exc).__name__}）')
            self.rule_wake.wait(1);self.rule_wake.clear()

    def snapshot(self):
        now=now_ms()
        if self.snapshot_cache is not None and now-self.snapshot_at<1000:return deepcopy(self.snapshot_cache)
        result=self.engine.snapshot(now)
        with self.lock:
            cid=(self.market or {}).get('condition_id');rule_error=self.rule_errors.get(cid)
        result.update(running=bool(self.thread and self.thread.is_alive()),error=self.error,
            frozen=self.frozen,
            rules_error=rule_error,control_token=self.control_token,queue_size=self.pending.qsize(),dropped_events=self.drops,
            last_trade_event_at=self.last_event_receive,public_request_latency_ms=list(self.latency_samples)[-1:] or None)
        if self.frozen:
            result.update(reason='V1 已冻结为历史对照，原始亏损和成交记录完整保留',reason_code='FROZEN')
        self.snapshot_cache=deepcopy(result);self.snapshot_at=now
        return result

    def update_settings(self,payload):
        if self.frozen:raise ValueError('V1 已冻结为历史对照，请使用独立的双翼 V2')
        self.engine.settings(payload,now_ms());self.snapshot_at=0
        return self.snapshot()
