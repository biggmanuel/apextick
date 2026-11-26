import os, sys, sqlite3, tempfile
from datetime import datetime, timezone, timedelta
path = os.path.join(tempfile.mkdtemp(), "legacy.db")
os.environ["SQLITE_DB_PATH"] = path; os.environ["DATABASE_URL"] = ""
sys.path.insert(0, os.getcwd())
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond: fails.append(name)

# an OLD database: trade tables created before the sniper columns existed
con = sqlite3.connect(path)
con.execute("CREATE TABLE spot_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, pair TEXT NOT NULL, trade_mode TEXT DEFAULT 'PAPER', status TEXT DEFAULT 'OPEN', entry_price REAL NOT NULL, exit_price REAL, qty REAL NOT NULL, notional_usdt REAL NOT NULL, profit_usdt REAL DEFAULT 0.0, profit_pct REAL DEFAULT 0.0, opened_at TEXT NOT NULL, closed_at TEXT)")
con.execute("CREATE TABLE futures_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, pair TEXT NOT NULL, trade_mode TEXT DEFAULT 'PAPER', direction TEXT NOT NULL, status TEXT DEFAULT 'OPEN', entry_price REAL NOT NULL, margin_usdt REAL NOT NULL, notional_usdt REAL NOT NULL, leverage INTEGER NOT NULL, pnl_usdt REAL DEFAULT 0.0, opened_at TEXT NOT NULL, closed_at TEXT)")
con.execute("INSERT INTO spot_trades (account_id,pair,entry_price,qty,notional_usdt,opened_at) VALUES ('a','X/USDT',1,1,1,'2026-01-01T00:00:00+00:00')")
con.commit(); con.close()

import database as D
D.init_db(); D.init_db()    # twice: must be repeatable
cols = lambda t: {r["name"] for r in D.execute_query(f"PRAGMA table_info({t})", fetch_all=True)}
sniper = {"ob_edge_price", "current_sl", "dynamic_tp1", "watermark_price", "tp1_hit"}
check("old spot_trades gets the sniper columns + remaining_qty", sniper <= cols("spot_trades") and "remaining_qty" in cols("spot_trades"))
check("old futures_trades gets sniper columns + remaining margin", sniper <= cols("futures_trades") and "remaining_margin_usdt" in cols("futures_trades"))
check("legacy rows survive the migration", D.execute_query("SELECT COUNT(*) c FROM spot_trades", fetch_one=True)["c"] == 1)
idx = {r["name"] for r in D.execute_query("SELECT name FROM sqlite_master WHERE type='index'", fetch_all=True)}
check("busy-table indexes exist", {"ix_futures_acct_mode_status", "ix_spot_acct_mode_status", "ix_pairs_status", "ix_logs_timestamp"} <= idx)
seeds = {r["key"]: r["value"] for r in D.execute_query("SELECT key, value FROM bot_settings", fetch_all=True)}
check("default settings seeded (paper on, gate override off, macro_halt 0)", seeds.get("paper_trading_enabled") == "1" and seeds.get("btc_gate_override") == "0" and seeds.get("macro_halt") == "0", str(seeds))
D.execute_query("UPDATE bot_settings SET value='0' WHERE key='paper_trading_enabled'", commit=True); D.init_db()
check("re-running init_db never overwrites a setting", D.execute_query("SELECT value v FROM bot_settings WHERE key='paper_trading_enabled'", fetch_one=True)["v"] == "0")
mode = D.execute_query("PRAGMA journal_mode", fetch_one=True)
check("WAL mode on", str(list(mode.values())[0]).lower() == "wal")

n = D.execute_batch([("INSERT INTO logs (timestamp,level,source,message) VALUES (?,?,?,?)", ("2026-01-01T00:00:00+00:00", "INFO", "t", f"m{i}")) for i in range(5)])
check("execute_batch: one transaction, row count returned", n == 5, str(n))
D.add_log("INFO", "t", "recent")
D.prune_logs(keep_days=7)
left = D.execute_query("SELECT COUNT(*) c FROM logs", fetch_one=True)["c"]
check("prune_logs removes old rows, keeps recent", left == 1, f"left={left}")

print("\nFAILED:", fails if fails else "none"); sys.exit(1 if fails else 0)