"""双翼配对 V1 — independent, training-free PAPER maker with real-tape fills.

No exchange order client, signer, wallet credentials or live execution interface.
Matched buys consume cash immediately; confirmation unlocks inventory after 12s.
Only completed equal shares are hedged. One-sided inventory remains risky.
"""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

STRATEGY_ID = 'PAIRED_MAKER_V1'
NAME = '双翼配对 V1'
VERSION = 'paired-paper-v1.2'
ZERO = Decimal('0')
ONE = Decimal('1')
ACTIVE = ('PENDING', 'OPEN', 'CANCELING', 'DRAINING')
DEFAULTS = {'enabled': True, 'budget': '5.00', 'max_markets': 5,
            'daily_loss_limit': '10.00', 'pair_price_cap': '0.98',
            'arrival_ms': 2000, 'cancel_ms': 2000, 'confirmation_ms': 12000,
            'claim_ms': 60000, 'source_age_ms': 3000, 'receive_age_ms': 2500,
            'quote_ttl_ms': 12000, 'imbalance_ms': 12000,
            'print_participation': '0.25', 'depth_participation': '0.25',
            'entry_start_seconds': 15, 'entry_end_remaining_seconds': 60}


def dec(v):
    if isinstance(v, bool): raise ValueError('数字不能为布尔值')
    try:x = Decimal(str(v))
    except ArithmeticError as exc:raise ValueError('金额或价格无效') from exc
    if not x.is_finite(): raise ValueError('金额或价格无效')
    return x


def money(v):
    return format(dec(v), 'f')


def floor(v, step='0.000001'):
    step = dec(step)
    return (dec(v) / step).to_integral_value(rounding=ROUND_DOWN) * step


def canonical(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(v):
    return hashlib.sha256(canonical(v).encode()).hexdigest()


def fee(q, p, rules):
    """Conservative cash fee rounding; maker fee/rebates are never invented."""
    p, q = dec(p), dec(q)
    return (q * dec(rules['fee_rate']) * (p*(1-p)) ** dec(rules['fee_exponent'])).quantize(Decimal('.00001'), rounding=ROUND_UP)


def good_book(b, token, now, config=DEFAULTS):
    try:
        if str(b['token_id']) != str(token): return False
        if not 0 <= now-b['source_ms'] <= config['source_age_ms']: return False
        if not 0 <= now-b['received_ms'] <= config['receive_age_ms']: return False
        bids, asks = b['bids'], b['asks']
        if not bids or not asks: return False
        if any(not ZERO < dec(x['price']) < ONE or dec(x['size']) <= 0 for x in bids+asks): return False
        return max(dec(x['price']) for x in bids) < min(dec(x['price']) for x in asks)
    except (KeyError, TypeError, ValueError, ArithmeticError): return False


def pair_quote(books, rules, budget, cap='0.98'):
    tick = dec(rules['tick_size'])
    prices = {s: floor(max(dec(x['price']) for x in books[s]['bids']), tick) for s in ('Up', 'Down')}
    if any(not Decimal('.15') <= p <= Decimal('.85') for p in prices.values()):
        raise ValueError('价格接近单边极端，等待下一段均衡盘口')
    if any(min(dec(x['price']) for x in books[s]['asks'])-prices[s] > Decimal('.04') for s in prices):
        raise ValueError('两侧价差过宽，暂不挂新单')
    # Quote each real book independently. Never manufacture the opposite book.
    while sum(prices.values()) > dec(cap):
        side = max(prices, key=prices.get); prices[side] -= tick
    if any(p <= ZERO for p in prices.values()): raise ValueError('无法形成有效配对价格')
    q = floor(dec(budget)/sum(prices.values()), '.01')
    if q < dec(rules['min_order_size']):
        raise ValueError(f"每组预算不足最低 {rules['min_order_size']} 份；当前至少需要 ${money(dec(rules['min_order_size'])*sum(prices.values()))}")
    return prices, q


class PairedPaperEngine:
    def __init__(self, path, *, config=None, at_ms=0):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL'); self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.executescript('''
          CREATE TABLE IF NOT EXISTS pair_wallet(id INTEGER PRIMARY KEY,cash TEXT,realized TEXT,initial TEXT,created INTEGER);
          CREATE TABLE IF NOT EXISTS pair_settings(id INTEGER PRIMARY KEY,json TEXT);
          CREATE TABLE IF NOT EXISTS pair_groups(id TEXT PRIMARY KEY,closed INTEGER NOT NULL DEFAULT 0,created INTEGER,json TEXT);
          CREATE TABLE IF NOT EXISTS pair_orders(id INTEGER PRIMARY KEY,group_id TEXT,status TEXT,created INTEGER,json TEXT);
          CREATE INDEX IF NOT EXISTS pair_orders_active ON pair_orders(group_id,status);
          CREATE TABLE IF NOT EXISTS pair_fills(id TEXT PRIMARY KEY,group_id TEXT,order_id INTEGER,status TEXT,due INTEGER,json TEXT);
          CREATE INDEX IF NOT EXISTS pair_fills_pending ON pair_fills(status,due);
          CREATE TABLE IF NOT EXISTS pair_tape(id TEXT PRIMARY KEY,received INTEGER,json TEXT);
          CREATE TABLE IF NOT EXISTS pair_events(id INTEGER PRIMARY KEY,at INTEGER,kind TEXT,group_id TEXT,json TEXT);
          CREATE TABLE IF NOT EXISTS pair_profit(id INTEGER PRIMARY KEY,at INTEGER,group_id TEXT,pnl TEXT,kind TEXT);
          CREATE TABLE IF NOT EXISTS pair_curve(id INTEGER PRIMARY KEY,at INTEGER,nav TEXT,cash TEXT,pnl TEXT);
        ''')
        cfg = {**DEFAULTS, **(config or {})}
        with self.tx():
            self.conn.execute("INSERT OR IGNORE INTO pair_wallet VALUES(1,'100','0','100',?)", (at_ms,))
            self.conn.execute('INSERT OR IGNORE INTO pair_settings VALUES(1,?)', (canonical(cfg),))
        self.config = json.loads(self.conn.execute('SELECT json FROM pair_settings').fetchone()[0])
        self.history = defaultdict(lambda: deque(maxlen=150))
        self.rules = {}; self.last_reason = '正在接入实时盘口'; self.last_code = 'CONNECTING'
        self.last_at = at_ms; self.last_curve = 0; self.last_decision = None; self.last_nav = None
        prior_nav=self.conn.execute('SELECT at,nav FROM pair_curve WHERE nav IS NOT NULL ORDER BY id DESC LIMIT 1').fetchone()
        if prior_nav:self.last_nav={'value':prior_nav['nav'],'at':prior_nav['at']}
        self.gap_until = 0; self.current_market = None
        self.entry_pause_reason=None
        self.stats = {'prints_received': 0, 'prints_valid': 0, 'duplicates': 0, 'stale_prints': 0, 'data_gaps': 0}
        self.recover(at_ms)

    @contextmanager
    def tx(self):
        with self.lock, localcontext() as ctx:
            ctx.prec = 50
            nested = self.conn.in_transaction
            if not nested: self.conn.execute('BEGIN IMMEDIATE')
            try:
                yield
                if not nested: self.conn.execute('COMMIT')
            except BaseException:
                if not nested: self.conn.execute('ROLLBACK')
                raise

    def close(self): self.conn.close()

    def event(self, kind, at, group=None, **payload):
        self.conn.execute('INSERT INTO pair_events(at,kind,group_id,json) VALUES(?,?,?,?)', (at,kind,group,canonical(payload)))

    def reason(self, code, message, at):
        if (code,message) != (self.last_code,self.last_reason):
            self.event('DECISION', at, code=code, message=message)
        self.last_code, self.last_reason = code, message

    def groups(self, active=True):
        return [json.loads(r[0]) for r in self.conn.execute('SELECT json FROM pair_groups'+(' WHERE closed=0' if active else '')+' ORDER BY created')]

    def save_group(self, g):
        self.conn.execute('INSERT INTO pair_groups VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET closed=excluded.closed,json=excluded.json',
            (g['id'],int(g['closed']),g['created'],canonical(g)))

    def orders(self, group=None, active=False):
        where, args = [], []
        if group is not None: where.append('group_id=?'); args.append(group)
        if active: where.append("status IN ('PENDING','OPEN','CANCELING','DRAINING')")
        sql = 'SELECT json FROM pair_orders'+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY id'
        return [json.loads(r[0]) for r in self.conn.execute(sql, args)]

    def save_order(self, o):
        self.conn.execute('UPDATE pair_orders SET status=?,json=? WHERE id=?', (o['status'],canonical(o),o['id']))

    def wallet(self): return dict(self.conn.execute('SELECT * FROM pair_wallet').fetchone())

    def cash_change(self, amount):
        self.conn.execute('UPDATE pair_wallet SET cash=? WHERE id=1', (money(dec(self.wallet()['cash'])+dec(amount)),))

    def profit(self, g, pnl, at, kind):
        pnl = dec(pnl)
        self.conn.execute('INSERT INTO pair_profit(at,group_id,pnl,kind) VALUES(?,?,?,?)', (at,g['id'],money(pnl),kind))
        self.conn.execute('UPDATE pair_wallet SET realized=? WHERE id=1', (money(dec(self.wallet()['realized'])+pnl),))
        g['pnl'] = money(dec(g['pnl'])+pnl)

    def reserved(self):
        result = ZERO
        for o in self.orders(active=True):
            if o['action'] == 'BUY': result += dec(o['remaining'])*dec(o['price'])+dec(o.get('fee_reserve','0'))
        return result

    def daily_pnl(self, now):
        return sum((dec(r[0]) for r in self.conn.execute('SELECT pnl FROM pair_profit WHERE at>?', (now-86400000,))),ZERO)

    def settings(self, changes, now):
        if not isinstance(changes,dict) or set(changes)-{'enabled','budget','max_markets','daily_loss_limit'}:
            raise ValueError('仅支持 Paper 开关、每组预算、并行市场数和日亏损额度')
        cfg = dict(self.config)
        for key,value in changes.items():
            if key=='enabled':
                if type(value) is not bool: raise ValueError('开关格式无效')
                cfg[key] = value
            elif key=='max_markets':
                if type(value) is not int or not 1<=value<=10: raise ValueError('并行市场数须为1–10')
                cfg[key] = value
            else:
                x = dec(value)
                if not Decimal('1') <= x <= Decimal('100'): raise ValueError('金额须为 $1–$100')
                cfg[key] = money(x.quantize(Decimal('.01')))
        with self.tx():
            self.conn.execute('UPDATE pair_settings SET json=? WHERE id=1', (canonical(cfg),))
            self.event('SETTINGS',now,config=cfg)
        self.config = cfg
        return dict(cfg)

    def add_book(self, b, at):
        if not isinstance(b,dict) or not b.get('token_id'): return
        token = str(b['token_id']); ring = self.history[token]
        if ring and (b.get('source_ms'),b.get('received_ms')) == (ring[-1].get('source_ms'),ring[-1].get('received_ms')): return
        if good_book(b,token,at,self.config): ring.append(deepcopy(b))

    def book_at(self, token, at, after=None):
        rows = [b for b in self.history[str(token)] if good_book(b,token,at,self.config)
                and (after is None or b['received_ms']>after)]
        return max(rows,key=lambda b:(b['received_ms'],b['source_ms'])) if rows else None

    def recover(self, now):
        with self.tx():
            for g in self.groups():
                affected = False
                for o in self.orders(g['id'],active=True):
                    o['status']='DONE';o['terminal_reason']='RESTART_DATA_GAP';self.save_order(o);affected=True
                if affected:
                    g['quality_ok']=False;g['phase']='MANAGING' if g['first_fill'] else 'QUOTING';g['retry_after']=now+3000
                    self.save_group(g);self.event('RECOVERY',now,g['id'],message='重启取消未完成模拟挂单；已知成交继续确认和结算')

    def gap(self, now, message):
        with self.tx():
            self.gap_until=max(self.gap_until,now+3000);self.stats['data_gaps']+=1
            for g in self.groups():
                if self.orders(g['id'],active=True):
                    g['quality_ok']=False;self.cancel(g,now,'数据中断，撤销模拟挂单');self.save_group(g)
            self.event('DATA_GAP',now,message=message)

    def cancel(self, g, now, reason):
        for o in self.orders(g['id'],active=True):
            if o['kind'] != 'MAKER': continue
            if o['status']=='PENDING':
                o['status']='DONE';o['terminal_reason']=reason
            elif o['status']=='OPEN':
                o['status']='CANCELING';o['cancel_at']=now+self.config['cancel_ms']
                # Preserve reserve through the accepted source lag PLUS bounded
                # local processing lag; a queued pre-cancel print cannot spend
                # funds that have already been reused by its replacement.
                o['terminal_at']=o['cancel_at']+self.config['source_age_ms']+3000;o['terminal_reason']=reason
            self.save_order(o)

    def new_order(self, g, side, price, q, now, *, kind='MAKER', action='BUY', fee_reserve=ZERO):
        o={'group_id':g['id'],'side':side,'token':g['tokens'][side],'kind':kind,'action':action,
           'model_version':VERSION,
           'price':money(price),'quantity':money(q),'remaining':money(q),'filled':'0','fee_reserve':money(fee_reserve),
           'created':now,'active_at':now+self.config['arrival_ms'],'status':'PENDING','queue_ahead':'0',
           'cancel_at':None,'terminal_at':None,'terminal_reason':None}
        cur=self.conn.execute('INSERT INTO pair_orders(group_id,status,created,json) VALUES(?,?,?,?)',(g['id'],'PENDING',now,'{}'))
        o['id']=cur.lastrowid;self.save_order(o);self.event('ORDER',now,g['id'],order=o)
        return o

    def _match(self, g, o, qty, price, fee_cash, now, key, source='PUBLIC_TAPE'):
        if self.conn.execute('SELECT 1 FROM pair_fills WHERE id=?',(key,)).fetchone(): return
        qty, price, fee_cash = dec(qty), dec(price), dec(fee_cash)
        if qty<=0 or qty>dec(o['remaining']): raise ValueError('模拟成交数量越界')
        side=o['side'];notional=qty*price
        if o['action']=='BUY':
            cash=notional+fee_cash
            if cash>dec(self.wallet()['cash']) or dec(g['spent'])+cash>dec(g['budget']): raise ValueError('模拟预算不足')
            self.cash_change(-cash);g['spent']=money(dec(g['spent'])+cash)
            g['qty'][side]=money(dec(g['qty'][side])+qty);g['cost'][side]=money(dec(g['cost'][side])+cash)
            basis=cash
        else:
            if qty>dec(g['qty'][side]): raise ValueError('模拟卖出超过持仓')
            basis=dec(g['cost'][side])*qty/dec(g['qty'][side]);cash=notional-fee_cash
            g['qty'][side]=money(dec(g['qty'][side])-qty);g['cost'][side]=money(dec(g['cost'][side])-basis)
        g['fees']=money(dec(g['fees'])+fee_cash);g['first_fill']=g['first_fill'] or now
        g['phase']='MATCHED_WAIT' if dec(g['qty']['Up'])==dec(g['qty']['Down']) else 'UNPAIRED'
        o['remaining']=money(dec(o['remaining'])-qty);o['filled']=money(dec(o['filled'])+qty)
        if dec(o['remaining'])==0: o['status']='DONE';o['terminal_reason']='FILLED'
        f={'id':key,'group_id':g['id'],'order_id':o['id'],'side':side,'action':o['action'],'kind':o['kind'],
           'source':source,'model_version':VERSION,
           'quantity':money(qty),'price':money(price),'cash':money(cash),'fee':money(fee_cash),'basis':money(basis),
           'matched_at':now,'confirmed_at':now+self.config['confirmation_ms'],'status':'MATCHED'}
        self.conn.execute('INSERT INTO pair_fills VALUES(?,?,?,?,?,?)',(key,g['id'],o['id'],'MATCHED',f['confirmed_at'],canonical(f)))
        self.save_order(o);self.save_group(g);self.event('MATCH',now,g['id'],fill=f)

    def tape(self, raw, received):
        """Only observed SELL prints can hit our passive BUY; touching isn't a fill."""
        if raw.get('event_type')!='last_trade_price': return
        self.stats['prints_received']+=1
        try:
            source=int(raw['timestamp']);price=dec(raw['price']);size=dec(raw['size']);token=str(raw['asset_id'])
            if not ZERO<price<ONE or size<=0 or not 0<=received-source<=self.config['source_age_ms']:
                self.stats['stale_prints']+=1;return
        except (KeyError, ValueError, TypeError, ArithmeticError): return
        key=digest({k:raw.get(k) for k in ('transaction_hash','asset_id','market','timestamp','price','size','side')})
        with self.tx():
            self.stats['prints_valid']+=1
            if raw.get('side')!='SELL': return
            if not any(o['token']==token for o in self.orders(raw.get('market'),active=True)):return
            if self.conn.execute('SELECT 1 FROM pair_tape WHERE id=?',(key,)).fetchone(): self.stats['duplicates']+=1;return
            self.conn.execute('INSERT INTO pair_tape VALUES(?,?,?)',(key,received,canonical(raw)))
            for g in self.groups():
                for o in self.orders(g['id'],active=True):
                    if o['status']=='PENDING' and o['active_at']<=received:self._activate(g,o,received)
            available=size
            for g in self.groups():
                if raw.get('market')!=g['id'] or token not in g['tokens'].values(): continue
                for o in self.orders(g['id'],active=True):
                    if (o['kind']!='MAKER' or o['token']!=token or o['status']=='PENDING'
                            or source<o['active_at'] or received<o['active_at']
                            or (o['cancel_at'] is not None and source>o['cancel_at'])
                            or price>dec(o['price']) or available<=0): continue
                    # A visible depth decrease can remove orders ahead, but is
                    # never itself a fill. Only use a book strictly earlier on
                    # the source clock, so this print's own book update cannot
                    # consume the queue twice. Newly added volume stays behind.
                    earlier=[b for b in self.history[token] if b['source_ms']<source and b['received_ms']<=received
                             and good_book(b,token,received,self.config)]
                    if earlier:
                        prior=max(earlier,key=lambda b:(b['source_ms'],b['received_ms']))
                        lowest=min(dec(x['price']) for x in prior['bids'])
                        if dec(o['price'])>=lowest:
                            visible=sum((dec(x['size']) for x in prior['bids'] if dec(x['price'])==dec(o['price'])),ZERO)
                            o['queue_ahead']=money(min(dec(o['queue_ahead']),visible))
                    ahead=dec(o['queue_ahead']);consumed=min(ahead,available);available-=consumed
                    o['queue_ahead']=money(ahead-consumed);self.save_order(o)
                    qty=floor(min(dec(o['remaining']),available*dec(self.config['print_participation'])))
                    if qty>0:
                        o['last_tape_source']=source
                        self._match(g,o,qty,dec(o['price']),ZERO,received,key+':'+str(o['id']))
                        available-=qty/dec(self.config['print_participation'])

    def _adverse_cross(self,g,o,now):
        """Do not reward a paper maker for missing adverse executions.

        An active bid above a later, executable ask would change the real tape.
        Model that exposure at OUR (worse) bid using bounded opposing depth,
        separately labelled from actual trade-print-driven fills. A touch at
        equality still does not fill. No repeated reuse of the same level.
        """
        if o['kind']!='MAKER' or o['status'] not in ('OPEN','CANCELING','DRAINING'):return
        b=self.book_at(o['token'],now)
        if (not b or b['source_ms']<=max(o['active_at'],o.get('last_tape_source',0))
                or b['received_ms']<o['active_at']
                or (o['cancel_at'] is not None and b['source_ms']>o['cancel_at'])
                or min(dec(x['price']) for x in b['asks'])>=dec(o['price'])):return
        used=o.setdefault('cross_used',{})
        for level in sorted(b['asks'],key=lambda x:dec(x['price'])):
            p=dec(level['price'])
            if p>=dec(o['price']):break
            available=max(ZERO,dec(level['size'])*dec(self.config['depth_participation'])-dec(used.get(money(p),'0')))
            q=floor(min(dec(o['remaining']),available))
            if q<=0:continue
            used[money(p)]=money(dec(used.get(money(p),'0'))+q);o['queue_ahead']='0'
            key=f"CROSS:{o['id']}:{b['source_ms']}:{money(p)}"
            self._match(g,o,q,dec(o['price']),ZERO,now,key,source='ASK_THROUGH_CONSERVATIVE')
            self.event('CROSS_EVIDENCE',now,g['id'],order_id=o['id'],fill_id=key,book=b,
                message='卖盘严格穿过已生效买价，按原买价和可见深度作保守假设成交')
            if dec(o['remaining'])<=0:break

    def _activate(self, g, o, now):
        due=o['active_at']
        if now-due>500 or due>=g['ends_at']:
            o.update(status='DONE',terminal_reason='错过模拟到达时刻，未补记成交');g['quality_ok']=False
        else:
            b=self.book_at(o['token'],due,after=o['created'])
            if not b:
                o.update(status='DONE',terminal_reason='到达时无有效新盘口')
            elif o['kind']=='MAKER':
                if dec(o['price'])>=min(dec(x['price']) for x in b['asks']):
                    o.update(status='DONE',terminal_reason='到达时将吃单，post-only 拒绝')
                else:
                    o['status']='OPEN';o['queue_ahead']=money(sum((dec(x['size']) for x in b['bids'] if dec(x['price'])==dec(o['price'])),ZERO))
            else:
                levels=sorted(b['asks' if o['action']=='BUY' else 'bids'],key=lambda x:dec(x['price']),reverse=o['action']=='SELL')
                for i,level in enumerate(levels):
                    price=dec(level['price'])
                    if (o['action']=='BUY' and price>dec(o['price'])) or (o['action']=='SELL' and price<dec(o['price'])): break
                    q=floor(min(dec(o['remaining']),dec(level['size'])*dec(self.config['depth_participation'])))
                    if q<=0: continue
                    f=fee(q,price,g['rules'])
                    if o['action']=='BUY' and dec(g['spent'])+q*price+f>dec(g['budget']): break
                    self._match(g,o,q,price,f,due,f"IOC:{o['id']}:{i}",source='ARRIVAL_DEPTH')
                o.update(status='DONE',terminal_reason='FAK 完成，未成交余量取消')
            self.event('ARRIVAL',due,g['id'],order_id=o['id'],status=o['status'],queue_ahead=o['queue_ahead'],book=b)
        self.save_order(o);self.save_group(g)

    def _confirm(self, now):
        for row in self.conn.execute("SELECT json FROM pair_fills WHERE status='MATCHED' AND due<=? ORDER BY due",(now,)).fetchall():
            f=json.loads(row[0]);f['status']='CONFIRMED'
            self.conn.execute("UPDATE pair_fills SET status='CONFIRMED',json=? WHERE id=?",(canonical(f),f['id']))
            if f['action']=='SELL':
                g=json.loads(self.conn.execute('SELECT json FROM pair_groups WHERE id=?',(f['group_id'],)).fetchone()[0])
                self.cash_change(dec(f['cash']));self.profit(g,dec(f['cash'])-dec(f['basis']),f['confirmed_at'],'EXIT');self.save_group(g)
            self.event('CONFIRM',f['confirmed_at'],f['group_id'],fill_id=f['id'])

    def _pending(self, gid):
        return bool(self.conn.execute("SELECT 1 FROM pair_fills WHERE group_id=? AND status='MATCHED'",(gid,)).fetchone())

    def _redeem(self, g, now):
        if g['claim_at'] is not None and now>=g['claim_at'] and dec(g['claim'])>0:
            self.cash_change(dec(g['claim']));self.event('CLAIM',now,g['id'],amount=g['claim'])
            g['claim']='0';g['claim_at']=None
        if self._pending(g['id']) or self.orders(g['id'],active=True): return
        up,down=dec(g['qty']['Up']),dec(g['qty']['Down']);matched=min(up,down)
        amount,basis=ZERO,ZERO
        if matched>0:
            for side in ('Up','Down'):
                part=dec(g['cost'][side])*matched/dec(g['qty'][side]);basis+=part
                g['qty'][side]=money(dec(g['qty'][side])-matched);g['cost'][side]=money(dec(g['cost'][side])-part)
            amount+=matched;g['paired']=money(dec(g['paired'])+matched)
            self.profit(g,matched-basis,now,'PAIR_MERGE');self.event('PAIR_MERGE',now,g['id'],shares=money(matched),cost=money(basis),payout=money(matched))
        if g['result'] is not None and now>=g['ends_at']:
            leftover_value=dec(g['qty'][g['result']]);leftover_cost=sum((dec(v) for v in g['cost'].values()),ZERO)
            if sum((dec(v) for v in g['qty'].values()),ZERO)>0:
                amount+=leftover_value;self.profit(g,leftover_value-leftover_cost,now,'FINAL')
                self.event('FINAL',now,g['id'],outcome=g['result'],payout=money(leftover_value),cost=money(leftover_cost))
                g['qty']={'Up':'0','Down':'0'};g['cost']={'Up':'0','Down':'0'}
        if amount>0:
            g['claim']=money(dec(g['claim'])+amount);g['claim_at']=max(g['claim_at'] or 0,now+self.config['claim_ms'])
        empty=all(dec(v)==0 for v in g['qty'].values())
        if empty and (g['first_fill'] is not None or now>=g['ends_at']-self.config['entry_end_remaining_seconds']*1000):
            g['closed']=dec(g['claim'])==0;g['phase']='COMPLETE' if g['closed'] else 'CLAIM_PENDING'
        elif g['first_fill'] is not None and g['phase'] not in ('SMALL_TAIL','WAIT_RESULT','RESULT_CONFLICT'): g['phase']='UNPAIRED'

    def official_result(self, cid, outcome, at, evidence):
        if outcome not in ('Up','Down') or not evidence: raise ValueError('官方最终结果证据不完整')
        with self.tx():
            row=self.conn.execute('SELECT json FROM pair_groups WHERE id=?',(cid,)).fetchone()
            if not row: return
            g=json.loads(row[0])
            if at<g['ends_at']: raise ValueError('结果早于市场结束')
            if g['result'] is not None and g['result']!=outcome:
                g['quality_ok']=False;g['phase']='RESULT_CONFLICT';self.save_group(g)
                self.event('RESULT_CONFLICT',at,cid,message='官方结果冲突，需要核对');return
            g['result']=outcome;g['result_evidence']=evidence;self.save_group(g)
            self.event('RESULT',at,cid,outcome=outcome,evidence=evidence)

    def _rescue(self, g, now):
        if self.orders(g['id'],active=True) or self._pending(g['id']) or now<g['retry_after']: return
        if g['id'] not in self.rules:return
        up,down=dec(g['qty']['Up']),dec(g['qty']['Down'])
        if up==down or now>=g['ends_at']-15000: return
        held,missing=('Up','Down') if up>down else ('Down','Up');q=abs(up-down)
        rules=g['rules'];minimum=dec(rules['min_order_size'])
        if q<minimum:
            g['phase']='SMALL_TAIL';return
        if g['rescue_attempts']>=3:
            g['phase']='WAIT_RESULT';return
        b=self.book_at(g['tokens'][missing],now)
        basis=dec(g['cost'][held])/dec(g['qty'][held]);tick=dec(rules['tick_size'])
        if b:
            price=min(ONE-tick,min(dec(x['price']) for x in b['asks'])+tick);cost=q*price+fee(q,price,rules)
            if basis+cost/q<=Decimal('.995') and dec(g['spent'])+cost<=dec(g['budget']) and cost<=dec(self.wallet()['cash'])-self.reserved() and q*price>=ONE:
                self.new_order(g,missing,price,q,now,kind='HEDGE',fee_reserve=fee(q,price,rules))
                g['phase']='HEDGING';g['rescue_attempts']+=1;g['retry_after']=now+15000
                self.event('HEDGE',now,g['id'],message='含费仍可配平，尝试补齐缺少一侧');return
        b=self.book_at(g['tokens'][held],now)
        if b:
            limit=max(tick,max(dec(x['price']) for x in b['bids'])-tick)
            self.new_order(g,held,limit,q,now,kind='EXIT',action='SELL')
            g['phase']='EXITING';g['rescue_attempts']+=1;g['retry_after']=now+15000
            self.event('EXIT',now,g['id'],message='补齐不划算，按真实买盘尝试退出裸露份额')

    def tick(self, now, market=None, books=None, rules=None, *, entry_pause_reason=None):
        with self.tx():
            self.entry_pause_reason=entry_pause_reason
            if now<self.last_at:
                self.gap(now,'本机时钟回退');return
            self.last_at=now;self.current_market=deepcopy(market)
            for b in (books or {}).values(): self.add_book(b,now)
            if market:
                if rules:self.rules[market['condition_id']]=deepcopy(rules)
                else:self.rules.pop(market['condition_id'],None)
            self._confirm(now)
            for g in self.groups():
                current_rule=self.rules.get(g['id'])
                if current_rule:
                    if any(current_rule[k]!=g['rules'][k] for k in ('tick_size','min_order_size','fee_rate','fee_exponent')):
                        self.cancel(g,now,'市场参数变化，撤单后重新报价')
                    g['rules']=deepcopy(current_rule)
                elif market and market['condition_id']==g['id']:
                    self.cancel(g,now,'市场参数重新核对，暂停新报价')
                for o in self.orders(g['id'],active=True):
                    if o['status']=='PENDING' and now>=o['active_at']: self._activate(g,o,now)
                    elif o['status']=='CANCELING' and now>=o['cancel_at']:
                        o['status']='DRAINING';self.save_order(o)
                    elif o['status']=='DRAINING' and now>=o['terminal_at']:
                        o['status']='DONE';self.save_order(o)
                    self._adverse_cross(g,o,now)
                if g['first_fill'] is None:
                    active=self.orders(g['id'],active=True)
                    if active and len({o['side'] for o in active if o['status'] in ('PENDING','OPEN')})==1:
                        self.cancel(g,now,'一侧报价未成立，撤销另一侧后重新双边报价')
                if not self.config['enabled'] or self.entry_pause_reason or now<self.gap_until or now>=g['ends_at']-60000:
                    self.cancel(g,now,'停止新增或市场临近结算')
                elif any(not self.book_at(g['tokens'][s],now) for s in ('Up','Down')):
                    if self.orders(g['id'],active=True): g['quality_ok']=False
                    self.cancel(g,now,'盘口已过期，自动撤销模拟报价')
                if g['first_fill'] is not None:
                    unequal=dec(g['qty']['Up'])!=dec(g['qty']['Down'])
                    if not unequal or now-g['first_fill']>=self.config['imbalance_ms']:
                        self.cancel(g,now,'停止增加裸露风险，进入配平管理')
                    if now-g['first_fill']>=self.config['imbalance_ms']: self._rescue(g,now)
                elif any(o['status']=='OPEN' and now-o['active_at']>=self.config['quote_ttl_ms'] for o in self.orders(g['id'],active=True)):
                    self.cancel(g,now,'报价到期，撤单后重新排队')
                self._redeem(g,now);self.save_group(g)
            self._quote_new(now,market,books or {},rules)
            if now-self.last_curve>=5000:
                nav=self.nav(now)
                self.conn.execute('INSERT INTO pair_curve(at,nav,cash,pnl) VALUES(?,?,?,?)',(now,money(nav) if nav is not None else None,self.wallet()['cash'],self.wallet()['realized']))
                if nav is not None: self.last_nav={'value':money(nav),'at':now}
                self.last_curve=now

    def _quote_new(self, now, market, books, rules):
        if self.entry_pause_reason:self.reason('PAUSED',self.entry_pause_reason,now);return
        if self.conn.execute("SELECT 1 FROM pair_groups WHERE json_extract(json,'$.phase')='RESULT_CONFLICT'").fetchone():
            self.reason('RESULT_CONFLICT','存在官方结果冲突，需要核对该条记录后继续',now);return
        if not self.config['enabled']: self.reason('PAUSED','已停止新报价，已有持仓继续管理',now);return
        if now<self.gap_until: self.reason('RECOVERING','行情恢复中，稍后自动继续',now);return
        if not market or not rules: self.reason('MARKET_LOADING','正在核对当前市场、费用和最小份额',now);return
        if not market['started_at']+self.config['entry_start_seconds']*1000<=now<=market['ends_at']-self.config['entry_end_remaining_seconds']*1000:
            self.reason('WINDOW_WAIT','开盘15秒后至剩余60秒挂新单；期间继续管理已有持仓',now);return
        tokens={'Up':market['up_token'],'Down':market['down_token']}
        if not all(good_book(books.get(s,{}),t,now,self.config) for s,t in tokens.items()):
            self.reason('BOOK_WAIT','等待两侧可用的新盘口，恢复后自动报价',now);return
        if abs(books['Up']['source_ms']-books['Down']['source_ms'])>1000:
            self.reason('BOOK_SKEW','两侧盘口时间差过大，等待同步',now);return
        cid=market['condition_id'];row=self.conn.execute('SELECT json FROM pair_groups WHERE id=?',(cid,)).fetchone()
        g=json.loads(row[0]) if row else None
        if g and g['closed']:
            self.reason('MARKET_DONE','本期已完成，下一期将自动继续',now);return
        if g and g['first_fill'] is not None:
            self.reason('MANAGING','本期已有成交；正在配平、退出或等待结算',now);return
        if g and now<g['retry_after']:self.reason('RECOVERING','重启后同步新盘口，稍后自动恢复报价',now);return
        if g and self.orders(cid,active=True):
            self.reason('QUOTING','模拟双边报价已提交；只按实际成交事件判断排队成交',now);return
        if self.daily_pnl(now)<=-dec(self.config['daily_loss_limit']):
            self.reason('DAILY_LIMIT','过去24小时已实现亏损达到本策略额度；额度恢复后自动继续',now);return
        if g is None and len(self.groups())>=self.config['max_markets']:
            self.reason('POSITION_LIMIT','已有市场占用达到上限，释放后自动继续',now);return
        budget=dec(self.config['budget'])
        if dec(self.wallet()['cash'])-self.reserved()<budget:
            self.reason('CASH_WAIT','可用模拟现金不足本组预算，等待确认或领取',now);return
        try: prices,q=pair_quote(books,rules,budget,self.config['pair_price_cap'])
        except ValueError as exc: self.reason('QUOTE_FILTER',str(exc),now);return
        if g is None:
            g={'id':cid,'market':market['round_id'],'slug':market.get('slug',market['round_id']), 'created':now,
               'ends_at':market['ends_at'],'tokens':tokens,'rules':deepcopy(rules),'budget':money(budget),'spent':'0',
               'qty':{'Up':'0','Down':'0'},'cost':{'Up':'0','Down':'0'},'pnl':'0','fees':'0','paired':'0',
               'claim':'0','claim_at':None,'first_fill':None,'result':None,'quality_ok':True,'closed':False,
               'phase':'QUOTING','retry_after':0,'rescue_attempts':0}
        g['budget']=money(budget);g['rules']=deepcopy(rules);self.save_group(g)
        for side in ('Up','Down'): self.new_order(g,side,prices[side],q,now)
        self.last_decision={'at':now,'prices':{s:money(p) for s,p in prices.items()},'pair_cost':money(sum(prices.values())),
                            'quantity':money(q),'potential_pair_profit':money(q*(ONE-sum(prices.values())))}
        self.reason('QUOTING','双边模拟买单已提交，等待真实卖出成交穿过排队',now)

    def nav(self, now):
        if self.conn.execute("SELECT 1 FROM pair_groups WHERE json_extract(json,'$.phase')='RESULT_CONFLICT'").fetchone():return None
        total=dec(self.wallet()['cash'])
        for row in self.conn.execute("SELECT json FROM pair_fills WHERE status='MATCHED'"):
            f=json.loads(row[0])
            if f['action']=='SELL': total+=dec(f['cash'])
        for g in self.groups():
            if g['phase']=='RESULT_CONFLICT': return None
            total+=dec(g['claim']);paired=min(dec(g['qty']['Up']),dec(g['qty']['Down']));total+=paired
            for side in ('Up','Down'):
                left=dec(g['qty'][side])-paired
                if left<=0: continue
                if g['result'] is not None: total+=left if g['result']==side else ZERO;continue
                b=self.book_at(g['tokens'][side],now)
                if not b: return None
                for level in sorted(b['bids'],key=lambda x:dec(x['price']),reverse=True):
                    q=min(left,dec(level['size']));p=dec(level['price']);total+=q*p-fee(q,p,g['rules']);left-=q
                    if left<=0: break
                if left>0: return None
        return total

    def snapshot(self, now):
        with self.lock:
            groups=self.groups(False);active=[g for g in groups if not g['closed']];wallet=self.wallet();nav=self.nav(now)
            completed=[g for g in groups if g['closed'] and g['first_fill'] is not None]
            fills=[json.loads(r[0]) for r in self.conn.execute('SELECT json FROM pair_fills ORDER BY due DESC LIMIT 100')]
            orders=[json.loads(r[0]) for r in self.conn.execute('SELECT json FROM pair_orders ORDER BY id DESC LIMIT 100')]
            total_curve=self.conn.execute('SELECT max(id) FROM pair_curve').fetchone()[0] or 0
            stride=max(1,(total_curve+299)//300)
            curve=[dict(r) for r in self.conn.execute('SELECT at,nav,cash,pnl FROM pair_curve WHERE id=1 OR id%?=0 OR id=? ORDER BY id',(stride,total_curve))]
            return {'enabled':True,'name':NAME,'strategy_id':STRATEGY_ID,'mode':'PAPER','version':VERSION,'live_enabled':False,
                'settings':dict(self.config),'external_pause':self.entry_pause_reason,'reason':self.last_reason,'reason_code':self.last_code,'last_tick':self.last_at,
                'last_decision':deepcopy(self.last_decision),'market':self.current_market,'initial_cash':wallet['initial'],
                'cash':wallet['cash'],'available_cash':money(dec(wallet['cash'])-self.reserved()),'reserved':money(self.reserved()),
                'nav':money(nav) if nav is not None else None,'last_nav':self.last_nav,
                'realized_pnl':wallet['realized'],'total_pnl':money(nav-dec(wallet['initial'])) if nav is not None else None,
                'claim_receivable':money(sum((dec(g['claim']) for g in active),ZERO)),
                'active_markets':len(active),'filled_markets':sum(g['first_fill'] is not None for g in groups),
                'completed_markets':len(completed),'win_rate':sum(dec(g['pnl'])>0 for g in completed)/len(completed) if completed else None,
                'quality_affected_markets':sum(not g['quality_ok'] for g in groups),'stats':dict(self.stats),
                'groups':list(reversed(groups[-100:])),'orders':orders,'fills':fills,
                'events':[{'at':r['at'],'kind':r['kind'],**json.loads(r['json'])} for r in self.conn.execute('SELECT * FROM pair_events ORDER BY id DESC LIMIT 30')],
                'curve':curve,
                'assumptions':{'maker_fee':'0','maker_rebate':'0','queue':'到达时同价可见买单排在前面；更早的盘口缩量可减少前方队列，但不能直接产生成交',
                    'fills':'真实 SELL 消耗排队后最多参与25%；另对严格卖盘穿价按原买价/25%深度记保守假设成交，双边独立',
                    'settlement':'等量已确认份额模拟合并；未配平份额按官方最终结果处理',
                    'evidence':'ASSUMED — 本地模拟排队和延迟，不能等同实盘成交保证'}}
