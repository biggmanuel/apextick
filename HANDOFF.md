# ApexTick - Project Handoff
*State of the project as of Tuesday, November 24, 2026. Written so a new AI assistant (Cursor) or developer can continue exactly where the previous assistant (Claude) stopped.*

## Contents
1. TL;DR
2. People, workflow, working agreement
3. Architecture
4. Repository map (file by file)
5. Database and accounting rules
6. Futures engine - exact logic
7. Spot engine - exact logic
8. Market data, gates and background services
9. Safety rails (do not break these)
10. Configuration reference
11. API and dashboard
12. Tests and tools
13. Runbook
14. Results so far
15. History: what was found and fixed
16. Known issues and open work
17. Roadmap: the "self-aware bot" brainstorm (NOT built)
18. Gotchas
19. Suggested first prompt for Cursor
20. Glossary

---

## 1. TL;DR
- **What it is:** a Python/Flask crypto trading bot for Binance. Two strategies share one ledger: a **spot "accumulator"** (buy dips into bullish SMC zones, sell at a target) and a **USDT-M futures "sniper"** (15x leverage, SMC zone entries, structural stop, 75/25 take-profit split with a trailing runner). A web dashboard on port 5000 shows everything.
- **Mode:** PAPER (simulated money, live Binance market data). Two paper books of $150 each (spot + futures) = $300 start. **LIVE trading is deliberately locked** (needs `ALLOW_LIVE_TRADING=1`) because the live order code still has known bugs (section 16).
- **Where it lives:** developed in a GitHub Codespace -> pushed to GitHub (`biggmanuel/apextick`, keep private) -> pulled on an Oracle Ubuntu server (`~/apextick`, python venv) where the bot actually runs. The user is now moving development to **Cursor**.
- **Where it stands (as of Nov 22 snapshot):** equity about $292 vs $300 start (about -2.6%). Spot is profitable (5 wins / 0 losses, +$2.14 realized). Futures is losing: 48 closed trades, 8 wins (16.7%), net -$15.25; it won 7 of its first 9 trades during a BTC dump, then lost 38 of the next 39 during the rebound and chop (streaks of 20 and 18 losses). The bot has no awareness of its own performance or of the market regime. That is the main open problem (section 17).
- **What the user wants next:** a "self-aware and smart" bot (adapts to market regimes, protects capital in bad regimes). They asked to **brainstorm first** and had not yet chosen between (a) protecting capital in bad markets and (b) improving entries to raise the win rate. Nothing from the brainstorm has been built.
- **Everything is tested:** `python3 run_all_tests.py` -> 9 suites, 201 checks, plus a jsdom UI test (55 checks) in `tools/`.

## 2. People, workflow, working agreement
- **User:** goes by Bigg Manuel; developer, content creator and trader (Python trading bots, Web3, full-stack). Windows PC with Chrome. Works in GitHub Codespaces; the server is an Oracle Cloud Ubuntu VM (hostname `apextick`). Sometimes away from the PC.
- **Deploy flow:** edit in Codespace -> run tests -> commit/push -> on the server: stop bot, back up DB, `git pull`, restart, confirm the dashboard says PAPER TRADING. The SQLite DB is NOT in git (it was committed early and then untracked; old commits still contain it, so keep the repo private).
- **Sharing data:** the user saves the dashboard from Chrome as `.mhtml` and uploads it. Chrome strips JavaScript when saving, so only rendered data is visible. Use `tools/parse_snapshot.py` to read these.
- **Working agreement (important):**
  - When the user says *brainstorm / discuss / explain*, do **not** write code. They once said "I said let's brainstorm, you went ahead to code". Ask before building.
  - Explain in plain language with exact copy-paste commands. The user has typed Python at the bash prompt by mistake; spell out which terminal/prompt a command belongs to.
  - Deliver complete files plus tests, and say what changed and what did not.
  - Be candid about uncertainty. Paper results are optimistic; one market episode is not proof; do not oversell.
  - The user cares about LONG/SHORT visibility, win/lose streaks, real-world comparison (BTC dump/pump), and about avoiding surprises.

## 3. Architecture
```
Binance public WS/REST ──► btc_decline_signal.py ──► MARKET_STATE (in-memory) + trading_pairs (DB)
                              │  (prices, BTC klines/pressure, pair scanner, BTC gate)
SMC mapper (15m klines) ──► liquidity_pools (DB)  ◄── zone lifecycle: PENDING→MITIGATED→INVALIDATED/EXPIRED
macro_calendar ──► bot_settings.macro_halt ("1" = flatten + pause entries)
regime_manager ──► market_regimes (BTC regime currently NEVER computed - see 16)
                              │
        ┌─────────────────────┴──────────────────────┐
  futures_engine.run_futures_engine_tick()   spot_engine.run_spot_scalper_tick()   (each every 3s, own thread)
        └─────────────────────┬──────────────────────┘
                              ▼
          SQLite ledger (apextick.db): accounts, futures_trades, spot_trades, bot_settings, logs ...
                              ▼
     app.py (Flask :5000)  ──►  templates/dashboard.html (polls /api/* every 3s / 15s)
```
**Threads started by `app.py: start_background_services()`** (in order): `init_db()` → warnings about ADMIN_KEY / LIVE lock → `backfill_btc_history()` → `sync_binance_pairs()` → `start_btc_stream_background()` (spawns: BTC stream thread, all-tickers stream thread, DB price sync loop, scanner loop) → RegimeManager thread → SmartMoneyMapper thread → MacroCalendar thread → GridEngine thread (stub) → spot loop → futures loop. All are daemon threads in ONE process (never run several workers: gunicorn/PM2 cluster would duplicate the engines).

**Data-freshness model:** `MARKET_STATE["prices_ts"]` is stamped on every websocket message or REST refresh. `get_fresh_price(pair)` returns 0.0 if the feed is older than 20 s. Engines fall back to `trading_pairs.last_price` only if `updated_at` is within 300 s, otherwise they do nothing (no entries, no stop evaluation).

## 4. Repository map (file by file)
All files below are in the zip (project root unless noted). Line counts are approximate.

| File | Lines | Purpose |
|---|---|---|
| `app.py` | 650 | Orchestrator + Flask API. Starts threads. Admin endpoints (mode toggle, BTC override, force close, liquidate all) with ADMIN_KEY checks. `/api/performance`. Paper-close accounting (`close_futures_paper`, `close_spot_paper`). |
| `config.py` | 165 | All tunables (dicts) + env loading + the shared **futures/swap** ccxt client `exchange`. See section 10. |
| `database.py` | 340 | `execute_query`, `execute_batch`, `prune_logs`, `init_db` (schema, migrations, indexes, seeds, SQLite WAL pragmas). SQLite default; PostgreSQL partially supported (untested). |
| `btc_decline_signal.py` | 600 | **Data core.** `MARKET_STATE`, freshness helpers, websocket streams, REST fallback, BTC trade pressure (rolling deque), BTC klines, **BTC gate** (`is_trading_allowed_by_btc_gate`, fails closed), **pair scanner** (`sync_binance_pairs`: top 50 USDT pairs by volume, OI cached, atomic upsert). |
| `smart_money_concepts.py` | 290 | `SmartMoneyMapper`: finds FVGs and Order Blocks on 15m candles, stores zones keyed by candle time (no duplicates), runs the zone lifecycle, expires/prunes. |
| `futures_engine.py` | 590 | `run_futures_engine_tick()`: manage open positions (Phase 1) then look for entries (Phase 2). See section 6. |
| `spot_engine.py` | 400 | `run_spot_scalper_tick()`. See section 7. |
| `macro_calendar.py` | 200 | Macro news kill-switch: sets `bot_settings.macro_halt`. Finnhub calendar is a PAID endpoint; manual events supported. |
| `regime_manager.py` | 143 | Computes TRENDING/RANGING + volatility per ACTIVE pair every 5 min (Bollinger band width on 1h). **BTC is never in the pairs table so its regime is never computed.** Unchanged from the original. |
| `grid_engine.py` | 137 | **Stub.** Only inserts `PENDING_GRID` rows into `spot_trades`; places no orders. Unchanged. |
| `templates/dashboard.html` | 980 | Single-page dashboard (Jinja template, CRLF line endings, inline CSS/JS). |
| `analyze_trades.py` | 120 | Read-only report on closed trades: streaks, by direction / stop distance / hold time / hour / pair. |
| `btc_vs_trades.py` | 170 | Read-only: your closed trades vs BTC candles hour by hour (tries binance, bybit, okx, kraken, coinbase). |
| `run_all_tests.py` | 20 | Runs every `test_*.py` (each isolated) and prints a summary. |
| `test_*.py` (9 suites) | - | See section 12. |
| `tools/parse_snapshot.py` | - | Reads saved `.mhtml` dashboard snapshots as text lines. |
| `tools/ui_test.js`, `tools/package.json` | - | jsdom simulated-browser test of the dashboard (55 checks). |
| `requirements.txt`, `.cursorrules`, `HANDOFF.md` | - | Dependencies, Cursor rules, this document. |

**Not in the zip (exist in the user's repo/server):** `.env` (secrets), `apextick.db` (ledger), `static/`, `venv/`, `structure.txt`, `.gitignore`, two old one-off scripts `patch_db.py` / `patch_db_smart.py` (obsolete; only edited database.py text; safe to delete). GitHub is the source of truth; the zip is the last state Claude delivered, so diff it against the repo before overwriting anything.

## 5. Database and accounting rules
SQLite file `apextick.db` (WAL mode, 20 s busy timeout). `database.execute_query(query, params, fetch_one, fetch_all, commit)` returns dicts; use `?` placeholders. `execute_batch([(query, params), ...])` runs many statements in ONE transaction (use it for bulk writes). Upserts use `ON CONFLICT ... DO UPDATE/NOTHING` (portable).

**Tables** (created/migrated by `init_db()`; SMC adds `candle_time` + indexes on start):
- `accounts`: `account_id` (seed: `Emmanuel_Paper_150`, display "Emmanuel ($150)"), `spot_enabled`, `futures_enabled`, `spot_balance_usdt`, `spot_capital_cap`, `futures_balance_usdt`, `futures_capital_cap`, `futures_live_withdraw`. **The balance columns are CASH only** (money not tied up in open positions). Both start at 150.
- `bot_settings` (key/value): `paper_trading_enabled` ("1" paper, "0" live), `btc_gate_override` ("1" = force BTC gate open), `macro_halt` ("1" = halted), optional `btc_decline_signal_enabled`.
- `trading_pairs`: hot list. `pair` UNIQUE, `status` ACTIVE/INACTIVE, `spot_supported`, `futures_supported`, `change_24h_pct`, `volume_24h_usd`, `last_price`, `oi_usd`, `oi_spot_ratio`, `atr` (always 0, nobody populates it), `updated_at`.
- `liquidity_pools`: SMC zones. `pool_type` FVG/ORDER_BLOCK, `direction` **BULLISH/BEARISH** (not LONG/SHORT!), `top_price`, `bottom_price`, `status` PENDING/MITIGATED/INVALIDATED/EXPIRED, `created_at` (= when the structure formed), `candle_time` (ms, unique key with pair/type/direction).
- `futures_trades`: `direction` LONG/SHORT, `status` OPEN/CLOSED, `trade_mode` PAPER/LIVE, `entry_price`, `exit_price`, `margin_usdt` (ORIGINAL margin, never overwritten), `remaining_margin_usdt`, `notional_usdt`, `leverage`, `pnl_usdt`, `pnl_pct`, `close_type`, `ob_edge_price`, `current_sl`, `dynamic_tp1`, `watermark_price`, `tp1_hit`, `peak_roi_pct`, `worst_roi_pct`, `opened_at`, `closed_at` (+ unused `tp2_hit`, `tp3_hit`).
- `spot_trades`: `status` OPEN/CLOSED/PENDING_GRID, `entry_price`, `exit_price`, `qty`, `remaining_qty`, `notional_usdt`, `profit_usdt`, `profit_pct`, `ob_edge_price`, `current_sl` (always 0 for spot), `dynamic_tp1`, `watermark_price`, `tp1_hit`, `opened_at`, `closed_at`.
- `market_regimes` (pair, regime TRENDING/RANGING, volatility_state), `logs` (timestamp, level, source, message; pruned to 7 days hourly).
- Indexes: `ix_futures_acct_mode_status`, `ix_futures_pair_status`, `ix_spot_acct_mode_status`, `ix_spot_pair_status`, `ix_pairs_status`, `ix_logs_timestamp`, `ux_pools_candle`, `ix_pools_pair_status`.
- Timestamps are ISO-8601 strings in UTC (`datetime.now(timezone.utc).isoformat()`).

**Accounting invariants (keep these true; tests assert them):**
1. Opening a futures trade debits cash by `margin + entry_fee`. The trade row starts with `pnl_usdt = -entry_fee`.
2. TP1 closes 75% of the ORIGINAL margin: cash is credited `margin_closed + net_pnl`; `remaining_margin_usdt` drops; `pnl_usdt` accumulates the banked profit; stop moves to entry (breakeven).
3. A full close credits `max(0, remaining_margin + net_pnl)` and writes `pnl_usdt = accumulated + net_pnl` (the TRUE total including the banked TP1 profit and the entry fee). `pnl_pct` = total return on ORIGINAL margin.
4. Therefore **balance change == recorded pnl** for any complete trade (tests check this). Fees: futures 0.05% of notional per leg, spot 0.1% per leg. Paper slippage (futures only): fills are 0.02% worse than the quote on entry and every exit.
5. Balance updates are always **relative** (`x = x + ?`). Never write an absolute balance computed earlier (dashboard force-close and engines run concurrently).
6. Dashboard "Futures Net PnL" = SUM(`pnl_usdt`) over all trades of the current mode **including banked TP1 profit of still-open trades**. "Open P/L" = unrealized runner PnL only. Equity = cash + open margin + unrealized (futures) and cash + open position value (spot).
7. Known accounting wart: on profitable full closes the futures engine also adds `net_pnl * withdraw_split_pct` (0.70) to `accounts.futures_live_withdraw` WITHOUT debiting the balance (likely double counting). Not fixed.

## 6. Futures engine - exact logic (`futures_engine.run_futures_engine_tick`)
Called every 3 s. Reads mode: **LIVE only if `paper_trading_enabled == "0"`**, halt if `macro_halt` in ("1","true").

**Phase 1 - manage open trades** (for each OPEN trade of the current mode; skipped with a "stale_prices" skip count if no fresh price):
1. Update watermark (best price since entry) and peak/worst ROI.
2. If macro halt: close at market ("MACRO HALT (Panic Sell)"), keeping banked TP1 profit and trade history.
3. Else rules (LONG shown; SHORT mirrored): (1) price <= `current_sl` and TP1 not hit -> `STRUCTURAL SL (SMC Invalidated)` full close; (2) price >= `dynamic_tp1` and TP1 not hit -> `TP1 SECURED` close 75% of original margin, stop -> entry; (3) price <= stop and TP1 hit -> `BREAKEVEN SL` close runner; (4) TP1 hit: trailing stop at `watermark * (1 - trailing_buffer_pct/100)` (1.5%) while price > entry -> `TRAILING SL HIT`.
4. Paper exits use `_paper_fill` slippage; LIVE exits would send a market order (live path is NOT production-ready).

**Phase 2 - entries** (only if not halted):
1. LIVE lock: if mode is LIVE and `ALLOW_LIVE_TRADING != "1"`: no entries (exits still managed).
2. `calculate_futures_macro_trend()`: BTC 1h EMA9 vs EMA21 -> BULLISH / BEARISH / NEUTRAL. **This single label sets the direction of EVERY trade** (BULLISH -> LONG, BEARISH -> SHORT; NEUTRAL -> BTC buy pressure >= 50% -> LONG else SHORT). So all positions are always the same direction = one correlated bet; an EMA flip flips the whole book.
3. BTC gate (`is_trading_allowed_by_btc_gate()`) unless `btc_gate_override == "1"`. BLOCK -> no entries.
4. Per account: cash >= `min_margin_usdt` (0.4); not within the account entry cooldown (600 s, checks the newest trade of ANY status); fewer than `max_open_positions` (5).
5. Candidate pairs: `trading_pairs` ACTIVE, `futures_supported=1`, `volume_24h_usd >= 20M`, ordered by volume desc; skip BTC/ETH/BNB and pairs already held; need a fresh price.
6. Zone lookup `check_micro_structure(pair, price, direction)`: LONG needs a **BULLISH** zone, SHORT a **BEARISH** zone, status PENDING/MITIGATED, price inside the zone +-0.2%, narrowest zone first. Stop edge = zone bottom (LONG) / top (SHORT). **Stop distance must be between `min_stop_pct` 0.4% and `max_stop_pct` 2.0%** or the zone is skipped (`stop_too_tight` / `stop_too_wide`).
7. Per-pair cooldown: 2 h after a `STRUCTURAL SL` close, 30 min after any other close.
8. Sizing (risk based): `margin = risk_per_trade_usdt (0.50) / (leverage (15) * stop_fraction)`, clamped to [`min_margin_usdt`, 5% of cash, cash]. So a stop-out loses about $0.50 whatever the zone width (actual average loss so far -$0.64 with fees/slippage/overshoot).
9. `dynamic_tp1 = price +- risk_distance * risk_reward_multiplier (2.0)`.
10. Insert trade (paper fill with slippage; entry fee booked; cash debited), log the entry. One entry per account per tick.
11. Skip reasons are counted and summarised in ONE log line every 5 min (source `futures_skips`): `no_zone_at_price`, `stop_too_wide`, `stop_too_tight`, `pair_cooldown`, `account_cooldown`, `max_positions`, `low_margin`, `btc_gate_block`, `stale_prices`, `live_locked`, `price_beyond_edge`.

## 7. Spot engine - exact logic (`spot_engine.run_spot_scalper_tick`)
- **Phase 1:** for each OPEN spot trade: macro halt -> flatten; **drawdown stop** if price is `max_drawdown_pct` (12%) below entry; **take profit** when price >= `dynamic_tp1` (sells 100%). There is no price-level stop-loss (`current_sl` stays 0): spot is an accumulator that holds dips, limited only by the 12% drawdown rule. Exits credit `sell_notional - fee`.
- **Phase 2 entries:** not halted; LIVE lock; pairs ACTIVE + `spot_supported`, volume desc; BTC gate unless overridden; "BTC RANGING" pause (dead code today because BTC regime is never computed); account cash >= `min_trade_usdt` 17, entry cooldown 180 s, < 5 open positions; skip BTC/ETH/BNB and held pairs; **BULLISH** zone containing price (+-0.2%), narrowest first, price above the zone bottom; no re-buy of a coin that closed at a loss in the last 4 h.
- **Target:** `dynamic_tp1 = max(price + risk_distance * 3.0, price * (1 + min_tp_pct 1.2%))` where risk_distance = price - zone bottom.
- **Size:** 12% of available cash, minimum $17 (the ATR scaling is inactive because `atr` is 0). Entry fee (0.1%) is booked in `profit_usdt` at entry.
- LIVE orders go through `spot_engine.live_spot_exchange` (a SPOT client; the shared `config.exchange` is a futures client). The spot LIVE path caps the sell quantity at the free balance (buy fees are taken in the base coin).
- Tokenized stocks / equity tokens (SPCXB, SOXLB, NVDAB, QQQB, INTCB, KORUB, MUB, SNXXB) are excluded by the scanner (`MARKET_SCANNER_CONFIG["excluded_bases"]`).

## 8. Market data, gates and background services
- **`MARKET_STATE`** (dict in `btc_decline_signal`): `live_prices`, `live_changes`, `prices_ts`, `ticker_ts`, `pressure_ts`, `klines_ts`, `buy_pressure_pct`, `sell_pressure_pct`, `klines_1h` (list of {t,o,h,l,c,v}, max 60), `btc_price`, `btc_24h_change`, `btc_trend` (24h-change based, only for display), `btc_sentiment`, `btc_atr_1h`, `ws_connected`, `computed_allow/reason`.
- **Streams:** (1) BTC combined stream `btcusdt@aggTrade/btcusdt@kline_1h` (pressure + live 1h candle), (2) `!ticker@arr` all tickers (prices). Reconnect with backoff, certificate verification ON (`WS_INSECURE_SSL=1` disables it). **The websockets drop every few minutes in production**; the watchdog sets `ws_connected=False` after 15 s of silence and a REST fallback (`fetch_tickers` every 3 s) keeps prices fresh. The dashboard shows "WS DOWN - REST FALLBACK". Root cause not found (candidates: network path from the Oracle VM, payload size of `!ticker@arr`; idea: switch to `!miniTicker@arr` and/or alternate endpoints/port 443).
- **BTC gate** (`eval_decline_from_closes` + pressure checks): BLOCK if BTC data missing/stale/short (fail closed); nuclear -8% in 4 h; soft decline (4 h <= -4% or (1 h <= -2% and >= 3 of last 5 candles red)) **combined with a volume spike** (recent volume >= 1.5x baseline); buy pressure < 20% or sell pressure > 80% (only while pressure data is fresh). Override via `btc_gate_override`.
- **Pair scanner** (`sync_binance_pairs`, every 30 s): spot tickers -> USDT pairs with 24h quote volume >= $3M, top 50 by volume, excluding stablecoins, leveraged tokens (BTCUP-style, matched only when the stripped name is a listed coin so JUP/SYRUP stay) and tokenized stocks. Open interest from futures (cached 150 s). One atomic transaction; pairs with open positions stay ACTIVE. BTC itself is tracked for the BTC state but never inserted into `trading_pairs`.
- **SMC mapper:** every 15 s; fetches 15m candles for a pair only when a new candle can exist; finds bullish/bearish FVGs (gap >= 0.2%) and Order Blocks (last opposite candle within 10 candles before the impulse); zones already broken by later candles are skipped, tapped ones stored as MITIGATED; lifecycle: PENDING -> MITIGATED when price taps; PENDING/MITIGATED -> INVALIDATED when price breaks the far edge; EXPIRED after 24 h; dead zones deleted after 48 h.
- **Macro calendar:** `macro_halt` is reset to 0 on boot; halts from 5 min before to 15 min after high-impact US events. Finnhub's `/calendar/economic` is a paid endpoint (403 on free keys: logs once and uses manual events). Manual events: `MACRO_MANUAL_EVENTS="2026-12-10 13:30|CPI;2026-12-17 19:00|FOMC"` (UTC) or `MACRO_CALENDAR_CONFIG["manual_events"]`. **No events are configured right now, so the kill switch never fires.**
- **Regime manager / grid:** see section 4 (regime for BTC never computed; grid is a stub).

## 9. Safety rails (do not break these)
1. **LIVE is locked**: dashboard cannot switch to LIVE and engines open no LIVE entries unless `ALLOW_LIVE_TRADING=1`. Mode flags fail safe: LIVE only on an explicit `"0"`.
2. **Admin actions** (toggle mode, BTC override, force close, liquidate all) need header `X-Admin-Key`; they are **disabled while ADMIN_KEY is unset/default** (`secret_key_here`, `changeme`, `admin`, empty). Constant-time compare.
3. **Fail closed on data:** BTC gate blocks without fresh BTC data; engines ignore stale prices.
4. Dashboard force-close/liquidate only act on OPEN **PAPER** trades (LIVE trades are refused: the dashboard sends no exchange orders), credit the account, use the same accounting as the engines, and use a row-count guard so a trade closed by the engine at the same moment is not double-credited.
5. Dashboard escapes all server strings (XSS) and uses a masked admin-key dialog with typed confirmations (`LIVE`, `LIQUIDATE`).
6. Secrets/data never in git; repo private.

## 10. Configuration reference
Dicts in `config.py` (values as of this handoff; engines read them at import time, so **restart after editing**). Most constants have `.get(..., default)` fallbacks in the engines.

```python
FUTURES_CONFIG = {
  "capital_cap_usdt": 150.0, "leverage": 15, "margin_size_pct": 2.0 (legacy, unused), "min_margin_usdt": 0.4,
  "max_open_positions": 5, "fee_pct": 0.05, "withdraw_split_pct": 0.70,
  "tp1_close_ratio": 0.75, "runner_close_ratio": 1.0 (unused), "risk_reward_multiplier": 2.0, "trailing_buffer_pct": 1.5,
  "min_stop_pct": 0.4, "max_stop_pct": 2.0, "risk_per_trade_usdt": 0.50, "max_margin_pct_per_trade": 5.0,
  "pair_stop_cooldown_seconds": 7200, "pair_close_cooldown_seconds": 1800, "min_volume_24h_usd": 20_000_000,
  "skip_log_interval_seconds": 300, "paper_slippage_pct": 0.02 }
  # code constant: FUTURES_ENTRY_COOLDOWN_SECONDS = 600 (account-wide spacing between entries)
SPOT_CONFIG = {
  "capital_cap_usdt": 150.0, "trade_size_pct": 12.0, "min_trade_usdt": 17.0, "max_open_positions": 5, "fee_pct": 0.1,
  "tp1_close_ratio": 0.75 (unused), "runner_close_ratio": 1.0 (unused), "risk_reward_multiplier": 3.0, "trailing_buffer_pct": 3.0 (unused),
  "never_close_below_tp": True (unused), "min_tp_pct": 1.2, "max_drawdown_pct": 12.0 (0 disables),
  "pair_cooldown_after_loss_seconds": 14400, "skip_log_interval_seconds": 300 }
  # code constant: SPOT_ENTRY_COOLDOWN_SECONDS = 180
BTC_GATE_CONFIG = { "enabled": True, "min_buy_pressure_pct": 20.0, "max_sell_pressure_pct": 80.0, "min_bars": 5, "baseline_bars": 24,
  "tail": 5, "vol_mult": 1.5, "red_look": 5, "red_need": 3, "nuclear_4h": -8.0, "soft_4h": -4.0, "soft_1h": -2.0, "window_ms": 60000 }
SMC_CONFIG = { "enabled": True, "lookback_candles": 40, "min_fvg_pct_gap": 0.2, "ob_mitigation_buffer": 0.1 (unused),
  "max_ob_lookback_candles": 10, "zone_max_age_hours": 24, "prune_after_hours": 48 }
MACRO_CALENDAR_CONFIG = { "enabled": True, "flatten_before_minutes": 5, "resume_after_minutes": 15, "manual_events": [] }
MARKET_SCANNER_CONFIG = { "enabled": True, "refresh_interval_minutes": 0.5, "min_24h_volume_usdt": 3_000_000, "top_n_coins": 50,
  "excluded_bases": ["SPCXB","SOXLB","NVDAB","QQQB","INTCB","KORUB","MUB","SNXXB"], (momentum/vwap/volume-spike keys are unused) }
DYNAMIC_RISK_CONFIG = {...}   # ATR sizing: inactive (trading_pairs.atr is always 0)
POWDER_KEG_CONFIG = {...}     # open-interest "powder keg" shorts: removed from the futures engine, config kept
```
`config.exchange` = ccxt Binance client with `defaultType: "swap"` (used by the futures LIVE path). The spot engine has its own spot client.

**Environment variables (`.env`, never commit):** `BINANCE_API_KEY`, `BINANCE_SECRET_KEY` (use trade-only keys, no withdrawals, IP-whitelisted), `ADMIN_KEY` (**set a long random value or admin actions stay disabled**), `FINNHUB_API_KEY` (optional, paid calendar), `DATABASE_URL` (empty = SQLite), `SQLITE_DB_PATH`, `BOT_NAME`, `ALLOW_LIVE_TRADING` (leave unset), `MACRO_MANUAL_EVENTS`, `APEXTICK_HOST` (default 0.0.0.0; use 127.0.0.1 behind a proxy), `APEXTICK_PORT` (5000), `WS_INSECURE_SSL` (only as a temporary workaround).

## 11. API and dashboard
**Endpoints (`app.py`):** `GET /` (page) · `GET /api/status` (status, mode, ws_connected, btc_gate_allowed/reason, macro_regime, btc_gate_override, macro_halt, prices_age_seconds) · `GET /api/btc-sentiment` · `GET /api/statistics` (spot) and `/api/futures/statistics` (trades, win_rate, net_pnl, portfolio_value = EQUITY, cash_balance, unrealized_pnl, open_positions) · `GET /api/positions` (open trades with real PnL = banked + runner, entry_raw/live_raw, leverage, banked_usdt, unrealized_usdt) · `GET /api/performance` (streaks, profit factor, expectancy, break-even win rate, by direction, 24h/7d) · `GET /api/trades` (last 200 closed per book; futures rows include `direction`) · `GET /api/logs` (500) · `GET /api/market/smc-zones`, `/api/market/powder-keg` · `POST /api/toggle-mode` {paper_trading_enabled: true/false/"1"/"0"} · `POST /api/toggle-btc-gate` {override} · `POST /api/trades/close/<futures|spot>/<id>` · `POST /api/trades/liquidate-all`. Admin POSTs require `X-Admin-Key`; errors come back as `{"error": "..."}`.

**Dashboard (`templates/dashboard.html`, ~980 lines, one inline script):** banners (LIVE funds, macro halt, BTC override, stale price feed) · header toggles (LIVE FUNDS, BTC OVERRIDE, LIQUIDATE ALL) + badges (system status, WS state, data age/PAUSED when tab hidden) · KPI cards (futures net PnL / spot net / open positions / portfolio equity + cash) · BTC gate widget with the block reason · Powder-keg OI table · SMC battle map · **Active positions** (SL/TP1 distances, "risk at stop % of margin", red "STOP BEYOND LIQUIDATION" flag, banked + runner split, Force Close with confirmation) · **Performance panel** (futures and spot) · Futures exits (with LONG/SHORT Type column) and Spot exits · system logs. Polling: fast data every 3 s, zones/OI/history/performance every ~15 s, logs every ~6 s, paused while the tab is hidden. Admin actions use a masked-key modal; the key is kept in memory for 5 minutes only.

## 12. Tests and tools
Run everything: `python3 run_all_tests.py` (each suite builds a temp SQLite DB; no network). Expected: **9 suites, 201 checks, "ALL GOOD"**.

| Suite | Checks | Covers |
|---|---|---|
| `test_app_dashboard.py` | 52 | admin protection, toggle validation + LIVE lock, positions/stat math, force-close & liquidate accounting, performance stats (streaks), page renders through Jinja |
| `test_btc_signal.py` | 29 | pressure window, klines, freshness, fail-closed gate, scanner (atomic, held pairs, filters, OI cache), SSL options |
| `test_btc_vs_trades.py` | 12 | candle fetch fallback across exchanges, hourly attribution, report |
| `test_database.py` | 9 | legacy-schema migration, indexes, seeds, batch, prune |
| `test_futures_risk.py` | 23 | zone labels, stop band, risk sizing, cooldowns, fees, balance==PnL, skip log |
| `test_futures_v2.py` | 18 | slippage, TP1/trailing, halt keeps banked profit, fail-safe flags, LIVE lock, stale prices, concurrent credit survives |
| `test_macro_calendar.py` | 18 | boot reset, Finnhub parsing, manual events, windows, backoff |
| `test_smc_mapper.py` | 18 | migration, dedupe/no resurrection, lifecycle, expiry/prune, fetch gating |
| `test_spot_engine.py` | 22 | min TP, entry fee, drawdown stop, loss cooldown, halt, stale, mode flags |

`tools/ui_test.js` (jsdom, 55 checks): `cd tools && npm install && node ui_test.js` (reads `../templates/dashboard.html`, or `DASH=path`). **Test-writing patterns:** set `SQLITE_DB_PATH` before importing `database`; mark prices fresh with `MARKET_STATE["prices_ts"] = time.time() + 10**9`; set `futures_engine.PAPER_SLIPPAGE_PCT = 0.0` for exact-number assertions; SQLite `LIKE` is case-insensitive (a test once matched a lowercase log line by accident).

**Analysis tools (read-only):** `python3 analyze_trades.py [db] [PAPER|LIVE]` (win rate vs break-even, streaks with windows, by direction/stop distance/hold/hour/pair, stop clusters); `python3 btc_vs_trades.py [db] [PAPER|LIVE] [--tf 15m] [--exchange kraken]` (hourly BTC vs trades; if Binance is blocked from your region, e.g. a US Codespace, use `--exchange kraken|coinbase|bitstamp`); `python3 tools/parse_snapshot.py file.mhtml` (read a saved dashboard).

## 13. Runbook
**Dev setup:** `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`; create `.env` (see section 10); `python3 database.py` creates an empty DB; `python3 app.py` starts everything (open http://localhost:5000). A Codespace/PC copy has its OWN paper DB, separate from the server's.
**Before every commit:** `python3 run_all_tests.py` (+ `tools/ui_test.js` if the dashboard changed); `git status` must not list `.env`, `*.db*`, `__pycache__`, `*.bak`.
**Deploy to the server (confirm how the process is supervised: PM2 or plain `python3 app.py`; one process only):**
```bash
cd ~/apextick
# stop the bot first (e.g. pm2 stop all  /  pkill -f app.py)
mkdir -p ~/backup && cp apextick.db* ~/backup/        # backup the ledger (+ -wal/-shm)
git pull origin main                                  # DB is untracked now; if git complains about apextick.db, move it aside, pull, move it back
python3 -c "from database import execute_query; print(execute_query('PRAGMA integrity_check', fetch_one=True))"   # expect 'ok'
# restart the bot, open the dashboard, confirm the PAPER banner/mode and 'UPDATED' badge
```
**Useful checks:** `grep ADMIN_KEY .env` (must be a strong value); logs in the dashboard (look for `futures_skips`, `spot_skips`, "Mapped N pairs", macro/WS warnings); `python3 analyze_trades.py`. **Rollback:** stop the bot, `cp ~/backup/apextick.db* ~/apextick/`.
**One-time cleanup after the SMC upgrade:** `DELETE FROM liquidity_pools WHERE status='INVALIDATED'` (old junk rows otherwise age out in 48 h).

## 14. Results so far (paper)
- **Snapshot Nov 22 (about 10:00 UTC):** equity $292.09 (cash $199.93) vs $300 start. Futures closed trades: 48, wins 8 (16.7%), net -$15.25, avg win +$1.31, avg loss -$0.64, payoff 2.05, **break-even win rate about 33%**, expectancy -$0.32/trade, 40 structural stop-outs, 7 trailing exits, 1 breakeven. The card showed -$12.39 because it includes $2.85 already banked at TP1 on 4 open longs (+$1.85 open runner profit). Spot: 5 closed wins (+$2.14), +$0.49 open, no drawdown stops triggered.
- **Streaks (futures):** best win streak 5 (+$8.31, Nov 21 04:12-04:47 UTC, trailing exits on shorts during the BTC dump); losing streaks **20** (-$12.95, Nov 21 09:23-16:06 UTC) then 1 breakeven win then **18** (-$11.55, Nov 21 22:08 - Nov 22 09:50). Win rate was 7/9 (78%) in the dump and 1/39 (3%) afterwards.
- **Market context (from news reports; verify with `btc_vs_trades.py`):** BTC fell below $60K on Fri Nov 20, extended to roughly $55K (two-year low, ~$1.8-2.7B liquidations) on Sat Nov 21, rebounded 8-10% the same day, failed to hold, then ranged roughly $59-62K through Nov 22-23.
- **Reading of the data:** the bot earns in sharp trends and bleeds in whipsaw/chop; 14 of 40 stops arrived in clusters of correlated positions (all same direction because direction = BTC EMA state); repeat offenders ZEC/HYPE/UNI/DOGE/SOL. Risk sizing works (losses are about $0.5-0.7 instead of $1.5-2). A rough hindsight test: pausing 3 h after 4 consecutive stops would have turned about -$15 into about -$1 (single episode; not proof).
- Earlier (before the upgrades, Nov 17-20) the futures engine did not trade at all for days because of a label bug, then lost mostly on oversized stops (BREV/MMT long losses of -55% to -71% of margin).

## 15. History: what was found and fixed (chronological, condensed)
1. **First review (Nov 17):** read the original code; listed ~40 defects (stale prices, fake fallback data, cooldown bug, wrong exit quantities in LIVE, no exchange-side stops, absolute balance writes, SMC zone resurrection, macro halt stuck after crash, etc.).
2. **Futures never traded:** the engine queried zones with `direction='LONG'/'SHORT'` but the SMC mapper stores `BULLISH/BEARISH`. Fixed by mapping the label. First futures trade appeared immediately.
3. **Futures batch 1 (risk):** stop band 0.4-2%, risk-based sizing ($0.50/stop), account cooldown fixed (newest trade of any status), per-pair cooldown, TP1 2x (was 3x), trailing 1.5% (was 3%), volume filter, entry fee, skip-reason logging.
4. **Dashboard backend (`app.py`):** win rate = PnL > 0, real open PnL (banked + runner), equity, fail-safe mode, validated toggles, ADMIN_KEY enforcement, LIVE lock, force-close/liquidate that credit the account (the old ones silently destroyed paper cash and overstated PnL), LIVE trades refused.
5. **SMC mapper:** candle-keyed unique zones (no resurrection), lifecycle incl. invalidating MITIGATED zones, expiry/prune, batched writes, fetch only on new candles.
6. **Big batch:** data core (freshness, ws watchdog, fail-closed BTC gate, atomic pair sync, tokenized-stock exclusion), macro calendar (boot reset, Finnhub shape, manual events), spot engine (min TP, 12% drawdown stop, entry fee, spot client, narrowest zone), futures v2 (slippage, total-return pnl_pct, halt keeps banked profit, relative balance writes), database (indexes, migrations, batch, WAL pragmas), config.
7. **Dashboard page:** XSS-safe rendering, masked admin dialog with typed confirmations, banners, risk badges, real WS state, BTC override sync, gate reason, tab-hidden state, raw prices (micro-priced coins), LONG/SHORT Type column in futures exits, Performance panel with streaks.
8. **Analysis & deployment help:** Git/Codespaces/Oracle workflow, DB backup/restore, `analyze_trades.py`, `btc_vs_trades.py`.
9. **Brainstorm (Nov 23):** how to make the bot "self-aware" (section 17). No code written for it.

## 16. Known issues and open work (prioritised)
**A. Live-readiness blockers (do NOT go LIVE before these):**
1. Exit quantity is computed from notional/price, not the stored filled quantity -> dust or accidental position flip. Store `qty` at entry; close `qty * fraction`; always send `reduceOnly` on futures exits.
2. No write-ahead order record / idempotency: a failed DB write after a fill can orphan a position or repeat the order. Record a PENDING row with a `clientOrderId` BEFORE sending; reconcile against the exchange.
3. No exchange-side stop orders; stops live only in the bot (a crash leaves leveraged positions unprotected).
4. Margin mode/leverage not set explicitly (use isolated margin); no liquidation-distance check; cross-account netting (one exchange account, several bot accounts).
5. Fill price/fees are assumed, not read from the exchange; no position reconciliation loop.
6. `futures_live_withdraw` accounting (double count) and shared balance columns between PAPER and LIVE.
**B. Strategy / "awareness" (the user's priority):** direction = BTC EMA state for every alt (one correlated bet); no streak/loss breaker; no regime or volatility awareness; no heat/correlation cap; entries on first zone tap; stops sit inside noise on volatile days. See section 17.
**C. Infrastructure:** websocket instability (REST fallback works; consider `!miniTicker@arr`, alternate endpoints); BTC regime never computed because BTC is not in `trading_pairs` (fix: always include BTC/USDT in `RegimeManager.get_active_pairs()`; WARNING: that activates the spot "pause when BTC is RANGING" rule and will reduce spot entries); `macro_halt` never fires without configured events; process supervision on the server unconfirmed; dashboard served over plain HTTP on a public IP (put behind HTTPS/VPN, firewall port 5000; set `APEXTICK_HOST=127.0.0.1` behind a proxy).
**D. Cleanup:** `trading_pairs.atr` never populated (ATR sizing code inactive); unused config keys listed in section 10; `grid_engine.py` stub still runs a thread (harmless; consider disabling); PostgreSQL path untested (`PRAGMA`s are SQLite-specific); paper force-close does not apply slippage; dashboard exits table lacks entry price/leverage; tokenized-stock exclusion list is manual; two obsolete `patch_db*.py` files on the server.

## 17. Roadmap: the "self-aware bot" brainstorm (NOT built - the user asked to brainstorm, not build)
**Key insight:** win rate collapsed from 78% to 3% when the market changed, and the bot kept trading at full size. It needs a **self brain** (is my edge working now?) and a **market brain** (what kind of market is this?).

| Idea | What it does | Helps on Nov 21-22? |
|---|---|---|
| Loss-streak circuit breaker with probation | After N stops pause hours, resume with tiny probe trades, full size after 2 wins | Yes (strongest, simplest) |
| Rolling-expectancy / equity-curve gate | Trade only while the last ~20 trades beat break-even or equity > its moving average | Yes, slower |
| Heat cap | Max 2 same-direction positions + cap on total open risk | Yes (stop clusters) |
| Market state label TREND / CHOP / SHOCK | BTC volatility percentile, efficiency ratio (trend vs chop), "BTC moved 4%+ in 4 h" or liquidation spike -> stand aside for hours; also market breadth/correlation | Likely |
| ATR-aware stops | Minimum stop scales with each coin's ATR; wider stop + smaller size when volatile | Likely fewer noise stops |
| Entry confirmation + higher-timeframe alignment | Don't buy the first tap; wait for a 5m reaction; only trade with the 4h/1d structure | Plausible, needs testing |
| Learning loop | Log market state/setup features at entry, score buckets, auto-disable negative buckets after enough trades, run challenger paper accounts with variants | Pays off over weeks |
| Replay backtester | Re-run the zone logic on months of candles incl. several crash episodes | The only way to avoid curve-fitting to one episode |
| State panel + alerts | Dashboard "bot state" (NORMAL/CAUTION/PAUSED/PROBATION) and Telegram alerts on state changes | Visibility |

**Cautions agreed so far:** one episode is n=1 (generic protections are safe, specific regime filters need more crashes); regime detectors lag (they limit damage, they do not predict reversals); a pause can miss the big trend winners (hence probation trades); with ~50 trades do not use machine learning (simple rules validated by backtest first); the dump itself produced the five best trades.
**Suggested build order:** (1) breaker with probation + heat cap, (2) ATR-aware stops + market state label (and fix BTC regime wiring), (3) entry-time feature logging, challenger accounts, backtester. **Open question for the user:** protect capital in bad markets first, or raise the win rate with better entries first?

## 18. Gotchas
- Zone directions are BULLISH/BEARISH; trade directions are LONG/SHORT (LONG -> BULLISH, SHORT -> BEARISH).
- `execute_query` returns `None` unless `fetch_one`/`fetch_all` is set; rows are dicts. Placeholders are `?`.
- Engines/config constants are read at import time: restart after changing `config.py`.
- `pnl_usdt` on an OPEN futures trade already contains banked TP1 profit and the (negative) entry fee. `margin_usdt` is the ORIGINAL margin.
- The page's "Current PnL" before the fix ignored banked TP1 profit; the API now returns the real figure (`banked_usdt + unrealized_usdt`).
- `btc_trend` in `MARKET_STATE` (24h-change based) is display-only; the futures engine uses its own 1h EMA9/21 state.
- Binance may be unreachable from some regions (HTTP 451) including some Codespaces; scripts accept `--exchange`.
- Local time vs logs: logs and the performance panel are UTC; the user is on UTC+1 (Lagos); the "UPDATED" badge shows local time.
- `dashboard.html` has CRLF line endings; keep them to avoid whole-file diffs. Editing it by search/replace is fine; run `tools/ui_test.js` afterwards.
- Never run two bot processes against one DB/exchange account.

## 19. Suggested first prompt for Cursor
> Read HANDOFF.md and .cursorrules fully, then run `python3 run_all_tests.py` and confirm 9 suites / 201 checks pass. Do not change any code yet. Summarise in your own words (1) how the futures engine decides direction and entries, (2) the accounting invariants, (3) the top 5 open issues. Then wait: the user wants to continue brainstorming how to make the bot "self-aware" (section 17) before building anything.

## 20. Glossary
**SMC** smart-money concepts. **Order Block (OB)** last opposite-colour candle before an impulsive move. **FVG** fair value gap (3-candle imbalance). **Mitigation** price returning into a zone. **Structural SL** stop at the zone edge. **TP1** first target; 75% of original margin closes there, stop moves to entry (breakeven). **Runner** the remaining 25% that trails the best price. **Watermark** best price since entry. **Notional** margin x leverage. **Break-even win rate** loss / (win + loss) from average win and loss. **Profit factor** gross profit / gross loss. **Expectancy** average PnL per trade. **Equity** cash + open positions. **Heat** total open risk. **Powder keg** high open-interest/spot-volume ratio (informational only now). **Paper** simulated fills on live prices.
