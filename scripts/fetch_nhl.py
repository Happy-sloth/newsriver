#!/usr/bin/env python3
# scripts/fetch_nhl.py
#
# Purpose: Build a clean NHL relay JSON for MyPyBITE flipboard.
# - Gets today's schedule (ET). If it's before 4 a.m. ET, also includes yesterday.
# - Tries multiple NHL endpoints with retries/backoff (statsapi first, api-web fallback).
# - Normalizes to your existing front-end shape: {"dates":[{"date": "...", "games":[...]}]}
# - Adds generated_utc and source for debugging/freshness.
#
# Env (optional):
#   SCHEDULE_DATE=YYYY-MM-DD   # fetch only this date (skips the early-morning "yesterday" rule)
#   OUTFILE=nhl.json           # output filename (default nhl.json)
#
# Stdlib only.

from __future__ import annotations
import os
import sys
import json
import time
import socket
import datetime
import urllib.request
import urllib.error

OUTFILE = os.environ.get("OUTFILE", "nhl.json")
SCHEDULE_DATE = os.environ.get("SCHEDULE_DATE")  # YYYY-MM-DD or None
EARLY_MORNING_ET_CUTOFF = 4  # include yesterday if now < 04:00 ET
USER_AGENT = "MyPyBITE/nhl-relay (newsriver)"

# ----- time helpers -----
def today_eastern_date() -> datetime.date:
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("America/Toronto"))
    except Exception:
        # Fallback: UTC; not perfect for boundary but safe
        now = datetime.datetime.utcnow()
    return now.date()

def now_eastern_hour() -> int:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/Toronto")).hour
    except Exception:
        return datetime.datetime.utcnow().hour  # fallback

def fmt_date(d: datetime.date) -> str:
    return d.strftime("%Y-%m-%d")

# ----- candidate endpoints (use statsapi first) -----
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

def _req(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )

def fetch_with_retries(url: str, attempts: int = 6, first_delay: float = 0.9):
    delay = first_delay
    last_err = None
    for i in range(1, attempts + 1):
        try:
            # Optional DNS warm-up; non-fatal
            try:
                host = urllib.request.urlparse(url).hostname
                if host:
                    socket.gethostbyname(host)
            except Exception:
                pass

            with urllib.request.urlopen(_req(url), timeout=22) as r:
                if r.status >= 500:
                    raise urllib.error.HTTPError(url, r.status, "Server error", r.headers, None)
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, socket.gaierror) as e:
            last_err = e
        except Exception as e:
            last_err = e

        if i < attempts:
            time.sleep(delay)
            delay = min(delay * 1.8, 10.0)
        else:
            raise last_err or RuntimeError("Unknown fetch error")

# ----- normalizers -----
def _abbr_from_team(team: dict) -> str:
    return (
        team.get("abbreviation")
        or team.get("triCode")
        or (team.get("name", "")[:3].upper() if team.get("name") else "")
        or ""
    ).upper()

def norm_from_statsapi(data: dict, date_str: str) -> list[dict]:
    """Return list of game entries normalized from statsapi schedule response."""
    games_out: list[dict] = []
    dates = (data or {}).get("dates") or []
    if dates and dates[0].get("games"):
        for g in dates[0]["games"]:
            status = g.get("status") or {}
            ls = g.get("linescore") or {}
            teams = g.get("teams") or {}
            aw = teams.get("away") or {}
            hm = teams.get("home") or {}
            awt = aw.get("team") or {}
            hmt = hm.get("team") or {}

            aw_abbr = _abbr_from_team(awt) or "AWY"
            hm_abbr = _abbr_from_team(hmt) or "HOM"

            games_out.append(
                {
                    "gamePk": g.get("gamePk"),
                    "gameDate": g.get("gameDate"),
                    "status": {
                        "abstractGameState": status.get("abstractGameState", "") or "",
                        "detailedState": status.get("detailedState", "") or status.get("abstractGameState", "") or "",
                    },
                    "teams": {
                        "away": {"team": {"abbreviation": aw_abbr, "triCode": aw_abbr}, "score": aw.get("score", 0)},
                        "home": {"team": {"abbreviation": hm_abbr, "triCode": hm_abbr}, "score": hm.get("score", 0)},
                    },
                    "linescore": {
                        "currentPeriod": ls.get("currentPeriod") or 0,
                        "currentPeriodOrdinal": ls.get("currentPeriodOrdinal") or "",
                        "currentPeriodTimeRemaining": ls.get("currentPeriodTimeRemaining") or "",
                    },
                }
            )
    return games_out

def norm_from_apiweb(data: dict, date_str: str) -> list[dict]:
    """Return list of game entries normalized from api-web schedule response."""
    games_raw: list[dict] = []

    if isinstance(data, dict):
        if "gameWeek" in data and isinstance(data["gameWeek"], list):
            for day in data["gameWeek"]:
                if (day or {}).get("date") == date_str and day.get("games"):
                    games_raw.extend(day["games"])
        if "dates" in data and isinstance(data["dates"], list):
            for day in data["dates"]:
                if (day or {}).get("date") == date_str and day.get("games"):
                    games_raw.extend(day["games"])
        if not games_raw and "games" in data and isinstance(data["games"], list):
            if (data.get("date") in (None, "", date_str)) or (data.get("date") == date_str):
                games_raw.extend(data["games"])

    def map_state(s: str) -> str:
        s = (s or "").upper()
        if any(x in s for x in ("IN_PROGRESS", "LIVE", "STARTED")):
            return "Live"
        if any(x in s for x in ("FINAL", "OFF")) or s == "FINAL":
            return "Final"
        if any(x in s for x in ("PRE", "FUT", "SCHEDULED")):
            return "Preview"
        return "Unknown"

    games_out: list[dict] = []
    for g in games_raw:
        game_date = g.get("startTimeUTC") or g.get("gameDate") or ""
        state_raw = (g.get("gameState") or "")
        if not state_raw and isinstance(g.get("status"), dict):
            st = g["status"]
            state_raw = st.get("detailedState") or st.get("abstractGameState") or ""

        aw = g.get("awayTeam") or {}
        hm = g.get("homeTeam") or {}

        def abbr(node: dict) -> str:
            return (
                node.get("abbrev")
                or node.get("abbreviation")
                or (node.get("triCode") if isinstance(node.get("triCode"), str) else None)
                or (node.get("name", "")[:3].upper() if node.get("name") else "")
                or ""
            ).upper()

        aw_abbr = abbr(aw) or "AWY"
        hm_abbr = abbr(hm) or "HOM"

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

        games_out.append(
            {
                "gamePk": g.get("id") or g.get("gamePk"),
                "gameDate": game_date,
                "status": {
                    "abstractGameState": map_state(state_raw),
                    "detailedState": (state_raw or "").upper() or map_state(state_raw),
                },
                "teams": {
                    "away": {"team": {"abbreviation": aw_abbr, "triCode": aw_abbr}, "score": aw_score},
                    "home": {"team": {"abbreviation": hm_abbr, "triCode": hm_abbr}, "score": hm_score},
                },
                "linescore": {
                    "currentPeriod": 0,
                    "currentPeriodOrdinal": "",
                    "currentPeriodTimeRemaining": "",
                },
            }
        )

    return games_out

# ----- per-date fetch using candidates -----
def fetch_games_for_date(date_str: str) -> tuple[list[dict], str]:
    """Return (games, source_name) for a single date, trying candidates in order."""
    errors = []
    for cand in CANDIDATES:
        url = cand["url"](date_str)
        try:
            data = fetch_with_retries(url)
            if cand["kind"] == "statsapi":
                return norm_from_statsapi(data, date_str), cand["name"]
            else:
                return norm_from_apiweb(data, date_str), cand["name"]
        except Exception as e:
            errors.append((cand["name"], str(e)))
            continue
    msgs = "; ".join([f"{name}: {msg}" for name, msg in errors])
    raise RuntimeError(f"All NHL endpoints failed for {date_str}: {msgs}")

def build_payload(primary_date: str, include_yesterday: bool) -> dict:
    sources_used: list[str] = []
    all_games: list[dict] = []

    # primary (today or SCHEDULE_DATE)
    games_today, src_today = fetch_games_for_date(primary_date)
    all_games.extend(games_today)
    sources_used.append(src_today)

    # include yesterday (early morning ET only, and only if not using explicit SCHEDULE_DATE)
    if include_yesterday:
        dt = datetime.datetime.strptime(primary_date, "%Y-%m-%d").date()
        y_str = fmt_date(dt - datetime.timedelta(days=1))
        try:
            games_y, src_y = fetch_games_for_date(y_str)
            # de-dupe by gamePk when both days overlap (unlikely, but safe)
            seen = set()
            merged: list[dict] = []
            for g in all_games + games_y:
                key = g.get("gamePk") or (g.get("teams", {}).get("away", {}).get("team", {}).get("abbreviation", "") + "-" +
                                          g.get("teams", {}).get("home", {}).get("team", {}).get("abbreviation", "") + "-" +
                                          (g.get("gameDate") or ""))
                if key in seen:
                    continue
                seen.add(key)
                merged.append(g)
            all_games = merged
            sources_used.append(src_y)
        except Exception as e:
            print(f"[warn] yesterday fetch failed: {e}", file=sys.stderr)

    payload = {
        "generated_utc": datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": list(dict.fromkeys(sources_used)),  # unique order
        "dates": [{"date": primary_date, "games": all_games}],
    }
    return payload

def main() -> int:
    if SCHEDULE_DATE:
        primary = SCHEDULE_DATE
        include_y = False
    else:
        today = today_eastern_date()
        primary = fmt_date(today)
        include_y = now_eastern_hour() < EARLY_MORNING_ET_CUTOFF

    try:
        payload = build_payload(primary, include_y)
    except Exception as e:
        print(f"Error building NHL payload: {e}", file=sys.stderr)
        return 1

    with open(OUTFILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    print(f"Wrote {OUTFILE} for {primary} with {len(payload['dates'][0]['games'])} games. sources={payload.get('source')}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
