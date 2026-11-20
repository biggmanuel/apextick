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

print("\nFAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)