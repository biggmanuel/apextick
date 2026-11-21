import os
import hmac
import time
import threading
import logging
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify

# --- APEXTICK CORE IMPORTS ---
from config import BOT_NAME, ADMIN_KEY, MARKET_SCANNER_CONFIG, FUTURES_CONFIG, SPOT_CONFIG
from database import init_db, execute_query, add_log, get_connection, IS_POSTGRES

from btc_decline_signal import (
    start_btc_stream_background, backfill_btc_history,
    sync_binance_pairs, is_trading_allowed_by_btc_gate, MARKET_STATE
)

# Execution Engines
from spot_engine import run_spot_scalper_tick
from futures_engine import run_futures_engine_tick
from grid_engine import GridEngine

# Quantitative Brains
from regime_manager import RegimeManager
from smart_money_concepts import SmartMoneyMapper
from macro_calendar import MacroCalendar

# =============================================================================
# APEXTICK: MASTER ORCHESTRATOR & API SERVER
# -----------------------------------------------------------------------------
# Dashboard-backend fixes in this version:
#   * Trade mode fails SAFE: anything other than an explicit "0" is treated as PAPER.
#   * Admin endpoints use a constant-time key check and are DISABLED while ADMIN_KEY is
#     unset/default. Switching to LIVE is refused unless ALLOW_LIVE_TRADING=1 is set.
#   * Mode / BTC-gate toggles validate their input (no more stray strings flipping LIVE).
#   * Win rate = closed trades with PnL > 0 (futures win rate no longer stuck at 0%).
#   * Open-position PnL = banked TP1 profit + the runner's unrealized PnL.
#   * portfolio_value = equity (cash + open positions); cash is exposed as cash_balance.
#   * Force Close / Liquidate All now credit the account, count TP1 banked profit and fees,
#     only close trades that are still OPEN, and refuse LIVE trades (no exchange orders
#     are sent from the dashboard, so it must not pretend to close them).
# =============================================================================

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

_DEFAULT_ADMIN_KEYS = {"", "secret_key_here", "changeme", "admin"}

# --- INDEPENDENT EXECUTION THREADS ---

def spot_engine_loop():
    add_log("INFO", "system", "Spot Engine Thread Started.")
    while True:
        try: run_spot_scalper_tick()
        except Exception as e: add_log("ERROR", "spot_engine", f"Execution Error: {e}")
        time.sleep(3)

def futures_engine_loop():
    add_log("INFO", "system", "Futures Engine Thread Started.")
    while True:
        try: run_futures_engine_tick()
        except Exception as e: add_log("ERROR", "futures_engine", f"Execution Error: {e}")
        time.sleep(3)

def start_background_services():
    """Initializes the Phase 1 & Phase 2 Quantitative Brains, then the Execution Arms."""
    init_db()

    if ADMIN_KEY in _DEFAULT_ADMIN_KEYS:
        add_log("WARNING", "system", "ADMIN_KEY is not set (or is a default). Admin actions (toggles, Force Close, "
                                     "Liquidate All) are DISABLED until you set a strong ADMIN_KEY in .env.")
    if os.getenv("ALLOW_LIVE_TRADING", "0") != "1":
        add_log("INFO", "system", "LIVE trading is locked (ALLOW_LIVE_TRADING != 1). The dashboard cannot switch to LIVE.")

    backfill_btc_history()
    sync_binance_pairs()
    start_btc_stream_background()

    # Phase 2: Spin up the Quantitative Brains
    regime = RegimeManager()
    threading.Thread(target=regime.start, daemon=True).start()

    smc = SmartMoneyMapper()
    threading.Thread(target=smc.start, daemon=True).start()

    macro = MacroCalendar()
    threading.Thread(target=macro.monitor_loop, daemon=True).start()

    # Phase 3: Spin up the Execution Arms
    grid = GridEngine()   # NOTE: stub - it only writes PENDING_GRID rows and places no orders
    threading.Thread(target=grid.start, daemon=True).start()

    threading.Thread(target=spot_engine_loop, daemon=True).start()
    threading.Thread(target=futures_engine_loop, daemon=True).start()

# =============================================================================
# HELPERS
# =============================================================================

def get_trade_mode() -> str:
    """LIVE only when the setting is an explicit '0'. Missing/garbage values mean PAPER."""
    row = execute_query("SELECT value FROM bot_settings WHERE key = 'paper_trading_enabled'", fetch_one=True)
    return "LIVE" if (row and str(row["value"]).strip() == "0") else "PAPER"

def _norm_flag(value):
    """Normalises true/false/1/0 style input to '1' or '0'. Returns None if unrecognised."""
    if isinstance(value, bool):
        return "1" if value else "0"
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return "1"
    if s in ("0", "false", "no", "off"):
        return "0"
    return None

def admin_error():
    """Returns a (response, status) tuple if the caller is NOT an authorised admin, else None."""
    if ADMIN_KEY in _DEFAULT_ADMIN_KEYS:
        return jsonify({"error": "ADMIN_KEY is not configured on the server. Set a strong ADMIN_KEY in .env and restart."}), 403
    supplied = request.headers.get("X-Admin-Key", "")
    if not hmac.compare_digest(supplied.encode("utf-8"), ADMIN_KEY.encode("utf-8")):
        return jsonify({"error": "Unauthorized"}), 403
    return None

def get_price(pair: str) -> float:
    """Live stream price first, then the last price stored in the database. 0.0 if unknown."""
    p = MARKET_STATE["live_prices"].get(pair)
    if p and float(p) > 0:
        return float(p)
    row = execute_query("SELECT last_price FROM trading_pairs WHERE pair = ?", (pair,), fetch_one=True)
    if row and row.get("last_price") and float(row["last_price"]) > 0:
        return float(row["last_price"])
    return 0.0

def _update_rowcount(query: str, params: tuple) -> int:
    """Runs one UPDATE and returns how many rows it changed (execute_query cannot report that)."""
    conn = get_connection()
    try:
        if IS_POSTGRES:
            query = query.replace("?", "%s")
        cur = conn.cursor()
        cur.execute(query, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()

def _futures_math(t: dict, price: float):
    """Returns (orig_margin, remaining_margin, banked_pnl, runner_unrealized) for a futures trade."""
    entry = float(t["entry_price"])
    lev = int(t["leverage"])
    orig = float(t.get("margin_usdt") or 0.0)
    rem = float(t.get("remaining_margin_usdt") or orig)
    banked = float(t.get("pnl_usdt") or 0.0)           # TP1 profit already locked (and the entry fee)
    move = ((price - entry) / entry) if t["direction"] == "LONG" else ((entry - price) / entry)
    return orig, rem, banked, rem * lev * move

def close_futures_paper(trade: dict, price: float, close_type: str):
    """Closes a PAPER futures trade with the same accounting the engine uses. Returns total PnL or None."""
    now_str = datetime.now(timezone.utc).isoformat()
    orig, rem, banked, gross = _futures_math(trade, price)
    lev = int(trade["leverage"])
    exit_fee = rem * lev * (FUTURES_CONFIG["fee_pct"] / 100.0)
    net = gross - exit_fee
    total = banked + net                                  # banked TP1 profit + runner result
    credit = max(0.0, rem + net)                          # remaining margin back to cash, plus runner result
    pct = (total / orig * 100.0) if orig > 0 else 0.0     # return on the ORIGINAL margin

    changed = _update_rowcount(
        """UPDATE futures_trades
           SET status = 'CLOSED', exit_price = ?, remaining_margin_usdt = 0.0, pnl_usdt = ?, pnl_pct = ?,
               close_type = ?, closed_at = ?
           WHERE id = ? AND status = 'OPEN'""",
        (price, total, pct, close_type, now_str, trade["id"])
    )
    if changed != 1:
        return None                                       # someone (the engine) closed it first
    execute_query("UPDATE accounts SET futures_balance_usdt = futures_balance_usdt + ? WHERE account_id = ?",
                  (credit, trade["account_id"]), commit=True)
    return total

def close_spot_paper(trade: dict, price: float):
    """Closes a PAPER spot trade with the same accounting the spot engine uses. Returns total PnL or None."""
    now_str = datetime.now(timezone.utc).isoformat()
    entry = float(trade["entry_price"])
    qty = float(trade.get("remaining_qty") or trade["qty"])
    accumulated = float(trade.get("profit_usdt") or 0.0)
    sell_notional = qty * price
    fee = sell_notional * (SPOT_CONFIG["fee_pct"] / 100.0)
    net = sell_notional - (qty * entry) - fee
    total = accumulated + net
    pct = ((price - entry) / entry * 100.0) if entry > 0 else 0.0

    changed = _update_rowcount(
        """UPDATE spot_trades
           SET status = 'CLOSED', exit_price = ?, remaining_qty = 0, profit_usdt = ?, profit_pct = ?, closed_at = ?
           WHERE id = ? AND status = 'OPEN'""",
        (price, total, pct, now_str, trade["id"])
    )
    if changed != 1:
        return None
    execute_query("UPDATE accounts SET spot_balance_usdt = spot_balance_usdt + ? WHERE account_id = ?",
                  (sell_notional - fee, trade["account_id"]), commit=True)
    return total

# =============================================================================
# GOD-MODE TOGGLES & UI ENDPOINTS
# =============================================================================

@app.route("/")
def index():
    return render_template("dashboard.html", bot_name=BOT_NAME, paper_trading=(get_trade_mode() == "PAPER"))

@app.route("/api/toggle-mode", methods=["POST"])
def toggle_trade_mode():
    """Hot-swaps between PAPER and LIVE. Input is validated; LIVE needs ALLOW_LIVE_TRADING=1."""
    denied = admin_error()
    if denied: return denied

    data = request.get_json(silent=True) or {}
    new_mode = _norm_flag(data.get("paper_trading_enabled"))
    if new_mode is None:
        return jsonify({"error": "paper_trading_enabled must be true/false or 1/0"}), 400

    if new_mode == "0" and os.getenv("ALLOW_LIVE_TRADING", "0") != "1":
        add_log("WARNING", "system_admin", "Attempt to switch to LIVE was refused (ALLOW_LIVE_TRADING is not 1).")
        return jsonify({"error": "LIVE mode is locked on this server. Set ALLOW_LIVE_TRADING=1 in .env and restart to unlock it."}), 403

    now_str = datetime.now(timezone.utc).isoformat()
    execute_query("UPDATE bot_settings SET value = ?, updated_at = ? WHERE key = 'paper_trading_enabled'",
                  (new_mode, now_str), commit=True)

    mode_str = "PAPER" if new_mode == "1" else "LIVE"
    add_log("WARNING", "system_admin", f"Trade Mode Hot-Swapped to {mode_str} by Admin UI.")
    return jsonify({"status": "success", "mode": mode_str})

@app.route("/api/toggle-btc-gate", methods=["POST"])
def toggle_btc_gate():
    """Forces the BTC safety gate open to test altcoins during crashes."""
    denied = admin_error()
    if denied: return denied

    data = request.get_json(silent=True) or {}
    override = _norm_flag(data.get("override"))
    if override is None:
        return jsonify({"error": "override must be true/false or 1/0"}), 400

    now_str = datetime.now(timezone.utc).isoformat()
    execute_query("UPDATE bot_settings SET value = ?, updated_at = ? WHERE key = 'btc_gate_override'",
                  (override, now_str), commit=True)

    add_log("WARNING", "system_admin", f"BTC Macro Gate Override set to: {'ON (Danger)' if override == '1' else 'OFF (Safe)'}")
    return jsonify({"status": "success", "override": override})

@app.route("/api/status", methods=["GET"])
def api_status():
    is_paper = (get_trade_mode() == "PAPER")

    halt_row = execute_query("SELECT value FROM bot_settings WHERE key = 'macro_halt'", fetch_one=True)
    is_halted = bool(halt_row and str(halt_row["value"]).strip() == "1")

    btc_regime_row = execute_query("SELECT regime FROM market_regimes WHERE pair = 'BTC/USDT'", fetch_one=True)

    override_row = execute_query("SELECT value FROM bot_settings WHERE key = 'btc_gate_override'", fetch_one=True)
    gate_override = bool(override_row and str(override_row["value"]).strip() == "1")

    prices_ts = float(MARKET_STATE.get("prices_ts") or 0.0)
    prices_age = (time.time() - prices_ts) if prices_ts > 0 else None

    allowed, gate_reason = is_trading_allowed_by_btc_gate()
    return jsonify({
        "btc_gate_override": gate_override,
        "macro_halt": is_halted,
        "prices_age_seconds": round(prices_age, 1) if prices_age is not None else None,
        "status": "HALTED" if is_halted else "RUNNING",
        "api_ok": True,
        "btc_gate_allowed": allowed,
        "btc_gate_reason": gate_reason,
        "ws_connected": MARKET_STATE.get("ws_connected", False),
        "execution_mode": "PAPER TRADING" if is_paper else "LIVE TRADING",
        "macro_regime": btc_regime_row["regime"] if btc_regime_row else "TRENDING"
    })

# =============================================================================
# DATA FEEDS
# =============================================================================

@app.route("/api/btc-sentiment", methods=["GET"])
def api_btc_sentiment():
    return jsonify({
        "sentiment": MARKET_STATE.get("btc_sentiment", "NEUTRAL"),
        "trend": MARKET_STATE.get("btc_trend", "NEUTRAL"),
        "atr": f"{MARKET_STATE.get('btc_atr_1h', 0.0):.2f}",
        "change_24h": f"{MARKET_STATE.get('btc_24h_change', 0.0):+.2f}%",
        "buy_pressure": f"{MARKET_STATE.get('buy_pressure_pct', 50.0)}%",
        "sell_pressure": f"{MARKET_STATE.get('sell_pressure_pct', 50.0)}%"
    })

@app.route("/api/market/powder-keg", methods=["GET"])
def api_powder_keg():
    """Feeds the Open Interest Matrix to the UI."""
    pairs = execute_query("""
        SELECT pair, last_price, oi_spot_ratio, change_24h_pct
        FROM trading_pairs
        WHERE oi_spot_ratio > 3.0 AND futures_supported = 1
        ORDER BY oi_spot_ratio DESC LIMIT 5
    """, fetch_all=True)
    return jsonify(pairs if pairs else [])

@app.route("/api/market/smc-zones", methods=["GET"])
def api_smc_zones():
    """Feeds active Order Blocks to the UI."""
    zones = execute_query("""
        SELECT pair, pool_type, direction, top_price, bottom_price, status
        FROM liquidity_pools
        WHERE status = 'PENDING'
        ORDER BY created_at DESC LIMIT 10
    """, fetch_all=True)
    return jsonify(zones if zones else [])

@app.route("/api/statistics", methods=["GET"])
def api_statistics():
    current_mode = get_trade_mode()

    spot_trades = execute_query("SELECT * FROM spot_trades WHERE trade_mode = ?", (current_mode,), fetch_all=True) or []
    closed = [t for t in spot_trades if t["status"] == "CLOSED"]
    open_t = [t for t in spot_trades if t["status"] == "OPEN"]

    realized = sum(float(t["profit_usdt"] or 0) for t in spot_trades)
    wins = len([t for t in closed if float(t["profit_usdt"] or 0) > 0])
    win_rate = (wins / len(closed) * 100.0) if closed else 0.0

    open_value, unrealized = 0.0, 0.0
    for t in open_t:
        entry = float(t["entry_price"])
        qty = float(t.get("remaining_qty") or t["qty"])
        price = MARKET_STATE["live_prices"].get(t["pair"]) or entry
        open_value += qty * price
        unrealized += (price - entry) * qty

    accts = execute_query("SELECT SUM(spot_balance_usdt) as tot FROM accounts", fetch_one=True)
    cash = float(accts["tot"]) if accts and accts["tot"] else 0.0

    return jsonify({
        "total_profit": round(realized, 2),
        "win_rate": f"{win_rate:.2f}%",
        "open_positions": len(open_t),
        "portfolio_value": round(cash + open_value, 2),   # equity: cash + open positions
        "cash_balance": round(cash, 2),
        "unrealized_pnl": round(unrealized, 2)
    })

@app.route("/api/futures/statistics", methods=["GET"])
def api_futures_statistics():
    current_mode = get_trade_mode()

    fut_trades = execute_query("SELECT * FROM futures_trades WHERE trade_mode = ?", (current_mode,), fetch_all=True) or []
    closed = [t for t in fut_trades if t["status"] == "CLOSED"]
    open_pos = [t for t in fut_trades if t["status"] == "OPEN"]

    # Net PnL = closed results + profit already banked on open trades (TP1 slices, entry fees)
    net_pnl = sum(float(t["pnl_usdt"] or 0) for t in fut_trades)
    wins = len([t for t in closed if float(t["pnl_usdt"] or 0) > 0])
    win_rate = (wins / len(closed) * 100.0) if closed else 0.0

    open_margin, unrealized = 0.0, 0.0
    for t in open_pos:
        entry = float(t["entry_price"])
        price = MARKET_STATE["live_prices"].get(t["pair"]) or entry
        _, rem, _, runner = _futures_math(t, price)
        open_margin += rem
        unrealized += runner

    accts = execute_query("SELECT SUM(futures_balance_usdt) as tot FROM accounts", fetch_one=True)
    cash = float(accts["tot"]) if accts and accts["tot"] else 0.0

    return jsonify({
        "completed_trades": len(closed),
        "win_rate": f"{win_rate:.2f}%",
        "net_pnl": round(net_pnl, 2),
        "portfolio_value": round(cash + open_margin + unrealized, 2),   # equity: cash + open margin + runner PnL
        "cash_balance": round(cash, 2),
        "unrealized_pnl": round(unrealized, 2),
        "open_positions": len(open_pos)
    })

@app.route("/api/positions", methods=["GET"])
def api_positions():
    current_mode = get_trade_mode()

    spot_open = execute_query("SELECT * FROM spot_trades WHERE status = 'OPEN' AND trade_mode = ?", (current_mode,), fetch_all=True) or []
    fut_open = execute_query("SELECT * FROM futures_trades WHERE status = 'OPEN' AND trade_mode = ?", (current_mode,), fetch_all=True) or []
    positions = []

    for t in spot_open:
        pair, entry = t["pair"], float(t["entry_price"])
        qty = float(t.get("remaining_qty") or t["qty"])
        live_price = MARKET_STATE["live_prices"].get(pair, entry)
        pnl = (live_price - entry) * qty
        roi_pct = ((live_price - entry) / entry) * 100.0 if entry > 0 else 0.0
        positions.append({
            "id": t["id"], "type": "spot", "pair": pair, "direction": "SPOT",
            "entry": f"${entry:.5f}", "live_price": f"${live_price:.5f}", "size": f"${(qty * live_price):.2f}",
            "pnl_usdt": pnl, "roi_pct": roi_pct, "tp1": bool(t["tp1_hit"]),
            "current_sl": float(t.get("current_sl") or 0.0),
            "dynamic_tp1": float(t.get("dynamic_tp1") or 0.0),
            "watermark": float(t.get("watermark_price") or entry)
        })

    for t in fut_open:
        pair, entry, lev, direction = t["pair"], float(t["entry_price"]), int(t["leverage"]), t["direction"]
        live_price = MARKET_STATE["live_prices"].get(pair, entry)
        tp1_hit = bool(t["tp1_hit"])

        orig, rem, banked, runner = _futures_math(t, live_price)
        total_pnl = banked + runner                                    # real result so far
        roi_pct = (total_pnl / orig * 100.0) if orig > 0 else 0.0      # return on ORIGINAL margin
        size = f"${rem:.2f} left of ${orig:.2f} ({lev}x)" if tp1_hit else f"${orig:.2f} ({lev}x)"

        positions.append({
            "id": t["id"], "type": "futures", "pair": pair, "direction": direction,
            "entry": f"${entry:.5f}", "live_price": f"${live_price:.5f}", "size": size,
            "pnl_usdt": total_pnl, "roi_pct": roi_pct, "peak_roi": float(t.get("peak_roi_pct") or 0.0),
            "banked_usdt": banked, "unrealized_usdt": runner, "leverage": lev,
            "tp1": tp1_hit,
            "current_sl": float(t.get("current_sl") or 0.0),
            "dynamic_tp1": float(t.get("dynamic_tp1") or 0.0),
            "watermark": float(t.get("watermark_price") or entry)
        })

    positions.sort(key=lambda x: x["roi_pct"], reverse=True)
    for p in positions: p["pnl_formatted"] = f"${p['pnl_usdt']:.2f} ({p['roi_pct']:+.2f}%)"
    return jsonify(positions)

# =============================================================================
# SECURE MANUAL OVERRIDE (FORCE CLOSE)
# =============================================================================

@app.route("/api/trades/close/<trade_type>/<int:trade_id>", methods=["POST"])
def force_close_trade(trade_type, trade_id):
    denied = admin_error()
    if denied: return denied

    if trade_type not in ("futures", "spot"):
        return jsonify({"error": "trade_type must be 'futures' or 'spot'"}), 400
    table = "futures_trades" if trade_type == "futures" else "spot_trades"

    trade = execute_query(f"SELECT * FROM {table} WHERE id = ?", (trade_id,), fetch_one=True)
    if not trade:
        return jsonify({"error": "Trade not found"}), 404
    if trade["status"] != "OPEN":
        return jsonify({"error": f"Trade is already {trade['status']}"}), 409
    if str(trade.get("trade_mode")) != "PAPER":
        return jsonify({"error": "This is a LIVE trade. The dashboard does not send exchange orders - close it on Binance."}), 409

    pair = trade["pair"]
    price = get_price(pair)
    if price <= 0:
        return jsonify({"error": f"No live price available for {pair}; try again in a moment."}), 503

    if trade_type == "futures":
        total = close_futures_paper(trade, price, "MANUAL_FORCE_CLOSE")
    else:
        total = close_spot_paper(trade, price)
    if total is None:
        return jsonify({"error": "Trade was closed by the engine at the same moment; nothing changed."}), 409

    add_log("WARNING", "system_admin", f"Manual Force Close: {trade_type.upper()} trade #{trade_id} ({pair}) at ${price:,.6g} | Final PnL ${total:+.2f}")
    return jsonify({"status": "success", "message": f"{pair} closed. Final PnL ${total:+.2f}", "pnl_usdt": round(total, 4)})

# =============================================================================
# NUCLEAR OPTION (LIQUIDATE ALL)
# =============================================================================

@app.route("/api/trades/liquidate-all", methods=["POST"])
def liquidate_all():
    denied = admin_error()
    if denied: return denied

    closed_spot = closed_fut = skipped_live = skipped_no_price = 0
    total_pnl = 0.0

    open_spot = execute_query("SELECT * FROM spot_trades WHERE status = 'OPEN'", fetch_all=True) or []
    for trade in open_spot:
        if str(trade.get("trade_mode")) != "PAPER":
            skipped_live += 1
            continue
        price = get_price(trade["pair"])
        if price <= 0:
            skipped_no_price += 1
            continue
        pnl = close_spot_paper(trade, price)
        if pnl is not None:
            closed_spot += 1
            total_pnl += pnl

    open_futures = execute_query("SELECT * FROM futures_trades WHERE status = 'OPEN'", fetch_all=True) or []
    for trade in open_futures:
        if str(trade.get("trade_mode")) != "PAPER":
            skipped_live += 1
            continue
        price = get_price(trade["pair"])
        if price <= 0:
            skipped_no_price += 1
            continue
        pnl = close_futures_paper(trade, price, "LIQUIDATE_ALL")
        if pnl is not None:
            closed_fut += 1
            total_pnl += pnl

    msg = (f"Liquidated {closed_spot} spot and {closed_fut} futures PAPER positions (PnL ${total_pnl:+.2f}). "
           f"Skipped: {skipped_live} LIVE (close on Binance), {skipped_no_price} with no price.")
    add_log("WARNING", "system_admin", f"NUCLEAR OPTION DEPLOYED: {msg}")
    return jsonify({"status": "success", "message": msg, "closed_spot": closed_spot, "closed_futures": closed_fut,
                    "skipped_live": skipped_live, "skipped_no_price": skipped_no_price})

# =============================================================================
# HISTORY & LOGS
# =============================================================================

@app.route("/api/trades", methods=["GET"])
def api_trades():
    current_mode = get_trade_mode()

    fut_exits = execute_query("""
        SELECT pair, 'FUTURES' as type, exit_price, pnl_usdt as realized_pnl, close_type, pnl_pct, closed_at
        FROM futures_trades
        WHERE status = 'CLOSED' AND trade_mode = ?
        ORDER BY closed_at DESC LIMIT 50
    """, (current_mode,), fetch_all=True)

    spot_exits = execute_query("""
        SELECT pair, 'SPOT' as type, exit_price, profit_usdt as realized_pnl, profit_pct, closed_at
        FROM spot_trades
        WHERE status = 'CLOSED' AND trade_mode = ?
        ORDER BY closed_at DESC LIMIT 70
    """, (current_mode,), fetch_all=True)

    return jsonify({
        "futures_trades": fut_exits if fut_exits else [],
        "spot_trades": spot_exits if spot_exits else []
    })

@app.route("/api/logs", methods=["GET"])
def api_logs():
    logs = execute_query("SELECT timestamp, level, source, message FROM logs ORDER BY id DESC LIMIT 500", fetch_all=True)
    return jsonify(logs if logs else [])

if __name__ == '__main__':
    os.makedirs("templates", exist_ok=True)
    os.makedirs("static", exist_ok=True)
    print("Booting ApexTick Institutional Orchestrator...")
    start_background_services()
    # Host/port can be overridden in .env, e.g. APEXTICK_HOST=127.0.0.1 behind a reverse proxy.
    app.run(host=os.getenv("APEXTICK_HOST", "0.0.0.0"), port=int(os.getenv("APEXTICK_PORT", "5000")),
            threaded=True, use_reloader=False)