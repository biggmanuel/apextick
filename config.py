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

# --- BINANCE DEMO TRADING CREDENTIALS ---
BINANCE_DEMO_API_KEY = os.getenv("BINANCE_DEMO_API_KEY", "")
BINANCE_DEMO_SECRET_KEY = os.getenv("BINANCE_DEMO_SECRET_KEY", "")

# --- CCXT UNIFIED BINANCE DEMO CLIENT ---
exchange = ccxt.binance({
    "apiKey": BINANCE_DEMO_API_KEY,
    "secret": BINANCE_DEMO_SECRET_KEY,
    "enableRateLimit": True,
    "options": {
        "defaultType": "spot",
        "adjustForTimeDifference": True,
        "recvWindow": 10000,
    },
})

# Configure endpoints for the unified Binance Demo / Mock Trading environment
exchange.urls["api"] = {
    "public": "https://demo-api.binance.com/api/v3",
    "private": "https://demo-api.binance.com/api/v3",
    "sapi": "https://demo-api.binance.com/sapi/v1",
    "fapiPublic": "https://testnet.binancefuture.com/fapi/v1",
    "fapiPrivate": "https://testnet.binancefuture.com/fapi/v1",
}

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
    "margin_size_pct": 10.0,         # 10% of $50 = $5.00 margin per position
    "min_margin_usdt": 3.0,
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
    "min_buy_pressure_pct": 45.0,
    "max_sell_pressure_pct": 65.0,
    
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
    "refresh_interval_minutes": 5,       # Pings Binance exchange-wide every 5m to rebuild the Hot List
    "min_24h_volume_usdt": 10_000_000,   # Institutional Liquidity Filter: Ignore dead/illiquid coins
    "top_n_coins": 50,                   # The ultimate pool size of active, high-volume pairs to track in the DB
    
    # Order-Flow / Volatility Micro-Triggers
    "min_momentum_pct": 3.0,             # Minimum daily % move to prove active market participation
    "max_overextended_pct": 25.0,        # Mean Reversion: Ignore coins already up/down > 25% to avoid tops/bottoms
    "vwap_tolerance_pct": 0.5,           # Sniper Trigger: Entry must occur within 0.5% of the VWAP / Demand Zone
    "volume_spike_multiplier": 3.0,      # Whale Wake: 1m/5m volume must be 3x the rolling average to confirm breakout/bounce
}