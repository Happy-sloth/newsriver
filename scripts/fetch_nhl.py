#!/usr/bin/env python3
"""
Fetch the official NHL schedule for a given day and write nhl.json
in the flat shape your front-end expects.

Inputs (optional):
  - ENV SCHEDULE_DATE: YYYY-MM-DD (defaults to "today" in America/Toronto)
  - ENV OUTFILE: path to write (defaults to ./nhl.json)
  - ENV NHL_API_HOST: override the NHL stats host (default: statsapi.web.nhl.com)

This script ONLY writes nhl.json — make sure no other script/workflow also writes it.
"""

import os, sys, json, datetime, time, socket
import urllib.request, urllib.error

# ---------- config ----------
API_HOST = os.environ.get("NHL_API_HOST", "statsapi.web.nhl.com").strip() or "statsapi.web.nhl.com"
API_BASE = f"https://{API_HOST}/api/v1/schedule"
# Hydrate linescore so you get currentPeriod/clock when LIVE.
QUERY = "?date={date}&hydrate=linescore,team,seriesSummary,series,game.seriesSummary,game.series"

OUTFILE = os.environ.get("OUTFILE", "nhl.json")
SCHEDULE_DATE = os.environ.get("SCHEDULE_DATE")  # YYYY-MM-DD or None


# Pick "today" in America/Toronto if not provided
def today_eastern():
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Toronto")
        d = datetime.datetime.now(tz)
    except Exception:
        d = datetime.datetime.utcnow()
    return d.strftime("%Y-%m-%d")


DATE_STR = SCHEDULE_DATE or today_eastern()


def fetch_json_with_retries(url: str, attempts: int = 6, first_delay: float = 0.8):
    """
    Robust fetch with exponential backoff.
    Retries on URLError, HTTPError (>= 500), and socket.gaierror (DNS).
    """
    delay = first_delay
    last_exc = None
    for i in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "nhl-json-builder/1.1 (+github actions)",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status >= 500:
                    raise urllib.error.HTTPError(url, resp.status, "Server error", resp.headers, None)
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 4xx likely permanent for this request — only retry 429/5xx
            if e.code == 429 or e.code >= 500:
                last_exc = e
            else:
                raise
        except (urllib.error.URLError, socket.gaierror) as e:
            last_exc = e
        except Exception as e:
            last_exc = e

        if i < attempts:
            time.sleep(delay)
            delay = min(delay * 1.8, 10.0)  # cap the backoff
        else:
            if isinstance(last_exc, Exception):
                raise last_exc
            else:
                raise RuntimeError("Unknown fetch error")


def normalize_game(g):
    """
    Map NHL Stats API game object -> your flattened shape.
    Only the keys your front-end uses are included.
    """
    # Status
    status = g.get("status") or {}
    abstract = status.get("abstractGameState", "") or ""
    detailed = status.get("detailedState", "") or abstract or ""

    # Teams & scores
    teams = g.get("teams") or {}
    away_raw = teams.get("away") or {}
    home_raw = teams.get("home") or {}
    away_team = away_raw.get("team") or {}
    home_team = home_raw.get("team") or {}

    def abbr(team):
        return (
            team.get("abbreviation")
            or team.get("triCode")
            or (team.get("name", "")[:3].upper() if team.get("name") else "")
        )

    away_abbr = (abbr(away_team) or "AWY").upper()
    home_abbr = (abbr(home_team) or "HOM").upper()

    # Linescore (when LIVE/FINAL; empty strings for Preview)
    ls = g.get("linescore") or {}
    current_period = ls.get("currentPeriod") or 0
    current_period_ord = ls.get("currentPeriodOrdinal") or ""
    time_remaining = ls.get("currentPeriodTimeRemaining") or ""

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
    # Small DNS warm-up: try resolving once so we fail fast with a clear message.
    try:
        socket.gethostbyname(API_HOST)
    except Exception:
        # We won’t abort here (retries may still succeed), but we log to stderr for visibility.
        print(f"Warning: DNS resolve failed for {API_HOST}; will retry via HTTP.", file=sys.stderr)

    url = API_BASE + QUERY.format(date=date_str)
    data = fetch_json_with_retries(url)
    dates = data.get("dates") or []

    games = []
    if dates and (dates[0].get("games")):
        for g in dates[0]["games"]:
            games.append(normalize_game(g))

    return {"dates": [{"date": date_str, "games": games}]}


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
