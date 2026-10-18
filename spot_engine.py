import time
from datetime import datetime, timezone
import pandas as pd
from config import SPOT_CONFIG
from database import execute_query, add_log
from btc_decline_signal import is_trading_allowed_by_btc_gate, MARKET_STATE

PAIR_TICK_HISTORY = {}
SPOT_ENTRY_COOLDOWN_SECONDS = 180  


def record_pair_ticks(pairs_list: list):
    for p_info in pairs_list:
        pair = p_info["pair"]
        live_p = MARKET_STATE["live_prices"].get(pair) or float(p_info.get("last_price") or 0.0)
        if live_p <= 0:
            continue

        history = PAIR_TICK_HISTORY.setdefault(pair, [])
        if not history or history[-1] != live_p or len(history) < 25:
            history.append(live_p)
            if len(history) > 60:
                history.pop(0)


def calculate_btc_macro_regime() -> dict:
    klines = MARKET_STATE.get("klines_1h", [])
    if len(klines) < 21:
        return {"trend": "NEUTRAL", "ema_fast": 0.0, "ema_slow": 0.0, "diff_pct": 0.0}

    closes = [float(k["c"]) for k in klines if float(k.get("c", 0)) > 0]
    if len(closes) < 21:
        return {"trend": "NEUTRAL", "ema_fast": 0.0, "ema_slow": 0.0, "diff_pct": 0.0}

    s = pd.Series(closes)
    ema_9 = float(s.ewm(span=9, adjust=False).mean().iloc[-1])
    ema_21 = float(s.ewm(span=21, adjust=False).mean().iloc[-1])

    diff_pct = ((ema_9 - ema_21) / ema_21) * 100.0 if ema_21 > 0 else 0.0

    if diff_pct > 0.04:
        trend = "BULLISH"
    elif diff_pct < -0.15:
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"

    return {
        "trend": trend,
        "ema_fast": round(ema_9, 2),
        "ema_slow": round(ema_21, 2),
        "diff_pct": round(diff_pct, 3),
    }


def compute_series_rsi(prices: list, period: int = 14) -> float:
    if len(prices) < period + 2:
        return 50.0

    s = pd.Series(prices, dtype="float64")
    delta = s.diff().dropna()

    if (delta == 0).all():
        return 50.0

    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)

    avg_gain = float(gains.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean().iloc[-1])
    avg_loss = float(losses.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean().iloc[-1])

    if avg_loss == 0:
        return 75.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(float(rsi), 2)


def evaluate_spot_entry_signal(pair: str, current_price: float, change_24h: float) -> tuple[bool, float, str]:
    if current_price <= 0:
        return False, 0.0, "Invalid price"

    if change_24h <= -15.0 or change_24h >= 28.0:
        return False, 0.0, f"24h change out of safe range ({change_24h:+.2f}%)"

    history = PAIR_TICK_HISTORY.get(pair, [])
    if len(history) < 8:
        return False, 0.0, "Warming up tick buffer"

    s = pd.Series(history, dtype="float64")
    ema_fast = float(s.ewm(span=5, adjust=False).mean().iloc[-1])
    ema_slow = float(s.ewm(span=13, adjust=False).mean().iloc[-1])
    rsi = compute_series_rsi(history, period=min(14, max(6, len(history) - 2)))

    if rsi > 68.0:
        return False, 0.0, f"Overbought RSI ({rsi:.1f})"

    last_tick_up = len(history) >= 2 and history[-1] >= history[-2]
    is_trend_aligned = (ema_fast >= ema_slow * 0.998) and (32.0 <= rsi <= 66.0)
    is_oversold_bounce = (rsi < 38.0) and last_tick_up

    if not (is_trend_aligned or is_oversold_bounce):
        return False, 0.0, f"No setup (RSI={rsi:.1f})"

    score = (65.0 - rsi) + (change_24h * 0.4)
    setup_label = "DIP-BOUNCE" if is_oversold_bounce else "EMA-TREND"
    reason = f"{setup_label} | RSI={rsi:.1f} | 24h={change_24h:+.1f}%"
    return True, round(score, 2), reason


def is_account_on_spot_cooldown(account_id: str) -> tuple[bool, int]:
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


def run_spot_scalper_tick() -> str:
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
        return "No active Spot pairs available in database."

    record_pair_ticks(pairs)

    accounts = execute_query("SELECT * FROM accounts WHERE spot_enabled = 1", fetch_all=True)
    if not accounts:
        return "No active Spot accounts enabled."

    activity_summary = []

    # --- PHASE 1: MANAGE OPEN POSITIONS (TIERED SCALING) ---
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
            current_price = MARKET_STATE["live_prices"].get(pair)
            if not current_price or current_price <= 0:
                p_row = execute_query("SELECT last_price FROM trading_pairs WHERE pair = ?", (pair,), fetch_one=True)
                current_price = float(p_row["last_price"]) if p_row and float(p_row["last_price"] or 0) > 0 else float(trade["entry_price"])

            entry_price = float(trade["entry_price"])
            if entry_price <= 0:
                continue

            profit_pct = ((current_price - entry_price) / entry_price) * 100.0
            
            orig_qty = float(trade["qty"])
            rem_qty = float(trade.get("remaining_qty") or orig_qty)
            accumulated_profit = float(trade.get("profit_usdt") or 0.0)
            
            tp1 = int(trade.get("tp1_hit") or 0)
            tp2 = int(trade.get("tp2_hit") or 0)

            # Check Tiers in descending order
            if profit_pct >= SPOT_CONFIG["tp3_pct"]:
                # TP3: Close remaining bag
                sell_notional = rem_qty * current_price
                gross_profit = sell_notional - (rem_qty * entry_price)
                fee_usdt = sell_notional * (SPOT_CONFIG["fee_pct"] / 100.0)
                net_profit_usdt = gross_profit - fee_usdt
                total_profit = accumulated_profit + net_profit_usdt
                now_str = datetime.now(timezone.utc).isoformat()

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

            elif profit_pct >= SPOT_CONFIG["tp2_pct"] and not tp2:
                # TP2: Scale out 40%
                sell_qty = orig_qty * SPOT_CONFIG["tp2_close_ratio"]
                if sell_qty > rem_qty: sell_qty = rem_qty
                
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
                # TP1: Scale out 30%
                sell_qty = orig_qty * SPOT_CONFIG["tp1_close_ratio"]
                if sell_qty > rem_qty: sell_qty = rem_qty
                
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


    # --- PHASE 2: EVALUATE NEW SPOT ENTRIES ---
    allowed, gate_reason = is_trading_allowed_by_btc_gate()
    if not allowed:
        return " | ".join(activity_summary) if activity_summary else f"Spot entries paused: BTC Gate BLOCK ({gate_reason})"

    btc_macro = calculate_btc_macro_regime()
    buy_pressure = float(MARKET_STATE.get("buy_pressure_pct", 50.0))

    if btc_macro["trend"] == "BEARISH" and buy_pressure < 45.0:
        return " | ".join(activity_summary) if activity_summary else "Spot entries paused: BTC Bearish + weak buy pressure."

    scored_candidates = []
    for p_info in pairs:
        pair = p_info["pair"]
        price = MARKET_STATE["live_prices"].get(pair) or float(p_info.get("last_price") or 0.0)
        chg_24h = MARKET_STATE["live_changes"].get(pair, float(p_info.get("change_24h_pct") or 0.0))

        is_ok, score, reason = evaluate_spot_entry_signal(pair, price, chg_24h)
        if is_ok:
            scored_candidates.append({
                "pair": pair,
                "price": price,
                "score": score,
                "reason": reason,
            })

    scored_candidates.sort(key=lambda x: x["score"], reverse=True)

    if not scored_candidates:
        return " | ".join(activity_summary) if activity_summary else "Spot Brain scanning: Waiting for high-probability RSI/EMA setup."

    for acct in accounts:
        acct_id = acct["account_id"]
        fresh_acct = execute_query("SELECT * FROM accounts WHERE account_id = ?", (acct_id,), fetch_one=True)
        balance = float(fresh_acct["spot_balance_usdt"] or 0.0)
        cap = float(fresh_acct["spot_capital_cap"] or 0.0)
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

        for cand in scored_candidates:
            pair = cand["pair"]
            if pair in held_pairs:
                continue

            price = cand["price"]
            qty = trade_usdt / price
            now_str = datetime.now(timezone.utc).isoformat()

            # Insert with remaining_qty populated for partial closing logic
            execute_query(
                """
                INSERT INTO spot_trades (
                    account_id, pair, status, entry_price, qty, remaining_qty, notional_usdt, opened_at
                ) VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?)
                """,
                (acct_id, pair, price, qty, qty, trade_usdt, now_str),
                commit=True
            )

            new_bal = balance - trade_usdt
            execute_query(
                "UPDATE accounts SET spot_balance_usdt = ? WHERE account_id = ?",
                (new_bal, acct_id),
                commit=True
            )

            msg = f"[{fresh_acct['display_name']}] SPOT ENTRY: Bought {pair} at ${price:,.4f} (${trade_usdt:.2f}) | {cand['reason']}"
            add_log("INFO", "apextick_bot", msg)
            activity_summary.append(msg)
            break  

    return " | ".join(activity_summary) if activity_summary else "Spot engine ticked: Holding active positions."