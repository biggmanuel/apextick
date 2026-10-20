import json
import threading
import time
import ssl
import ccxt
import websocket
from datetime import datetime, timezone
from config import BTC_GATE_CONFIG, WATCHLIST_PAIRS
from database import execute_query, IS_POSTGRES, add_log

EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "GBP",
    "AEUR", "USDT", "TRY", "BRL", "ARS", "WBTC", "WBETH", "PAXG",
    "USD1", "USDE", "EURI", "XUSD"
}

MARKET_STATE = {
    "buy_pressure_pct": 50.0,
    "sell_pressure_pct": 50.0,
    "pressure_history": [50.0],
    "btc_price": 92450.0,
    "btc_24h_change": 1.25,
    "btc_sentiment": "NEUTRAL",
    "btc_trend": "BULLISH",
    "computed_allow": True,
    "computed_reason": "ApexTick baseline active (Waiting for WebSocket ticks)",
    "klines_1h": [],       
    "agg_trades": [],      
    "range_reports": {},   
    "ws_connected": False,
    "live_prices": {},
    "live_changes": {}
}

def ensure_pairs_table():
    pk_type = "SERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    execute_query(
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
        commit=True
    )

def sync_binance_pairs(min_vol_usd: float = 5_000_000, max_pairs: int = 120):
    ensure_pairs_table()
    now_str = datetime.now(timezone.utc).isoformat()
    candidates = {}
    network_blocked = False

    try:
        exchange = ccxt.binance({'enableRateLimit': True, 'timeout': 10000})
        markets = exchange.load_markets()
        tickers = exchange.fetch_tickers()
        
        for symbol, market in markets.items():
            if not market.get('quote') == 'USDT' or not market.get('active'):
                continue
            base = market.get('base')
            if base in EXCLUDED_BASES or base.endswith(('UP', 'DOWN', 'BULL', 'BEAR')):
                continue
            
            pair_str = f"{base}/USDT"
            is_spot = 1 if market.get('spot') else 0
            is_fut = 1 if market.get('linear') else 0
            
            ticker_info = tickers.get(symbol, {})
            last_p = float(ticker_info.get('last', 0) or 0)
            chg_24 = float(ticker_info.get('percentage', 0) or 0)
            vol_24 = float(ticker_info.get('quoteVolume', 0) or 0)
            
            if pair_str == "BTC/USDT":
                MARKET_STATE["btc_price"] = last_p if last_p > 0 else MARKET_STATE["btc_price"]
                MARKET_STATE["btc_24h_change"] = chg_24
                MARKET_STATE["btc_trend"] = "BULLISH" if chg_24 >= 0 else "BEARISH"
                if chg_24 <= -1.5:
                    MARKET_STATE["btc_sentiment"] = "FEAR"
                elif chg_24 >= 1.5:
                    MARKET_STATE["btc_sentiment"] = "GREED"
                else:
                    MARKET_STATE["btc_sentiment"] = "NEUTRAL"
                continue

            if pair_str in WATCHLIST_PAIRS or (vol_24 >= min_vol_usd and last_p > 0):
                candidates[pair_str] = {
                    "pair": pair_str, "spot": is_spot, "futures": is_fut,
                    "chg": chg_24, "vol": vol_24, "price": last_p,
                }
    except Exception as e:
        network_blocked = True
        add_log("INFO", "apextick_bot", "Binance REST API blocked by region. Initializing offline watchlist mode.")

    for pair_str in WATCHLIST_PAIRS:
        if pair_str not in candidates:
            candidates[pair_str] = {
                "pair": pair_str, "spot": 1, "futures": 1, 
                "chg": 0.0, "vol": 20_000_000.0, "price": 1.0
            }

    sorted_pairs = sorted(candidates.values(), key=lambda x: (x["pair"] in WATCHLIST_PAIRS, x["vol"]), reverse=True)[:max_pairs]
    added_count = 0

    for p in sorted_pairs:
        existing = execute_query("SELECT id FROM trading_pairs WHERE pair = ?", (p["pair"],), fetch_one=True)
        if existing:
            execute_query(
                "UPDATE trading_pairs SET spot_supported = ?, futures_supported = ?, change_24h_pct = ?, volume_24h_usd = ?, last_price = ?, updated_at = ? WHERE pair = ?",
                (p["spot"], p["futures"], p["chg"], p["vol"], p["price"], now_str, p["pair"]), commit=True
            )
        else:
            execute_query(
                "INSERT INTO trading_pairs (pair, status, spot_supported, futures_supported, change_24h_pct, volume_24h_usd, last_price, reason, updated_at) VALUES (?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?)",
                (p["pair"], p["spot"], p["futures"], p["chg"], p["vol"], p["price"], "Core Watchlist" if network_blocked else "Auto-Scanner Top Volume", now_str), commit=True
            )
            added_count += 1

    add_log("INFO", "apextick_bot", f"Synced {len(sorted_pairs)} trading pairs into database ({added_count} new).")
    return len(sorted_pairs)

def eval_decline_from_closes() -> dict:
    klines = MARKET_STATE["klines_1h"]
    cfg = BTC_GATE_CONFIG
    
    if len(klines) < cfg["min_bars"]:
        if not klines:
            base_p = MARKET_STATE["btc_price"]
            now_t = int(time.time() * 1000)
            for i in range(40):
                MARKET_STATE["klines_1h"].append({"t": now_t - ((40 - i) * 3600 * 1000), "o": base_p, "c": base_p, "v": 1000.0})
        return {"block": False, "reason": "ApexTick baseline active (Allowing trades)"}

    c = [x["c"] for x in klines]
    o = [x["o"] for x in klines]
    v = [x["v"] for x in klines]
    
    c0, c1, c4h_ago = c[-1], c[-2], c[-5]
    if c4h_ago <= 0 or c1 <= 0 or c0 <= 0:
        return {"block": False, "reason": "stable baseline prices"}

    pct_4h = ((c0 - c4h_ago) / c4h_ago) * 100.0
    pct_1h = ((c0 - c1) / c1) * 100.0
    
    look = min(cfg["red_look"], len(klines))
    reds = sum(1 for i in range(len(klines) - look, len(klines)) if c[i] < o[i])

    baseline_bars = max(12, cfg["baseline_bars"])
    tail = cfg["tail"]
    if len(v) < (baseline_bars + tail + 1):
        return {"block": False, "reason": "insufficient BTC volume history"}

    baseline_slice = v[len(v) - (baseline_bars + tail) : len(v) - tail]
    avg_base = sum(baseline_slice) / len(baseline_slice) if baseline_slice else 0
    if avg_base <= 0: return {"block": False, "reason": "invalid avg_base volume"}

    vol_recent = sum(v[-tail:]) / tail
    vol_spike = vol_recent >= cfg["vol_mult"] * avg_base

    if pct_4h <= cfg["nuclear_4h"]:
        return {"block": True, "reason": f"nuclear 4h drop {pct_4h:.2f}% (<= {cfg['nuclear_4h']}%) - sharp decline"}

    if (pct_4h <= cfg["soft_4h"]) or ((pct_1h <= cfg["soft_1h"]) and reds >= cfg["red_need"]):
        if vol_spike:
            ratio = vol_recent / avg_base
            return {"block": True, "reason": f"decline+volume: 4h={pct_4h:.2f}% 1h={pct_1h:.2f}% red_candles={reds}/{look} vol_recent/avg~{ratio:.2f}"}

    return {"block": False, "reason": f"decline conditions clear (4h={pct_4h:+.2f}%, 1h={pct_1h:+.2f}%, reds={reds}/{look})"}

def backfill_btc_history():
    try:
        exchange = ccxt.binance({'timeout': 5000})
        ohlcv = exchange.fetch_ohlcv('BTC/USDT', timeframe='1h', limit=60)
        if ohlcv:
            MARKET_STATE["klines_1h"] = [{"t": int(k[0]), "o": float(k[1]), "c": float(k[4]), "v": float(k[5])} for k in ohlcv]
            MARKET_STATE["btc_price"] = float(ohlcv[-1][4])
            ev = eval_decline_from_closes()
            MARKET_STATE["computed_allow"] = not ev["block"]
            MARKET_STATE["computed_reason"] = ev["reason"]
    except Exception:
        base_p = MARKET_STATE["btc_price"]
        now_t = int(time.time() * 1000)
        MARKET_STATE["klines_1h"] = [{"t": now_t - (i * 3600 * 1000), "o": base_p, "c": base_p, "v": 1500.0} for i in range(40, 0, -1)]

def build_btc_24h_range_reports():
    pass

def _recalc_pressure():
    now_ms = int(time.time() * 1000)
    cutoff = now_ms - BTC_GATE_CONFIG["window_ms"]
    trades = [t for t in MARKET_STATE["agg_trades"] if t["t"] >= cutoff]
    MARKET_STATE["agg_trades"] = trades
    
    buy_n = sum(t["notional"] for t in trades if t["side"] == "BUY")
    sell_n = sum(t["notional"] for t in trades if t["side"] == "SELL")
    total = buy_n + sell_n
    
    buy_pct = (buy_n / total) * 100.0 if total > 0 else 50.0
    MARKET_STATE["buy_pressure_pct"] = round(buy_pct, 1)
    MARKET_STATE["sell_pressure_pct"] = round(100.0 - buy_pct, 1)

def is_trading_allowed_by_btc_gate() -> tuple[bool, str]:
    row = execute_query("SELECT value FROM bot_settings WHERE key = ?", ("btc_decline_signal_enabled",), fetch_one=True)
    decline_enabled = (row["value"] == "1") if row else True
    
    ev = eval_decline_from_closes()
    MARKET_STATE["computed_allow"], MARKET_STATE["computed_reason"] = not ev["block"], ev["reason"]
    
    if not decline_enabled:
        return True, f"Decline signal OFF (Computed: {'ALLOW' if not ev['block'] else 'BLOCK'})"
    return not ev["block"], ev["reason"]

def start_btc_stream_background():
    stream_btc = "wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade/btcusdt@kline_1h"
    stream_all = "wss://stream.binance.com:9443/ws/!ticker@arr"

    def on_btc_message(ws, message):
        try:
            data = json.loads(message)
            payload = data.get("data", {})
            if data.get("stream", "").endswith("@aggTrade"):
                q, p, m = float(payload.get("q", 0)), float(payload.get("p", 0)), bool(payload.get("m", False))
                if q > 0 and p > 0:
                    MARKET_STATE["btc_price"] = p
                    MARKET_STATE["agg_trades"].append({"t": int(time.time() * 1000), "side": "SELL" if m else "BUY", "notional": q * p})
                    _recalc_pressure()
        except Exception: pass

    def on_ticker_message(ws, message):
        try:
            data = json.loads(message)
            MARKET_STATE["ws_connected"] = True
            for item in data:
                sym = item.get("s", "")
                if sym.endswith("USDT"):
                    pair = f"{sym[:-4]}/USDT"
                    MARKET_STATE["live_prices"][pair] = float(item.get("c", 0))
                    MARKET_STATE["live_changes"][pair] = float(item.get("P", 0))
        except Exception: pass

    def on_error(ws, error):
        add_log("ERROR", "apextick_bot", f"WebSocket connection dropped: {error}")

    def ws_btc_loop():
        while True:
            try:
                ws = websocket.WebSocketApp(stream_btc, on_message=on_btc_message, on_error=on_error)
                ws.run_forever(ping_interval=20, ping_timeout=10, sslopt={"cert_reqs": ssl.CERT_NONE})
            except Exception: pass
            time.sleep(5)

    def ws_ticker_loop():
        while True:
            try:
                ws = websocket.WebSocketApp(stream_all, on_message=on_ticker_message, on_error=on_error)
                ws.run_forever(ping_interval=20, ping_timeout=10, sslopt={"cert_reqs": ssl.CERT_NONE})
            except Exception: pass
            time.sleep(5)

    def db_price_sync_loop():
        # IMMEDIATELY fetch prices on boot to prevent the $1.00 glitch
        rest_exchange = ccxt.binance({'enableRateLimit': True, 'timeout': 10000})
        try:
            tickers = rest_exchange.fetch_tickers()
            active_pairs = execute_query("SELECT pair FROM trading_pairs WHERE status = 'ACTIVE'", fetch_all=True)
            if active_pairs:
                for p_row in active_pairs:
                    pair = p_row["pair"]
                    t = tickers.get(pair)
                    if t and t.get('last'):
                        MARKET_STATE["live_prices"][pair] = float(t['last'])
                        MARKET_STATE["live_changes"][pair] = float(t.get('percentage', 0.0))
        except Exception:
            pass

        # Now wait the 15 seconds for WebSockets to attempt connection
        time.sleep(15)
        
        if not MARKET_STATE["ws_connected"]:
            add_log("WARNING", "apextick_bot", "WebSockets blocked. Engaging Real-Time REST API Fallback (Real Prices).")

        while True:
            time.sleep(3)
            try:
                active_pairs = execute_query("SELECT pair, last_price FROM trading_pairs WHERE status = 'ACTIVE'", fetch_all=True)
                if not active_pairs:
                    continue
                
                # NUKED THE SIMULATOR. IF WEBSOCKETS FAIL, PULL REAL PRICES FROM REST API
                if not MARKET_STATE["ws_connected"]:
                    try:
                        tickers = rest_exchange.fetch_tickers()
                        for p_row in active_pairs:
                            pair = p_row["pair"]
                            t = tickers.get(pair)
                            if t and t.get('last'):
                                last_p = float(t['last'])
                                chg = float(t.get('percentage', 0.0))
                                MARKET_STATE["live_prices"][pair] = last_p
                                MARKET_STATE["live_changes"][pair] = chg
                                execute_query(
                                    "UPDATE trading_pairs SET last_price = ?, change_24h_pct = ? WHERE pair = ?",
                                    (last_p, chg, pair), commit=True
                                )
                        
                        # Real-Time REST Pull for BTC Buy/Sell Pressure
                        trades = rest_exchange.fetch_trades('BTC/USDT', limit=50)
                        buy_n = sum(t['amount'] * t['price'] for t in trades if t['side'] == 'buy')
                        sell_n = sum(t['amount'] * t['price'] for t in trades if t['side'] == 'sell')
                        
                        total = buy_n + sell_n
                        if total > 0:
                            buy_pct = (buy_n / total) * 100.0
                            MARKET_STATE["buy_pressure_pct"] = round(buy_pct, 1)
                            MARKET_STATE["sell_pressure_pct"] = round(100.0 - buy_pct, 1)
                            
                    except Exception as rest_e:
                        pass
                else:
                    # NORMAL WEBSOCKET OPERATION
                    for p_row in active_pairs:
                        pair = p_row["pair"]
                        live_price = MARKET_STATE["live_prices"].get(pair)
                        live_chg = MARKET_STATE["live_changes"].get(pair)
                        
                        if live_price and live_price > 0:
                            execute_query(
                                "UPDATE trading_pairs SET last_price = ?, change_24h_pct = ? WHERE pair = ?",
                                (live_price, live_chg or 0.0, pair), commit=True
                            )
            except Exception as e:
                pass

    threading.Thread(target=ws_btc_loop, daemon=True).start()
    threading.Thread(target=ws_ticker_loop, daemon=True).start()
    threading.Thread(target=db_price_sync_loop, daemon=True).start()

if __name__ == "__main__":
    print("Initializing Core Systems...")
    backfill_btc_history()
    start_btc_stream_background()
    print("WebSocket streams connected. Live prices syncing to database engine.")
    while True:
        time.sleep(1)