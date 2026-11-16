"""Independent optimized-account experiment informed by the live ledger review.

The original forecast, cost/risk settings and all other strategy classes remain
unchanged. Historical selection findings motivate a forward experiment, not a
claim that this policy has already earned the screened historical profits.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

from .models import Signal
from .optimized_main import OptimizedMainBot

VARIANT_ID="MAIN_OPTIMIZED_PLUS"
LABEL="主账户（优化进阶版）"
POLICY_VERSION="main-optimized-plus-20261116-v1"
POLICY={"one_entry_per_market":True,"up_momentum_window_seconds":30,
        "up_momentum_max_bps":2.,"late_entry_after_seconds":120,
        "late_entry_filter_enabled":False}
_FIELDS=frozenset({
    "policy_version","decision_id","decision_at","observed_at","round_id","mode",
    "code","state","reason","stage","execution_reason","execution_state",
    "baseline_allowed","plus_allowed","candidate_side","score","base_selected_score","up_score",
    "entry_price","ask","bid","all_in_cost","limit_price","min_edge","expected_edge",
    "fee_rate","fee_exponent","fee_per_share","fee_source","fee_adjusted_edge",
    "quote_source_ms","quote_received_ms","time_left_seconds","market_ends_at","started_at",
    "target_price","move_bps","projected_final_twap","forecast_source_ms","snapshot_id","order_id",
    "submission_limit","submission_all_in_cost","submission_expected_edge",
    "same_round_blocked","up_chase_blocked","late_entry","momentum_30_bps","momentum_available",
    "momentum_source_ms","momentum_received_ms","momentum_start_ms","momentum_span_seconds",
    "momentum_reason","up_momentum_max_bps",
})


def compact_plus_decision(features,*,mode=None,**updates):
    values={**(features if isinstance(features,dict) else {}),**updates}
    if mode is not None:values["mode"]=mode
    result={}
    for key in _FIELDS:
        if key not in values:continue
        value=values[key]
        if value is None or isinstance(value,bool):result[key]=value
        elif isinstance(value,(int,float)) and math.isfinite(value):result[key]=value
        elif isinstance(value,str):result[key]=value[:240]
    return result


class PlusDecisionJournal:
    """Bounded diagnostic logs; a diagnostic disk error never suspends orders."""
    def __init__(self,path,mode,max_bytes=4*1024*1024):
        self.path,self.mode,self.max_bytes=Path(path),mode,max_bytes
        self.lock=threading.RLock();self._last_key=None;self._last_at=0.;self.error=None

    def record(self,features,*,force=False):
        row=compact_plus_decision(features,mode=self.mode);now=time.time()
        key=tuple(row.get(k) for k in ("round_id","code","state","candidate_side","plus_allowed","stage"))
        with self.lock:
            if not force and key==self._last_key and now-self._last_at<5:return False
            row["logged_at"]=now
            encoded=json.dumps(row,ensure_ascii=False,allow_nan=False,separators=(",",":"))+"\n"
            try:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                if self.path.exists() and self.path.stat().st_size+len(encoded.encode())>self.max_bytes:
                    self.path.replace(self.path.with_suffix(self.path.suffix+".1"))
                with self.path.open("a",encoding="utf-8") as stream:stream.write(encoded)
            except OSError as exc:
                self.error=type(exc).__name__;return False
            self.error=None;self._last_key=key;self._last_at=now
            return True


def assess_plus_cost(score,all_in_cost,*,min_edge=None):
    requested=.02 if min_edge is None else min_edge
    finite=lambda v:isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
    minimum=max(.02,requested) if finite(requested) else None
    valid=all(finite(v) for v in (score,all_in_cost,minimum))
    valid=valid and .5<=score<=1 and 0<all_in_cost<1
    edge=score-all_in_cost if valid else None
    return {"allowed":bool(valid and edge+1e-12>=minimum),"min_edge":minimum,"expected_edge":edge,
            "reason":f"官方/配置费用后模型优势 {edge:.4f}" if valid else "成本或模型分数无效"}


def momentum30_context(points,*,decision_at,forecast_source_ms):
    """Log return across 30 source seconds, using only already received ticks."""
    result={"momentum_available":False,"momentum_30_bps":None,"momentum_source_ms":None,
            "momentum_received_ms":None,"momentum_start_ms":None,"momentum_span_seconds":None,
            "momentum_reason":"等待本次预测对应的连续30秒行情"}
    valid=lambda x:isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
    if not valid(decision_at) or not valid(forecast_source_ms):return result
    cutoff=min(decision_at*1000,forecast_source_ms)
    known={}
    for point in points:
        if len(point)<3:continue
        source,price,received=point[:3]
        if (not all(valid(v) for v in (source,price,received)) or price<=0
                or not cutoff-35000<=source<=cutoff or received>decision_at*1000):continue
        previous=known.get(source)
        if previous is None or received<previous[2]:known[source]=(source,price,received)
    rows=sorted(known.values())
    if not rows:return result
    latest=rows[-1];target=latest[0]-30000
    anchor=next((p for p in reversed(rows) if p[0]<=target),None)
    result.update(momentum_source_ms=latest[0],momentum_received_ms=latest[2])
    if decision_at*1000-latest[0]>3000 or cutoff-latest[0]>2000:
        result["momentum_reason"]="30秒动量所需的最新行情已过期";return result
    if anchor is None or target-anchor[0]>2000:return result
    window=[p for p in rows if p[0]>=anchor[0]]
    if any(b[0]-a[0]>4000 for a,b in zip(window,window[1:])):
        result["momentum_reason"]="30秒行情存在断档，继续等待最新完整数据";return result
    result.update(momentum_available=True,momentum_30_bps=math.log(latest[1]/anchor[1])*10000,
        momentum_start_ms=anchor[0],momentum_span_seconds=(latest[0]-anchor[0])/1000,
        momentum_reason="来自本次预测及之前已收到的行情")
    return result


def read_momentum30(references,*,decision_at,forecast_source_ms):
    points=[]
    try:
        cutoff=min(decision_at*1000,forecast_source_ms)
        with references.lock:
            points=references.conn.execute("""SELECT source_ms,price,received_ms FROM reference_ticks
                WHERE channel='price.crypto' AND source_ms BETWEEN ? AND ? AND received_ms<=?
                ORDER BY source_ms""",(cutoff-35000,cutoff,decision_at*1000)).fetchall()
    except (AttributeError,TypeError,ValueError,OSError,sqlite3.Error):pass
    return momentum30_context(points,decision_at=decision_at,forecast_source_ms=forecast_source_ms)


def _paper_window(path,started_at,asof):
    if not Path(path).exists():return {"available":False,"reason":"账本尚不可用"}
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True,timeout=2)) as conn:
            conn.row_factory=sqlite3.Row;conn.execute("PRAGMA query_only=ON");conn.execute("BEGIN")
            rows=[dict(r) for r in conn.execute("""SELECT status,opened_at,settled_at,stake,pnl FROM trades
                WHERE opened_at>=? AND opened_at<=? ORDER BY settled_at,id""",(started_at,asof))]
    except (sqlite3.Error,OSError) as exc:return {"available":False,"reason":type(exc).__name__}
    settled=[r for r in rows if r["status"]=="SETTLED" and r["settled_at"] is not None and r["settled_at"]<=asof]
    profits={}
    for r in settled:profits[r["settled_at"]]=profits.get(r["settled_at"],0.)+(r["pnl"] or 0.)
    equity=peak=drawdown=0.
    for _,pnl in sorted(profits.items()):
        equity+=pnl;peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
    wins=sum((r["pnl"] or 0)>0 for r in settled)
    return {"available":True,"settled_trades":len(settled),"wins":wins,
        "win_rate":100*wins/len(settled) if settled else None,
        "pnl":round(sum(r["pnl"] or 0 for r in settled),6),
        "normalized_5_pnl":round(sum(5*(r["pnl"] or 0)/r["stake"] for r in settled if r["stake"]>0),6),
        "drawdown":round(drawdown,6),"open_trades":len(rows)-len(settled)}


def paper_comparison(variant_path,baseline_path,*,asof=None):
    now=time.time() if asof is None else asof
    result={"started_at":None,"asof":now,"baseline":{"available":False},"variant":{"available":False},
        "note":"新策略启动以来，各自实际Paper成交；排除启动前开的旧仓。不同成交机会，非反事实回测；等额$5为成本归一展示。"}
    try:
        with closing(sqlite3.connect(Path(variant_path).resolve().as_uri()+"?mode=ro",uri=True,timeout=2)) as conn:
            row=conn.execute("SELECT created_at FROM account LIMIT 1").fetchone()
        if row is None or not isinstance(row[0],(int,float)) or not math.isfinite(row[0]):return result
        started=row[0]
    except (sqlite3.Error,OSError):return result
    result.update(started_at=started,baseline=_paper_window(baseline_path,started,now),
                  variant=_paper_window(variant_path,started,now))
    return result


class PlusMainBot(OptimizedMainBot):
    strategy_label=LABEL
    policy_version=POLICY_VERSION

    def __init__(self,settings,store):
        super().__init__(settings,store)
        self._plus_legacy_journal=PlusDecisionJournal(settings.db_path.with_suffix(".plus-decisions.jsonl"),"paper")
        self._plus_comparison_cache=None;self._plus_comparison_at=0.
        self.last_signal={"side":"NO_TRADE","reason":LABEL+"等待同源行情"}

    def evaluate(self,market,reference,quotes,now):
        context=SimpleNamespace(settings=self.settings,references=self.references,store=self.store,
            paper_trading_paused=self.paper_trading_paused,strategy_label=LABEL,
            adjust_prediction=lambda prediction,_market,_now:prediction)
        signal,features=OptimizedMainBot.evaluate(context,market,reference,quotes,now)
        features.update(policy_version=POLICY_VERSION,decision_at=now,observed_at=now,
            market_ends_at=market.ends_at,started_at=market.started_at,
            baseline_allowed=signal.side in ("Up","Down"),plus_allowed=False,
            min_edge=max(.02,self.settings.min_edge),up_momentum_max_bps=POLICY["up_momentum_max_bps"],
            late_entry=now-market.started_at>=POLICY["late_entry_after_seconds"],fee_source="not-evaluated")
        def finish(value):
            features.update(reason=value.reason,plus_allowed=value.side in ("Up","Down"),
                            state="candidate" if value.side in ("Up","Down") else "rejected")
            return value,features
        if signal.side not in ("Up","Down"):return finish(signal)
        features.update(candidate_side=signal.side,base_selected_score=features["score"],
                        entry_price=signal.entry_price,move_bps=signal.move_bps)
        features.update(read_momentum30(self.references,decision_at=now,
                                      forecast_source_ms=features.get("forecast_source_ms")))
        prior=bool(self.store.trades_for_round(market.round_id))
        chase=(signal.side=="Up" and features["momentum_available"]
               and features["momentum_30_bps"]>POLICY["up_momentum_max_bps"]+1e-9)
        features.update(same_round_blocked=prior,up_chase_blocked=bool(chase))
        def reject(code,reason):
            features["code"]=code
            return finish(Signal("BTC","NO_TRADE",0,0,0,LABEL+" ["+code+"] "+reason))
        if prior:return reject("SAME_ROUND_ENTRY","本期已有成交或待确认，不重复入场或反手")
        if signal.side=="Up" and not features["momentum_available"]:
            return reject("PLUS_CONTEXT_WAIT",features["momentum_reason"])
        if chase:return reject("UP_CHASE","Up候选遇到30秒短线上冲，等待动量回落后重新评估")
        limit=features["limit_price"]
        cost=limit+self.settings.paper_taker_fee_rate*limit*(1-limit)
        gate=assess_plus_cost(features["score"],cost,min_edge=self.settings.min_edge)
        features.update(expected_edge=gate["expected_edge"],all_in_cost=cost,
                        fee_source=getattr(self,"execution_cost_source","configured-paper"))
        if not gate["allowed"]:return reject("PLUS_COST",gate["reason"])
        return finish(replace(signal,reason=LABEL+" [ALLOW] 每期一次，方向动量检查通过；"+gate["reason"]))

    def _audit(self,record):
        self._plus_legacy_journal.record({"policy_version":POLICY_VERSION,**record},
                                         force=record.get("event")=="execution")

    def plus_policy_snapshot(self):
        parameters={**POLICY,**{key:getattr(self.settings,key) for key in
            ("min_confidence","min_edge","max_entry_price","max_spread","min_time_left_seconds","max_time_left_seconds")}}
        parameters["min_edge"]=max(.02,parameters["min_edge"])
        parameters["max_spread"]=min(.03,parameters["max_spread"])
        with self._lock:decision=dict(self.last_decision or {})
        decision["reason"]=decision.get("execution_reason") or decision.get("reason")
        now=time.time()
        if self._plus_comparison_cache is None or now-self._plus_comparison_at>=3:
            self._plus_comparison_cache=paper_comparison(self.settings.db_path,
                Path(self.settings.strategy_experiments_db_dir)/"main_optimized.sqlite3",asof=now)
            self._plus_comparison_at=now
        return {"policy_version":POLICY_VERSION,"state":"FORWARD_PAPER",
            "description":"独立升级实验：每期一次，过滤Up短线追涨；晚入场只观察。继承原优化版评分和费用规则，无新增训练等待，实际效果待对照。",
            "parameters":parameters,"last_decision":compact_plus_decision(decision),
            "comparison":self._plus_comparison_cache}

    def diagnostics(self):
        result=super().diagnostics();result["plus_policy"]=self.plus_policy_snapshot()
        journal=self._plus_legacy_journal if self.paper_execution is None else self.paper_execution.signals.plus_journal
        result.update(journal_path=str(journal.path),decision_journal_error=journal.error)
        return result
