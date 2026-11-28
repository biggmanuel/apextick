import os, sys, tempfile
from datetime import datetime, timezone, timedelta

os.environ["SQLITE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "replay.db")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, os.getcwd())

import database as D
D.init_db()
import backtest_replay as BR

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond: fails.append(name)

# ---- 1. Futures replay: tight-stop filter + probation drawdown compression
t0 = datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc)
def mk_f(i, pnl, entry=100.0, edge=99.0, margin=3.33):
    return {
        "id": i, "pair": f"C{i}/USDT", "entry_price": entry, "ob_edge_price": edge,
        "margin_usdt": margin, "leverage": 15, "pnl_usdt": pnl,
        "closed_at": (t0 + timedelta(minutes=10 * i)).isoformat(),
    }

trades = [
    mk_f(1, -0.50, entry=100.0, edge=99.7),   # 0.3% stop -> skipped by 0.5% min stop
    mk_f(2, -0.60),                           # L1
    mk_f(3, -0.60),                           # L2
    mk_f(4, -0.60),                           # L3 -> enters PROBATION
    mk_f(5, -0.60),                           # L4 in PROBATION (25% risk)
    mk_f(6, -0.60),                           # L5 in PROBATION (25% risk)
    mk_f(7, +1.20),                           # W1 in PROBATION -> exits PROBATION
    mk_f(8, +1.20),                           # W2 back at NORMAL
]
rep = BR.replay_futures_trades(trades, min_stop_pct=0.5, probation_loss_streak=3, probation_risk_multiplier=0.25, probation_wins_to_recover=1)
check("tight stop (<0.5%) is filtered out", rep["skipped_tight_stops"] == 1 and rep["simulated"]["trades"] == 7, str(rep))
check("probation applies to L4, L5, and recovery win W1 (3 trades)", rep["probation_trades"] == 3, str(rep["probation_trades"]))
check("simulated max drawdown is significantly lower than actual", rep["simulated"]["max_drawdown"] < rep["actual"]["max_drawdown"], f"{rep['simulated']['max_drawdown']} < {rep['actual']['max_drawdown']}")
check("simulated net PnL beats actual on heavy losing streak", rep["simulated"]["net_pnl"] > rep["actual"]["net_pnl"], f"{rep['simulated']['net_pnl']} > {rep['actual']['net_pnl']}")

# ---- 2. Spot summary isolates excluded tokenized stocks
spot_trades = [
    {"pair": "SOL/USDT", "profit_usdt": 0.60},
    {"pair": "LINK/USDT", "profit_usdt": 0.50},
    {"pair": "COINB/USDT", "profit_usdt": -2.10},
    {"pair": "NBISB/USDT", "profit_usdt": -2.11},
]
sp = BR.summarize_spot_trades(spot_trades)
check("spot summary flags COINB & NBISB as excluded equity tokens", sp["excluded_stock_trades"] == 2 and abs(sp["excluded_stock_pnl"] - (-4.21)) < 1e-6, str(sp))
check("spot net without excluded tokens equals +$1.10", abs(sp["net_pnl_without_excluded"] - 1.10) < 1e-6, str(sp["net_pnl_without_excluded"]))

# ---- 3. Spot MAE stop sweep math
mae_trades = [
    {"pair": "W1/USDT", "profit_usdt": 0.60, "notional_usdt": 18.0, "mae_pct": -2.0},   # survives all stops >= 3%
    {"pair": "W2/USDT", "profit_usdt": 0.60, "notional_usdt": 18.0, "mae_pct": -5.5},   # killed by 5%, survives 6%+
    {"pair": "L1/USDT", "profit_usdt": -2.20, "notional_usdt": 18.0, "mae_pct": -12.5}, # improved by tighter stops
]
sweep = {r["stop_pct"]: r for r in BR.simulate_spot_stop_sweep(mae_trades, stop_levels=(5.0, 6.0, 12.0))}
check("5% stop kills W2 winner but saves L1", sweep[5.0]["wins_killed"] == 1 and sweep[5.0]["losers_saved"] == 1, str(sweep[5.0]))
check("6% stop preserves both winners and improves L1", sweep[6.0]["wins_killed"] == 0 and sweep[6.0]["losers_saved"] == 1 and sweep[6.0]["simulated_net_pnl"] > sweep[12.0]["simulated_net_pnl"], str(sweep[6.0]))

# ---- 4. Read-only DB loader works on real SQLite schema
db_path = os.environ["SQLITE_DB_PATH"]
D.execute_query(
    """INSERT INTO futures_trades (account_id,pair,trade_mode,direction,status,entry_price,margin_usdt,remaining_margin_usdt,notional_usdt,leverage,pnl_usdt,opened_at,closed_at)
       VALUES ('a','SOL/USDT','PAPER','LONG','CLOSED',100,2,0,30,15,0.8,?,?)""",
    (t0.isoformat(), (t0 + timedelta(minutes=5)).isoformat()), commit=True
)
f_rows, s_rows = BR.load_closed_trades(db_path, "PAPER")
check("read-only DB loader reads closed trades cleanly", len(f_rows) == 1 and len(s_rows) == 0)

print("\nFAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)
