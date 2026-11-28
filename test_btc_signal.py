import os, sys, ssl, tempfile, time
from datetime import datetime, timezone
os.environ["SQLITE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "btc.db"); os.environ["DATABASE_URL"] = ""
sys.path.insert(0, os.getcwd())
import database as D; D.init_db()
import btc_decline_signal as B
q = D.execute_query; MS = B.MARKET_STATE
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond: fails.append(name)

# ---------------- pressure (rolling window, running sums)
B._reset_pressure(); now = int(time.time() * 1000)
B._add_trade(now - 120_000, "SELL", 1000.0)      # older than the 60s window
B._add_trade(now, "BUY", 300.0); B._add_trade(now, "SELL", 100.0)
check("old trades expire from the window; pressure = 75% buy", MS["buy_pressure_pct"] == 75.0 and MS["sell_pressure_pct"] == 25.0, f"{MS['buy_pressure_pct']}/{MS['sell_pressure_pct']}")
check("running sums match the deque", abs(B._sums["BUY"] - 300) < 1e-9 and abs(B._sums["SELL"] - 100) < 1e-9)
B._expire_trades(now + 10**6)
check("empty window resets sums and reports 50/50", B._sums == {"BUY": 0.0, "SELL": 0.0})
B._recalc_pressure(); check("pressure back to 50/50", MS["buy_pressure_pct"] == 50.0)

# ---------------- live klines
MS["klines_1h"] = []
B._apply_kline({"t": 1000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10})
B._apply_kline({"t": 1000, "o": 1, "h": 3, "l": 0.5, "c": 2.5, "v": 20})
B._apply_kline({"t": 2000, "o": 2.5, "h": 3, "l": 2, "c": 2.7, "v": 5})
check("kline update replaces the forming candle, new candle appends", len(MS["klines_1h"]) == 2 and MS["klines_1h"][0]["h"] == 3 and MS["btc_price"] == 2.7)
for i in range(100): B._apply_kline({"t": 3000 + i, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1})
check("kline list capped at 60", len(MS["klines_1h"]) == 60)

# ---------------- freshness
MS["live_prices"]["AAA/USDT"] = 12.5
MS["prices_ts"] = time.time() - 300
check("stale feed -> price 0.0", B.get_fresh_price("AAA/USDT") == 0.0 and not B.prices_are_fresh())
MS["prices_ts"] = time.time()
check("fresh feed -> price returned", B.get_fresh_price("AAA/USDT") == 12.5)
check("unknown pair -> 0.0", B.get_fresh_price("NOPE/USDT") == 0.0)

# ---------------- BTC gate fails CLOSED, no fabricated data
def klines(closes, age_hours=0.0, vol=1000.0):
    t0 = int(time.time() * 1000) - int(age_hours * 3600 * 1000)
    return [{"t": t0 - (len(closes) - 1 - i) * 3600_000, "o": c, "h": c * 1.001, "l": c * 0.999, "c": c, "v": vol} for i, c in enumerate(closes)]
MS["klines_1h"] = []
ev = B.eval_decline_from_closes()
check("no BTC data -> BLOCK (fail-closed)", ev["block"] is True and "unavailable" in ev["reason"], ev["reason"])
check("no fake candles were created", MS["klines_1h"] == [])
MS["klines_1h"] = klines([100.0] * 60, age_hours=6)
ev = B.eval_decline_from_closes(); check("stale BTC data (6h old) -> BLOCK", ev["block"] and "stale" in ev["reason"], ev["reason"])
MS["klines_1h"] = klines([100.0] * 3)
ev = B.eval_decline_from_closes(); check("too few bars -> BLOCK", ev["block"] and "insufficient" in ev["reason"], ev["reason"])
MS["klines_1h"] = klines([100.0] * 60)
ev = B.eval_decline_from_closes(); check("healthy flat BTC -> ALLOW", ev["block"] is False, ev["reason"])
MS["klines_1h"] = klines([100.0] * 56 + [97, 95, 93, 90])
ev = B.eval_decline_from_closes(); check("-10% in 4h -> nuclear BLOCK", ev["block"] and "nuclear" in ev["reason"], ev["reason"])
check("ATR computed from real candles", MS["btc_atr_1h"] > 0)

# ---------------- gate with pressure freshness
MS["klines_1h"] = klines([100.0] * 60)
MS["pressure_ts"] = time.time(); MS["buy_pressure_pct"] = 10.0; MS["sell_pressure_pct"] = 90.0
ok, reason = B.is_trading_allowed_by_btc_gate(); check("fresh low buy pressure blocks", ok is False and "pressure" in reason.lower(), reason[:50])
MS["pressure_ts"] = time.time() - 1000
ok, reason = B.is_trading_allowed_by_btc_gate(); check("STALE pressure is ignored (not a permanent block)", ok is True, reason[:50])
MS["klines_1h"] = []
ok, reason = B.is_trading_allowed_by_btc_gate(); check("gate blocks when BTC data is missing", ok is False)

# ---------------- websocket ssl options
os.environ.pop("WS_INSECURE_SSL", None)
check("certificate checking is ON by default", B._ssl_options() == {})
os.environ["WS_INSECURE_SSL"] = "1"
check("CERT_NONE only when explicitly requested", B._ssl_options() == {"cert_reqs": ssl.CERT_NONE})
os.environ.pop("WS_INSECURE_SSL")

# ---------------- batched DB price writes stamp updated_at
old = "2020-01-01T00:00:00+00:00"
for p in ("P1/USDT", "P2/USDT"):
    q("INSERT INTO trading_pairs (pair,status,last_price,updated_at) VALUES (?,?,?,?)", (p, "ACTIVE", 1.0, old), commit=True)
B._sync_db_prices([("P1/USDT", 5.0, 1.1), ("P2/USDT", 6.0, -2.0)])
rows = {r["pair"]: r for r in q("SELECT * FROM trading_pairs WHERE pair IN ('P1/USDT','P2/USDT')", fetch_all=True)}
check("prices written and updated_at refreshed", rows["P1/USDT"]["last_price"] == 5.0 and rows["P2/USDT"]["change_24h_pct"] == -2.0 and rows["P1/USDT"]["updated_at"] > old)

# ---------------- pair sync: atomic, filters, held pairs, OI cache
class FakeSpot:
    def __init__(self, tickers): self.tickers = tickers
    def fetch_tickers(self): return self.tickers
class FakeFut:
    def __init__(self): self.calls = 0
    def fetch_open_interest(self, sym): self.calls += 1; return {"openInterestValue": 5_000_000.0}
mk = lambda base: {"base": base, "quote": "USDT", "active": True, "spot": True}
markets = {f"{b}/USDT": mk(b) for b in ("BTC", "ETH", "AAA", "JUP", "BTCUP", "SOXLB", "COINB", "NBISB", "GOOGLB", "SOXSB", "LOW", "HELD", "USDC")}
vol = {"BTC": 9e9, "ETH": 5e9, "AAA": 4e8, "JUP": 3e8, "BTCUP": 9e8, "SOXLB": 9e8, "COINB": 9e8, "NBISB": 9e8, "GOOGLB": 9e8, "SOXSB": 9e8, "LOW": 1e6, "HELD": 1e6, "USDC": 9e9}
tickers = {f"{b}/USDT": {"last": 10.0, "percentage": 1.5, "quoteVolume": v} for b, v in vol.items()}
tickers["BTC/USDT"]["last"] = 91000.0; tickers["BTC/USDT"]["percentage"] = -2.0
fut = FakeFut(); fut_markets = {"AAA/USDT:USDT": {}, "ETH/USDT:USDT": {}}
B._get_scan_clients = lambda: (FakeSpot(tickers), fut, markets, fut_markets); B._fut_client = fut; B._oi_cache.clear()
now_s = datetime.now(timezone.utc).isoformat()
for p in ("OLD/USDT", "HELD/USDT"):
    q("INSERT INTO trading_pairs (pair,status,last_price,updated_at) VALUES (?,?,?,?)", (p, "ACTIVE", 1.0, now_s), commit=True)
q("INSERT INTO futures_trades (account_id,pair,trade_mode,direction,status,entry_price,margin_usdt,remaining_margin_usdt,notional_usdt,leverage,opened_at) VALUES ('a','HELD/USDT','PAPER','LONG','OPEN',1,2,2,30,15,?)", (now_s,), commit=True)
n = B.sync_binance_pairs()
active = {r["pair"] for r in q("SELECT pair FROM trading_pairs WHERE status='ACTIVE'", fetch_all=True)}
check("hot list = liquid coins only (AAA, JUP, ETH)", {"AAA/USDT", "JUP/USDT", "ETH/USDT"} <= active and n == 3, f"n={n}")
check("JUP kept (not mistaken for a leveraged token), BTCUP/stablecoin/tokenized stocks excluded",
      "JUP/USDT" in active and not ({"BTCUP/USDT", "SOXLB/USDT", "COINB/USDT", "NBISB/USDT", "GOOGLB/USDT", "SOXSB/USDT", "USDC/USDT", "LOW/USDT"} & active))
check("pair that dropped out of the list is deactivated", q("SELECT status s FROM trading_pairs WHERE pair='OLD/USDT'", fetch_one=True)["s"] == "INACTIVE")
check("pair with an OPEN position stays ACTIVE (keeps getting prices)", "HELD/USDT" in active)
check("BTC trend tracked from the scan", MS["btc_trend"] == "BEARISH" and MS["btc_price"] == 91000.0)
calls_first = fut.calls
B.sync_binance_pairs()
check("open interest is cached between scans", fut.calls == calls_first and calls_first == 2, f"calls {calls_first} -> {fut.calls}")
aaa = q("SELECT oi_usd, oi_spot_ratio FROM trading_pairs WHERE pair='AAA/USDT'", fetch_one=True)
check("oi ratio = OI / 24h volume", abs(aaa["oi_spot_ratio"] - 5e6 / 4e8) < 1e-9, str(aaa))

# ---------------- miniTicker parser & port-443 WS endpoint list
mini = [
    {"s": "BTCUSDT", "c": "105.0", "o": "100.0"},
    {"s": "ETHUSDT", "c": "2000.0", "P": "-2.5"},
    {"s": "ETHBTC", "c": "0.05", "o": "0.05"},
    {"s": "BADUSDT", "c": "0", "o": "10"},
]
parsed = dict((p, (c, chg)) for p, c, chg in B._parse_ticker_payload(mini))
check("miniTicker computes 24h % from c and o, keeps legacy P, skips non-USDT/zero",
      "BTC/USDT" in parsed and abs(parsed["BTC/USDT"][1] - 5.0) < 1e-6
      and "ETH/USDT" in parsed and abs(parsed["ETH/USDT"][1] - (-2.5)) < 1e-6
      and "ETH/BTC" not in parsed and "BAD/USDT" not in parsed, str(parsed))

os.environ.pop("BINANCE_WS_BASE", None)
urls = B._get_ws_urls("/ws/!miniTicker@arr")
check("default WS endpoints use port 443 vision + binance fallback",
      len(urls) == 2 and urls[0] == "wss://data-stream.binance.vision/ws/!miniTicker@arr" and urls[1] == "wss://stream.binance.com:443/ws/!miniTicker@arr", str(urls))
os.environ["BINANCE_WS_BASE"] = "wss://custom.example.com/"
check("BINANCE_WS_BASE env override respected", B._get_ws_urls("/ws/!miniTicker@arr") == ["wss://custom.example.com/ws/!miniTicker@arr"])
os.environ.pop("BINANCE_WS_BASE")

print("\nFAILED:", fails if fails else "none"); sys.exit(1 if fails else 0)
