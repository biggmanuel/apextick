import os, sys, tempfile
from datetime import datetime, timezone
os.environ["SQLITE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "regime.db")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, os.getcwd())

import database as D
D.init_db()
import regime_manager as R
from btc_decline_signal import MARKET_STATE

q = D.execute_query
NOW = datetime.now(timezone.utc).isoformat()
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond: fails.append(name)

# ---- 1. classify_btc_market_state: TREND, CHOP, SHOCK
trend_klines = [{"c": 100.0 + i * 0.5} for i in range(25)]
state, er, pct4 = R.classify_btc_market_state(trend_klines)
check("steady directional trend -> TREND with ER=1.0", state == "TREND" and er == 1.0, f"{state} er={er} 4h={pct4}")

chop_closes = [100.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(25)]
state, er, pct4 = R.classify_btc_market_state([{"c": c} for c in chop_closes])
check("sawtooth back-and-forth -> CHOP with low ER", state == "CHOP" and er < 0.25, f"{state} er={er} 4h={pct4}")

shock_closes = [100.0] * 20 + [100.5, 101.2, 102.5, 103.5, 104.2]
state, er, pct4 = R.classify_btc_market_state([{"c": c} for c in shock_closes])
check("sharp +4.2% 4h impulse -> SHOCK", state == "SHOCK" and pct4 >= 3.5, f"{state} er={er} 4h={pct4}")

down_shock = [100.0] * 20 + [99.5, 98.0, 97.0, 96.2, 95.8]
state, er, pct4 = R.classify_btc_market_state([{"c": c} for c in down_shock])
check("sharp -4.2% 4h drop -> SHOCK", state == "SHOCK" and pct4 <= -3.5, f"{state} er={er} 4h={pct4}")

short_state, short_er, _ = R.classify_btc_market_state([{"c": 100.0}] * 3)
check("short history fails safe to TREND", short_state == "TREND" and short_er == 1.0)

# ---- 2. get_active_pairs always includes BTC/USDT even when missing from trading_pairs
q("INSERT INTO trading_pairs (pair,status,last_price,updated_at) VALUES ('SOL/USDT','ACTIVE',150,?)", (NOW,), commit=True)
mgr = R.RegimeManager()
pairs = mgr.get_active_pairs()
check("get_active_pairs always includes BTC/USDT", "BTC/USDT" in pairs and "SOL/USDT" in pairs, str(pairs))

# ---- 3. run_regime_cycle persists ATR to trading_pairs and updates BTC market_regimes + MARKET_STATE
def make_ohlcv(closes, spread=0.5):
    return [[1000 * i, c, c + spread, c - spread, c, 1000.0] for i, c in enumerate(closes)]

tight_chop = [100.0 + (0.4 if i % 2 == 0 else -0.4) for i in range(25)]
btc_ohlcv = make_ohlcv(tight_chop, spread=0.4)   # tight BBW (1.6% < 2.5%) + sawtooth (ER=0) -> RANGING + CHOP
sol_ohlcv = make_ohlcv([100.0 + i * 2.0 for i in range(25)], spread=2.5)  # wide BBW -> TRENDING + positive ATR
mgr.fetch_klines = lambda pair, timeframe='1h', limit=30: btc_ohlcv if pair == "BTC/USDT" else sol_ohlcv
mgr.run_regime_cycle()

btc_reg = q("SELECT * FROM market_regimes WHERE pair='BTC/USDT'", fetch_one=True)
check("BTC/USDT written to market_regimes as RANGING", btc_reg is not None and btc_reg["regime"] == "RANGING", str(btc_reg))
check("MARKET_STATE updated with CHOP and low ER", MARKET_STATE["btc_market_state"] == "CHOP" and MARKET_STATE["btc_efficiency_ratio"] < 0.25, str((MARKET_STATE["btc_market_state"], MARKET_STATE["btc_efficiency_ratio"])))

sol_row = q("SELECT atr FROM trading_pairs WHERE pair='SOL/USDT'", fetch_one=True)
check("SOL/USDT ATR persisted to trading_pairs (> 0)", sol_row is not None and float(sol_row["atr"]) > 0, str(sol_row))
sol_reg = q("SELECT * FROM market_regimes WHERE pair='SOL/USDT'", fetch_one=True)
check("SOL/USDT regime written as TRENDING", sol_reg is not None and sol_reg["regime"] == "TRENDING", str(sol_reg))

# ---- 4. backwards-compatible 2-tuple analyze_regime_and_volatility
r2, v2 = mgr.analyze_regime_and_volatility(sol_ohlcv)
check("analyze_regime_and_volatility keeps 2-tuple contract", r2 == "TRENDING" and v2 in ("LOW", "HIGH", "EXTREME"), f"{r2}/{v2}")

print("\nFAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)
