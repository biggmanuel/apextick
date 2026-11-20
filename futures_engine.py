import math
import time
from datetime import datetime, timezone
import pandas as pd
import ccxt

# IMPORT THE NEW CONFIG DICTIONARIES
from config import (FUTURES_CONFIG, MARKET_SCANNER_CONFIG, POWDER_KEG_CONFIG, 
                    DYNAMIC_RISK_CONFIG, BINANCE_API_KEY, BINANCE_SECRET_KEY, 
                    exchange as live_exchange)
from database import execute_query, add_log
from btc_decline_signal import is_trading_allowed_by_btc_gate, MARKET_STATE

# =============================================================================
# CONSTANTS & CONFIGURATION
# =============================================================================
FUTURES_ENTRY_COOLDOWN_SECONDS = 600  # Cooldown between new entries per account
STALE_PRICE_THRESHOLD_SECONDS = 300   # Max allowed age for a fallback price

# --- RISK CONTROLS (all tunable in config.py -> FUTURES_CONFIG; these are the fallbacks) ---
MIN_STOP_PCT = FUTURES_CONFIG.get("min_stop_pct", 0.4)                  # stops tighter than this = noise
MAX_STOP_PCT = FUTURES_CONFIG.get("max_stop_pct", 2.0)                  # stops wider than this = oversized loss
RISK_PER_TRADE_USDT = FUTURES_CONFIG.get("risk_per_trade_usdt", 0.50)   # target loss if the stop is hit
MAX_MARGIN_PCT_PER_TRADE = FUTURES_CONFIG.get("max_margin_pct_per_trade", 5.0)  # cap on margin vs balance
PAIR_STOP_COOLDOWN_SECONDS = FUTURES_CONFIG.get("pair_stop_cooldown_seconds", 7200)   # after a stop-out
PAIR_CLOSE_COOLDOWN_SECONDS = FUTURES_CONFIG.get("pair_close_cooldown_seconds", 1800) # after any other close
MIN_VOLUME_24H_USD = FUTURES_CONFIG.get("min_volume_24h_usd", 20_000_000)
SKIP_LOG_INTERVAL_SECONDS = FUTURES_CONFIG.get("skip_log_interval_seconds", 300)

# --- SKIP-REASON TRACKING (one summary log line per interval instead of per-tick spam) ---
_SKIP_STATS = {}
_LAST_SKIP_LOG = time.time()
COUNT_ONLY_REASONS = {"no_zone_at_price", "account_cooldown", "max_positions", "low_margin", "btc_gate_block"}

def _note_skip(reason: str, pair: str):
    bucket = _SKIP_STATS.setdefault(reason, {})
    bucket[pair] = bucket.get(pair, 0) + 1

def _flush_skip_log():
    """Writes a single INFO line summarising why entries were skipped since the last flush."""
    global _LAST_SKIP_LOG
    if not _SKIP_STATS or (time.time() - _LAST_SKIP_LOG) < SKIP_LOG_INTERVAL_SECONDS:
        return
    parts = []
    for reason, pairs in sorted(_SKIP_STATS.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(pairs.values())
        if reason in COUNT_ONLY_REASONS:
            parts.append(f"{reason}: {total}")
        else:
            top = ", ".join(p for p, _ in sorted(pairs.items(), key=lambda kv: -kv[1])[:3])
            parts.append(f"{reason}: {total} ({top})")
    add_log("INFO", "futures_skips", f"Entry skips (last {SKIP_LOG_INTERVAL_SECONDS}s) -> " + " | ".join(parts))
    _SKIP_STATS.clear()
    _LAST_SKIP_LOG = time.time()


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_live_price_safely(pair: str) -> float:
    """
    Prefers the real-time stream (MARKET_STATE), falls back to the database's
    last known price, and completely rejects invalid/stale prices.
    """
    current_price = MARKET_STATE["live_prices"].get(pair)
    if current_price and current_price > 0:
        return float(current_price)

    p_row = execute_query("SELECT last_price FROM trading_pairs WHERE pair = ?", (pair,), fetch_one=True)
    if p_row and p_row.get("last_price"):
        db_price = float(p_row["last_price"])
        if db_price > 0:
            return db_price

    return 0.0

def calculate_futures_macro_trend() -> str:
    """Calculates 9-EMA and 21-EMA crossover on BTC to determine bias."""
    klines = MARKET_STATE.get("klines_1h", [])
    if len(klines) < 21:
        return "NEUTRAL"

    closes = [float(k["c"]) for k in klines if float(k.get("c", 0)) > 0]
    if len(closes) < 21:
        return "NEUTRAL"

    series = pd.Series(closes)
    ema_9 = float(series.ewm(span=9, adjust=False).mean().iloc[-1])
    ema_21 = float(series.ewm(span=21, adjust=False).mean().iloc[-1])

    if ema_9 > ema_21:
        return "BULLISH"
    elif ema_9 < ema_21:
        return "BEARISH"
    return "NEUTRAL"

def _seconds_since(iso_str: str) -> float:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()

def is_account_on_futures_cooldown(account_id: str, mode: str) -> tuple[bool, int]:
    """Spacing between ANY two entries. Uses the newest trade regardless of status, so
    long-running open runners can no longer hide a trade that just opened/closed."""
    last_trade = execute_query(
        "SELECT opened_at FROM futures_trades WHERE account_id = ? AND trade_mode = ? ORDER BY id DESC LIMIT 1",
        (account_id, mode), fetch_one=True
    )
    if not last_trade:
        return False, 0
    try:
        elapsed = _seconds_since(last_trade["opened_at"])
        if elapsed < FUTURES_ENTRY_COOLDOWN_SECONDS:
            return True, int(FUTURES_ENTRY_COOLDOWN_SECONDS - elapsed)
    except Exception as e:
        add_log("WARNING", "futures_engine", f"Cooldown check could not parse opened_at: {e}")
    return False, 0

def is_pair_on_cooldown(account_id: str, pair: str, mode: str) -> tuple[bool, int, str]:
    """Blocks re-entering the same pair right after it closed. A structural stop-out
    blocks it for longer (PAIR_STOP_COOLDOWN_SECONDS) than any other close."""
    row = execute_query(
        """SELECT closed_at, close_type FROM futures_trades
           WHERE account_id = ? AND trade_mode = ? AND pair = ? AND status = 'CLOSED'
           ORDER BY id DESC LIMIT 1""",
        (account_id, mode, pair), fetch_one=True
    )
    if not row or not row.get("closed_at"):
        return False, 0, ""
    close_type = str(row.get("close_type") or "")
    window = PAIR_STOP_COOLDOWN_SECONDS if close_type.startswith("STRUCTURAL SL") else PAIR_CLOSE_COOLDOWN_SECONDS
    try:
        elapsed = _seconds_since(row["closed_at"])
        if elapsed < window:
            return True, int(window - elapsed), close_type
    except Exception as e:
        add_log("WARNING", "futures_engine", f"Pair cooldown could not parse closed_at for {pair}: {e}")
    return False, 0, ""

def get_dynamic_margin(available_margin: float, atr: float, current_price: float) -> float:
    """
    LAYER 5 RISK SHIELD: Dynamically sizes margin based on Average True Range.
    Inverses volatility: Higher volatility = smaller capital allocation.
    """
    if not DYNAMIC_RISK_CONFIG["enabled"] or atr <= 0 or current_price <= 0:
        return available_margin * (FUTURES_CONFIG["margin_size_pct"] / 100.0)
        
    natr_pct = (atr / current_price) * 100.0
    baseline_natr = 2.5 # Normal healthy volatility target
    
    volatility_modifier = baseline_natr / max(natr_pct, 0.1)
    adjusted_margin_pct = FUTURES_CONFIG["margin_size_pct"] * volatility_modifier
    
    # Clamp to configured limits
    final_pct = max(DYNAMIC_RISK_CONFIG["min_margin_pct"], min(adjusted_margin_pct, DYNAMIC_RISK_CONFIG["max_margin_pct"]))
    
    return available_margin * (final_pct / 100.0)

def check_micro_structure(pair: str, current_price: float, direction: str) -> tuple[bool, str, float]:
    """
    SMC Snipe Integration for Futures.
    Requires price to be inside an active Order Block / FVG that matches the trade direction
    (LONG -> BULLISH zone, SHORT -> BEARISH zone, as stored by smart_money_concepts.py).
    Among matching zones the NARROWEST is tried first, and a zone is only accepted if its
    stop distance is inside [MIN_STOP_PCT, MAX_STOP_PCT].
    Returns: (is_valid, reason, ob_edge_price). On failure `reason` is a short skip code.
    """
    zone_dir = "BULLISH" if direction == "LONG" else "BEARISH"
    try:
        buffer = current_price * 0.002
        pools = execute_query(
            """
            SELECT pool_type, top_price, bottom_price
            FROM liquidity_pools
            WHERE pair = ? AND direction = ? AND status IN ('PENDING', 'MITIGATED')
              AND bottom_price <= ? AND top_price >= ?
            ORDER BY (top_price - bottom_price) ASC LIMIT 10
            """, (pair, zone_dir, current_price + buffer, current_price - buffer), fetch_all=True
        )
        if not pools:
            return False, "no_zone_at_price", 0.0

        failures = set()
        for pool in pools:
            top, bottom = float(pool["top_price"]), float(pool["bottom_price"])
            edge = bottom if direction == "LONG" else top

            # Price already beyond the invalidation line
            if (direction == "LONG" and current_price < edge) or (direction == "SHORT" and current_price > edge):
                failures.add("price_beyond_edge")
                continue

            stop_pct = abs(current_price - edge) / current_price * 100.0
            if stop_pct < MIN_STOP_PCT:
                failures.add("stop_too_tight")
                continue
            if stop_pct > MAX_STOP_PCT:
                failures.add("stop_too_wide")
                continue

            return True, f"Price mitigated {pool['pool_type']} ({direction}) | stop {stop_pct:.2f}%", edge

        for code in ("stop_too_wide", "stop_too_tight", "price_beyond_edge"):
            if code in failures:
                return False, code, 0.0
        return False, "no_zone_at_price", 0.0
    except Exception as e:
        return False, f"db_error: {e}", 0.0


# =============================================================================
# MAIN EXECUTION ENGINE
# =============================================================================

def run_futures_engine_tick() -> str:
    """Master loop targeting SMC Snipes & 75/25 Risk Engine."""
    
    # 1. READ GOD-MODE STATE (Hot-Swap Live from Database)
    mode_row = execute_query("SELECT value FROM bot_settings WHERE key = 'paper_trading_enabled'", fetch_one=True)
    is_paper = (mode_row["value"] == "1") if mode_row else True
    CURRENT_MODE = "PAPER" if is_paper else "LIVE"

    halt_row = execute_query("SELECT value FROM bot_settings WHERE key = 'macro_halt'", fetch_one=True)
    is_halted = (halt_row["value"] == "1") if halt_row else False

    accounts = execute_query("SELECT * FROM accounts WHERE futures_enabled = 1", fetch_all=True)
    if not accounts: return "Execution halted: No active accounts."

    activity_summary = []
    now_str = datetime.now(timezone.utc).isoformat()

    # -------------------------------------------------------------------------
    # PHASE 1: MANAGE OPEN POSITIONS (75/25 SNIPER EXECUTION)
    # -------------------------------------------------------------------------
    for acct in accounts:
        acct_id = acct["account_id"]
        balance = float(acct["futures_balance_usdt"] or 0.0)

        open_positions = execute_query(
            """
            SELECT * FROM futures_trades
            WHERE account_id = ? AND status = 'OPEN' AND trade_mode = ?
            """, (acct_id, CURRENT_MODE), fetch_all=True
        )

        for trade in open_positions:
            pair = trade["pair"]
            trade_id = trade["id"]
            
            current_price = get_live_price_safely(pair)
            if current_price <= 0: continue

            entry_price = float(trade["entry_price"])
            direction = trade["direction"]
            leverage = int(trade["leverage"])
            
            price_diff_pct = ((current_price - entry_price) / entry_price) * 100.0 if direction == "LONG" else ((entry_price - current_price) / entry_price) * 100.0
            roi_pct = price_diff_pct * leverage

            peak_roi = max(float(trade.get("peak_roi_pct") or 0.0), roi_pct)
            worst_roi = min(float(trade.get("worst_roi_pct") or 0.0), roi_pct)

            orig_margin = float(trade["margin_usdt"])
            rem_margin = float(trade.get("remaining_margin_usdt") or orig_margin)
            accumulated_pnl = float(trade.get("pnl_usdt") or 0.0)
            
            tp1_hit = int(trade.get("tp1_hit") or 0)
            current_sl = float(trade.get("current_sl") or 0.0)
            dynamic_tp1 = float(trade.get("dynamic_tp1") or 0.0)
            watermark_price = float(trade.get("watermark_price") or entry_price)

            # --- UPDATE WATERMARK & ROI MEMORY ---
            if (direction == "LONG" and current_price > watermark_price) or (direction == "SHORT" and current_price < watermark_price):
                watermark_price = current_price
            
            execute_query(
                "UPDATE futures_trades SET peak_roi_pct = ?, worst_roi_pct = ?, watermark_price = ? WHERE id = ?",
                (peak_roi, worst_roi, watermark_price, trade_id), commit=True
            )

            # --- MACRO KILL-SWITCH (PANIC SELL) ---
            if is_halted:
                risk_exit_type = "MACRO HALT (Panic Sell)"
                notional_closed = rem_margin * leverage
                if not is_paper:
                    try:
                        symbol, side = pair.replace("/", ""), "sell" if direction == "LONG" else "buy"
                        live_exchange.load_markets()
                        qty = live_exchange.amount_to_precision(symbol, notional_closed / current_price)
                        live_exchange.create_order(symbol, "market", side, qty)
                    except Exception as e:
                        add_log("ERROR", "api_execution", f"Failed MACRO HALT close for {pair}: {e}")
                        continue
                
                gross_loss = rem_margin * (roi_pct / 100.0)
                net_loss = gross_loss - (notional_closed * (FUTURES_CONFIG["fee_pct"] / 100.0))
                
                execute_query(
                    """
                    UPDATE futures_trades
                    SET status = 'CLOSED', exit_price = ?, remaining_margin_usdt = 0.0,
                        margin_usdt = ?, notional_usdt = ?, pnl_usdt = ?, pnl_pct = ?, close_type = ?, closed_at = ?
                    WHERE id = ?
                    """,
                    (current_price, rem_margin, notional_closed, net_loss, roi_pct, risk_exit_type, now_str, trade_id),
                    commit=True
                )
                
                execute_query("UPDATE accounts SET futures_balance_usdt = futures_balance_usdt + ? WHERE account_id = ?",
                              (rem_margin + net_loss, acct_id), commit=True)
                activity_summary.append(f"[{acct['display_name']}] 🚨 SHIELD ACTIVATED: {pair} flattened at ${current_price:,.4f}")
                continue

            # --- 75/25 SMC SNIPER EXIT PROTOCOL ---
            close_reason = ""
            margin_to_close = 0.0
            full_close = False

            # Long Logic
            if direction == "LONG":
                # Rule 1: Structural SL
                if current_price <= current_sl and not tp1_hit:
                    close_reason, margin_to_close, full_close = "STRUCTURAL SL (SMC Invalidated)", rem_margin, True
                # Rule 2: TP1 (75% Secure)
                elif current_price >= dynamic_tp1 and not tp1_hit:
                    close_reason, margin_to_close, full_close = "TP1 SECURED (75% Bag Locked)", min(orig_margin * FUTURES_CONFIG["tp1_close_ratio"], rem_margin), False
                # Rule 3: Breakeven Switch
                elif current_price <= current_sl and tp1_hit:
                    close_reason, margin_to_close, full_close = "BREAKEVEN SL (Runner Stopped at Entry)", rem_margin, True
                # Rule 4: Trailing Runner
                elif tp1_hit:
                    trail_trigger = watermark_price * (1 - (FUTURES_CONFIG["trailing_buffer_pct"] / 100.0))
                    if current_price <= trail_trigger and current_price > entry_price:
                        close_reason, margin_to_close, full_close = f"TRAILING SL HIT (-{FUTURES_CONFIG['trailing_buffer_pct']:g}% from peak ${watermark_price:.4f})", rem_margin, True
            
            # Short Logic
            else:
                # Rule 1: Structural SL
                if current_price >= current_sl and not tp1_hit:
                    close_reason, margin_to_close, full_close = "STRUCTURAL SL (SMC Invalidated)", rem_margin, True
                # Rule 2: TP1 (75% Secure)
                elif current_price <= dynamic_tp1 and not tp1_hit:
                    close_reason, margin_to_close, full_close = "TP1 SECURED (75% Bag Locked)", min(orig_margin * FUTURES_CONFIG["tp1_close_ratio"], rem_margin), False
                # Rule 3: Breakeven Switch
                elif current_price >= current_sl and tp1_hit:
                    close_reason, margin_to_close, full_close = "BREAKEVEN SL (Runner Stopped at Entry)", rem_margin, True
                # Rule 4: Trailing Runner
                elif tp1_hit:
                    trail_trigger = watermark_price * (1 + (FUTURES_CONFIG["trailing_buffer_pct"] / 100.0))
                    if current_price >= trail_trigger and current_price < entry_price:
                        close_reason, margin_to_close, full_close = f"TRAILING SL HIT (+{FUTURES_CONFIG['trailing_buffer_pct']:g}% from peak ${watermark_price:.4f})", rem_margin, True


            # --- EXECUTE APPLICABLE EXIT PROTOCOL ---
            if close_reason:
                notional_closed = margin_to_close * leverage
                
                if not is_paper:
                    try:
                        symbol, side = pair.replace("/", ""), "sell" if direction == "LONG" else "buy"
                        live_exchange.load_markets()
                        qty = live_exchange.amount_to_precision(symbol, notional_closed / current_price)
                        live_exchange.create_order(symbol, "market", side, qty)
                    except Exception as e:
                        add_log("ERROR", "api_execution", f"Failed LIVE exit for {pair}: {e}")
                        continue
                
                net_pnl = (margin_to_close * (roi_pct / 100.0)) - (notional_closed * (FUTURES_CONFIG["fee_pct"] / 100.0))

                if full_close:
                    execute_query(
                        """
                        UPDATE futures_trades
                        SET status = 'CLOSED', exit_price = ?, remaining_margin_usdt = 0.0,
                            pnl_usdt = ?, pnl_pct = ?, close_type = ?, closed_at = ?
                        WHERE id = ?
                        """,
                        (current_price, accumulated_pnl + net_pnl, roi_pct, close_reason, now_str, trade_id), commit=True
                    )
                    log_level = "SUCCESS" if (accumulated_pnl + net_pnl) > 0 else "WARNING"
                    msg = f"[{acct['display_name']}] {CURRENT_MODE} {close_reason}: {pair} {direction} closed. Final PnL: ${accumulated_pnl + net_pnl:+.2f}"
                else: # TP1 Partial Close
                    execute_query(
                        """
                        UPDATE futures_trades 
                        SET remaining_margin_usdt = ?, pnl_usdt = ?, pnl_pct = ?, tp1_hit = 1, current_sl = ? 
                        WHERE id = ?
                        """,
                        (rem_margin - margin_to_close, accumulated_pnl + net_pnl, roi_pct, entry_price, trade_id), commit=True
                    )
                    msg = f"[{CURRENT_MODE}] {close_reason}: {pair} | Locked: +${net_pnl:.2f} | SL moved to Breakeven."
                    log_level = "SUCCESS"
                
                balance += max(0.0, margin_to_close + net_pnl)
                # Only split profits to withdrawal pool on a full close (TP2 hit)
                w_pool = (net_pnl * FUTURES_CONFIG["withdraw_split_pct"]) if net_pnl > 0 and full_close else 0.0
                
                execute_query("UPDATE accounts SET futures_balance_usdt = ?, futures_live_withdraw = futures_live_withdraw + ? WHERE account_id = ?",
                              (balance, w_pool, acct_id), commit=True)
                
                add_log(log_level, "apextick_bot", msg)
                activity_summary.append(msg)


    # -------------------------------------------------------------------------
    # PHASE 2: EVALUATE NEW ENTRIES (SMC & TREND)
    # -------------------------------------------------------------------------
    if is_halted:
        return " | ".join(activity_summary) if activity_summary else "Futures Entries Disabled: MACRO KILL-SWITCH ACTIVE."

    pairs = execute_query(
        """
        SELECT pair, last_price, change_24h_pct, oi_spot_ratio, atr 
        FROM trading_pairs 
        WHERE status = 'ACTIVE' AND futures_supported = 1 AND last_price > 0
          AND volume_24h_usd >= ?
        ORDER BY volume_24h_usd DESC
        """, (MIN_VOLUME_24H_USD,), fetch_all=True
    )
    if not pairs: return "Execution halted: No valid pairs in database."

    btc_trend = calculate_futures_macro_trend()
    buy_pressure = float(MARKET_STATE.get("buy_pressure_pct", 50.0))

    # Master BTC Override (God-Mode UI Check)
    override_row = execute_query("SELECT value FROM bot_settings WHERE key = 'btc_gate_override'", fetch_one=True)
    gate_override = (override_row["value"] == "1") if override_row else False
    
    if not gate_override:
        allowed, gate_reason = is_trading_allowed_by_btc_gate()
        if not allowed:
            _note_skip("btc_gate_block", "-")
            _flush_skip_log()
            return " | ".join(activity_summary) if activity_summary else f"Futures paused: BTC Gate BLOCK ({gate_reason})"

    for acct in accounts:
        acct_id = acct["account_id"]
        fresh_acct = execute_query("SELECT * FROM accounts WHERE account_id = ?", (acct_id,), fetch_one=True)
        balance = float(fresh_acct["futures_balance_usdt"] or 0.0)
        available_margin = min(balance, float(fresh_acct.get("futures_capital_cap") or 999999.0))

        if available_margin < FUTURES_CONFIG["min_margin_usdt"]:
            _note_skip("low_margin", "-")
            continue
            
        on_cooldown, _ = is_account_on_futures_cooldown(acct_id, CURRENT_MODE)
        if on_cooldown:
            _note_skip("account_cooldown", "-")
            continue

        open_positions = execute_query(
            "SELECT pair FROM futures_trades WHERE account_id = ? AND status = 'OPEN' AND trade_mode = ?",
            (acct_id, CURRENT_MODE), fetch_all=True
        )
        if len(open_positions) >= FUTURES_CONFIG["max_open_positions"]:
            _note_skip("max_positions", "-")
            continue
        held_pairs = {p["pair"] for p in open_positions}

        for p_info in pairs:
            pair = p_info["pair"]
            if pair in ["BTC/USDT", "ETH/USDT", "BNB/USDT"] or pair in held_pairs: continue

            atr = float(p_info.get("atr", 0.0))
            current_price = get_live_price_safely(pair)
            
            target_direction = None

            # --- LAYER 1: STRUCTURAL TREND PRIORITY (Powder Keg Bypassed) ---
            if btc_trend == "BULLISH": 
                target_direction = "LONG"
            elif btc_trend == "BEARISH": 
                target_direction = "SHORT"
            else:
                # If BTC is Neutral/Choppy, let the local altcoin buy pressure decide the direction
                target_direction = "LONG" if buy_pressure >= 50.0 else "SHORT"
                
            if not target_direction: continue

            # --- LAYER 3: SMC ORDER BLOCK TRIGGER ---
            passed_smc, smc_reason, ob_edge_price = check_micro_structure(pair, current_price, target_direction)
            if not passed_smc:
                _note_skip(smc_reason.split(":")[0], pair)
                continue

            on_pair_cd, pair_cd_left, last_close = is_pair_on_cooldown(acct_id, pair, CURRENT_MODE)
            if on_pair_cd:
                _note_skip("pair_cooldown", pair)
                continue

            # Validate entry isn't already past invalidation
            if (target_direction == "LONG" and current_price < ob_edge_price) or (target_direction == "SHORT" and current_price > ob_edge_price):
                _note_skip("price_beyond_edge", pair)
                continue
                
            entry_reason = f"SMC Zone: {smc_reason} | Macro: {btc_trend}"

            # --- THE DYNAMIC R:R TARGET MATH ---
            rr_multiplier = FUTURES_CONFIG["risk_reward_multiplier"]
            risk_distance = abs(current_price - ob_edge_price)
            
            if target_direction == "LONG":
                dynamic_tp1 = current_price + (risk_distance * rr_multiplier)
            else:
                dynamic_tp1 = current_price - (risk_distance * rr_multiplier)


            # --- LAYER 5: DYNAMIC RISK ALLOCATION ---
            # Size the position so that hitting the stop loses about RISK_PER_TRADE_USDT,
            # whatever the stop distance:  margin = risk / (leverage * stop_fraction)
            stop_fraction = risk_distance / current_price
            margin_by_risk = RISK_PER_TRADE_USDT / (FUTURES_CONFIG["leverage"] * stop_fraction)
            max_margin = available_margin * (MAX_MARGIN_PCT_PER_TRADE / 100.0)
            margin_usdt = max(FUTURES_CONFIG["min_margin_usdt"], min(margin_by_risk, max_margin, available_margin))

            notional_value = margin_usdt * FUTURES_CONFIG["leverage"]
            entry_fee = notional_value * (FUTURES_CONFIG["fee_pct"] / 100.0)
            
            if not is_paper:
                try:
                    symbol, side = pair.replace("/", ""), "buy" if target_direction == "LONG" else "sell"
                    live_exchange.set_leverage(FUTURES_CONFIG["leverage"], symbol)
                    live_exchange.load_markets()
                    qty = live_exchange.amount_to_precision(symbol, notional_value / current_price)
                    
                    order = live_exchange.create_order(symbol, "market", side, qty)
                    current_price = float(order.get("average", current_price))
                    
                    # Recalculate targets based on live exchange execution price
                    risk_distance = abs(current_price - ob_edge_price)
                    dynamic_tp1 = current_price + (risk_distance * rr_multiplier) if target_direction == "LONG" else current_price - (risk_distance * rr_multiplier)
                except Exception as e:
                    add_log("ERROR", "api_execution", f"Failed LIVE ENTRY for {pair}: {e}")
                    continue
            
            execute_query(
                """
                INSERT INTO futures_trades (
                    account_id, pair, trade_mode, direction, status, entry_price, 
                    margin_usdt, remaining_margin_usdt, notional_usdt, leverage,
                    ob_edge_price, current_sl, dynamic_tp1, watermark_price, opened_at, pnl_usdt
                ) VALUES (?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (acct_id, pair, CURRENT_MODE, target_direction, current_price, margin_usdt, margin_usdt, notional_value, FUTURES_CONFIG["leverage"], 
                 ob_edge_price, ob_edge_price, dynamic_tp1, current_price, now_str, -entry_fee), 
                commit=True
            )
            
            execute_query("UPDATE accounts SET futures_balance_usdt = ? WHERE account_id = ?", (balance - margin_usdt - entry_fee, acct_id), commit=True)
            
            msg = f"[{fresh_acct['display_name']}] {CURRENT_MODE} ENTRY: {target_direction} on {pair} at ${current_price:,.4f} (Margin: ${margin_usdt:.2f} @ {FUTURES_CONFIG['leverage']}x | Risk ~${margin_usdt * FUTURES_CONFIG['leverage'] * stop_fraction:.2f} | Entry fee ${entry_fee:.3f})\n🎯 Risk/Reward Locked: SL @ ${ob_edge_price:.4f} ({stop_fraction*100:.2f}% away) | TP1 @ ${dynamic_tp1:.4f}\n🧠 Trigger: {entry_reason}"
            add_log("INFO", "apextick_bot", msg)
            activity_summary.append(msg)
            break 

    _flush_skip_log()
    return " | ".join(activity_summary) if activity_summary else "Futures Engine: Monitoring SMC Pools."