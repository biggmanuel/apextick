# ApexTick Fix Plan

Target repository: `biggmanuel/apextick` (Python, Flask, SQLite/Postgres, CCXT).
Scope of this document: a plan only. No application code has been changed.

Sources: the handover document `apextick_cursor_handover_document.md`, the live dashboard snapshots taken on Nov 24, 2026 (WAT), and the current `main` of `biggmanuel/apextick` (`btc_decline_signal.py`, `regime_manager.py`, `app.py`, `database.py`, `futures_engine.py`, `spot_engine.py`, `config.py`). Line numbers come from the handover document and may drift slightly.

---

## 1. Problem Statement

The bot is in `PAPER` mode and sits near its starting equity (`$299.24` vs `$300` start). The losses are structural, not random:

| Metric (6:16 PM WAT snapshot) | Value |
| :--- | :--- |
| Spot closed trades | 20 wins totalling +$9.40 (avg +$0.47), 3 losses totalling -$6.72 (avg -$2.24) |
| Spot realized / open | +$2.64 / -$2.65 |
| Futures realized / open | -$1.78 / +$1.03 |
| Spot break-even win rate at current averages | ~83% (actual 87%, so the edge is ~4 points) |

Root causes, in order of impact:

1. **Spot risk/reward is inverted.** Wins are capped near +1% to +3% (`min_tp_pct = 1.2`), but a losing bag is only closed at -12% (`max_drawdown_pct = 12`), about -$2.10 per loss on a ~$17 bag. Stop losses are also not written to `current_sl`, so the dashboard shows `SL: none` while the bag is protected only by a hidden percentage check.
2. **Spot and Futures trade against each other.** `futures_engine.py` uses BTC's 1h 9/21 EMA trend (`calculate_futures_macro_trend`) to go SHORT, while `spot_engine.py` only checks the crash gate (`is_trading_allowed_by_btc_gate`) and buys the same coins.
3. **The price feed is broken.** `wss://.../ws/!ticker@arr` is retired, both sockets drop with `ping/pong timed out`, and the bot falls back to REST polling. The dashboard shows `WS DOWN - REST FALLBACK`.
4. **Regime detection is dead for BTC.** `sync_binance_pairs()` skips `BTC/USDT` (`continue`), so `RegimeManager` never analyses it. `trading_pairs.atr` is computed but never saved, so ATR sizing always falls back to flat sizing.
5. **Futures sizing ignores costs.** `margin = RISK / (leverage * stop_fraction)` leaves out round-trip fees and slippage, so real stop losses land at -$0.55 to -$0.90 instead of $0.50.
6. **Clustered stop-outs.** Up to five same-direction Futures positions open together and stop out together, with no shadow mode or directional audit.
7. **Exit race (handover Bug 5).** Engine exit `UPDATE` statements in `futures_engine.py` and `spot_engine.py` have no `AND status = 'OPEN'` guard, so a manual force-close and an engine exit can both credit the account.
8. **Other data issues.** `api_powder_keg` shows stale `WLFI/USDT` (no `status = 'ACTIVE'` filter). Tokenized stocks (`COINB`, `NBISB`, `GOOGLB`, `ANTB`) are not in `excluded_bases`. The dashboard's `Trend: BULLISH` uses 24h change, while Futures trades on the 1h EMA, so the two disagree.

---

## 2. Work Packages

Each package lists the files, the change, and the acceptance criteria. Numeric values are proposals to be validated, not final.

### WP1: Spot Risk/Reward (highest priority)

**Files:** `config.py` (`SPOT_CONFIG`), `spot_engine.py`, `app.py` (`api_positions`), `test_spot_engine.py`.

**Changes:**
- Replace the single `max_drawdown_pct` check with a **stop price** written to `current_sl` at entry: `current_sl = entry * (1 - stop_pct / 100)`.
- Add a **dollar cap on each loss**, `max_loss_usdt` (proposed `0.75`), so `stop_pct` is never wider than `max_loss_usdt / trade_usdt`.
- Enforce a **minimum reward-to-risk** ratio: `dynamic_tp1 >= entry * (1 + min_rr * stop_pct / 100)`, with `min_rr` proposed at `1.5`.
- Add `max_tp_pct` (proposed `8.0`) so targets like ACE's `+62.84%` no longer lock a slot until they hit or the bag is stopped out.
- Exit when `current_price <= current_sl` (not a percentage check), and keep the `MACRO HALT` flatten.
- Add `AND status = 'OPEN'` to the exit `UPDATE` and only credit the account when `rowcount == 1` (handover Bug 5).
- Decide whether to apply the new stop to the five currently open Spot bags (see open questions).

**Proposed starting values:** `stop_pct = 4.0`, `max_loss_usdt = 0.75`, `min_rr = 1.5`, `min_tp_pct = 1.2` retained as a floor, `max_tp_pct = 8.0`.

Break-even win rate at a -4% stop and +6% target is 40%. At a -4% stop and +3% target it is 57%. The final values must come from backtesting against the trades in `analyze_trades.py` output.

**Acceptance criteria (tests):**
- Entry writes `current_sl` equal to `entry * (1 - stop_pct/100)`.
- A bag closes when price touches `current_sl`, and not before.
- The realized loss on a stopped bag never exceeds `max_loss_usdt` plus fees.
- No target is closer than `min_rr` times the stop distance, or further than `max_tp_pct`.
- Exit double-run: a second exit attempt on an already-closed trade credits nothing.

### WP2: Spot / Futures Coordination

**Files:** new shared module `market_context.py` (moves `calculate_futures_macro_trend` out of `futures_engine.py` to avoid circular imports), `spot_engine.py`, `futures_engine.py`, tests.

**Changes:**
- Spot does not open new entries when the BTC 1h trend is `BEARISH`.
- Spot does not buy a pair while an open Futures position on that pair is `SHORT`, and Futures does not open `SHORT` on a pair held long on Spot.
- Log the reason through the existing skip summary (`btc_trend_block`, `futures_opposite_open`).

**Trade-off to confirm:** this reduces Spot entry frequency during downtrends. That is intended, because Spot's current losses cluster in those periods.

**Acceptance criteria (tests):** a bearish trend blocks Spot entries; an open opposite-direction Futures position blocks the matching Spot entry; a bullish trend with no conflict still allows entries.

### WP3: Price Feed, BTC Regime, ATR Persistence

**Files:** `btc_decline_signal.py`, `regime_manager.py`, `app.py`, tests (`test_btc_signal.py`).

**Changes:**
- Replace `!ticker@arr` with `!miniTicker@arr`. Compute 24h change as `((c - o) / o) * 100` when `o > 0`.
- Set `ping_interval=60, ping_timeout=30` for both streams.
- Remove the `BTC/USDT` `continue` in `sync_binance_pairs()` so BTC is stored in `trading_pairs`. Engines already exclude `BTC/USDT`, `ETH/USDT`, and `BNB/USDT` from entries.
- In `RegimeManager`, persist the computed ATR to `trading_pairs.atr` for each pair.
- Comment out or disable `GridEngine` in `app.py` until its unmanaged-grid bug is fixed.

**Acceptance criteria (tests):** miniTicker messages update `live_prices` and `live_changes` correctly; BTC appears in `trading_pairs` with a regime row; ATR is non-zero after a regime cycle; `GridEngine` is not started.

### WP4: Futures Risk Formula, Shadow Mode, and Same-Direction Limits

**Files:** `futures_engine.py`, new `self_awareness.py`, `config.py` (`FUTURES_CONFIG`), tests (`test_futures_risk.py`, `test_futures_v2.py`).

**Changes:**
- Margin formula becomes `margin = RISK / (leverage * (stop_fraction + 2 * fee_fraction + slippage_buffer))`.
- `min_stop_pct` raised to `0.65` (from `0.4`).
- Engine exit `UPDATE` statements get the same `AND status = 'OPEN'` guard as WP1.
- `self_awareness.py` implements:
  - **Shadow mode:** after `shadow_trigger_losses` consecutive closed losses (proposed `5`, not `3`), new signals are recorded with `trade_mode = 'SHADOW'` and no balance changes.
  - **Recovery:** a shadow trade reaching TP1 returns the engine to normal mode. Shadow trades also close on their stop and expire after a set number of candles (see Open Questions).
  - **Directional audit:** after `direction_trigger_losses` consecutive same-direction losses (proposed `4`), that direction is blocked for new entries until a shadow trade in that direction reaches TP1.
  - **Risk throttle:** after `throttle_trigger_losses` consecutive losses (proposed `3`), risk drops from `$0.50` to `$0.25` and same-direction slots drop from 2 to 1.
  - **Calibration (required before merge):** at the current ~30% win rate, a run of 3 losses occurs about a third of the time and a run of 5 about one time in six. The thresholds above are starting points. Replay the closed-trade history in `analyze_trades.py` and choose thresholds so each mode is active for no more than about 25% of trades. Record the chosen values and the measured active fraction in the PR.
- Phase 1 of the Futures engine also manages open `SHADOW` positions (watermark, TP1, breakeven, trailing) without touching balances or calling the exchange.

**Acceptance criteria (tests):** margin at a given stop equals the formula; a realized loss is within `$0.50` plus rounding; shadow mode activates after 3 losses and recovers after a shadow TP1; same-direction slot limits hold; shadow positions close without balance changes.

### WP5: Data Quality and Dashboard

**Files:** `app.py`, `templates/dashboard.html`, `config.py` (`MARKET_SCANNER_CONFIG.excluded_bases`), tests (`test_app_dashboard.py`).

**Changes:**
- Add `AND status = 'ACTIVE'` to `api_powder_keg`.
- Add the new tokenized stocks (`COINB`, `NBISB`, `GOOGLB`, `ANTB`) and any other `*B` equity tokens to `excluded_bases`.
- Show the 1h EMA trend next to the 24h trend in the Macro Gate widget.
- Show `current_sl` for Spot bags.
- Add `max_win_streak`, `max_lose_streak`, `current_streak`, `profit_factor`, and `payoff_ratio` to `/api/futures/statistics`, and display them in the Futures KPI card. Add the same for Spot.
- Fix the SMC Battle Map column widths so long pair names do not run into their neighbours.
- Replace the root `test_app_dashboard.py` with `templates/test_app_dashboard.py`, after comparing both for coverage.

**Acceptance criteria (tests):** Powder Keg shows only active pairs; excluded tokens never appear in `trading_pairs`; new statistics fields are returned by the API and rendered.

### WP6: Pre-LIVE Hardening (last, and gated)

**Files:** `futures_engine.py`, `spot_engine.py`, `config.py`.

**Changes:** use CCXT unified swap symbols (`SOL/USDT:USDT`), add `reduceOnly` to all exit orders, store the exact fill `qty` from `order["filled"]`, and set isolated margin (`set_margin_mode("isolated", symbol)`).

**Gate:** `ALLOW_LIVE_TRADING` stays `0` until WP1 to WP5 are merged and the paper-trading validation below passes.

---

## 3. Execution Order

1. WP1 and the WP2 Spot-side rules (they change Spot's economics directly).
2. WP3 (feed and regime), because WP2 depends on the BTC trend being correct.
3. WP4 (Futures sizing, shadow mode).
4. WP5 (data and dashboard).
5. WP6 only after validation.

Each package is committed separately, with its tests, and pushed only after the test suite (`run_all_tests.py`) passes.

---

## 4. Validation

Validation is measured in closed trades, not calendar time:

- Run the updated bot in `PAPER` mode until at least **50 closed Spot trades** and **50 closed Futures trades** have accumulated under the new rules.
- Compare against the baseline above using `analyze_trades.py`: win rate, average win, average loss, payoff ratio, profit factor, and maximum losing streak.
- Promotion to any LIVE discussion requires positive expectancy on both engines and no violations of the stop cap in the logs.

---

## 5. Open Questions

1. **Existing open Spot bags:** should the new stop apply to the five currently open bags (`SOL`, `COINB`, `NEAR`, `ACE`, `ZRO`), or only to new entries?
2. **Stop and target values:** confirm the proposed starting values in WP1, or choose a different risk/reward profile.
3. **BTC trend gate for Spot:** accept fewer Spot entries during bearish trends?
4. **Shadow mode closure:** shadow trades are only recovered by a TP1 hit. Should a shadow trade also be closed by its stop, and should it expire after a set number of candles?
5. **Dashboard trend label:** should the widget show the 1h EMA trend, the 24h trend, or both?

---

## 6. Out of Scope

- Changing the Finnhub macro calendar integration.
- Any change that enables live order placement.
- Changing the database backend (SQLite vs Postgres).
