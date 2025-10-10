#!/usr/bin/env python3
"""
Builds nfl.json in the same schema as nhl.json for the MYPYBITE site.

Output (truncated):
{
  "dates": [{
    "date": "2025-10-10",
    "games": [{
      "gamePk": 401547405,
      "gameDate": "2025-10-10T17:00:00Z",
      "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
      "linescore": {
        "currentPeriod": 0,
        "currentPeriodOrdinal": "",
        "currentPeriodTimeRemaining": None
      },
      "teams": {
        "away": {"team": {"abbreviation": "BUF"}, "score": None},
        "home": {"team": {"abbreviation": "NYJ"}, "score": None}
      }
    }, ...]
  }]
}
"""

from __future__ import annotations
import json, sys, datetime as dt

try:
    import requests  # noqa
except Exception:
    print("This script requires 'requests'. In CI we install it automatically.", file=sys.stderr)
    raise

ESPN_URL = "https://site.api.espn.com/apis/v2/sports/football/nfl/scoreboard"

def today_yyyymmdd() -> str:
    # Use UTC "today" so the date param is stable for CI runners
    today = dt.datetime.utcnow().date()
    return today.strftime("%Y%m%d")

def ord_label(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}.get(n, "OT" if n >= 5 else "")

def map_state(espn_state: str) -> tuple[str, str]:
    """Map ESPN state to our Preview/Live/Final trio, return (abstract, detailed)."""
    s = (espn_state or "").upper()
    if "IN_PROGRESS" in s:
        return "Live", "In Progress"
    if "STATUS_HALFTIME" in s or "HALFTIME" in s:
        return "Live", "Halftime"
    if "FINAL" in s:
        return "Final", "Final"
    if "POSTPONED" in s:
        return "Preview", "Postponed"
    # Scheduled / pregame / created / etc.
    return "Preview", "Scheduled"

def normalize(espn: dict) -> dict:
    games_out = []
    for ev in espn.get("events", []):
        gid = int(ev.get("id"))
        gdate = ev.get("date")

        comp = (ev.get("competitions") or [{}])[0]
        status = (ev.get("status") or {}).get("type", {})
        abs_state, det_state = map_state(status.get("name") or status.get("state") or "")

        # period/clock
        period = int(status.get("period") or 0)
        display_clock = status.get("displayClock")

        # competitors
        away_abbr = home_abbr = "UNK"
        away_score = home_score = None
        for side in comp.get("competitors", []):
            abbr = side.get("team", {}).get("abbreviation") or side.get("team", {}).get("shortDisplayName") or "UNK"
            score = side.get("score")
            if score is not None:
                try:
                    score = int(score)
                except Exception:
                    pass
            if side.get("homeAway") == "away":
                away_abbr, away_score = abbr.upper(), score
            else:
                home_abbr, home_score = abbr.upper(), score

        games_out.append({
            "gamePk": gid,
            "gameDate": gdate,
            "status": {
                "abstractGameState": abs_state,
                "detailedState": det_state
            },
            "linescore": {
                "currentPeriod": period,
                "currentPeriodOrdinal": ord_label(period),
                "currentPeriodTimeRemaining": display_clock
            },
            "teams": {
                "away": {"team": {"abbreviation": away_abbr}, "score": away_score},
                "home": {"team": {"abbreviation": home_abbr}, "score": home_score}
            }
        })

    out = {
        "dates": [{
            "date": dt.datetime.utcnow().date().isoformat(),
            "games": games_out
        }]
    }
    return out

def main() -> int:
    params = {"dates": today_yyyymmdd()}
    r = requests.get(ESPN_URL, params=params, timeout=12)
    r.raise_for_status()
    data = r.json()
    payload = normalize(data)

    with open("nfl.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Wrote nfl.json with {len(payload['dates'][0]['games'])} games")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
