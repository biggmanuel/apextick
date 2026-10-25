import os
from pathlib import Path
from dotenv import load_dotenv
import ccxt

# ==============================================================================
# APEXTICK: INSTITUTIONAL SCALPING & ACCUMULATION ENGINE
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

# --- SYSTEM EXECUTION MODE (THE MASTER TOGGLE) ---
# True = Scans LIVE market, logs trades to local SQLite database using fake portfolio.
# False = Sends live orders to Binance using real capital and API keys.
PAPER_TRADING = True 

# --- LIVE TRADING CREDENTIALS ---
# Load your real Binance API keys here. 
# (When PAPER_TRADING=True, these are only used to fetch live unrestricted market data).
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")

# --- CCXT LIVE BINANCE CLIENT ---
exchange = ccxt.binance({
    "apiKey": BINANCE_API_KEY,
    "secret": BINANCE_SECRET_KEY,
    "enableRateLimit": True,
    "options": {
        "defaultType": "swap",  # Prioritizes LIVE USDT-M Futures market (swap) over spot
        "adjustForTimeDifference": True,
        "recvWindow": 10000,
    },
})
# NOTE: The hardcoded exchange.urls block for Testnet/Demo has been COMPLETELY REMOVED.
# This forces the CCXT client to pull data from the unrestricted live Binance Mainnet.

# --- SPOT TRADING ENGINE CONFIGURATION (The Accumulator) ---
SPOT_CONFIG = {
    "capital_cap_usdt": 100.0,
    "trade_size_pct": 18.0,          # 18% of $100 = $18.00 per entry
    "min_trade_usdt": 17.0,          # Guarantees 30% TP1 scale-out >= $5.10
    "max_open_positions": 5,
    "fee_pct": 0.1,                  
    "tp1_pct": 2.0,                  
    "tp2_pct": 4.0,                  
    "tp3_pct": 6.0,                  
    "tp1_close_ratio": 0.30,
    "tp2_close_ratio": 0.40,
    "never_close_below_tp": True,    # Forces reliance on exact Demand Zone/VWAP entries
}

# --- USDT-M FUTURES TRADING ENGINE CONFIGURATION (The Sniper) ---
FUTURES_CONFIG = {
    "leverage": 15,
    "margin_size_pct": 2.0,          # 2% of $50 = $1 margin per position
    "min_margin_usdt": 0.4,          # Lowered so your $1.00 margin isn't blocked internally
    "max_open_positions": 5,         
    "fee_pct": 0.05,                 
    "withdraw_split_pct": 0.70,      
    
    # Tiered Scale-Outs
    "tp1_roi": 20.0,                 
    "tp2_roi": 50.0,                 
    "tp1_close_ratio": 0.30,
    "tp2_close_ratio": 0.40,

    # Outlier Runner Engine
    "outlier_arm_roi": 80.0,         
    "outlier_trail_buffer_pct": 25.0, 
    "hard_tp3_cap_roi": 400.0,       
    
    # Strict Micro-Risk Limits
    "candle_sl_roi_pct": -5.0,       
    "hard_sl_roi_pct": -15.0,        
    "recovery_arm_roi_pct": -8.0,    
    "recovery_tp_roi_pct": 0.50,     
}

# --- BTC MACRO GATE SETTINGS (The 1H Trend Bias) ---
BTC_GATE_CONFIG = {
    "enabled": True,
    "min_buy_pressure_pct": 20.0,
    "max_sell_pressure_pct": 80.0,
    
    # Required by btc_decline_signal.py eval_decline_from_closes()
    "min_bars": 5,           # Minimum klines required to evaluate
    "baseline_bars": 24,     # Baseline lookback window
    "tail": 5,               # Recent candles to evaluate for volume
    "vol_mult": 1.5,         # Multiplier to detect volume spikes
    "red_look": 5,           # Number of recent candles to inspect
    "red_need": 3,           # Number of red candles needed to trigger soft decline
    "nuclear_4h": -8.0,      # Trigger block if 4h drop exceeds this %
    "soft_4h": -4.0,         # Soft 4h drop threshold
    "soft_1h": -2.0,         # Soft 1h drop threshold
    
    # Required by btc_decline_signal.py _recalc_pressure()
    "window_ms": 60000       # Rolling 60-second window for buy/sell pressure
}

# --- DYNAMIC MARKET SCANNER SETTINGS (Replaces Hard-Coded Watchlist) ---
MARKET_SCANNER_CONFIG = {
    "enabled": True,
    "refresh_interval_minutes": 0.5,     # 0.5 minutes = 30 seconds tick rate
    "min_24h_volume_usdt": 3_000_000,    # Lowered to $3M to catch a wider pool of volatile altcoins
    "top_n_coins": 50,                   # The ultimate pool size of active, high-volume pairs to track in the DB
    
    # Order-Flow / Volatility Micro-Triggers
    "min_momentum_pct": 1.5,             # Lowered from 3.0% to catch standard intraday moves
    "max_overextended_pct": 25.0,        # Mean Reversion: Ignore coins already up/down > 25% to avoid tops/bottoms
    "vwap_tolerance_pct": 1.5,           # Widened from 0.5% to allow slightly looser entries around the VWAP
    "volume_spike_multiplier": 1.0,      # FIXED: Brought down to 1.0 from 10 to ensure it fires on normal breakouts
}