#!/usr/bin/env python3
# scripts/fetch_nba.py
#
# Build nba.json in the relay shape your flipboard expects (written to repo root).
# - Source: ESPN NBA scoreboard (yesterday + today merged)
# - Stdlib only
# - Robust state mapping (Preview/Live/Final)
# - Correct quarter labeling (1st/2nd/3rd/4th, OT for 5+)
# - Stable sorting (Live → Preview by start → Final by recency)
# - Adds generated_utc and a small _meta block for debugging

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore

# IMPORTANT: Pages is configured for main / (root), so write to the repo root.
OUT = Path("nba.json")

# ESPN NBA scoreboard
# Today: https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard
# Specific date: append ?dates=YYYYMMDD
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

# Network behavior
HTTP_TIMEOUT = 12.0
USER_AGENT = "NewsRiverRelay/1.0 (NBA) +https://mypybite.github.io/newsriver/"

# Minor abbreviation fixes for logos / consistency
ABBR_FIX = {
    # Warriors sometimes appear as "GS"
    "GS": "GSW",
    # Occasional variants seen across feeds; harmless if not present
    "NO": "NOP",
    "SA": "SAS",
    "NY": "NYK",
    "PHO": "PHX",
    "UTH": "UTA",
}

# ---------- Helpers ----------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _et_today_and_yesterday() -> tuple[datetime, datetime]:
    """
    Return (today_ET, yesterday_ET) as dates anchored to America/Toronto.
    Only the date part is used to build ESPN ?dates=YYYYMMDD.
    """
    if ZoneInfo is None:
        # Fallback: approximate ET by UTC-4 (OK for current needs).
        now = _now_utc() - timedelta(hours=4)
    else:
        now = datetime.now(ZoneInfo("America/Toronto"))
    today = now.date()
    yday = (now - timedelta(days=1)).date()
    # Return as UTC-aware midnights for convenience (only strftime is used)
    return (
        datetime(today.year, today.month, today.day, tzinfo=timezone.utc),
        datetime(yday.year, yday.month, yday.day, tzinfo=timezone.utc),
    )

def _parse_iso_or_none(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def ord_period(n: Optional[int]) -> Optional[str]:
    """
    Quarter label:
      1 -> 1st, 2 -> 2nd, 3 -> 3rd, 4 -> 4th, 5+ -> OT
    """
    if not n:
        return None
    if n == 1:
        return "1st"
    if n == 2:
        return "2nd"
    if n == 3:
        return "3rd"
    if n == 4:
        return "4th"
    return "OT"

def map_state_from_types(status_obj: Dict[str, Any], comp_status_obj: Dict[str, Any]) -> str:
    """
    Prefer ESPN status.type.state when available: 'pre' | 'in' | 'post'
    Fallback to description/name heuristics.
    """
    def _state_from(d: Dict[str, Any]) -> Optional[str]:
        t = (d or {}).get("type") or {}
        st = (t.get("state") or "").strip().lower()
        nm = (t.get("name") or "").strip().lower()
        desc = (t.get("description") or "").strip().lower()

        if st in ("pre", "in", "post"):
            return {"pre": "Preview", "in": "Live", "post": "Final"}[st]

        blob = " ".join((st, nm, desc))
        if any(k in blob for k in ("final", "post", "complete", "ended", "status_final", "postponed", "canceled", "cancelled")):
            return "Final"
        if any(k in blob for k in ("in progress", "live", "status_in_progress", "playing", "halftime", "qtr", "ot", "overtime")):
            return "Live"
        if any(k in blob for k in ("pre", "scheduled", "pre-game", "upcoming", "tbd", "future", "pregame")):
            return "Preview"
        return None

    m = _state_from(status_obj) or _state_from(comp_status_obj)
    return m or "Preview"

def abbr(team_obj: Dict[str, Any]) -> str:
    raw = ((team_obj or {}).get("abbreviation") or "TEAM").upper()
    return ABBR_FIX.get(raw, raw)

def to_int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except Exception:
        return None

def fetch_json(url: str, timeout: float = HTTP_TIMEOUT) -> Optional[dict]:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Cache-Control": "no-cache",
                "User-Agent": USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                print(f"[nba] ESPN fetch failed: HTTP {resp.status} for {url}", file=sys.stderr)
                return None
            return json.load(resp)
    except Exception as e:
        print(f"[nba] ESPN fetch error for {url}: {e}", file=sys.stderr)
        return None

def espn_url_for_date(dt: Optional[datetime]) -> str:
    if not dt:
        return ESPN_BASE
    return f"{ESPN_BASE}?dates={dt.strftime('%Y%m%d')}"

def to_relay_from_espn(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Convert a single ESPN payload to the relay game's shape the FE expects.
    Relay fields used by FE:
      - gamePk, gameDate
      - status.detailedState / abstractGameState (Live/Final/Preview)
      - linescore.currentPeriodOrdinal / currentQuarter
      - teams.away/home.team.abbreviation
      - teams.away/home.score
    """
    events = (data or {}).get("events") or []
    out: List[Dict[str, Any]] = []

    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        status = (ev.get("status") or {})
        comp_status = comp.get("status") or {}

        abs_state = map_state_from_types(status, comp_status)
        det_state = abs_state

        start_iso = ev.get("date")
        start_dt = _parse_iso_or_none(start_iso)

        game_id = ev.get("id") or comp.get("id") or ""

        # Period / quarter info (NBA)
        period_num = (
            status.get("period")
            or (comp_status.get("period") if isinstance(comp_status, dict) else None)
        )
        try:
            period_num = int(period_num) if period_num is not None else None
        except Exception:
            period_num = None

        current_ord = None
        if abs_state == "Live":
            current_ord = ord_period(period_num)
        elif abs_state == "Final":
            current_ord = "Final"

        # Teams (home/away)
        competitors = comp.get("competitors") or []
        c_away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        c_home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        if c_away is None and len(competitors) >= 2:
            c_away = competitors[1]
        if c_home is None and len(competitors) >= 1:
            c_home = competitors[0]

        away_team = (c_away or {}).get("team") or {}
        home_team = (c_home or {}).get("team") or {}

        game = {
            "gamePk": game_id,
            "gameDate": start_iso,
            "status": {
                "detailedState": det_state,
                "abstractGameState": abs_state,
            },
            "linescore": {
                "currentPeriodOrdinal": current_ord,
                "currentQuarter": current_ord,
            },
            "teams": {
                "away": {
                    "team": {"abbreviation": abbr(away_team)},
                    "score": to_int((c_away or {}).get("score")),
                },
                "home": {
                    "team": {"abbreviation": abbr(home_team)},
                    "score": to_int((c_home or {}).get("score")),
                },
            },
        }

        # Attach parsed start_dt internally (not written) to help sorting later
        game["_start_dt"] = start_dt.isoformat() if start_dt else None
        out.append(game)

    return out

def write_fallback():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump({"dates": [{"games": []}]}, f, indent=2)
    print(f"[nba] wrote fallback {OUT}", file=sys.stderr)

def _state_rank(s: str) -> int:
    # Live → Preview → Final
    s = (s or "").lower()
    if s == "live":
        return 0
    if s == "preview":
        return 1
    return 2  # Final/Unknown

def _start_dt_of(game: Dict[str, Any]) -> datetime:
    s = game.get("_start_dt")
    if s:
        try:
            return datetime.fromisoformat(s)
        except Exception:
            pass
    return _parse_iso_or_none(game.get("gameDate")) or _now_utc()

def main():
    # Build URLs for yesterday+today in America/Toronto
    today_et, yday_et = _et_today_and_yesterday()
    yday_url = espn_url_for_date(yday_et)
    today_url = espn_url_for_date(today_et)

    games_all: List[Dict[str, Any]] = []

    # Yesterday
    yday_data = fetch_json(yday_url)
    if yday_data:
        part = to_relay_from_espn(yday_data)
        print(f"[nba] {yday_url} events={len(part)}", file=sys.stderr)
        games_all.extend(part)

    # Today
    today_data = fetch_json(today_url)
    if today_data:
        part = to_relay_from_espn(today_data)
        print(f"[nba] {today_url} events={len(part)}", file=sys.stderr)
        games_all.extend(part)

    # If both empty, try plain (no dates) once
    if not games_all:
        plain_data = fetch_json(ESPN_BASE)
        if plain_data:
            part = to_relay_from_espn(plain_data)
            print(f"[nba] {ESPN_BASE} events={len(part)}", file=sys.stderr)
            games_all.extend(part)

    if not games_all:
        write_fallback()
        return

    # Deduplicate by gamePk
    seen = set()
    unique: List[Dict[str, Any]] = []
    for g in games_all:
        gid = g.get("gamePk")
        if gid in seen:
            continue
        seen.add(gid)
        unique.append(g)

    # Sort: Live → Preview (by start ascending) → Final (by start descending)
    def _sort_key(g: Dict[str, Any]):
        state = (g.get("status") or {}).get("abstractGameState", "")
        rank = _state_rank(state)
        start_dt = _start_dt_of(g)
        if state == "Final":
            # newer Finals first
            return (rank, -start_dt.timestamp())
        # earlier games first for Live/Preview
        return (rank, start_dt.timestamp())

    unique.sort(key=_sort_key)

    relay = {
        "generated_utc": _now_utc().isoformat().replace("+00:00", "Z"),
        "dates": [{"games": [
            {k: v for k, v in g.items() if k != "_start_dt"} for g in unique
        ]}],
        "_meta": {
            "source": "espn",
            "urls": [yday_url, today_url],
            "games_count": len(unique),
            "http_timeout_sec": HTTP_TIMEOUT,
            "version": "nba-relay-1.0",
        }
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump(relay, f, indent=2)
    print(f"[nba] wrote {OUT} games={len(unique)}", file=sys.stderr)

if __name__ == "__main__":
    main()
