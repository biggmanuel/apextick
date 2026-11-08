import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

# =============================================================================
# APEXTICK: MASTER DATABASE CONFIGURATION (INSTITUTIONAL QUANT SCHEMA)
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
            tp1_hit INTEGER DEFAULT 0,
            tp2_hit INTEGER DEFAULT 0,
            tp3_hit INTEGER DEFAULT 0,
            opened_at TEXT NOT NULL,
            closed_at TEXT
        )
    """, commit=True)

    # 4. Futures Trades (Upgraded for Trailing Peaks and Liquidation Hunting)
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
            tp1_hit INTEGER DEFAULT 0,
            tp2_hit INTEGER DEFAULT 0,
            tp3_hit INTEGER DEFAULT 0,
            opened_at TEXT NOT NULL,
            closed_at TEXT
        )
    """, commit=True)

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
        ('btc_gate_override', '0')
    ]
    for key, val in default_settings:
        execute_query(
            "INSERT OR IGNORE INTO bot_settings (key, value, updated_at) VALUES (?, ?, ?)",
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
