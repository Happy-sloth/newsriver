#!/usr/bin/env python3
# scripts/fetch_nhl.py
#
# Purpose: Build a clean NHL relay JSON for MyPyBITE flipboard.
# - Gets today's schedule in ET. If before 04:00 ET, also includes yesterday.
# - Tries multiple NHL endpoints with retries and backoff (statsapi first, api-web fallback).
# - Normalizes to: {"generated_utc": "...Z", "source": [...], "dates":[{"date":"YYYY-MM-DD","games":[...]}]}
# - Writes to OUTFILE and also to newsriver/OUTFILE if OUTFILE is a bare filename.
#
# Env (optional):
#   SCHEDULE_DATE=YYYY-MM-DD
#   OUTFILE=nhl.json
#   OUTFILE_EXTRA=newsriver/nhl.json   # if set, also write here
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
from typing import Tuple, List, Dict, Any

OUTFILE = os.environ.get("OUTFILE", "nhl.json")
OUTFILE_EXTRA = os.environ.get("OUTFILE_EXTRA")
SCHEDULE_DATE = os.environ.get("SCHEDULE_DATE")  # YYYY-MM-DD or None
EARLY_MORNING_ET_CUTOFF = 4  # include yesterday if now < 04:00 ET
USER_AGENT = "MyPyBITE/nhl-relay (newsriver)"

CANADIAN_ABBRS = {"TOR", "MTL", "OTT", "WPG", "EDM", "CGY", "VAN"}


# ----- time helpers -----
def today_eastern_date() -> datetime.date:
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("America/Toronto"))
    except Exception:
        now = datetime.datetime.utcnow()
    return now.date()

def now_eastern_hour() -> int:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/Toronto")).hour
    except Exception:
        return datetime.datetime.utcnow().hour

def fmt_date(d: datetime.date) -> str:
    return d.strftime("%Y-%m-%d")


# ----- candidate endpoints (statsapi first) -----
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


# ----- state helpers -----
def map_state_generic(val: str) -> str:
    t = (val or "").upper()
    if ("IN_PROGRESS" in t) or ("LIVE" in t) or ("STARTED" in t):
        return "Live"
    if ("FINAL" in t) or (t == "FINAL") or ("OFF" in t):
        return "Final"
    if ("PRE" in t) or ("FUT" in t) or ("SCHEDULED" in t) or ("UPCOMING" in t) or (t == "PREVIEW"):
        return "Preview"
    return "Unknown"


# ----- normalizers -----
def _abbr_from_team(team: Dict[str, Any]) -> str:
    return (
        team.get("abbreviation")
        or team.get("triCode")
        or (team.get("shortName") if isinstance(team.get("shortName"), str) else None)
        or (team.get("name", "")[:3].upper() if team.get("name") else "")
        or ""
    ).upper()

def _score_or_none(x: Any):
    return x if isinstance(x, int) else None

def _final_winner_abbr(away_abbr: str, home_abbr: str, away_score, home_score, state: str):
    if state != "Final":
        return None
    if isinstance(away_score, int) and isinstance(home_score, int) and away_score != home_score:
        return away_abbr if away_score > home_score else home_abbr
    return None

def norm_from_statsapi(data: Dict[str, Any], date_str: str) -> List[Dict[str, Any]]:
    games_out: List[Dict[str, Any]] = []
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

            abstract = status.get("abstractGameState", "") or ""
            detailed = status.get("detailedState", "") or abstract
            mapped = map_state_generic(detailed or abstract)

            away_score = _score_or_none(aw.get("score"))
            home_score = _score_or_none(hm.get("score"))

            games_out.append(
                {
                    "gamePk": g.get("gamePk"),
                    "gameDate": g.get("gameDate"),
                    "status": {
                        "abstractGameState": mapped,
                        "detailedState": (detailed or "").upper() or mapped,
                    },
                    "teams": {
                        "away": {"team": {"abbreviation": aw_abbr, "triCode": aw_abbr}, "score": away_score},
                        "home": {"team": {"abbreviation": hm_abbr, "triCode": hm_abbr}, "score": home_score},
                    },
                    "linescore": {
                        "currentPeriod": ls.get("currentPeriod") or 0,
                        "currentPeriodOrdinal": ls.get("currentPeriodOrdinal") or "",
                        "currentPeriodTimeRemaining": ls.get("currentPeriodTimeRemaining") or "",
                    },
                    "finalWinner": _final_winner_abbr(aw_abbr, hm_abbr, away_score, home_score, mapped),
                }
            )
    return games_out

def norm_from_apiweb(data: Dict[str, Any], date_str: str) -> List[Dict[str, Any]]:
    games_raw: List[Dict[str, Any]] = []

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

    def abbr(node: Dict[str, Any]) -> str:
        return (
            node.get("abbrev")
            or node.get("abbreviation")
            or (node.get("triCode") if isinstance(node.get("triCode"), str) else None)
            or (node.get("name", "")[:3].upper() if node.get("name") else "")
            or ""
        ).upper()

    games_out: List[Dict[str, Any]] = []
    for g in games_raw:
        game_date = g.get("startTimeUTC") or g.get("gameDate") or ""

        state_raw = (g.get("gameState") or "")
        if not state_raw and isinstance(g.get("status"), dict):
            st = g["status"]
            state_raw = st.get("detailedState") or st.get("abstractGameState") or ""
        mapped = map_state_generic(state_raw)

        aw = g.get("awayTeam") or {}
        hm = g.get("homeTeam") or {}

        aw_abbr = abbr(aw) or "AWY"
        hm_abbr = abbr(hm) or "HOM"

        away_score = _score_or_none(
            aw.get("score") if isinstance(aw.get("score"), int) else g.get("awayTeamScore")
        )
        home_score = _score_or_none(
            hm.get("score") if isinstance(hm.get("score"), int) else g.get("homeTeamScore")
        )

        games_out.append(
            {
                "gamePk": g.get("id") or g.get("gamePk"),
                "gameDate": game_date,
                "status": {
                    "abstractGameState": mapped,
                    "detailedState": (state_raw or "").upper() or mapped,
                },
                "teams": {
                    "away": {"team": {"abbreviation": aw_abbr, "triCode": aw_abbr}, "score": away_score},
                    "home": {"team": {"abbreviation": hm_abbr, "triCode": hm_abbr}, "score": home_score},
                },
                "linescore": {
                    "currentPeriod": 0,
                    "currentPeriodOrdinal": "",
                    "currentPeriodTimeRemaining": "",
                },
                "finalWinner": _final_winner_abbr(aw_abbr, hm_abbr, away_score, home_score, mapped),
            }
        )

    return games_out


# ----- per-date fetch using candidates -----
def fetch_games_for_date(date_str: str) -> Tuple[List[Dict[str, Any]], str]:
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

def build_payload(primary_date: str, include_yesterday: bool) -> Dict[str, Any]:
    sources_used: List[str] = []
    all_games: List[Dict[str, Any]] = []

    games_today, src_today = fetch_games_for_date(primary_date)
    all_games.extend(games_today)
    sources_used.append(src_today)

    if include_yesterday:
        dt = datetime.datetime.strptime(primary_date, "%Y-%m-%d").date()
        y_str = fmt_date(dt - datetime.timedelta(days=1))
        try:
            games_y, src_y = fetch_games_for_date(y_str)
            seen = set()
            merged: List[Dict[str, Any]] = []
            for g in all_games + games_y:
                key = g.get("gamePk") or (
                    (g.get("teams", {}).get("away", {}).get("team", {}).get("abbreviation", "") + "-" +
                     g.get("teams", {}).get("home", {}).get("team", {}).get("abbreviation", "") + "-" +
                     (g.get("gameDate") or ""))
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(g)
            all_games = merged
            sources_used.append(src_y)
        except Exception as e:
            print(f"[warn] yesterday fetch failed: {e}", file=sys.stderr)

    payload: Dict[str, Any] = {
        "generated_utc": datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": list(dict.fromkeys(sources_used)),
        "meta": {
            "canadian_abbrs": sorted(CANADIAN_ABBRS),
        },
        "dates": [{"date": primary_date, "games": all_games}],
    }
    return payload


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def _default_extra_path(outfile: str) -> str | None:
    # If OUTFILE is a bare filename like "nhl.json", mirror to "newsriver/nhl.json"
    base = os.path.basename(outfile)
    if base == outfile:
        return os.path.join("newsriver", base)
    return None


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

    # Write primary OUTFILE
    _ensure_dir(OUTFILE)
    with open(OUTFILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    # Write secondary path if requested or if OUTFILE is bare
    extra_path = OUTFILE_EXTRA or _default_extra_path(OUTFILE)
    if extra_path:
        try:
            _ensure_dir(extra_path)
            with open(extra_path, "w", encoding="utf-8") as f2:
                json.dump(payload, f2, ensure_ascii=False, separators=(",", ":"))
        except Exception as e:
            print(f"[warn] failed to write extra output {extra_path}: {e}", file=sys.stderr)

    count = len(payload["dates"][0]["games"])
    sources = payload.get("source")
    print(f"Wrote {OUTFILE} for {primary} with {count} games. sources={sources}")
    if extra_path:
        print(f"Mirrored to {extra_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
