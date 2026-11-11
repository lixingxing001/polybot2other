#!/usr/bin/env python3
"""衡准 V1 的纯离线数学参考与黄金用例自检。

本文件不访问网络、不发送订单、不读取钱包、不训练模型。
全部例子均为 SCENARIO 人工数值场景，不构成历史回测或盈利证据。

Python 仅使用标准库，便于其他 AI 复现或翻译成 Java。
运行：python reference_math.py --self-test
示例：python reference_math.py --example

重要口径：
1. 模型时间取最新有效价格的源时间 t_obs；开仓时间限制使用 wall 时间。
2. sigma2 的单位是美元平方/秒，TWAP 积分单位是美元乘秒。
3. 代理 TWAP 误差方差按独立工作假设加到过程方差上。
4. 0.25 bps/sqrt(s) 波动下限与 1 bps 代理误差下限仅为研究初值。
5. 手续费示例采用现金另加费用；真实接入须重新核验市场参数和舍入规则。
6. 份额精度、最小份额、价格 tick 必须由调用方提供，本文件不宣称市场值。
"""

from __future__ import annotations

import argparse
import json
import math
from decimal import Decimal, ROUND_FLOOR, localcontext
from pathlib import Path
from typing import Any


H_SECONDS = 60.0
SCENARIO_SIGMA_FLOOR_BPS_SQRT_S = 0.25
SCENARIO_PROXY_SD_FLOOR_BPS = 1.0
NUMERIC_PROBABILITY_CLIP = 1e-12
DECIMAL_PRECISION = 50


def _finite(value: Any, name: str) -> float:
    """拒绝 NaN、无穷大和布尔值，避免静默污染数学结果。"""
    if isinstance(value, bool):
        raise ValueError(f"{name} 不接受布尔值")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} 必须为有限数值")
    return result


def _decimal(value: Any, name: str = "数值") -> Decimal:
    """通过十进制字符串转换，避免把二进制浮点误差带入金额。"""
    if isinstance(value, bool):
        raise ValueError(f"{name} 不接受布尔值")
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"{name} 必须为有限数值")
    return result


def normal_cdf(z: float) -> float:
    """标准正态分布 CDF；输入来自先验工作模型。"""
    return 0.5 * math.erfc(-_finite(z, "z") / math.sqrt(2.0))


def source_timing(
    end_ts_ms: float, wall_ts_ms: float, source_ts_ms: float
) -> dict[str, float]:
    """三个时间均由调用方传入，单位为毫秒，不读取系统当前时间。"""
    end = _finite(end_ts_ms, "end_ts_ms")
    wall = _finite(wall_ts_ms, "wall_ts_ms")
    source = _finite(source_ts_ms, "source_ts_ms")
    if source > wall:
        raise ValueError("价格源时间晚于决策时间，须先解决时钟或数据问题")
    if source > end:
        raise ValueError("源时间已经超过结算时刻，不能用本函数预测未来 TWAP")
    return {
        "tau_wall_s": (end - wall) / 1000.0,
        "tau_model_s": (end - source) / 1000.0,
        "source_age_s": (wall - source) / 1000.0,
    }


def twap_prior(
    tau_model_s: float,
    source_price: float,
    reference_twap: float,
    sigma2_usd2_per_s: float,
    known_integral: float | None = None,
    proxy_error_variance_usd2: float = 0.0,
    proxy_bias_usd: float = 0.0,
    sigma_floor_bps_sqrt_s: float = SCENARIO_SIGMA_FLOOR_BPS_SQRT_S,
    proxy_sd_floor_bps: float = SCENARIO_PROXY_SD_FLOOR_BPS,
    probability_clip: float = NUMERIC_PROBABILITY_CLIP,
) -> dict[str, float | bool | str]:
    """零漂移、算术布朗、Normal 的终局 TWAP 先验。

    known_integral 仅覆盖 [T-60, t_obs]；不能填入 t_obs 到 wall 的未知价格。
    纯公式单元测试可显式将两个 floor 设为 0；实际研究默认值属于 SCENARIO。
    概率截断只保证数值可用，不能视为概率校准或置信保证。
    """
    tau = _finite(tau_model_s, "tau_model_s")
    price = _finite(source_price, "source_price")
    reference = _finite(reference_twap, "reference_twap")
    input_sigma2 = _finite(sigma2_usd2_per_s, "sigma2_usd2_per_s")
    proxy_variance_input = _finite(proxy_error_variance_usd2, "proxy_error_variance_usd2")
    proxy_bias = _finite(proxy_bias_usd, "proxy_bias_usd")
    sigma_floor = _finite(sigma_floor_bps_sqrt_s, "sigma_floor_bps_sqrt_s")
    proxy_floor = _finite(proxy_sd_floor_bps, "proxy_sd_floor_bps")
    clip = _finite(probability_clip, "probability_clip")
    if tau < 0 or price <= 0 or reference <= 0:
        raise ValueError("剩余模型时间须非负，价格与基准须为正")
    if min(input_sigma2, proxy_variance_input, sigma_floor, proxy_floor) < 0:
        raise ValueError("方差与下限参数须非负")
    if not 0 < clip < 0.5:
        raise ValueError("概率数值截断须在 0 与 0.5 之间")

    # 两个 floor 单位不同：第一个控制每秒方差率，第二个控制 TWAP 价格误差。
    sigma2_floor = (price * sigma_floor / 10000.0) ** 2
    sigma2_used = max(input_sigma2, sigma2_floor)
    # 代理误差的 bps 以该时点 spot 归一化，此处统一用当前 S 换回美元。
    proxy_variance_floor = (price * proxy_floor / 10000.0) ** 2
    proxy_variance = max(proxy_variance_input, proxy_variance_floor)

    if tau >= H_SECONDS:
        if known_integral is not None and _finite(known_integral, "known_integral") != 0:
            raise ValueError("尚未进入最终窗口时，known_integral 应为空或为 0")
        mean = price
        variance_kernel_s = tau - 2.0 * H_SECONDS / 3.0
    else:
        if known_integral is None:
            raise ValueError("进入最终窗口后必须提供当时已经观察到的积分")
        integral = _finite(known_integral, "known_integral")
        mean = (integral + tau * price) / H_SECONDS
        variance_kernel_s = tau ** 3 / (3.0 * H_SECONDS ** 2)

    mean += proxy_bias
    process_variance = sigma2_used * variance_kernel_s
    total_variance = process_variance + proxy_variance
    if not total_variance > 0:
        raise ValueError("总方差为 0，须提供有效波动或代理误差；不能伪造 100% 先验")
    z = (mean - reference) / math.sqrt(total_variance)
    normal_probability = normal_cdf(z)
    p_up = min(1.0 - clip, max(clip, normal_probability))
    return {
        "model": "ZERO_DRIFT_ARITHMETIC_BROWNIAN_NORMAL_SCENARIO",
        "tau_model_s": tau,
        "mean": mean,
        "variance_kernel_s": variance_kernel_s,
        "sigma2_used_usd2_per_s": sigma2_used,
        "process_variance": process_variance,
        "proxy_variance": proxy_variance,
        "total_variance": total_variance,
        "normal_z": z,
        "p_up": p_up,
        "p_down": 1.0 - p_up,
        "probability_was_clipped": p_up != normal_probability,
    }


def forecast_from_timestamps(
    end_ts_ms: float, wall_ts_ms: float, source_ts_ms: float, **model_inputs: Any
) -> dict[str, Any]:
    """先从 t_obs 计算模型时间，再调用先验，禁止回退使用 wall 时间。"""
    timing = source_timing(end_ts_ms, wall_ts_ms, source_ts_ms)
    if "tau_model_s" in model_inputs:
        raise ValueError("tau_model_s 由源时间计算，不允许调用方重复传入")
    result = twap_prior(tau_model_s=timing["tau_model_s"], **model_inputs)
    return {**result, **timing}


def previous_tick_integral(
    ticks: list[list[float]], start_s: float, end_s: float
) -> dict[str, float]:
    """不规则采样的前值保持积分，返回质量指标，数据是否合格由上层判断。

    ticks 必须已经按当时可见的数据筛选，并按源时间严格递增。
    本函数无法替调用方判断某条历史 tick 在当时是否已收到。
    """
    start = _finite(start_s, "start_s")
    end = _finite(end_s, "end_s")
    if end < start or not ticks:
        raise ValueError("积分区间或样本为空")
    parsed = [(_finite(item[0], "tick时间"), _finite(item[1], "tick价格")) for item in ticks]
    if any(price <= 0 for _, price in parsed):
        raise ValueError("tick 价格须为正")
    if any(parsed[i][0] >= parsed[i + 1][0] for i in range(len(parsed) - 1)):
        raise ValueError("tick 源时间须严格递增，先去重并排序")
    anchors = [i for i, (ts, _) in enumerate(parsed) if ts <= start]
    if not anchors:
        raise ValueError("缺少积分左边界之前的价格锚点")
    anchor = anchors[-1]
    integral = 0.0
    max_source_hold = 0.0
    for i in range(anchor, len(parsed)):
        ts, price = parsed[i]
        if ts >= end:
            break
        next_ts = parsed[i + 1][0] if i + 1 < len(parsed) else end
        left = max(start, ts)
        right = min(end, next_ts)
        if right > left:
            integral += price * (right - left)
            max_source_hold = max(max_source_hold, right - ts)
    return {
        "integral": integral,
        "covered_seconds": end - start,
        "max_source_hold_s": max_source_hold,
        "left_anchor_age_s": start - parsed[anchor][0],
    }


def realized_variance_from_log_returns(
    log_returns: list[float], interval_s: float, source_price: float
) -> dict[str, float | int]:
    """固定网格、不重叠收益的未裁剪实现方差；历史覆盖门槛由上层检查。"""
    dt = _finite(interval_s, "interval_s")
    price = _finite(source_price, "source_price")
    if dt <= 0 or price <= 0 or not log_returns:
        raise ValueError("时间间隔、价格和收益样本须有效")
    returns = [_finite(item, "log_return") for item in log_returns]
    log_variance_rate = math.fsum(item * item for item in returns) / (dt * len(returns))
    dollar_variance_rate = price * price * log_variance_rate
    return {
        "sample_count": len(returns),
        "log_variance_rate": log_variance_rate,
        "dollar_variance_rate": dollar_variance_rate,
        "dollar_sigma": math.sqrt(dollar_variance_rate),
    }


def effective_cost_cash(price: Any, fee_rate: Any = "0.07") -> Decimal:
    """每净份额现金成本 q+r*q*(1-q)，未包含交易接口的额外舍入。"""
    q = _decimal(price, "price")
    r = _decimal(fee_rate, "fee_rate")
    if not 0 <= q <= 1 or not 0 <= r <= 1:
        raise ValueError("价格及此参考公式的费率须在 0 到 1 之间")
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return q + r * q * (1 - q)


def conservative_guards(
    p_fit_up: Any, p10_up: Any, p90_up: Any, buffer: Any = "0.02"
) -> dict[str, Decimal]:
    """两边保守决策值不可再次归一化；它们没有置信保证。"""
    fit, p10, p90, margin = [_decimal(v) for v in (p_fit_up, p10_up, p90_up, buffer)]
    if any(not 0 <= v <= 1 for v in (fit, p10, p90, margin)) or p10 > p90:
        raise ValueError("概率、缓冲或分位数顺序不合法")
    up = max(Decimal(0), min(fit, p10) - margin)
    down = max(Decimal(0), min(1 - fit, 1 - p90) - margin)
    return {"guard_up": up, "guard_down": down, "guard_sum": up + down}


def cost_cap_from_guard(
    guard: Any, min_edge: Any = "0.03", min_roi: Any = "0.05"
) -> Decimal:
    """把净概率优势与净收益率两个门槛合并成最高有效成本。"""
    p = _decimal(guard, "guard")
    edge = _decimal(min_edge, "min_edge")
    roi = _decimal(min_roi, "min_roi")
    if not 0 <= p <= 1 or edge < 0 or roi < 0:
        raise ValueError("guard 或优势门槛不合法")
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return max(Decimal(0), min(p - edge, p / (1 + roi)))


def maximum_price_for_cash_cost(cost_cap: Any, fee_rate: Any = "0.07") -> Decimal:
    """稳定根式，避免小差相减；尚未按市场 tick 向下取整。"""
    cap = _decimal(cost_cap, "cost_cap")
    r = _decimal(fee_rate, "fee_rate")
    if not 0 <= cap <= 1 or not 0 <= r <= 1:
        raise ValueError("成本上限和费率须在 0 到 1 之间")
    if r == 0:
        return cap
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        discriminant = (1 + r) ** 2 - 4 * r * cap
        return 2 * cap / ((1 + r) + discriminant.sqrt())


def floor_to_tick(value: Any, tick: Any) -> Decimal:
    """价格或份额按传入步长向下取整，不能使用四舍五入。"""
    amount = _decimal(value, "value")
    step = _decimal(tick, "tick")
    if amount < 0 or step <= 0:
        raise ValueError("数值须非负，步长须为正")
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return (amount / step).to_integral_value(rounding=ROUND_FLOOR) * step


def value_gates(
    guard: Any, effective_cost: Any, min_edge: Any = "0.03", min_roi: Any = "0.05"
) -> dict[str, Any]:
    """只检查数学门槛，返回通过不代表准许交易。"""
    p = _decimal(guard, "guard")
    cost = _decimal(effective_cost, "effective_cost")
    edge_min = _decimal(min_edge, "min_edge")
    roi_min = _decimal(min_roi, "min_roi")
    if cost <= 0:
        raise ValueError("有效成本须为正")
    cap = cost_cap_from_guard(p, edge_min, roi_min)
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        edge = p - cost
        roi = p / cost - 1
    return {
        "net_edge": edge,
        "guard_roi": roi,
        "cost_cap": cap,
        "passes_edge": edge >= edge_min,
        "passes_roi": roi >= roi_min,
        "passes_math_gates": cost <= cap,
    }


def budget_to_shares(
    budget: Any, price: Any, quantity_step: Any, min_shares: Any, fee_rate: Any = "0.07"
) -> dict[str, Any]:
    """预算含现金手续费；仅做份额算术，无真实订单与成交假设。"""
    budget_decimal = _decimal(budget, "budget")
    q = _decimal(price, "price")
    minimum = _decimal(min_shares, "min_shares")
    if budget_decimal < 0 or minimum <= 0:
        raise ValueError("预算须非负，最小份额须为正")
    cost = effective_cost_cash(q, fee_rate)
    if cost <= 0:
        raise ValueError("有效成本须为正")
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        raw_quantity = budget_decimal / cost
        quantity = floor_to_tick(raw_quantity, quantity_step)
        nominal = quantity * q
        fee = quantity * (cost - q)
        total = nominal + fee
    return {
        "raw_quantity_upper_bound": raw_quantity,
        "quantity": quantity,
        "nominal_cash": nominal,
        "fee_cash": fee,
        "total_cash": total,
        "unused_budget": budget_decimal - total,
        "meets_minimum_shares": quantity >= minimum,
        "within_budget": total <= budget_decimal,
    }


def equal_budget_pair_math(
    up_price: Any, down_price: Any, budget_each: Any, fee_rate: Any = "0.07"
) -> dict[str, Any]:
    """理论连续份额的双向持有损益；不模拟盘口、成交和份额舍入。"""
    budget = _decimal(budget_each, "budget_each")
    up_cost = effective_cost_cash(up_price, fee_rate)
    down_cost = effective_cost_cash(down_price, fee_rate)
    if budget <= 0 or min(up_cost, down_cost) <= 0:
        raise ValueError("预算和两边有效成本须为正")
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        up_shares = budget / up_cost
        down_shares = budget / down_cost
        total_cash = 2 * budget
        pnl_up = up_shares - total_cash
        pnl_down = down_shares - total_cash
    return {
        "up_shares": up_shares,
        "down_shares": down_shares,
        "total_cash": total_cash,
        "pnl_if_up": pnl_up,
        "pnl_if_down": pnl_down,
        "outcome_independent": up_shares == down_shares,
    }


def settlement_label(final_twap: Any, reference_twap: Any) -> str:
    """本规格按终局 >= 开盘归 UP；正式接入仍须核验具体市场规则。"""
    final = _decimal(final_twap, "final_twap")
    reference = _decimal(reference_twap, "reference_twap")
    return "UP" if final >= reference else "DOWN"


def reference_trade_example(price: Any = "0.61") -> dict[str, Any]:
    """固定人工输入的数学示例，不能被解释为当前行情或交易建议。"""
    guards = conservative_guards("0.70", "0.68", "0.72")
    cost = effective_cost_cash(price)
    gates = value_gates(guards["guard_up"], cost)
    continuous_price_cap = maximum_price_for_cash_cost(gates["cost_cap"])
    return {
        "status": "SCENARIO_ONLY_NOT_TRAINED",
        "price": _decimal(price),
        **guards,
        "effective_cost": cost,
        **gates,
        "continuous_price_cap": continuous_price_cap,
        "price_cap_at_tick_0_01": floor_to_tick(continuous_price_cap, "0.01"),
        "budget_example": budget_to_shares("5", price, "0.01", "5"),
    }


def _dispatch(operation: str, inputs: dict[str, Any]) -> Any:
    operations = {
        "twap_prior": twap_prior,
        "forecast_from_timestamps": forecast_from_timestamps,
        "source_timing": source_timing,
        "previous_tick_integral": previous_tick_integral,
        "realized_variance": realized_variance_from_log_returns,
        "effective_cost_cash": effective_cost_cash,
        "conservative_guards": conservative_guards,
        "cost_cap_from_guard": cost_cap_from_guard,
        "maximum_price_for_cash_cost": maximum_price_for_cash_cost,
        "floor_to_tick": floor_to_tick,
        "value_gates": value_gates,
        "budget_to_shares": budget_to_shares,
        "equal_budget_pair_math": equal_budget_pair_math,
        "settlement_label": settlement_label,
        "reference_trade_example": reference_trade_example,
    }
    if operation not in operations:
        raise ValueError(f"未知数学操作：{operation}")
    return operations[operation](**inputs)


def _assert_expected(actual: Any, expected: Any, abs_tol: float, path: str = "result") -> None:
    """期望字典允许只核查相关字段；金额字符串按 Decimal 精确核对。"""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise AssertionError(f"{path} 类型不符")
        for key, value in expected.items():
            if key not in actual:
                raise AssertionError(f"{path} 缺少字段 {key}")
            _assert_expected(actual[key], value, abs_tol, f"{path}.{key}")
    elif isinstance(expected, bool):
        if actual is not expected:
            raise AssertionError(f"{path}: {actual!r} != {expected!r}")
    elif isinstance(actual, Decimal) and isinstance(expected, str):
        if actual != Decimal(expected):
            raise AssertionError(f"{path}: {actual} != {expected}")
    elif isinstance(expected, (int, float)):
        if isinstance(actual, bool) or not math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=abs_tol):
            raise AssertionError(f"{path}: {actual!r} != {expected!r}")
    elif actual != expected:
        raise AssertionError(f"{path}: {actual!r} != {expected!r}")


def run_self_test(case_path: Path) -> int:
    document = json.loads(case_path.read_text(encoding="utf-8"))
    passed = 0
    failures: list[str] = []
    for case in document["cases"]:
        case_id = case["id"]
        try:
            try:
                actual = _dispatch(case["operation"], case["inputs"])
            except Exception as exc:
                if case.get("expected_error") == type(exc).__name__:
                    passed += 1
                    print(f"通过：{case_id}，已拒绝无效输入")
                    continue
                raise
            if "expected_error" in case:
                raise AssertionError(f"应抛出 {case['expected_error']}，实际未抛出")
            _assert_expected(actual, case["expected"], float(case.get("abs_tolerance", 1e-10)))
            passed += 1
            print(f"通过：{case_id}")
        except Exception as exc:
            failures.append(f"{case_id}: {type(exc).__name__}: {exc}")
            print(f"失败：{failures[-1]}")
    print(f"离线数学自检：{passed}/{len(document['cases'])} 通过；无网络、无订单、无训练结果。")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="衡准 V1 纯离线数学参考")
    parser.add_argument("--self-test", action="store_true", help="运行黄金数值用例")
    parser.add_argument("--example", action="store_true", help="输出指定人工门槛示例")
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("golden_cases.json"))
    args = parser.parse_args()
    if args.example:
        print(json.dumps(reference_trade_example(), ensure_ascii=False, indent=2, default=str))
        return 0
    return run_self_test(args.cases)


if __name__ == "__main__":
    raise SystemExit(main())
