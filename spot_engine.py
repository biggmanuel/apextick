import math
import time
from datetime import datetime, timezone
import pandas as pd
import ccxt

from config import SPOT_CONFIG, MARKET_SCANNER_CONFIG
from database import execute_query, add_log
from btc_decline_signal import is_trading_allowed_by_btc_gate, MARKET_STATE

# =============================================================================
# CONSTANTS & CONFIGURATION
# =============================================================================
SPOT_ENTRY_COOLDOWN_SECONDS = 180  # 3-minute cooldown between spot entries
STALE_PRICE_THRESHOLD_SECONDS = 300


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_live_price_safely(pair: str) -> float:
    """
    Safely retrieves the current market price for a given pair.
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


def calculate_btc_macro_regime() -> str:
    """
    Calculates the 9-EMA and 21-EMA crossover on real-time BTC 1h klines.
    Spot engine ONLY buys when the macro trend is Bullish or heavily Neutral.
    """
    klines = MARKET_STATE.get("klines_1h", [])
    if len(klines) < 21:
        return "NEUTRAL"

    closes = [float(k["c"]) for k in klines if float(k.get("c", 0)) > 0]
    if len(closes) < 21:
        return "NEUTRAL"

    s = pd.Series(closes)
    ema_9 = float(s.ewm(span=9, adjust=False).mean().iloc[-1])
    ema_21 = float(s.ewm(span=21, adjust=False).mean().iloc[-1])

    if ema_9 > ema_21:
        return "BULLISH"
    elif ema_9 < ema_21:
        return "BEARISH"
    
    return "NEUTRAL"


def is_account_on_spot_cooldown(account_id: str) -> tuple[bool, int]:
    """
    Prevents the Spot engine from deploying all capital into the same 
    millisecond dip by enforcing a time-based cooldown.
    """
    last_trade = execute_query(
        "SELECT opened_at FROM spot_trades WHERE account_id = ? ORDER BY id DESC LIMIT 1",
        (account_id,),
        fetch_one=True
    )
    if not last_trade or not last_trade.get("opened_at"):
        return False, 0

    try:
        last_dt = datetime.fromisoformat(last_trade["opened_at"])
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
            
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        if elapsed < SPOT_ENTRY_COOLDOWN_SECONDS:
            return True, int(SPOT_ENTRY_COOLDOWN_SECONDS - elapsed)
    except Exception:
        pass

    return False, 0


def check_spot_accumulation_zone(pair: str, current_price: float) -> tuple[bool, str]:
    """
    THE INSTITUTIONAL ACCUMULATOR: Replaces the old RSI knife-catching logic.
    Calculates 5-minute VWAP and detects institutional volume spikes.
    Ensures Spot bags are only bought at high-probability mechanical bounce zones.
    """
    try:
        exchange = ccxt.binance({'enableRateLimit': True, 'timeout': 5000})
        ohlcv = exchange.fetch_ohlcv(pair, timeframe='5m', limit=24) # ~2 hours of data
        
        if not ohlcv or len(ohlcv) < 20:
            return False, "Insufficient 5m order-flow data"

        cumulative_vp = 0.0
        cumulative_vol = 0.0
        volumes = []

        # Baseline average calculation
        for k in ohlcv[:-1]: 
            high, low, close, vol = float(k[2]), float(k[3]), float(k[4]), float(k[5])
            typical_price = (high + low + close) / 3.0
            cumulative_vp += (typical_price * vol)
            cumulative_vol += vol
            volumes.append(vol)

        if cumulative_vol == 0:
            return False, "Zero volume baseline"

        vwap = cumulative_vp / cumulative_vol
        avg_vol = sum(volumes) / len(volumes)

        # Catch active or immediate post-dip volume
        recent_vol = max(float(ohlcv[-1][5]), float(ohlcv[-2][5]))
        vol_multiplier = recent_vol / avg_vol if avg_vol > 0 else 0

        vwap_tol = MARKET_SCANNER_CONFIG.get("vwap_tolerance_pct", 0.5)
        req_vol_mult = MARKET_SCANNER_CONFIG.get("volume_spike_multiplier", 3.0)

        dist_pct = (abs(current_price - vwap) / vwap) * 100.0

        if dist_pct > vwap_tol:
            return False, f"Price {dist_pct:.2f}% away from VWAP (Needs <= {vwap_tol}%)"
        
        if vol_multiplier < req_vol_mult:
            return False, f"Volume only {vol_multiplier:.1f}x (Needs >= {req_vol_mult}x)"

        # Extra safety for Spot: Only buy if current price is AT or slightly BELOW VWAP (catching the dip)
        if current_price > (vwap * 1.002):
            return False, "Price is slightly above VWAP Demand Zone, waiting for deeper dip."

        return True, f"VWAP Demand Zone ({dist_pct:.2f}% dist) + Volume Spike ({vol_multiplier:.1f}x)"
        
    except Exception as e:
        return False, f"Micro-structure API error: {e}"


# =============================================================================
# MAIN EXECUTION ENGINE
# =============================================================================

def run_spot_scalper_tick() -> str:
    """
    Master loop for the Live Paper Trading Spot Engine.
    Executes tiered scale-outs and high-probability accumulation.
    """
    pairs = execute_query(
        """
        SELECT pair, last_price, change_24h_pct, volume_24h_usd
        FROM trading_pairs
        WHERE status = 'ACTIVE' AND spot_supported = 1
        ORDER BY volume_24h_usd DESC
        """,
        fetch_all=True
    )
    if not pairs:
        return "Execution halted: No active Spot pairs available in dynamic hot list."

    accounts = execute_query("SELECT * FROM accounts WHERE spot_enabled = 1", fetch_all=True)
    if not accounts:
        return "Execution halted: No active Spot accounts enabled."

    activity_summary = []

    # -------------------------------------------------------------------------
    # PHASE 1: MANAGE OPEN POSITIONS (TIERED SCALING)
    # -------------------------------------------------------------------------
    for acct in accounts:
        acct_id = acct["account_id"]
        balance = float(acct["spot_balance_usdt"] or 0.0)

        open_positions = execute_query(
            """
            SELECT id, pair, entry_price, qty, remaining_qty, notional_usdt, profit_usdt, 
                   tp1_hit, tp2_hit, opened_at 
            FROM spot_trades 
            WHERE account_id = ? AND status = 'OPEN'
            """,
            (acct_id,),
            fetch_all=True
        )

        for trade in open_positions:
            pair = trade["pair"]
            current_price = get_live_price_safely(pair)
            if current_price <= 0:
                continue

            entry_price = float(trade["entry_price"])
            if entry_price <= 0:
                continue

            # Calculate exact ROI based on Spot (1x) holding
            profit_pct = ((current_price - entry_price) / entry_price) * 100.0
            
            orig_qty = float(trade["qty"])
            rem_qty = float(trade.get("remaining_qty") or orig_qty)
            accumulated_profit = float(trade.get("profit_usdt") or 0.0)
            
            tp1 = int(trade.get("tp1_hit") or 0)
            tp2 = int(trade.get("tp2_hit") or 0)
            now_str = datetime.now(timezone.utc).isoformat()

            # Check Tiers in descending order to catch massive flash pumps
            if profit_pct >= SPOT_CONFIG["tp3_pct"]:
                # TP3 (+6%): Close remaining bag completely
                sell_notional = rem_qty * current_price
                gross_profit = sell_notional - (rem_qty * entry_price)
                fee_usdt = sell_notional * (SPOT_CONFIG["fee_pct"] / 100.0)
                net_profit_usdt = gross_profit - fee_usdt
                total_profit = accumulated_profit + net_profit_usdt

                execute_query(
                    """
                    UPDATE spot_trades
                    SET status = 'CLOSED', exit_price = ?, remaining_qty = 0, profit_usdt = ?, profit_pct = ?, tp3_hit = 1, closed_at = ?
                    WHERE id = ?
                    """,
                    (current_price, total_profit, profit_pct, now_str, trade["id"]),
                    commit=True
                )
                
                balance += (sell_notional - fee_usdt)
                execute_query("UPDATE accounts SET spot_balance_usdt = ? WHERE account_id = ?", (balance, acct_id), commit=True)

                msg = f"[{acct['display_name']}] SPOT TP3 HIT 🎯: Fully closed {pair} at ${current_price:,.4f} | Final Profit: +${total_profit:.2f}"
                add_log("SUCCESS", "apextick_bot", msg)
                activity_summary.append(msg)
                continue

            elif profit_pct >= SPOT_CONFIG["tp2_pct"] and not tp2:
                # TP2 (+4%): Scale out 40%
                sell_qty = orig_qty * SPOT_CONFIG["tp2_close_ratio"]
                if sell_qty > rem_qty: 
                    sell_qty = rem_qty
                
                sell_notional = sell_qty * current_price
                gross_profit = sell_notional - (sell_qty * entry_price)
                fee_usdt = sell_notional * (SPOT_CONFIG["fee_pct"] / 100.0)
                net_profit_usdt = gross_profit - fee_usdt
                
                new_rem_qty = rem_qty - sell_qty
                total_profit = accumulated_profit + net_profit_usdt

                execute_query(
                    "UPDATE spot_trades SET remaining_qty = ?, profit_usdt = ?, profit_pct = ?, tp2_hit = 1 WHERE id = ?",
                    (new_rem_qty, total_profit, profit_pct, trade["id"]),
                    commit=True
                )
                
                balance += (sell_notional - fee_usdt)
                execute_query("UPDATE accounts SET spot_balance_usdt = ? WHERE account_id = ?", (balance, acct_id), commit=True)
                
                msg = f"[{acct['display_name']}] SPOT TP2 HIT 🚀: Scaled out of {pair} at ${current_price:,.4f} | Locked: +${net_profit_usdt:.2f}"
                add_log("SUCCESS", "apextick_bot", msg)
                activity_summary.append(msg)

            elif profit_pct >= SPOT_CONFIG["tp1_pct"] and not tp1:
                # TP1 (+2%): Scale out 30%
                sell_qty = orig_qty * SPOT_CONFIG["tp1_close_ratio"]
                if sell_qty > rem_qty: 
                    sell_qty = rem_qty
                
                sell_notional = sell_qty * current_price
                gross_profit = sell_notional - (sell_qty * entry_price)
                fee_usdt = sell_notional * (SPOT_CONFIG["fee_pct"] / 100.0)
                net_profit_usdt = gross_profit - fee_usdt
                
                new_rem_qty = rem_qty - sell_qty
                total_profit = accumulated_profit + net_profit_usdt

                execute_query(
                    "UPDATE spot_trades SET remaining_qty = ?, profit_usdt = ?, profit_pct = ?, tp1_hit = 1 WHERE id = ?",
                    (new_rem_qty, total_profit, profit_pct, trade["id"]),
                    commit=True
                )
                
                balance += (sell_notional - fee_usdt)
                execute_query("UPDATE accounts SET spot_balance_usdt = ? WHERE account_id = ?", (balance, acct_id), commit=True)
                
                msg = f"[{acct['display_name']}] SPOT TP1 HIT 💰: Scaled out of {pair} at ${current_price:,.4f} | Locked: +${net_profit_usdt:.2f}"
                add_log("SUCCESS", "apextick_bot", msg)
                activity_summary.append(msg)


    # -------------------------------------------------------------------------
    # PHASE 2: EVALUATE NEW SPOT ENTRIES (MACRO GATE + VWAP SNIPER)
    # -------------------------------------------------------------------------
    
    # 1. Macro Gate Check
    allowed, gate_reason = is_trading_allowed_by_btc_gate()
    if not allowed:
        return " | ".join(activity_summary) if activity_summary else f"Spot entries paused: BTC Gate BLOCK ({gate_reason})"

    # 2. Strict Spot Regime Filter (No buying in a Bear Market)
    btc_macro = calculate_btc_macro_regime()
    buy_pressure = float(MARKET_STATE.get("buy_pressure_pct", 50.0))

    if btc_macro == "BEARISH":
        return " | ".join(activity_summary) if activity_summary else "Spot Accumulation Paused: Macro Trend is BEARISH."
    if btc_macro == "NEUTRAL" and buy_pressure < 50.0:
        return " | ".join(activity_summary) if activity_summary else "Spot Accumulation Paused: Neutral market with weak buying pressure."

    # Load scanner thresholds
    min_mom = MARKET_SCANNER_CONFIG.get("min_momentum_pct", 3.0)
    max_ext = MARKET_SCANNER_CONFIG.get("max_overextended_pct", 25.0)

    # 3. Account-Level Execution
    for acct in accounts:
        acct_id = acct["account_id"]
        
        fresh_acct = execute_query("SELECT * FROM accounts WHERE account_id = ?", (acct_id,), fetch_one=True)
        balance = float(fresh_acct["spot_balance_usdt"] or 0.0)
        cap = float(fresh_acct.get("spot_capital_cap") or 999999.0)
        available_usdt = min(balance, cap)

        on_cooldown, rem_sec = is_account_on_spot_cooldown(acct_id)
        if on_cooldown:
            continue

        open_positions = execute_query(
            "SELECT pair FROM spot_trades WHERE account_id = ? AND status = 'OPEN'",
            (acct_id,),
            fetch_all=True
        )
        held_pairs = {p["pair"] for p in open_positions}

        if len(held_pairs) >= SPOT_CONFIG["max_open_positions"]:
            continue

        if available_usdt < SPOT_CONFIG["min_trade_usdt"]:
            continue

        trade_usdt = max(
            available_usdt * (SPOT_CONFIG["trade_size_pct"] / 100.0),
            SPOT_CONFIG["min_trade_usdt"]
        )
        trade_usdt = min(trade_usdt, available_usdt)

        # 4. Filter and Snipe (Iterate Dynamic Hot List)
        for p_info in pairs:
            pair = p_info["pair"]
            
            # Ban the heavy legacy coins to maximize spot ROI potential
            if pair in ["BTC/USDT", "ETH/USDT", "BNB/USDT"]:
                continue
                
            if pair in held_pairs:
                continue

            chg_24h = float(p_info.get("change_24h_pct") or 0.0)
            
            # Ensure the coin is actively trending up, but not massively overbought
            if not (min_mom <= chg_24h <= max_ext):
                continue

            current_price = get_live_price_safely(pair)
            if current_price <= 0:
                continue

            # --- THE ACCUMULATOR TRIGGER (VWAP & Volume Anomaly) ---
            passed_micro, micro_reason = check_spot_accumulation_zone(pair, current_price)
            if not passed_micro:
                continue

            qty = trade_usdt / current_price
            now_str = datetime.now(timezone.utc).isoformat()

            # Execute Paper Trade
            execute_query(
                """
                INSERT INTO spot_trades (
                    account_id, pair, status, entry_price, qty, remaining_qty, notional_usdt, opened_at
                ) VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?)
                """,
                (acct_id, pair, current_price, qty, qty, trade_usdt, now_str),
                commit=True
            )

            new_bal = balance - trade_usdt
            execute_query(
                "UPDATE accounts SET spot_balance_usdt = ? WHERE account_id = ?",
                (new_bal, acct_id),
                commit=True
            )

            msg = (
                f"[{fresh_acct['display_name']}] SPOT PAPER ENTRY: Bought {pair} at ${current_price:,.4f} (${trade_usdt:.2f})\n"
                f"🎯 Sniper Logic: {micro_reason} | 24h: +{chg_24h:.2f}%"
            )
            add_log("INFO", "apextick_bot", msg)
            activity_summary.append(msg)
            
            # Break after executing one trade to respect account cooldown limits
            break  

    return " | ".join(activity_summary) if activity_summary else "Spot engine ticked: Holding active positions."