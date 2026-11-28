#!/usr/bin/env python3
"""
ApexTick Offline Replay & Spot MAE Research Tool.

Opens any ApexTick SQLite database in READ-ONLY mode and evaluates:
  1. Futures replay: compares recorded performance against the upgraded rules
     (min_stop_pct, fee-aware sizing, and Probation circuit breaker).
  2. Spot summary: win rate vs break-even and tokenized-stock impact.
  3. Optional (--spot-mae): fetches Binance 15m candles over each closed Spot
     trade's holding window to compute Maximum Adverse Excursion (MAE) and
     sweep candidate Spot stop-loss levels before changing max_drawdown_pct.
"""

import argparse
import os
import sqlite3
from datetime import datetime, timezone

from config import FUTURES_CONFIG, SPOT_CONFIG, MARKET_SCANNER_CONFIG


def _iso_to_ms(iso_str: str) -> int:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 4)


def _worst_loss_streak(pnls: list[float]) -> tuple[int, float]:
    cur_len = 0
    cur_loss = 0.0
    worst_len = 0
    worst_loss = 0.0
    for p in pnls:
        if p <= 0:
            cur_len += 1
            cur_loss += p
            if cur_loss < worst_loss:
                worst_loss = cur_loss
                worst_len = cur_len
        else:
            cur_len = 0
            cur_loss = 0.0
    return worst_len, round(worst_loss, 4)


def replay_futures_trades(
    trades: list[dict],
    min_stop_pct: float = None,
    probation_loss_streak: int = None,
    probation_risk_multiplier: float = None,
    probation_wins_to_recover: int = None,
    risk_per_trade_usdt: float = None,
    fee_pct: float = None,
    rt_slippage_pct: float = None,
    leverage: int = None,
) -> dict:
    """
    Chronologically replays closed Futures trades under the upgraded sizing and
    probation rules without modifying any database.
    """
    min_stop_pct = FUTURES_CONFIG.get("min_stop_pct", 0.5) if min_stop_pct is None else float(min_stop_pct)
    trigger = int(FUTURES_CONFIG.get("probation_loss_streak", 3) if probation_loss_streak is None else probation_loss_streak)
    prob_mult = float(FUTURES_CONFIG.get("probation_risk_multiplier", 0.25) if probation_risk_multiplier is None else probation_risk_multiplier)
    wins_needed = max(1, int(FUTURES_CONFIG.get("probation_wins_to_recover", 1) if probation_wins_to_recover is None else probation_wins_to_recover))
    risk_usdt = float(FUTURES_CONFIG.get("risk_per_trade_usdt", 0.50) if risk_per_trade_usdt is None else risk_per_trade_usdt)
    fee_pct = float(FUTURES_CONFIG.get("fee_pct", 0.05) if fee_pct is None else fee_pct)
    rt_slip_pct = float(FUTURES_CONFIG.get("live_slippage_buffer_pct", 0.04) if rt_slippage_pct is None else rt_slippage_pct)
    default_lev = int(FUTURES_CONFIG.get("leverage", 15) if leverage is None else leverage)
    min_margin = float(FUTURES_CONFIG.get("min_margin_usdt", 0.4))

    ordered = sorted(trades, key=lambda t: (str(t.get("closed_at") or ""), int(t.get("id") or 0)))

    actual_pnls = [float(t.get("pnl_usdt") or 0.0) for t in ordered]
    sim_pnls = []
    skipped_tight = 0
    probation_trades = 0

    state = "NORMAL"
    loss_streak = 0
    recovery_wins = 0

    cost_frac = (2.0 * fee_pct + rt_slip_pct) / 100.0

    for t in ordered:
        entry = float(t.get("entry_price") or 0.0)
        edge = float(t.get("ob_edge_price") or 0.0)
        orig_margin = float(t.get("margin_usdt") or 0.0)
        lev = int(t.get("leverage") or default_lev)
        orig_pnl = float(t.get("pnl_usdt") or 0.0)

        stop_frac = abs(entry - edge) / entry if (entry > 0 and edge > 0) else 0.0
        if stop_frac > 0 and (stop_frac * 100.0) < min_stop_pct:
            skipped_tight += 1
            continue

        mult = prob_mult if state == "PROBATION" else 1.0
        if state == "PROBATION":
            probation_trades += 1

        if stop_frac > 0 and orig_margin > 0 and lev > 0:
            target_margin = max(min_margin, (risk_usdt * mult) / (lev * (stop_frac + cost_frac)))
            scale = min(target_margin / orig_margin, 1.5)
        else:
            scale = mult

        sim_pnl = orig_pnl * scale
        sim_pnls.append(sim_pnl)

        if sim_pnl > 0:
            loss_streak = 0
            if state == "PROBATION":
                recovery_wins += 1
                if recovery_wins >= wins_needed:
                    state = "NORMAL"
                    recovery_wins = 0
        else:
            loss_streak += 1
            recovery_wins = 0
            if trigger > 0 and loss_streak >= trigger:
                state = "PROBATION"

    def _stats(pnls: list[float]) -> dict:
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        worst_len, worst_loss = _worst_loss_streak(pnls)
        return {
            "trades": n,
            "wins": wins,
            "win_rate_pct": round((wins / n * 100.0), 2) if n else 0.0,
            "net_pnl": round(sum(pnls), 2),
            "max_drawdown": round(_max_drawdown(pnls), 2),
            "worst_streak_len": worst_len,
            "worst_streak_pnl": round(worst_loss, 2),
        }

    return {
        "actual": _stats(actual_pnls),
        "simulated": _stats(sim_pnls),
        "skipped_tight_stops": skipped_tight,
        "probation_trades": probation_trades,
    }


def summarize_spot_trades(trades: list[dict], excluded_bases=None) -> dict:
    """Summarizes closed Spot trades and isolates PnL from newly excluded tokenized stocks."""
    excl = set(excluded_bases if excluded_bases is not None else MARKET_SCANNER_CONFIG.get("excluded_bases", []))
    n = len(trades)
    wins = [float(t.get("profit_usdt") or 0.0) for t in trades if float(t.get("profit_usdt") or 0.0) > 0]
    losses = [float(t.get("profit_usdt") or 0.0) for t in trades if float(t.get("profit_usdt") or 0.0) <= 0]
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (abs(sum(losses)) / len(losses)) if losses else 0.0
    be_wr = (avg_loss / (avg_win + avg_loss) * 100.0) if (avg_win + avg_loss) > 0 else 0.0

    excl_trades = [t for t in trades if str(t.get("pair") or "").split("/")[0] in excl]
    excl_pnl = sum(float(t.get("profit_usdt") or 0.0) for t in excl_trades)
    total_pnl = sum(float(t.get("profit_usdt") or 0.0) for t in trades)

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round((len(wins) / n * 100.0), 2) if n else 0.0,
        "breakeven_win_rate_pct": round(be_wr, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(-avg_loss, 2),
        "net_pnl": round(total_pnl, 2),
        "excluded_stock_trades": len(excl_trades),
        "excluded_stock_pnl": round(excl_pnl, 2),
        "net_pnl_without_excluded": round(total_pnl - excl_pnl, 2),
    }


def simulate_spot_stop_sweep(
    trades_with_mae: list[dict],
    stop_levels=(3.0, 5.0, 6.0, 8.0, 10.0, 12.0),
    fee_pct: float = None,
) -> list[dict]:
    """
    Evaluates candidate Spot drawdown stops using each closed trade's intra-trade MAE (%).
    `mae_pct` is negative (e.g. -4.5 means price dipped 4.5% below entry before closing).
    """
    fee_pct = float(SPOT_CONFIG.get("fee_pct", 0.1) if fee_pct is None else fee_pct)
    rt_fee_frac = (2.0 * fee_pct) / 100.0
    results = []

    for stop_pct in stop_levels:
        sim_net = 0.0
        wins = 0
        wins_killed = 0
        losers_saved = 0
        for t in trades_with_mae:
            actual_pnl = float(t.get("profit_usdt") or 0.0)
            notional = float(t.get("notional_usdt") or 18.0)
            mae_pct = float(t.get("mae_pct") or 0.0)
            if mae_pct <= -stop_pct:
                sim_pnl = -notional * ((stop_pct / 100.0) + rt_fee_frac)
                if actual_pnl > 0:
                    wins_killed += 1
                elif sim_pnl > actual_pnl:
                    losers_saved += 1
            else:
                sim_pnl = actual_pnl
            sim_net += sim_pnl
            if sim_pnl > 0:
                wins += 1

        n = len(trades_with_mae)
        results.append({
            "stop_pct": float(stop_pct),
            "trades": n,
            "win_rate_pct": round((wins / n * 100.0), 2) if n else 0.0,
            "wins_killed": wins_killed,
            "losers_saved": losers_saved,
            "simulated_net_pnl": round(sim_net, 2),
        })
    return results


def fetch_spot_mae_from_binance(spot_trades: list[dict]) -> list[dict]:
    """Fetches 15m candles from Binance via ccxt to compute exact MAE for each closed Spot trade."""
    import ccxt

    ex = ccxt.binance({"enableRateLimit": True, "timeout": 15000})
    enriched = []
    for t in spot_trades:
        entry = float(t.get("entry_price") or 0.0)
        opened_at = t.get("opened_at")
        closed_at = t.get("closed_at")
        if entry <= 0 or not opened_at or not closed_at:
            continue
        start_ms = _iso_to_ms(opened_at)
        end_ms = _iso_to_ms(closed_at)
        min_low = entry
        cursor = start_ms
        try:
            while cursor <= end_ms:
                candles = ex.fetch_ohlcv(t["pair"], timeframe="15m", since=cursor, limit=1000)
                if not candles:
                    break
                for c in candles:
                    if c[0] > end_ms:
                        break
                    if c[3] < min_low:
                        min_low = float(c[3])
                last_ts = candles[-1][0]
                if last_ts <= cursor or last_ts >= end_ms:
                    break
                cursor = last_ts + 15 * 60 * 1000
        except Exception as e:
            print(f"  [warn] Skipping MAE fetch for {t['pair']}: {e}")
            continue

        mae_pct = ((min_low - entry) / entry) * 100.0
        row = dict(t)
        row["mae_pct"] = round(mae_pct, 3)
        enriched.append(row)
    return enriched


def load_closed_trades(db_path: str, mode: str = "PAPER") -> tuple[list[dict], list[dict]]:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        fut = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM futures_trades WHERE status = 'CLOSED' AND trade_mode = ? ORDER BY closed_at ASC, id ASC",
                (mode,),
            ).fetchall()
        ]
        spot = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM spot_trades WHERE status = 'CLOSED' AND trade_mode = ? ORDER BY closed_at ASC, id ASC",
                (mode,),
            ).fetchall()
        ]
        return fut, spot
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="ApexTick Read-Only Backtest Replay & Spot MAE Tool")
    ap.add_argument("--db", default="apextick.db", help="Path to SQLite database (opened read-only)")
    ap.add_argument("--mode", default="PAPER", choices=["PAPER", "LIVE"], help="Trade mode to analyze")
    ap.add_argument("--spot-mae", action="store_true", help="Fetch Binance 15m candles to sweep Spot stop-loss levels")
    args = ap.parse_args()

    fut_trades, spot_trades = load_closed_trades(args.db, args.mode)

    print(f"=== ApexTick Replay ({args.mode}) on {args.db} ===")
    fut_rep = replay_futures_trades(fut_trades)
    a, s = fut_rep["actual"], fut_rep["simulated"]
    print("\n--- FUTURES (Actual vs Upgraded Rules: 0.5% min stop + fee-aware sizing + L3 Probation) ---")
    print(f"Actual    : {a['trades']} trades | WR {a['win_rate_pct']:.1f}% | Net ${a['net_pnl']:+.2f} | Max DD ${a['max_drawdown']:.2f} | Worst streak L{a['worst_streak_len']} (${a['worst_streak_pnl']:+.2f})")
    print(f"Simulated : {s['trades']} trades | WR {s['win_rate_pct']:.1f}% | Net ${s['net_pnl']:+.2f} | Max DD ${s['max_drawdown']:.2f} | Worst streak L{s['worst_streak_len']} (${s['worst_streak_pnl']:+.2f})")
    print(f"Filtered  : {fut_rep['skipped_tight_stops']} tight stops (<0.5%) skipped | {fut_rep['probation_trades']} trades sized at Probation risk")

    sp = summarize_spot_trades(spot_trades)
    print("\n--- SPOT SUMMARY ---")
    print(f"Closed    : {sp['trades']} trades ({sp['wins']}W / {sp['losses']}L) | WR {sp['win_rate_pct']:.1f}% (break-even {sp['breakeven_win_rate_pct']:.1f}%) | Avg W ${sp['avg_win']:+.2f} / Avg L ${sp['avg_loss']:+.2f}")
    print(f"Net PnL   : ${sp['net_pnl']:+.2f} | Excluded equity tokens ({sp['excluded_stock_trades']} trades): ${sp['excluded_stock_pnl']:+.2f} | Without excluded: ${sp['net_pnl_without_excluded']:+.2f}")

    if args.spot_mae:
        print("\n--- SPOT 15M CANDLE MAE STOP SWEEP ---")
        enriched = fetch_spot_mae_from_binance(spot_trades)
        if not enriched:
            print("No candles could be fetched (network/exchange unavailable).")
        else:
            for row in simulate_spot_stop_sweep(enriched):
                print(
                    f"Stop -{row['stop_pct']:4.1f}% | Net ${row['simulated_net_pnl']:+6.2f} | WR {row['win_rate_pct']:5.1f}% | "
                    f"winners killed: {row['wins_killed']:2d} | losers improved: {row['losers_saved']:2d}"
                )


if __name__ == "__main__":
    main()
