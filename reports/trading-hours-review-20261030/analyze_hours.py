"""Read-only hourly performance attribution on immutable SQLite snapshots."""
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import statistics
from zoneinfo import ZoneInfo

OUT = Path(__file__).resolve().parent
META = json.loads((OUT / "snapshot-manifest.json").read_text())
TZ = ZoneInfo("Asia/Shanghai")
CUTOFF = META["as_of"]
RECENT = datetime(2026, 10, 26, tzinfo=TZ).timestamp()


def net_sum(values):
    return sum(round(x * 1_000_000) for x in values) / 1_000_000


def local(at):
    return datetime.fromtimestamp(at, TZ)


def window_label(hour, length=1):
    end = hour + length
    return f"{hour:02d}:00–{end if end <= 24 else end - 24:02d}:00"


def summarize(rows, time_key, hour, length=1):
    selected = [r for r in rows if (local(r[time_key]).hour-hour) % 24 < length]
    days = defaultdict(list)
    for row in selected:
        stamp = local(row[time_key])
        day = stamp.date()-timedelta(days=int(stamp.hour < hour))
        days[day.isoformat()].append(row)
    daily = []
    for day, entries in sorted(days.items()):
        daily.append({"date": day, "trades": len(entries), "pnl": net_sum(r["pnl"] for r in entries),
                      "stake": net_sum(r["stake"] for r in entries)})
    pnls = [r["pnl"] for r in selected]
    d = [x["pnl"] for x in daily]
    total = net_sum(pnls)
    stake = net_sum(r["stake"] for r in selected)
    return {"hour": hour, "length": length, "label": window_label(hour, length), "trades": len(selected),
            "pnl": total, "stake": stake, "roi_pct": total / stake * 100 if stake else None,
            "avg_pnl_per_trade": total / len(selected) if selected else None,
            "win_rate_pct": sum(x > 0 for x in pnls) / len(selected) * 100 if selected else None,
            "days": len(d), "positive_days": sum(x > 0 for x in d), "negative_days": sum(x < 0 for x in d),
            "flat_days": sum(x == 0 for x in d),
            "positive_day_rate_pct": sum(x > 0 for x in d) / len(d) * 100 if d else None,
            "median_day_pnl": statistics.median(d) if d else None,
            "mean_day_pnl": total / len(d) if d else None,
            "best_day_pnl": max(d) if d else None, "worst_day_pnl": min(d) if d else None,
            "without_best_day": net_sum([total, -max(d)]) if d else None,
            "without_best_two_days": net_sum([total, *[-x for x in sorted(d, reverse=True)[:2]]]) if len(d) >= 2 else None,
            "without_best_trade": net_sum([total, -max(pnls)]) if pnls else None,
            "without_best_three_trades": net_sum([total, *[-x for x in sorted(pnls, reverse=True)[:3]]]) if len(pnls) >= 3 else None,
            "daily": daily}


def profiles(rows, time_key):
    return [summarize(rows, time_key, hour) for hour in range(24)]


def read_account(key, info):
    conn = sqlite3.connect(Path(info["snapshot"]).as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    raw = [dict(r) for r in conn.execute("""SELECT t.*,m.ends_at FROM trades t
        LEFT JOIN market_rounds m USING(round_id) WHERE t.opened_at<=? ORDER BY t.opened_at,t.id""", (CUTOFF,))]
    closed = [r for r in raw if r["status"] == "SETTLED" and r["settled_at"] <= CUTOFF]
    trades = []
    for row in closed:
        assert row["settled_at"] >= row["opened_at"] and row["ends_at"] is not None
        trades.append({"id": row["id"], "round_id": row["round_id"], "side": row["side"],
                       "entry_at": row["opened_at"], "realized_at": row["settled_at"], "market_end": row["ends_at"],
                       "pnl": row["pnl"], "stake": row["stake"], "source": row["settlement_source"]})
    legs = list(trades)
    if key == "MAIN_TP_SL":
        # Partial sales belong to one original entry, not multiple independent bets.
        trades = []
        for row in conn.execute("""SELECT l.*,m.ends_at FROM exit_links l
            JOIN market_rounds m USING(round_id) WHERE l.trade_id IS NOT NULL AND l.parent_opened_at<=?""", (CUTOFF,)):
            parts = [dict(p) for p in conn.execute("SELECT t.* FROM exit_parts p JOIN trades t ON t.id=p.trade_id WHERE p.parent_id=?", (row["parent_id"],))]
            if any(p["status"] != "SETTLED" or p["settled_at"] > CUTOFF for p in parts):
                continue
            assert abs(sum(p["stake"] for p in parts)-row["original_stake"]) <= .000002
            trades.append({"id": row["parent_id"], "round_id": row["round_id"], "side": row["side"],
                           "entry_at": row["parent_opened_at"], "realized_at": max(p["settled_at"] for p in parts),
                           "market_end": row["ends_at"], "stake": row["original_stake"],
                           "pnl": net_sum(p["pnl"] for p in parts), "source": "grouped_entry",
                           "hold_pnl": row["hold_pnl"], "resolved_at": row["resolved_at"]})
        assert abs(net_sum(r["pnl"] for r in trades)-net_sum(r["pnl"] for r in legs)) < .000001
    periods = {
        "all": trades,
        "recent": [r for r in trades if r["entry_at"] >= RECENT],
        "legacy": [r for r in trades if r["entry_at"] < RECENT],
    }
    result = {"name": info["name"], "raw_trade_rows": len(raw), "settled_trade_rows": len(closed),
              "closed_original_entries": len(trades), "pending_rows": len(raw)-len(closed),
              "ledger_realized_pnl": net_sum(r["pnl"] for r in legs),
              "first_entry": local(min(r["entry_at"] for r in trades)).isoformat(),
              "last_entry": local(max(r["entry_at"] for r in trades)).isoformat(),
              "active_entry_days": len({local(r["entry_at"]).date() for r in trades}),
              "account": dict(conn.execute("SELECT * FROM account").fetchone()),
              "periods": {}, "trades": trades, "realized_legs": legs}
    for name, cohort in periods.items():
        record = {"trades": len(cohort), "pnl": net_sum(r["pnl"] for r in cohort),
                  "entry_hours": profiles(cohort, "entry_at"),
                  "entry_two_hours": [summarize(cohort, "entry_at", h, 2) for h in range(24)],
                  "market_end_hours": profiles(cohort, "market_end")}
        assert abs(net_sum(x["pnl"] for x in record["entry_hours"])-record["pnl"]) < .000001
        assert sum(x["trades"] for x in record["entry_hours"]) == len(cohort)
        result["periods"][name] = record
    receipt_periods = {"all": legs, "recent": [r for r in legs if r["realized_at"] >= RECENT]}
    result["cashflow"] = {name: profiles(cohort, "realized_at") for name, cohort in receipt_periods.items()}
    for name, cohort in receipt_periods.items():
        assert abs(net_sum(x["pnl"] for x in result["cashflow"][name])-net_sum(r["pnl"] for r in cohort)) < .000001
    lag = [r["realized_at"]-r["market_end"] for r in legs if r["source"] == "polymarket_official"]
    result["quality"] = {"unknown_source_entries": sum(r["source"] is None for r in legs),
                         "official_resolution_median_delay_seconds": statistics.median(lag) if lag else None,
                         "official_resolution_max_delay_seconds": max(lag) if lag else None,
                         "official_delay_over_1h_count": sum(x > 3600 for x in lag),
                         "entries_after_market_end": sum(r["entry_at"] >= r["market_end"] for r in trades)}
    # Recording activity does not prove quote freshness; show it separately from trade outcomes.
    minutes = [dict(x) for x in conn.execute("""SELECT strftime('%Y-%m-%d %H',created_at,'unixepoch','+8 hours') slot,
        count(DISTINCT cast(created_at/60 AS INTEGER)) recorded_minutes FROM equity_curve
        WHERE created_at>=? AND created_at<=? GROUP BY slot""", (RECENT, CUTOFF))]
    result["equity_recording_minutes_recent"] = minutes
    if key == "MAIN":
        curve = [dict(x) for x in conn.execute("SELECT id,created_at,total_equity FROM equity_curve WHERE created_at>=? AND created_at<=? ORDER BY created_at,id", (RECENT-3600, CUTOFF))]
        stamps = [r["created_at"] for r in curve]
        focus = []
        for day in (26, 27, 28, 29):
            start = datetime(2026, 10, day, 23, tzinfo=TZ).timestamp()
            end = start+3600
            before = bisect_left(stamps, start)-1
            last = bisect_left(stamps, end)-1
            points = curve[before:last+1]
            peak = points[0]["total_equity"]
            drawdown = 0
            for point in points:
                peak = max(peak, point["total_equity"])
                drawdown = max(drawdown, peak-point["total_equity"])
            entered = [r for r in trades if start <= r["entry_at"] < end]
            realized = [r for r in legs if start <= r["realized_at"] < end]
            gain = curve[last]["total_equity"]-curve[before]["total_equity"]
            assert abs(gain-net_sum(r["pnl"] for r in realized)) < .000002
            focus.append({"date": f"2026-10-{day}", "entry_trades": len(entered), "entry_pnl": net_sum(r["pnl"] for r in entered),
                          "realized_trades": len(realized), "realized_pnl": net_sum(r["pnl"] for r in realized),
                          "observed_equity_change": round(gain, 6), "observed_intrahour_drawdown": round(drawdown, 6),
                          "start_sample_age_seconds": start-points[0]["created_at"],
                          "end_sample_age_seconds": end-points[-1]["created_at"],
                          "max_sample_gap_seconds": max(b["created_at"]-a["created_at"] for a,b in zip(points, points[1:])),
                          "points": [{"minutes": (x["created_at"]-start)/60,
                                      "pnl": round(x["total_equity"]-points[0]["total_equity"],6)} for x in points]})
        result["recent_23_equity"] = focus
        receipts = [r for r in legs if r["realized_at"] >= RECENT and local(r["realized_at"]).hour == 23]
        by_entry = defaultdict(list)
        for r in receipts:
            by_entry[local(r["entry_at"]).hour].append(r)
        result["recent_23_cashflow_sources"] = [{"entry_hour": h, "trades": len(rs), "pnl": net_sum(r["pnl"] for r in rs)} for h,rs in sorted(by_entry.items())]
        outgoing = [r for r in trades if r["entry_at"] >= RECENT and local(r["entry_at"]).hour == 23 and local(r["realized_at"]).hour != 23]
        result["recent_23_entries_realized_later"] = {"trades": len(outgoing), "pnl": net_sum(r["pnl"] for r in outgoing),
                                                   "rows": outgoing}
    conn.close()
    return result


def main():
    accounts = {key: read_account(key, info) for key, info in META["accounts"].items()}
    shared_start = max(r["account"]["created_at"] for r in accounts.values())
    for account in accounts.values():
        shared = [r for r in account["trades"] if r["entry_at"] >= shared_start]
        account["periods"]["common"] = {"trades": len(shared), "pnl": net_sum(r["pnl"] for r in shared),
                                            "entry_hours": profiles(shared, "entry_at")}
    analysis = {"as_of": META["as_of_local"], "timezone": "Asia/Shanghai (UTC+8)",
                "recent_start": local(RECENT).isoformat(), "common_start": local(shared_start).isoformat(),
                "accounts": accounts,
                "method": ["Net profits use the ledger's recorded PnL and historical fee model; unrecorded fees are not retroactively inferred.",
                           "Entry attribution assigns the final recorded PnL to actual buy time. Realized attribution assigns it to settlement/sale time. They answer different questions.",
                           "Positive-day fractions are conditional on days with trades in that window; absent trades and data gaps are not counted as profitable days.",
                           "Recent means entries since 2026-10-26 China time. Main history before the restart is shown separately; calendar gaps are not fabricated as trading days.",
                           "An hourly positive total does not imply a monotonically rising curve. Observed equity drawdown can miss excursions across sample gaps.",
                           "Two-hour windows overlap; their profits must not be added. Rankings are exploratory, not independent validation or a schedule-filter backtest.",
                           "Account sizes, $5/$2 stakes, participation and launch dates differ. The common four-account period contains no night-time observations.",
                           "Partial exit rows are grouped by original entry for trade counts; realized cash flow retains individual sale timestamps."]}
    (OUT / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2, allow_nan=False))
    for key, a in accounts.items():
        print(key, a["closed_original_entries"], a["ledger_realized_pnl"])
        for period in ("all", "recent"):
            top = sorted(a["periods"][period]["entry_hours"], key=lambda x: -x["pnl"])[:3]
            print(period, [{k: r[k] for k in ("label", "trades", "pnl", "positive_days", "days", "without_best_day")} for r in top])
    print("MAIN_23_SOURCES", accounts["MAIN"]["recent_23_cashflow_sources"])
    print("MAIN_23_LATER", {k:v for k,v in accounts["MAIN"]["recent_23_entries_realized_later"].items() if k != "rows"})


if __name__ == "__main__":
    main()
