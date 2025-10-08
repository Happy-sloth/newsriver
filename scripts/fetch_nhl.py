#!/usr/bin/env python3
"""
Fetch the official NHL schedule for a given day and write nhl.json
in the flat shape your front-end expects.

Inputs (optional):
  - ENV SCHEDULE_DATE: YYYY-MM-DD (defaults to "today" in America/Toronto)
  - ENV OUTFILE: path to write (defaults to ./nhl.json)

This script ONLY writes nhl.json — make sure no other script/workflow also writes it.
"""

import os, sys, json, datetime, urllib.request, urllib.error

# ---------- config ----------
API = "https://statsapi.web.nhl.com/api/v1/schedule"
# We hydrate linescore so you get currentPeriod/clock when LIVE.
QUERY = "?date={date}&hydrate=linescore,team,seriesSummary,series,game.seriesSummary,game.series"

OUTFILE = os.environ.get("OUTFILE", "nhl.json")
SCHEDULE_DATE = os.environ.get("SCHEDULE_DATE")  # YYYY-MM-DD or None

# Pick "today" in America/Toronto if not provided
def today_eastern():
    # Use UTC now and shift to ET by using fixed offset (handles DST if system tz is UTC).
    # Good enough for schedule write; your front-end formats times for viewers anyway.
    from datetime import timezone, timedelta
    # Eastern offset during summer is -4, winter is -5. We’ll infer from Toronto clock.
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("America/Toronto")
        d = datetime.datetime.now(tz)
    except Exception:
        # Fallback: UTC (safe)
        d = datetime.datetime.utcnow()
    return d.strftime("%Y-%m-%d")

DATE_STR = SCHEDULE_DATE or today_eastern()


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "nhl-json-builder/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_game(g):
    """
    Map NHL Stats API game object -> your flattened shape.
    Only the keys your front-end uses are included.
    """
    # Status
    abstract = (g.get("status", {}) or {}).get("abstractGameState", "") or ""
    detailed = (g.get("status", {}) or {}).get("detailedState", "") or abstract or ""

    # Teams & scores
    away_raw = (g.get("teams", {}) or {}).get("away", {}) or {}
    home_raw = (g.get("teams", {}) or {}).get("home", {}) or {}
    away_team = (away_raw.get("team", {}) or {})
    home_team = (home_raw.get("team", {}) or {})

    def abbr(team):
        # NHL Stats usually has "abbreviation"; triCode is often the same.
        return (team.get("abbreviation")
                or team.get("triCode")
                or (team.get("name","")[:3].upper() if team.get("name") else ""))

    away_abbr = abbr(away_team)
    home_abbr = abbr(home_team)

    # Linescore (when LIVE/FINAL; empty strings for Preview)
    ls = g.get("linescore") or {}
    current_period = ls.get("currentPeriod") or 0
    current_period_ord = ls.get("currentPeriodOrdinal") or ""
    time_remaining = ls.get("currentPeriodTimeRemaining") or ""

    # Assemble
    return {
        "gamePk": g.get("gamePk"),
        "gameDate": g.get("gameDate"),  # UTC ISO8601
        "status": {
            "abstractGameState": abstract,
            "detailedState": detailed,
        },
        "teams": {
            "away": {
                "team": {"abbreviation": away_abbr, "triCode": away_abbr},
                "score": away_raw.get("score", 0),
            },
            "home": {
                "team": {"abbreviation": home_abbr, "triCode": home_abbr},
                "score": home_raw.get("score", 0),
            },
        },
        "linescore": {
            "currentPeriod": current_period,
            "currentPeriodOrdinal": current_period_ord,
            "currentPeriodTimeRemaining": time_remaining,
        },
    }


def build_payload(date_str: str):
    url = API + QUERY.format(date=date_str)
    data = fetch_json(url)
    dates = data.get("dates") or []

    # If no games today (common off-days), keep the same outer shape with empty games.
    games = []
    if dates and (dates[0].get("games")):
        for g in dates[0]["games"]:
            games.append(normalize_game(g))

    return {
        "dates": [
            {
                "date": date_str,
                "games": games,
            }
        ]
    }


def main():
    try:
        payload = build_payload(DATE_STR)
    except urllib.error.HTTPError as e:
        print(f"HTTP error fetching NHL schedule: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error building NHL payload: {e}", file=sys.stderr)
        sys.exit(1)

    # Write compact, stable JSON (no trailing spaces)
    with open(OUTFILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ": "))

    print(f"Wrote {OUTFILE} for date {DATE_STR} with {len(payload['dates'][0]['games'])} games.")


if __name__ == "__main__":
    main()
