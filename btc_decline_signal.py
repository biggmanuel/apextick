import collections
import json
import os
import ssl
import threading
import time
import ccxt
import websocket
from datetime import datetime, timezone

# IMPORT THE NEW DYNAMIC CONFIG
from config import BTC_GATE_CONFIG, MARKET_SCANNER_CONFIG
from database import execute_query, execute_batch, IS_POSTGRES, add_log, prune_logs

# ==============================================================================
# APEXTICK: DATA CORE (prices, BTC gate, pair scanner)
# ------------------------------------------------------------------------------
# Fixes in this version:
#   * Price freshness: MARKET_STATE["prices_ts"] is stamped on every websocket message / REST
#     refresh, and get_fresh_price() returns 0.0 when prices are stale (engines must not trade
#     on old prices).
#   * ws_connected is cleared on close/error and by a watchdog (no ticker message for 15s), so the
#     REST fallback actually takes over when the stream drops.
#   * Certificate checking is ON for the websockets (set WS_INSECURE_SSL=1 only if you must).
#   * BTC buy/sell pressure uses a rolling deque with running sums (O(1) per trade, not a rescan).
#   * The BTC 1h kline stream is applied live; REST backfill is only a periodic safety refresh.
#   * The BTC gate FAILS CLOSED: no/old/short BTC data means "BLOCK", not fake flat candles at a
#     hardcoded price.
#   * Pair sync is one atomic transaction (the ACTIVE list never goes empty), pairs you hold stay
#     ACTIVE, ccxt clients/markets are reused, OI is cached, and tokenized stocks are excluded.
#   * DB price writes are batched into one transaction.
# ==============================================================================

EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "GBP",
    "AEUR", "USDT", "TRY", "BRL", "ARS", "WBTC", "WBETH", "PAXG",
    "USD1", "USDE", "EURI", "XUSD"
} | set(MARKET_SCANNER_CONFIG.get("excluded_bases", []))

LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

PRICE_MAX_AGE_SECONDS = 20        # prices older than this are "stale"
TICKER_SILENCE_SECONDS = 15       # no ticker message for this long = stream is dead
KLINE_MAX_AGE_SECONDS = 2.5 * 3600  # BTC 1h data whose newest candle opened longer ago than this is stale
PRESSURE_MAX_AGE_SECONDS = 120    # BTC trade-pressure older than this is ignored
OI_REFRESH_SECONDS = 150          # open interest changes slowly; do not re-fetch it every scan

MARKET_STATE = {
    "buy_pressure_pct": 50.0,
    "sell_pressure_pct": 50.0,
    "pressure_history": [50.0],
    "btc_price": 0.0,
    "btc_24h_change": 0.0,
    "btc_sentiment": "NEUTRAL",
    "btc_trend": "NEUTRAL",
    "btc_atr_1h": 0.0,
    "computed_allow": False,
    "computed_reason": "Waiting for BTC data (fail-closed)",
    "klines_1h": [],
    "range_reports": {},
    "ws_connected": False,
    "live_prices": {},
    "live_changes": {},
    # --- freshness stamps (epoch seconds) ---
    "prices_ts": 0.0,      # last time ANY price source (websocket or REST) delivered prices
    "ticker_ts": 0.0,      # last websocket ticker message
    "pressure_ts": 0.0,    # last time BTC trade pressure was updated
    "klines_ts": 0.0,      # last time BTC klines were updated
}

# =============================================================================
# FRESHNESS HELPERS (used by the engines)
# =============================================================================

def prices_are_fresh(max_age: float = PRICE_MAX_AGE_SECONDS) -> bool:
    return (time.time() - MARKET_STATE["prices_ts"]) <= max_age

def get_fresh_price(pair: str, max_age: float = PRICE_MAX_AGE_SECONDS) -> float:
    """Latest streamed/REST price, or 0.0 if the price feed itself is stale."""
    if not prices_are_fresh(max_age):
        return 0.0
    p = MARKET_STATE["live_prices"].get(pair)
    return float(p) if p and p > 0 else 0.0

_last_log = {}
def _log_throttled(key: str, level: str, source: str, message: str, every: float = 60.0):
    now = time.time()
    if now - _last_log.get(key, 0.0) >= every:
        _last_log[key] = now
        add_log(level, source, message)

# =============================================================================
# BTC TRADE PRESSURE (rolling window, O(1) per trade)
# =============================================================================

_pressure_lock = threading.Lock()
_trades = collections.deque()      # (t_ms, side, notional)
_sums = {"BUY": 0.0, "SELL": 0.0}

def _publish_pressure():
    total = _sums["BUY"] + _sums["SELL"]
    buy_pct = (_sums["BUY"] / total) * 100.0 if total > 0 else 50.0
    MARKET_STATE["buy_pressure_pct"] = round(buy_pct, 1)
    MARKET_STATE["sell_pressure_pct"] = round(100.0 - buy_pct, 1)
    MARKET_STATE["pressure_ts"] = time.time()

def _expire_trades(now_ms: int):
    cutoff = now_ms - BTC_GATE_CONFIG["window_ms"]
    while _trades and _trades[0][0] < cutoff:
        _, side, notional = _trades.popleft()
        _sums[side] -= notional
    if not _trades:
        _sums["BUY"] = _sums["SELL"] = 0.0   # kill floating-point drift

def _add_trade(t_ms: int, side: str, notional: float):
    with _pressure_lock:
        _trades.append((t_ms, side, notional))
        _sums[side] += notional
        _expire_trades(t_ms)
        _publish_pressure()

def _recalc_pressure():
    """Re-publishes pressure after expiring old trades (kept for backwards compatibility)."""
    with _pressure_lock:
        _expire_trades(int(time.time() * 1000))
        _publish_pressure()

def _reset_pressure():
    with _pressure_lock:
        _trades.clear()
        _sums["BUY"] = _sums["SELL"] = 0.0

# =============================================================================
# BTC KLINES
# =============================================================================

def _apply_kline(k: dict):
    """Applies one live 1h kline update from the websocket (replace the forming candle or append a new one)."""
    row = {"t": int(k["t"]), "o": float(k["o"]), "h": float(k["h"]), "l": float(k["l"]), "c": float(k["c"]), "v": float(k["v"])}
    kl = MARKET_STATE["klines_1h"]
    if kl and kl[-1]["t"] == row["t"]:
        kl[-1] = row
    elif not kl or row["t"] > kl[-1]["t"]:
        kl.append(row)
        del kl[:-60]
    MARKET_STATE["klines_ts"] = time.time()
    if row["c"] > 0:
        MARKET_STATE["btc_price"] = row["c"]

_btc_rest = None
def backfill_btc_history() -> bool:
    """REST refresh of BTC 1h candles. Never fabricates data: on failure the old data is kept (and goes stale)."""
    global _btc_rest
    try:
        if _btc_rest is None:
            _btc_rest = ccxt.binance({'enableRateLimit': True, 'timeout': 8000})
        ohlcv = _btc_rest.fetch_ohlcv('BTC/USDT', timeframe='1h', limit=60)
        if not ohlcv:
            return False
        MARKET_STATE["klines_1h"] = [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in ohlcv]
        MARKET_STATE["btc_price"] = float(ohlcv[-1][4])
        MARKET_STATE["klines_ts"] = time.time()
        ev = eval_decline_from_closes()
        MARKET_STATE["computed_allow"] = not ev["block"]
        MARKET_STATE["computed_reason"] = ev["reason"]
        return True
    except Exception as e:
        _log_throttled("btc_backfill", "WARNING", "apextick_bot", f"BTC history refresh failed (keeping existing data): {e}", every=300)
        return False

def eval_decline_from_closes() -> dict:
    klines = MARKET_STATE["klines_1h"]
    cfg = BTC_GATE_CONFIG

    # --- FAIL CLOSED: no usable BTC data means we do not open new trades ---
    if not klines:
        return {"block": True, "reason": "BTC data unavailable - entries blocked (fail-closed)"}
    age_s = time.time() - (klines[-1]["t"] / 1000.0)
    if age_s > KLINE_MAX_AGE_SECONDS:
        return {"block": True, "reason": f"BTC 1h data is stale ({age_s/3600:.1f}h old) - entries blocked (fail-closed)"}
    if len(klines) < cfg["min_bars"]:
        return {"block": True, "reason": f"insufficient BTC history ({len(klines)} bars) - entries blocked (fail-closed)"}

    # --- ATR VOLATILITY CALCULATOR ---
    if len(klines) >= 15:
        tr_list = []
        for i in range(1, len(klines)):
            h = klines[i]["h"]
            l = klines[i]["l"]
            prev_c = klines[i-1]["c"]
            tr_list.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
        recent_trs = tr_list[-14:]
        MARKET_STATE["btc_atr_1h"] = sum(recent_trs) / len(recent_trs)

    c = [x["c"] for x in klines]
    o = [x["o"] for x in klines]
    v = [x["v"] for x in klines]

    c0, c1, c4h_ago = c[-1], c[-2], c[-5]
    if c4h_ago <= 0 or c1 <= 0 or c0 <= 0:
        return {"block": True, "reason": "invalid BTC prices - entries blocked (fail-closed)"}

    pct_4h = ((c0 - c4h_ago) / c4h_ago) * 100.0
    pct_1h = ((c0 - c1) / c1) * 100.0

    look = min(cfg["red_look"], len(klines))
    reds = sum(1 for i in range(len(klines) - look, len(klines)) if c[i] < o[i])

    baseline_bars = max(12, cfg["baseline_bars"])
    tail = cfg["tail"]
    if len(v) < (baseline_bars + tail + 1):
        return {"block": True, "reason": "insufficient BTC volume history - entries blocked (fail-closed)"}

    baseline_slice = v[len(v) - (baseline_bars + tail) : len(v) - tail]
    avg_base = sum(baseline_slice) / len(baseline_slice) if baseline_slice else 0
    if avg_base <= 0:
        return {"block": True, "reason": "invalid BTC baseline volume - entries blocked (fail-closed)"}

    vol_recent = sum(v[-tail:]) / tail
    vol_spike = vol_recent >= cfg["vol_mult"] * avg_base

    if pct_4h <= cfg["nuclear_4h"]:
        return {"block": True, "reason": f"nuclear 4h drop {pct_4h:.2f}% (<= {cfg['nuclear_4h']}%) - sharp decline"}

    if (pct_4h <= cfg["soft_4h"]) or ((pct_1h <= cfg["soft_1h"]) and reds >= cfg["red_need"]):
        if vol_spike:
            ratio = vol_recent / avg_base
            return {"block": True, "reason": f"decline+volume: 4h={pct_4h:.2f}% 1h={pct_1h:.2f}% red_candles={reds}/{look} vol_recent/avg~{ratio:.2f}"}

    return {"block": False, "reason": f"decline conditions clear (4h={pct_4h:+.2f}%, 1h={pct_1h:+.2f}%, reds={reds}/{look})"}

def is_trading_allowed_by_btc_gate() -> tuple[bool, str]:
    if not BTC_GATE_CONFIG.get("enabled", True):
        return True, "BTC gate disabled in config"

    row = execute_query("SELECT value FROM bot_settings WHERE key = ?", ("btc_decline_signal_enabled",), fetch_one=True)
    decline_enabled = (str(row["value"]) == "1") if row else True

    ev = eval_decline_from_closes()
    MARKET_STATE["computed_allow"], MARKET_STATE["computed_reason"] = not ev["block"], ev["reason"]

    if not decline_enabled:
        return True, f"Decline signal OFF (Computed: {'ALLOW' if not ev['block'] else 'BLOCK'})"

    # Trade-pressure checks only count while the pressure data is fresh
    if (time.time() - MARKET_STATE["pressure_ts"]) <= PRESSURE_MAX_AGE_SECONDS:
        if MARKET_STATE["buy_pressure_pct"] < BTC_GATE_CONFIG["min_buy_pressure_pct"]:
            return False, f"Buy pressure ({MARKET_STATE['buy_pressure_pct']}%) below minimum ({BTC_GATE_CONFIG['min_buy_pressure_pct']}%)"
        if MARKET_STATE["sell_pressure_pct"] > BTC_GATE_CONFIG["max_sell_pressure_pct"]:
            return False, f"Sell pressure ({MARKET_STATE['sell_pressure_pct']}%) exceeds maximum ({BTC_GATE_CONFIG['max_sell_pressure_pct']}%)"

    return not ev["block"], ev["reason"]

# =============================================================================
# PAIR SCANNER
# =============================================================================

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

    new_columns = {"oi_usd": "REAL DEFAULT 0.0", "oi_spot_ratio": "REAL DEFAULT 0.0", "atr": "REAL DEFAULT 0.0"}
    for col, col_type in new_columns.items():
        try:
            execute_query(f"ALTER TABLE trading_pairs ADD COLUMN {col} {col_type}", commit=True)
        except Exception:
            pass  # Column already exists, safe to ignore

_spot_client = None
_fut_client = None
_markets = None
_fut_markets = None
_markets_ts = 0.0
_oi_cache = {}   # pair -> (oi_usd, fetched_at)

def _get_scan_clients():
    """Re-uses the ccxt clients and only reloads the (heavy) market lists hourly."""
    global _spot_client, _fut_client, _markets, _fut_markets, _markets_ts
    if _spot_client is None:
        _spot_client = ccxt.binance({'enableRateLimit': True, 'timeout': 10000})
        _fut_client = ccxt.binance({'options': {'defaultType': 'swap'}, 'enableRateLimit': True, 'timeout': 10000})
    if _markets is None or (time.time() - _markets_ts) > 3600:
        _markets = _spot_client.load_markets(True)
        _fut_markets = _fut_client.load_markets(True)
        _markets_ts = time.time()
    return _spot_client, _fut_client, _markets, _fut_markets

def _is_leveraged_token(base: str, all_bases: set) -> bool:
    """BTCUP/BTCDOWN style tokens only: the stripped name must itself be a listed coin (so JUP, SYRUP... stay)."""
    for suffix in LEVERAGED_SUFFIXES:
        if base.endswith(suffix) and base[: -len(suffix)] in all_bases:
            return True
    return False

def _held_pairs() -> set:
    rows = execute_query(
        "SELECT pair FROM spot_trades WHERE status = 'OPEN' UNION SELECT pair FROM futures_trades WHERE status = 'OPEN'",
        fetch_all=True
    ) or []
    return {r["pair"] for r in rows}

def sync_binance_pairs():
    """
    Pings all active Binance USDT markets, filters for liquidity, refreshes Open Interest ratios,
    and ranks coins by volume. All database changes happen in ONE transaction.
    """
    ensure_pairs_table()
    now_str = datetime.now(timezone.utc).isoformat()

    min_vol_usd = MARKET_SCANNER_CONFIG.get("min_24h_volume_usdt", 3_000_000)
    max_pairs = MARKET_SCANNER_CONFIG.get("top_n_coins", 50)
    candidates = {}

    try:
        spot_client, fut_client, markets, fut_markets = _get_scan_clients()
        tickers = spot_client.fetch_tickers()
        all_bases = {m.get('base') for m in markets.values() if m.get('quote') == 'USDT'}

        for symbol, market in markets.items():
            if not market.get('quote') == 'USDT' or not market.get('active'):
                continue

            base = market.get('base')
            if base in EXCLUDED_BASES or _is_leveraged_token(base, all_bases):
                continue

            pair_str = f"{base}/USDT"
            fut_symbol = f"{base}/USDT:USDT"   # CCXT unified format for Binance USDT-M futures

            is_spot = 1 if market.get('spot') else 0
            is_fut = 1 if fut_symbol in fut_markets else 0

            ticker_info = tickers.get(symbol, {})
            last_p = float(ticker_info.get('last', 0) or 0)
            chg_24 = float(ticker_info.get('percentage', 0) or 0)
            vol_24 = float(ticker_info.get('quoteVolume', 0) or 0)

            # --- BTC MACRO GATE TRACKING ---
            if pair_str == "BTC/USDT":
                if last_p > 0:
                    MARKET_STATE["btc_price"] = last_p
                MARKET_STATE["btc_24h_change"] = chg_24
                MARKET_STATE["btc_trend"] = "BULLISH" if chg_24 >= 0 else "BEARISH"
                if chg_24 <= -1.5:
                    MARKET_STATE["btc_sentiment"] = "FEAR"
                elif chg_24 >= 1.5:
                    MARKET_STATE["btc_sentiment"] = "GREED"
                else:
                    MARKET_STATE["btc_sentiment"] = "NEUTRAL"
                continue

            if vol_24 >= min_vol_usd and last_p > 0:
                candidates[pair_str] = {
                    "pair": pair_str, "spot": is_spot, "futures": is_fut,
                    "chg": chg_24, "vol": vol_24, "price": last_p,
                    "oi_usd": 0.0, "oi_spot_ratio": 0.0
                }
    except Exception as e:
        add_log("WARNING", "apextick_bot", f"Scanner API Blocked or Timeout. Retaining current Hot List. ({e})")
        return 0

    sorted_pairs = sorted(candidates.values(), key=lambda x: x["vol"], reverse=True)[:max_pairs]
    if not sorted_pairs:
        return 0

    # --- OPEN INTEREST (cached; refreshed every OI_REFRESH_SECONDS per pair) ---
    now = time.time()
    oi_errors = 0
    for p in sorted_pairs:
        if p["futures"] == 1:
            cached = _oi_cache.get(p["pair"])
            if cached and (now - cached[1]) < OI_REFRESH_SECONDS:
                p["oi_usd"] = cached[0]
            else:
                try:
                    fut_symbol = f"{p['pair'].split('/')[0]}/USDT:USDT"
                    oi_data = _fut_client.fetch_open_interest(fut_symbol)
                    if oi_data and oi_data.get('openInterestValue'):
                        p["oi_usd"] = float(oi_data['openInterestValue'])
                    elif oi_data and oi_data.get('openInterestAmount'):
                        p["oi_usd"] = float(oi_data['openInterestAmount']) * p["price"]
                    _oi_cache[p["pair"]] = (p["oi_usd"], now)
                except Exception:
                    oi_errors += 1
                    if cached:
                        p["oi_usd"] = cached[0]
                time.sleep(0.05)   # Anti-rate-limit spacing
            if p["vol"] > 0:
                p["oi_spot_ratio"] = p["oi_usd"] / p["vol"]
    if oi_errors:
        _log_throttled("oi_errors", "WARNING", "apextick_bot", f"Open interest fetch failed for {oi_errors} pairs (using cached values).", every=600)

    # --- ONE ATOMIC TRANSACTION: upsert the hot list, deactivate the rest, never touch pairs we hold ---
    statements = []
    for p in sorted_pairs:
        statements.append((
            """
            INSERT INTO trading_pairs
            (pair, status, spot_supported, futures_supported, change_24h_pct, volume_24h_usd, last_price, oi_usd, oi_spot_ratio, reason, updated_at)
            VALUES (?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, 'Dynamic Hot List Scan', ?)
            ON CONFLICT (pair) DO UPDATE SET
                status = 'ACTIVE', spot_supported = excluded.spot_supported, futures_supported = excluded.futures_supported,
                change_24h_pct = excluded.change_24h_pct, volume_24h_usd = excluded.volume_24h_usd, last_price = excluded.last_price,
                oi_usd = excluded.oi_usd, oi_spot_ratio = excluded.oi_spot_ratio, updated_at = excluded.updated_at
            """,
            (p["pair"], p["spot"], p["futures"], p["chg"], p["vol"], p["price"], p["oi_usd"], p["oi_spot_ratio"], now_str)
        ))

    keep = [p["pair"] for p in sorted_pairs] + sorted(_held_pairs())   # open positions keep getting prices
    placeholders = ",".join("?" for _ in keep)
    statements.append((f"UPDATE trading_pairs SET status = 'INACTIVE' WHERE status = 'ACTIVE' AND pair NOT IN ({placeholders})", tuple(keep)))
    execute_batch(statements)

    add_log("INFO", "apextick_bot", f"Market Scan Complete: Tracked Top {len(sorted_pairs)} Liquid Pairs with OI Matrices.")
    return len(sorted_pairs)

# =============================================================================
# STREAMS & BACKGROUND LOOPS
# =============================================================================

def _ssl_options() -> dict:
    if os.getenv("WS_INSECURE_SSL", "0") == "1":
        _log_throttled("ssl_insecure", "WARNING", "apextick_bot", "WS_INSECURE_SSL=1: websocket certificates are NOT being verified.", every=3600)
        return {"cert_reqs": ssl.CERT_NONE}
    return {}   # default: full certificate verification

def _sync_db_prices(rows):
    """One transaction for all price updates. rows = [(pair, price, change)]."""
    now_str = datetime.now(timezone.utc).isoformat()
    execute_batch([
        ("UPDATE trading_pairs SET last_price = ?, change_24h_pct = ?, updated_at = ? WHERE pair = ?", (price, chg, now_str, pair))
        for pair, price, chg in rows
    ])

def _rest_price_refresh(rest_exchange) -> bool:
    """REST fallback: refresh live prices (and BTC pressure) when the websocket is down."""
    tickers = rest_exchange.fetch_tickers()
    active = execute_query("SELECT pair FROM trading_pairs WHERE status = 'ACTIVE'", fetch_all=True) or []
    rows = []
    for p_row in active:
        pair = p_row["pair"]
        t = tickers.get(pair)
        if t and t.get('last'):
            last_p = float(t['last'])
            chg = float(t.get('percentage', 0.0) or 0.0)
            MARKET_STATE["live_prices"][pair] = last_p
            MARKET_STATE["live_changes"][pair] = chg
            rows.append((pair, last_p, chg))
    if rows:
        MARKET_STATE["prices_ts"] = time.time()
        _sync_db_prices(rows)

    try:
        trades = rest_exchange.fetch_trades('BTC/USDT', limit=50)
        buy_n = sum(t['amount'] * t['price'] for t in trades if t['side'] == 'buy')
        sell_n = sum(t['amount'] * t['price'] for t in trades if t['side'] == 'sell')
        total = buy_n + sell_n
        if total > 0:
            buy_pct = (buy_n / total) * 100.0
            MARKET_STATE["buy_pressure_pct"] = round(buy_pct, 1)
            MARKET_STATE["sell_pressure_pct"] = round(100.0 - buy_pct, 1)
            MARKET_STATE["pressure_ts"] = time.time()
    except Exception:
        pass
    return bool(rows)

def start_btc_stream_background():
    stream_btc = "wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade/btcusdt@kline_1h"
    stream_all = "wss://stream.binance.com:9443/ws/!ticker@arr"

    def on_btc_message(ws, message):
        try:
            data = json.loads(message)
            payload = data.get("data", {})
            stream = data.get("stream", "")
            if stream.endswith("@aggTrade"):
                q, p, m = float(payload.get("q", 0)), float(payload.get("p", 0)), bool(payload.get("m", False))
                if q > 0 and p > 0:
                    MARKET_STATE["btc_price"] = p
                    _add_trade(int(time.time() * 1000), "SELL" if m else "BUY", q * p)
            elif "@kline_" in stream and payload.get("k"):
                _apply_kline(payload["k"])
        except Exception as e:
            _log_throttled("btc_msg", "WARNING", "apextick_bot", f"BTC stream message error: {e}")

    def on_ticker_message(ws, message):
        try:
            data = json.loads(message)
            now = time.time()
            MARKET_STATE["ticker_ts"] = now
            MARKET_STATE["prices_ts"] = now
            MARKET_STATE["ws_connected"] = True
            for item in data:
                sym = item.get("s", "")
                if sym.endswith("USDT"):
                    pair = f"{sym[:-4]}/USDT"
                    MARKET_STATE["live_prices"][pair] = float(item.get("c", 0))
                    MARKET_STATE["live_changes"][pair] = float(item.get("P", 0))
        except Exception as e:
            _log_throttled("ticker_msg", "WARNING", "apextick_bot", f"Ticker stream message error: {e}")

    def run_stream(name, url, on_message, is_ticker):
        def on_error(ws, error):
            _log_throttled(f"ws_err_{name}", "ERROR", "apextick_bot", f"WebSocket '{name}' error: {error}")
        def on_close(ws, code, msg):
            if is_ticker:
                MARKET_STATE["ws_connected"] = False   # lets the REST fallback take over immediately
            _log_throttled(f"ws_close_{name}", "WARNING", "apextick_bot", f"WebSocket '{name}' closed ({code}). Reconnecting.")
        backoff = 5
        while True:
            started = time.time()
            try:
                ws = websocket.WebSocketApp(url, on_message=on_message, on_error=on_error, on_close=on_close)
                ws.run_forever(ping_interval=20, ping_timeout=10, sslopt=_ssl_options())
            except Exception as e:
                _log_throttled(f"ws_exc_{name}", "ERROR", "apextick_bot", f"WebSocket '{name}' crashed: {e}")
            if is_ticker:
                MARKET_STATE["ws_connected"] = False
            backoff = 5 if (time.time() - started) > 60 else min(backoff * 2, 60)
            time.sleep(backoff)

    def db_price_sync_loop():
        rest_exchange = ccxt.binance({'enableRateLimit': True, 'timeout': 10000})
        try:
            _rest_price_refresh(rest_exchange)   # seed prices immediately
        except Exception:
            pass
        time.sleep(10)

        while True:
            time.sleep(3)
            try:
                # Watchdog: a silent stream is a dead stream
                if MARKET_STATE["ws_connected"] and (time.time() - MARKET_STATE["ticker_ts"]) > TICKER_SILENCE_SECONDS:
                    MARKET_STATE["ws_connected"] = False
                    _log_throttled("ws_silent", "WARNING", "apextick_bot", "Ticker stream went silent. Using REST price fallback.")

                if not MARKET_STATE["ws_connected"]:
                    try:
                        _rest_price_refresh(rest_exchange)
                    except Exception as e:
                        _log_throttled("rest_fail", "ERROR", "apextick_bot", f"REST price fallback failed: {e}", every=120)
                else:
                    active = execute_query("SELECT pair FROM trading_pairs WHERE status = 'ACTIVE'", fetch_all=True) or []
                    rows = []
                    for p_row in active:
                        pair = p_row["pair"]
                        price = MARKET_STATE["live_prices"].get(pair)
                        if price and price > 0:
                            rows.append((pair, price, MARKET_STATE["live_changes"].get(pair) or 0.0))
                    _sync_db_prices(rows)
            except Exception as e:
                _log_throttled("price_loop", "ERROR", "apextick_bot", f"Price sync loop error: {e}", every=120)

    def dynamic_scanner_loop():
        interval = MARKET_SCANNER_CONFIG.get("refresh_interval_minutes", 0.5) * 60
        cycle = 0
        while True:
            try:
                if cycle % 10 == 0:          # klines stream live; REST backfill every ~5 min as a safety net
                    backfill_btc_history()
                sync_binance_pairs()
                if cycle % 120 == 0:         # about hourly
                    prune_logs(keep_days=7)
            except Exception as e:
                add_log("ERROR", "apextick_bot", f"Scanner Loop Error: {e}")
            cycle += 1
            time.sleep(interval)

    threading.Thread(target=run_stream, args=("btc", stream_btc, on_btc_message, False), daemon=True).start()
    threading.Thread(target=run_stream, args=("ticker", stream_all, on_ticker_message, True), daemon=True).start()
    threading.Thread(target=db_price_sync_loop, daemon=True).start()
    threading.Thread(target=dynamic_scanner_loop, daemon=True).start()

if __name__ == "__main__":
    print("Initializing Institutional Data Core...")
    backfill_btc_history()
    sync_binance_pairs()
    start_btc_stream_background()
    print("Dynamic Market Scanner, OI Matrix, and WebSockets connected.")
    while True:
        time.sleep(60)
