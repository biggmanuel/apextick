import os, sys, tempfile, time
from datetime import datetime, timezone, timedelta
db = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["SQLITE_DB_PATH"] = db
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, os.getcwd())

import database as D
D.init_db()
import futures_engine as F
F.PAPER_SLIPPAGE_PCT = 0.0
import time as _t
from btc_decline_signal import MARKET_STATE as _MS
_MS["prices_ts"] = _t.time() + 10**9   # fixture prices count as always fresh
from btc_decline_signal import MARKET_STATE

q = D.execute_query
NOW = datetime.now(timezone.utc)
iso = lambda dt: dt.isoformat()
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond: fails.append(name)

# ---- fixtures
def pair(p, vol=1e9, price=100.0):
    q("INSERT INTO trading_pairs (pair,status,futures_supported,spot_supported,last_price,volume_24h_usd,updated_at) VALUES (?,?,?,?,?,?,?)",
      (p,"ACTIVE",1,1,price,vol,iso(NOW)), commit=True)
    MARKET_STATE["live_prices"][p] = price
def zone(p, direction, bottom, top, status="PENDING", ptype="ORDER_BLOCK"):
    q("INSERT INTO liquidity_pools (pair,pool_type,direction,top_price,bottom_price,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
      (p,ptype,direction,top,bottom,status,iso(NOW),iso(NOW)), commit=True)
def bal(): return q("SELECT futures_balance_usdt b FROM accounts", fetch_one=True)["b"]

q("INSERT OR REPLACE INTO bot_settings (key,value,updated_at) VALUES ('btc_gate_override','1',?)", (iso(NOW),), commit=True)
MARKET_STATE["klines_1h"] = [{"c": 100 + i} for i in range(40)]   # rising -> BULLISH
MARKET_STATE["buy_pressure_pct"] = 60.0
check("trend is BULLISH in fixture", F.calculate_futures_macro_trend() == "BULLISH")

# ---- 1. check_micro_structure: labels, narrowest-first, stop band
pair("AAA/USDT"); zone("AAA/USDT","BULLISH",99.0,101.0); zone("AAA/USDT","BULLISH",90.0,110.0)
ok, reason, edge = F.check_micro_structure("AAA/USDT", 100.0, "LONG")
check("LONG finds BULLISH zone, picks narrowest (edge 99.0)", ok and edge == 99.0, reason)
pair("BBB/USDT"); zone("BBB/USDT","BULLISH",95.0,101.0)
ok, reason, _ = F.check_micro_structure("BBB/USDT", 100.0, "LONG")
check("5% stop rejected as too wide", (not ok) and reason == "stop_too_wide", reason)
pair("CCC/USDT"); zone("CCC/USDT","BULLISH",99.9,101.0)
ok, reason, _ = F.check_micro_structure("CCC/USDT", 100.0, "LONG")
check("0.1% stop rejected as too tight", (not ok) and reason == "stop_too_tight", reason)
pair("CCD/USDT"); zone("CCD/USDT","BULLISH",99.55,101.0)
ok, reason, _ = F.check_micro_structure("CCD/USDT", 100.0, "LONG")
check("0.45% stop rejected under raised 0.5% min_stop_pct", (not ok) and reason == "stop_too_tight", reason)
pair("SSS/USDT"); zone("SSS/USDT","BEARISH",99.0,101.0)
ok, reason, edge = F.check_micro_structure("SSS/USDT", 100.0, "SHORT")
check("SHORT finds BEARISH zone, edge = top (101.0)", ok and edge == 101.0, reason)
ok, reason, _ = F.check_micro_structure("SSS/USDT", 100.0, "LONG")
check("LONG does not use BEARISH zone", not ok, reason)
ok, reason, _ = F.check_micro_structure("AAA/USDT", 50.0, "LONG")
check("price far from any zone -> no_zone_at_price", (not ok) and reason == "no_zone_at_price", reason)

# ---- 2. account cooldown: old OPEN runner must not hide a fresh trade
q("""INSERT INTO futures_trades (account_id,pair,trade_mode,direction,status,entry_price,margin_usdt,remaining_margin_usdt,notional_usdt,leverage,opened_at)
     VALUES ('Emmanuel_Paper_150','OLD/USDT','PAPER','SHORT','OPEN',1,2,2,30,15,?)""", (iso(NOW - timedelta(hours=9)),), commit=True)
q("""INSERT INTO futures_trades (account_id,pair,trade_mode,direction,status,entry_price,margin_usdt,remaining_margin_usdt,notional_usdt,leverage,opened_at,closed_at,close_type)
     VALUES ('Emmanuel_Paper_150','ENA/USDT','PAPER','SHORT','CLOSED',1,2,0,30,15,?,?,'STRUCTURAL SL (SMC Invalidated)')""",
  (iso(NOW - timedelta(seconds=60)), iso(NOW - timedelta(seconds=30))), commit=True)
cd, left = F.is_account_on_futures_cooldown("Emmanuel_Paper_150", "PAPER")
check("account cooldown active even with an old OPEN trade present", cd, f"{left}s left")
cd, left, ct = F.is_pair_on_cooldown("Emmanuel_Paper_150", "ENA/USDT", "PAPER")
check("pair cooldown after structural SL (~2h)", cd and left > 7000, f"{left}s, {ct}")
cd, _, _ = F.is_pair_on_cooldown("Emmanuel_Paper_150", "ZZZ/USDT", "PAPER")
check("pair with no history is free", not cd)
q("DELETE FROM futures_trades", commit=True)

# ---- 3. full tick: entry sizing, TP1 math, fee booking
b0 = bal()
F.run_futures_engine_tick()
t = q("SELECT * FROM futures_trades WHERE status='OPEN'", fetch_one=True)
check("entry taken on AAA (only valid setup)", t is not None and t["pair"] == "AAA/USDT", str(t and t["pair"]))
if t:
    stop_frac = (100 - 99) / 100
    rt_fee = 2.0 * (F.FUTURES_CONFIG["fee_pct"] / 100.0)
    exp_margin = min(0.50 / (15 * (stop_frac + rt_fee)), 150 * 0.05)
    check("fee-aware margin = $0.50/(15*(1%+0.1%)) = $3.03", abs(t["margin_usdt"] - exp_margin) < 1e-6, f"{t['margin_usdt']:.4f}")
    check("TP1 = entry + 2x stop = 102.0", abs(t["dynamic_tp1"] - 102.0) < 1e-6, f"{t['dynamic_tp1']}")
    entry_fee = t["notional_usdt"] * 0.0005
    check("entry fee booked on the trade (pnl_usdt = -fee)", abs(t["pnl_usdt"] + entry_fee) < 1e-9, f"{t['pnl_usdt']:.4f}")
    check("balance dropped by margin + entry fee", abs((b0 - bal()) - (t["margin_usdt"] + entry_fee)) < 1e-9, f"{b0-bal():.4f}")
    total_loss_at_stop = t["notional_usdt"] * (stop_frac + rt_fee)
    check("total loss at stop (incl. round-trip fees) == $0.50 target", abs(total_loss_at_stop - 0.50) < 1e-6, f"{total_loss_at_stop:.4f}")

# ---- 4. second tick: account cooldown blocks another entry
pair("EEE/USDT"); zone("EEE/USDT","BULLISH",99.0,101.0)
F.run_futures_engine_tick()
n_open = q("SELECT COUNT(*) c FROM futures_trades WHERE status='OPEN'", fetch_one=True)["c"]
check("no second entry inside the 10-min account cooldown", n_open == 1, f"open={n_open}")

# ---- 5. stop-out: balance delta must equal recorded pnl (entry fee counted once)
MARKET_STATE["live_prices"]["AAA/USDT"] = 98.9
F.run_futures_engine_tick()
c = q("SELECT * FROM futures_trades WHERE pair='AAA/USDT'", fetch_one=True)
check("trade closed at structural SL", c["status"] == "CLOSED" and c["close_type"].startswith("STRUCTURAL SL"), c["close_type"])
check("balance change == recorded pnl (no double-count)", abs((bal() - b0) - c["pnl_usdt"]) < 1e-9, f"delta={bal()-b0:.4f} pnl={c['pnl_usdt']:.4f}")
check("stop-out costs about the $0.50 risk (+ fees/overshoot)", -0.75 < c["pnl_usdt"] < -0.45, f"{c['pnl_usdt']:.3f}")
MARKET_STATE["live_prices"]["AAA/USDT"] = 100.0
cd, left, _ = F.is_pair_on_cooldown("Emmanuel_Paper_150", "AAA/USDT", "PAPER")
check("AAA now blocked for ~2h after its stop-out", cd and left > 7000, f"{left}s")

# ---- 6. liquidity filter + skip log
pair("THIN/USDT", vol=1e6); zone("THIN/USDT","BULLISH",99.0,101.0)
check("thin pair filtered by volume rule in SQL", q("SELECT COUNT(*) c FROM trading_pairs WHERE volume_24h_usd >= ? AND pair='THIN/USDT'", (F.MIN_VOLUME_24H_USD,), fetch_one=True)["c"] == 0)
F.SKIP_LOG_INTERVAL_SECONDS = 0; F._LAST_SKIP_LOG = 0
q("DELETE FROM futures_trades WHERE status='CLOSED' AND pair='ENA/USDT'", commit=True)
F.run_futures_engine_tick()
row = q("SELECT message FROM logs WHERE source='futures_skips' ORDER BY id DESC LIMIT 1", fetch_one=True)
check("skip-reason summary written to logs", row is not None, row["message"] if row else "")

# ---- 7. Probation circuit breaker: activates after 3 losses, shrinks risk & slots, recovers on 1 win
q("DELETE FROM futures_trades", commit=True)
q("DELETE FROM trading_pairs", commit=True)
q("DELETE FROM liquidity_pools", commit=True)
q("UPDATE accounts SET futures_balance_usdt=150", commit=True)
for k in range(3):
    t_close = NOW - timedelta(hours=3, minutes=30 - k * 5)
    q("""INSERT INTO futures_trades (account_id,pair,trade_mode,direction,status,entry_price,margin_usdt,remaining_margin_usdt,notional_usdt,leverage,pnl_usdt,close_type,opened_at,closed_at)
         VALUES ('Emmanuel_Paper_150',?,'PAPER','LONG','CLOSED',100,3,0,45,15,-0.5,'STRUCTURAL SL',?,?)""",
      (f"L{k}/USDT", iso(t_close - timedelta(minutes=5)), iso(t_close)), commit=True)

st = F.get_futures_bot_state("Emmanuel_Paper_150", "PAPER")
check("probation activates after 3 consecutive losses", st["state"] == "PROBATION" and st["risk_multiplier"] == 0.25 and st["max_positions"] == 2, str(st))

pair("PRB1/USDT"); zone("PRB1/USDT", "BULLISH", 99.0, 101.0)
F.run_futures_engine_tick()
pt = q("SELECT * FROM futures_trades WHERE pair='PRB1/USDT' AND status='OPEN'", fetch_one=True)
exp_prob_margin = max(F.FUTURES_CONFIG["min_margin_usdt"], (0.50 * 0.25) / (15 * (0.01 + 2.0 * 0.0005)))
check("probation entry uses 25% risk sizing", pt is not None and abs(pt["margin_usdt"] - exp_prob_margin) < 1e-6, f"{pt and pt['margin_usdt']}")
import json as _json
ctx = _json.loads(pt["entry_context"]) if (pt and pt.get("entry_context")) else {}
check("entry_context JSON logged with bot_state, market_state, and stop_pct",
      ctx.get("bot_state") == "PROBATION" and ctx.get("btc_ema_trend") == "BULLISH" and abs(ctx.get("stop_pct", 0) - 1.0) < 0.01, str(ctx))

# Fill up to probation_max_positions (2) and verify a 3rd slot is blocked
q("""INSERT INTO futures_trades (account_id,pair,trade_mode,direction,status,entry_price,margin_usdt,remaining_margin_usdt,notional_usdt,leverage,opened_at)
     VALUES ('Emmanuel_Paper_150','PRB2/USDT','PAPER','LONG','OPEN',100,1,1,15,15,?)""", (iso(NOW - timedelta(hours=1)),), commit=True)
q("UPDATE futures_trades SET opened_at=? WHERE pair='PRB1/USDT'", (iso(NOW - timedelta(hours=1)),), commit=True)
pair("PRB3/USDT"); zone("PRB3/USDT", "BULLISH", 99.0, 101.0)
F.run_futures_engine_tick()
check("probation caps open positions at 2 (blocks 3rd slot)", q("SELECT COUNT(*) c FROM futures_trades WHERE status='OPEN'", fetch_one=True)["c"] == 2)

# A winning close returns state to NORMAL
q("""INSERT INTO futures_trades (account_id,pair,trade_mode,direction,status,entry_price,margin_usdt,remaining_margin_usdt,notional_usdt,leverage,pnl_usdt,close_type,opened_at,closed_at)
     VALUES ('Emmanuel_Paper_150','WIN/USDT','PAPER','LONG','CLOSED',100,1,0,15,15,0.6,'TRAILING SL HIT',?,?)""",
  (iso(NOW - timedelta(minutes=25)), iso(NOW - timedelta(minutes=20))), commit=True)
st2 = F.get_futures_bot_state("Emmanuel_Paper_150", "PAPER")
check("1 winning close exits probation back to NORMAL", st2["state"] == "NORMAL" and st2["risk_multiplier"] == 1.0 and st2["max_positions"] == 5, str(st2))

# BTC CHOP enters CAUTION (50% risk, max 2 slots) even with no loss streak
saved_kl = list(MARKET_STATE["klines_1h"])
MARKET_STATE["klines_1h"] = [{"c": 100.0 + (1.0 if i % 2 == 0 else -1.0)} for i in range(25)]
st_chop = F.get_futures_bot_state("Emmanuel_Paper_150", "PAPER")
check("BTC CHOP triggers CAUTION (50% risk, max 2 slots)", st_chop["state"] == "CAUTION" and st_chop["risk_multiplier"] == 0.5 and st_chop["max_positions"] == 2 and st_chop["market_state"] == "CHOP", str(st_chop))
MARKET_STATE["klines_1h"] = saved_kl

# ---- 8. Atomic exit guard: concurrent force-close prevents double-credit in engine
q("DELETE FROM futures_trades", commit=True)
q("DELETE FROM trading_pairs", commit=True)
q("DELETE FROM liquidity_pools", commit=True)
q("UPDATE accounts SET futures_balance_usdt=100", commit=True)
q("""INSERT INTO futures_trades (account_id,pair,trade_mode,direction,status,entry_price,margin_usdt,remaining_margin_usdt,notional_usdt,leverage,pnl_usdt,ob_edge_price,current_sl,dynamic_tp1,watermark_price,opened_at)
     VALUES ('Emmanuel_Paper_150','RACE/USDT','PAPER','LONG','OPEN',100,2,2,30,15,0,99,99,102,100,?)""", (iso(NOW - timedelta(hours=1)),), commit=True)
pair("RACE/USDT", price=98.0)
real_upd = F.execute_update_rowcount
def force_close_first(query, params=()):
    q("UPDATE futures_trades SET status='CLOSED' WHERE pair='RACE/USDT'", commit=True)
    return real_upd(query, params)
F.execute_update_rowcount = force_close_first
b_race = bal()
F.run_futures_engine_tick()
F.execute_update_rowcount = real_upd
check("atomic exit guard: already-closed trade credits $0 (no double-credit)", abs(bal() - b_race) < 1e-9, f"delta={bal()-b_race:.4f}")

print("\nFAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)
