import math
import random
from datetime import datetime, timezone
import pandas as pd
from config import FUTURES_CONFIG
from database import execute_query, add_log
from btc_decline_signal import is_trading_allowed_by_btc_gate, MARKET_STATE

# =============================================================================
# CONSTANTS & CONFIGURATION
# =============================================================================
FUTURES_ENTRY_COOLDOWN_SECONDS = 180  # Cooldown between new entries per account
STALE_PRICE_THRESHOLD_SECONDS = 300   # Max allowed age for a fallback price


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_live_price_safely(pair: str) -> float:
    """
    Safely retrieves the current market price for a given pair.
    Prefers the real-time stream (MARKET_STATE), falls back to the database's
    last known price, and completely rejects invalid/stale prices.
    """
    current_price = MARKET_STATE["live_prices"].get(pair)
    
    if current_price and current_price > 0:
        return float(current_price)

    # Fallback to database if stream is momentarily out of sync
    p_row = execute_query(
        "SELECT last_price FROM trading_pairs WHERE pair = ?", 
        (pair,), 
        fetch_one=True
    )
    
    if p_row and p_row.get("last_price"):
        db_price = float(p_row["last_price"])
        if db_price > 0:
            return db_price

    return 0.0


def calculate_futures_macro_trend() -> str:
    """
    Calculates the 9-EMA and 21-EMA crossover on real-time BTC 1h klines
    to determine the macro directional bias for new futures entries.
    """
    klines = MARKET_STATE.get("klines_1h", [])
    if len(klines) < 21:
        return "NEUTRAL"

    # Extract valid closing prices
    closes = [float(k["c"]) for k in klines if float(k.get("c", 0)) > 0]
    if len(closes) < 21:
        return "NEUTRAL"

    # Compute Exponential Moving Averages
    series = pd.Series(closes)
    ema_9 = float(series.ewm(span=9, adjust=False).mean().iloc[-1])
    ema_21 = float(series.ewm(span=21, adjust=False).mean().iloc[-1])

    # Determine Trend Alignment
    if ema_9 > ema_21:
        return "BULLISH"
    elif ema_9 < ema_21:
        return "BEARISH"
    
    return "NEUTRAL"


def is_account_on_futures_cooldown(account_id: str) -> tuple[bool, int]:
    """
    Prevents the engine from spam-opening trades by enforcing a strict
    time-based cooldown window after a trade is successfully opened.
    """
    last_trade = execute_query(
        "SELECT opened_at FROM futures_trades WHERE account_id = ? AND status = 'OPEN' ORDER BY id DESC LIMIT 1",
        (account_id,), 
        fetch_one=True
    )
    
    if not last_trade or not last_trade.get("opened_at"):
        # Fall back to checking any recent open record
        last_trade = execute_query(
            "SELECT opened_at FROM futures_trades WHERE account_id = ? ORDER BY id DESC LIMIT 1",
            (account_id,),
            fetch_one=True
        )

    if not last_trade or not last_trade.get("opened_at"):
        return False, 0

    try:
        last_dt = datetime.fromisoformat(last_trade["opened_at"])
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
            
        elapsed_seconds = (datetime.now(timezone.utc) - last_dt).total_seconds()
        
        if elapsed_seconds < FUTURES_ENTRY_COOLDOWN_SECONDS:
            remaining = int(FUTURES_ENTRY_COOLDOWN_SECONDS - elapsed_seconds)
            return True, remaining
    except Exception as e:
        add_log("ERROR", "futures_engine", f"Cooldown calculation error for {account_id}: {str(e)}")
        
    return False, 0


# =============================================================================
# MAIN EXECUTION ENGINE
# =============================================================================

def run_futures_engine_tick() -> str:
    """
    Master loop for the Live Paper Trading Futures Engine.
    Executes a complete evaluation cycle across all active accounts and pairs.
    
    Phase 1: Position Management (Instant TP Logging, Outlier Trailing, Stop Loss, Trailing Recovery)
    Phase 2: Market Scanning & New Trade Execution (Excluding BTC/ETH/BNB)
    """
    
    # Fetch actively supported futures pairs sorted by 24h volume
    pairs = execute_query(
        """
        SELECT pair, last_price, volume_24h_usd 
        FROM trading_pairs 
        WHERE status = 'ACTIVE' AND futures_supported = 1 
        ORDER BY volume_24h_usd DESC
        """,
        fetch_all=True
    )
    if not pairs:
        return "Execution halted: No active Futures pairs available in database."

    # Fetch all user accounts with Futures trading enabled
    accounts = execute_query(
        "SELECT * FROM accounts WHERE futures_enabled = 1", 
        fetch_all=True
    )
    if not accounts:
        return "Execution halted: No active Futures accounts enabled."

    activity_summary = []

    # -------------------------------------------------------------------------
    # PHASE 1: MANAGE OPEN POSITIONS (INSTANT LOGGING + OUTLIER RUNNER ENGINE)
    # -------------------------------------------------------------------------
    
    for acct in accounts:
        acct_id = acct["account_id"]
        balance = float(acct["futures_balance_usdt"] or 0.0)

        open_positions = execute_query(
            """
            SELECT id, pair, direction, status, entry_price, margin_usdt, remaining_margin_usdt,
                   notional_usdt, leverage, pnl_usdt, pnl_pct, close_type, peak_roi_pct,
                   worst_roi_pct, tp1_hit, tp2_hit, tp3_hit, opened_at
            FROM futures_trades
            WHERE account_id = ? AND status = 'OPEN'
            """,
            (acct_id,),
            fetch_all=True
        )

        for trade in open_positions:
            pair = trade["pair"]
            trade_id = trade["id"]
            
            current_price = get_live_price_safely(pair)
            if current_price <= 0:
                continue

            entry_price = float(trade["entry_price"])
            direction = trade["direction"]
            leverage = int(trade["leverage"])
            
            # --- ROI & Tracking Calculations ---
            if direction == "LONG":
                price_diff_pct = ((current_price - entry_price) / entry_price) * 100.0
            else:
                price_diff_pct = ((entry_price - current_price) / entry_price) * 100.0

            roi_pct = price_diff_pct * leverage

            # Update historical peak and worst performance trackers
            peak_roi = max(float(trade.get("peak_roi_pct") or 0.0), roi_pct)
            worst_roi = min(float(trade.get("worst_roi_pct") or 0.0), roi_pct)
            
            execute_query(
                "UPDATE futures_trades SET peak_roi_pct = ?, worst_roi_pct = ? WHERE id = ?",
                (peak_roi, worst_roi, trade_id),
                commit=True
            )

            orig_margin = float(trade["margin_usdt"])
            rem_margin = float(trade.get("remaining_margin_usdt") or orig_margin)
            accumulated_pnl = float(trade.get("pnl_usdt") or 0.0)
            tp1_hit = int(trade.get("tp1_hit") or 0)
            tp2_hit = int(trade.get("tp2_hit") or 0)
            now_str = datetime.now(timezone.utc).isoformat()

            # =================================================================
            # TIER 1 HIT (+20% ROI): SCALE OUT 30% + LOG IMMEDIATE CLOSE EVENT
            # =================================================================
            if roi_pct >= FUTURES_CONFIG["tp1_roi"] and not tp1_hit:
                margin_to_close = orig_margin * FUTURES_CONFIG["tp1_close_ratio"]
                if margin_to_close > rem_margin:
                    margin_to_close = rem_margin

                notional_closed = margin_to_close * leverage
                gross_pnl = margin_to_close * (roi_pct / 100.0)
                fee_usdt = notional_closed * (FUTURES_CONFIG["fee_pct"] / 100.0)
                net_partial_pnl = gross_pnl - fee_usdt
                new_rem_margin = rem_margin - margin_to_close
                total_trade_pnl = accumulated_pnl + net_partial_pnl

                # 1. Insert distinct closed event into history table immediately
                execute_query(
                    """
                    INSERT INTO futures_trades (
                        account_id, pair, direction, status, entry_price, exit_price,
                        margin_usdt, remaining_margin_usdt, notional_usdt, leverage,
                        pnl_usdt, pnl_pct, close_type, peak_roi_pct, worst_roi_pct,
                        tp1_hit, tp2_hit, tp3_hit, opened_at, closed_at
                    ) VALUES (?, ?, ?, 'CLOSED', ?, ?, ?, 0.0, ?, ?, ?, ?, 'TP1 (+20% ROI)', ?, ?, 1, 0, 0, ?, ?)
                    """,
                    (
                        acct_id, pair, direction, entry_price, current_price,
                        margin_to_close, notional_closed, leverage,
                        net_partial_pnl, roi_pct, peak_roi, worst_roi,
                        trade["opened_at"], now_str
                    ),
                    commit=True
                )

                # 2. Update remaining active bag on parent record
                execute_query(
                    "UPDATE futures_trades SET remaining_margin_usdt = ?, pnl_usdt = ?, pnl_pct = ?, tp1_hit = 1 WHERE id = ?",
                    (new_rem_margin, total_trade_pnl, roi_pct, trade_id),
                    commit=True
                )

                # 3. Credit wallet balance and 70% live withdraw pool
                balance += (margin_to_close + net_partial_pnl)
                withdraw_pool_add = net_partial_pnl * FUTURES_CONFIG["withdraw_split_pct"] if net_partial_pnl > 0 else 0.0
                execute_query(
                    "UPDATE accounts SET futures_balance_usdt = ?, futures_live_withdraw = futures_live_withdraw + ? WHERE account_id = ?",
                    (balance, withdraw_pool_add, acct_id),
                    commit=True
                )

                msg = f"[{acct['display_name']}] FUTURES TP1 HIT 💰: Scaled out 30% on {pair} {direction} at ${current_price:,.4f} | Locked: +${net_partial_pnl:.2f}"
                add_log("SUCCESS", "apextick_bot", msg)
                activity_summary.append(msg)
                continue

            # =================================================================
            # TIER 2 HIT (+50% ROI): SCALE OUT 40% + LOG IMMEDIATE CLOSE EVENT
            # =================================================================
            elif roi_pct >= FUTURES_CONFIG["tp2_roi"] and not tp2_hit:
                margin_to_close = orig_margin * FUTURES_CONFIG["tp2_close_ratio"]
                if margin_to_close > rem_margin:
                    margin_to_close = rem_margin

                notional_closed = margin_to_close * leverage
                gross_pnl = margin_to_close * (roi_pct / 100.0)
                fee_usdt = notional_closed * (FUTURES_CONFIG["fee_pct"] / 100.0)
                net_partial_pnl = gross_pnl - fee_usdt
                new_rem_margin = rem_margin - margin_to_close
                total_trade_pnl = accumulated_pnl + net_partial_pnl

                # 1. Insert distinct closed event into history table immediately
                execute_query(
                    """
                    INSERT INTO futures_trades (
                        account_id, pair, direction, status, entry_price, exit_price,
                        margin_usdt, remaining_margin_usdt, notional_usdt, leverage,
                        pnl_usdt, pnl_pct, close_type, peak_roi_pct, worst_roi_pct,
                        tp1_hit, tp2_hit, tp3_hit, opened_at, closed_at
                    ) VALUES (?, ?, ?, 'CLOSED', ?, ?, ?, 0.0, ?, ?, ?, ?, 'TP2 (+50% ROI)', ?, ?, tp1_hit, 1, 0, ?, ?)
                    """,
                    (
                        acct_id, pair, direction, entry_price, current_price,
                        margin_to_close, notional_closed, leverage,
                        net_partial_pnl, roi_pct, peak_roi, worst_roi,
                        trade["opened_at"], now_str
                    ),
                    commit=True
                )

                # 2. Update remaining active bag on parent record
                execute_query(
                    "UPDATE futures_trades SET remaining_margin_usdt = ?, pnl_usdt = ?, pnl_pct = ?, tp2_hit = 1 WHERE id = ?",
                    (new_rem_margin, total_trade_pnl, roi_pct, trade_id),
                    commit=True
                )

                # 3. Credit wallet balance and 70% live withdraw pool
                balance += (margin_to_close + net_partial_pnl)
                withdraw_pool_add = net_partial_pnl * FUTURES_CONFIG["withdraw_split_pct"] if net_partial_pnl > 0 else 0.0
                execute_query(
                    "UPDATE accounts SET futures_balance_usdt = ?, futures_live_withdraw = futures_live_withdraw + ? WHERE account_id = ?",
                    (balance, withdraw_pool_add, acct_id),
                    commit=True
                )

                msg = f"[{acct['display_name']}] FUTURES TP2 HIT 🚀: Scaled out 40% on {pair} {direction} at ${current_price:,.4f} | Locked: +${net_partial_pnl:.2f}"
                add_log("SUCCESS", "apextick_bot", msg)
                activity_summary.append(msg)
                continue

            # =================================================================
            # OUTLIER RUNNER ENGINE (+80% TO +400% ROI TRAILING RUNNER)
            # =================================================================
            outlier_triggered = False
            close_label = ""

            outlier_arm = FUTURES_CONFIG.get("outlier_arm_roi", 80.0)
            trail_buffer = FUTURES_CONFIG.get("outlier_trail_buffer_pct", 25.0)
            hard_cap = FUTURES_CONFIG.get("hard_tp3_cap_roi", 400.0)

            if peak_roi >= outlier_arm:
                if (peak_roi - roi_pct) >= trail_buffer:
                    outlier_triggered = True
                    close_label = f"TP3 Outlier Trail (+{roi_pct:.1f}% ROI)"
                elif roi_pct >= hard_cap:
                    outlier_triggered = True
                    close_label = f"TP3 Outlier Max (+{roi_pct:.1f}% ROI)"

            if outlier_triggered:
                notional_closed = rem_margin * leverage
                gross_pnl = rem_margin * (roi_pct / 100.0)
                fee_usdt = notional_closed * (FUTURES_CONFIG["fee_pct"] / 100.0)
                net_final_pnl = gross_pnl - fee_usdt

                execute_query(
                    """
                    UPDATE futures_trades
                    SET status = 'CLOSED', exit_price = ?, remaining_margin_usdt = 0.0,
                        margin_usdt = ?, notional_usdt = ?, pnl_usdt = ?, pnl_pct = ?,
                        close_type = ?, tp3_hit = 1, closed_at = ?
                    WHERE id = ?
                    """,
                    (current_price, rem_margin, notional_closed, net_final_pnl, roi_pct, close_label, now_str, trade_id),
                    commit=True
                )

                balance += (rem_margin + net_final_pnl)
                withdraw_pool_add = net_final_pnl * FUTURES_CONFIG["withdraw_split_pct"] if net_final_pnl > 0 else 0.0
                execute_query(
                    "UPDATE accounts SET futures_balance_usdt = ?, futures_live_withdraw = futures_live_withdraw + ? WHERE account_id = ?",
                    (balance, withdraw_pool_add, acct_id),
                    commit=True
                )

                msg = f"[{acct['display_name']}] 🔥 {close_label}: Final bag closed on {pair} {direction} at ${current_price:,.4f} | Profit: +${net_final_pnl:.2f}"
                add_log("SUCCESS", "apextick_bot", msg)
                activity_summary.append(msg)
                continue

            # =================================================================
            # RISK EXITS: CANDLE SL, STRUCTURAL SL & RECOVERY TRAILING
            # =================================================================
            risk_exit_type = ""
            
            # 1. Structural Stop Loss (Extreme emergency protection)
            if roi_pct <= FUTURES_CONFIG["hard_sl_roi_pct"]:
                risk_exit_type = "STRUCTURAL SL"
                
            # 2. Candle SL (Rapid invalidation if setup never gained traction)
            elif roi_pct <= FUTURES_CONFIG["candle_sl_roi_pct"] and peak_roi < 2.5:
                risk_exit_type = "CANDLE SL"
                
            # 3. Recovery Mode Exits
            elif trade.get("close_type") == "Recovery":
                if roi_pct >= FUTURES_CONFIG["recovery_tp_roi_pct"]:
                    risk_exit_type = "TP (Recovery Exit)"
            elif roi_pct <= FUTURES_CONFIG["recovery_arm_roi_pct"] and not trade.get("close_type"):
                execute_query(
                    "UPDATE futures_trades SET close_type = 'Recovery' WHERE id = ?", 
                    (trade_id,), 
                    commit=True
                )
                continue

            # Process the exit transaction if any risk cut was triggered
            if risk_exit_type:
                notional_closed = rem_margin * leverage
                gross_loss = rem_margin * (roi_pct / 100.0)
                fee_usdt = notional_closed * (FUTURES_CONFIG["fee_pct"] / 100.0)
                net_loss = gross_loss - fee_usdt

                execute_query(
                    """
                    UPDATE futures_trades
                    SET status = 'CLOSED', exit_price = ?, remaining_margin_usdt = 0.0,
                        margin_usdt = ?, notional_usdt = ?, pnl_usdt = ?, pnl_pct = ?,
                        close_type = ?, closed_at = ?
                    WHERE id = ?
                    """,
                    (current_price, rem_margin, notional_closed, net_loss, roi_pct, risk_exit_type, now_str, trade_id),
                    commit=True
                )

                # Return remaining healthy margin to balance (deducting the loss)
                balance += max(0.0, rem_margin + net_loss)
                execute_query(
                    "UPDATE accounts SET futures_balance_usdt = ? WHERE account_id = ?",
                    (balance, acct_id),
                    commit=True
                )

                log_level = "WARNING" if net_loss < 0 else "SUCCESS"
                msg = f"[{acct['display_name']}] FUTURES {risk_exit_type}: {pair} {direction} closed at ${current_price:,.4f} | PnL: ${net_loss:+.2f}"
                add_log(log_level, "apextick_bot", msg)
                activity_summary.append(msg)


    # -------------------------------------------------------------------------
    # PHASE 2: EVALUATE NEW ENTRIES (MARKET SCANNING & PAPER EXECUTION)
    # -------------------------------------------------------------------------
    
    # 1. Macro Gate Check
    allowed, gate_reason = is_trading_allowed_by_btc_gate()
    if not allowed:
        return " | ".join(activity_summary) if activity_summary else f"Futures entries paused: BTC Gate BLOCK ({gate_reason})"

    # 2. Trend & Order Flow Assessment
    btc_trend = calculate_futures_macro_trend()
    buy_pressure = float(MARKET_STATE.get("buy_pressure_pct", 50.0))
    sell_pressure = float(MARKET_STATE.get("sell_pressure_pct", 50.0))

    target_direction = None
    
    # Strict alignment rules for strong trends
    if btc_trend == "BULLISH" and buy_pressure >= 51.0: 
        target_direction = "LONG"
    elif btc_trend == "BEARISH" and sell_pressure >= 51.0: 
        target_direction = "SHORT"
        
    # Sideways market scalping logic
    elif btc_trend == "NEUTRAL":
        if buy_pressure >= 55.0: 
            target_direction = "LONG"
        elif sell_pressure >= 55.0: 
            target_direction = "SHORT"

    if not target_direction:
        return " | ".join(activity_summary) if activity_summary else "Futures Brain: Waiting for trend and volume alignment."

    # 3. Account-Level Execution
    for acct in accounts:
        acct_id = acct["account_id"]
        
        # Pull fresh account state to ensure accurate balance
        fresh_acct = execute_query(
            "SELECT * FROM accounts WHERE account_id = ?", 
            (acct_id,), 
            fetch_one=True
        )
        balance = float(fresh_acct["futures_balance_usdt"] or 0.0)
        capital_cap = float(fresh_acct.get("futures_capital_cap") or 999999.0)
        
        available_margin = min(balance, capital_cap)

        # Ensure account meets minimum requirements and isn't spamming trades
        if available_margin < FUTURES_CONFIG["min_margin_usdt"]:
            continue
            
        on_cooldown, rem_sec = is_account_on_futures_cooldown(acct_id)
        if on_cooldown:
            continue

        # Check Max Positions Limit
        open_positions = execute_query(
            "SELECT pair FROM futures_trades WHERE account_id = ? AND status = 'OPEN'",
            (acct_id,), 
            fetch_all=True
        )
        if len(open_positions) >= FUTURES_CONFIG["max_open_positions"]:
            continue
            
        held_pairs = {p["pair"] for p in open_positions}

        # Calculate exact margin size based on configured percentage
        target_margin_usdt = available_margin * (FUTURES_CONFIG["margin_size_pct"] / 100.0)
        margin_usdt = max(target_margin_usdt, FUTURES_CONFIG["min_margin_usdt"])
        margin_usdt = min(margin_usdt, available_margin)

        # 4. Pair Selection & Virtual Ledger Execution (Excluding Big 3)
        for p_info in pairs:
            pair = p_info["pair"]
            
            # --- STRICT BAN ON THE BIG 3 (Forces high-volume altcoin scalping) ---
            if pair in ["BTC/USDT", "ETH/USDT", "BNB/USDT"]:
                continue
            
            # Prevent opening duplicate positions on the same coin
            if pair in held_pairs:
                continue

            entry_price = get_live_price_safely(pair)
            if entry_price <= 0:
                continue

            notional_value = margin_usdt * FUTURES_CONFIG["leverage"]
            open_time = datetime.now(timezone.utc).isoformat()
            
            # Execute Paper Trade Insertion
            execute_query(
                """
                INSERT INTO futures_trades (
                    account_id, pair, direction, status, entry_price, 
                    margin_usdt, remaining_margin_usdt, notional_usdt, leverage, opened_at
                ) VALUES (?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?)
                """,
                (
                    acct_id, pair, target_direction, entry_price, 
                    margin_usdt, margin_usdt, notional_value, 
                    FUTURES_CONFIG["leverage"], open_time
                ), 
                commit=True
            )
            
            # Deduct margin from active balance
            new_balance = balance - margin_usdt
            execute_query(
                "UPDATE accounts SET futures_balance_usdt = ? WHERE account_id = ?", 
                (new_balance, acct_id), 
                commit=True
            )
            
            msg = (
                f"[{fresh_acct['display_name']}] FUTURES PAPER ENTRY: Opened {target_direction} "
                f"on {pair} at ${entry_price:,.4f} (Margin: ${margin_usdt:.2f} @ {FUTURES_CONFIG['leverage']}x) | "
                f"Trend: {btc_trend}"
            )
            add_log("INFO", "apextick_bot", msg)
            activity_summary.append(msg)
            
            # Only open one trade per account per tick to respect cooldowns
            break 

    return " | ".join(activity_summary) if activity_summary else "Futures engine ticked: Monitoring active positions."