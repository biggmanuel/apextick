import math
import time
import ccxt
from datetime import datetime, timezone
from database import execute_query, add_log

# ==============================================================================
# APEXTICK: REGIME MANAGER (MACRO TREND & VOLATILITY CLASSIFIER)
# ==============================================================================

class RegimeManager:
    def __init__(self):
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'timeout': 15000
        })
        self.tick_interval = 300 # Run statistical calculations every 5 minutes

    def get_active_pairs(self):
        """Fetch all actively tracked institutional pairs from the ledger."""
        rows = execute_query("SELECT pair FROM trading_pairs WHERE status = 'ACTIVE'", fetch_all=True)
        return [r["pair"] for r in rows] if rows else []

    def fetch_klines(self, pair, timeframe='1h', limit=30):
        """Fetch recent candlestick arrays for mathematical processing."""
        try:
            ohlcv = self.exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit)
            return ohlcv
        except Exception as e:
            add_log("WARNING", "regime_manager", f"Failed to fetch klines for {pair}: {e}")
            return None

    def analyze_regime_and_volatility(self, ohlcv):
        """
        Translates raw OHLCV arrays into a quantitative market state.
        Calculates Standard Deviation Variance and Normalized ATR.
        """
        if not ohlcv or len(ohlcv) < 20:
            return "TRENDING", "LOW" # Fallback failsafe

        # Extract strict numerical arrays
        closes = [x[4] for x in ohlcv[-20:]]
        
        # --- 1. THE VARIANCE CHECK (RANGING VS TRENDING) ---
        sma = sum(closes) / len(closes)
        
        # Calculate standard deviation (Sigma) of closing prices
        variance = sum((x - sma) ** 2 for x in closes) / len(closes)
        std_dev = math.sqrt(variance)
        
        # Calculate mathematical Bollinger Band Width % (BBW)
        # BBW = (Upper Band - Lower Band) / SMA
        bbw_pct = ((4 * std_dev) / sma) * 100

        # Institutional Regime Logic: 
        # If variance compresses below 2.5%, the market is locked in a tight chop channel.
        if bbw_pct < 2.5:
            regime = "RANGING"
        else:
            regime = "TRENDING"

        # --- 2. NORMALIZED ATR (VOLATILITY SIZING) ---
        trs = []
        # Calculate True Range for the last 14 periods
        for i in range(1, 15):
            h = ohlcv[-i][2]
            l = ohlcv[-i][3]
            prev_close = ohlcv[-i - 1][4]
            
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            trs.append(tr)

        atr = sum(trs) / len(trs)
        current_price = closes[-1]
        
        # Normalized ATR % (Allows us to compare volatility apples-to-apples across assets)
        natr_pct = (atr / current_price) * 100

        if natr_pct >= 4.0:
            vol_state = "EXTREME"
        elif natr_pct >= 2.0:
            vol_state = "HIGH"
        else:
            vol_state = "LOW"

        return regime, vol_state

    def run_regime_cycle(self):
        """Executes a full sweep of the market to update structural states."""
        pairs = self.get_active_pairs()
        
        if not pairs:
            return

        updated_count = 0
        now_str = datetime.now(timezone.utc).isoformat()

        for pair in pairs:
            ohlcv = self.fetch_klines(pair, timeframe='1h', limit=30)
            if not ohlcv:
                continue

            regime, vol_state = self.analyze_regime_and_volatility(ohlcv)

            # Upsert the structural state into the database for the execution engines to read
            existing = execute_query("SELECT id FROM market_regimes WHERE pair = ?", (pair,), fetch_one=True)
            if existing:
                execute_query(
                    """
                    UPDATE market_regimes 
                    SET regime = ?, volatility_state = ?, updated_at = ? 
                    WHERE pair = ?
                    """,
                    (regime, vol_state, now_str, pair),
                    commit=True
                )
            else:
                execute_query(
                    """
                    INSERT INTO market_regimes (pair, regime, volatility_state, updated_at) 
                    VALUES (?, ?, ?, ?)
                    """,
                    (pair, regime, vol_state, now_str),
                    commit=True
                )
            
            updated_count += 1
            time.sleep(0.1) # Anti-rate-limit spacing

        add_log("INFO", "regime_manager", f"Regime Cycle Complete. Updated Variance States for {updated_count} pairs.")

    def start(self):
        """Infinite loop to continuously calculate statistical drift."""
        add_log("INFO", "regime_manager", "Quantitative Regime Brain Online. Calculating Market Variance.")
        while True:
            try:
                self.run_regime_cycle()
            except Exception as e:
                add_log("ERROR", "regime_manager", f"Critical math fault in regime loop: {e}")
            time.sleep(self.tick_interval)

if __name__ == "__main__":
    manager = RegimeManager()
    manager.start()