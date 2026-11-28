import os
from pathlib import Path
from dotenv import load_dotenv
import ccxt

# ==============================================================================
# APEXTICK: INSTITUTIONAL SCALPING & QUANTITATIVE ENGINE
# (Upgraded for 75/25 SMC Sniper Architecture)
# ==============================================================================

# --- ENVIRONMENT SETUP ---
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")

# --- DASHBOARD & SYSTEM CREDENTIALS ---
BOT_NAME = os.getenv("BOT_NAME", "ApexTick")
ADMIN_KEY = os.getenv("ADMIN_KEY", "secret_key_here")

# --- DATABASE CONFIGURATION ---
DATABASE_URL = os.getenv("DATABASE_URL", "")
SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", str(BASE_DIR / "apextick.db"))

# --- LIVE TRADING CREDENTIALS ---
# (PAPER_TRADING master toggle has been moved to the live SQLite 'bot_settings' table for UI God-Mode control).
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")

# --- ECONOMIC NLP API (For the Macro Kill-Switch) ---
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "") # Will be used by macro_calendar.py

# --- CCXT LIVE BINANCE CLIENT ---
exchange = ccxt.binance({
    "apiKey": BINANCE_API_KEY,
    "secret": BINANCE_SECRET_KEY,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",  # Prioritizes LIVE USDT-M Futures market
        "adjustForTimeDifference": True,
        "recvWindow": 10000,
    },
})

# --- SPOT TRADING ENGINE CONFIGURATION (The Accumulator) ---
SPOT_CONFIG = {
    "capital_cap_usdt": 150.0,       # Base capital limit
    "trade_size_pct": 12.0,          # 12% of $150 = $18.00 per entry
    "min_trade_usdt": 17.0,          # Guarantees fractional scale-outs remain valid on Binance
    "max_open_positions": 5,
    "fee_pct": 0.1,                  
    
    # NEW: 75/25 Dynamic SMC Sniper Execution
    "tp1_close_ratio": 0.75,         # Secure 75% of the bag at TP1
    "runner_close_ratio": 1.0,       # Market close the remaining 25% on trailing stop
    "risk_reward_multiplier": 3.0,   # Dynamic Target: TP1 is placed 3x the distance of the Structural SL
    "trailing_buffer_pct": 3.0,      # The 25% runner trails the absolute peak watermark by 3%
    "never_close_below_tp": True,    # Forces reliance on exact Demand Zone entries

    # --- NEW: spot risk controls ---
    "min_tp_pct": 1.2,               # TP is never closer than 1.2% (a 0.2% round-trip fee eats tiny targets)
    "max_drawdown_pct": 12.0,        # Sell a bag that is 12% under water (0 = never, the old accumulator behaviour)
    "pair_cooldown_after_loss_seconds": 14400,  # After a losing close, do not re-buy that coin for 4h
    "skip_log_interval_seconds": 300,
}

# --- USDT-M FUTURES TRADING ENGINE CONFIGURATION (The Sniper) ---
FUTURES_CONFIG = {
    "capital_cap_usdt": 150.0,       # Base capital limit
    "leverage": 15,
    "margin_size_pct": 2.0,          # (legacy) fixed-margin sizing, no longer used for entries
    "min_margin_usdt": 0.4,
    "max_open_positions": 5,
    "fee_pct": 0.05,
    "withdraw_split_pct": 0.70,

    # 75/25 Dynamic SMC Sniper Execution
    "tp1_close_ratio": 0.75,         # Secure 75% of margin at TP1. Triggers Breakeven Switch.
    "runner_close_ratio": 1.0,
    "risk_reward_multiplier": 2.0,   # TP1 = 2x the stop distance (was 3.0 - too far away, ~1 in 5 hit it)
    "trailing_buffer_pct": 1.5,      # Runner trails the peak by 1.5% (was 3.0 = ~45% of margin at 15x)

    # --- NEW: risk controls ---
    "min_stop_pct": 0.5,             # Skip trades whose stop is closer than this (noise stop-outs, pennies of profit)
    "max_stop_pct": 2.0,             # Skip trades whose stop is further than this (at 15x, 2% = 30% of margin)
    "risk_per_trade_usdt": 0.50,     # Target loss if the stop is hit (including round-trip fees & slippage)
    "max_margin_pct_per_trade": 5.0, # Never put more than 5% of balance into one trade's margin
    "pair_stop_cooldown_seconds": 7200,   # After a stop-out, don't re-enter that pair for 2h
    "pair_close_cooldown_seconds": 1800,  # After any other close, wait 30 min on that pair
    "min_volume_24h_usd": 20_000_000,     # Skip thin coins (BREV/MMT-style gaps)
    "skip_log_interval_seconds": 300,     # One "why entries were skipped" summary per 5 min
    "paper_slippage_pct": 0.02,           # Paper fills are 0.02% worse than the quote (entry and every exit)
    "live_slippage_buffer_pct": 0.04,     # Round-trip slippage buffer used when sizing LIVE trades

    # --- SELF-AWARENESS: probation circuit breaker ---
    "probation_loss_streak": 3,           # Consecutive closed losses before entering PROBATION (0 = disabled)
    "probation_risk_multiplier": 0.25,    # Risk multiplier during PROBATION ($0.50 -> $0.125)
    "probation_max_positions": 2,         # Max simultaneous open futures slots during PROBATION
    "probation_wins_to_recover": 1,       # Consecutive winning closes required to exit PROBATION
}

# --- DYNAMIC RISK MANAGEMENT (ATR SIZING) ---
DYNAMIC_RISK_CONFIG = {
    "enabled": True,
    "atr_lookback": 14,              # 14-period True Range for volatility check
    "max_margin_pct": 4.0,           # Max exposure when market is calm/predictable
    "min_margin_pct": 0.5,           # Min exposure when volatility is extremely high
}

# --- THE POWDER KEG DETECTOR (Liquidation Hunter) ---
POWDER_KEG_CONFIG = {
    "enabled": True,
    "min_oi_spot_ratio": 5.0,        # Trigger if Open Interest is 5x larger than Spot Volume
    "liquidation_trap_distance": 1.5 # Wait for price to drop 1.5% to confirm margin calls have started
}

# --- SMART MONEY CONCEPTS (SMC) MAPPING ---
SMC_CONFIG = {
    "enabled": True,
    "lookback_candles": 40,          # Rolling array size for structural mapping
    "min_fvg_pct_gap": 0.2,          # Minimum size required to register a Fair Value Gap
    "ob_mitigation_buffer": 0.1,     # Tolerance % for price hitting an Order Block
    "max_ob_lookback_candles": 10,   # Order Block must be within 10 candles of the impulse
    "zone_max_age_hours": 24,        # Zones older than this expire
    "prune_after_hours": 48,         # Dead zones are deleted after this long
}

# --- MACRO CALENDAR (The NLP Kill-Switch) ---
MACRO_CALENDAR_CONFIG = {
    "enabled": True,
    "flatten_before_minutes": 5,     # Market-sell all positions 5 minutes before high-impact news
    "resume_after_minutes": 15,      # Keep execution engines paused until 15 minutes after news
    # Used when the Finnhub calendar is unavailable (it is a paid endpoint). Format: "YYYY-MM-DD HH:MM|Name" in UTC.
    # You can also set MACRO_MANUAL_EVENTS in .env, separated by semicolons.
    "manual_events": [],
}

# --- BTC MACRO GATE SETTINGS (The 1H Trend Bias) ---
BTC_GATE_CONFIG = {
    "enabled": True,
    "min_buy_pressure_pct": 20.0,
    "max_sell_pressure_pct": 80.0,
    
    # Required by btc_decline_signal.py eval_decline_from_closes()
    "min_bars": 5,           
    "baseline_bars": 24,     
    "tail": 5,               
    "vol_mult": 1.5,         
    "red_look": 5,           
    "red_need": 3,           
    "nuclear_4h": -8.0,      
    "soft_4h": -4.0,         
    "soft_1h": -2.0,         
    
    # Required by btc_decline_signal.py _recalc_pressure()
    "window_ms": 60000       
}

# --- DYNAMIC MARKET SCANNER SETTINGS ---
MARKET_SCANNER_CONFIG = {
    "enabled": True,
    "refresh_interval_minutes": 0.5,     # 30 seconds tick rate
    "min_24h_volume_usdt": 3_000_000,    
    "top_n_coins": 50,                   
    
    # Order-Flow / Volatility Micro-Triggers
    "min_momentum_pct": 1.5,             
    "max_overextended_pct": 25.0,        
    "vwap_tolerance_pct": 100.0,         # Wide for testing, restricted by SMC engine
    "volume_spike_multiplier": 0.0,

    # Tokenized stocks / equity tokens (market hours, gaps, thin books). Add more tickers here.
    "excluded_bases": ["SPCXB", "SOXLB", "NVDAB", "QQQB", "INTCB", "KORUB", "MUB", "SNXXB"],
}
