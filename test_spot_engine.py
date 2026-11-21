import os, sys, tempfile, time
from datetime import datetime, timezone, timedelta
os.environ["SQLITE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "spot.db"); os.environ["DATABASE_URL"] = ""
os.environ.pop("ALLOW_LIVE_TRADING", None)
sys.path.insert(0, os.getcwd())
import database as D; D.init_db()
import spot_engine as S
from btc_decline_signal import MARKET_STATE as MS
MS["prices_ts"] = time.time() + 10**9          # fixture prices count as fresh
q = D.execute_query
ACCT = "Emmanuel_Paper_150"
NOW = datetime.now(timezone.utc); iso = lambda d: d.isoformat()
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond: fails.append(name)
cash = lambda: q("SELECT spot_balance_usdt b FROM accounts WHERE account_id=?", (ACCT,), fetch_one=True)["b"]
setv = lambda k, v: q("INSERT INTO bot_settings (key,value,updated_at) VALUES (?,?,?) ON CONFLICT (key) DO UPDATE SET value=excluded.value", (k, v, iso(NOW)), commit=True)
def pair(p, price=100.0):
    q("INSERT INTO trading_pairs (pair,status,spot_supported,futures_supported,last_price,volume_24h_usd,updated_at) VALUES (?,?,?,?,?,?,?)", (p, "ACTIVE", 1, 1, price, 1e9, iso(NOW)), commit=True)
    MS["live_prices"][p] = price
def zone(p, bottom, top, direction="BULLISH", status="PENDING"):
    q("INSERT INTO liquidity_pools (pair,pool_type,direction,top_price,bottom_price,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (p, "ORDER_BLOCK", direction, top, bottom, status, iso(NOW), iso(NOW)), commit=True)
def old_open(p, entry=100.0, qty=0.18, tp=105.0, mode="PAPER", opened=None):
    q("""INSERT INTO spot_trades (account_id,pair,trade_mode,status,entry_price,qty,remaining_qty,notional_usdt,dynamic_tp1,watermark_price,opened_at)
         VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (ACCT, p, mode, "OPEN", entry, qty, qty, entry * qty, tp, entry, iso(opened or (NOW - timedelta(hours=2)))), commit=True)
    return q("SELECT MAX(id) i FROM spot_trades", fetch_one=True)["i"]
setv("btc_gate_override", "1")

# ---------------- zones: narrowest first
pair("SPT/USDT"); zone("SPT/USDT", 99.9, 101.0); zone("SPT/USDT", 90.0, 110.0)
ok, reason, edge = S.check_smc_accumulation_zone("SPT/USDT", 100.0)
check("picks the narrowest zone containing price (edge 99.9)", ok and edge == 99.9, reason)
ok, reason, _ = S.check_smc_accumulation_zone("SPT/USDT", 50.0)
check("no zone at price -> skip code", (not ok) and reason == "no_zone_at_price")

# ---------------- entry: min TP, entry fee, balance
b0 = cash(); S.run_spot_scalper_tick()
t = q("SELECT * FROM spot_trades WHERE status='OPEN'", fetch_one=True)
check("entry taken", t is not None and t["pair"] == "SPT/USDT")
check("tiny stop (0.1%) -> TP lifted to the 1.2% minimum (101.2), not 100.3", abs(t["dynamic_tp1"] - 101.2) < 1e-9, f"{t['dynamic_tp1']}")
check("entry fee booked on the trade", abs(t["profit_usdt"] + 18 * 0.001) < 1e-9, f"{t['profit_usdt']}")
check("balance fell by cost + fee", abs((b0 - cash()) - (18 + 0.018)) < 1e-9, f"{b0 - cash():.4f}")
check("no drawdown stop price stored, SL stays 0 (stop is rule-based)", t["current_sl"] == 0.0)
pair("EEE/USDT"); zone("EEE/USDT", 99.0, 101.0)
S.run_spot_scalper_tick()
check("3-minute account cooldown blocks a second entry", q("SELECT COUNT(*) c FROM spot_trades WHERE status='OPEN'", fetch_one=True)["c"] == 1)

# ---------------- take profit: balance change == recorded profit (fee counted once)
MS["live_prices"]["SPT/USDT"] = 101.3
S.run_spot_scalper_tick()
c = q("SELECT * FROM spot_trades WHERE pair='SPT/USDT'", fetch_one=True)
check("TP hit closes the bag", c["status"] == "CLOSED")
check("balance change == recorded profit", abs((cash() - b0) - c["profit_usdt"]) < 1e-9, f"delta={cash()-b0:.5f} profit={c['profit_usdt']:.5f}")

# ---------------- drawdown stop
pair("DD/USDT", 87.0); zone("DD/USDT", 80.0, 90.0)       # price far below entry 100
b1 = cash(); did = old_open("DD/USDT", opened=NOW)   # opened just now: the entry cooldown keeps Phase 2 quiet
S.run_spot_scalper_tick()
d = q("SELECT * FROM spot_trades WHERE id=?", (did,), fetch_one=True)
check("-13% bag is sold by the 12% drawdown stop", d["status"] == "CLOSED" and d["profit_usdt"] < -2.3, f"{d['profit_usdt']:.3f}")
check("drawdown close credits proceeds minus fee", abs((cash() - b1) - (0.18 * 87.0 * (1 - 0.001))) < 1e-9, f"{cash()-b1:.4f}")
check("drawdown stop is logged", q("SELECT COUNT(*) c FROM logs WHERE message LIKE '%DRAWDOWN STOP (-%'", fetch_one=True)["c"] == 1)
check("losing close blocks a re-buy of that coin for hours", S.is_pair_on_loss_cooldown(ACCT, "DD/USDT", "PAPER"))
check("a winning close does not", not S.is_pair_on_loss_cooldown(ACCT, "SPT/USDT", "PAPER"))
q("DELETE FROM spot_trades", commit=True)

# ---------------- a 7% dip is NOT sold (accumulator behaviour kept)
pair("DIP/USDT", 93.0); did = old_open("DIP/USDT")
S.run_spot_scalper_tick()
check("-7% bag is held", q("SELECT status s FROM spot_trades WHERE id=?", (did,), fetch_one=True)["s"] == "OPEN")

# ---------------- macro halt flattens, and blocks entries
setv("macro_halt", "1"); S.run_spot_scalper_tick(); setv("macro_halt", "0")
check("macro halt flattens open bags", q("SELECT status s FROM spot_trades WHERE id=?", (did,), fetch_one=True)["s"] == "CLOSED")
q("DELETE FROM spot_trades", commit=True)

# ---------------- stale prices: do nothing
did = old_open("DIP/USDT"); MS["prices_ts"] = 0
q("UPDATE trading_pairs SET updated_at=?, last_price=100 WHERE pair='DIP/USDT'", (iso(NOW - timedelta(hours=1)),), commit=True)
MS["live_prices"]["DIP/USDT"] = 80.0                     # -20% on the (stale) stream
S.run_spot_scalper_tick()
check("stale feed + old DB price: no action taken", q("SELECT status s FROM spot_trades WHERE id=?", (did,), fetch_one=True)["s"] == "OPEN")
MS["prices_ts"] = time.time() + 10**9; S.run_spot_scalper_tick()
check("fresh feed: the -20% bag is stopped", q("SELECT status s FROM spot_trades WHERE id=?", (did,), fetch_one=True)["s"] == "CLOSED")
q("DELETE FROM spot_trades", commit=True)

# ---------------- mode flags fail safe + live lock
pair("MODE/USDT"); zone("MODE/USDT", 99.0, 101.0)
setv("paper_trading_enabled", "true"); S.run_spot_scalper_tick()
_row = q("SELECT trade_mode m FROM spot_trades LIMIT 1", fetch_one=True)
check("stray mode value 'true' trades as PAPER", _row is not None and _row["m"] == "PAPER")
q("DELETE FROM spot_trades", commit=True)
setv("paper_trading_enabled", "0")
msg = S.run_spot_scalper_tick()
check("LIVE mode without ALLOW_LIVE_TRADING opens nothing", q("SELECT COUNT(*) c FROM spot_trades", fetch_one=True)["c"] == 0 and "locked" in msg, msg[:60])
setv("paper_trading_enabled", "1")

# ---------------- skip summary
S.SKIP_LOG_INTERVAL_SECONDS = 0; S._LAST_SKIP_LOG = 0
q("DELETE FROM spot_trades", commit=True); S.run_spot_scalper_tick()
check("skip summary line written", q("SELECT COUNT(*) c FROM logs WHERE source='spot_skips'", fetch_one=True)["c"] >= 1)

print("\nFAILED:", fails if fails else "none"); sys.exit(1 if fails else 0)