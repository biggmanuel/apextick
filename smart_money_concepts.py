import time
import ccxt
from datetime import datetime, timezone
from database import execute_query, add_log
from config import SMC_CONFIG

# ==============================================================================
# APEXTICK: SMART MONEY CONCEPTS (SMC) & LIQUIDITY MAPPER
# ==============================================================================

class SmartMoneyMapper:
    def __init__(self):
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'timeout': 15000
        })
        self.tick_interval = 60 # Run the structural mapper every 60 seconds
        self.lookback = SMC_CONFIG.get("lookback_candles", 40)
        self.min_gap = SMC_CONFIG.get("min_fvg_pct_gap", 0.2) / 100.0

    def get_active_pairs(self):
        """Fetch all institutional pairs from the ledger."""
        rows = execute_query("SELECT pair FROM trading_pairs WHERE status = 'ACTIVE'", fetch_all=True)
        return [r["pair"] for r in rows] if rows else []

    def fetch_klines(self, pair, timeframe='15m'):
        """Fetch recent candlestick arrays. 15m is optimal for intraday structural voids."""
        try:
            ohlcv = self.exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=self.lookback + 5)
            return ohlcv
        except Exception as e:
            add_log("WARNING", "smc_mapper", f"Failed to fetch klines for {pair}: {e}")
            return None

    def map_structure(self, pair, ohlcv):
        """
        Mathematical extraction of Fair Value Gaps (FVG) and Order Blocks (OB).
        OHLCV Index: 0=Time, 1=Open, 2=High, 3=Low, 4=Close, 5=Volume
        """
        if not ohlcv or len(ohlcv) < 3:
            return

        now_str = datetime.now(timezone.utc).isoformat()
        discovered_pools = []

        # Iterate through the array looking for 3-candle SMC structures
        for i in range(2, len(ohlcv) - 1):
            c1_high, c1_low = ohlcv[i-2][2], ohlcv[i-2][3]
            c2_open, c2_close = ohlcv[i-1][1], ohlcv[i-1][4]
            c3_high, c3_low = ohlcv[i][2], ohlcv[i][3]

            # --- BULLISH STRUCTURE (Price expanding upwards) ---
            if c1_high < c3_low and c2_close > c2_open:
                gap_size = (c3_low - c1_high) / c1_high
                if gap_size >= self.min_gap:
                    # 1. Log the Bullish FVG
                    discovered_pools.append({
                        "type": "FVG", "direction": "BULLISH",
                        "top": c3_low, "bottom": c1_high
                    })
                    
                    # 2. Traceback to find the Bullish Order Block (Last Red Candle)
                    for j in range(i-2, -1, -1):
                        if ohlcv[j][4] < ohlcv[j][1]: # Is Bearish (Red)
                            ob_high = ohlcv[j][2]
                            ob_low = ohlcv[j][3]
                            discovered_pools.append({
                                "type": "ORDER_BLOCK", "direction": "BULLISH",
                                "top": ob_high, "bottom": ob_low
                            })
                            break

            # --- BEARISH STRUCTURE (Price expanding downwards) ---
            elif c1_low > c3_high and c2_close < c2_open:
                gap_size = (c1_low - c3_high) / c3_high
                if gap_size >= self.min_gap:
                    # 1. Log the Bearish FVG
                    discovered_pools.append({
                        "type": "FVG", "direction": "BEARISH",
                        "top": c1_low, "bottom": c3_high
                    })
                    
                    # 2. Traceback to find the Bearish Order Block (Last Green Candle)
                    for j in range(i-2, -1, -1):
                        if ohlcv[j][4] > ohlcv[j][1]: # Is Bullish (Green)
                            ob_high = ohlcv[j][2]
                            ob_low = ohlcv[j][3]
                            discovered_pools.append({
                                "type": "ORDER_BLOCK", "direction": "BEARISH",
                                "top": ob_high, "bottom": ob_low
                            })
                            break

        self.save_pools_to_db(pair, discovered_pools, now_str)

    def save_pools_to_db(self, pair, pools, now_str):
        """Saves unique institutional pools into the SQLite ledger."""
        if not pools:
            return

        for pool in pools:
            # Check if a pending pool of the same type/direction already exists at this exact level (+/- 0.5% tolerance)
            tolerance = pool["top"] * 0.005
            existing = execute_query(
                """
                SELECT id FROM liquidity_pools 
                WHERE pair = ? AND pool_type = ? AND direction = ? AND status = 'PENDING'
                AND ABS(top_price - ?) < ?
                """,
                (pair, pool["type"], pool["direction"], pool["top"], tolerance),
                fetch_one=True
            )
            
            if not existing:
                execute_query(
                    """
                    INSERT INTO liquidity_pools 
                    (pair, pool_type, direction, top_price, bottom_price, status, created_at, updated_at) 
                    VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)
                    """,
                    (pair, pool["type"], pool["direction"], pool["top"], pool["bottom"], now_str, now_str),
                    commit=True
                )

    def manage_mitigation(self):
        """
        Tracks live prices against all PENDING liquidity pools. 
        Updates database states to MITIGATED or INVALIDATED.
        """
        now_str = datetime.now(timezone.utc).isoformat()
        pending_pools = execute_query("SELECT * FROM liquidity_pools WHERE status = 'PENDING'", fetch_all=True)
        
        if not pending_pools:
            return

        # Fetch current live prices from the active pairs table
        prices_db = execute_query("SELECT pair, last_price FROM trading_pairs", fetch_all=True)
        live_prices = {row["pair"]: row["last_price"] for row in prices_db} if prices_db else {}

        mitigated_count = 0
        invalidated_count = 0

        for pool in pending_pools:
            pair = pool["pair"]
            price = live_prices.get(pair)
            
            if not price or price <= 0:
                continue

            top = pool["top_price"]
            bottom = pool["bottom_price"]
            direction = pool["direction"]

            # Bullish Pools (Support Zones)
            if direction == "BULLISH":
                if price < bottom:
                    # Price completely smashed through the bottom of the support block
                    execute_query("UPDATE liquidity_pools SET status = 'INVALIDATED', updated_at = ? WHERE id = ?", (now_str, pool["id"]), commit=True)
                    invalidated_count += 1
                elif bottom <= price <= top:
                    # Price tapped perfectly inside the zone (Mitigation)
                    execute_query("UPDATE liquidity_pools SET status = 'MITIGATED', updated_at = ? WHERE id = ?", (now_str, pool["id"]), commit=True)
                    mitigated_count += 1
            
            # Bearish Pools (Resistance Zones)
            elif direction == "BEARISH":
                if price > top:
                    # Price smashed entirely through the top of the resistance block
                    execute_query("UPDATE liquidity_pools SET status = 'INVALIDATED', updated_at = ? WHERE id = ?", (now_str, pool["id"]), commit=True)
                    invalidated_count += 1
                elif bottom <= price <= top:
                    # Price tapped inside the zone (Mitigation)
                    execute_query("UPDATE liquidity_pools SET status = 'MITIGATED', updated_at = ? WHERE id = ?", (now_str, pool["id"]), commit=True)
                    mitigated_count += 1

        if mitigated_count > 0 or invalidated_count > 0:
            add_log("INFO", "smc_mapper", f"Mitigation Engine: {mitigated_count} zones MITIGATED (Hit), {invalidated_count} zones INVALIDATED (Broken).")

    def run_smc_cycle(self):
        """Master execution loop for the SMC Mapper."""
        pairs = self.get_active_pairs()
        if not pairs:
            return

        for pair in pairs:
            ohlcv = self.fetch_klines(pair)
            if ohlcv:
                self.map_structure(pair, ohlcv)
            time.sleep(0.1) # Anti-rate limit 
            
        self.manage_mitigation()

    def start(self):
        """Infinite background loop."""
        add_log("INFO", "smc_mapper", "Smart Money Concepts (SMC) Architecture Online. Mapping Liquidity Voids.")
        while True:
            try:
                self.run_smc_cycle()
            except Exception as e:
                add_log("ERROR", "smc_mapper", f"Critical fault in SMC mapping loop: {e}")
            time.sleep(self.tick_interval)

if __name__ == "__main__":
    mapper = SmartMoneyMapper()
    mapper.start()