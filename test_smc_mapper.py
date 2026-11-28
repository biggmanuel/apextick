import os, sys, tempfile, time
from datetime import datetime, timezone, timedelta
os.environ["SQLITE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "smc.db"); os.environ["DATABASE_URL"] = ""
sys.path.insert(0, os.getcwd())
import database as D; D.init_db()
q = D.execute_query
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond: fails.append(name)

# a LEGACY pool row (no candle_time) that must survive the migration
now_iso = datetime.now(timezone.utc).isoformat()
q("INSERT INTO liquidity_pools (pair,pool_type,direction,top_price,bottom_price,status,created_at,updated_at) VALUES ('OLD/USDT','FVG','BULLISH',2,1,'PENDING',?,?)", (now_iso, now_iso), commit=True)
cols_before = [r["name"] for r in q("PRAGMA table_info(liquidity_pools)", fetch_all=True)]
from smart_money_concepts import SmartMoneyMapper, TIMEFRAME_MS
m = SmartMoneyMapper()
cols_after = [r["name"] for r in q("PRAGMA table_info(liquidity_pools)", fetch_all=True)]
check("migration adds candle_time, keeps legacy rows", "candle_time" not in cols_before and "candle_time" in cols_after and q("SELECT COUNT(*) c FROM liquidity_pools", fetch_one=True)["c"] == 1)
check("indexes created", len(q("SELECT name FROM sqlite_master WHERE type='index' AND name IN ('ux_pools_candle','ix_pools_pair_status')", fetch_all=True)) == 2)
SmartMoneyMapper()   # second boot must not fail
check("migration is repeatable", True)

T0 = int(time.time() * 1000) - 8 * TIMEFRAME_MS
def candles(rows):   # rows: (o,h,l,c) -> ohlcv with timestamps
    return [[T0 + k * TIMEFRAME_MS, o, h, l, c, 1.0] for k, (o, h, l, c) in enumerate(rows)]

BULL = [(10,10.2,9.8,9.9), (9.9,10.0,9.85,9.95), (9.95,10.6,9.95,10.5), (10.5,10.9,10.3,10.8), (10.8,10.9,10.6,10.7), (10.7,10.8,10.6,10.7)]
n = m.map_structure("BUL/USDT", candles(BULL))
rows = q("SELECT pool_type, direction, status, top_price, bottom_price FROM liquidity_pools WHERE pair='BUL/USDT' ORDER BY pool_type", fetch_all=True)
check("bullish FVG + OB found, both PENDING", n == 2 and {(r["pool_type"], r["status"]) for r in rows} == {("FVG","PENDING"),("ORDER_BLOCK","PENDING")}, str([(r["pool_type"], r["top_price"], r["bottom_price"]) for r in rows]))
check("FVG gap is 10.0-10.3", any(r["pool_type"]=="FVG" and r["bottom_price"]==10.0 and r["top_price"]==10.3 for r in rows))

n2 = m.map_structure("BUL/USDT", candles(BULL))
check("re-mapping the same candles adds nothing", n2 == 0 and q("SELECT COUNT(*) c FROM liquidity_pools WHERE pair='BUL/USDT'", fetch_one=True)["c"] == 2)

q("UPDATE liquidity_pools SET status='INVALIDATED' WHERE pair='BUL/USDT'", commit=True)
n3 = m.map_structure("BUL/USDT", candles(BULL))
check("INVALIDATED zones are NOT resurrected as PENDING", n3 == 0 and q("SELECT COUNT(*) c FROM liquidity_pools WHERE pair='BUL/USDT' AND status='PENDING'", fetch_one=True)["c"] == 0)

BROKEN = list(BULL); BROKEN[4] = (10.8,10.9,9.9,10.7)      # later candle dips to 9.9: breaks the FVG (bottom 10.0), only taps the OB
n4 = m.map_structure("BRK/USDT", candles(BROKEN))
rows = q("SELECT pool_type, status FROM liquidity_pools WHERE pair='BRK/USDT'", fetch_all=True)
check("zone already broken by later candles is skipped; tapped one stored as MITIGATED", [(r["pool_type"], r["status"]) for r in rows] == [("ORDER_BLOCK","MITIGATED")], str(rows))

BEAR = [(19.9,20.2,19.8,20.1), (20.1,20.15,20.0,20.05), (20.05,20.05,19.4,19.5), (19.5,19.7,19.1,19.2), (19.2,19.3,19.0,19.1), (19.1,19.2,19.0,19.1)]
n5 = m.map_structure("BER/USDT", candles(BEAR))
rows = q("SELECT pool_type, direction, status, top_price, bottom_price FROM liquidity_pools WHERE pair='BER/USDT'", fetch_all=True)
check("bearish FVG (20.0-19.7) + OB (20.2-19.8) found, all PENDING", n5 == 3 and all(r["direction"] == "BEARISH" and r["status"] == "PENDING" for r in rows) and {(r["pool_type"], r["top_price"], r["bottom_price"]) for r in rows} >= {("FVG", 20.0, 19.7), ("ORDER_BLOCK", 20.2, 19.8)}, str([(r["pool_type"], r["top_price"], r["bottom_price"]) for r in rows]))

# ---- lifecycle: the main bug (MITIGATED zones were never invalidated)
q("INSERT INTO trading_pairs (pair,status,futures_supported,spot_supported,last_price,volume_24h_usd,updated_at) VALUES ('LIFE/USDT','ACTIVE',1,1,100,1e9,?)", (now_iso,), commit=True)
q("INSERT INTO liquidity_pools (pair,pool_type,direction,top_price,bottom_price,status,created_at,updated_at,candle_time) VALUES ('LIFE/USDT','ORDER_BLOCK','BULLISH',101,99,'PENDING',?,?,1)", (now_iso, now_iso), commit=True)
status = lambda: q("SELECT status s FROM liquidity_pools WHERE pair='LIFE/USDT'", fetch_one=True)["s"]
m.manage_mitigation();                                  check("price inside zone: PENDING -> MITIGATED", status() == "MITIGATED")
q("UPDATE trading_pairs SET last_price=98.5 WHERE pair='LIFE/USDT'", commit=True)
m.manage_mitigation();                                  check("price breaks MITIGATED zone: -> INVALIDATED (was never possible before)", status() == "INVALIDATED")
q("UPDATE trading_pairs SET last_price=100 WHERE pair='LIFE/USDT'", commit=True)
m.manage_mitigation();                                  check("price back inside: stays INVALIDATED", status() == "INVALIDATED")
q("INSERT INTO liquidity_pools (pair,pool_type,direction,top_price,bottom_price,status,created_at,updated_at,candle_time) VALUES ('LIFE/USDT','FVG','BEARISH',105,103,'PENDING',?,?,2)", (now_iso, now_iso), commit=True)
q("UPDATE trading_pairs SET last_price=106 WHERE pair='LIFE/USDT'", commit=True)
m.manage_mitigation()
check("bearish zone broken above top -> INVALIDATED", q("SELECT status s FROM liquidity_pools WHERE pair='LIFE/USDT' AND direction='BEARISH'", fetch_one=True)["s"] == "INVALIDATED")

# ---- expiry + prune
old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(); very_old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
q("INSERT INTO liquidity_pools (pair,pool_type,direction,top_price,bottom_price,status,created_at,updated_at,candle_time) VALUES ('EXP/USDT','FVG','BULLISH',5,4,'PENDING',?,?,3)", (old, old), commit=True)
q("INSERT INTO liquidity_pools (pair,pool_type,direction,top_price,bottom_price,status,created_at,updated_at,candle_time) VALUES ('DEAD/USDT','FVG','BULLISH',5,4,'INVALIDATED',?,?,4)", (very_old, very_old), commit=True)
m._last_maintenance = 0; m._maintenance()
check("30h-old zone expired", q("SELECT status s FROM liquidity_pools WHERE pair='EXP/USDT'", fetch_one=True)["s"] == "EXPIRED")
check("72h-old dead zone deleted", q("SELECT COUNT(*) c FROM liquidity_pools WHERE pair='DEAD/USDT'", fetch_one=True)["c"] == 0)
check("recent zones untouched by maintenance", q("SELECT COUNT(*) c FROM liquidity_pools WHERE pair='BER/USDT'", fetch_one=True)["c"] == 3)

# ---- fetch gating (REST calls only when a new candle can exist)
q("INSERT INTO trading_pairs (pair,status,futures_supported,spot_supported,last_price,volume_24h_usd,updated_at) VALUES ('GATE/USDT','ACTIVE',1,1,100,1e9,?)", (now_iso,), commit=True)
calls = []
fresh = candles(BULL); base = int(time.time() * 1000) - 60_000
for k, c in enumerate(fresh): c[0] = base - (len(fresh) - 1 - k) * TIMEFRAME_MS
m.fetch_klines = lambda pair, timeframe='15m': (calls.append(pair), fresh)[1]
m.next_fetch = {}
m.run_smc_cycle(); first = len([p for p in calls if p == "GATE/USDT"])
m.run_smc_cycle(); second = len([p for p in calls if p == "GATE/USDT"])
check("pair fetched once, then not again until a new candle is due", first == 1 and second == 1, f"calls={first},{second}")
check("log summary written", q("SELECT COUNT(*) c FROM logs WHERE source='smc_mapper'", fetch_one=True)["c"] > 0)

print("\nFAILED:", fails if fails else "none")
sys.exit(1 if fails else 0)
