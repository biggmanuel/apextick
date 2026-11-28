import os, sys, tempfile
from datetime import datetime, timezone
os.environ["SQLITE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "app.db")
os.environ["DATABASE_URL"] = ""
os.environ["ADMIN_KEY"] = "test-key-123"
os.environ.pop("ALLOW_LIVE_TRADING", None)
sys.path.insert(0, os.getcwd())

import database as D; D.init_db()
import app as A
from btc_decline_signal import MARKET_STATE
q = D.execute_query
client = A.app.test_client()
H = {"X-Admin-Key": "test-key-123"}
ACCT = "Emmanuel_Paper_150"
iso = datetime.now(timezone.utc).isoformat()
iso_dt = lambda d: d.isoformat()
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond: fails.append(name)
def fut_cash(): return q("SELECT futures_balance_usdt b FROM accounts WHERE account_id=?", (ACCT,), fetch_one=True)["b"]
def spot_cash(): return q("SELECT spot_balance_usdt b FROM accounts WHERE account_id=?", (ACCT,), fetch_one=True)["b"]

def add_fut(pair, direction, entry, margin, rem, banked, tp1, mode="PAPER", status="OPEN", close_type=None, pnl=None):
    q("""INSERT INTO futures_trades (account_id,pair,trade_mode,direction,status,entry_price,margin_usdt,remaining_margin_usdt,
         notional_usdt,leverage,pnl_usdt,tp1_hit,opened_at,close_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (ACCT,pair,mode,direction,status,entry,margin,rem,margin*15,15,banked if pnl is None else pnl,tp1,iso,close_type), commit=True)
    return q("SELECT MAX(id) i FROM futures_trades", fetch_one=True)["i"]

# ---------------- admin protection
r = client.post("/api/toggle-mode", json={"paper_trading_enabled": True})
check("no admin key -> 403", r.status_code == 403)
r = client.post("/api/toggle-mode", json={"paper_trading_enabled": True}, headers={"X-Admin-Key": "wrong"})
check("wrong admin key -> 403", r.status_code == 403)
A.ADMIN_KEY = "secret_key_here"
r = client.post("/api/toggle-mode", json={"paper_trading_enabled": True}, headers={"X-Admin-Key": "secret_key_here"})
check("default ADMIN_KEY is rejected even when supplied", r.status_code == 403 and "not configured" in r.get_json()["error"], r.get_json()["error"][:40])
A.ADMIN_KEY = "test-key-123"

# ---------------- toggle validation + LIVE lock
r = client.post("/api/toggle-mode", json={}, headers=H);                         check("missing value -> 400", r.status_code == 400)
r = client.post("/api/toggle-mode", json={"paper_trading_enabled": "banana"}, headers=H); check("garbage value -> 400", r.status_code == 400)
r = client.post("/api/toggle-mode", json={"paper_trading_enabled": True}, headers=H)
check("boolean true -> PAPER", r.status_code == 200 and r.get_json()["mode"] == "PAPER")
r = client.post("/api/toggle-mode", json={"paper_trading_enabled": "0"}, headers=H)
check("switch to LIVE refused while locked", r.status_code == 403, r.get_json().get("error","")[:45])
check("mode stayed PAPER", A.get_trade_mode() == "PAPER")
os.environ["ALLOW_LIVE_TRADING"] = "1"
r = client.post("/api/toggle-mode", json={"paper_trading_enabled": False}, headers=H)
check("LIVE allowed once ALLOW_LIVE_TRADING=1", r.status_code == 200 and r.get_json()["mode"] == "LIVE")
client.post("/api/toggle-mode", json={"paper_trading_enabled": "1"}, headers=H)
os.environ.pop("ALLOW_LIVE_TRADING")
q("UPDATE bot_settings SET value='true' WHERE key='paper_trading_enabled'", commit=True)
check("stray value 'true' in DB fails SAFE to PAPER", A.get_trade_mode() == "PAPER")
q("UPDATE bot_settings SET value='1' WHERE key='paper_trading_enabled'", commit=True)
r = client.post("/api/toggle-btc-gate", json={"override": "nope"}, headers=H);  check("btc-gate garbage -> 400", r.status_code == 400)

# ---------------- positions / statistics
q("UPDATE accounts SET futures_balance_usdt=100, spot_balance_usdt=100", commit=True)
MARKET_STATE["live_prices"].update({"AAA/USDT": 90.0, "SPT/USDT": 11.0})
tp1_id = add_fut("AAA/USDT", "SHORT", 100.0, 2.8, 0.7, 0.337, 1)          # 75% already banked at TP1
add_fut("W1/USDT", "LONG", 1, 2, 0, 0, 0, status="CLOSED", close_type="TRAILING SL HIT (-1.5%)", pnl=0.9)
add_fut("L1/USDT", "LONG", 1, 2, 0, 0, 0, status="CLOSED", close_type="STRUCTURAL SL (SMC Invalidated)", pnl=-0.5)
pos = [p for p in client.get("/api/positions").get_json() if p["pair"] == "AAA/USDT"][0]
runner = 0.7 * 15 * 0.10
check("open PnL = banked TP1 + runner (not whole-margin guess)", abs(pos["pnl_usdt"] - (0.337 + runner)) < 1e-9, f"{pos['pnl_usdt']:.4f}")
check("ROI is on ORIGINAL margin", abs(pos["roi_pct"] - (0.337 + runner) / 2.8 * 100) < 1e-6, f"{pos['roi_pct']:.2f}%")
check("size shows what is left", "left of" in pos["size"], pos["size"])
fs = client.get("/api/futures/statistics").get_json()
check("futures win rate counts pnl>0 (50%, was 0%)", fs["win_rate"] == "50.00%", fs["win_rate"])
check("futures equity = cash + open margin + runner", abs(fs["portfolio_value"] - (100 + 0.7 + runner)) < 0.01, str(fs["portfolio_value"]))
q("""INSERT INTO spot_trades (account_id,pair,trade_mode,status,entry_price,qty,remaining_qty,notional_usdt,opened_at)
     VALUES (?,?,?,?,?,?,?,?,?)""", (ACCT,"SPT/USDT","PAPER","OPEN",10.0,2.0,2.0,20.0,iso), commit=True)
ss = client.get("/api/statistics").get_json()
check("spot equity = cash + open value (22)", abs(ss["portfolio_value"] - 122.0) < 0.01 and ss["cash_balance"] == 100.0, str(ss))

# ---------------- the page itself renders through Jinja (skipped if templates/dashboard.html is not there)
if os.path.exists("templates/dashboard.html"):
    r = client.get("/")
    page = r.get_data(as_text=True)
    check("dashboard page renders (HTTP 200, bot name filled in)", r.status_code == 200 and "{{" not in page and "ApexTick" in page, f"{r.status_code}")
    check("page ships the confirmation modal and no prompt()/alert() boxes", 'id="modal"' in page and "prompt(" not in page and "alert(" not in page)
    check("page escapes server data before innerHTML (esc helper present)", "const esc =" in page)

# ---------------- new fields the dashboard page uses
st = client.get("/api/status").get_json()
check("status exposes btc_gate_override / macro_halt / prices_age_seconds", {"btc_gate_override", "macro_halt", "prices_age_seconds"} <= set(st), str(sorted(st)))
check("override flag follows the database switch", st["btc_gate_override"] is False)
client.post("/api/toggle-btc-gate", json={"override": True}, headers=H)
check("override flag turns on after the toggle", client.get("/api/status").get_json()["btc_gate_override"] is True)
client.post("/api/toggle-btc-gate", json={"override": "0"}, headers=H)
check("positions carry raw numeric prices (formatted strings round micro-priced coins to 0)", pos.get("entry_raw") == 100.0 and pos.get("live_raw") == 90.0, str((pos.get("entry_raw"), pos.get("live_raw"))))
check("futures positions carry numeric leverage", pos["leverage"] == 15, str(pos.get("leverage")))

# ---------------- futures exits include LONG / SHORT for the history table
add_fut("S1/USDT", "SHORT", 1, 2, 0, 0, 0, status="CLOSED", close_type="TRAILING SL HIT (+1.5%)", pnl=0.4)
q("UPDATE futures_trades SET closed_at=? WHERE status='CLOSED'", (iso,), commit=True)
hist = client.get("/api/trades").get_json()["futures_trades"]
check("futures exits carry direction (LONG and SHORT)", len(hist) >= 3 and all(h.get("direction") in ("LONG", "SHORT") for h in hist) and {"LONG", "SHORT"} <= {h["direction"] for h in hist}, str(sorted({h.get("direction") for h in hist})))

# ---------------- force close: futures with banked TP1
cash0 = fut_cash()
equity_before = cash0 + 0.7 + runner
r = client.post(f"/api/trades/close/futures/{tp1_id}", headers=H)
check("force close ok", r.status_code == 200, str(r.get_json()))
fee = 0.7 * 15 * (A.FUTURES_CONFIG["fee_pct"] / 100)
t = q("SELECT * FROM futures_trades WHERE id=?", (tp1_id,), fetch_one=True)
check("balance credited (remaining margin + runner - fee)", abs((fut_cash() - cash0) - (0.7 + runner - fee)) < 1e-9, f"+{fut_cash()-cash0:.4f}")
check("recorded PnL = banked + runner - fee (not whole-margin)", abs(t["pnl_usdt"] - (0.337 + runner - fee)) < 1e-9, f"{t['pnl_usdt']:.4f}")
check("equity only changes by the exit fee", abs(equity_before - fut_cash() - fee) < 1e-9)
check("trade row closed, remaining margin zeroed", t["status"] == "CLOSED" and t["remaining_margin_usdt"] == 0 and t["close_type"] == "MANUAL_FORCE_CLOSE")
r = client.post(f"/api/trades/close/futures/{tp1_id}", headers=H);  check("second close -> 409 (no double credit)", r.status_code == 409)
cash1 = fut_cash(); client.post(f"/api/trades/close/futures/{tp1_id}", headers=H)
check("balance unchanged after repeat", fut_cash() == cash1)
live_id = add_fut("LV/USDT", "LONG", 1, 2, 2, 0, 0, mode="LIVE")
r = client.post(f"/api/trades/close/futures/{live_id}", headers=H);  check("LIVE trade refused (no fake close)", r.status_code == 409)
check("LIVE trade still OPEN", q("SELECT status s FROM futures_trades WHERE id=?", (live_id,), fetch_one=True)["s"] == "OPEN")
check("unknown trade -> 404", client.post("/api/trades/close/futures/99999", headers=H).status_code == 404)
check("bad trade type -> 400", client.post("/api/trades/close/options/1", headers=H).status_code == 400)

# ---------------- force close: spot
sid = q("SELECT id i FROM spot_trades WHERE pair='SPT/USDT'", fetch_one=True)["i"]
sc0 = spot_cash()
r = client.post(f"/api/trades/close/spot/{sid}", headers=H)
sfee = 22.0 * (A.SPOT_CONFIG["fee_pct"] / 100)
check("spot close credits proceeds minus fee", abs((spot_cash() - sc0) - (22.0 - sfee)) < 1e-9, f"+{spot_cash()-sc0:.4f}")
st = q("SELECT * FROM spot_trades WHERE id=?", (sid,), fetch_one=True)
check("spot profit recorded = 22 - 20 - fee", abs(st["profit_usdt"] - (2.0 - sfee)) < 1e-9, f"{st['profit_usdt']:.4f}")

# ---------------- liquidate all
a = add_fut("AAA/USDT", "LONG", 80.0, 2.0, 2.0, 0.0, 0)
b = add_fut("AAA/USDT", "SHORT", 95.0, 2.0, 2.0, 0.0, 0)
c = add_fut("NOPRICE/USDT", "LONG", 5.0, 2.0, 2.0, 0.0, 0)
cash2 = fut_cash()
r = client.post("/api/trades/liquidate-all", headers=H).get_json()
check("liquidate-all: 2 closed, 1 no-price skipped, LIVE skipped", r["closed_futures"] == 2 and r["skipped_no_price"] == 1 and r["skipped_live"] == 1, r["message"][:90])
check("liquidate-all credited the account", fut_cash() > cash2 + 3.5, f"+{fut_cash()-cash2:.3f}")
check("LIVE trade untouched by liquidate-all", q("SELECT status s FROM futures_trades WHERE id=?", (live_id,), fetch_one=True)["s"] == "OPEN")

# ---------------- performance statistics: streaks, profit factor, expectancy (known sequence)
from datetime import timedelta
q("DELETE FROM futures_trades", commit=True); q("DELETE FROM spot_trades", commit=True)
base = datetime.now(timezone.utc) - timedelta(hours=3)
seq = [(1.0, "LONG"), (2.0, "LONG"), (-0.5, "LONG"), (-0.5, "LONG"), (-0.5, "SHORT"), (1.0, "SHORT"), (-0.5, "SHORT")]
for k, (p, d) in enumerate(seq):
    closed = base + timedelta(minutes=10 * k)
    q("""INSERT INTO futures_trades (account_id,pair,trade_mode,direction,status,entry_price,margin_usdt,remaining_margin_usdt,notional_usdt,leverage,pnl_usdt,tp1_hit,close_type,opened_at,closed_at)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (ACCT, "P%d/USDT" % k, "PAPER", d, "CLOSED", 1, 2, 0, 30, 15, p, 1 if p > 0 else 0, "TRAILING SL HIT" if p > 0 else "STRUCTURAL SL (SMC Invalidated)", iso_dt(closed - timedelta(minutes=10)), iso_dt(closed)), commit=True)
q("""INSERT INTO futures_trades (account_id,pair,trade_mode,direction,status,entry_price,margin_usdt,remaining_margin_usdt,notional_usdt,leverage,pnl_usdt,opened_at,closed_at)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (ACCT, "LIVE/USDT", "LIVE", "LONG", "CLOSED", 1, 2, 0, 30, 15, 99.0, iso_dt(base), iso_dt(base)), commit=True)
for k, p in enumerate((0.4, 0.6)):
    q("""INSERT INTO spot_trades (account_id,pair,trade_mode,status,entry_price,qty,remaining_qty,notional_usdt,profit_usdt,opened_at,closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
      (ACCT, "S%d/USDT" % k, "PAPER", "CLOSED", 1, 1, 0, 1, p, iso_dt(base), iso_dt(base + timedelta(minutes=k))), commit=True)
perf = client.get("/api/performance").get_json(); f = perf["futures"]
check("performance: counts, win rate and net (LIVE trades excluded)", f["trades"] == 7 and f["wins"] == 3 and f["losses"] == 4 and abs(f["win_rate"] - 42.86) < 0.01 and abs(f["net_pnl"] - 2.0) < 1e-9, str((f["trades"], f["win_rate"], f["net_pnl"])))
check("performance: profit factor, avg win/loss, payoff, break-even win rate", f["profit_factor"] == 2.0 and abs(f["avg_win"] - 1.3333) < 1e-3 and f["avg_loss"] == -0.5 and abs(f["payoff_ratio"] - 2.667) < 1e-3 and abs(f["breakeven_win_rate"] - 27.27) < 0.01, str((f["profit_factor"], f["payoff_ratio"], f["breakeven_win_rate"])))
check("performance: expectancy, best and worst trade", abs(f["expectancy"] - 0.2857) < 1e-3 and f["best_trade"] == 2.0 and f["worst_trade"] == -0.5)
check("performance: longest WIN streak = 2 (+$3.00) with its time window", f["longest_win_streak"]["length"] == 2 and f["longest_win_streak"]["net"] == 3.0 and f["longest_win_streak"]["start"] < f["longest_win_streak"]["end"])
check("performance: longest LOSING streak = 3 (-$1.50)", f["longest_loss_streak"]["length"] == 3 and f["longest_loss_streak"]["net"] == -1.5)
check("performance: current streak is L1, avg hold 10 min, TP1 hits 3, stop-outs 4", f["current_streak"] == {"type": "L", "length": 1} and f["avg_hold_minutes"] == 10.0 and f["tp1_hits"] == 3 and f["stop_outs"] == 4)
check("performance: by direction", f["by_direction"]["LONG"]["trades"] == 4 and f["by_direction"]["SHORT"]["trades"] == 3 and abs(f["by_direction"]["LONG"]["net_pnl"] - 2.0) < 1e-9)
check("performance: last-24h PnL covers the window", abs(f["pnl_24h"] - 2.0) < 1e-9)
sp = perf["spot"]
check("performance: spot with no losses (profit factor / payoff null, current streak W2)", sp["trades"] == 2 and sp["profit_factor"] is None and sp["payoff_ratio"] is None and sp["current_streak"] == {"type": "W", "length": 2})
q("DELETE FROM futures_trades", commit=True); q("DELETE FROM spot_trades", commit=True)
empty = client.get("/api/performance").get_json()
check("performance: empty history does not crash", empty["futures"] == {"trades": 0} and empty["spot"] == {"trades": 0})

print("\nFAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)
