"""按净份额计算价值；Decimal 固定预算，不能用 float epsilon 超支。"""
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP, localcontext
from functools import wraps

from .contracts import require_verified

D = Decimal
ROUNDINGS = {'DOWN':ROUND_DOWN, 'UP':ROUND_UP, 'HALF_UP':ROUND_HALF_UP,
             'ROUND_DOWN':ROUND_DOWN, 'ROUND_UP':ROUND_UP, 'ROUND_HALF_UP':ROUND_HALF_UP}


def financial_precision(fn):
    @wraps(fn)
    def wrapped(*args,**kwargs):
        with localcontext() as context:
            context.prec=128
            return fn(*args,**kwargs)
    return wrapped


def decimal(value):
    result = D(str(value))
    if not result.is_finite(): raise ValueError('NONFINITE_FINANCIAL_VALUE')
    return result


def floor_step(value, step):
    value, step = decimal(value), decimal(step)
    if step <= 0: raise ValueError('INVALID_QUANTITY_STEP')
    return (value/step).to_integral_value(rounding=ROUND_DOWN)*step


@financial_precision
def fee_adapter(contract, side, price, gross_qty, *, worst_case=False):
    """只支持已核验的 p(1-p) 费用合同；builder 等未知费用由合同拒绝。"""
    require_verified(contract)
    if side not in ('Up','Down','UP','DOWN'): raise ValueError('INVALID_SIDE')
    fee = contract['trading']['fee_contract']
    p, qty = decimal(price), decimal(gross_qty)
    if not 0 < p < 1 or qty <= 0: raise ValueError('INVALID_ORDER_SIZE_OR_PRICE')
    if fee['verification_status'] != 'VERIFIED': raise ValueError('FEE_UNVERIFIED')
    mode = fee['collection_mode']
    if fee['fee_formula_id'] not in ('CRYPTO_P_ONE_MINUS_P','ZERO_FEE'):
        raise ValueError('FEE_FORMULA_UNSUPPORTED')
    rate = decimal(fee['coefficient'])
    if not 0 <= rate <= 1: raise ValueError('FEE_COEFFICIENT_INVALID')
    raw_fee = D(0) if fee['fee_formula_id']=='ZERO_FEE' else qty*rate*p*(1-p)
    increment = decimal(fee['rounding_increment'])
    if increment <= 0: raise ValueError('FEE_PRECISION_UNKNOWN')
    minimum = decimal(fee['minimum_fee'])
    rounding = fee['rounding_mode']
    if rounding == 'EXACT':
        paid_fee = max(minimum, raw_fee) if raw_fee else D(0)
    elif rounding in ROUNDINGS:
        # The smallest asset unit bounds the number of possible child fills;
        # do not assume partial fills obey the submitted quantity step.
        if worst_case:
            max_legs = (qty*(D(10)**contract['trading']['quantity_decimals'])).to_integral_value(rounding=ROUND_UP)
            rounding_bound = D(0) if ROUNDINGS[rounding] == ROUND_DOWN else increment*max_legs
            paid_fee = raw_fee+rounding_bound+minimum*max_legs
        else:
            paid_fee = max(minimum, (raw_fee/increment).to_integral_value(rounding=ROUNDINGS[rounding])*increment) if raw_fee else D(0)
    else: raise ValueError('FEE_ROUNDING_UNVERIFIED')
    cash = qty*p
    if mode == 'CASH_ADDED':
        net, fee_cash, fee_shares = qty, paid_fee, D(0)
        cash += fee_cash
    elif mode == 'SHARES_DEDUCTED':
        fee_shares = paid_fee/p
        step = D(1)/(D(10)**contract['trading']['quantity_decimals'])
        fee_shares = (fee_shares/step).to_integral_value(rounding=ROUND_UP)*step
        net, fee_cash = qty-fee_shares, D(0)
    elif mode == 'NONE' and paid_fee == 0:
        net, fee_cash, fee_shares = qty, D(0), D(0)
    else: raise ValueError('FEE_COLLECTION_UNVERIFIED')
    if net <= 0: raise ValueError('NO_NET_SHARES')
    return {k:str(v) for k,v in {'cash_paid':cash,'net_qty':net,'gross_qty':qty,
        'fee_cash':fee_cash,'fee_shares':fee_shares,'cost_per_share':cash/net}.items()}


@financial_precision
def plan_side(contract, side, book, guard, config):
    require_verified(contract)
    rule=config['entry'];trading=contract['trading'];tick=decimal(trading['tick_size']);step=decimal(trading['quantity_step'])
    if not book or not book.get('is_complete') or not book.get('asks') or not book.get('bids'):
        raise ValueError('BOOK_INVALID')
    clean={}
    for name in ('asks','bids'):
        clean[name]=[]
        for row in book[name]:
            size=decimal(row.get('quantity_shares',row.get('size','-1')));price=decimal(row['price'])
            if size<0 or not 0<price<1:raise ValueError('BOOK_INVALID')
            if size>0:clean[name].append(price)
        if not clean[name]:raise ValueError('BOOK_INVALID')
    ask=min(clean['asks']);bid=max(clean['bids'])
    if ask<decimal(rule['minimum_best_ask']): raise ValueError('PRICE_TOO_LOW')
    if not 0<bid<=ask<1:raise ValueError('BOOK_INVALID')
    if ask-bid>decimal(rule['max_spread']):raise ValueError('SPREAD_TOO_WIDE')
    probability=decimal(guard);budget=decimal(rule['budget_usd'])
    if not 0<=probability<=1:raise ValueError('INVALID_PROBABILITY')
    ceiling=floor_step(min(decimal(rule['hard_limit_price_cap']),ask+tick*rule['max_slippage_ticks']),tick)
    best=None;minimum_passed=False
    if int(ceiling/tick)>100000:raise ValueError('PRICE_GRID_UNSUPPORTED')
    for index in range(int(ceiling/tick), int((ask/tick).to_integral_value(rounding=ROUND_UP))-1, -1):
        limit=tick*index
        # Binary search quantity on legal steps, including fee/rounding bounds.
        low,high=0,int(budget/limit/step)
        while low<high:
            mid=(low+high+1)//2
            try:quote=fee_adapter(contract,side,limit,mid*step,worst_case=True);fits=decimal(quote['cash_paid'])<=budget
            except ValueError as exc:
                if str(exc)=='NO_NET_SHARES':fits=False
                else:raise
            if fits:low=mid
            else:high=mid-1
        if low==0:continue
        qty=low*step;quote=fee_adapter(contract,side,limit,qty,worst_case=True)
        minimum=decimal(trading['min_order_size']);unit=trading['min_order_size_unit']
        if (qty if unit=='SHARES' else qty*limit if unit=='PRE_FEE_COLLATERAL' else decimal(quote['cash_paid']) if unit=='ALL_IN_COLLATERAL' else D(-1))<minimum:
            continue
        minimum_passed=True
        cost=decimal(quote['cost_per_share']);edge=probability-cost;roi=probability/cost-1
        if edge<decimal(rule['minimum_guard_edge']) or roi<decimal(rule['minimum_guard_roi']):continue
        expected=decimal(quote['net_qty'])*(probability-cost)
        best={'side':side,'limit_price':str(limit),'gross_qty':str(qty),'budget_usd':str(budget),
              'guard_probability':float(probability),'cost':quote,'guard_edge':str(edge),'guard_roi':str(roi),
              'guard_expected_value':str(expected),'contract_hash':contract['contract_sha256']}
        break
    if best is None:raise ValueError('VALUE_EDGE_FAILED' if minimum_passed else 'BELOW_MARKET_MINIMUM')
    return best


def cost_quote(contract,side,price,quantity,*,worst_case=True):
    q=fee_adapter(contract,side,price,quantity,worst_case=worst_case)
    fee=contract['trading']['fee_contract']
    return {'fee_model_id':fee['fee_model_id'],'fee_collection_mode':fee['collection_mode'],
            'gross_shares':q['gross_qty'],'net_shares':q['net_qty'],
            'cash_before_fees':str(decimal(price)*decimal(quantity)),
            'fee_cash':q['fee_cash'],'fee_shares':q['fee_shares'],
            'all_in_cash':q['cash_paid'],'all_in_cost_per_net_share':q['cost_per_share'],
            'levels_used':[{'price':str(price),'gross_shares':q['gross_qty'],'fee_cash':q['fee_cash'],'fee_shares':q['fee_shares']}],
            'rounding_rule_id':fee['rounding_mode']}


def choose_entry(contract, books, guards, config):
    candidates=[];reasons=[]
    for side in ('Up','Down'):
        try:candidates.append(plan_side(contract,side,books.get(side),guards[side],config))
        except ValueError as exc:reasons.append(side+':'+str(exc))
    candidates.sort(key=lambda p:decimal(p['guard_expected_value']),reverse=True)
    if len(candidates)==2 and abs(decimal(candidates[0]['guard_expected_value'])-decimal(candidates[1]['guard_expected_value']))<=decimal(config['entry']['value_tie_tolerance_usd']):
        return None,['VALUE_TIE']
    return (candidates[0] if candidates else None),reasons
