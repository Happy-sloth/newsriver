#!/usr/bin/env python3
# scripts/fetch_nhl.py
#
# Purpose: Build a clean NHL relay JSON for MyPyBITE flipboard.
# - Gets today's schedule (and yesterday if before ~4 a.m. Toronto time)
# - For LIVE games, hydrates from per-game live feed to pick up latest goals/period
# - For PREVIEW/FINAL, falls back to schedule data
# - Emits a single object shaped like NHL schedule with dates[0].games[…]
# - Adds generated_utc and source fields at top level
#
# Writes: nhl.json  (repo root, per your front-end expectations)

from __future__ import annotations
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None  # Fallback to UTC if unavailable

SCHEDULE_URL = "https://statsapi.web.nhl.com/api/v1/schedule?date={date}"
LIVE_FEED_URL = "https://statsapi.web.nhl.com/api/v1/game/{gamePk}/feed/live"

# --- Config ---
REQUEST_TIMEOUT = 7            # seconds
RETRY = 2                      # simple retry for GETs
LOCAL_TZ = "America/Toronto"   # for early-morning include-yesterday rule
EARLY_MORNING_HOUR = 4         # include yesterday if now < this hour
OUT_PATH = "nhl.json"

def _now_toronto() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo(LOCAL_TZ))
    # Fallback: UTC (slightly less perfect for boundary rule)
    return datetime.now(timezone.utc)

def _http_get_json(url: str) -> dict:
    last_err = None
    for _ in range(RETRY + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MyPyBITE/nhl-relay"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(0.6)
    raise RuntimeError(f"GET failed: {url} :: {last_err}")

def _dates_to_fetch() -> list[str]:
    now = _now_toronto()
    today = now.date()
    want = [today.strftime("%Y-%m-%d")]
    if now.hour < EARLY_MORNING_HOUR:
        y = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        want.append(y)
    return want

def _normalize_from_schedule_game(g: dict) -> dict:
    """Map schedule game node into our normalized game entry."""
    # Teams block in schedule:
    a = g.get("teams", {}).get("away", {})
    h = g.get("teams", {}).get("home", {})
    a_team = a.get("team", {}) or {}
    h_team = h.get("team", {}) or {}

    # Scores are present in schedule for live/final, often 0 for preview
    a_score = a.get("score")
    h_score = h.get("score")

    status = g.get("status", {}) or {}
    abstract = status.get("abstractGameState", "") or ""
    detailed = status.get("detailedState", "") or ""
    game_pk = g.get("gamePk")
    game_date = g.get("gameDate")

    # linescore fields are not reliable in schedule; we fill best-effort here
    linescore = {
        "currentPeriod": None,
        "currentPeriodOrdinal": "",
        "currentPeriodTimeRemaining": "",
    }

    return {
        "gamePk": game_pk,
        "gameDate": game_date,
        "status": {
            "abstractGameState": abstract,
            "detailedState": detailed,
        },
        "teams": {
            "away": {
                "team": {
                    "abbreviation": a_team.get("abbreviation") or a_team.get("triCode") or "",
                    "triCode": a_team.get("triCode") or a_team.get("abbreviation") or "",
                },
                "score": a_score if a_score is not None else 0 if abstract.lower() == "preview" else a_score,
            },
            "home": {
                "team": {
                    "abbreviation": h_team.get("abbreviation") or h_team.get("triCode") or "",
                    "triCode": h_team.get("triCode") or h_team.get("abbreviation") or "",
                },
                "score": h_score if h_score is not None else 0 if abstract.lower() == "preview" else h_score,
            },
        },
        "linescore": linescore,
    }

def _hydrate_from_live_feed(game_pk: int, base_entry: dict) -> dict:
    """Overlay live feed data onto base entry for accurate, up-to-date scoring."""
    data = _http_get_json(LIVE_FEED_URL.format(gamePk=game_pk))

    # Abbreviations from live feed (more consistent)
    home_t = data.get("gameData", {}).get("teams", {}).get("home", {}) or {}
    away_t = data.get("gameData", {}).get("teams", {}).get("away", {}) or {}

    ls = data.get("liveData", {}).get("linescore", {}) or {}

    home_goals = (ls.get("teams", {}).get("home", {}) or {}).get("goals")
    away_goals = (ls.get("teams", {}).get("away", {}) or {}).get("goals")

    current_period = ls.get("currentPeriod")
    current_period_ord = ls.get("currentPeriodOrdinal") or ""
    current_time_rem = ls.get("currentPeriodTimeRemaining") or ""

    abstract = base_entry["status"].get("abstractGameState") or ""
    detailed = base_entry["status"].get("detailedState") or ""

    # Replace with live values when present
    base_entry["teams"]["home"]["team"]["abbreviation"] = home_t.get("abbreviation") or home_t.get("triCode") or base_entry["teams"]["home"]["team"]["abbreviation"]
    base_entry["teams"]["home"]["team"]["triCode"] = home_t.get("triCode") or home_t.get("abbreviation") or base_entry["teams"]["home"]["team"]["triCode"]
    base_entry["teams"]["away"]["team"]["abbreviation"] = away_t.get("abbreviation") or away_t.get("triCode") or base_entry["teams"]["away"]["team"]["abbreviation"]
    base_entry["teams"]["away"]["team"]["triCode"] = away_t.get("triCode") or away_t.get("abbreviation") or base_entry["teams"]["away"]["team"]["triCode"]

    if away_goals is not None:
        base_entry["teams"]["away"]["score"] = away_goals
    if home_goals is not None:
        base_entry["teams"]["home"]["score"] = home_goals

    base_entry["linescore"] = {
        "currentPeriod": current_period,
        "currentPeriodOrdinal": current_period_ord,
        "currentPeriodTimeRemaining": current_time_rem,
    }

    # Some feeds flip state strings; keep schedule status but prefer LIVE if linescore is active
    if current_period:
        abstract = "Live"
        detailed = "LIVE"
    base_entry["status"]["abstractGameState"] = abstract
    base_entry["status"]["detailedState"] = detailed

    return base_entry

def _collect_games_for_date(date_str: str) -> list[dict]:
    sched = _http_get_json(SCHEDULE_URL.format(date=date_str))
    games = []
    for g in (sched.get("dates") or [{}])[0].get("games", []):
        entry = _normalize_from_schedule_game(g)
        state = (entry["status"]["abstractGameState"] or "").lower()

        # Only hydrate from per-game feed if live/in progress
        if state in ("live", "in progress", "in_progress"):
            try:
                entry = _hydrate_from_live_feed(entry["gamePk"], entry)
            except Exception as e:
                # Keep schedule fallback on failure; still better than nothing
                sys.stderr.write(f"[warn] live hydrate failed for {entry['gamePk']}: {e}\n")

        games.append(entry)
    return games

def main() -> int:
    want_dates = _dates_to_fetch()
    all_games: list[dict] = []
    for d in want_dates:
        all_games.extend(_collect_games_for_date(d))

    # If both days fetched, keep unique by gamePk (last write wins — today takes precedence)
    uniq = {}
    for g in all_games:
        uniq[g["gamePk"]] = g
    games_out = list(uniq.values())

    # Shape matches your tolerant front-end normalizer:
    # {"dates":[{"games":[…]}], "generated_utc": "...", "source":[…]}
    out = {
        "generated_utc": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": ["nhl schedule", "nhl live feed"],
        "dates": [{"date": want_dates[0], "games": games_out}],
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"), sort_keys=False)

    print(f"Wrote {OUT_PATH} with {len(games_out)} games (dates: {', '.join(want_dates)})")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
