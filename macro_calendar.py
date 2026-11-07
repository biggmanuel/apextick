import time
import requests
from datetime import datetime, timezone, timedelta
from database import execute_query, add_log
from config import MACRO_CALENDAR_CONFIG, FINNHUB_API_KEY

# ==============================================================================
# APEXTICK: MACROECONOMIC CALENDAR & NLP KILL-SWITCH
# ==============================================================================

class MacroCalendar:
    def __init__(self):
        self.enabled = MACRO_CALENDAR_CONFIG.get("enabled", True)
        self.flatten_before = MACRO_CALENDAR_CONFIG.get("flatten_before_minutes", 5)
        self.resume_after = MACRO_CALENDAR_CONFIG.get("resume_after_minutes", 15)
        self.api_key = FINNHUB_API_KEY
        
        self.high_impact_events = []
        self.last_fetch_time = None
        self.is_halted = False

        self._ensure_db_state()

    def _ensure_db_state(self):
        """Ensures the macro_halt toggle exists in the database on boot."""
        now_str = datetime.now(timezone.utc).isoformat()
        execute_query(
            "INSERT OR IGNORE INTO bot_settings (key, value, updated_at) VALUES ('macro_halt', '0', ?)",
            (now_str,),
            commit=True
        )
        # Reset to 0 on boot just in case it crashed during a halt
        self.set_halt_state(False, "System Boot / Reset")

    def set_halt_state(self, halt: bool, reason: str):
        """Writes the kill-switch command directly to the database for engines to read."""
        if self.is_halted == halt:
            return # State unchanged
            
        now_str = datetime.now(timezone.utc).isoformat()
        val = '1' if halt else '0'
        
        execute_query(
            "UPDATE bot_settings SET value = ?, updated_at = ? WHERE key = 'macro_halt'",
            (val, now_str),
            commit=True
        )
        self.is_halted = halt
        
        if halt:
            add_log("WARNING", "macro_killswitch", f"RED ALERT: {reason}. Trading HALTED. Engines ordered to flatten.")
        else:
            add_log("INFO", "macro_killswitch", f"ALL CLEAR: {reason}. Trading RESUMED.")

    def fetch_todays_events(self):
        """
        Pings the Finnhub Economic Calendar API for today's USD events.
        Filters for High-Impact volatility catalysts.
        """
        if not self.api_key:
            if self.last_fetch_time is None: # Only log this once on boot
                add_log("WARNING", "macro_killswitch", "No Finnhub API Key found. Macro Kill-Switch running in passive monitoring mode.")
            self.last_fetch_time = datetime.now(timezone.utc)
            return

        add_log("INFO", "macro_killswitch", "Fetching daily macroeconomic schedule...")
        
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        url = f"https://finnhub.io/api/v1/economic?from={today}&to={today}&token={self.api_key}"
        
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                events = []
                
                # Filter for critical USD events that destroy algos (FOMC, CPI, NFP)
                for item in data:
                    if item.get("country") == "US" and item.get("impact") == "High":
                        event_time_str = item.get("time") # format: "2026-11-07 14:30:00"
                        if event_time_str:
                            event_dt = datetime.strptime(event_time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                            events.append({
                                "event": item.get("event", "Unknown High Impact Event"),
                                "time": event_dt
                            })
                
                self.high_impact_events = events
                self.last_fetch_time = datetime.now(timezone.utc)
                add_log("INFO", "macro_killswitch", f"Loaded {len(events)} High-Impact USD events for today.")
            else:
                add_log("ERROR", "macro_killswitch", f"API Error: {response.status_code} - {response.text}")
        except Exception as e:
            add_log("ERROR", "macro_killswitch", f"Failed to fetch economic calendar: {e}")

    def monitor_loop(self):
        """Tick-by-tick loop checking if we are inside the blast radius of a macro event."""
        while True:
            if not self.enabled:
                time.sleep(60)
                continue

            now = datetime.now(timezone.utc)
            
            # Refresh calendar once every 12 hours
            if self.last_fetch_time is None or (now - self.last_fetch_time).total_seconds() > 43200:
                self.fetch_todays_events()

            in_danger_zone = False
            danger_reason = ""

            for ev in self.high_impact_events:
                ev_time = ev["time"]
                # Calculate the blast radius
                halt_start = ev_time - timedelta(minutes=self.flatten_before)
                halt_end = ev_time + timedelta(minutes=self.resume_after)

                if halt_start <= now <= halt_end:
                    in_danger_zone = True
                    danger_reason = f"Approaching {ev['event']} at {ev_time.strftime('%H:%M')} UTC"
                    break

            # Execute State Change
            if in_danger_zone and not self.is_halted:
                self.set_halt_state(True, danger_reason)
            elif not in_danger_zone and self.is_halted:
                self.set_halt_state(False, "Macro event blast radius cleared")

            time.sleep(15) # Check timeline every 15 seconds

if __name__ == "__main__":
    calendar = MacroCalendar()
    add_log("INFO", "macro_killswitch", "Macroeconomic Defense System Online.")
    calendar.monitor_loop()