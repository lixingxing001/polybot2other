"""Value Confirm V1: an isolated, forward-tested Paper hypothesis."""
from collections import deque
from dataclasses import replace
import json
import math
from pathlib import Path
import time
import threading

VARIANT_ID = "VALUE_CONFIRM_V1"
DISPLAY_NAME = "价值确认 V1"
POLICY_VERSION = "20261026-calibrated-v1.1"
# Officially settled observations strictly before 2026-06-09 17:35 UTC.
# (score lower bound, upper bound, wins, count); reproduced by calibrate_value_confirm.py.
SCORE_CALIBRATION = ((.6,.7,337,575), (.7,.8,345,549), (.8,.9,151,267), (.9,1.001,84,143))
MIN_PRICE = 0.20
MAX_PRICE = 0.65
MAX_DATA_AGE_MS = 3000
MIN_NET_EDGE = 0.03
MAX_SLIPPAGE = 0.01
DAILY_LOSS_LIMIT = 6.0
TOTAL_LOSS_LIMIT = 20.0


def reference_probability(score):
    if not isinstance(score,(int,float)) or not math.isfinite(score):
        return None
    for lo,hi,wins,count in SCORE_CALIBRATION:
        if lo <= score < hi:
            return (wins+10)/(count+20)
    return None


def stake_budget(account, daily_pnl=0.0):
    capital = max(0.0, float(account['initial_balance']) + float(account['realized_pnl']))
    remaining_total = max(0.0, TOTAL_LOSS_LIMIT + min(0.0, float(account['realized_pnl'])))
    remaining_daily = max(0.0, DAILY_LOSS_LIMIT + min(0.0, daily_pnl))
    return round(min(2.0, capital * .02, max(0.0, float(account['cash_balance'])), remaining_total, remaining_daily), 6)


def entry_limit(signal, fee_rate=.07):
    limit = min(MAX_PRICE, signal.entry_price + MAX_SLIPPAGE)
    return limit if signal.confidence - limit - fee_rate * limit * (1-limit) >= MIN_NET_EDGE else signal.entry_price


class ValueConfirmGuard:
    def __init__(self, store, fee_rate=.07):
        self.store = store
        self.execution_lock = threading.Lock()
        self.fee_rate = fee_rate
        self.history = deque(maxlen=180)
        self.last_decision = {}
        self._last_logged = None
        self._last_logged_at = 0.0
        self.journal = Path(store.db_path).with_suffix('.decisions.jsonl')

    def evaluate(self, market, signal, price, quotes, now=None):
        now = time.time() if now is None else now
        account = self.store.account()
        daily_pnl = self.store.daily_realized_pnl()
        budget = stake_budget(account, daily_pnl)
        reference = reference_probability(signal.confidence)
        features = {'version': VARIANT_ID, 'policy_version':POLICY_VERSION,
                    'round_id': market.round_id, 'observed_at': now,
                    'original_side': signal.side, 'heuristic_score': signal.confidence,
                    'reference_win_rate': reference, 'stake_budget': budget,
                    'target_price': market.target_price, 'entry_price': signal.entry_price,
                    'base_reason':signal.reason, 'time_left_seconds':market.ends_at-now,
                    'chainlink_price':price.get('chainlink'),
                    'source_price_timestamp_ms':price.get('chainlink_updated_ms'),
                    'quote_asks':{side:(quotes.get(side) or {}).get('best_ask') for side in ('Up','Down')},
                    'quote_timestamps_ms':{side:(quotes.get(side) or {}).get('updated_at_ms') for side in ('Up','Down')}}
        chainlink, stamp = price.get('chainlink'), price.get('chainlink_updated_ms')
        features['price_age_ms'] = now*1000-stamp if isinstance(stamp,(int,float)) and math.isfinite(stamp) else None
        fresh = self._fresh(stamp, now, MAX_DATA_AGE_MS)
        valid_price = isinstance(chainlink, (int,float)) and math.isfinite(chainlink) and chainlink > 0
        if fresh and valid_price and (not self.history or stamp > self.history[-1][0]):
            self.history.append((stamp, chainlink))
        while self.history and self.history[0][0] < now * 1000 - 90000:
            self.history.popleft()
        def finish(code, message, allow=False):
            result = replace(signal, side=signal.side if allow else 'NO_TRADE',
                             confidence=reference if allow else signal.confidence,
                             reason=f'{DISPLAY_NAME} [{code}] {message}')
            self.last_decision = {**features, 'code':code, 'allow_trade':allow, 'reason':result.reason}
            key = (market.round_id, code, signal.side)
            if key != self._last_logged or now-self._last_logged_at >= 10:
                self._append({'event':'decision', **self.last_decision})
                self._last_logged, self._last_logged_at = key, now
            return result
        if not fresh or not valid_price:
            return finish('PRICE_STALE', '等待新鲜 Chainlink 价格')
        if signal.side not in ('Up','Down'):
            return finish('BASE_BLOCK', signal.reason)
        if reference is None:
            return finish('CALIBRATION', '此分数区间没有历史校准样本')
        if not 30 <= market.ends_at-now <= 270:
            return finish('TIME_WINDOW', '仅在开盘后 30 至 270 秒考虑入场')
        quote = quotes.get(signal.side) or {}
        features['price_age_ms'] = now*1000-stamp
        features['quote_timestamp_ms'] = quote.get('updated_at_ms')
        if not self._fresh(quote.get('updated_at_ms'), now, MAX_DATA_AGE_MS):
            return finish('QUOTE_STALE', '本方向盘口过期')
        ask, bid = quote.get('best_ask'), quote.get('best_bid')
        if not all(isinstance(x,(int,float)) and math.isfinite(x) for x in (ask,bid)) or not 0 < bid <= ask < 1:
            return finish('BAD_BOOK', '盘口缺失或交叉')
        if abs(ask-signal.entry_price) > .0001:
            return finish('QUOTE_MOVED', '信号与盘口价格不同，等待下一次报价')
        if not MIN_PRICE <= ask <= MAX_PRICE:
            return finish('PRICE_BAND', '候选价格须在 0.20 至 0.65，仍需通过扣费后的优势检查')
        if ask-bid > .020001:
            return finish('SPREAD', '价差超过 0.02')
        limit = min(MAX_PRICE, ask+MAX_SLIPPAGE)
        edge = reference-limit-self.fee_rate*limit*(1-limit)
        features.update({'limit_price':limit, 'net_reference_edge':edge})
        if edge < MIN_NET_EDGE:
            return finish('FEE_EDGE', '扣除手续费和一美分滑点后参考优势不足 0.03')
        if self.store.trades_for_round(market.round_id):
            return finish('ONE_ENTRY', '本期已经成交过，不反手或重复开仓')
        if daily_pnl <= -DAILY_LOSS_LIMIT:
            return finish('DAILY_STOP', '滚动 24 小时已亏损达到 6 美元')
        if float(account['realized_pnl']) <= -TOTAL_LOSS_LIMIT:
            return finish('TOTAL_STOP', '累计已实现亏损达到 20 美元，保留账本等待复核')
        if self.store.open_trade_count('BTC') >= 1:
            return finish('POSITION_LIMIT', '已有一笔持仓，等待官方结算')
        if budget < .1:
            return finish('CASH', '模拟资金不足')
        direction = 1 if signal.side == 'Up' else -1
        confirmations = []
        for source, key in (('okx','okx'),('binance','binance_market')):
            value = price.get(key)
            basis = price.get(source+'_basis_median_bps')
            samples = price.get(source+'_basis_samples') or 0
            if not self._fresh(price.get(key+'_updated_ms'), now, 4500) or not isinstance(value,(int,float)) or not isinstance(basis,(int,float)) or samples < 5:
                continue
            if not math.isfinite(value) or not math.isfinite(basis) or value <= 0 or basis <= -10000:
                continue
            aligned = value/(1+basis/10000)
            confirmations.append(direction*(aligned-market.target_price)/market.target_price*10000)
        features['exchange_confirmation_bps'] = confirmations
        if not confirmations or max(confirmations) <= 0 or min(confirmations) < -1.5:
            return finish('CONFIRMATION', '等待交易所价格经基差校正后同向确认')
        previous = [p for ts,p in self.history if now*1000-40000 <= ts <= now*1000-20000]
        if not previous:
            return finish('WARMUP', '等待 20 秒连续价格样本')
        momentum = direction*(chainlink-previous[-1])/previous[-1]*10000
        features['momentum_20s_bps'] = momentum
        if momentum <= 0:
            return finish('MOMENTUM', '短期价格动量尚未同向')
        depth = sum(float(level.get('price',0))*float(level.get('size',0)) for level in quote.get('asks',[])
                    if 0 < float(level.get('price',0)) <= limit and float(level.get('size',0)) > 0)
        if depth < budget:
            return finish('DEPTH', '限价内可见深度不足本笔预算')
        return finish('ALLOW', f'同向确认，限价 {limit:.3f}，预算 ${budget:.2f}，参考优势 {edge:.3f}', True)

    def record_execution(self, market, trade_ids, reason):
        self._append({'event':'execution','observed_at':time.time(),'round_id':market.round_id,
                      'policy_version':POLICY_VERSION,
                      'trade_ids':list(trade_ids),'reason':reason, 'execution_mode':'PAPER'})

    def _append(self, row):
        # Append only: every observation retains the timestamp of its own features.
        with self.journal.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')

    @staticmethod
    def _fresh(stamp, now, max_age):
        return isinstance(stamp,(int,float)) and math.isfinite(stamp) and -1000 <= now*1000-stamp <= max_age
