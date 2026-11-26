import time
import ccxt
from datetime import datetime, timezone, timedelta
from database import execute_query, add_log, get_connection, IS_POSTGRES
from config import SMC_CONFIG

# ==============================================================================
# APEXTICK: SMART MONEY CONCEPTS (SMC) & LIQUIDITY MAPPER
# ------------------------------------------------------------------------------
# Fixes in this version:
#   * No more zone "resurrection": every zone is keyed by the candle that formed it
#     (unique index), so an INVALIDATED / MITIGATED zone is never re-inserted as PENDING.
#   * A zone is checked against the candles that came AFTER it before it is stored: zones
#     that were already broken are skipped, zones already tapped are stored as MITIGATED.
#   * MITIGATED zones can now be INVALIDATED when price breaks through them (before, they
#     stayed "valid" forever and the engines kept trading them).
#   * Zones expire after zone_max_age_hours, and dead zones are pruned (table no longer
#     grows by thousands of rows an hour).
#   * All writes for a pair / a mitigation pass go in ONE transaction (fewer
#     "database is locked" errors), and klines are only re-fetched when a new 15m candle
#     can exist (about 50 REST calls per 15 minutes instead of per minute).
#   * Order Block search is limited to the candles just before the impulse.
# ==============================================================================

TIMEFRAME_MS = 15 * 60 * 1000   # 15m candles


def _execute_batch(statements) -> int:
    """Runs many (query, params) statements in ONE connection / ONE commit.
    Returns how many rows were actually changed (ignored duplicate inserts count as 0)."""
    if not statements:
        return 0
    conn = get_connection()
    changed = 0
    try:
        cur = conn.cursor()
        for query, params in statements:
            if IS_POSTGRES:
                query = query.replace("?", "%s")
            cur.execute(query, params)
            if cur.rowcount and cur.rowcount > 0:
                changed += cur.rowcount
        conn.commit()
        return changed
    finally:
        conn.close()


class SmartMoneyMapper:
    def __init__(self):
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'timeout': 15000
        })
        self.tick_interval = 15                      # mitigation runs every 15s; mapping only on new candles
        self.enabled = SMC_CONFIG.get("enabled", True)
        self.lookback = SMC_CONFIG.get("lookback_candles", 40)
        self.min_gap = SMC_CONFIG.get("min_fvg_pct_gap", 0.2) / 100.0
        self.max_ob_lookback = SMC_CONFIG.get("max_ob_lookback_candles", 10)
        self.zone_max_age_hours = SMC_CONFIG.get("zone_max_age_hours", 24)
        self.prune_after_hours = SMC_CONFIG.get("prune_after_hours", 48)
        self.next_fetch = {}                         # pair -> epoch seconds when a new candle can exist
        self._last_maintenance = 0.0
        self._ensure_schema()

    # ------------------------------------------------------------------ schema
    def _ensure_schema(self):
        """Adds the candle_time key + indexes (safe to run on every boot)."""
        try:
            execute_query("ALTER TABLE liquidity_pools ADD COLUMN candle_time BIGINT", commit=True)
        except Exception:
            pass  # column already exists
        execute_query("CREATE UNIQUE INDEX IF NOT EXISTS ux_pools_candle ON liquidity_pools (pair, pool_type, direction, candle_time)", commit=True)
        execute_query("CREATE INDEX IF NOT EXISTS ix_pools_pair_status ON liquidity_pools (pair, status)", commit=True)

    # ------------------------------------------------------------------ data
    def get_active_pairs(self):
        """Fetch all institutional pairs from the ledger."""
        rows = execute_query("SELECT pair FROM trading_pairs WHERE status = 'ACTIVE'", fetch_all=True)
        return [r["pair"] for r in rows] if rows else []

    def fetch_klines(self, pair, timeframe='15m'):
        """Fetch recent candlestick arrays. 15m is optimal for intraday structural voids."""
        try:
            return self.exchange.fetch_ohlcv(pair, timeframe=timeframe, limit=self.lookback + 5)
        except Exception as e:
            add_log("WARNING", "smc_mapper", f"Failed to fetch klines for {pair}: {e}")
            return None

    # ------------------------------------------------------------------ structure
    @staticmethod
    def _status_from_history(direction, top, bottom, later_candles):
        """
        Looks at the candles that came after the zone formed.
        Returns None if price already broke through it, 'MITIGATED' if it was already tapped,
        otherwise 'PENDING'.
        """
        status = "PENDING"
        for c in later_candles:
            high, low = c[2], c[3]
            if direction == "BULLISH":
                if low < bottom:
                    return None
                if low <= top:
                    status = "MITIGATED"
            else:
                if high > top:
                    return None
                if high >= bottom:
                    status = "MITIGATED"
        return status

    def map_structure(self, pair, ohlcv):
        """
        Mathematical extraction of Fair Value Gaps (FVG) and Order Blocks (OB).
        OHLCV Index: 0=Time, 1=Open, 2=High, 3=Low, 4=Close, 5=Volume
        Returns the number of NEW zones stored.
        """
        if not ohlcv or len(ohlcv) < 5:
            return 0

        n = len(ohlcv)
        found = []   # (type, direction, top, bottom, candle_time_ms, formation_index)

        for i in range(2, n - 1):
            c1_high, c1_low = ohlcv[i-2][2], ohlcv[i-2][3]
            c2_open, c2_close = ohlcv[i-1][1], ohlcv[i-1][4]
            c3_high, c3_low = ohlcv[i][2], ohlcv[i][3]

            # --- BULLISH STRUCTURE (Price expanding upwards) ---
            if c1_high < c3_low and c2_close > c2_open:
                if (c3_low - c1_high) / c1_high >= self.min_gap:
                    found.append(("FVG", "BULLISH", c3_low, c1_high, int(ohlcv[i-1][0]), i))
                    # Bullish Order Block = last red candle shortly before the impulse
                    for j in range(i - 2, max(i - 2 - self.max_ob_lookback, -1), -1):
                        if ohlcv[j][4] < ohlcv[j][1]:
                            found.append(("ORDER_BLOCK", "BULLISH", ohlcv[j][2], ohlcv[j][3], int(ohlcv[j][0]), i))
                            break

            # --- BEARISH STRUCTURE (Price expanding downwards) ---
            elif c1_low > c3_high and c2_close < c2_open:
                if (c1_low - c3_high) / c3_high >= self.min_gap:
                    found.append(("FVG", "BEARISH", c1_low, c3_high, int(ohlcv[i-1][0]), i))
                    # Bearish Order Block = last green candle shortly before the impulse
                    for j in range(i - 2, max(i - 2 - self.max_ob_lookback, -1), -1):
                        if ohlcv[j][4] > ohlcv[j][1]:
                            found.append(("ORDER_BLOCK", "BEARISH", ohlcv[j][2], ohlcv[j][3], int(ohlcv[j][0]), i))
                            break

        if not found:
            return 0

        # One query for the zones that are still alive on this pair (near-duplicate guard)
        live = execute_query(
            "SELECT pool_type, direction, top_price FROM liquidity_pools WHERE pair = ? AND status IN ('PENDING', 'MITIGATED')",
            (pair,), fetch_all=True
        ) or []

        now_str = datetime.now(timezone.utc).isoformat()
        statements, seen = [], set()
        for ptype, direction, top, bottom, candle_ms, idx in found:
            key = (ptype, direction, candle_ms)
            if key in seen:
                continue
            seen.add(key)

            status = self._status_from_history(direction, top, bottom, ohlcv[idx + 1:])
            if status is None:
                continue   # already broken by later candles

            tolerance = top * 0.005
            if any(z["pool_type"] == ptype and z["direction"] == direction and abs(float(z["top_price"]) - top) < tolerance for z in live):
                continue   # a live zone already sits at this level

            created = datetime.fromtimestamp(candle_ms / 1000.0, tz=timezone.utc).isoformat()
            statements.append((
                """
                INSERT INTO liquidity_pools
                (pair, pool_type, direction, top_price, bottom_price, status, created_at, updated_at, candle_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (pair, ptype, direction, top, bottom, status, created, now_str, candle_ms)
            ))

        return _execute_batch(statements)

    # ------------------------------------------------------------------ lifecycle
    def manage_mitigation(self):
        """
        Tracks live prices against every live zone (PENDING and MITIGATED).
        PENDING -> MITIGATED when price taps it; PENDING/MITIGATED -> INVALIDATED when price breaks it.
        """
        pools = execute_query(
            "SELECT id, pair, direction, top_price, bottom_price, status FROM liquidity_pools WHERE status IN ('PENDING', 'MITIGATED')",
            fetch_all=True
        )
        if not pools:
            return

        prices_db = execute_query("SELECT pair, last_price FROM trading_pairs", fetch_all=True)
        live_prices = {row["pair"]: row["last_price"] for row in prices_db} if prices_db else {}

        now_str = datetime.now(timezone.utc).isoformat()
        updates, mitigated, invalidated = [], 0, 0

        for pool in pools:
            price = live_prices.get(pool["pair"])
            if not price or price <= 0:
                continue

            top, bottom = float(pool["top_price"]), float(pool["bottom_price"])
            new_status = None

            if pool["direction"] == "BULLISH":
                if price < bottom:
                    new_status = "INVALIDATED"
                elif bottom <= price <= top and pool["status"] == "PENDING":
                    new_status = "MITIGATED"
            else:  # BEARISH
                if price > top:
                    new_status = "INVALIDATED"
                elif bottom <= price <= top and pool["status"] == "PENDING":
                    new_status = "MITIGATED"

            if new_status:
                updates.append(("UPDATE liquidity_pools SET status = ?, updated_at = ? WHERE id = ?", (new_status, now_str, pool["id"])))
                if new_status == "INVALIDATED":
                    invalidated += 1
                else:
                    mitigated += 1

        _execute_batch(updates)
        if updates:
            add_log("INFO", "smc_mapper", f"Mitigation Engine: {mitigated} zones MITIGATED (tapped), {invalidated} zones INVALIDATED (broken).")

    def _maintenance(self):
        """Every 10 minutes: expire old zones and delete dead ones so the table stays small."""
        if time.time() - self._last_maintenance < 600:
            return
        self._last_maintenance = time.time()
        now = datetime.now(timezone.utc)
        expire_cutoff = (now - timedelta(hours=self.zone_max_age_hours)).isoformat()
        prune_cutoff = (now - timedelta(hours=self.prune_after_hours)).isoformat()
        execute_query(
            "UPDATE liquidity_pools SET status = 'EXPIRED', updated_at = ? WHERE status IN ('PENDING', 'MITIGATED') AND created_at < ?",
            (now.isoformat(), expire_cutoff), commit=True
        )
        execute_query(
            "DELETE FROM liquidity_pools WHERE status IN ('INVALIDATED', 'EXPIRED') AND updated_at < ?",
            (prune_cutoff,), commit=True
        )

    def run_smc_cycle(self):
        """Master execution cycle: map new candles (only when due), then update zone states."""
        pairs = self.get_active_pairs()
        now = time.time()
        fetched, added = 0, 0

        for pair in pairs:
            if now < self.next_fetch.get(pair, 0.0):
                continue   # no new 15m candle yet
            ohlcv = self.fetch_klines(pair)
            if ohlcv:
                added += self.map_structure(pair, ohlcv)
                self.next_fetch[pair] = (ohlcv[-1][0] + TIMEFRAME_MS) / 1000.0 + 3.0
                fetched += 1
            else:
                self.next_fetch[pair] = now + 60.0   # retry in a minute
            time.sleep(0.1)  # Anti-rate limit

        if fetched:
            add_log("INFO", "smc_mapper", f"Mapped {fetched} pairs, {added} new zones.")

        self.manage_mitigation()
        self._maintenance()

    def start(self):
        """Infinite background loop."""
        add_log("INFO", "smc_mapper", "Smart Money Concepts (SMC) Architecture Online. Mapping Liquidity Voids.")
        while True:
            try:
                if self.enabled:
                    self.run_smc_cycle()
            except Exception as e:
                add_log("ERROR", "smc_mapper", f"Critical fault in SMC mapping loop: {e}")
            time.sleep(self.tick_interval)

if __name__ == "__main__":
    mapper = SmartMoneyMapper()
    mapper.start()