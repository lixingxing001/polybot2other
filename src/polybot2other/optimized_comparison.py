"""Same-period comparison. Historic profits and provisional settlements are excluded."""
import json
from pathlib import Path
import threading
import time


class MainComparison:
    def __init__(self, baseline, optimized, path: Path):
        self.baseline=baseline
        self.optimized=optimized
        self.path=path
        self.lock=threading.Lock()
        self._cached=None
        self._cached_at=0
        if path.exists():
            self.manifest=json.loads(path.read_text())
            if Path(self.manifest["baseline_db"]).resolve()!=Path(baseline.db_path).resolve():
                raise ValueError("Comparison belongs to a different baseline database")
            if Path(self.manifest["optimized_db"]).resolve()!=Path(optimized.db_path).resolve():
                raise ValueError("Comparison belongs to a different optimized database")
        else:
            with baseline._lock:
                base_last=baseline.conn.execute("SELECT coalesce(max(id),0) FROM trades").fetchone()[0]
                base_metrics=baseline.metrics()
            with optimized._lock:
                opt_last=optimized.conn.execute("SELECT coalesce(max(id),0) FROM trades").fetchone()[0]
                opt_metrics=optimized.metrics()
            self.manifest={
                "version":1,"started_at":time.time(),
                "baseline_trade_cursor":base_last,"optimized_trade_cursor":opt_last,
                "baseline_start_equity":base_metrics["total_equity"],
                "optimized_start_equity":opt_metrics["total_equity"],
                "baseline_existing_positions":base_metrics["open_trades"],
                "baseline_prior_daily_pnl":base_metrics["daily_realized_pnl"],
                "baseline_db":str(baseline.db_path),"optimized_db":str(optimized.db_path),
            }
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open("x") as stream:
                json.dump(self.manifest,stream,ensure_ascii=False,indent=2)

    def _stats(self, store, cursor):
        with store._lock:
            rows=[dict(r) for r in store.conn.execute(
                "SELECT * FROM trades WHERE id>? AND opened_at>=? ORDER BY opened_at,id",
                (cursor,self.manifest["started_at"]),
            )]
            orders=store.conn.execute(
                "SELECT count(*),coalesce(sum(fee),0) FROM paper_orders WHERE created_at>=?",
                (self.manifest["started_at"],),
            ).fetchone()
        final=[r for r in rows if r["status"]=="SETTLED" and r["settlement_source"] in ("polymarket_official","early_exit")]
        final_ids={r["id"] for r in final}
        pending=[r for r in rows if r["id"] not in final_ids]
        pnl=sum(r["pnl"] or 0 for r in final)
        stake=sum(r["stake"] for r in final)
        wins=sum((r["pnl"] or 0)>0 for r in final)
        equity=peak=drawdown=0
        for r in sorted(final,key=lambda x:(x["settled_at"] or x["opened_at"],x["id"])):
            equity+=r["pnl"] or 0;peak=max(peak,equity);drawdown=max(drawdown,peak-equity)
        by_market={}
        pending_markets={r["round_id"] for r in pending}
        for r in final:
            if r["round_id"] not in pending_markets:
                by_market[r["round_id"]]=by_market.get(r["round_id"],0)+(r["pnl"] or 0)
        return {
            "fills":len(rows),"settled":len(final),"pending":len(pending),"wins":wins,
            "win_rate":round(wins/len(final)*100,4) if final else None,
            "realized_pnl":round(pnl,6),"settled_stake":round(stake,6),
            "return_on_stake_pct":round(pnl/stake*100,4) if stake else None,
            "fees":round(orders[1],6),"order_attempts":orders[0],
            "settled_sequence_drawdown":round(drawdown,6),
            "pending_stake":round(sum(r["stake"] for r in pending),6),
            "by_market":by_market,
        }

    def snapshot(self):
        with self.lock:
            now=time.time()
            if self._cached is not None and now-self._cached_at<2:
                return self._cached
            base=self._stats(self.baseline,self.manifest["baseline_trade_cursor"])
            optimized=self._stats(self.optimized,self.manifest["optimized_trade_cursor"])
            common=set(base["by_market"]) & set(optimized["by_market"])
            difference=optimized["realized_pnl"]-base["realized_pnl"]
            common_difference=sum(optimized["by_market"][m]-base["by_market"][m] for m in common)
            payload={
                "started_at":self.manifest["started_at"],
                "baseline_start_equity":self.manifest["baseline_start_equity"],
                "optimized_start_equity":self.manifest["optimized_start_equity"],
                "baseline":{k:v for k,v in base.items() if k!="by_market"},
                "optimized":{k:v for k,v in optimized.items() if k!="by_market"},
                "realized_pnl_difference":round(difference,6),
                "win_rate_difference_pp":round(optimized["win_rate"]-base["win_rate"],4) if optimized["win_rate"] is not None and base["win_rate"] is not None else None,
                "common_settled_markets":len(common),
                "common_market_pnl_difference":round(common_difference,6),
                "status":"待共同样本" if len(common)<30 else "初步观察，尚不能证明稳定提升",
                "scope":"只统计对照开始后的新成交；盈亏只计官方结算或实际提前平仓；回撤为已结算序列，不含浮动市值。",
                "baseline_existing_positions_excluded":self.manifest["baseline_existing_positions"],
                "baseline_prior_daily_pnl":self.manifest["baseline_prior_daily_pnl"],
                "configuration_note":"起始权益和单笔预算对齐；原账户保留自己的历史及滚动风控状态。",
            }
            self._cached=payload;self._cached_at=now
            return payload
