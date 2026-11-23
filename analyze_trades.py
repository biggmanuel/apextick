#!/usr/bin/env python3
"""
Read-only report on your CLOSED trades: where the edge is and where it leaks.
Usage:   python3 analyze_trades.py                 (uses SQLITE_DB_PATH or ./apextick.db)
         python3 analyze_trades.py /path/to/apextick.db [PAPER|LIVE]
It never writes to the database.
"""
import collections
import os
import sqlite3
import sys
from datetime import datetime

path = sys.argv[1] if len(sys.argv) > 1 else os.getenv("SQLITE_DB_PATH", "apextick.db")
mode = (sys.argv[2] if len(sys.argv) > 2 else "PAPER").upper()

con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
fut = [dict(r) for r in con.execute("SELECT * FROM futures_trades WHERE status = 'CLOSED' AND trade_mode = ? ORDER BY closed_at", (mode,))]
spot = [dict(r) for r in con.execute("SELECT * FROM spot_trades WHERE status = 'CLOSED' AND trade_mode = ? ORDER BY closed_at", (mode,))]

def ts(s):
    try:
        return datetime.fromisoformat(str(s))
    except Exception:
        return None

for t in fut:
    entry, edge = float(t.get("entry_price") or 0), float(t.get("ob_edge_price") or 0)
    t["stop_pct"] = abs(entry - edge) / entry * 100 if entry > 0 and edge > 0 else None
    a, b = ts(t.get("opened_at")), ts(t.get("closed_at"))
    t["hold_min"] = (b - a).total_seconds() / 60 if a and b else None
    t["hour"] = b.hour if b else None
    t["pnl"] = float(t.get("pnl_usdt") or 0)

def table(title, groups, min_n=1):
    print(f"\n{title}")
    print(f"  {'group':<22}{'n':>4}{'win%':>7}{'net $':>9}{'avg $':>8}{'avg win':>9}{'avg loss':>9}")
    for name, items in groups:
        if len(items) < min_n:
            continue
        pn = [x["pnl"] for x in items]
        w = [p for p in pn if p > 0]
        l = [p for p in pn if p <= 0]
        print(f"  {str(name):<22}{len(pn):>4}{100 * len(w) / len(pn):>6.0f}%{sum(pn):>9.2f}{sum(pn) / len(pn):>8.2f}"
              f"{(sum(w) / len(w) if w else 0):>9.2f}{(sum(l) / len(l) if l else 0):>9.2f}")

def bucket(items, key, edges, labels):
    out = collections.OrderedDict((lab, []) for lab in labels)
    for it in items:
        v = it.get(key)
        if v is None:
            continue
        for i, e in enumerate(edges):
            if v < e:
                out[labels[i]].append(it)
                break
        else:
            out[labels[-1]].append(it)
    return list(out.items())

print(f"=== FUTURES ({mode}) - {len(fut)} closed trades  [{path}] ===")
if fut:
    pn = [t["pnl"] for t in fut]
    wins = [p for p in pn if p > 0]
    losses = [p for p in pn if p <= 0]
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = sum(losses) / len(losses) if losses else 0
    be = (-avg_l / (avg_w - avg_l) * 100) if (avg_w - avg_l) else 0
    print(f"net ${sum(pn):.2f} | win rate {100 * len(wins) / len(pn):.1f}% | avg win ${avg_w:.2f} | avg loss ${avg_l:.2f}")
    print(f"break-even win rate at that payoff: {be:.1f}%   expectancy per trade: ${sum(pn) / len(pn):.2f}")
    streak = best = 0
    for p in pn:
        streak = streak + 1 if p <= 0 else 0
        best = max(best, streak)
    print(f"longest losing streak: {best}   TP1 reached on {sum(1 for t in fut if t.get('tp1_hit'))} of {len(fut)} trades")

    stops = [t for t in fut if str(t.get("close_type", "")).startswith("STRUCTURAL SL")]
    clustered, group = 0, []
    for t in stops:
        if group and (ts(t["closed_at"]) - ts(group[-1]["closed_at"])).total_seconds() <= 90:
            group.append(t)
        else:
            clustered += len(group) if len(group) >= 2 else 0
            group = [t]
    clustered += len(group) if len(group) >= 2 else 0
    print(f"stop-outs: {len(stops)}; {clustered} of them hit within 90s of another one (correlated positions = one bet)")

    d = collections.defaultdict(list)
    for t in fut: d[t.get("direction")].append(t)
    table("BY DIRECTION", sorted(d.items()))
    d = collections.defaultdict(list)
    for t in fut: d[str(t.get("close_type", "")).split(" (")[0]].append(t)
    table("BY EXIT TYPE", sorted(d.items(), key=lambda kv: sum(x["pnl"] for x in kv[1])))
    table("BY STOP DISTANCE (% of entry)", bucket(fut, "stop_pct", [0.5, 0.75, 1.0, 1.5, 2.01], ["<0.5%", "0.5-0.75%", "0.75-1.0%", "1.0-1.5%", "1.5-2.0%", ">2.0%"]))
    table("BY HOLD TIME", bucket(fut, "hold_min", [5, 15, 60, 240], ["<5 min", "5-15 min", "15-60 min", "1-4 h", ">4 h"]))
    table("BY UTC HOUR (4h blocks)", bucket(fut, "hour", [4, 8, 12, 16, 20], ["00-04", "04-08", "08-12", "12-16", "16-20", "20-24"]))
    d = collections.defaultdict(list)
    for t in fut: d[t["pair"]].append(t)
    table("WORST PAIRS (n>=3)", sorted(d.items(), key=lambda kv: sum(x["pnl"] for x in kv[1]))[:8], min_n=3)
    table("BEST PAIRS (n>=3)", sorted(d.items(), key=lambda kv: -sum(x["pnl"] for x in kv[1]))[:5], min_n=3)

print(f"\n=== SPOT ({mode}) - {len(spot)} closed trades ===")
if spot:
    sp = [float(t.get("profit_usdt") or 0) for t in spot]
    print(f"net ${sum(sp):.2f} | win rate {100 * sum(1 for p in sp if p > 0) / len(sp):.0f}% | avg ${sum(sp) / len(sp):.2f} | worst ${min(sp):.2f}")
print("\n(read-only report; nothing was changed)")