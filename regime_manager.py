import math
import time
import ccxt
from datetime import datetime, timezone
from config import REGIME_CONFIG
from database import execute_query, add_log
from btc_decline_signal import MARKET_STATE

# ==============================================================================
# APEXTICK: REGIME MANAGER (MACRO TREND, CHOP/SHOCK & VOLATILITY CLASSIFIER)
# ==============================================================================

def classify_btc_market_state(klines, cfg=None) -> tuple[str, float, float]:
    """
    Classifies BTC 1h candles into TREND, CHOP, or SHOCK using:
      - 4-hour absolute % move >= shock_4h_move_pct -> SHOCK
      - Kaufman Efficiency Ratio over er_lookback_candles < er_chop_threshold -> CHOP
      - Otherwise -> TREND
    Accepts either dict candles ({"c": ...}) or ccxt OHLCV rows ([t, o, h, l, c, v]).
    Returns (market_state, efficiency_ratio, pct_4h).
    """
    cfg = cfg or REGIME_CONFIG
    er_lookback = max(2, int(cfg.get("er_lookback_candles", 12)))
    er_chop = float(cfg.get("er_chop_threshold", 0.25))
    shock_4h = float(cfg.get("shock_4h_move_pct", 3.5))

    closes = []
    for k in (klines or []):
        c = k.get("c") if isinstance(k, dict) else (k[4] if len(k) > 4 else 0.0)
        try:
            c_val = float(c or 0.0)
        except Exception:
            c_val = 0.0
        if c_val > 0:
            closes.append(c_val)

    min_needed = max(er_lookback + 1, 5)
    if len(closes) < min_needed:
        return "TREND", 1.0, 0.0

    pct_4h = ((closes[-1] - closes[-5]) / closes[-5]) * 100.0
    window = closes[-(er_lookback + 1):]
    net_move = abs(window[-1] - window[0])
    path_len = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    er = (net_move / path_len) if path_len > 0 else 0.0

    if abs(pct_4h) >= shock_4h:
        state = "SHOCK"
    elif er < er_chop:
        state = "CHOP"
    else:
        state = "TREND"

    return state, round(er, 3), round(pct_4h, 2)

class RegimeManager:
    def __init__(self):
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'timeout': 15000
        })
        self.tick_interval = 300 # Run statistical calculations every 5 minutes

    def get_active_pairs(self):
        """Fetch all actively tracked pairs plus BTC/USDT so the macro regime is always maintained."""
        rows = execute_query("SELECT pair FROM trading_pairs WHERE status = 'ACTIVE'", fetch_all=True)
        pairs = [r["pair"] for r in rows] if rows else []
        if "BTC/USDT" not in pairs:
            pairs.insert(0, "BTC/USDT")
        return pairs

    def fetch_klines(self, pair, timeframe='1h', limit=30):
        """Fetch recent candlestick arrays for mathematical processing."""
        try:
            ohlcv = self.exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=limit)
            return ohlcv
        except Exception as e:
            add_log("WARNING", "regime_manager", f"Failed to fetch klines for {pair}: {e}")
            return None

    def analyze_metrics(self, ohlcv):
        """
        Translates raw OHLCV arrays into (regime, vol_state, atr).
        Calculates Bollinger Band Width (variance) and 14-period ATR / Normalized ATR.
        """
        if not ohlcv or len(ohlcv) < 20:
            return "TRENDING", "LOW", 0.0 # Fallback failsafe

        closes = [float(x[4]) for x in ohlcv[-20:]]

        # --- 1. THE VARIANCE CHECK (RANGING VS TRENDING) ---
        sma = sum(closes) / len(closes)
        if sma <= 0:
            return "TRENDING", "LOW", 0.0
        variance = sum((x - sma) ** 2 for x in closes) / len(closes)
        std_dev = math.sqrt(variance)
        bbw_pct = ((4 * std_dev) / sma) * 100.0

        bbw_threshold = float(REGIME_CONFIG.get("bbw_ranging_threshold_pct", 2.5))
        regime = "RANGING" if bbw_pct < bbw_threshold else "TRENDING"

        # --- 2. NORMALIZED ATR (VOLATILITY SIZING) ---
        trs = []
        for i in range(1, 15):
            h = float(ohlcv[-i][2])
            l = float(ohlcv[-i][3])
            prev_close = float(ohlcv[-i - 1][4])
            trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))

        atr = sum(trs) / len(trs)
        current_price = closes[-1]
        natr_pct = (atr / current_price) * 100.0 if current_price > 0 else 0.0

        if natr_pct >= 4.0:
            vol_state = "EXTREME"
        elif natr_pct >= 2.0:
            vol_state = "HIGH"
        else:
            vol_state = "LOW"

        return regime, vol_state, atr

    def analyze_regime_and_volatility(self, ohlcv):
        """Backwards-compatible 2-tuple wrapper returning (regime, vol_state)."""
        regime, vol_state, _ = self.analyze_metrics(ohlcv)
        return regime, vol_state

    def run_regime_cycle(self):
        """Executes a full sweep of the market to update structural states and persist ATR."""
        pairs = self.get_active_pairs()
        if not pairs:
            return

        updated_count = 0
        now_str = datetime.now(timezone.utc).isoformat()

        for pair in pairs:
            ohlcv = self.fetch_klines(pair, timeframe='1h', limit=30)
            if not ohlcv:
                continue

            regime, vol_state, atr = self.analyze_metrics(ohlcv)

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

            if atr > 0:
                execute_query("UPDATE trading_pairs SET atr = ? WHERE pair = ?", (atr, pair), commit=True)

            if pair == "BTC/USDT":
                m_state, er, _ = classify_btc_market_state(ohlcv)
                MARKET_STATE["btc_market_state"] = m_state
                MARKET_STATE["btc_efficiency_ratio"] = er

            updated_count += 1
            time.sleep(0.1) # Anti-rate-limit spacing

        add_log("INFO", "regime_manager", f"Regime Cycle Complete. Updated Variance & ATR for {updated_count} pairs.")

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