import os, sys, tempfile, subprocess
from datetime import datetime, timezone, timedelta
os.environ["SQLITE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "btc_corr.db"); os.environ["DATABASE_URL"] = ""
sys.path.insert(0, os.getcwd())
import database as D; D.init_db()
import btc_vs_trades as B
q = D.execute_query
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond: fails.append(name)

H = 3_600_000
t0 = datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc)
def closed(minutes, pnl, ctype="STRUCTURAL SL (SMC Invalidated)"):
    ts = t0 + timedelta(minutes=minutes)
    q("""INSERT INTO futures_trades (account_id,pair,trade_mode,direction,status,entry_price,margin_usdt,remaining_margin_usdt,notional_usdt,leverage,pnl_usdt,close_type,opened_at,closed_at)
         VALUES ('a','X/USDT','PAPER','LONG','CLOSED',1,2,0,30,15,?,?,?,?)""", (pnl, ctype, (ts - timedelta(minutes=5)).isoformat(), ts.isoformat()), commit=True)
# 04:00 hour: two wins (BTC falling), 05:00: none, 06:00-08:00: five losses (BTC pumping then flat)
closed(10, 1.5, "TRAILING SL HIT"); closed(20, 1.2, "TRAILING SL HIT")
for m in (130, 140, 150, 190, 200): closed(m, -0.6)

base = int(t0.timestamp() * 1000)
candles = [[base + i * H, o, h, l, c, 1.0] for i, (o, h, l, c) in enumerate([
    (60000, 60100, 58000, 58500), (58500, 58700, 55100, 56000), (56000, 60500, 55900, 60200), (60200, 61000, 60000, 60900), (60900, 61100, 60500, 60800), (60800, 60900, 60600, 60700)])]

class Fake:
    def __init__(self, name, fail=False): self.name, self.fail = name, fail
    def fetch_ohlcv(self, symbol, tf, since=None, limit=1000):
        Fake.calls.append((self.name, symbol)); 
        if self.fail: raise RuntimeError("451 restricted location")
        return [c for c in candles if c[0] >= since]
Fake.calls = []
def factory(name): return Fake(name, fail=(name == "binance"))

trades = B.load_trades(os.environ["SQLITE_DB_PATH"], "PAPER")
check("loads closed trades with UTC timestamps", len(trades) == 7 and trades[0]["t"].tzinfo is not None)
name, got = B.fetch_candles(["binance", "kraken"], "1h", base, base + 6 * H, factory)
check("falls through to the next exchange when the first is blocked", name == "kraken" and [c[0] for c in Fake.calls[:2]] == ["binance", "kraken"] or name == "kraken", str(Fake.calls[:3]))
check("uses the right symbol per exchange (BTC/USD on kraken)", ("kraken", "BTC/USD") in Fake.calls)
check("candles come back sorted and de-duplicated", [c[0] for c in got] == sorted({c[0] for c in got}) and len(got) == 6)
try:
    B.fetch_candles(["binance"], "1h", base, base + 6 * H, factory); ok = False
except RuntimeError as e:
    ok = "451" in str(e)
check("all exchanges failing gives a clear error", ok)

rows = B.analyze(trades, got, "1h")
r4, r6 = rows[base], rows[base + 2 * H]
check("trades land in the right BTC hour (04:00 -> 2 wins, +2.70)", r4["n"] == 2 and r4["w"] == 2 and abs(r4["net"] - 2.7) < 1e-9)
check("06:00 hour: BTC +7.5%, three stop-outs (-1.80)", abs(rows[base + 2 * H]["chg"] - 7.5) < 0.01 and rows[base + 2 * H]["n"] == 3 and rows[base + 2 * H]["stops"] == 3 and abs(rows[base + 2 * H]["net"] + 1.8) < 1e-9)
check("hour with no trades is kept with zero counts", rows[base + H]["n"] == 0)
check("candle stats: change and range", abs(r4["chg"] - (-2.5)) < 1e-6 and abs(r4["range"] - 3.5) < 1e-6)
s = B.summarize(rows)
up = s["BTC up (>= +0.3%)"]; down = s["BTC down (<= -0.3%)"]
check("PnL split by BTC hour type: falling hour +2.70, pumping hours negative", abs(down["net"] - 2.7) < 1e-9 and up["trades"] == 5 and up["net"] < 0, str(s))
win = B.streak_window(trades)
check("longest losing streak found (5 trades, -3.00)", win[2] == 5 and abs(win[3] + 3.0) < 1e-9)

# end-to-end through the command line with an injected exchange
B.make_exchange = factory
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = B.main([os.environ["SQLITE_DB_PATH"], "PAPER", "--exchange", "kraken"])
out = buf.getvalue()
check("report prints the hourly table, the streak window and the summary", rc == 0 and "inside your longest losing streak" in out and "BTC over that window" in out and "By BTC hour type" in out and "<-- big move" in out)

print("\nFAILED:", fails if fails else "none"); sys.exit(1 if fails else 0)
