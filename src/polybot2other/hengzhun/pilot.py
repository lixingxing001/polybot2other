"""User-authorized, uncalibrated Hengzhun Paper using an immutable data seed.

The original strict runtime, model artifact and source journal are untouched.
Only the public market adapter is borrowed. No live execution client exists.
"""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
import queue
import sqlite3
import threading

from ..paired_paper import PairedPaperEngine, dec, money, floor, fee, ZERO, ONE, canonical, good_book
from ..paired_paper_service import PairedPaperService, now_ms
from ..paired_v2 import PairV2Engine
from .config import load_config
from .contracts import digest
from .model import ModelUnavailable
from .pilot_data import PilotPrior, SPOT, TWAP

PILOT_CONFIG={'budget':'5.00','max_markets':1,'daily_loss_limit':'10.00','experiment_loss_limit':'15.00',
              'source_age_ms':3000,'receive_age_ms':2000,'arrival_ms':2000,'depth_participation':'0.25',
              'entry_start_seconds':210,'entry_end_remaining_seconds':30,'confirmation_ms':12000,'claim_ms':60000}


class PilotEngine(PairV2Engine):
    strategy_id='HENGZHUN_V1_EXISTING_DATA_PAPER'
    display_name='衡准 V1 · 现有数据试验'

    def __init__(self,path,*,at_ms=0):
        super().__init__(path,config=PILOT_CONFIG,at_ms=at_ms)
        self.model_version='hengzhun-v1-uncalibrated-paper-1'
        self.prediction=None;self.input_error='正在载入已有价格数据';self.failure_counts={}
        with self.tx():
            self.conn.execute('CREATE TABLE IF NOT EXISTS pilot_state(key TEXT PRIMARY KEY,value TEXT)')
            self.conn.execute("INSERT OR IGNORE INTO pilot_state VALUES('nav_high_water','100')")
            self.conn.execute('CREATE TABLE IF NOT EXISTS pilot_prices(channel TEXT,source_at INTEGER,received_at INTEGER,value TEXT,PRIMARY KEY(channel,source_at))')
            self.conn.execute('CREATE TABLE IF NOT EXISTS pilot_decisions(id INTEGER PRIMARY KEY,at INTEGER,market TEXT,code TEXT,json TEXT)')
        self.last_saved_decision=None

    def _rescue(self,g,now):
        # Hengzhun holds its one selected direction to the official result.
        return

    def tape(self,*args):
        # No maker queue or adverse-cross model in this strategy.
        return

    def _adverse_cross(self,*args):
        return

    def add_book(self,b,at):
        if not isinstance(b,dict) or not b.get('token_id'):return
        try:
            if b['source_ms']>at or b['received_ms']>at:return
        except (KeyError,TypeError):return
        ring=self.history[str(b['token_id'])]
        if not ring or (b.get('source_ms'),b.get('received_ms'))!=(ring[-1].get('source_ms'),ring[-1].get('received_ms')):
            ring.append(deepcopy(b))

    def book_at(self,token,at,after=None):
        rows=[b for b in self.history[str(token)] if b['source_ms']<=at and b['received_ms']<=at
              and (after is None or b['received_ms']>after)]
        if not rows:return None
        latest=max(rows,key=lambda b:(b['source_ms'],b['received_ms']))
        return latest if good_book(latest,token,at,self.config) else None

    def _redeem(self,g,now):
        if g['phase']=='RESULT_CONFLICT':return
        super()._redeem(g,now)
        if not g['closed'] and any(dec(q)>0 for q in g['qty'].values()) and g['phase']!='RESULT_CONFLICT':
            g['phase']='MATCHED_WAIT' if self._pending(g['id']) else 'WAIT_RESULT' if now>=g['ends_at'] else 'HOLD_TO_FINAL'

    def _activate(self,g,o,now):
        if o['created']<=self.last_gap_at<=o['active_at']:
            o.update(status='DONE',terminal_reason='提交后发生行情缺口，无法验证到达成交；本期不重试')
            self.save_order(o);return
        rule=self.rules.get(g['id'])
        if (not rule or now-rule.get('checked_at',0)>45000 or any(rule[k]!=o['execution_rules'][k]
                for k in ['tick_size','min_order_size','fee_rate','fee_exponent'])):
            o.update(status='DONE',terminal_reason='到达时市场参数已变化或过期；本期不重试')
            self.save_order(o);return
        if dec(o['quantity'])<dec(rule['min_order_size']) or dec(o['price'])%dec(rule['tick_size']):
            o.update(status='DONE',terminal_reason='到达时精度或最小份额未满足');self.save_order(o);return
        return super()._activate(g,o,now)

    def reason(self,code,message,at):
        changed=(code,message)!=(self.last_code,self.last_reason)
        super().reason(code,message,at)
        if changed and hasattr(self,'failure_counts'):
            self.failure_counts[code]=self.failure_counts.get(code,0)+1

    def _quote_new(self,now,market,books,rules):
        if self.entry_pause_reason or not self.config['enabled']:
            self.reason('PAUSED',self.entry_pause_reason or '已暂停新开仓，已有持仓继续结算',now);return
        if self.conn.execute("SELECT 1 FROM pair_groups WHERE json_extract(json,'$.phase')='RESULT_CONFLICT'").fetchone():
            self.reason('RESULT_CONFLICT','官方结果冲突尚待核验，保留资金与记录',now);return
        if not market or not rules or now-rules.get('checked_at',0)>45000:
            self.reason('MARKET_LOADING','核对本期资产、费用和最低下单量，恢复后自动继续',now);return
        cid=market['condition_id']
        if self.conn.execute('SELECT 1 FROM pair_groups WHERE id=?',(cid,)).fetchone():
            self.reason('MARKET_CONSUMED','本期已有一次意图；持仓等待官方结算，下一期重新判断',now);return
        remaining=(market['ends_at']-now)/1000
        if not 30<=remaining<=90 or remaining-self.config['arrival_ms']/1000<25:
            self.reason('WINDOW_WAIT','等待本期剩余 30–90 秒的衡准入场窗口',now);return
        if now<self.gap_until:
            self.reason('RECOVERING','行情重新同步，随后自动恢复',now);return
        if self.input_error or not self.prediction:
            self.reason('INPUT_WAIT',self.input_error or '等待先验计算',now);return
        p=self.prediction
        if not 0<=now-p['decision_at']<=1000 or not 0<=now-p['source_at']<=3000:
            self.reason('PRICE_STALE','先验输入已过期，等待新价格',now);return
        tokens={'Up':market['up_token'],'Down':market['down_token']}
        if not all(good_book(books.get(s,{}),t,now,self.config) for s,t in tokens.items()):
            self.reason('BOOK_WAIT','等待两侧有效实时盘口',now);return
        if abs(books['Up']['source_ms']-books['Down']['source_ms'])>500:
            self.reason('BOOK_SKEW','双侧盘口时间差超过 500ms',now);return
        active=self.groups();risk=self.risk_snapshot(now)
        unresolved=[g for g in active if self.orders(g['id'],active=True) or self._pending(g['id']) or any(dec(q)>0 for q in g['qty'].values())]
        committed=self.reserved()+sum((sum((dec(v) for v in g['cost'].values()),ZERO)
                    if dec(g['claim'])==0 else dec(g['spent']) for g in active),ZERO)
        if len(unresolved)>=1 or len(active)>=2 or committed+dec(self.config['budget'])>10:
            self.reason('POSITION_LIMIT','上一笔风险或待领取资金仍占额度，继续管理已有记录',now);return
        if not risk['new_group_allowed']:
            self.reason('RISK_BUDGET','亏损与未决风险占用额度，已有持仓继续结算',now);return
        budget=dec(self.config['budget'])
        if dec(self.wallet()['cash'])-self.reserved()<budget:
            self.reason('CASH_WAIT','可用 Paper 现金不足单笔预算',now);return
        candidates=[];details=[];tick=dec(rules['tick_size'])
        for side in tokens:
            b=books[side];ask=min(dec(x['price']) for x in b['asks']);bid=max(dec(x['price']) for x in b['bids'])
            limit=min(Decimal('.70'),ask+tick);limit=floor(limit,tick)
            if ask<Decimal('.20') or ask>limit or ask-bid>Decimal('.02'):
                details.append({'side':side,'reason':'PRICE_OR_SPREAD','ask':money(ask),'spread':money(ask-bid)});continue
            guard=max(ZERO,dec(p['p_raw_up'] if side=='Up' else p['p_raw_down'])-Decimal('.02'))
            # Reserve fee rounding for every observed level, with extra room for
            # finer price levels at arrival. Full all-in cost stays <= $5.
            rounding=Decimal('.00001')*max(1,int(limit/tick)+1)
            q=floor((budget-rounding)/(limit+fee(ONE,limit,rules)),'.01')
            reserve=fee(q,limit,rules)+rounding
            cost=q*limit+reserve
            if q<=0 or q<dec(rules['min_order_size']) or q*limit<ONE:
                details.append({'side':side,'reason':'MINIMUM_SIZE'});continue
            unit=cost/q;edge=guard-unit;roi=edge/unit
            row={'side':side,'guard':money(guard),'limit':money(limit),'quantity':money(q),'fee_reserve':money(reserve),
                 'cost':money(cost),'edge':money(edge),'roi':money(roi),'value':money(q*edge)}
            details.append(row)
            if edge>=Decimal('.03') and roi>=Decimal('.05'):candidates.append(row)
        self.last_decision={'at':now,'market':market['round_id'],'prior':deepcopy(p),'candidates':details,'probability_basis':'UNCALIBRATED_PRIOR_MINUS_0_02'}
        key=(cid,p['source_at'],digest(details))
        if key!=self.last_saved_decision:
            self.conn.execute('INSERT INTO pilot_decisions(at,market,code,json) VALUES(?,?,?,?)',
                              (now,cid,'EVALUATED',canonical(self.last_decision)));self.last_saved_decision=key
        if not candidates:
            if all(x.get('reason')=='PRICE_OR_SPREAD' for x in details):
                prices=' / '.join(f"{x['side']} 卖一 ${x['ask']}、价差 ${x['spread']}" for x in details)
                self.reason('PRICE_FILTER',prices+'；等待 $0.20–$0.70 价格且价差不超过 $0.02',now)
            elif all(x.get('reason') for x in details) and any(x.get('reason')=='MINIMUM_SIZE' for x in details):
                self.reason('MINIMUM_SIZE','合适一侧的 $5 含费预算不足实际最低下单量，不自动加金额',now)
            else:self.reason('EDGE_WAIT','已有数据先验未达到含费净优势 0.03 / 收益率 5%，继续观察',now)
            return
        candidates.sort(key=lambda x:dec(x['value']),reverse=True)
        if len(candidates)>1 and abs(dec(candidates[0]['value'])-dec(candidates[1]['value']))<=Decimal('1e-9'):
            self.reason('VALUE_TIE','两侧价值相近，不强选方向',now);return
        pick=candidates[0]
        g={'id':cid,'market':market['round_id'],'slug':market.get('slug',market['round_id']),'created':now,'ends_at':market['ends_at'],
           'tokens':tokens,'rules':deepcopy(rules),'budget':money(budget),'spent':'0','qty':{'Up':'0','Down':'0'},'cost':{'Up':'0','Down':'0'},
           'pnl':'0','fees':'0','paired':'0','claim':'0','claim_at':None,'first_fill':None,'result':None,'quality_ok':True,'closed':False,
           'phase':'PENDING','retry_after':0,'rescue_attempts':0,'selected_side':pick['side'],'signal':deepcopy(self.last_decision),
           'experiment':'UNCALIBRATED_EXISTING_DATA','seed_manifest_sha256':p['seed_manifest_sha256']}
        self.save_group(g)
        self.new_order(g,pick['side'],dec(pick['limit']),dec(pick['quantity']),now,kind='ENTRY',fee_reserve=dec(pick['fee_reserve']))
        self.reason('SUBMITTED',f"{pick['side']} 模拟限价单已提交，按到达盘口撮合",now)

    def tick(self,now,market=None,books=None,rules=None,**kwargs):
        with self.tx():
            for b in (books or {}).values():self.add_book(b,now)
            nav=self.nav(now)
            if nav is not None:
                high=max(dec(self.conn.execute("SELECT value FROM pilot_state WHERE key='nav_high_water'").fetchone()[0]),nav)
                self.conn.execute("UPDATE pilot_state SET value=? WHERE key='nav_high_water'",(money(high),))
                if high>0 and nav/high<=Decimal('.85'):
                    self.conn.execute('INSERT OR IGNORE INTO pair_v2_halt VALUES(1,?,?)',('DRAWDOWN_15_PERCENT',now))
            super().tick(now,market,books,rules,**kwargs)


class HengzhunPilotService(PairedPaperService):
    mode='PAPER'
    capture_baseline=False

    def __init__(self,path,feed,*,seed,**kwargs):
        self.path=Path(path);self.path.mkdir(parents=True,exist_ok=True)
        self.prior=PilotPrior(seed,now_ms());self.seed_path=Path(seed);self.record_summary=self.prior.manifest
        super().__init__(path,feed,engine_factory=PilotEngine,listener_key='hengzhun-pilot',**kwargs)
        with self.engine.tx():
            old=self.engine.conn.execute("SELECT value FROM pilot_state WHERE key='seed_hash'").fetchone()
            if old and old[0]!=self.prior.manifest['manifest_sha256']:raise ValueError('SEED_CHANGED_CREATE_NEW_EXPERIMENT')
            self.engine.conn.execute("INSERT OR IGNORE INTO pilot_state VALUES('seed_hash',?)",(self.prior.manifest['manifest_sha256'],))
            for s,r,v in self.engine.conn.execute('SELECT source_at,received_at,value FROM pilot_prices WHERE channel=? AND source_at>=?',(SPOT,now_ms()-780000)):
                self.prior.add(s,r,float(v))
        self.last_reference_poll=0;self.last_reference_source=0;self.latest_prior=None
        self.last_predict_error=None;self.view_lock=threading.RLock()

    def capture(self,kind,payload,received_ms,raw_text=None):
        if kind=='BOOK_HEALTH' or (kind=='BOOK' and payload.get('event_type')=='tick_size_change'):
            return super().capture(kind,payload,received_ms,raw_text)

    def _load_live_prices(self,now):
        if now-self.last_reference_poll<750:return
        reference=self.feed.references
        with reference.lock:
            rows=reference.conn.execute('SELECT channel,source_ms,received_ms,exact_price FROM reference_ticks '
                'WHERE source_ms>=? AND source_ms<=? AND received_ms<=? AND channel IN (?,?) ORDER BY source_ms',
                (max(now-780000,self.last_reference_source-2000),now,now,SPOT,TWAP)).fetchall()
        with self.engine.tx():
            for channel,s,r,v in rows:
                self.engine.conn.execute('INSERT OR IGNORE INTO pilot_prices VALUES(?,?,?,?)',(channel,int(s),int(r),str(v)))
                if channel==SPOT:self.prior.add(int(s),int(r),float(v))
                self.last_reference_source=max(self.last_reference_source,int(s))
        self.last_reference_poll=now

    def _run(self):
        while not self.stop_event.is_set():
            now=now_ms()
            try:
                market,reference,books,_=self.feed.observation();m=asdict(market) if market else None
                if m:m.update(started_at=int(market.started_at*1000),ends_at=int(market.ends_at*1000))
                with self.lock:
                    changed=(m or {}).get('condition_id')!=(self.market or {}).get('condition_id');self.market=m
                    rules=deepcopy(self.rules.get(m['condition_id'])) if m else None
                if changed:self.rule_wake.set()
                self._load_live_prices(now)
                prediction=None;input_error=None
                try:
                    spot=(reference.get('latest') or {}).get(SPOT)
                    if not m or not spot or reference.get('state')!='streaming':raise ModelUnavailable('PRICE_MISSING')
                    opening=self.feed.references.opening(market.started_at)
                    if opening is None or abs(float(opening)-float(m['target_price']))>.01:
                        raise ModelUnavailable('OPENING_REFERENCE_UNVERIFIED')
                    prediction=self.prior.predict(m,spot,now)
                except (ValueError,KeyError) as exc:
                    input_error=str(exc);self.last_predict_error=input_error
                self.latest_prior=prediction
                with self.engine.tx():
                    while True:
                        try:kind,payload,received=self.pending.get_nowait()
                        except queue.Empty:break
                        if kind=='BOOK_HEALTH':self.engine.gap(now,'盘口重连，自动同步')
                        else:
                            self.engine.gap(now,'市场精度改变，重新核验')
                            with self.lock:
                                if m:self.rules.pop(m['condition_id'],None);self.rule_at.pop(m['condition_id'],None)
                            rules=None;self.rule_wake.set()
                    self.engine.prediction=prediction;self.engine.input_error=input_error
                    pause='全局 Paper 已暂停，已有持仓继续管理' if self.entries_paused() else None
                    self.engine.tick(now,m,books,rules,entry_pause_reason=pause)
                self.error=None
            except Exception as exc:
                self.error=f'衡准 Paper 处理重试：{type(exc).__name__}: {str(exc)[:140]}'
            self.stop_event.wait(.1)

    def snapshot(self):
        with self.view_lock:
            d=super().snapshot();m=self.prior.manifest
            with self.engine.lock:
                failures=[{'code':k,'count':v} for k,v in self.engine.failure_counts.items()]
                for group in d['groups']:
                    buys=[json.loads(r[0]) for r in self.engine.conn.execute('SELECT json FROM pair_fills WHERE group_id=?',(group['id'],))]
                    buys=[f for f in buys if f['action']=='BUY']
                    quantity=sum((dec(f['quantity']) for f in buys),ZERO)
                    notional=sum((dec(f['cash'])-dec(f['fee']) for f in buys),ZERO)
                    group['buy_shares']=money(quantity)
                    group['average_buy_price']=money(notional/quantity) if quantity else None
                    group['settlement_price']=('1' if group['result']==group['selected_side'] else '0') if group['result'] else None
                    order=next((o for o in d['orders'] if o['group_id']==group['id']),None)
                    group['order_status']=order['status'] if order else None
                    group['order_reason']=order['terminal_reason'] if order else None
            return {**d,'pilot':True,'mode':'PAPER','live_execution_enabled':False,'model_status':'UNFITTED',
                'research_status':'EXPERIMENTAL_UNCALIBRATED','recording_mode':'COMPACT_PAPER_EVIDENCE',
                'seed':m,'config':load_config(),'last_prior':deepcopy(self.latest_prior),'formal_paper_qualified':False,
                'wallet':{'initial_cash':d['initial_cash'],'nav':d['nav'],'cash_available':d['available_cash'],
                          'cash_reserved':d['reserved'],'claim_receivable':d['claim_receivable'],'realized_pnl':d['realized_pnl'],
                          'open_risk_slots_used':d['active_markets']},
                'trade_stats':{'settled_count':d['completed_markets'],'win_rate':d['win_rate']},
                'reason_codes':[d['reason_code']],'failure_counts':failures,
                'note':'按用户指示使用现有数据提前开展未校准 Paper；不满足原设计正式训练验证标准。旧原始库完整保留，当前只记录计算与交易所需证据。'}

    def update_settings(self,payload):
        raise ValueError('衡准试验固定 $5；不接受实盘或其他策略设置')
