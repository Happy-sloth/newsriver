#!/usr/bin/env python3
# scripts/fetch_nfl.py
#
# Purpose: Build a clean NFL relay JSON for MyPyBITE flipboard.
# - Gets today's schedule (and yesterday if before ~4 a.m. Toronto time)
# - Uses ESPN NFL scoreboard (stable public endpoint)
# - Normalizes into a schedule-like object with dates[0].games[…]
# - Adds generated_utc and source at top level
#
# Writes: nfl.json  (repo root, per your front-end expectations)

from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None  # Fallback to UTC if unavailable

# --- Sources ---
# ESPN scoreboard supports ?dates=YYYYMMDD
SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={yyyymmdd}"

# --- Config ---
REQUEST_TIMEOUT = 8            # seconds
RETRY = 2                      # simple retry for GETs
LOCAL_TZ = "America/Toronto"   # for early-morning include-yesterday rule
EARLY_MORNING_HOUR = 4         # include yesterday if now < this hour
OUT_PATH = "nfl.json"


# --------------- time / http helpers ---------------

def _now_toronto() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo(LOCAL_TZ))
    # Fallback: UTC (slightly less perfect for boundary rule)
    return datetime.now(timezone.utc)


def _http_get_json(url: str) -> dict:
    last_err: Optional[BaseException] = None
    for attempt in range(RETRY + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "MyPyBITE/nfl-relay (+https://www.mypybite.com)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            # jitter-less small backoff
            if attempt < RETRY:
                time.sleep(0.8 * (2 ** attempt))
    raise RuntimeError(f"GET failed: {url} :: {last_err}")


def _dates_to_fetch() -> List[str]:
    """Return list of ISO dates (YYYY-MM-DD). Include yesterday if early morning."""
    now = _now_toronto()
    today = now.date()
    want = [today.strftime("%Y-%m-%d")]
    if now.hour < EARLY_MORNING_HOUR:
        want.append((today - timedelta(days=1)).strftime("%Y-%m-%d"))
    return want


# --------------- normalization helpers ---------------

def _status_to_schedule_like(status_block: Dict[str, Any]) -> Dict[str, str]:
    """
    ESPN status mapping to something close to NHL schedule semantics:
      abstractGameState: Preview | Live | Final
      detailedState: free-text detail ("FINAL", "Q4 02:31", "7:30 PM ET", etc.)
    """
    t = (status_block or {}).get("type", {}) or {}
    name = (t.get("name") or "").upper()           # e.g., STATUS_SCHEDULED, STATUS_IN_PROGRESS, STATUS_FINAL
    detail = t.get("detail") or t.get("description") or t.get("shortDetail") or ""
    # ESPN also gives clock & period fields
    display_clock = (status_block or {}).get("displayClock") or ""
    period = (status_block or {}).get("period")

    if "FINAL" in name:
        abstract = "Final"
        detailed = "FINAL"
    elif "IN_PROGRESS" in name:
        abstract = "Live"
        # Prefer clock+period when present
        if period:
            detailed = f"Q{period} {display_clock}".strip()
        else:
            detailed = detail or "LIVE"
    elif "STATUS_HALFTIME" in name or "HALFTIME" in detail.upper():
        abstract = "Live"
        detailed = "HALFTIME"
    else:
        abstract = "Preview"
        # Scheduled kickoff time if available, else generic
        detailed = detail or "Scheduled"

    return {
        "abstractGameState": abstract,
        "detailedState": detailed,
    }


def _abbr_from_competitor(c: Dict[str, Any]) -> str:
    team = (c or {}).get("team", {}) or {}
    return team.get("abbreviation") or team.get("shortDisplayName") or team.get("name") or ""


def _score_from_competitor(c: Dict[str, Any]) -> int:
    s = (c or {}).get("score")
    try:
        return int(s) if s is not None else 0
    except (TypeError, ValueError):
        return 0


def _linescore_from_status(status_block: Dict[str, Any]) -> Dict[str, Any]:
    """
    Provide a minimal "linescore" block akin to NHL shape, using ESPN period/clock.
    """
    period = (status_block or {}).get("period")
    display_clock = (status_block or {}).get("displayClock") or ""

    # NFL uses quarters 1..4 (and possibly OT as 5)
    if isinstance(period, int) and period > 0:
        current_period = period
        current_period_ordinal = f"Q{period}" if period <= 4 else ("OT" if period == 5 else f"P{period}")
    else:
        current_period = None
        current_period_ordinal = ""

    current_time_remaining = display_clock

    return {
        "currentPeriod": current_period,
        "currentPeriodOrdinal": current_period_ordinal,
        "currentPeriodTimeRemaining": current_time_remaining,
    }


def _normalize_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a single ESPN 'event' into a game entry resembling our NHL relay.
    """
    game_id = ev.get("id")
    game_date = ev.get("date")  # ISO8601

    comps = (ev.get("competitions") or [])
    comp = comps[0] if comps else {}

    status_block = (comp.get("status") or {})
    status = _status_to_schedule_like(status_block)
    linescore = _linescore_from_status(status_block)

    competitors = (comp.get("competitors") or [])
    home = next((c for c in competitors if (c or {}).get("homeAway") == "home"), None)
    away = next((c for c in competitors if (c or {}).get("homeAway") == "away"), None)

    # Some events may be missing, skip safely with placeholders
    home_abbr = _abbr_from_competitor(home or {})
    away_abbr = _abbr_from_competitor(away or {})
    home_score = _score_from_competitor(home or {})
    away_score = _score_from_competitor(away or {})

    # Mirror the NHL keys your front-end expects:
    entry = {
        "gamePk": game_id,              # keep key name for frontend tolerance
        "gameDate": game_date,
        "status": status,
        "teams": {
            "away": {
                "team": {
                    "abbreviation": away_abbr,
                    "triCode": away_abbr,  # NFL doesn't have triCode; reuse abbr
                },
                "score": away_score,
            },
            "home": {
                "team": {
                    "abbreviation": home_abbr,
                    "triCode": home_abbr,
                },
                "score": home_score,
            },
        },
        "linescore": linescore,
    }
    return entry


def _collect_games_for_date(iso_date: str) -> List[Dict[str, Any]]:
    """
    Fetch and normalize all games for one ISO date (YYYY-MM-DD).
    """
    yyyymmdd = iso_date.replace("-", "")
    data = _http_get_json(SCOREBOARD_URL.format(yyyymmdd=yyyymmdd))
    events = (data or {}).get("events") or []
    out: List[Dict[str, Any]] = []
    for ev in events:
        try:
            out.append(_normalize_event(ev))
        except Exception as e:
            # Keep going; one bad event shouldn't kill the file
            sys.stderr.write(f"[warn] normalize failed for event id={ev.get('id')}: {e}\n")
    return out


# --------------- CLI / main ---------------

def _iter_dates(start: datetime.date, end: datetime.date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _parse_args(argv: List[str]) -> Dict[str, Any]:
    """
    Very light arg parsing for optional --start YYYY-MM-DD --end YYYY-MM-DD
    """
    start: Optional[str] = None
    end: Optional[str] = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--start" and i + 1 < len(argv):
            start = argv[i + 1]
            i += 2
            continue
        if tok == "--end" and i + 1 < len(argv):
            end = argv[i + 1]
            i += 2
            continue
        i += 1

    if start and end:
        try:
            s = datetime.strptime(start, "%Y-%m-%d").date()
            e = datetime.strptime(end, "%Y-%m-%d").date()
            if e < s:
                raise ValueError("end before start")
            return {"mode": "range", "start": s, "end": e}
        except Exception as e:
            raise SystemExit(f"Invalid date range: {start} .. {end} :: {e}")
    else:
        return {"mode": "auto", "dates": _dates_to_fetch()}


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    args = _parse_args(argv)

    all_games: List[Dict[str, Any]] = []

    if args["mode"] == "range":
        for d in _iter_dates(args["start"], args["end"]):
            all_games.extend(_collect_games_for_date(d.strftime("%Y-%m-%d")))
        primary_date = args["start"].strftime("%Y-%m-%d")
    else:
        want_dates = args["dates"]
        for d in want_dates:
            all_games.extend(_collect_games_for_date(d))
        primary_date = want_dates[0]

    # De-dup by gamePk (string ESPN id). Keep last write (later day wins).
    uniq: Dict[str, Dict[str, Any]] = {}
    for g in all_games:
        gid = str(g.get("gamePk"))
        if gid:
            uniq[gid] = g

    games_out = list(uniq.values())

    out = {
        "generated_utc": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": ["espn nfl scoreboard"],
        "dates": [{"date": primary_date, "games": games_out}],
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"), sort_keys=False)

    print(f"Wrote {OUT_PATH} with {len(games_out)} games (date: {primary_date})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
