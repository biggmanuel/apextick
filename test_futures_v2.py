import os, sys, tempfile, time
from datetime import datetime, timezone, timedelta
os.environ["SQLITE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "fv2.db"); os.environ["DATABASE_URL"] = ""
os.environ.pop("ALLOW_LIVE_TRADING", None)
sys.path.insert(0, os.getcwd())
import database as D; D.init_db()
import futures_engine as F
from btc_decline_signal import MARKET_STATE as MS
MS["prices_ts"] = time.time() + 10**9
q = D.execute_query
ACCT = "Emmanuel_Paper_150"; NOW = datetime.now(timezone.utc); iso = lambda d: d.isoformat()
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond: fails.append(name)
cash = lambda: q("SELECT futures_balance_usdt b FROM accounts WHERE account_id=?", (ACCT,), fetch_one=True)["b"]
setv = lambda k, v: q("INSERT INTO bot_settings (key,value,updated_at) VALUES (?,?,?) ON CONFLICT (key) DO UPDATE SET value=excluded.value", (k, v, iso(NOW)), commit=True)
def pair(p, price=100.0):
    q("INSERT INTO trading_pairs (pair,status,futures_supported,spot_supported,last_price,volume_24h_usd,updated_at) VALUES (?,?,?,?,?,?,?)", (p, "ACTIVE", 1, 1, price, 1e9, iso(NOW)), commit=True)
    MS["live_prices"][p] = price
def zone(p, direction, bottom, top):
    q("INSERT INTO liquidity_pools (pair,pool_type,direction,top_price,bottom_price,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (p, "ORDER_BLOCK", direction, top, bottom, "PENDING", iso(NOW), iso(NOW)), commit=True)
def open_trade(p, direction, entry, margin, rem, banked, tp1_hit, sl, tp1, opened=None):
    q("""INSERT INTO futures_trades (account_id,pair,trade_mode,direction,status,entry_price,margin_usdt,remaining_margin_usdt,notional_usdt,leverage,pnl_usdt,tp1_hit,
         ob_edge_price,current_sl,dynamic_tp1,watermark_price,opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (ACCT, p, "PAPER", direction, "OPEN", entry, margin, rem, margin * 15, 15, banked, tp1_hit, sl, sl, tp1, entry, iso(opened or NOW - timedelta(hours=2))), commit=True)
    return q("SELECT MAX(id) i FROM futures_trades", fetch_one=True)["i"]
get = lambda i: q("SELECT * FROM futures_trades WHERE id=?", (i,), fetch_one=True)
setv("btc_gate_override", "1"); MS["klines_1h"] = [{"c": 100 + i} for i in range(40)]; MS["buy_pressure_pct"] = 60.0
FEE = F.FUTURES_CONFIG["fee_pct"] / 100.0

# ---------------- paper slippage (entry and exit)
F.PAPER_SLIPPAGE_PCT = 0.05; s = 0.0005
pair("AAA/USDT"); zone("AAA/USDT", "BULLISH", 99.0, 101.0)
b0 = cash(); F.run_futures_engine_tick()
t = q("SELECT * FROM futures_trades WHERE status='OPEN'", fetch_one=True)
check("LONG entry fills 0.05% WORSE than the quote (100.05)", abs(t["entry_price"] - 100.05) < 1e-9, f"{t['entry_price']}")
check("stop and TP1 stay on the zone/quote grid (99.0 / 102.0)", t["current_sl"] == 99.0 and abs(t["dynamic_tp1"] - 102.0) < 1e-9)
MS["live_prices"]["AAA/USDT"] = 98.9; F.run_futures_engine_tick()
c = get(t["id"]); exit_px = 98.9 * (1 - s)
exp_net = t["margin_usdt"] * ((exit_px - 100.05) / 100.05 * 15) - t["notional_usdt"] * FEE
exp_total = -t["notional_usdt"] * FEE + exp_net
check("stop-out exits 0.05% worse than the quote", c["status"] == "CLOSED" and abs(c["exit_price"] - exit_px) < 1e-9, f"{c['exit_price']}")
check("recorded PnL = entry fee + slipped exit - exit fee", abs(c["pnl_usdt"] - exp_total) < 1e-9, f"{c['pnl_usdt']:.5f} vs {exp_total:.5f}")
check("balance change == recorded PnL", abs((cash() - b0) - c["pnl_usdt"]) < 1e-9, f"{cash()-b0:.5f}")
check("pnl_pct = total return on the ORIGINAL margin", abs(c["pnl_pct"] - c["pnl_usdt"] / c["margin_usdt"] * 100) < 1e-9, f"{c['pnl_pct']:.2f}%")
q("DELETE FROM futures_trades", commit=True)

# ---------------- TP1 -> breakeven -> trailing, percent is on original margin
F.PAPER_SLIPPAGE_PCT = 0.0
q("DELETE FROM liquidity_pools", commit=True)
tid = open_trade("AAA/USDT", "LONG", 100.0, 2.8, 2.8, -0.02, 0, 99.0, 102.0)
MS["live_prices"]["AAA/USDT"] = 102.1; F.run_futures_engine_tick()
t1 = get(tid); exp_net = 2.1 * (0.021 * 15) - (2.1 * 15) * FEE
check("TP1 banks 75% of margin", t1["tp1_hit"] == 1 and abs(t1["remaining_margin_usdt"] - 0.7) < 1e-9)
check("TP1 pnl_pct = (banked + fee)/orig margin", abs(t1["pnl_pct"] - ((-0.02 + exp_net) / 2.8 * 100)) < 1e-6, f"{t1['pnl_pct']:.2f}%")
MS["live_prices"]["AAA/USDT"] = 103.0; F.run_futures_engine_tick()
MS["live_prices"]["AAA/USDT"] = 103.0 * (1 - F.FUTURES_CONFIG["trailing_buffer_pct"] / 100) - 0.01; F.run_futures_engine_tick()
t2 = get(tid)
check("trailing exit uses the configured 1.5% (label + trigger)", t2["status"] == "CLOSED" and "-1.5% from peak" in t2["close_type"], t2["close_type"])
q("DELETE FROM futures_trades", commit=True)

# ---------------- macro halt keeps banked profit and trade history
F.PAPER_SLIPPAGE_PCT = 0.05
hid = open_trade("AAA/USDT", "SHORT", 100.0, 2.8, 0.7, 0.337, 1, 100.0, 98.0)
MS["live_prices"]["AAA/USDT"] = 99.0; b0 = cash()
setv("macro_halt", "1"); F.run_futures_engine_tick(); setv("macro_halt", "0")
h = get(hid); px = 99.0 * (1 + 0.0005)
net = 0.7 * ((100.0 - px) / 100.0 * 15) - (0.7 * 15) * FEE
check("halt closes the runner", h["status"] == "CLOSED" and h["close_type"].startswith("MACRO HALT"), h["close_type"])
check("halt keeps the ORIGINAL margin/notional (history no longer overwritten)", h["margin_usdt"] == 2.8 and abs(h["notional_usdt"] - 42.0) < 1e-9)
check("halt PnL = banked TP1 + runner result", abs(h["pnl_usdt"] - (0.337 + net)) < 1e-9, f"{h['pnl_usdt']:.5f}")
check("halt credits remaining margin + runner result", abs((cash() - b0) - (0.7 + net)) < 1e-9, f"{cash()-b0:.5f}")
q("DELETE FROM futures_trades", commit=True)

# ---------------- mode flags fail safe, LIVE lock
F.PAPER_SLIPPAGE_PCT = 0.0
zone("AAA/USDT", "BULLISH", 99.0, 101.0); MS["live_prices"]["AAA/USDT"] = 100.0
setv("paper_trading_enabled", "true"); F.run_futures_engine_tick()
r = q("SELECT trade_mode m FROM futures_trades LIMIT 1", fetch_one=True)
check("stray mode value 'true' trades as PAPER", r is not None and r["m"] == "PAPER")
q("DELETE FROM futures_trades", commit=True)
setv("paper_trading_enabled", "0"); msg = F.run_futures_engine_tick()
check("LIVE without ALLOW_LIVE_TRADING opens nothing", q("SELECT COUNT(*) c FROM futures_trades", fetch_one=True)["c"] == 0 and "locked" in msg, msg[:60])
setv("paper_trading_enabled", "1")

# ---------------- stale prices: no action on old data
sid = open_trade("AAA/USDT", "LONG", 100.0, 2.8, 2.8, 0.0, 0, 99.0, 102.0)
MS["prices_ts"] = 0; MS["live_prices"]["AAA/USDT"] = 95.0
q("UPDATE trading_pairs SET updated_at=?, last_price=100 WHERE pair='AAA/USDT'", (iso(NOW - timedelta(hours=1)),), commit=True)
F.run_futures_engine_tick()
check("stale stream + old DB price: stop NOT evaluated on old data", get(sid)["status"] == "OPEN")
MS["prices_ts"] = time.time() + 10**9; F.run_futures_engine_tick()
check("fresh feed: the stop-out happens", get(sid)["status"] == "CLOSED")
q("DELETE FROM futures_trades", commit=True)

# ---------------- exits no longer overwrite a balance changed elsewhere (dashboard credit)
rid = open_trade("AAA/USDT", "LONG", 100.0, 2.8, 2.8, 0.0, 0, 99.0, 102.0)
b0 = cash(); real = F.execute_query
def sneaky(query, params=(), **kw):               # a dashboard credit lands right before the engine's balance write
    if query.startswith("UPDATE accounts SET futures_balance_usdt = futures_balance_usdt +") and not getattr(sneaky, "done", False):
        sneaky.done = True; real("UPDATE accounts SET futures_balance_usdt = futures_balance_usdt + 10 WHERE account_id = ?", (ACCT,), commit=True)
    return real(query, params, **kw)
F.execute_query = sneaky; MS["live_prices"]["AAA/USDT"] = 98.0; F.run_futures_engine_tick(); F.execute_query = real
r = get(rid)
check("a concurrent +$10 credit survives the engine's exit (relative update)", abs((cash() - b0) - (10 + 2.8 + (r["pnl_usdt"]))) < 1e-9, f"delta={cash()-b0:.4f}")

print("\nFAILED:", fails if fails else "none"); sys.exit(1 if fails else 0)
