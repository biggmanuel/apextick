import os
import time
import requests
from datetime import datetime, timezone, timedelta
from database import execute_query, add_log
from config import MACRO_CALENDAR_CONFIG, FINNHUB_API_KEY

# ==============================================================================
# APEXTICK: MACROECONOMIC CALENDAR & KILL-SWITCH
# ------------------------------------------------------------------------------
# Fixes in this version:
#   * Boot reset really writes macro_halt = 0 (before, the early "state unchanged" return meant a
#     crash during a halt left the engines halted forever).
#   * Finnhub parsing matches the real API: /calendar/economic, {"economicCalendar": [...]},
#     lowercase impact. The calendar is a PAID endpoint; on 401/403 the bot says so once and uses
#     your manual events instead.
#   * Manual events: MACRO_CALENDAR_CONFIG["manual_events"] or MACRO_MANUAL_EVENTS in .env
#     ("2026-12-10 13:30|CPI;2026-12-17 19:00|FOMC", UTC).
#   * Events refresh when the UTC date changes (and every 6h), and fetch today + tomorrow, so
#     early-morning events are not missed.
#   * A failed fetch backs off for 10 minutes instead of hammering the API every 15 seconds.
# ==============================================================================

FINNHUB_URL = "https://finnhub.io/api/v1/calendar/economic"
REFRESH_SECONDS = 6 * 3600
RETRY_SECONDS = 600


def _parse_dt(text: str):
    """Parses 'YYYY-MM-DD HH:MM[:SS]' as UTC. Returns None if it does not parse."""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class MacroCalendar:
    def __init__(self):
        self.enabled = MACRO_CALENDAR_CONFIG.get("enabled", True)
        self.flatten_before = MACRO_CALENDAR_CONFIG.get("flatten_before_minutes", 5)
        self.resume_after = MACRO_CALENDAR_CONFIG.get("resume_after_minutes", 15)
        self.api_key = FINNHUB_API_KEY

        self.api_events = []                 # events fetched from Finnhub
        self.manual_events = self._load_manual_events()
        self.last_fetch_time = None
        self._next_fetch_ts = 0.0
        self._fetched_for_date = None        # UTC date the cached events were fetched on
        self._warned_plan = False
        self.is_halted = False

        self._ensure_db_state()

    # ------------------------------------------------------------------ state
    def _ensure_db_state(self):
        """Creates the macro_halt toggle and RESETS it to 0 on boot, whatever it was before."""
        now_str = datetime.now(timezone.utc).isoformat()
        execute_query(
            "INSERT INTO bot_settings (key, value, updated_at) VALUES ('macro_halt', '0', ?) ON CONFLICT (key) DO NOTHING",
            (now_str,), commit=True
        )
        execute_query("UPDATE bot_settings SET value = '0', updated_at = ? WHERE key = 'macro_halt'", (now_str,), commit=True)
        self.is_halted = False

    def set_halt_state(self, halt: bool, reason: str):
        """Writes the kill-switch command directly to the database for engines to read."""
        if self.is_halted == halt:
            return  # State unchanged

        now_str = datetime.now(timezone.utc).isoformat()
        execute_query(
            "UPDATE bot_settings SET value = ?, updated_at = ? WHERE key = 'macro_halt'",
            ('1' if halt else '0', now_str), commit=True
        )
        self.is_halted = halt

        if halt:
            add_log("WARNING", "macro_killswitch", f"RED ALERT: {reason}. Trading HALTED. Engines ordered to flatten.")
        else:
            add_log("INFO", "macro_killswitch", f"ALL CLEAR: {reason}. Trading RESUMED.")

    # ------------------------------------------------------------------ events
    def _load_manual_events(self):
        raw = list(MACRO_CALENDAR_CONFIG.get("manual_events", []) or [])
        env = os.getenv("MACRO_MANUAL_EVENTS", "")
        raw += [x for x in env.split(";") if x.strip()]
        events = []
        for entry in raw:
            when, _, name = str(entry).partition("|")
            dt = _parse_dt(when)
            if dt is None:
                add_log("WARNING", "macro_killswitch", f"Ignoring manual macro event with bad format: {entry!r} (use 'YYYY-MM-DD HH:MM|Name', UTC)")
                continue
            events.append({"event": name.strip() or "Manual macro event", "time": dt})
        return events

    @staticmethod
    def parse_finnhub(data) -> list:
        """Extracts high-impact US events from a Finnhub economic-calendar response."""
        items = data.get("economicCalendar", []) if isinstance(data, dict) else (data or [])
        events = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("country", "")).upper() != "US":
                continue
            if str(item.get("impact", "")).lower() not in ("high", "3"):
                continue
            dt = _parse_dt(item.get("time"))
            if dt is None:
                continue
            events.append({"event": item.get("event", "Unknown High Impact Event"), "time": dt})
        return events

    @property
    def high_impact_events(self):
        return self.api_events + self.manual_events

    def fetch_todays_events(self):
        """Refreshes the Finnhub events (today + tomorrow, UTC). Never raises; schedules its own retry."""
        now = datetime.now(timezone.utc)
        self._fetched_for_date = now.strftime('%Y-%m-%d')

        if not self.api_key:
            if self.last_fetch_time is None:
                add_log("WARNING", "macro_killswitch", "No Finnhub API key: using manual macro events only (MACRO_MANUAL_EVENTS).")
            self.last_fetch_time = now
            self._next_fetch_ts = time.time() + REFRESH_SECONDS
            return

        params = {
            "from": now.strftime('%Y-%m-%d'),
            "to": (now + timedelta(days=1)).strftime('%Y-%m-%d'),
            "token": self.api_key,
        }
        try:
            response = requests.get(FINNHUB_URL, params=params, timeout=10)
            if response.status_code == 200:
                self.api_events = self.parse_finnhub(response.json())
                self.last_fetch_time = now
                self._next_fetch_ts = time.time() + REFRESH_SECONDS
                add_log("INFO", "macro_killswitch", f"Loaded {len(self.api_events)} High-Impact USD events (today + tomorrow); {len(self.manual_events)} manual.")
            elif response.status_code in (401, 403):
                if not self._warned_plan:
                    self._warned_plan = True
                    add_log("WARNING", "macro_killswitch", "Finnhub economic calendar is not available on this API plan (HTTP "
                            f"{response.status_code}). Using manual events only - set MACRO_MANUAL_EVENTS in .env.")
                self._next_fetch_ts = time.time() + REFRESH_SECONDS
            else:
                add_log("ERROR", "macro_killswitch", f"Finnhub API error: HTTP {response.status_code}")
                self._next_fetch_ts = time.time() + RETRY_SECONDS
        except Exception as e:
            add_log("ERROR", "macro_killswitch", f"Failed to fetch economic calendar: {e}")
            self._next_fetch_ts = time.time() + RETRY_SECONDS

    def _needs_refresh(self, now: datetime) -> bool:
        return time.time() >= self._next_fetch_ts or self._fetched_for_date != now.strftime('%Y-%m-%d')

    def danger_zone(self, now: datetime):
        """Returns (in_danger, reason) for the given moment."""
        for ev in self.high_impact_events:
            halt_start = ev["time"] - timedelta(minutes=self.flatten_before)
            halt_end = ev["time"] + timedelta(minutes=self.resume_after)
            if halt_start <= now <= halt_end:
                return True, f"Approaching {ev['event']} at {ev['time'].strftime('%H:%M')} UTC"
        return False, ""

    # ------------------------------------------------------------------ loop
    def tick(self, now: datetime = None):
        """One monitoring step (separate from the loop so it can be tested)."""
        now = now or datetime.now(timezone.utc)
        if self._needs_refresh(now):
            self.fetch_todays_events()

        in_danger, reason = self.danger_zone(now)
        if in_danger and not self.is_halted:
            self.set_halt_state(True, reason)
        elif not in_danger and self.is_halted:
            self.set_halt_state(False, "Macro event blast radius cleared")

    def monitor_loop(self):
        """Checks every 15 seconds whether we are inside the blast radius of a macro event."""
        while True:
            if not self.enabled:
                time.sleep(60)
                continue
            try:
                self.tick()
            except Exception as e:
                add_log("ERROR", "macro_killswitch", f"Monitor loop error: {e}")
            time.sleep(15)


if __name__ == "__main__":
    calendar = MacroCalendar()
    add_log("INFO", "macro_killswitch", "Macroeconomic Defense System Online.")
    calendar.monitor_loop()