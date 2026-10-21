import os
from pathlib import Path
from dotenv import load_dotenv
import ccxt

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

# Configure endpoints for the unified Binance Demo / Mock Trading environment[cite: 10]
exchange.urls["api"] = {
    "public": "https://demo-api.binance.com/api/v3",
    "private": "https://demo-api.binance.com/api/v3",
    "sapi": "https://demo-api.binance.com/sapi/v1",
    "fapiPublic": "https://testnet.binancefuture.com/fapi/v1",
    "fapiPrivate": "https://testnet.binancefuture.com/fapi/v1",
}

# --- SPOT TRADING ENGINE CONFIGURATION ---
SPOT_CONFIG = {
    "capital_cap_usdt": 250.0,
    "trade_size_pct": 5.0,           # 5% of available capital per entry (~$12.50)[cite: 10]
    "min_trade_usdt": 10.0,
    "max_open_positions": 5,
    "fee_pct": 0.1,                  # Binance standard spot fee[cite: 10]
    "tp1_pct": 2.0,                  # +2.0% price gain -> scale out 30%[cite: 10]
    "tp2_pct": 4.0,                  # +4.0% price gain -> scale out 40%[cite: 10]
    "tp3_pct": 6.0,                  # +6.0% price gain -> close remaining bag[cite: 10]
    "tp1_close_ratio": 0.30,
    "tp2_close_ratio": 0.40,
    "never_close_below_tp": True,    # Bag-hold strategy on spot[cite: 10]
}

# --- USDT-M FUTURES TRADING ENGINE CONFIGURATION ---
FUTURES_CONFIG = {
    "leverage": 15,
    "margin_size_pct": 4.0,          # 4% margin per position[cite: 10]
    "min_margin_usdt": 0.85,
    "max_open_positions": 5,         # Expanded to 5 active slots 
    "fee_pct": 0.05,                 # Taker fee estimate[cite: 10]
    "withdraw_split_pct": 0.70,      # 70% of realized profits to Live Withdraw Pool[cite: 10]
    
    # Tiered Take-Profit targets (ROI on margin @ 15x leverage)[cite: 10]
    "tp1_roi": 20.0,                 # +20.0% ROI -> scale out 30%[cite: 10]
    "tp2_roi": 50.0,                 # +50.0% ROI -> scale out 40%[cite: 10]
    "tp1_close_ratio": 0.30,
    "tp2_close_ratio": 0.40,
    
    # Outlier Runner Engine (Final 30% Bag)
    "outlier_arm_roi": 80.0,         # Arms trailing runner mode once past +80% ROI 
    "outlier_trail_buffer_pct": 25.0, # Closes only if ROI pulls back 25% from peak 
    "hard_tp3_cap_roi": 400.0,       # Absolute ceiling cap if trend never pulls back 
    
    # Widened risk parameters to absorb standard market chop[cite: 10]
    "candle_sl_roi_pct": -45.0,      # Relaxed minor cut[cite: 10]
    "hard_sl_roi_pct": -75.0,        # Maximum structural cut[cite: 10]
    "recovery_arm_roi_pct": -30.0,   # Trigger recovery trailing mode[cite: 10]
    "recovery_tp_roi_pct": 5.0,      # Exit recovery trades at breakeven/small gain[cite: 10]
}

# --- BTC MACRO GATE SETTINGS ---
BTC_GATE_CONFIG = {
    "enabled": True,
    "min_buy_pressure_pct": 45.0,
    "max_sell_pressure_pct": 65.0,
    
    # Required by btc_decline_signal.py eval_decline_from_closes()[cite: 10]
    "min_bars": 5,           # Minimum klines required to evaluate[cite: 10]
    "baseline_bars": 24,     # Baseline lookback window[cite: 10]
    "tail": 5,               # Recent candles to evaluate for volume[cite: 10]
    "vol_mult": 1.5,         # Multiplier to detect volume spikes[cite: 10]
    "red_look": 5,           # Number of recent candles to inspect[cite: 10]
    "red_need": 3,           # Number of red candles needed to trigger soft decline[cite: 10]
    "nuclear_4h": -8.0,      # Trigger block if 4h drop exceeds this %[cite: 10]
    "soft_4h": -4.0,         # Soft 4h drop threshold[cite: 10]
    "soft_1h": -2.0,         # Soft 1h drop threshold[cite: 10]
    
    # Required by btc_decline_signal.py _recalc_pressure()[cite: 10]
    "window_ms": 60000       # Rolling 60-second window for buy/sell pressure[cite: 10]
}

# --- MARKET SCANNER SETTINGS ---
WATCHLIST_PAIRS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
    "ADA/USDT", "DOGE/USDT", "TRX/USDT", "DOT/USDT", "POL/USDT",
    "LTC/USDT", "BCH/USDT", "AVAX/USDT", "LINK/USDT", "XLM/USDT",
    "ATOM/USDT", "UNI/USDT", "XMR/USDT", "ETC/USDT", "FIL/USDT"
]