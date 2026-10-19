import time
import threading
import logging
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify
from config import BOT_NAME, ADMIN_KEY
from database import init_db, execute_query, add_log
from btc_decline_signal import (
    start_btc_stream_background, backfill_btc_history,
    build_btc_24h_range_reports, sync_binance_pairs,
    is_trading_allowed_by_btc_gate, MARKET_STATE
)
from spot_engine import run_spot_scalper_tick
from futures_engine import run_futures_engine_tick

app = Flask(__name__)

# Silence repetitive Flask access logs to keep your WSL2 terminal clean
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

def apex_master_bot_loop():
    """Runs background bot ticks every 5 seconds for Spot and Futures engines."""
    add_log("INFO", "apextick_bot", "ApexTick Master Execution Engine started (Brain-Upgraded mode).")
    while True:
        try:
            run_spot_scalper_tick()
            run_futures_engine_tick()
        except Exception as e:
            add_log("ERROR", "apextick_bot", f"Execution loop error: {e}")
        time.sleep(5)

def start_background_services():
    """Initializes database, background WebSocket streams, and master bot workers."""
    init_db()
    backfill_btc_history()
    build_btc_24h_range_reports()
    sync_binance_pairs()
    start_btc_stream_background()
    
    bot_thread = threading.Thread(target=apex_master_bot_loop, daemon=True)
    bot_thread.start()

@app.route("/")
def index():
    return render_template("dashboard.html", bot_name=BOT_NAME)

@app.route("/api/status", methods=["GET"])
def api_status():
    allowed, reason = is_trading_allowed_by_btc_gate()
    return jsonify({
        "status": "running",
        "api_ok": True,
        "btc_gate_allowed": allowed,
        "btc_gate_reason": reason,
        "ws_connected": MARKET_STATE["ws_connected"]
    })

@app.route("/api/statistics", methods=["GET"])
def api_statistics():
    spot_trades = execute_query("SELECT * FROM spot_trades", fetch_all=True)
    total_trades = len(spot_trades) if spot_trades else 0
    closed_trades = [t for t in spot_trades if t["status"] == "CLOSED"] if spot_trades else []
    open_trades = [t for t in spot_trades if t["status"] == "OPEN"] if spot_trades else []
    
    total_profit = sum(float(t["profit_usdt"] or 0) for t in closed_trades)
    total_volume = sum(float(t["notional_usdt"] or 0) for t in spot_trades)
    
    wins = [t for t in closed_trades if float(t.get("profit_usdt", 0) or 0) > 0]
    win_rate = (len(wins) / len(closed_trades) * 100.0) if closed_trades else 0.0
    
    accts = execute_query("SELECT SUM(spot_balance_usdt) as tot FROM accounts", fetch_one=True)
    portfolio_value = float(accts["tot"]) if accts and accts["tot"] else 0.0

    return jsonify({
        "total_profit": round(total_profit, 2),
        "win_rate": f"{win_rate:.2f}%",
        "total_trades": total_trades,
        "total_volume": round(total_volume, 2),
        "open_positions": len(open_trades),
        "portfolio_value": round(portfolio_value, 2),
    })

@app.route("/api/futures/statistics", methods=["GET"])
def api_futures_statistics():
    fut_trades = execute_query("SELECT * FROM futures_trades", fetch_all=True)
    closed = [t for t in fut_trades if t["status"] == "CLOSED"] if fut_trades else []
    open_pos = [t for t in fut_trades if t["status"] == "OPEN"] if fut_trades else []
    
    tp_closes = [t for t in closed if t.get("close_type") and "TP" in t["close_type"]]
    sl_closes = [t for t in closed if t.get("close_type") and ("SL" in t["close_type"] or "SL" == t["close_type"])]
    
    tp_profit = sum(float(t["pnl_usdt"] or 0) for t in tp_closes if float(t.get("pnl_usdt", 0) or 0) > 0)
    sl_loss = sum(float(t["pnl_usdt"] or 0) for t in sl_closes if float(t.get("pnl_usdt", 0) or 0) < 0)
    net_pnl = sum(float(t["pnl_usdt"] or 0) for t in closed)
    total_vol = sum(float(t["notional_usdt"] or 0) for t in fut_trades) if fut_trades else 0.0
    
    win_rate = (len(tp_closes) / len(closed) * 100.0) if closed else 0.0
    accts = execute_query("SELECT SUM(futures_balance_usdt) as tot, SUM(futures_live_withdraw) as lw FROM accounts", fetch_one=True)
    portfolio = float(accts["tot"]) if accts and accts["tot"] else 0.0
    live_withdraw = float(accts["lw"]) if accts and accts["lw"] else 0.0

    return jsonify({
        "completed_trades": len(closed),
        "win_rate": f"{win_rate:.2f}%",
        "tp_count": len(tp_closes),
        "tp_profit": round(tp_profit, 2),
        "sl_count": len(sl_closes),
        "sl_profit": round(sl_loss, 2),
        "portfolio_value": round(portfolio, 2),
        "net_pnl": round(net_pnl, 2),
        "open_positions": len(open_pos),
        "total_volume": round(total_vol, 2),
        "live_withdraw": round(live_withdraw, 2),
    })

@app.route("/api/trades", methods=["GET"])
def api_trades():
    # Increased limit to 500 for compounding scrollbox support
    limit = int(request.args.get("limit", 500))
    trades = execute_query(
        "SELECT pair, entry_price, exit_price, profit_usdt, profit_pct, opened_at FROM spot_trades WHERE status = 'CLOSED' ORDER BY id DESC LIMIT ?",
        (limit,), fetch_all=True
    )
    formatted = []
    if trades:
        for t in trades:
            profit_usdt = float(t['profit_usdt'] or 0)
            profit_pct = float(t['profit_pct'] or 0)
            formatted.append({
                "pair": t["pair"],
                "entry": f"${float(t['entry_price'] or 0):.6f}",
                "exit": f"${float(t['exit_price'] or 0):.6f}",
                "profit": f"${profit_usdt:.2f} ({profit_pct:+.2f}%)",
                "time": t["opened_at"]
            })
    return jsonify(formatted)

@app.route("/api/futures/trades", methods=["GET"])
def api_futures_trades():
    """Dedicated endpoint for the new Futures Closes table with compounding history."""
    limit = int(request.args.get("limit", 500))
    trades = execute_query(
        """
        SELECT ft.*, a.display_name 
        FROM futures_trades ft
        LEFT JOIN accounts a ON ft.account_id = a.account_id
        WHERE ft.status = 'CLOSED' 
        ORDER BY ft.id DESC 
        LIMIT ?
        """,
        (limit,), fetch_all=True
    )
    formatted = []
    if trades:
        for t in trades:
            pnl_usdt = float(t['pnl_usdt'] or 0)
            pnl_pct = float(t['pnl_pct'] or 0)
            formatted.append({
                "pair": t["pair"],
                "direction": t["direction"],
                "exit": f"${float(t['exit_price'] or 0):.6f}",
                "pnl": f"${pnl_usdt:.2f} ({pnl_pct:+.1f}% ROI)",
                "type": t["close_type"] or "CLOSED",
                "account": t["display_name"] or t["account_id"],
                "time": t["closed_at"] or t["opened_at"]
            })
    return jsonify(formatted)

@app.route("/api/logs", methods=["GET"])
def api_logs():
    # Increased limit to 500 for compounding logs scrollbox support
    limit = int(request.args.get("limit", 500))
    logs = execute_query(
        "SELECT timestamp, level, source, message FROM system_logs ORDER BY id DESC LIMIT ?",
        (limit,), fetch_all=True
    )
    return jsonify(logs if logs else [])

@app.route("/api/btc-sentiment", methods=["GET"])
def api_btc_sentiment():
    return jsonify({
        "sentiment": MARKET_STATE.get("btc_sentiment", "NEUTRAL"),
        "trend": MARKET_STATE.get("btc_trend", "NEUTRAL"),
        "change_24h": f"{MARKET_STATE.get('btc_24h_change', 0.0):+.2f}%",
        "buy_pressure": f"{MARKET_STATE.get('buy_pressure_pct', 50.0)}%",
        "sell_pressure": f"{MARKET_STATE.get('sell_pressure_pct', 50.0)}%",
        "computed_allow": MARKET_STATE.get("computed_allow", True),
        "computed_reason": MARKET_STATE.get("computed_reason", "Initializing...")
    })

@app.route("/api/wallet", methods=["GET"])
def api_wallet():
    return jsonify({"wallet": "ApexTick Secure Multi-Account Node (WSL2)"})

@app.route("/api/bot/btc-config", methods=["GET"])
def api_get_btc_config():
    row = execute_query("SELECT value FROM bot_settings WHERE key = ?", ("btc_decline_signal_enabled",), fetch_one=True)
    enabled = (row["value"] == "1") if row else True
    return jsonify({"btc_decline_signal_enabled": enabled})

@app.route("/api/admin/btc-config", methods=["POST"])
def api_set_btc_config():
    admin_key = request.headers.get("X-Admin-Key", "")
    if admin_key != ADMIN_KEY:
        return jsonify({"error": "Invalid admin password"}), 403
    data = request.get_json() or {}
    enabled_val = "1" if data.get("enabled", True) else "0"
    execute_query(
        "INSERT INTO bot_settings (key, value) VALUES ('btc_decline_signal_enabled', ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        (enabled_val, enabled_val), commit=True
    )
    add_log("INFO", "apextick_admin", f"BTC decline signal toggled to: {'ON' if enabled_val == '1' else 'OFF'}")
    return jsonify({"btc_decline_signal_enabled": enabled_val == "1"})

# if __name__ == "__main__":
#     start_background_services()
#     app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)

if __name__ == '__main__':
    start_background_services()
    app.run(host='0.0.0.0', port=5000, threaded=True, use_reloader=False)