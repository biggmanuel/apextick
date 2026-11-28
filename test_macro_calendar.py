import os, sys, tempfile, time
from datetime import datetime, timezone, timedelta
os.environ["SQLITE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "macro.db"); os.environ["DATABASE_URL"] = ""
os.environ["MACRO_MANUAL_EVENTS"] = "2026-12-10 13:30|CPI; 2026-12-17 19:00|FOMC ;not-a-date|Broken"
sys.path.insert(0, os.getcwd())
import database as D; D.init_db()
import macro_calendar as M
q = D.execute_query
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond: fails.append(name)
halt = lambda: q("SELECT value v FROM bot_settings WHERE key='macro_halt'", fetch_one=True)["v"]
UTC = timezone.utc

# ---------------- boot reset: a crash during a halt must not leave the engines halted forever
q("UPDATE bot_settings SET value='1' WHERE key='macro_halt'", commit=True)
cal = M.MacroCalendar()
check("boot resets macro_halt to 0 (was '1' after a crash)", halt() == "0" and cal.is_halted is False)

# ---------------- manual events
check("manual events parsed from MACRO_MANUAL_EVENTS (bad one ignored)", [e["event"] for e in cal.manual_events] == ["CPI", "FOMC"], str(cal.manual_events))
check("bad manual event was logged", q("SELECT COUNT(*) c FROM logs WHERE message LIKE '%bad format%'", fetch_one=True)["c"] == 1)

# ---------------- Finnhub parsing (real response shape: dict, lowercase impact)
resp = {"economicCalendar": [
    {"country": "US", "event": "Core CPI", "impact": "high", "time": "2026-12-10 13:30:00"},
    {"country": "US", "event": "Housing Starts", "impact": "medium", "time": "2026-12-10 13:30:00"},
    {"country": "EU", "event": "ECB Rate", "impact": "high", "time": "2026-12-10 13:15:00"},
    {"country": "us", "event": "NFP", "impact": "High", "time": "2026-12-11 13:30"},
    {"country": "US", "event": "No time", "impact": "high"},
]}
ev = M.MacroCalendar.parse_finnhub(resp)
check("only high-impact US events with a time are kept", [e["event"] for e in ev] == ["Core CPI", "NFP"], str([e["event"] for e in ev]))
check("old list-shaped responses still parse", len(M.MacroCalendar.parse_finnhub(resp["economicCalendar"])) == 2)

# ---------------- blast radius
cpi = datetime(2026, 12, 10, 13, 30, tzinfo=UTC)
check("5 min before the event: danger", cal.danger_zone(cpi - timedelta(minutes=5))[0])
check("15 min after the event: still danger", cal.danger_zone(cpi + timedelta(minutes=15))[0])
check("6 min before / 16 min after: clear", not cal.danger_zone(cpi - timedelta(minutes=6))[0] and not cal.danger_zone(cpi + timedelta(minutes=16))[0])

# ---------------- tick halts and resumes (no API key in the sandbox -> manual events only)
cal.tick(cpi - timedelta(minutes=3))
check("tick inside the window sets macro_halt = 1", halt() == "1" and cal.is_halted)
cal.tick(cpi + timedelta(minutes=30))
check("tick after the window clears it", halt() == "0" and not cal.is_halted)

# ---------------- refresh logic
cal2 = M.MacroCalendar()
cal2._fetched_for_date = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d"); cal2._next_fetch_ts = time.time() + 99999
check("a new UTC date forces a refresh even if the 6h timer has not expired", cal2._needs_refresh(datetime.now(UTC)))
cal2._fetched_for_date = datetime.now(UTC).strftime("%Y-%m-%d")
check("same day + timer running: no refresh", not cal2._needs_refresh(datetime.now(UTC)))

class R:
    def __init__(self, code, body=None): self.status_code, self._b, self.text = code, body, ""
    def json(self): return self._b
calls = []
def fake_get(code, body=None, boom=False):
    def _g(url, params=None, timeout=None):
        calls.append((url, params))
        if boom: raise RuntimeError("network down")
        return R(code, body)
    return _g
cal3 = M.MacroCalendar(); cal3.api_key = "k"
M.requests.get = fake_get(200, resp); cal3.fetch_todays_events()
check("200: events loaded from the real endpoint", len(cal3.api_events) == 2 and calls[-1][0].endswith("/calendar/economic"), calls[-1][0])
check("window = today + tomorrow, token sent as a param (not in the logged URL)", calls[-1][1]["to"] > calls[-1][1]["from"] and "token" in calls[-1][1])
check("success: next refresh in ~6h", 5.9 * 3600 < cal3._next_fetch_ts - time.time() <= 6 * 3600 + 5)

cal4 = M.MacroCalendar(); cal4.api_key = "k"
M.requests.get = fake_get(403)
cal4.fetch_todays_events(); cal4.fetch_todays_events()
check("403 (paid plan): warns ONCE and falls back to manual events", q("SELECT COUNT(*) c FROM logs WHERE message LIKE '%not available on this API plan%'", fetch_one=True)["c"] == 1 and len(cal4.high_impact_events) == 2)

cal5 = M.MacroCalendar(); cal5.api_key = "k"
M.requests.get = fake_get(0, boom=True); n_before = len(calls)
cal5.fetch_todays_events()
check("network error: retry in ~10 min, not every 15 seconds", 590 < cal5._next_fetch_ts - time.time() <= 605)
cal5.tick(datetime.now(UTC)); cal5.tick(datetime.now(UTC))
check("tick does not re-fetch during the back-off", len(calls) == n_before + 1, f"calls {len(calls) - n_before}")

print("\nFAILED:", fails if fails else "none"); sys.exit(1 if fails else 0)
