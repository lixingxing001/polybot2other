"""Read-only, time-ordered comparison of the Paper league's independent wallets."""
from bisect import bisect_right
from collections import Counter, defaultdict
from contextlib import closing
import math
from pathlib import Path
import sqlite3
import threading
import time

from .strategy_league import catalog_hash
from .strategy_league_policies import CONTROL_KEY, POLICIES

DAY = 86400
PERIOD_DAYS = 7
MIN_CLOSED = 100
MIN_COVERAGE = .90
STRESS_PER_SHARE = .02


def _period(trades, start, end):
    """Use only positions opened in the window and fully settled by this decision."""
    selected = [t for t in trades if start <= t["opened_at"] < end and t["closed_at"] is not None
                and t["closed_at"] <= end]
    selected.sort(key=lambda t: t["closed_at"])
    pnl = shares = peak = dd = first = 0.
    for t in selected:
        pnl += t["pnl"]
        shares += t["bought_shares"]
        peak = max(peak, pnl)
        dd = max(dd, peak-pnl)
        if t["opened_at"] < (start+end)/2:
            first += t["pnl"]
    return {"start": start, "end": end, "rounds": len(selected), "pnl": round(pnl, 6),
            "stressed": round(pnl-shares*STRESS_PER_SHARE, 6), "drawdown": round(dd, 6),
            "first_half": round(first, 6), "second_half": round(pnl-first, 6)}


def _review_at(trades, control, pending_intervals, start):
    closed = sorted(t["closed_at"] for t in trades if t["opened_at"] >= start and t["closed_at"] is not None)
    if len(closed) < MIN_CLOSED:
        return None
    days = max(PERIOD_DAYS, math.ceil((closed[MIN_CLOSED-1]-start)/DAY))
    end = start + days*DAY
    # Decisions before the boundary must finish in that sample. A completed past boundary stays fixed.
    intervals = [(t["opened_at"], t["closed_at"]) for t in (*trades, *control)]
    for opened, released in sorted((*intervals, *pending_intervals), key=lambda pair: pair[0]):
        if opened < start:
            continue
        if opened >= end:
            break
        if released is None:
            return None
        if released > end:
            end = math.ceil(released/300)*300
    return end


def _coverage(valid_ends, start, end):
    expected = max(0, int((end-start)//300))
    actual = bisect_right(valid_ends, end) - bisect_right(valid_ends, start)
    return {"usable": actual, "expected": expected, "ratio": min(1., actual/expected) if expected else None}


def _compare(trades, control, valid_ends, start, end):
    result = _period(trades,start,end)
    baseline = _period(control,start,end)
    result.update(control_pnl=baseline["pnl"], excess=round(result["pnl"]-baseline["pnl"],6),
                  coverage=_coverage(valid_ends,start,end))
    return result


def _passes(p, unrestricted):
    return p["pnl"] > 0 and p["stressed"] > 0 and p["excess"] > 0 and (unrestricted or p["drawdown"] <= 10)


def _grades(rows, accounts, order_intervals, valid_ends, start, now, *, unrestricted, previous_stops):
    """Family winners use only information available on their original review day."""
    grades = {}
    passed = defaultdict(list)
    control = rows[CONTROL_KEY]
    for policy in POLICIES:
        if policy.is_control:
            continue
        key = policy.key
        checkpoint = _review_at(rows[key], control, (*order_intervals[key], *order_intervals[CONTROL_KEY]), start)
        grade = grades[key] = {"code":"collecting", "label":"样本积累中", "screen":None, "validation":None}
        if checkpoint is None or checkpoint > now:
            continue
        p = _compare(rows[key],control,valid_ends,start,checkpoint)
        grade["screen"] = p
        stopped = previous_stops.get(key)
        if (p["coverage"]["ratio"] or 0) < MIN_COVERAGE:
            grade.update(code="coverage", label="初筛行情不足")
        elif (not unrestricted and stopped is not None and stopped <= checkpoint) or not _passes(p, unrestricted):
            grade.update(code="rejected", label="初筛未通过")
        else:
            passed[policy.family].append((checkpoint, -p["stressed"], key))

    for candidates in passed.values():
        candidates.sort()
        for _, _, key in candidates[1:]:
            grades[key].update(code="reserve", label="同组备选")
        boundary, _, key = candidates[0]
        grade = grades[key]
        grade.update(code="validation", label="入围，等待复验")
        checkpoint = _review_at(rows[key], control, (*order_intervals[key], *order_intervals[CONTROL_KEY]), boundary)
        immature = checkpoint is None or checkpoint > now
        validation = _compare(rows[key],control,valid_ends,boundary,now if immature else checkpoint)
        grade["validation"] = validation
        if immature:
            continue
        if (validation["coverage"]["ratio"] or 0) < MIN_COVERAGE:
            grade.update(code="coverage", label="复验行情不足")
        elif (_passes(validation, unrestricted) and validation["first_half"] > 0 and validation["second_half"] > 0):
            grade.update(code="focus", label="复验通过，重点观察")
        else:
            grade.update(code="rejected", label="复验未通过")

    if not unrestricted:
        for key, g in grades.items():
            stopped = previous_stops.get(key)
            if stopped is not None and stopped <= now:
                g.update(code="stopped", label="旧规则已停开仓")
            elif g["code"] == "focus" and accounts[key]["max_drawdown"] > 10:
                g.update(code="drawdown", label="旧规则回撤超限")
    return grades


def read_league(path, now):
    uri = Path(path).resolve().as_uri()+"?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=.8)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        run = conn.execute("SELECT * FROM league_run WHERE id=1").fetchone()
        if run is None:
            return {"enabled":True, "available":False, "reason":"等待试验初始化"}
        start = run["starts_at"]
        accounts = {r["strategy_id"]:dict(r) for r in conn.execute("SELECT * FROM league_accounts")}
        if set(accounts) != {p.key for p in POLICIES} or run["catalog_hash"] != catalog_hash():
            return {"enabled":True, "available":False, "reason":"账户与当前策略清单不匹配"}
        # Old snapshots remain readable. The writer records the activation in the same
        # transaction that releases existing wallets, so the report never enables it early.
        has_policy = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='league_admission_policy'").fetchone()
        policy = conn.execute("SELECT unrestricted_at FROM league_admission_policy WHERE id=1").fetchone() if has_policy else None
        unrestricted_at = policy["unrestricted_at"] if policy else None
        unrestricted = unrestricted_at is not None and (unrestricted_at == start or now >= unrestricted_at)
        screen_start = max(start, math.ceil(unrestricted_at / 300) * 300) if unrestricted else start
        releases = {r["strategy_id"]:dict(r) for r in conn.execute(
            "SELECT strategy_id,stopped_at,released_at FROM league_risk_releases")} if policy else {}
        previous_stops = {k:releases[k]["stopped_at"] if k in releases else a["stopped_at"] for k,a in accounts.items()}
        orders = {r["strategy_id"]:r for r in conn.execute("""SELECT strategy_id,count(*) AS submitted,
            sum(filled_shares>0) AS filled,sum(status='PENDING') AS pending FROM league_orders
            WHERE action='BUY' GROUP BY strategy_id""")}
        # Only orders crossing a five-minute boundary can keep a review open by themselves.
        order_intervals = defaultdict(list)
        for order in conn.execute("""SELECT strategy_id,submitted_at,finished_at FROM league_orders
                WHERE submitted_at>=? AND submitted_at<=? AND (finished_at IS NULL
                OR CAST(submitted_at/300 AS INTEGER) != CAST(finished_at/300 AS INTEGER))""", (start,now)):
            order_intervals[order["strategy_id"]].append((order["submitted_at"], order["finished_at"]))
        rows = defaultdict(list)
        for t in conn.execute("""SELECT p.strategy_id,p.round_id,p.side,coalesce(o.submitted_at,p.opened_at) AS opened_at,
                    p.closed_at,p.shares,p.bought_shares,p.cost,p.pnl,p.fees FROM league_positions p
                    LEFT JOIN league_orders o ON o.strategy_id=p.strategy_id AND o.round_id=p.round_id
                    AND o.action='BUY' AND o.filled_shares>0 WHERE p.opened_at>=? AND p.opened_at<=?""", (start,now)):
            rows[t["strategy_id"]].append(t)
        markets = conn.execute("""SELECT ends_at,last_valid,gap FROM league_rounds
                    WHERE starts_at>=? AND starts_at<=? ORDER BY ends_at""", (start,now)).fetchall()
    valid_ends = [r["ends_at"] for r in markets if r["last_valid"] is not None and not r["gap"]]
    coverage = _coverage(valid_ends,start,now)
    grades = _grades(rows, accounts, order_intervals, valid_ends, screen_start, now,
                     unrestricted=unrestricted, previous_stops=previous_stops)
    ctrl = rows[CONTROL_KEY]
    ctrl_closed = {r["round_id"] for r in ctrl if r["closed_at"] is not None and r["closed_at"]<=now}
    control_pnl = _period(ctrl,start,now)["pnl"]
    cohort_control_pnl = _period(ctrl,screen_start,now)["pnl"] if screen_start > start else control_pnl
    arms=[]
    for p in POLICIES:
        key=p.key
        a=accounts[key]
        positions=rows[key]
        view=_period(positions,start,now)
        cohort = _period(positions,screen_start,now) if screen_start > start else view
        o=orders.get(key)
        current = [{"round_id":r["round_id"], "side":r["side"], "shares":r["shares"]} for r in positions
                   if r["closed_at"] is None or r["closed_at"] > now]
        arms.append({
            "key":key, "name":p.name, "family":p.family, "entry":p.description, "exit":p.exit_rule,
            "weakness":p.weakness, "is_control":p.is_control, "cash":round(a["cash"],6),
            "equity":round(a["equity"],6), "realized":round(a["realized"],6), "max_drawdown":round(a["max_drawdown"],6),
            "unpriced_shares":a["unknown_shares"], "stopped_at":None if unrestricted else previous_stops[key], "last_reason":a["last_reason"],
            "past_stop":releases.get(key) if unrestricted else None,
            "position":current[0] if current else None, "positions":current,
            "submitted":o["submitted"] if o else 0, "fills":o["filled"] if o else 0, "pending":o["pending"] if o else 0,
            "closed":view["rounds"], "closed_pnl":view["pnl"], "stressed":view["stressed"],
            "excess":round(view["pnl"]-control_pnl,6), "fees":round(sum(r["fees"] for r in positions),6),
            "screen_to_date":{"rounds":cohort["rounds"], "pnl":cohort["pnl"],
                              "excess":round(cohort["pnl"]-cohort_control_pnl,6)},
            "shared_closed":sum(r["round_id"] in ctrl_closed for r in positions if r["closed_at"] is not None and r["closed_at"]<=now),
            "grade":grades.get(key,{"code":"control","label":"同期对照","screen":None,"validation":None}),
        })
    return {"enabled":True,"available":True,"as_of":now,"start_at":start,"last_seen":run["last_seen"],
            "unrestricted":unrestricted,"unrestricted_at":unrestricted_at,"screen_start":screen_start,
            "last_valid":run["last_valid"],"catalog_hash":run["catalog_hash"],"observed":len(markets),
            "coverage":coverage,"candidate_count":len(grades),"family_count":len({p.family for p in POLICIES if not p.is_control}),
            "state_counts":dict(Counter(g["code"] for g in grades.values())),"arms":arms}


class StrategyLeagueReport:
    def __init__(self, bot):
        self.bot=bot
        self.cached=None
        self.cached_at=0.
        self.lock=threading.Lock()

    def snapshot(self):
        if not getattr(self.bot.settings,"strategy_league_enabled",False):
            return {"enabled":False,"available":False,"reason":"此实例未启用候选策略试验"}
        with self.lock:
            now=time.time()
            if self.cached is not None and now-self.cached_at<15:
                return self.cached
            path=Path(self.bot.store.db_path).resolve().parent/"strategy-league.sqlite3"
            try:
                result=read_league(path,now)
            except sqlite3.Error:
                result={"enabled":True,"available":False,"reason":"暂时读不到试验账本，稍后重试"}
            self.cached,self.cached_at=result,now
            return result
