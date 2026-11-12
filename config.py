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
}

# --- USDT-M FUTURES TRADING ENGINE CONFIGURATION (The Sniper) ---
FUTURES_CONFIG = {
    "capital_cap_usdt": 150.0,       # Base capital limit
    "leverage": 15,
    "margin_size_pct": 2.0,          # 2% of $150 = $3.00 margin per position base (modified by ATR)
    "min_margin_usdt": 0.4,          
    "max_open_positions": 5,         
    "fee_pct": 0.05,                 
    "withdraw_split_pct": 0.70,      
    
    # NEW: 75/25 Dynamic SMC Sniper Execution
    "tp1_close_ratio": 0.75,         # Secure 75% of margin at TP1. Triggers Breakeven Switch.
    "runner_close_ratio": 1.0,       # Market close the remaining 25% on trailing stop
    "risk_reward_multiplier": 3.0,   # Dynamic Target: TP1 is placed 3x the distance of the Structural SL
    "trailing_buffer_pct": 3.0,      # The 25% runner trails the absolute peak watermark by 3%
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
}

# --- MACRO CALENDAR (The NLP Kill-Switch) ---
MACRO_CALENDAR_CONFIG = {
    "enabled": True,
    "flatten_before_minutes": 5,     # Market-sell all positions 5 minutes before high-impact news
    "resume_after_minutes": 15,      # Keep execution engines paused until 15 minutes after news
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
}
