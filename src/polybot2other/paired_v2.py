"""双翼 V2: isolated Paper with reserved loss budget and independent exit checks.

The two execution scenarios use separate funded ledgers. Trade-print simulation
is not evidence of live fills; adverse-cross results remain visible as stress.
"""
from collections import deque
from copy import deepcopy
from decimal import Decimal
import json
import uuid

from .paired_paper import (PairedPaperEngine, DEFAULTS, ZERO, ONE, ACTIVE,
                          dec, money, floor, fee, canonical, digest, good_book)

V2_DEFAULTS = {**DEFAULTS, 'budget':'5.00', 'max_markets':5, 'daily_loss_limit':'10.00',
    'pair_price_cap':'0.96', 'quote_ttl_ms':4000, 'imbalance_ms':2000,
    'entry_start_seconds':30, 'entry_end_remaining_seconds':120,
    'entry_source_age_ms':1500, 'entry_receive_age_ms':1000,
    'stability_span_ms':2000, 'stability_lookback_ms':4000,
    'maximum_mid_range':'0.02', 'minimum_quote_price':'0.30', 'maximum_quote_price':'0.70',
    'maximum_spread':'0.03', 'maximum_single_leg_loss':'0.40',
    'exit_rule_max_age_ms':15000, 'exit_retry_ms':2000, 'emergency_age_ms':15000,
    'experiment_loss_limit':'10.00'}


class PairV2Engine(PairedPaperEngine):
    strategy_id='PAIRED_MAKER_V2'
    display_name='双翼配对 V2'
    model_version='paired-paper-v2.0'

    def __init__(self,path,*,config=None,at_ms=0,scenario='TAPE'):
        if scenario not in ('TAPE','STRESS'):raise ValueError('未知模拟情景')
        self.scenario=scenario;self.exit_rules={};self.exit_invalid={};self.stress_crosses=0;self.last_gap_at=0
        self.model_version='paired-paper-v2.0-'+scenario.lower()
        super().__init__(path,config={**V2_DEFAULTS,**(config or {})},at_ms=at_ms)
        with self.tx():
            self.conn.execute('CREATE TABLE IF NOT EXISTS pair_v2_identity(id INTEGER PRIMARY KEY,json TEXT)')
            old=self.conn.execute('SELECT json FROM pair_v2_identity WHERE id=1').fetchone()
            if old:
                identity=json.loads(old[0])
                if identity['scenario']!=scenario or identity['strategy_id']!=self.strategy_id:raise ValueError('模拟账户身份不匹配')
            else:
                if self.conn.execute('SELECT count(*) FROM pair_groups').fetchone()[0]:raise ValueError('不能把 V1 账本升级成 V2')
                identity={'run_id':uuid.uuid4().hex,'strategy_id':self.strategy_id,'scenario':scenario,'started_at':at_ms}
                self.conn.execute('INSERT INTO pair_v2_identity VALUES(1,?)',(canonical(identity),))
            self.identity=identity
            self.conn.execute('CREATE TABLE IF NOT EXISTS pair_v2_exit_rules(cid TEXT PRIMARY KEY,json TEXT)')
            self.conn.execute('CREATE TABLE IF NOT EXISTS pair_v2_halt(id INTEGER PRIMARY KEY,reason TEXT,at INTEGER)')
            for row in self.conn.execute('SELECT cid,json FROM pair_v2_exit_rules'):
                self.exit_rules[row[0]]=json.loads(row[1])

    def set_exit_rules(self,cid,rules,books,now):
        with self.tx():
            payload=deepcopy(rules)
            if payload['checked_at']>now:raise ValueError('退出核验时刻不能来自未来')
            self.exit_rules[cid]=payload
            self.exit_invalid.pop(cid,None)
            self.conn.execute('INSERT OR REPLACE INTO pair_v2_exit_rules VALUES(?,?)',(cid,canonical(payload)))
            for book in books.values():self.add_book(book,now)

    def invalidate_exit_rules(self,cid,now,reason):
        with self.tx():
            self.exit_invalid[cid]=reason
            self.event('EXIT_RULE_INVALID',now,cid,message=reason)

    def settings(self,changes,now):
        # Preserve one atomic configuration snapshot for the quote/risk check.
        with self.tx():return super().settings(changes,now)

    def gap(self,now,message):
        self.last_gap_at=max(self.last_gap_at,now)
        return super().gap(now,message)

    def _pending_buys(self,gid,side):
        return sum((dec(json.loads(r[0])['quantity']) for r in self.conn.execute(
            "SELECT json FROM pair_fills WHERE group_id=? AND status='MATCHED' AND json_extract(json,'$.action')='BUY' AND json_extract(json,'$.side')=?",
            (gid,side))),ZERO)

    def confirmed_inventory(self,g):
        return {s:max(ZERO,dec(g['qty'][s])-self._pending_buys(g['id'],s)) for s in ('Up','Down')}

    def risk_snapshot(self,now,next_budget=None):
        """No profit offset. Fully paid/confirmed pairs alone receive risk credit."""
        gross=sum((max(ZERO,-dec(r[0])) for r in self.conn.execute('SELECT pnl FROM pair_profit WHERE at>?',(now-86400000,))),ZERO)
        positions=ZERO;pending_sell=ZERO
        for g in self.groups():
            confirmed=self.confirmed_inventory(g)
            credit=min(confirmed.values()) if g['phase']!='RESULT_CONFLICT' else ZERO
            positions+=max(ZERO,sum((dec(v) for v in g['cost'].values()),ZERO)-credit)
        for r in self.conn.execute("SELECT json FROM pair_fills WHERE status='MATCHED'"):
            f=json.loads(r[0])
            if f['action']=='SELL':pending_sell+=dec(f['basis'])
        orders=self.reserved();risk=positions+pending_sell+orders;limit=dec(self.config['daily_loss_limit'])
        nxt=dec(self.config['budget'] if next_budget is None else next_budget)
        experiment_loss=max(ZERO,-dec(self.wallet()['realized']));experiment_limit=dec(self.config['experiment_loss_limit'])
        halt=self.conn.execute('SELECT reason,at FROM pair_v2_halt WHERE id=1').fetchone()
        return {'gross_loss_24h':money(gross),'position_max_loss':money(positions),'pending_sell_basis':money(pending_sell),
            'order_reservations':money(orders),'unresolved_risk':money(risk),'next_budget':money(nxt),'limit':money(limit),
            'used':money(gross+risk),'available':money(max(ZERO,min(limit-gross-risk,experiment_limit-experiment_loss-risk))),
            'experiment_loss':money(experiment_loss),'experiment_limit':money(experiment_limit),
            'new_group_allowed':not halt and gross+risk+nxt<=limit and experiment_loss+risk+nxt<=experiment_limit,
            'over_limit':gross+risk>limit or experiment_loss+risk>experiment_limit,'experiment_halt':dict(halt) if halt else None,
            'scope':'已实现负损益总额 + 未平仓最大损失 + 在途/未确认风险 + 下一组预算；盈利不抵扣'}

    def new_order(self,g,side,price,q,now,*,kind='MAKER',action='BUY',fee_reserve=ZERO):
        with self.tx():
            cost=dec(q)*dec(price)+dec(fee_reserve) if action=='BUY' else ZERO
            if action=='BUY':
                risk=self.risk_snapshot(now,next_budget=cost)
                # Completing a pair can reduce risk, but reserving its full cash
                # remains conservative until the new shares actually confirm.
                if not risk['new_group_allowed']:raise ValueError('RISK_BUDGET_EXHAUSTED')
                if dec(self.wallet()['cash'])-self.reserved()<cost:raise ValueError('CASH_RESERVATION_EXHAUSTED')
            elif dec(q)>self.confirmed_inventory(g)[side]:raise ValueError('UNCONFIRMED_INVENTORY')
            o=super().new_order(g,side,price,q,now,kind=kind,action=action,fee_reserve=fee_reserve)
            o['execution_rules']=deepcopy(g['rules'])
            b=self.book_at(o['token'],now)
            o['reference_mid']=money((dec(b['best_bid'])+dec(b['best_ask']))/2) if b and b.get('best_bid') is not None and b.get('best_ask') is not None else None
            self.save_order(o);return o

    @staticmethod
    def _fresh_exit_book(b,token,now):
        try:
            if str(b['token_id'])!=str(token) or not 0<=now-b['source_ms']<=3000 or not 0<=now-b['received_ms']<=2500:return False
            if not b['bids'] or any(not ZERO<dec(x['price'])<ONE or dec(x['size'])<=0 for x in b['bids']):return False
            if b.get('asks') and max(dec(x['price']) for x in b['bids'])>=min(dec(x['price']) for x in b['asks']):return False
            return True
        except (ValueError,TypeError,KeyError):return False

    def add_book(self,b,at):
        if b.get('source')!='paired-v2-exit-rest':return super().add_book(b,at)
        token=b.get('token_id')
        if token and 0<=at-b.get('source_ms',-1)<=3000 and 0<=at-b.get('received_ms',-1)<=2500:
            ring=self.history[str(token)]
            if not ring or (b['source_ms'],b['received_ms'])!=(ring[-1]['source_ms'],ring[-1]['received_ms']):
                view=deepcopy(b);view['_exit_invalid']=not self._fresh_exit_book(b,token,at);ring.append(view)

    def exit_book_at(self,token,at,after=None):
        rows=[b for b in self.history[str(token)] if b['source_ms']<=at and b['received_ms']<=at and (after is None or b['received_ms']>after)]
        if not rows:return None
        b=max(rows,key=lambda b:(b['source_ms'],b['received_ms']))
        return b if self._fresh_exit_book(b,token,at) and not b.get('_exit_invalid') else None

    def _usable_exit_rules(self,g,now):
        if g['id'] in self.exit_invalid:return None
        candidates=[self.exit_rules.get(g['id']),self.rules.get(g['id'])]
        valid=[r for r in candidates if r and 0<=now-r.get('checked_at',-1000000)<=self.config['exit_rule_max_age_ms']]
        return max(valid,key=lambda r:r['checked_at']) if valid else None

    def _exit_status(self,g,code,message,now):
        if g.get('exit_code')!=code or g.get('exit_reason')!=message:
            g.update(exit_code=code,exit_reason=message,exit_status_since=now)
            self.event('EXIT_STATUS',now,g['id'],code=code,message=message)

    def _rescue(self,g,now):
        if now<g['retry_after'] or now>=g['ends_at']:return
        if self.orders(g['id'],active=True):
            self._exit_status(g,'CANCEL_WAIT','正在撤回原报价；资金与可能的迟到成交继续保留',now);return
        up,down=dec(g['qty']['Up']),dec(g['qty']['Down'])
        if up==down:return
        held,missing=('Up','Down') if up>down else ('Down','Up')
        confirmed=self.confirmed_inventory(g)
        confirmed_excess=max(ZERO,confirmed[held]-dec(g['qty'][missing]))
        quantity=floor(confirmed_excess,'.01')
        if quantity<=0:
            if confirmed_excess>0:
                g['phase']='SMALL_TAIL';self._exit_status(g,'BELOW_EXIT_MINIMUM','已确认余量小于可提交精度，保留到终局，不凑单增加风险',now)
            else:self._exit_status(g,'CONFIRM_WAIT','已有成交尚待确认；确认后立即重新评估退出',now)
            return
        rules=self._usable_exit_rules(g,now)
        if not rules:
            self._exit_status(g,'EXIT_RULE_WAIT','独立退出通道正在核对 CLOB 费用、精度和资产；不会使用猜测值',now);return
        if quantity<dec(rules['min_order_size']):
            g['phase']='SMALL_TAIL';self._exit_status(g,'BELOW_EXIT_MINIMUM','已确认残余小于实际最小份额，继续核验并等待终局',now);return
        # Keep the original entry proof but use separately verified current
        # execution terms for new risk-reducing orders.
        g['rules']=deepcopy(rules);tick=dec(rules['tick_size'])
        own=self.exit_book_at(g['tokens'][held],now)
        other=self.book_at(g['tokens'][missing],now)
        if not own:
            self._exit_status(g,'EXIT_BOOK_WAIT','独立退出通道正在补齐新买盘；不使用陈旧价格伪造退出',now);return
        basis=dec(g['cost'][held])/max(dec(g['qty'][held]),Decimal('.000001'))
        if other:
            limit=min(ONE-tick,min(dec(x['price']) for x in other['asks'])+tick)
            cost=quantity*limit+fee(quantity,limit,rules)
            risk=self.risk_snapshot(now,next_budget=cost)
            if (basis+cost/quantity<=Decimal('.99') and dec(g['spent'])+cost<=dec(g['budget'])
                    and quantity*limit>=ONE and risk['new_group_allowed'] and cost<=dec(self.wallet()['cash'])-self.reserved()):
                self.new_order(g,missing,limit,quantity,now,kind='HEDGE',fee_reserve=fee(quantity,limit,rules))
                g['phase']='HEDGING';g['rescue_attempts']+=1;g['retry_after']=now+self.config['exit_retry_ms']
                self._exit_status(g,'HEDGING','含费仍有配对优势且风险额度足够，尝试补齐',now);return
        bid=max(dec(x['price']) for x in own['bids']);mark=quantity*bid-fee(quantity,bid,rules)
        estimated_loss=max(ZERO,quantity*basis-mark)
        urgent=(estimated_loss>=dec(self.config['maximum_single_leg_loss']) or g['rescue_attempts']>0
                or now-g['first_fill']>=self.config['emergency_age_ms'])
        # This is a trigger, not a guaranteed stop price. FAK always uses real
        # arrival depth, so empty/vanished bids can still leave a residual.
        limit=tick if urgent else max(tick,bid-2*tick)
        self.new_order(g,held,limit,quantity,now,kind='EXIT',action='SELL')
        g['phase']='EXITING';g['rescue_attempts']+=1;g['retry_after']=now+self.config['exit_retry_ms']
        self._exit_status(g,'EXITING_URGENT' if urgent else 'EXITING','风险降低优先：按到达时真实买盘退出，未成交余量继续重试',now)

    def _activate(self,g,o,now):
        if o['kind']=='MAKER':
            if dec(o['quantity'])<dec(g['rules']['min_order_size']) or dec(o['price'])%dec(g['rules']['tick_size'])!=ZERO:
                o.update(status='DONE',terminal_reason='到达时精度或最低份额已变化');self.save_order(o);return
            return super()._activate(g,o,now)
        due=o['active_at']
        if now-due>500 or due>=g['ends_at']:
            o.update(status='DONE',terminal_reason='无法验证模拟到达时刻，保留持仓重新尝试');g['quality_ok']=False
        else:
            b=self.exit_book_at(o['token'],due,after=o['created']) if o['action']=='SELL' else self.book_at(o['token'],due,after=o['created'])
            rule=o['execution_rules']
            if not b:o.update(status='DONE',terminal_reason='到达时缺少新盘口，未虚构成交')
            elif b.get('tick_size') and dec(o['price'])%dec(b['tick_size'])!=ZERO:
                o.update(status='DONE',terminal_reason='到达时价格精度变化，重新按新规则提交')
            else:
                levels=sorted(b['bids' if o['action']=='SELL' else 'asks'],key=lambda x:dec(x['price']),reverse=o['action']=='SELL')
                for i,level in enumerate(levels):
                    p=dec(level['price'])
                    if (o['action']=='SELL' and p<dec(o['price'])) or (o['action']=='BUY' and p>dec(o['price'])):break
                    q=floor(min(dec(o['remaining']),dec(level['size'])*dec(self.config['depth_participation'])))
                    if q<=0:continue
                    f=fee(q,p,rule)
                    if o['action']=='BUY' and dec(g['spent'])+q*p+f>dec(g['budget']):break
                    self._match(g,o,q,p,f,due,f"IOC:{o['id']}:{i}",source='ARRIVAL_DEPTH')
                o.update(status='DONE',terminal_reason='FAK 已结束，未成交余量继续管理')
                self.event('ARRIVAL',due,g['id'],order_id=o['id'],status=o['status'],book=b,rules=rule)
        self.save_order(o);self.save_group(g)

    def _adverse_cross(self,g,o,now):
        if self.scenario=='STRESS':return super()._adverse_cross(g,o,now)
        # Keep the counterfactual signal visible; the stress ledger accounts for
        # its money independently. Never silently turn pressure losses off.
        if o['kind']!='MAKER' or o['status'] not in ('OPEN','CANCELING','DRAINING'):return
        b=self.book_at(o['token'],now)
        if b and b['source_ms']>o['active_at'] and (o['cancel_at'] is None or b['source_ms']<=o['cancel_at']) and min(dec(x['price']) for x in b['asks'])<dec(o['price']):
            if not o.get('stress_cross_seen'):
                o['stress_cross_seen']=True;self.save_order(o);self.stress_crosses+=1
                self.event('STRESS_CROSS',now,g['id'],order_id=o['id'],message='发现严格穿价；独立压力账本处理其假设成交')

    def _stable(self,books,now):
        for side,b in books.items():
            rows=[x for x in self.history[b['token_id']] if max(self.last_gap_at,now-self.config['stability_lookback_ms'])<=x['received_ms']<=now
                  and good_book(x,b['token_id'],x['received_ms'],self.config)]
            if len(rows)<4 or max(x['received_ms'] for x in rows)-min(x['received_ms'] for x in rows)<self.config['stability_span_ms']:
                return False,'等待约2秒连续盘口；这是实时观察，不需要历史训练'
            mids=[(max(dec(x['price']) for x in r['bids'])+min(dec(x['price']) for x in r['asks']))/2 for r in rows]
            if max(mids)-min(mids)>dec(self.config['maximum_mid_range']):return False,f'{side} 短时价格跳动较大，避免挂出陈旧报价'
        return True,None

    def _quote_new(self,now,market,books,rules):
        if self.entry_pause_reason:self.reason('PAUSED',self.entry_pause_reason,now);return
        if not self.config['enabled']:self.reason('PAUSED','已暂停 V2 新报价，已有份额继续管理',now);return
        cid=market['condition_id'] if market else None
        row=self.conn.execute('SELECT json FROM pair_groups WHERE id=?',(cid,)).fetchone() if cid else None
        g=json.loads(row[0]) if row else None
        if g and (g['closed'] or g['first_fill']):
            self.reason('MARKET_DONE' if g['closed'] else 'MANAGING','本期已完成，下一期自动继续' if g['closed'] else g.get('exit_reason') or '正在管理本期成交与资金回收',now);return
        if g and now<g['retry_after']:self.reason('RECOVERING','重启后重新同步盘口，原有成交继续管理',now);return
        if g and self.orders(cid,active=True):self.reason('QUOTING','V2 双边报价排队中；成交带与穿价压力分别核算',now);return
        risk=self.risk_snapshot(now)
        if not risk['new_group_allowed']:
            self.reason('EXPERIMENT_STOP' if risk['experiment_halt'] else 'RISK_BUDGET',
                '本轮停止已触发，保留账本等待复盘' if risk['experiment_halt'] else f"日额度/本轮额度不足：可新增风险 ${risk['available']}，下一组需要 ${risk['next_budget']}；持仓继续管理",now);return
        if now<self.gap_until:self.reason('RECOVERING','数据恢复中；退出通道继续运行',now);return
        if not market or not rules:self.reason('MARKET_LOADING','新开仓规则正在核对；不影响独立退出核验',now);return
        if not market['started_at']+self.config['entry_start_seconds']*1000<=now<=market['ends_at']-self.config['entry_end_remaining_seconds']*1000:
            self.reason('WINDOW_WAIT','只在开盘30秒后、剩余至少120秒报价，已有成交继续管理',now);return
        gates={**self.config,'source_age_ms':self.config['entry_source_age_ms'],'receive_age_ms':self.config['entry_receive_age_ms']}
        tokens={'Up':market['up_token'],'Down':market['down_token']}
        if not all(good_book(books.get(s,{}),t,now,gates) for s,t in tokens.items()):self.reason('BOOK_WAIT','新开仓等待两侧新鲜盘口；持仓退出使用独立核验通道',now);return
        if abs(books['Up']['source_ms']-books['Down']['source_ms'])>500:self.reason('BOOK_SKEW','两侧盘口时间差超过500毫秒，等待同步',now);return
        stable,message=self._stable(books,now)
        if not stable:self.reason('STABILITY_WAIT',message,now);return
        if len(self.groups())>=self.config['max_markets'] and g is None:self.reason('POSITION_LIMIT','并行市场达到上限，等待资金释放',now);return
        budget=dec(self.config['budget'])
        if dec(self.wallet()['cash'])-self.reserved()<budget:self.reason('CASH_WAIT','可用模拟现金不足，不加预算或借用其他账户',now);return
        tick=dec(rules['tick_size']);bids={s:max(dec(x['price']) for x in books[s]['bids']) for s in tokens}
        asks={s:min(dec(x['price']) for x in books[s]['asks']) for s in tokens}
        prices={s:floor(bids[s]-tick,tick) for s in tokens}
        while sum(prices.values())>dec(self.config['pair_price_cap']):
            side=min(prices,key=lambda s:((bids[s]-prices[s])/tick,-prices[s]));prices[side]-=tick
        if any(not dec(self.config['minimum_quote_price'])<=p<=dec(self.config['maximum_quote_price']) for p in prices.values()):self.reason('QUOTE_FILTER','V2 仅在双侧均衡价带报价，减少高价单边暴露',now);return
        if any(asks[s]-bids[s]>dec(self.config['maximum_spread']) for s in tokens):self.reason('QUOTE_FILTER','盘口价差过宽，退出成本不合适',now);return
        q=floor(budget/sum(prices.values()),'.01')
        if q<dec(rules['min_order_size']):self.reason('MINIMUM_SIZE','预算不足实际最小份额，不能向上凑单',now);return
        pair_gain=q*(ONE-sum(prices.values()))
        worst_exit=max(max(ZERO,q*prices[s]-(q*max(tick,bids[s]-2*tick)-fee(q,max(tick,bids[s]-2*tick),rules))) for s in tokens)
        if worst_exit>min(dec(self.config['maximum_single_leg_loss']),2*pair_gain):self.reason('EXIT_COST_FILTER','即时退出成本相对配对价差过高，跳过此组',now);return
        if g is None:
            g={'id':cid,'market':market['round_id'],'slug':market.get('slug',market['round_id']),'created':now,'ends_at':market['ends_at'],
               'tokens':tokens,'rules':deepcopy(rules),'budget':money(budget),'spent':'0','qty':{'Up':'0','Down':'0'},'cost':{'Up':'0','Down':'0'},
               'pnl':'0','fees':'0','paired':'0','claim':'0','claim_at':None,'first_fill':None,'result':None,'quality_ok':True,'closed':False,
               'phase':'QUOTING','retry_after':0,'rescue_attempts':0}
        g['rules']=deepcopy(rules);g['budget']=money(budget);g['entry_risk']=risk;g['scenario']=self.scenario
        self.save_group(g)
        for side in tokens:self.new_order(g,side,prices[side],q,now)
        self.last_decision={'at':now,'prices':{s:money(p) for s,p in prices.items()},'pair_cost':money(sum(prices.values())),
            'quantity':money(q),'potential_pair_profit':money(pair_gain),'current_exit_cost_bound':money(worst_exit)}
        self.reason('QUOTING','V2 风险已预留，双侧报价已提交；等待真实成交证据',now)

    def tick(self,now,market=None,books=None,rules=None,*,entry_pause_reason=None):
        with self.tx():
            for b in (books or {}).values():self.add_book(b,now)
            risk=self.risk_snapshot(now,next_budget=ZERO)
            for g in self.groups():
                for o in self.orders(g['id'],active=True):
                    if o['kind']!='MAKER' or o['status']!='OPEN':continue
                    b=self.book_at(o['token'],now)
                    if b and o.get('reference_mid') is not None:
                        mid=(max(dec(x['price']) for x in b['bids'])+min(dec(x['price']) for x in b['asks']))/2
                        if abs(mid-dec(o['reference_mid']))>=Decimal('.02'):self.cancel(g,now,'盘口已移动，提前撤掉陈旧报价')
                if risk['over_limit'] or risk['experiment_halt']:self.cancel(g,now,'风险额度已占满，撤回未成交买单')
            super().tick(now,market,books,rules,entry_pause_reason=entry_pause_reason)
            wallet=self.wallet()
            if dec(wallet['realized'])<=-dec(self.config['experiment_loss_limit']):
                self.conn.execute('INSERT OR IGNORE INTO pair_v2_halt VALUES(1,?,?)',('EXPERIMENT_LOSS',now))
            # Internal conservation, independent from market-price valuation.
            costs=sum((dec(v) for g in self.groups(False) for v in g['cost'].values()),ZERO)
            claims=sum((dec(g['claim']) for g in self.groups(False)),ZERO)
            pending=[json.loads(r[0]) for r in self.conn.execute("SELECT json FROM pair_fills WHERE status='MATCHED'")]
            sell_cash=sum((dec(f['cash']) for f in pending if f['action']=='SELL'),ZERO)
            sell_pnl=sum((dec(f['cash'])-dec(f['basis']) for f in pending if f['action']=='SELL'),ZERO)
            residual=dec(wallet['cash'])+costs+claims+sell_cash-dec(wallet['initial'])-dec(wallet['realized'])-sell_pnl
            if abs(residual)>Decimal('1e-20'):
                self.conn.execute('INSERT OR IGNORE INTO pair_v2_halt VALUES(1,?,?)',('ACCOUNTING_MISMATCH',now))
                self.event('ACCOUNTING_MISMATCH',now,residual=money(residual))
            # Only the in-memory quote cache is pruned. Financial and execution
            # evidence stays in SQLite, and unsettled markets retain their books.
            keep=set()
            if market:keep.update((market['up_token'],market['down_token']))
            for g in self.groups():keep.update(g['tokens'].values())
            for token in list(self.history):
                if token not in keep:self.history.pop(token,None)

    def snapshot(self,now):
        with self.lock:
            result=super().snapshot(now);result['risk']=self.risk_snapshot(now)
            result['scenario']=self.scenario;result['run_id']=self.identity['run_id']
            result['source_summary']=[dict(r) for r in self.conn.execute("SELECT json_extract(json,'$.source') source,count(*) fills FROM pair_fills GROUP BY 1")]
            result['exit_statuses']=[{'market':g['market'],'code':g.get('exit_code'),'reason':g.get('exit_reason'),'since':g.get('exit_status_since')} for g in self.groups() if g['first_fill']]
            groups=self.groups(False);turnover=sum((dec(g['spent']) for g in groups),ZERO)
            bought=sum((dec(json.loads(r[0])['quantity']) for r in self.conn.execute("SELECT json FROM pair_fills WHERE json_extract(json,'$.action')='BUY'")),ZERO)
            paired=sum((dec(g['paired']) for g in groups),ZERO)
            result['turnover']=money(turnover)
            result['pnl_on_turnover']=money(dec(result['realized_pnl'])/turnover) if turnover else None
            result['paired_share_fraction']=money(2*paired/bought) if bought else None
            breakdown={}
            for row in self.conn.execute('SELECT kind,pnl FROM pair_profit'):
                breakdown[row['kind']]=money(dec(breakdown.get(row['kind'],'0'))+dec(row['pnl']))
            result['pnl_breakdown']=breakdown
            result['assumptions']['fills']='成交带主账本仅用真实 SELL 事件；穿价假设在独立压力账本核算，两个账户收益不相加'
            result['assumptions']['risk']='24小时负损益不由盈利抵扣；未确认、在途和持仓风险均占额度'
            return result
