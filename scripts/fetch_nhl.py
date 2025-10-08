#!/usr/bin/env python3
"""
Build nhl.json from official NHL endpoints, resilient to DNS/host hiccups.

Inputs (optional):
  - SCHEDULE_DATE   (YYYY-MM-DD; default: "today" in America/Toronto)
  - OUTFILE         (default: nhl.json)

This script:
  1) Tries multiple NHL hosts (different CDNs / APIs).
  2) Retries with backoff on DNS/HTTP errors.
  3) Normalizes to your front-end’s compact shape.
"""

import os, sys, json, time, socket, datetime
import urllib.request, urllib.error

OUTFILE = os.environ.get("OUTFILE", "nhl.json")
SCHEDULE_DATE = os.environ.get("SCHEDULE_DATE")  # YYYY-MM-DD or None


def today_eastern() -> str:
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Toronto")
        d = datetime.datetime.now(tz)
    except Exception:
        d = datetime.datetime.utcnow()
    return d.strftime("%Y-%m-%d")


DATE_STR = SCHEDULE_DATE or today_eastern()

# --- Candidate endpoints (different DNS/edges) ---
# 1) statsapi.web.nhl.com classic v1 (dates[0].games)
# 2) api-web.nhle.com modern web API (gameWeek[].games or dates[].games)
# 3) If the above two both fail, we hard-fail.
CANDIDATES = [
    {
        "name": "statsapi",
        "url": lambda d: f"https://statsapi.web.nhl.com/api/v1/schedule?date={d}&hydrate=linescore,team",
        "kind": "statsapi",
    },
    {
        "name": "api-web",
        "url": lambda d: f"https://api-web.nhle.com/v1/schedule/{d}",
        "kind": "apiweb",
    },
]


def _req(url: str):
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": "nhl-json-builder/2.0 (+github actions)",
            "Accept": "application/json",
        },
    )


def fetch_with_retries(url: str, attempts: int = 6, first_delay: float = 0.9):
    delay = first_delay
    last = None
    for i in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(_req(url), timeout=22) as r:
                if r.status >= 500:
                    raise urllib.error.HTTPError(url, r.status, "Server error", r.headers, None)
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, socket.gaierror) as e:
            last = e
        except Exception as e:
            last = e
        if i < attempts:
            time.sleep(delay)
            delay = min(delay * 1.8, 10.0)
        else:
            raise last or RuntimeError("Unknown fetch error")


# ---------- Normalizers ----------
def norm_from_statsapi(data: dict, date_str: str):
    """statsapi.web.nhl.com shape: {'dates':[{'games':[...]}]}"""
    dates = (data or {}).get("dates") or []
    games = []
    if dates and dates[0].get("games"):
        for g in dates[0]["games"]:
            status = g.get("status") or {}
            ls = g.get("linescore") or {}
            teams = g.get("teams") or {}
            aw = teams.get("away") or {}
            hm = teams.get("home") or {}
            awt = aw.get("team") or {}
            hmt = hm.get("team") or {}

            def abbr(t):
                return (t.get("abbreviation") or t.get("triCode") or (t.get("name", "")[:3].upper() if t.get("name") else "")).upper()

            games.append(
                {
                    "gamePk": g.get("gamePk"),
                    "gameDate": g.get("gameDate"),
                    "status": {
                        "abstractGameState": status.get("abstractGameState", "") or "",
                        "detailedState": status.get("detailedState", "") or status.get("abstractGameState", "") or "",
                    },
                    "teams": {
                        "away": {"team": {"abbreviation": abbr(awt), "triCode": abbr(awt)}, "score": aw.get("score", 0)},
                        "home": {"team": {"abbreviation": abbr(hmt), "triCode": abbr(hmt)}, "score": hm.get("score", 0)},
                    },
                    "linescore": {
                        "currentPeriod": ls.get("currentPeriod") or 0,
                        "currentPeriodOrdinal": ls.get("currentPeriodOrdinal") or "",
                        "currentPeriodTimeRemaining": ls.get("currentPeriodTimeRemaining") or "",
                    },
                }
            )
    return {"dates": [{"date": date_str, "games": games}]}


def norm_from_apiweb(data, date_str: str):
    """
    api-web.nhle.com/v1/schedule/{YYYY-MM-DD}
    Observed shapes:
      - {'gameWeek':[{'date': 'YYYY-MM-DD', 'games':[...]} , ...]}
      - or sometimes {'dates':[{'date': '...', 'games':[...]}]}
      - each game has keys like:
          startTimeUTC, gameState, awayTeam:{abbrev}, homeTeam:{abbrev}, score or boxscore?
    """
    # unify list of games for the requested date
    games_raw = []

    if isinstance(data, dict):
        if "gameWeek" in data and isinstance(data["gameWeek"], list):
            for day in data["gameWeek"]:
                if (day or {}).get("date") == date_str and day.get("games"):
                    games_raw.extend(day["games"])
        if "dates" in data and isinstance(data["dates"], list):
            for day in data["dates"]:
                if (day or {}).get("date") == date_str and day.get("games"):
                    games_raw.extend(day["games"])
        # Some responses are just {'games':[...], 'date':'...'}
        if not games_raw and "games" in data and isinstance(data["games"], list):
            # If there is a top-level 'date', ensure match or accept if missing.
            if (data.get("date") in (None, "", date_str)) or (data.get("date") == date_str):
                games_raw.extend(data["games"])

    games = []
    for g in games_raw:
        # Fields vary; be defensive.
        # Times: startTimeUTC or gameDate
        game_date = g.get("startTimeUTC") or g.get("gameDate") or ""
        # State: gameState (e.g., "FUT", "LIVE", "FINAL") or status/detailedState
        state = (g.get("gameState") or "").upper()
        if not state and isinstance(g.get("status"), dict):
            st = g["status"]
            state = (st.get("detailedState") or st.get("abstractGameState") or "").upper()

        def map_state(s: str):
            s = (s or "").upper()
            if any(x in s for x in ("IN_PROGRESS", "LIVE", "STARTED")):
                return "Live"
            if any(x in s for x in ("FINAL", "OFF")) or s == "FINAL":
                return "Final"
            if any(x in s for x in ("PRE", "FUT", "SCHEDULED")):
                return "Preview"
            return "Unknown"

        # Teams
        aw = g.get("awayTeam") or {}
        hm = g.get("homeTeam") or {}
        # Abbrev keys vary: "abbrev" or "abbreviation" or nested
        def abbr(node):
            return (
                node.get("abbrev")
                or node.get("abbreviation")
                or (node.get("triCode") if isinstance(node.get("triCode"), str) else None)
                or (node.get("name", "")[:3].upper() if node.get("name") else "")
                or ""
            ).upper()

        aw_abbr = abbr(aw) or "AWY"
        hm_abbr = abbr(hm) or "HOM"

        # Scores: can be nested under "score" or boxscore
        aw_score = (
            (aw.get("score") if isinstance(aw.get("score"), int) else None)
            or (g.get("awayTeamScore") if isinstance(g.get("awayTeamScore"), int) else None)
            or 0
        )
        hm_score = (
            (hm.get("score") if isinstance(hm.get("score"), int) else None)
            or (g.get("homeTeamScore") if isinstance(g.get("homeTeamScore"), int) else None)
            or 0
        )

        # Period info (if live) — many api-web responses omit; keep empty
        current_period = 0
        current_period_ord = ""
        time_remaining = ""

        games.append(
            {
                "gamePk": g.get("id") or g.get("gamePk"),  # api-web uses 'id'
                "gameDate": game_date,
                "status": {
                    "abstractGameState": map_state(state),
                    "detailedState": state or map_state(state),
                },
                "teams": {
                    "away": {"team": {"abbreviation": aw_abbr, "triCode": aw_abbr}, "score": aw_score},
                    "home": {"team": {"abbreviation": hm_abbr, "triCode": hm_abbr}, "score": hm_score},
                },
                "linescore": {
                    "currentPeriod": current_period,
                    "currentPeriodOrdinal": current_period_ord,
                    "currentPeriodTimeRemaining": time_remaining,
                },
            }
        )

    return {"dates": [{"date": date_str, "games": games}]}


def build_payload(date_str: str):
    errors = []
    for cand in CANDIDATES:
        url = cand["url"](date_str)
        # DNS warm-up (non-fatal)
        try:
            host = urllib.request.urlparse(url).hostname
            if host:
                socket.gethostbyname(host)
        except Exception as e:
            print(f"Warning: DNS resolve failed for {host}; will still try HTTP. ({e})", file=sys.stderr)

        try:
            data = fetch_with_retries(url)
            if cand["kind"] == "statsapi":
                return norm_from_statsapi(data, date_str)
            else:
                return norm_from_apiweb(data, date_str)
        except Exception as e:
            errors.append((cand["name"], str(e)))
            continue

    # If we reached here, all candidates failed
    msgs = "; ".join([f"{name}: {msg}" for name, msg in errors])
    raise RuntimeError(f"All NHL endpoints failed: {msgs}")


def main():
    try:
        payload = build_payload(DATE_STR)
    except Exception as e:
        print(f"Error building NHL payload: {e}", file=sys.stderr)
        sys.exit(1)

    with open(OUTFILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ": "))

    print(f"Wrote {OUTFILE} for date {DATE_STR} with {len(payload['dates'][0]['games'])} games.")


if __name__ == "__main__":
    main()
