import math
import time
from datetime import datetime, timezone
import pandas as pd
import ccxt

# IMPORT THE NEW CONFIG DICTIONARIES
from config import SPOT_CONFIG, MARKET_SCANNER_CONFIG, DYNAMIC_RISK_CONFIG, BINANCE_API_KEY, BINANCE_SECRET_KEY, exchange as live_exchange
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
    current_price = MARKET_STATE["live_prices"].get(pair)
    if current_price and current_price > 0:
        return float(current_price)

    p_row = execute_query("SELECT last_price FROM trading_pairs WHERE pair = ?", (pair,), fetch_one=True)
    if p_row and p_row.get("last_price"):
        db_price = float(p_row["last_price"])
        if db_price > 0:
            return db_price
    return 0.0

def is_account_on_spot_cooldown(account_id: str, mode: str) -> tuple[bool, int]:
    last_trade = execute_query(
        "SELECT opened_at FROM spot_trades WHERE account_id = ? AND trade_mode = ? ORDER BY id DESC LIMIT 1",
        (account_id, mode), fetch_one=True
    )
    if not last_trade: return False, 0
    
    try:
        last_dt = datetime.fromisoformat(last_trade["opened_at"])
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        if elapsed < SPOT_ENTRY_COOLDOWN_SECONDS:
            return True, int(SPOT_ENTRY_COOLDOWN_SECONDS - elapsed)
    except Exception: pass
    return False, 0

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
    LAYER 3 SMC FILTER: 
    Queries the database to see if the live price is mitigating a Bullish Order Block.
    Returns: (is_valid, reason, ob_edge_price)
    """
    try:
        pools = execute_query(
            """
            SELECT pool_type, top_price, bottom_price 
            FROM liquidity_pools 
            WHERE pair = ? AND direction = 'BULLISH' AND status IN ('PENDING', 'MITIGATED')
            ORDER BY created_at DESC LIMIT 3
            """, (pair,), fetch_all=True
        )
        
        if not pools:
            return False, "No active Smart Money Support Blocks found.", 0.0

        for pool in pools:
            top = pool["top_price"]
            bottom = pool["bottom_price"] 
            
            buffer = current_price * 0.002 
            
            if (bottom - buffer) <= current_price <= (top + buffer):
                return True, f"Price mitigated {pool['pool_type']} Demand Zone (${bottom:.4f} - ${top:.4f})", bottom
                
        return False, f"Price (${current_price:.4f}) is outside active SMC zones.", 0.0
    except Exception as e:
        return False, f"SMC Database error: {e}", 0.0


# =============================================================================
# MAIN EXECUTION ENGINE
# =============================================================================

def run_spot_scalper_tick() -> str:
    """Master loop targeting SMC Accumulation and 100% Target Execution."""
    
    # 1. READ GOD-MODE STATE
    mode_row = execute_query("SELECT value FROM bot_settings WHERE key = 'paper_trading_enabled'", fetch_one=True)
    is_paper = (mode_row["value"] == "1") if mode_row else True
    CURRENT_MODE = "PAPER" if is_paper else "LIVE"

    halt_row = execute_query("SELECT value FROM bot_settings WHERE key = 'macro_halt'", fetch_one=True)
    is_halted = (halt_row["value"] == "1") if halt_row else False

    accounts = execute_query("SELECT * FROM accounts WHERE spot_enabled = 1", fetch_all=True)
    if not accounts: return "Execution halted: No active Spot accounts enabled."

    activity_summary = []
    now_str = datetime.now(timezone.utc).isoformat()

    # -------------------------------------------------------------------------
    # PHASE 1: MANAGE OPEN POSITIONS (ACCUMULATOR LOGIC, 100% TP CLOSE)
    # -------------------------------------------------------------------------
    for acct in accounts:
        acct_id = acct["account_id"]
        balance = float(acct["spot_balance_usdt"] or 0.0)

        open_positions = execute_query(
            """
            SELECT * FROM spot_trades 
            WHERE account_id = ? AND status = 'OPEN' AND trade_mode = ?
            """, (acct_id, CURRENT_MODE), fetch_all=True
        )

        for trade in open_positions:
            pair = trade["pair"]
            trade_id = trade["id"]
            
            current_price = get_live_price_safely(pair)
            if current_price <= 0: continue

            entry_price = float(trade["entry_price"])
            profit_pct = ((current_price - entry_price) / entry_price) * 100.0
            
            orig_qty = float(trade["qty"])
            rem_qty = float(trade.get("remaining_qty") or orig_qty)
            accumulated_profit = float(trade.get("profit_usdt") or 0.0)
            
            tp1_hit = int(trade.get("tp1_hit") or 0)
            dynamic_tp1 = float(trade.get("dynamic_tp1") or 0.0)
            watermark_price = float(trade.get("watermark_price") or entry_price)

            # --- UPDATE WATERMARK (TRAILING PEAK) ---
            if current_price > watermark_price:
                execute_query("UPDATE spot_trades SET watermark_price = ? WHERE id = ?", (current_price, trade_id), commit=True)
                watermark_price = current_price

            # --- MACRO KILL-SWITCH (PANIC SELL) ---
            if is_halted:
                if not is_paper:
                    try:
                        symbol = pair.replace("/", "")
                        live_exchange.load_markets()
                        qty_to_sell = float(live_exchange.amount_to_precision(symbol, rem_qty))
                        live_exchange.create_order(symbol, "market", "sell", qty_to_sell)
                    except Exception as e:
                        add_log("ERROR", "api_execution", f"Failed MACRO HALT close for {pair}: {e}")
                        continue
                        
                sell_notional = rem_qty * current_price
                gross_profit = sell_notional - (rem_qty * entry_price)
                fee_usdt = sell_notional * (SPOT_CONFIG["fee_pct"] / 100.0)
                net_profit_usdt = gross_profit - fee_usdt

                execute_query(
                    """
                    UPDATE spot_trades
                    SET status = 'CLOSED', exit_price = ?, remaining_qty = 0, profit_usdt = ?, profit_pct = ?, closed_at = ?
                    WHERE id = ?
                    """, (current_price, accumulated_profit + net_profit_usdt, profit_pct, now_str, trade_id), commit=True
                )
                
                execute_query("UPDATE accounts SET spot_balance_usdt = spot_balance_usdt + ? WHERE account_id = ?",
                              (sell_notional - fee_usdt, acct_id), commit=True)
                activity_summary.append(f"[{acct['display_name']}] 🚨 SHIELD ACTIVATED: Spot {pair} flattened at ${current_price:,.4f}")
                continue

            close_reason = ""
            sell_qty = 0.0
            full_close = False

            # SPOT ENGINE HAS NO STOP LOSS. IT ACCUMULATES AND WAITS FOR PROFIT.
            
            # --- RULE 1: THE 100% SECURE (TP HIT) ---
            if current_price >= dynamic_tp1 and not tp1_hit:
                close_reason = "TP SECURED (100% Bag Locked)"
                sell_qty = rem_qty
                full_close = True

            # --- EXECUTE APPLICABLE EXIT PROTOCOL ---
            if close_reason:
                if not is_paper:
                    try:
                        symbol = pair.replace("/", "")
                        live_exchange.load_markets()
                        qty_to_sell = float(live_exchange.amount_to_precision(symbol, sell_qty))
                        live_exchange.create_order(symbol, "market", "sell", qty_to_sell)
                    except Exception: continue
                
                sell_notional = sell_qty * current_price
                fee_usdt = sell_notional * (SPOT_CONFIG["fee_pct"] / 100.0)
                net_profit = (sell_notional - (sell_qty * entry_price)) - fee_usdt

                # It will always be a full close now
                if full_close:
                    execute_query(
                        """
                        UPDATE spot_trades 
                        SET status = 'CLOSED', exit_price = ?, remaining_qty = 0, profit_usdt = ?, profit_pct = ?, closed_at = ? 
                        WHERE id = ?
                        """, (current_price, accumulated_profit + net_profit, profit_pct, now_str, trade_id), commit=True
                    )
                    log_msg = f"[{CURRENT_MODE}] {close_reason}: Closed {pair} | Final PnL: +${accumulated_profit + net_profit:.2f}"
                    add_log("SUCCESS", "apextick_bot", log_msg)
                    activity_summary.append(log_msg)
                
                balance += (sell_notional - fee_usdt)
                execute_query("UPDATE accounts SET spot_balance_usdt = ? WHERE account_id = ?", (balance, acct_id), commit=True)


    # -------------------------------------------------------------------------
    # PHASE 2: EVALUATE NEW SPOT ENTRIES (SMC FILTER)
    # -------------------------------------------------------------------------
    if is_halted:
        return " | ".join(activity_summary) if activity_summary else "Spot Entries Disabled: MACRO KILL-SWITCH ACTIVE."

    pairs = execute_query(
        """
        SELECT pair, last_price, change_24h_pct, atr 
        FROM trading_pairs 
        WHERE status = 'ACTIVE' AND spot_supported = 1 AND last_price > 0
        ORDER BY volume_24h_usd DESC
        """, fetch_all=True
    )
    if not pairs: return "Execution halted: No valid pairs in database."

    # Master BTC Override (God-Mode UI Check)
    override_row = execute_query("SELECT value FROM bot_settings WHERE key = 'btc_gate_override'", fetch_one=True)
    gate_override = (override_row["value"] == "1") if override_row else False
    
    if not gate_override:
        allowed, gate_reason = is_trading_allowed_by_btc_gate()
        if not allowed:
            return " | ".join(activity_summary) if activity_summary else f"Spot paused: BTC Gate BLOCK ({gate_reason})"
            
        # Spot-Specific strict bias: Only buy if macro trend allows it
        row = execute_query("SELECT regime FROM market_regimes WHERE pair = 'BTC/USDT'", fetch_one=True)
        btc_regime = row["regime"] if row else "TRENDING"
        if btc_regime == "RANGING":
            return " | ".join(activity_summary) if activity_summary else "Spot paused: BTC is currently RANGING."

    for acct in accounts:
        acct_id = acct["account_id"]
        fresh_acct = execute_query("SELECT * FROM accounts WHERE account_id = ?", (acct_id,), fetch_one=True)
        balance = float(fresh_acct["spot_balance_usdt"] or 0.0)
        available_usdt = min(balance, float(fresh_acct.get("spot_capital_cap") or 999999.0))

        if available_usdt < SPOT_CONFIG["min_trade_usdt"]: continue
            
        on_cooldown, _ = is_account_on_spot_cooldown(acct_id, CURRENT_MODE)
        if on_cooldown: continue

        open_positions = execute_query(
            "SELECT pair FROM spot_trades WHERE account_id = ? AND status = 'OPEN' AND trade_mode = ?",
            (acct_id, CURRENT_MODE), fetch_all=True
        )
        if len(open_positions) >= SPOT_CONFIG["max_open_positions"]: continue
        held_pairs = {p["pair"] for p in open_positions}

        for p_info in pairs:
            pair = p_info["pair"]
            if pair in ["BTC/USDT", "ETH/USDT", "BNB/USDT"] or pair in held_pairs: continue

            atr = float(p_info.get("atr", 0.0))
            current_price = get_live_price_safely(pair)

            # --- LAYER 3: SMC ORDER BLOCK TRIGGER ---
            passed_smc, smc_reason, ob_edge_price = check_smc_accumulation_zone(pair, current_price)
            if not passed_smc or ob_edge_price >= current_price:
                continue

            # --- THE DYNAMIC R:R TARGET MATH (SPOT) ---
            # 1. Calculate distance from entry to bottom of FVG
            risk_distance = current_price - ob_edge_price 
            
            # 2. Multiply by R:R configuration to set TP target
            rr_multiplier = SPOT_CONFIG["risk_reward_multiplier"]
            dynamic_tp1 = current_price + (risk_distance * rr_multiplier)

            # --- LAYER 5: DYNAMIC RISK ALLOCATION ---
            trade_usdt = get_dynamic_spot_margin(available_usdt, atr, current_price)
            qty = trade_usdt / current_price
            
            if not is_paper:
                try:
                    symbol = pair.replace("/", "")
                    live_exchange.load_markets()
                    qty = float(live_exchange.amount_to_precision(symbol, qty))
                    order = live_exchange.create_order(symbol, "market", "buy", qty)
                    current_price = float(order.get("average", current_price))
                    
                    risk_distance = current_price - ob_edge_price 
                    dynamic_tp1 = current_price + (risk_distance * rr_multiplier)
                except Exception as e:
                    add_log("ERROR", "api_execution", f"Failed LIVE ENTRY for {pair}: {e}")
                    continue

            # In Spot, current_sl stays 0.0 because it is a strict accumulator
            execute_query(
                """
                INSERT INTO spot_trades (
                    account_id, pair, trade_mode, status, entry_price, qty, remaining_qty, notional_usdt, 
                    ob_edge_price, current_sl, dynamic_tp1, watermark_price, opened_at
                ) VALUES (?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (acct_id, pair, CURRENT_MODE, current_price, qty, qty, trade_usdt, 
                 ob_edge_price, 0.0, dynamic_tp1, current_price, now_str), commit=True
            )

            execute_query("UPDATE accounts SET spot_balance_usdt = ? WHERE account_id = ?", (balance - trade_usdt, acct_id), commit=True)

            msg = f"[{fresh_acct['display_name']}] {CURRENT_MODE} SPOT ENTRY: Bought {pair} at ${current_price:,.4f} (${trade_usdt:.2f})\n🎯 TP Target Locked @ ${dynamic_tp1:.4f} (No SL)\n🧠 Sniper Logic: {smc_reason}"
            add_log("INFO", "apextick_bot", msg)
            activity_summary.append(msg)
            break  

    return " | ".join(activity_summary) if activity_summary else "Spot Engine: Monitoring SMC Pools."
