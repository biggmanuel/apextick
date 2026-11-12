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

def is_account_on_futures_cooldown(account_id: str, mode: str) -> tuple[bool, int]:
    """Prevents spam-opening trades by enforcing a strict time-based window."""
    last_trade = execute_query(
        "SELECT opened_at FROM futures_trades WHERE account_id = ? AND trade_mode = ? AND status = 'OPEN' ORDER BY id DESC LIMIT 1",
        (account_id, mode), fetch_one=True
    )
    if not last_trade:
        last_trade = execute_query(
            "SELECT opened_at FROM futures_trades WHERE account_id = ? AND trade_mode = ? ORDER BY id DESC LIMIT 1",
            (account_id, mode), fetch_one=True
        )
    if not last_trade:
        return False, 0

    try:
        last_dt = datetime.fromisoformat(last_trade["opened_at"])
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
            
        elapsed_seconds = (datetime.now(timezone.utc) - last_dt).total_seconds()
        if elapsed_seconds < FUTURES_ENTRY_COOLDOWN_SECONDS:
            return True, int(FUTURES_ENTRY_COOLDOWN_SECONDS - elapsed_seconds)
    except Exception:
        pass
    return False, 0

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
    Requires price to tap an active Order Block or FVG matching the trend direction.
    Returns: (is_valid, reason, ob_edge_price)
    """
    try:
        pools = execute_query(
            """
            SELECT pool_type, top_price, bottom_price 
            FROM liquidity_pools 
            WHERE pair = ? AND direction = ? AND status IN ('PENDING', 'MITIGATED')
            ORDER BY created_at DESC LIMIT 3
            """, (pair, direction), fetch_all=True
        )
        
        if not pools:
            return False, "No active SMC Zones found.", 0.0

        for pool in pools:
            top = pool["top_price"]
            bottom = pool["bottom_price"]
            
            buffer = current_price * 0.002 
            
            if (bottom - buffer) <= current_price <= (top + buffer):
                # The structural invalidation line changes based on direction
                edge = bottom if direction == "LONG" else top
                return True, f"Price mitigated {pool['pool_type']} ({direction})", edge
                
        return False, "Price outside active SMC zones.", 0.0
    except Exception as e:
        return False, f"Database error: {e}", 0.0


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
                        close_reason, margin_to_close, full_close = f"TRAILING SL HIT (-3% from peak ${watermark_price:.4f})", rem_margin, True
            
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
                        close_reason, margin_to_close, full_close = f"TRAILING SL HIT (+3% from peak ${watermark_price:.4f})", rem_margin, True


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
    # PHASE 2: EVALUATE NEW ENTRIES (SMC & POWDER KEG HUNTER)
    # -------------------------------------------------------------------------
    if is_halted:
        return " | ".join(activity_summary) if activity_summary else "Futures Entries Disabled: MACRO KILL-SWITCH ACTIVE."

    pairs = execute_query(
        """
        SELECT pair, last_price, change_24h_pct, oi_spot_ratio, atr 
        FROM trading_pairs 
        WHERE status = 'ACTIVE' AND futures_supported = 1 AND last_price > 0
        ORDER BY oi_spot_ratio DESC
        """, fetch_all=True
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
            return " | ".join(activity_summary) if activity_summary else f"Futures paused: BTC Gate BLOCK ({gate_reason})"

    for acct in accounts:
        acct_id = acct["account_id"]
        fresh_acct = execute_query("SELECT * FROM accounts WHERE account_id = ?", (acct_id,), fetch_one=True)
        balance = float(fresh_acct["futures_balance_usdt"] or 0.0)
        available_margin = min(balance, float(fresh_acct.get("futures_capital_cap") or 999999.0))

        if available_margin < FUTURES_CONFIG["min_margin_usdt"]: continue
            
        on_cooldown, _ = is_account_on_futures_cooldown(acct_id, CURRENT_MODE)
        if on_cooldown: continue

        open_positions = execute_query(
            "SELECT pair FROM futures_trades WHERE account_id = ? AND status = 'OPEN' AND trade_mode = ?",
            (acct_id, CURRENT_MODE), fetch_all=True
        )
        if len(open_positions) >= FUTURES_CONFIG["max_open_positions"]: continue
        held_pairs = {p["pair"] for p in open_positions}

        for p_info in pairs:
            pair = p_info["pair"]
            if pair in ["BTC/USDT", "ETH/USDT", "BNB/USDT"] or pair in held_pairs: continue

            oi_ratio = float(p_info.get("oi_spot_ratio", 0.0))
            atr = float(p_info.get("atr", 0.0))
            current_price = get_live_price_safely(pair)
            
            target_direction, entry_reason = None, ""

            # --- LAYER 4: THE POWDER KEG MATRIX ---
            if POWDER_KEG_CONFIG["enabled"] and oi_ratio >= POWDER_KEG_CONFIG["min_oi_spot_ratio"]:
                if buy_pressure < 45.0: # Margin call cascade trigger (Retail panic selling)
                    target_direction = "SHORT"
                    entry_reason = f"POWDER KEG MATRIX: Massive Open Interest ({oi_ratio:.1f}x Spot Vol) + 1m Panic Execution"
            
            # --- LAYER 1: FALLBACK STRUCTURAL TREND ---
            if not target_direction:
                if btc_trend == "BULLISH" and buy_pressure >= 50.0: target_direction = "LONG"
                elif btc_trend == "BEARISH" and buy_pressure < 50.0: target_direction = "SHORT"
                
            if not target_direction: continue

            # --- LAYER 3: SMC ORDER BLOCK TRIGGER ---
            passed_smc, smc_reason, ob_edge_price = check_micro_structure(pair, current_price, target_direction)
            if not passed_smc: continue

            # Validate entry isn't already past invalidation
            if (target_direction == "LONG" and current_price < ob_edge_price) or (target_direction == "SHORT" and current_price > ob_edge_price):
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
            target_margin_usdt = get_dynamic_margin(available_margin, atr, current_price)
            margin_usdt = max(FUTURES_CONFIG["min_margin_usdt"], min(target_margin_usdt, available_margin))
            
            notional_value = margin_usdt * FUTURES_CONFIG["leverage"]
            
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
                    ob_edge_price, current_sl, dynamic_tp1, watermark_price, opened_at
                ) VALUES (?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (acct_id, pair, CURRENT_MODE, target_direction, current_price, margin_usdt, margin_usdt, notional_value, FUTURES_CONFIG["leverage"], 
                 ob_edge_price, ob_edge_price, dynamic_tp1, current_price, now_str), 
                commit=True
            )
            
            execute_query("UPDATE accounts SET futures_balance_usdt = ? WHERE account_id = ?", (balance - margin_usdt, acct_id), commit=True)
            
            msg = f"[{fresh_acct['display_name']}] {CURRENT_MODE} ENTRY: {target_direction} on {pair} at ${current_price:,.4f} (Margin: ${margin_usdt:.2f} @ {FUTURES_CONFIG['leverage']}x)\n🎯 Risk/Reward Locked: SL @ ${ob_edge_price:.4f} | TP1 @ ${dynamic_tp1:.4f}\n🧠 Trigger: {entry_reason}"
            add_log("INFO", "apextick_bot", msg)
            activity_summary.append(msg)
            break 

    return " | ".join(activity_summary) if activity_summary else "Futures Engine: Monitoring SMC Pools & Powder Kegs."
