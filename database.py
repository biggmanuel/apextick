import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

# =============================================================================
# APEXTICK: MASTER DATABASE CONFIGURATION (INSTITUTIONAL QUANT SCHEMA)
# (Upgraded for 75/25 SMC Sniper Architecture)
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "")
IS_POSTGRES = DATABASE_URL.startswith("postgres")

if not IS_POSTGRES:
    SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", str(BASE_DIR / "apextick.db"))

def get_connection():
    """Establishes a connection to the active database (PostgreSQL or SQLite)."""
    if IS_POSTGRES:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    else:
        conn = sqlite3.connect(SQLITE_DB_PATH, timeout=20.0)
        conn.row_factory = sqlite3.Row
        # Enable concurrent read/writes to stop "database is locked" errors
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")   # safe with WAL, far fewer fsyncs
        conn.execute("PRAGMA busy_timeout=20000;")   # wait up to 20s for a lock instead of failing
        return conn

def execute_query(query: str, params: tuple = (), fetch_one: bool = False, fetch_all: bool = False, commit: bool = False):
    """Executes a SQL query safely across both SQLite and PostgreSQL."""
    conn = get_connection()
    try:
        if IS_POSTGRES:
            query = query.replace("?", "%s")
            
        cursor = conn.cursor()
        cursor.execute(query, params)
        
        result = None
        if fetch_one:
            row = cursor.fetchone()
            result = dict(row) if row else None
        elif fetch_all:
            rows = cursor.fetchall()
            result = [dict(r) for r in rows]
            
        if commit:
            conn.commit()
            
        return result
    finally:
        conn.close()

def execute_update_rowcount(query: str, params: tuple = ()) -> int:
    """Runs a single UPDATE/DELETE in one commit and returns rows modified (for atomic status guards)."""
    conn = get_connection()
    try:
        if IS_POSTGRES:
            query = query.replace("?", "%s")
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()

def execute_batch(statements) -> int:
    """
    Runs many (query, params) statements in ONE connection and ONE commit.
    Use it instead of hundreds of execute_query(..., commit=True) calls: far fewer locks.
    Returns the number of rows actually changed.
    """
    if not statements:
        return 0
    conn = get_connection()
    changed = 0
    try:
        cursor = conn.cursor()
        for query, params in statements:
            if IS_POSTGRES:
                query = query.replace("?", "%s")
            cursor.execute(query, params)
            if cursor.rowcount and cursor.rowcount > 0:
                changed += cursor.rowcount
        conn.commit()
        return changed
    finally:
        conn.close()

def prune_logs(keep_days: int = 7):
    """Deletes log rows older than keep_days so the logs table does not grow forever."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    execute_query("DELETE FROM logs WHERE timestamp < ?", (cutoff,), commit=True)

def init_db():
    """
    Initializes the database schema. 
    Upgraded for Smart Money Concepts (SMC), Regime Detection, and Live Toggles.
    """
    pk = "SERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    
    # 1. Accounts Table (Single Clean Account Architecture)
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS accounts (
            id {pk},
            account_id TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            spot_enabled INTEGER DEFAULT 1,
            futures_enabled INTEGER DEFAULT 1,
            spot_balance_usdt REAL DEFAULT 150.0,
            spot_capital_cap REAL DEFAULT 150.0,
            futures_balance_usdt REAL DEFAULT 150.0,
            futures_capital_cap REAL DEFAULT 150.0,
            futures_live_withdraw REAL DEFAULT 0.0
        )
    """, commit=True)

    # 2. Dynamic Trading Pairs (Upgraded with OI Matrix and ATR)
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS trading_pairs (
            id {pk},
            pair TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'ACTIVE',
            spot_supported INTEGER DEFAULT 1,
            futures_supported INTEGER DEFAULT 1,
            change_24h_pct REAL DEFAULT 0.0,
            volume_24h_usd REAL DEFAULT 0.0,
            last_price REAL DEFAULT 0.0,
            oi_usd REAL DEFAULT 0.0,
            oi_spot_ratio REAL DEFAULT 0.0,
            atr REAL DEFAULT 0.0,
            reason TEXT DEFAULT 'Dynamic Market Scan',
            updated_at TEXT NOT NULL
        )
    """, commit=True)

    # --- DYNAMIC MIGRATION: Safely inject OI and ATR columns if upgrading an old SQLite file ---
    new_pair_columns = {
        "oi_usd": "REAL DEFAULT 0.0",
        "oi_spot_ratio": "REAL DEFAULT 0.0",
        "atr": "REAL DEFAULT 0.0"
    }
    for col, col_type in new_pair_columns.items():
        try:
            execute_query(f"ALTER TABLE trading_pairs ADD COLUMN {col} {col_type}", commit=True)
        except Exception:
            pass # Column already exists, proceed normally
    
    # 3. Spot Trades (Upgraded for SMC execution and Paper/Live separation)
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS spot_trades (
            id {pk},
            account_id TEXT NOT NULL,
            pair TEXT NOT NULL,
            trade_mode TEXT DEFAULT 'PAPER', 
            status TEXT DEFAULT 'OPEN',
            entry_price REAL NOT NULL,
            exit_price REAL,
            qty REAL NOT NULL,
            remaining_qty REAL NOT NULL,
            notional_usdt REAL NOT NULL,
            profit_usdt REAL DEFAULT 0.0,
            profit_pct REAL DEFAULT 0.0,
            
            -- NEW 75/25 SNIPER COLUMNS
            ob_edge_price REAL DEFAULT 0.0,
            current_sl REAL DEFAULT 0.0,
            dynamic_tp1 REAL DEFAULT 0.0,
            watermark_price REAL DEFAULT 0.0,
            tp1_hit INTEGER DEFAULT 0,
            
            opened_at TEXT NOT NULL,
            closed_at TEXT
        )
    """, commit=True)

    # 4. Futures Trades (Upgraded for 75/25 SMC Sniper Execution)
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS futures_trades (
            id {pk},
            account_id TEXT NOT NULL,
            pair TEXT NOT NULL,
            trade_mode TEXT DEFAULT 'PAPER',
            direction TEXT NOT NULL,
            status TEXT DEFAULT 'OPEN',
            entry_price REAL NOT NULL,
            exit_price REAL,
            margin_usdt REAL NOT NULL,
            remaining_margin_usdt REAL NOT NULL,
            notional_usdt REAL NOT NULL,
            leverage INTEGER NOT NULL,
            pnl_usdt REAL DEFAULT 0.0,
            pnl_pct REAL DEFAULT 0.0,
            close_type TEXT,
            peak_roi_pct REAL DEFAULT 0.0,
            worst_roi_pct REAL DEFAULT 0.0,
            
            -- NEW 75/25 SNIPER COLUMNS
            ob_edge_price REAL DEFAULT 0.0,
            current_sl REAL DEFAULT 0.0,
            dynamic_tp1 REAL DEFAULT 0.0,
            watermark_price REAL DEFAULT 0.0,
            tp1_hit INTEGER DEFAULT 0,
            
            opened_at TEXT NOT NULL,
            closed_at TEXT
        )
    """, commit=True)

    # --- DYNAMIC MIGRATION: Inject Take-Profit tracking columns into existing trade tables ---
    tp_columns = {
        "tp1_hit": "INTEGER DEFAULT 0",
        "tp2_hit": "INTEGER DEFAULT 0",
        "tp3_hit": "INTEGER DEFAULT 0"
    }
    for table in ["spot_trades", "futures_trades"]:
        for col, col_type in tp_columns.items():
            try:
                execute_query(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}", commit=True)
            except Exception:
                pass # Column already exists, proceed normally

    # --- DYNAMIC MIGRATION: sniper columns for databases created before the 75/25 upgrade ---
    sniper_columns = {
        "ob_edge_price": "REAL DEFAULT 0.0",
        "current_sl": "REAL DEFAULT 0.0",
        "dynamic_tp1": "REAL DEFAULT 0.0",
        "watermark_price": "REAL DEFAULT 0.0",
    }
    extra_columns = {
        "spot_trades": {"remaining_qty": "REAL DEFAULT 0.0"},
        "futures_trades": {"remaining_margin_usdt": "REAL DEFAULT 0.0", "peak_roi_pct": "REAL DEFAULT 0.0", "worst_roi_pct": "REAL DEFAULT 0.0"},
    }
    for table in ["spot_trades", "futures_trades"]:
        wanted = dict(sniper_columns)
        wanted.update(extra_columns.get(table, {}))
        for col, col_type in wanted.items():
            try:
                execute_query(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}", commit=True)
            except Exception:
                pass  # Column already exists, proceed normally

    # 5. System Logs
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS logs (
            id {pk},
            timestamp TEXT NOT NULL,
            level TEXT NOT NULL,
            source TEXT NOT NULL,
            message TEXT NOT NULL
        )
    """, commit=True)

    # 6. Bot Settings (God-Mode Toggles)
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """, commit=True)

    # 7. Liquidity Pools (NEW: Smart Money Concepts Mapping)
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS liquidity_pools (
            id {pk},
            pair TEXT NOT NULL,
            pool_type TEXT NOT NULL,      -- 'FVG' or 'ORDER_BLOCK'
            direction TEXT NOT NULL,      -- 'BULLISH' or 'BEARISH'
            top_price REAL NOT NULL,
            bottom_price REAL NOT NULL,
            status TEXT DEFAULT 'PENDING',-- 'PENDING', 'MITIGATED', 'INVALIDATED'
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """, commit=True)

    # 8. Market Regimes (NEW: Trend vs Range State Machine)
    execute_query(f"""
        CREATE TABLE IF NOT EXISTS market_regimes (
            id {pk},
            pair TEXT UNIQUE NOT NULL,
            regime TEXT DEFAULT 'TRENDING',    -- 'TRENDING' or 'RANGING'
            volatility_state TEXT DEFAULT 'LOW', -- 'LOW', 'HIGH', 'EXTREME'
            updated_at TEXT NOT NULL
        )
    """, commit=True)


    # --- INDEXES (the engines query these tables every 3 seconds) ---
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_futures_acct_mode_status ON futures_trades (account_id, trade_mode, status)",
        "CREATE INDEX IF NOT EXISTS ix_futures_pair_status ON futures_trades (pair, status)",
        "CREATE INDEX IF NOT EXISTS ix_spot_acct_mode_status ON spot_trades (account_id, trade_mode, status)",
        "CREATE INDEX IF NOT EXISTS ix_spot_pair_status ON spot_trades (pair, status)",
        "CREATE INDEX IF NOT EXISTS ix_pairs_status ON trading_pairs (status)",
        "CREATE INDEX IF NOT EXISTS ix_logs_timestamp ON logs (timestamp)",
    ):
        execute_query(stmt, commit=True)

    # --- SEED DEFAULT DATA ---
    now_str = datetime.now(timezone.utc).isoformat()
    
    # Ensure Account matches $150 Capital requirements
    existing_acct = execute_query("SELECT id FROM accounts WHERE account_id = 'Emmanuel_Paper_150'", fetch_one=True)
    if not existing_acct:
        execute_query("""
            INSERT INTO accounts (
                account_id, display_name, spot_enabled, futures_enabled,
                spot_balance_usdt, spot_capital_cap, 
                futures_balance_usdt, futures_capital_cap, futures_live_withdraw
            ) VALUES (?, ?, 1, 1, ?, ?, ?, ?, 0.0)
        """, ("Emmanuel_Paper_150", "Emmanuel ($150)", 150.0, 150.0, 150.0, 150.0), commit=True)

    # Pre-Seed the God-Mode Dashboard Toggles
    default_settings = [
        ('paper_trading_enabled', '1'),
        ('btc_gate_override', '0'),
        ('macro_halt', '0')
    ]
    for key, val in default_settings:
        execute_query(
            "INSERT INTO bot_settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO NOTHING",
            (key, val, now_str),
            commit=True
        )

def add_log(level: str, source: str, message: str):
    """Inserts a system log and prints it to the console."""
    now_str = datetime.now(timezone.utc).isoformat()
    try:
        execute_query(
            "INSERT INTO logs (timestamp, level, source, message) VALUES (?, ?, ?, ?)",
            (now_str, level, source, message),
            commit=True
        )
        # Print to console for immediate visibility during PM2/terminal monitoring
        print(f"[{now_str}] [{level}] {message}")
    except Exception as e:
        print(f"Failed to write log to database: {e}")

if __name__ == "__main__":
    init_db()
    print("ApexTick Database Initialized with Smart Money Concepts, Regime Tracking, and God-Mode UI Memory.")
