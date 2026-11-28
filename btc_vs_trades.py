#!/usr/bin/env python3
"""
Lines your CLOSED futures trades up against BTC's real price action, hour by hour.

Usage:
  python3 btc_vs_trades.py                       (db = SQLITE_DB_PATH or ./apextick.db, PAPER trades, 1h candles)
  python3 btc_vs_trades.py apextick.db PAPER --tf 15m
  python3 btc_vs_trades.py apextick.db PAPER --exchange kraken     (if Binance is blocked from where you run it)

It reads the database read-only and fetches public BTC candles (no API key needed).
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}
EXCHANGES = {   # name -> BTC symbol
    "binance": "BTC/USDT", "bybit": "BTC/USDT", "okx": "BTC/USDT", "kraken": "BTC/USD", "coinbase": "BTC/USD", "bitstamp": "BTC/USD",
}

def parse_ts(value):
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def load_trades(db_path, mode):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM futures_trades WHERE status = 'CLOSED' AND trade_mode = ? ORDER BY closed_at, id", (mode,))]
    con.close()
    out = []
    for r in rows:
        ts = parse_ts(r.get("closed_at"))
        if ts:
            out.append({"t": ts, "pnl": float(r.get("pnl_usdt") or 0.0), "pair": r.get("pair"), "direction": r.get("direction"), "close_type": str(r.get("close_type") or "")})
    return out

def make_exchange(name):
    import ccxt
    return getattr(ccxt, name)({"enableRateLimit": True, "timeout": 15000})

def fetch_candles(names, tf, since_ms, until_ms, factory=make_exchange):
    """Tries each exchange in turn until one returns candles. Returns (exchange_name, candles)."""
    errors = []
    for name in names:
        symbol = EXCHANGES.get(name, "BTC/USDT")
        try:
            ex = factory(name)
            candles, cursor = [], since_ms
            while cursor < until_ms:
                batch = ex.fetch_ohlcv(symbol, tf, since=cursor, limit=1000)
                if not batch:
                    break
                candles += [c for c in batch if c[0] >= since_ms]
                nxt = batch[-1][0] + TF_MS[tf]
                if nxt <= cursor:
                    break
                cursor = nxt
            if candles:
                seen = {}
                for c in candles:
                    seen[c[0]] = c
                return name, [seen[k] for k in sorted(seen)]
            errors.append(f"{name}: no candles")
        except Exception as e:
            errors.append(f"{name}: {str(e)[:90]}")
    raise RuntimeError("could not fetch BTC candles -> " + " | ".join(errors))

def streak_window(trades):
    """Longest losing run: (start, end, length, net)."""
    best, cur = None, []
    for t in trades + [None]:
        if t is not None and t["pnl"] <= 0:
            cur.append(t)
            continue
        if cur and (best is None or len(cur) > best[2]):
            best = (cur[0]["t"], cur[-1]["t"], len(cur), sum(x["pnl"] for x in cur))
        cur = []
    return best

def analyze(trades, candles, tf):
    step = TF_MS[tf]
    rows = {}
    for c in candles:
        rows[c[0]] = {"open": c[1], "high": c[2], "low": c[3], "close": c[4], "n": 0, "w": 0, "l": 0, "net": 0.0, "stops": 0}
    for t in trades:
        key = int(t["t"].timestamp() * 1000) // step * step
        r = rows.get(key)
        if r is None:
            continue
        r["n"] += 1
        r["w" if t["pnl"] > 0 else "l"] += 1
        r["net"] += t["pnl"]
        r["stops"] += 1 if t["close_type"].startswith("STRUCTURAL SL") else 0
    for r in rows.values():
        r["chg"] = (r["close"] - r["open"]) / r["open"] * 100 if r["open"] else 0.0
        r["range"] = (r["high"] - r["low"]) / r["open"] * 100 if r["open"] else 0.0
    return rows

def summarize(rows, flat=0.3):
    groups = {"BTC up (>= +%.1f%%)" % flat: [], "BTC down (<= -%.1f%%)" % flat: [], "BTC flat": []}
    for r in rows.values():
        if r["n"] == 0:
            continue
        key = "BTC up (>= +%.1f%%)" % flat if r["chg"] >= flat else ("BTC down (<= -%.1f%%)" % flat if r["chg"] <= -flat else "BTC flat")
        groups[key].append(r)
    out = {}
    for k, v in groups.items():
        n = sum(r["n"] for r in v)
        out[k] = {"hours": len(v), "trades": n, "wins": sum(r["w"] for r in v), "net": sum(r["net"] for r in v)}
    return out

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default=os.getenv("SQLITE_DB_PATH", "apextick.db"))
    ap.add_argument("mode", nargs="?", default="PAPER")
    ap.add_argument("--tf", default="1h", choices=sorted(TF_MS))
    ap.add_argument("--exchange", default=None, help="binance | bybit | okx | kraken | coinbase | bitstamp (default: tries them in that order)")
    args = ap.parse_args(argv)

    trades = load_trades(args.db, args.mode.upper())
    if not trades:
        print("No closed futures trades found.")
        return 0
    step = TF_MS[args.tf]
    first, last = trades[0]["t"], trades[-1]["t"]
    since = int((first - timedelta(hours=2)).timestamp() * 1000) // step * step
    until = int((last + timedelta(hours=1)).timestamp() * 1000)
    names = [args.exchange] if args.exchange else ["binance", "bybit", "okx", "kraken", "coinbase"]
    name, candles = fetch_candles(names, args.tf, since, until, factory=make_exchange)
    rows = analyze(trades, candles, args.tf)

    win = streak_window(trades)
    print(f"{len(trades)} closed futures trades ({args.mode.upper()}) | BTC {args.tf} candles from {name} | all times UTC\n")
    print(f"{'hour':<12}{'BTC open':>10}{'close':>10}{'chg%':>7}{'range%':>8}{'trades':>8}{'W/L':>7}{'net $':>8}")
    for key in sorted(rows):
        r = rows[key]
        stamp = datetime.fromtimestamp(key / 1000, tz=timezone.utc)
        mark = " *" if win and win[0].timestamp() * 1000 // step * step <= key <= win[1].timestamp() * 1000 // step * step else ""
        flag = "  <-- big move" if abs(r["chg"]) >= 1.5 or r["range"] >= 3 else ""
        print(f"{stamp:%m-%d %H:%M}  {r['open']:>10.0f}{r['close']:>10.0f}{r['chg']:>+7.2f}{r['range']:>8.2f}{r['n']:>8}{str(r['w']) + '/' + str(r['l']):>7}{r['net']:>+8.2f}{mark}{flag}")
    print("\n(* = inside your longest losing streak)")

    if win:
        in_win = [c for c in candles if win[0].timestamp() * 1000 // step * step <= c[0] <= win[1].timestamp() * 1000 // step * step]
        if in_win:
            o, c_, hi, lo = in_win[0][1], in_win[-1][4], max(x[2] for x in in_win), min(x[3] for x in in_win)
            print(f"\nLongest losing streak: {win[2]} trades, {win[3]:+.2f} USD, {win[0]:%m-%d %H:%M} -> {win[1]:%m-%d %H:%M}")
            print(f"BTC over that window: open {o:.0f}, close {c_:.0f} ({(c_ - o) / o * 100:+.2f}%), high {hi:.0f}, low {lo:.0f} (range {(hi - lo) / o * 100:.1f}%)")

    print("\nBy BTC hour type (only hours where you closed trades):")
    for k, v in summarize(rows).items():
        per = v["net"] / v["trades"] if v["trades"] else 0.0
        print(f"  {k:<22} hours {v['hours']:>3} | trades {v['trades']:>3} | wins {v['wins']:>3} | net {v['net']:>+8.2f} | per trade {per:>+6.2f}")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print("ERROR:", e)
        print("Tip: run it from a machine/region where an exchange is reachable, or pass --exchange kraken")
        sys.exit(1)
