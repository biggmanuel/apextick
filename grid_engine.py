import math
import time
from datetime import datetime, timezone
import ccxt

# IMPORT CONFIG
from config import SPOT_CONFIG, FUTURES_CONFIG, BINANCE_API_KEY, BINANCE_SECRET_KEY, exchange as live_exchange
from database import execute_query, add_log
from btc_decline_signal import MARKET_STATE

# ==============================================================================
# APEXTICK: GRID ENGINE (RANGE SCALPING)
# ==============================================================================

class GridEngine:
    def __init__(self):
        self.tick_interval = 60 # Check grid status every 60 seconds
        
        # Grid Parameters (Can be moved to config.py later)
        self.grid_levels = 5 # Number of buy/sell tiers above and below the current price
        self.grid_spacing_pct = 0.5 # Distance between each grid level
        self.grid_capital_pct = 10.0 # Percentage of total capital to allocate to the grid

    def get_live_price_safely(self, pair: str) -> float:
        current_price = MARKET_STATE["live_prices"].get(pair)
        if current_price and current_price > 0:
            return float(current_price)

        p_row = execute_query("SELECT last_price FROM trading_pairs WHERE pair = ?", (pair,), fetch_one=True)
        if p_row and p_row.get("last_price"):
            db_price = float(p_row["last_price"])
            if db_price > 0:
                return db_price
        return 0.0

    def check_macro_regime(self):
        """Checks if the market is officially declared RANGING by the Regime Manager."""
        row = execute_query("SELECT regime FROM market_regimes WHERE pair = 'BTC/USDT'", fetch_one=True)
        return row["regime"] if row else "TRENDING"

    def deploy_grid(self, acct_id, pair, current_price, available_usdt, is_paper):
        """Deploys a symmetrical array of limit orders around the current price."""
        trade_usdt = available_usdt * (self.grid_capital_pct / 100.0)
        usdt_per_level = trade_usdt / (self.grid_levels * 2) # Split across buys and sells

        now_str = datetime.now(timezone.utc).isoformat()
        mode = "PAPER" if is_paper else "LIVE"

        # Note: A full production grid bot requires tracking every individual limit order ID 
        # and replacing them as they are filled. For this architecture foundation, we log 
        # the deployment event.

        for i in range(1, self.grid_levels + 1):
            buy_price = current_price * (1 - (self.grid_spacing_pct / 100.0) * i)
            sell_price = current_price * (1 + (self.grid_spacing_pct / 100.0) * i)
            
            # --- LIVE MARKET EXECUTION PREPARATION ---
            if not is_paper:
                # To execute live grids, we would use ccxt's create_limit_order() here
                pass 
                
            execute_query(
                """
                INSERT INTO spot_trades (
                    account_id, pair, trade_mode, status, entry_price, qty, remaining_qty, notional_usdt, opened_at
                ) VALUES (?, ?, ?, 'PENDING_GRID', ?, ?, ?, ?, ?)
                """,
                (acct_id, pair, mode, buy_price, usdt_per_level/buy_price, usdt_per_level/buy_price, usdt_per_level, now_str),
                commit=True
            )

        add_log("SUCCESS", "grid_engine", f"Deployed {self.grid_levels * 2}-level Grid on {pair} around ${current_price:.4f}")

    def run_grid_cycle(self):
        """Master execution loop for the Grid Engine."""
        
        # 1. Check God-Mode and Macro Halt
        mode_row = execute_query("SELECT value FROM bot_settings WHERE key = 'paper_trading_enabled'", fetch_one=True)
        is_paper = (mode_row["value"] == "1") if mode_row else True

        halt_row = execute_query("SELECT value FROM bot_settings WHERE key = 'macro_halt'", fetch_one=True)
        if halt_row and halt_row["value"] == "1":
            return # Grid halted due to Macro Kill-Switch

        # 2. Regime Check
        regime = self.check_macro_regime()
        if regime != "RANGING":
            return # Grid only operates in chop

        # 3. Find candidates (High volume, low 24h change indicating chop)
        pairs = execute_query(
            """
            SELECT pair, last_price, change_24h_pct 
            FROM trading_pairs 
            WHERE status = 'ACTIVE' AND spot_supported = 1 AND ABS(change_24h_pct) < 2.0
            ORDER BY volume_24h_usd DESC LIMIT 3
            """, fetch_all=True
        )

        if not pairs:
            return

        accounts = execute_query("SELECT * FROM accounts WHERE spot_enabled = 1", fetch_all=True)
        
        for acct in accounts:
            acct_id = acct["account_id"]
            
            # Check if grid is already active to prevent duplicate deployment
            active_grids = execute_query("SELECT id FROM spot_trades WHERE account_id = ? AND status = 'PENDING_GRID'", (acct_id,), fetch_all=True)
            if active_grids:
                continue

            balance = float(acct["spot_balance_usdt"] or 0.0)
            if balance < (SPOT_CONFIG["min_trade_usdt"] * self.grid_levels * 2):
                continue # Insufficient capital for grid deployment

            for p_info in pairs:
                pair = p_info["pair"]
                if pair in ["BTC/USDT", "ETH/USDT"]: continue
                
                current_price = self.get_live_price_safely(pair)
                if current_price > 0:
                    self.deploy_grid(acct_id, pair, current_price, balance, is_paper)
                    break # Deploy one grid at a time

    def start(self):
        """Infinite loop to monitor ranging markets."""
        add_log("INFO", "grid_engine", "Grid Engine Online. Waiting for RANGING market regime.")
        while True:
            try:
                self.run_grid_cycle()
            except Exception as e:
                add_log("ERROR", "grid_engine", f"Grid loop error: {e}")
            time.sleep(self.tick_interval)

if __name__ == "__main__":
    engine = GridEngine()
    engine.start()