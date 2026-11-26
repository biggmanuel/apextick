import os
import time
from datetime import datetime, timezone
import ccxt

# IMPORT THE NEW CONFIG DICTIONARIES
from config import SPOT_CONFIG, DYNAMIC_RISK_CONFIG, BINANCE_API_KEY, BINANCE_SECRET_KEY
from database import execute_query, add_log
from btc_decline_signal import is_trading_allowed_by_btc_gate, get_fresh_price, MARKET_STATE

# =============================================================================
# SPOT ENGINE (SMC accumulation, 100% target exit)
# -----------------------------------------------------------------------------
# Changes in this version:
#   * Mode flags fail SAFE (LIVE only on an explicit "0"; halt on "1"/"true").
#   * LIVE entries are refused unless ALLOW_LIVE_TRADING=1 (exits are still managed).
#   * LIVE orders go through a dedicated SPOT client (the shared config client is a futures one).
#   * Prices come from get_fresh_price(); stale prices pause management instead of acting on old data.
#   * Zones: only zones containing the price, narrowest first.
#   * TP is never closer than min_tp_pct; a drawdown stop (max_drawdown_pct) ends a bag that keeps falling.
#   * Entry fee booked (paper), no re-buy of a coin that just closed at a loss, error logging in exits,
#     one summary log line for skipped entries.
# =============================================================================

SPOT_ENTRY_COOLDOWN_SECONDS = 180  # 3-minute cooldown between spot entries
STALE_PRICE_THRESHOLD_SECONDS = 300

MIN_TP_PCT = SPOT_CONFIG.get("min_tp_pct", 1.2)
MAX_DRAWDOWN_PCT = SPOT_CONFIG.get("max_drawdown_pct", 12.0)
PAIR_COOLDOWN_AFTER_LOSS_SECONDS = SPOT_CONFIG.get("pair_cooldown_after_loss_seconds", 14400)
SKIP_LOG_INTERVAL_SECONDS = SPOT_CONFIG.get("skip_log_interval_seconds", 300)

# Dedicated SPOT client for LIVE orders. (config.exchange is a swap/futures client.)
live_spot_exchange = ccxt.binance({
    "apiKey": BINANCE_API_KEY,
    "secret": BINANCE_SECRET_KEY,
    "enableRateLimit": True,
    "options": {"defaultType": "spot", "adjustForTimeDifference": True, "recvWindow": 10000},
})

_SKIP_STATS = {}
_LAST_SKIP_LOG = time.time()
_COUNT_ONLY = {"no_zone_at_price", "account_cooldown", "max_positions", "low_balance", "btc_gate_block", "btc_ranging", "stale_prices"}

def _note_skip(reason: str, pair: str = "-"):
    bucket = _SKIP_STATS.setdefault(reason, {})
    bucket[pair] = bucket.get(pair, 0) + 1

def _flush_skip_log():
    global _LAST_SKIP_LOG
    if not _SKIP_STATS or (time.time() - _LAST_SKIP_LOG) < SKIP_LOG_INTERVAL_SECONDS:
        return
    parts = []
    for reason, pairs in sorted(_SKIP_STATS.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(pairs.values())
        if reason in _COUNT_ONLY:
            parts.append(f"{reason}: {total}")
        else:
            top = ", ".join(p for p, _ in sorted(pairs.items(), key=lambda kv: -kv[1])[:3])
            parts.append(f"{reason}: {total} ({top})")
    add_log("INFO", "spot_skips", f"Spot entry skips (last {SKIP_LOG_INTERVAL_SECONDS}s) -> " + " | ".join(parts))
    _SKIP_STATS.clear()
    _LAST_SKIP_LOG = time.time()

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _seconds_since(iso_str: str) -> float:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()

def get_live_price_safely(pair: str) -> float:
    """Fresh stream price, else a recently-updated database price, else 0.0 (do not act)."""
    p = get_fresh_price(pair)
    if p > 0:
        return p
    row = execute_query("SELECT last_price, updated_at FROM trading_pairs WHERE pair = ?", (pair,), fetch_one=True)
    if row and row.get("last_price") and float(row["last_price"]) > 0:
        try:
            if _seconds_since(row["updated_at"]) <= STALE_PRICE_THRESHOLD_SECONDS:
                return float(row["last_price"])
        except Exception:
            pass
    return 0.0

def is_account_on_spot_cooldown(account_id: str, mode: str) -> tuple[bool, int]:
    last_trade = execute_query(
        "SELECT opened_at FROM spot_trades WHERE account_id = ? AND trade_mode = ? ORDER BY id DESC LIMIT 1",
        (account_id, mode), fetch_one=True
    )
    if not last_trade:
        return False, 0
    try:
        elapsed = _seconds_since(last_trade["opened_at"])
        if elapsed < SPOT_ENTRY_COOLDOWN_SECONDS:
            return True, int(SPOT_ENTRY_COOLDOWN_SECONDS - elapsed)
    except Exception as e:
        add_log("WARNING", "spot_engine", f"Cooldown check could not parse opened_at: {e}")
    return False, 0

def is_pair_on_loss_cooldown(account_id: str, pair: str, mode: str) -> bool:
    """Do not re-buy a coin right after a LOSING close (drawdown stop / halt flatten)."""
    row = execute_query(
        """SELECT closed_at, profit_usdt FROM spot_trades
           WHERE account_id = ? AND trade_mode = ? AND pair = ? AND status = 'CLOSED'
           ORDER BY id DESC LIMIT 1""",
        (account_id, mode, pair), fetch_one=True
    )
    if not row or not row.get("closed_at") or float(row.get("profit_usdt") or 0.0) >= 0:
        return False
    try:
        return _seconds_since(row["closed_at"]) < PAIR_COOLDOWN_AFTER_LOSS_SECONDS
    except Exception:
        return False

def get_dynamic_spot_margin(available_margin: float, atr: float, current_price: float) -> float:
    if not DYNAMIC_RISK_CONFIG["enabled"] or atr <= 0 or current_price <= 0:
        return max(available_margin * (SPOT_CONFIG["trade_size_pct"] / 100.0), SPOT_CONFIG["min_trade_usdt"])

    natr_pct = (atr / current_price) * 100.0
    baseline_natr = 2.5
    volatility_modifier = baseline_natr / max(natr_pct, 0.1)
    adjusted_pct = SPOT_CONFIG["trade_size_pct"] * volatility_modifier
    trade_usdt = available_margin * (adjusted_pct / 100.0)
    return max(SPOT_CONFIG["min_trade_usdt"], min(trade_usdt, available_margin))

def check_smc_accumulation_zone(pair: str, current_price: float) -> tuple[bool, str, float]:
    """
    LAYER 3 SMC FILTER: is the live price inside an active Bullish zone?
    Narrowest matching zone first. Returns (is_valid, reason, ob_edge_price) - on failure `reason` is a skip code.
    """
    try:
        buffer = current_price * 0.002
        pools = execute_query(
            """
            SELECT pool_type, top_price, bottom_price
            FROM liquidity_pools
            WHERE pair = ? AND direction = 'BULLISH' AND status IN ('PENDING', 'MITIGATED')
              AND bottom_price <= ? AND top_price >= ?
            ORDER BY (top_price - bottom_price) ASC LIMIT 10
            """, (pair, current_price + buffer, current_price - buffer), fetch_all=True
        )
        if not pools:
            return False, "no_zone_at_price", 0.0

        for pool in pools:
            top, bottom = float(pool["top_price"]), float(pool["bottom_price"])
            if bottom < current_price:   # price must be above the invalidation edge
                return True, f"Price mitigated {pool['pool_type']} Demand Zone (${bottom:.4f} - ${top:.4f})", bottom
        return False, "price_below_zone", 0.0
    except Exception as e:
        return False, f"db_error: {e}", 0.0

def _live_sell(pair: str, qty: float):
    """LIVE market sell on the SPOT client. Fees on spot buys are taken in the base coin, so never sell more than we hold."""
    symbol = pair.replace("/", "")
    live_spot_exchange.load_markets()
    try:
        base = pair.split("/")[0]
        free = float(live_spot_exchange.fetch_balance().get("free", {}).get(base, qty) or qty)
        qty = min(qty, free)
    except Exception:
        pass
    qty_to_sell = float(live_spot_exchange.amount_to_precision(symbol, qty))
    return live_spot_exchange.create_order(symbol, "market", "sell", qty_to_sell)

# =============================================================================
# MAIN EXECUTION ENGINE
# =============================================================================

def run_spot_scalper_tick() -> str:
    """Master loop targeting SMC Accumulation and 100% Target Execution."""

    # 1. READ GOD-MODE STATE (fail safe: anything but an explicit "0" is PAPER)
    mode_row = execute_query("SELECT value FROM bot_settings WHERE key = 'paper_trading_enabled'", fetch_one=True)
    is_paper = not (mode_row and str(mode_row["value"]).strip() == "0")
    CURRENT_MODE = "PAPER" if is_paper else "LIVE"

    halt_row = execute_query("SELECT value FROM bot_settings WHERE key = 'macro_halt'", fetch_one=True)
    is_halted = bool(halt_row and str(halt_row["value"]).strip().lower() in ("1", "true"))

    accounts = execute_query("SELECT * FROM accounts WHERE spot_enabled = 1", fetch_all=True)
    if not accounts:
        return "Execution halted: No active Spot accounts enabled."

    activity_summary = []
    now_str = datetime.now(timezone.utc).isoformat()

    # -------------------------------------------------------------------------
    # PHASE 1: MANAGE OPEN POSITIONS
    # -------------------------------------------------------------------------
    for acct in accounts:
        acct_id = acct["account_id"]
        balance = float(acct["spot_balance_usdt"] or 0.0)

        open_positions = execute_query(
            "SELECT * FROM spot_trades WHERE account_id = ? AND status = 'OPEN' AND trade_mode = ?",
            (acct_id, CURRENT_MODE), fetch_all=True
        )

        for trade in open_positions:
            pair = trade["pair"]
            trade_id = trade["id"]

            current_price = get_live_price_safely(pair)
            if current_price <= 0:
                _note_skip("stale_prices", pair)
                continue

            entry_price = float(trade["entry_price"])
            profit_pct = ((current_price - entry_price) / entry_price) * 100.0

            orig_qty = float(trade["qty"])
            rem_qty = float(trade.get("remaining_qty") or orig_qty)
            accumulated_profit = float(trade.get("profit_usdt") or 0.0)

            tp1_hit = int(trade.get("tp1_hit") or 0)
            dynamic_tp1 = float(trade.get("dynamic_tp1") or 0.0)
            watermark_price = float(trade.get("watermark_price") or entry_price)

            if current_price > watermark_price:
                execute_query("UPDATE spot_trades SET watermark_price = ? WHERE id = ?", (current_price, trade_id), commit=True)
                watermark_price = current_price

            # --- DECIDE WHETHER TO EXIT ---
            close_reason = ""
            if is_halted:
                close_reason = "MACRO HALT (Flattened)"
            elif MAX_DRAWDOWN_PCT > 0 and profit_pct <= -MAX_DRAWDOWN_PCT:
                close_reason = f"DRAWDOWN STOP (-{MAX_DRAWDOWN_PCT:g}%)"
            elif current_price >= dynamic_tp1 and not tp1_hit:
                close_reason = "TP SECURED (100% Bag Locked)"

            if not close_reason:
                continue

            if not is_paper:
                try:
                    _live_sell(pair, rem_qty)
                except Exception as e:
                    add_log("ERROR", "api_execution", f"Failed LIVE EXIT ({close_reason}) for {pair}: {e}")
                    continue

            sell_notional = rem_qty * current_price
            fee_usdt = sell_notional * (SPOT_CONFIG["fee_pct"] / 100.0)
            net_profit = (sell_notional - (rem_qty * entry_price)) - fee_usdt
            final_pnl = accumulated_profit + net_profit

            execute_query(
                """
                UPDATE spot_trades
                SET status = 'CLOSED', exit_price = ?, remaining_qty = 0, profit_usdt = ?, profit_pct = ?, closed_at = ?
                WHERE id = ?
                """, (current_price, final_pnl, profit_pct, now_str, trade_id), commit=True
            )
            execute_query("UPDATE accounts SET spot_balance_usdt = spot_balance_usdt + ? WHERE account_id = ?",
                          (sell_notional - fee_usdt, acct_id), commit=True)

            level = "SUCCESS" if final_pnl >= 0 else "WARNING"
            log_msg = f"[{CURRENT_MODE}] {close_reason}: Closed {pair} at ${current_price:,.6g} | Final PnL: ${final_pnl:+.2f}"
            add_log(level, "apextick_bot", log_msg)
            activity_summary.append(log_msg)

    # -------------------------------------------------------------------------
    # PHASE 2: EVALUATE NEW SPOT ENTRIES (SMC FILTER)
    # -------------------------------------------------------------------------
    if is_halted:
        _flush_skip_log()
        return " | ".join(activity_summary) if activity_summary else "Spot Entries Disabled: MACRO KILL-SWITCH ACTIVE."

    if not is_paper and os.getenv("ALLOW_LIVE_TRADING", "0") != "1":
        _note_skip("live_locked")
        _flush_skip_log()
        return " | ".join(activity_summary) if activity_summary else "Spot entries paused: LIVE mode is locked (ALLOW_LIVE_TRADING != 1)."

    pairs = execute_query(
        """
        SELECT pair, last_price, change_24h_pct, atr
        FROM trading_pairs
        WHERE status = 'ACTIVE' AND spot_supported = 1 AND last_price > 0
        ORDER BY volume_24h_usd DESC
        """, fetch_all=True
    )
    if not pairs:
        return "Execution halted: No valid pairs in database."

    override_row = execute_query("SELECT value FROM bot_settings WHERE key = 'btc_gate_override'", fetch_one=True)
    gate_override = bool(override_row and str(override_row["value"]).strip() == "1")

    if not gate_override:
        allowed, gate_reason = is_trading_allowed_by_btc_gate()
        if not allowed:
            _note_skip("btc_gate_block")
            _flush_skip_log()
            return " | ".join(activity_summary) if activity_summary else f"Spot paused: BTC Gate BLOCK ({gate_reason})"

        row = execute_query("SELECT regime FROM market_regimes WHERE pair = 'BTC/USDT'", fetch_one=True)
        btc_regime = row["regime"] if row else "TRENDING"
        if btc_regime == "RANGING":
            _note_skip("btc_ranging")
            _flush_skip_log()
            return " | ".join(activity_summary) if activity_summary else "Spot paused: BTC is currently RANGING."

    for acct in accounts:
        acct_id = acct["account_id"]
        fresh_acct = execute_query("SELECT * FROM accounts WHERE account_id = ?", (acct_id,), fetch_one=True)
        balance = float(fresh_acct["spot_balance_usdt"] or 0.0)
        available_usdt = min(balance, float(fresh_acct.get("spot_capital_cap") or 999999.0))

        if available_usdt < SPOT_CONFIG["min_trade_usdt"]:
            _note_skip("low_balance")
            continue

        on_cooldown, _ = is_account_on_spot_cooldown(acct_id, CURRENT_MODE)
        if on_cooldown:
            _note_skip("account_cooldown")
            continue

        open_positions = execute_query(
            "SELECT pair FROM spot_trades WHERE account_id = ? AND status = 'OPEN' AND trade_mode = ?",
            (acct_id, CURRENT_MODE), fetch_all=True
        )
        if len(open_positions) >= SPOT_CONFIG["max_open_positions"]:
            _note_skip("max_positions")
            continue
        held_pairs = {p["pair"] for p in open_positions}

        for p_info in pairs:
            pair = p_info["pair"]
            if pair in ["BTC/USDT", "ETH/USDT", "BNB/USDT"] or pair in held_pairs:
                continue

            current_price = get_live_price_safely(pair)
            if current_price <= 0:
                _note_skip("stale_prices", pair)
                continue

            passed_smc, smc_reason, ob_edge_price = check_smc_accumulation_zone(pair, current_price)
            if not passed_smc:
                _note_skip(smc_reason.split(":")[0], pair)
                continue
            if ob_edge_price >= current_price:
                _note_skip("price_beyond_edge", pair)
                continue

            if is_pair_on_loss_cooldown(acct_id, pair, CURRENT_MODE):
                _note_skip("pair_loss_cooldown", pair)
                continue

            # --- TARGET MATH: R:R multiple of the distance to the zone edge, but never closer than MIN_TP_PCT ---
            atr = float(p_info.get("atr") or 0.0)
            risk_distance = current_price - ob_edge_price
            rr_multiplier = SPOT_CONFIG["risk_reward_multiplier"]
            dynamic_tp1 = max(current_price + (risk_distance * rr_multiplier), current_price * (1 + MIN_TP_PCT / 100.0))

            trade_usdt = get_dynamic_spot_margin(available_usdt, atr, current_price)
            qty = trade_usdt / current_price

            if not is_paper:
                try:
                    symbol = pair.replace("/", "")
                    live_spot_exchange.load_markets()
                    qty = float(live_spot_exchange.amount_to_precision(symbol, qty))
                    order = live_spot_exchange.create_order(symbol, "market", "buy", qty)
                    current_price = float(order.get("average") or order.get("price") or current_price)
                    qty = float(order.get("filled") or qty)
                    trade_usdt = qty * current_price
                    risk_distance = current_price - ob_edge_price
                    dynamic_tp1 = max(current_price + (risk_distance * rr_multiplier), current_price * (1 + MIN_TP_PCT / 100.0))
                except Exception as e:
                    add_log("ERROR", "api_execution", f"Failed LIVE ENTRY for {pair}: {e}")
                    continue

            entry_fee = trade_usdt * (SPOT_CONFIG["fee_pct"] / 100.0)

            execute_query(
                """
                INSERT INTO spot_trades (
                    account_id, pair, trade_mode, status, entry_price, qty, remaining_qty, notional_usdt,
                    ob_edge_price, current_sl, dynamic_tp1, watermark_price, opened_at, profit_usdt
                ) VALUES (?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (acct_id, pair, CURRENT_MODE, current_price, qty, qty, trade_usdt,
                 ob_edge_price, 0.0, dynamic_tp1, current_price, now_str, -entry_fee), commit=True
            )
            execute_query("UPDATE accounts SET spot_balance_usdt = spot_balance_usdt - ? WHERE account_id = ?",
                          (trade_usdt + entry_fee, acct_id), commit=True)

            tp_pct = (dynamic_tp1 / current_price - 1) * 100.0
            drawdown_note = f"drawdown stop -{MAX_DRAWDOWN_PCT:g}%" if MAX_DRAWDOWN_PCT > 0 else "no stop"
            msg = (f"[{fresh_acct['display_name']}] {CURRENT_MODE} SPOT ENTRY: Bought {pair} at ${current_price:,.6g} (${trade_usdt:.2f}, fee ${entry_fee:.3f})"
                   f"\n🎯 TP Target Locked @ ${dynamic_tp1:.6g} (+{tp_pct:.2f}%) | {drawdown_note}"
                   f"\n🧠 Sniper Logic: {smc_reason}")
            add_log("INFO", "apextick_bot", msg)
            activity_summary.append(msg)
            break

    _flush_skip_log()
    return " | ".join(activity_summary) if activity_summary else "Spot Engine: Monitoring SMC Pools."