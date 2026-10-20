import sqlite3
from datetime import datetime, timezone
import psycopg
from psycopg.rows import dict_row
from config import DATABASE_URL, SQLITE_DB_PATH
import time

IS_POSTGRES = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")


def get_connection():
    """Returns a PostgreSQL connection if DATABASE_URL is set, otherwise local SQLite with a timeout."""
    if IS_POSTGRES:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    # Increased timeout to 15 seconds so SQLite waits gracefully if another thread is writing
    conn = sqlite3.connect(SQLITE_DB_PATH, timeout=15.0, check_same_thread=False)
    # Use WAL mode to vastly improve concurrent read/write performance
    conn.execute('pragma journal_mode=wal')
    conn.row_factory = sqlite3.Row
    return conn


def _prep_sql(query: str) -> str:
    """Converts SQLite '?' placeholders to PostgreSQL '%s' automatically."""
    if IS_POSTGRES:
        return query.replace("?", "%s")
    return query


def execute_query(query: str, params: tuple = (), fetch_one: bool = False, fetch_all: bool = False, commit: bool = False):
    """Unified query runner with automatic retry for SQLite locks."""
    max_retries = 5
    for attempt in range(max_retries):
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(_prep_sql(query), params)
            result = None
            if fetch_one:
                row = cur.fetchone()
                result = dict(row) if row else None
            elif fetch_all:
                rows = cur.fetchall()
                result = [dict(r) for r in rows]
            if commit:
                conn.commit()
            return result
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(0.2)  # Wait 200ms and retry
                continue
            raise e
        finally:
            conn.close()


def init_db():
    """Creates all tables for Accounts, Spot, Futures, Settings, and Logs."""
    pk_type = "SERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"

    queries = [
        f"""
        CREATE TABLE IF NOT EXISTS accounts (
            id {pk_type},
            account_id TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            masked_name TEXT NOT NULL,
            mode TEXT DEFAULT 'PAPER',
            spot_enabled INTEGER DEFAULT 1,
            futures_enabled INTEGER DEFAULT 1,
            spot_capital_cap REAL DEFAULT 250.0,
            futures_capital_cap REAL DEFAULT 250.0,
            spot_balance_usdt REAL DEFAULT 250.0,
            futures_balance_usdt REAL DEFAULT 250.0,
            futures_live_withdraw REAL DEFAULT 0.0,
            manual_open_count INTEGER DEFAULT 0,
            manual_trade_count INTEGER DEFAULT 0,
            api_key TEXT DEFAULT '',
            api_secret TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        """,
        f"""
        CREATE TABLE IF NOT EXISTS trading_pairs (
            id {pk_type},
            pair TEXT UNIQUE NOT NULL,
            status TEXT DEFAULT 'ACTIVE',
            spot_supported INTEGER DEFAULT 1,
            futures_supported INTEGER DEFAULT 1,
            change_24h_pct REAL DEFAULT 0.0,
            volume_24h_usd REAL DEFAULT 0.0,
            last_price REAL DEFAULT 0.0,
            reason TEXT DEFAULT 'Auto-discovered liquid USDT pair',
            updated_at TEXT NOT NULL
        );
        """,
        f"""
        CREATE TABLE IF NOT EXISTS spot_trades (
            id {pk_type},
            account_id TEXT NOT NULL,
            pair TEXT NOT NULL,
            status TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL DEFAULT 0.0,
            qty REAL NOT NULL,
            remaining_qty REAL NOT NULL,
            notional_usdt REAL NOT NULL,
            profit_usdt REAL DEFAULT 0.0,
            profit_pct REAL DEFAULT 0.0,
            tp1_hit INTEGER DEFAULT 0,
            tp2_hit INTEGER DEFAULT 0,
            tp3_hit INTEGER DEFAULT 0,
            opened_at TEXT NOT NULL,
            closed_at TEXT DEFAULT ''
        );
        """,
        f"""
        CREATE TABLE IF NOT EXISTS futures_trades (
            id {pk_type},
            account_id TEXT NOT NULL,
            pair TEXT NOT NULL,
            direction TEXT NOT NULL,
            status TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL DEFAULT 0.0,
            margin_usdt REAL NOT NULL,
            remaining_margin_usdt REAL NOT NULL,
            notional_usdt REAL NOT NULL,
            leverage INTEGER DEFAULT 15,
            pnl_usdt REAL DEFAULT 0.0,
            pnl_pct REAL DEFAULT 0.0,
            close_type TEXT DEFAULT '',
            peak_roi_pct REAL DEFAULT 0.0,
            worst_roi_pct REAL DEFAULT 0.0,
            tp1_hit INTEGER DEFAULT 0,
            tp2_hit INTEGER DEFAULT 0,
            tp3_hit INTEGER DEFAULT 0,
            opened_at TEXT NOT NULL,
            closed_at TEXT DEFAULT ''
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """,
        f"""
        CREATE TABLE IF NOT EXISTS system_logs (
            id {pk_type},
            timestamp TEXT NOT NULL,
            level TEXT NOT NULL,
            source TEXT NOT NULL,
            message TEXT NOT NULL
        );
        """
    ]

    for q in queries:
        execute_query(q, commit=True)

    existing_setting = execute_query(
        "SELECT value FROM bot_settings WHERE key = ?", ("btc_decline_signal_enabled",), fetch_one=True
    )
    if not existing_setting:
        execute_query(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?)",
            ("btc_decline_signal_enabled", "1"),
            commit=True
        )

    existing_accts = execute_query("SELECT COUNT(*) as cnt FROM accounts", fetch_one=True)
    if existing_accts and existing_accts["cnt"] == 0:
        now_str = datetime.now(timezone.utc).isoformat()
        execute_query(
            """
            INSERT INTO accounts (
                account_id, display_name, masked_name, mode,
                spot_enabled, futures_enabled,
                spot_capital_cap, futures_capital_cap,
                spot_balance_usdt, futures_balance_usdt, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("apextick_1", "Demo_Full_250", "dem*****", "PAPER", 1, 1, 250.0, 250.0, 250.0, 250.0, now_str),
            commit=True
        )
        execute_query(
            """
            INSERT INTO accounts (
                account_id, display_name, masked_name, mode,
                spot_enabled, futures_enabled,
                spot_capital_cap, futures_capital_cap,
                spot_balance_usdt, futures_balance_usdt, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("apextick_micro", "Emmanuel_Micro_75", "emm*****", "PAPER", 0, 1, 0.0, 75.0, 0.0, 75.0, now_str),
            commit=True
        )
        add_log("INFO", "apextick_bot", "Database initialized. Seeded Demo_Full_250 ($250 Spot/$250 Futures) and Emmanuel_Micro_75 ($75 Futures Only).")


def add_log(level: str, source: str, message: str):
    """Writes a timestamped log entry."""
    now_str = datetime.now(timezone.utc).isoformat()
    try:
        execute_query(
            "INSERT INTO system_logs (timestamp, level, source, message) VALUES (?, ?, ?, ?)",
            (now_str, level, source, message),
            commit=True
        )
    except Exception:
        pass


if __name__ == "__main__":
    init_db()
    accts = execute_query("SELECT account_id, display_name, spot_enabled, futures_enabled, spot_balance_usdt, futures_balance_usdt FROM accounts", fetch_all=True)
    print(f"ApexTick Database Ready ({'PostgreSQL' if IS_POSTGRES else 'SQLite'})!")
    for a in accts:
        print(f" -> Account: {a['account_id']} ({a['display_name']}) | Spot: {'ON' if a['spot_enabled'] else 'OFF'} (${a['spot_balance_usdt']}) | Futures: {'ON' if a['futures_enabled'] else 'OFF'} (${a['futures_balance_usdt']})")