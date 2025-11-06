#!/usr/bin/env python3
# scripts/fetch_cfl.py
# Build newsriver/cfl.json in the relay shape your flipboard expects.
# - Source: ESPN CFL scoreboard (yesterday + today merged)
# - Stdlib only
# - Robust state mapping (Preview/Live/Final)
# - Strict ET time window filter to prevent stale (yday 00:00 ET → tomorrow 06:00 ET)
# - Stable sorting (Live → Preview by start → Final by recency)
# - Optional server-side final linger trim via env CFL_RECENT_FINAL_MAX_HOURS (default off)
# - Adds generated_utc and a small _meta block for debugging

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

OUT = Path("newsriver/cfl.json")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/cfl/scoreboard"

HTTP_TIMEOUT = float(os.getenv("CFL_HTTP_TIMEOUT_SEC", "7.5"))
USER_AGENT = os.getenv("CFL_HTTP_UA", "NewsRiverRelay/1.1 (CFL) +https://mypybite.github.io/newsriver/")

# Optional: limit “Final” items by age (hours). 0 = disabled.
RECENT_FINAL_MAX_HOURS = int(os.getenv("CFL_RECENT_FINAL_MAX_HOURS", "0"))

# ---------- Time helpers (America/Toronto anchor) ----------

try:
    from zoneinfo import ZoneInfo  # py3.9+
    ET = ZoneInfo("America/Toronto")
except Exception:
    ET = None

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _now_et() -> datetime:
    if ET is not None:
        return datetime.now(ET)
    # Fallback approximation; OK for our window purposes
    return datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(hours=4)

def _et_window() -> tuple[datetime, datetime]:
    """
    Return (start_utc, end_utc):
      start = yesterday 00:00 ET
      end   = tomorrow 06:00 ET  (lets late night games through)
    Both returned as UTC datetimes for easy comparison.
    """
    now_et = _now_et()
    yday = (now_et - timedelta(days=1)).date()
    tmrw = (now_et + timedelta(days=1)).date()

    start_et = datetime(yday.year, yday.month, yday.day, 0, 0, tzinfo=ET or timezone.utc)
    end_et   = datetime(tmrw.year, tmrw.month, tmrw.day, 6, 0, tzinfo=ET or timezone.utc)

    # convert to UTC
    if start_et.tzinfo is not None:
        start_utc = start_et.astimezone(timezone.utc)
        end_utc = end_et.astimezone(timezone.utc)
    else:
        start_utc = start_et
        end_utc = end_et
    return start_utc, end_utc

# ---------- Parsing / mapping ----------

def _parse_iso_or_none(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def _hours_since(dt: Optional[datetime], ref: Optional[datetime] = None) -> float:
    if dt is None:
        return 1e9
    base = ref or _now_utc()
    return max(0.0, (base - dt).total_seconds() / 3600.0)

def map_state(status_obj: Dict[str, Any], comp_status_obj: Dict[str, Any]) -> str:
    """
    Classify into Preview / Live / Final using ESPN status fields:
    - Prefer type.state: 'pre' | 'in' | 'post'
    - Fallback to type.name/description text
    - Fallback to competitions[0].status.type.* if present
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
        if any(k in blob for k in ("pre", "scheduled", "upcoming", "tbd", "future", "pregame")):
            return "Preview"
        return None

    m = _state_from(status_obj) or _state_from(comp_status_obj)
    return m or "Preview"

def ord_period(n: Optional[int]) -> Optional[str]:
    if not n:
        return None
    if n == 1: return "1st"
    if n == 2: return "2nd"
    if n == 3: return "3rd"
    if n == 4: return "4th"
    return "OT"

def abbr(team_obj: Dict[str, Any]) -> str:
    t = team_obj or {}
    return (t.get("abbreviation") or t.get("shortDisplayName") or t.get("displayName") or "TEAM").upper()[:4]

def to_int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except Exception:
        return None

# ---------- Fetch / normalize ----------

def fetch_json(url: str, timeout: float = HTTP_TIMEOUT) -> Optional[dict]:
    try:
        req = urllib.request.Request(
            url,
            headers={"Cache-Control": "no-cache", "User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                print(f"[cfl] ESPN fetch failed: HTTP {resp.status} for {url}", file=sys.stderr)
                return None
            return json.load(resp)
    except Exception as e:
        print(f"[cfl] ESPN fetch error for {url}: {e}", file=sys.stderr)
        return None

def espn_url_for_date(dt: Optional[datetime]) -> str:
    if not dt:
        return ESPN_BASE
    return f"{ESPN_BASE}?dates={dt.strftime('%Y%m%d')}"

def to_relay_from_espn(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    events = (data or {}).get("events") or []
    out: List[Dict[str, Any]] = []
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        status = (ev.get("status") or {})
        comp_status = comp.get("status") or {}

        abs_state = map_state(status, comp_status)
        det_state = abs_state

        start_iso = ev.get("date")
        start_dt = _parse_iso_or_none(start_iso)

        game_id = ev.get("id") or comp.get("id") or ""

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
                "away": {"team": {"abbreviation": abbr(away_team)}, "score": to_int((c_away or {}).get("score"))},
                "home": {"team": {"abbreviation": abbr(home_team)}, "score": to_int((c_home or {}).get("score"))},
            },
        }

        game["_start_dt"] = start_dt.isoformat() if start_dt else None
        out.append(game)
    return out

def write_empty():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump({"dates": [{"games": []}]}, f, indent=2)
    print(f"[cfl] wrote EMPTY {OUT}", file=sys.stderr)

def _state_rank(s: str) -> int:
    s = (s or "").lower()
    if s == "live": return 0
    if s == "preview": return 1
    return 2

def _start_dt_of(game: Dict[str, Any]) -> datetime:
    s = game.get("_start_dt")
    if s:
        try:
            return datetime.fromisoformat(s)
        except Exception:
            pass
    return _parse_iso_or_none(game.get("gameDate")) or _now_utc()

# ---------- Main ----------

def main():
    now_utc = _now_utc()
    yday_utc, today_utc = now_utc - timedelta(days=1), now_utc
    yday_url = espn_url_for_date(yday_utc)
    today_url = espn_url_for_date(today_utc)

    games_all: List[Dict[str, Any]] = []

    yday_data = fetch_json(yday_url)
    if yday_data:
        games_all.extend(to_relay_from_espn(yday_data))

    today_data = fetch_json(today_url)
    if today_data:
        games_all.extend(to_relay_from_espn(today_data))

    if not games_all:
        plain_data = fetch_json(ESPN_BASE)
        if plain_data:
            games_all.extend(to_relay_from_espn(plain_data))

    # If still nothing: empty file and exit
    if not games_all:
        write_empty()
        return

    # Dedup
    seen = set()
    unique: List[Dict[str, Any]] = []
    for g in games_all:
        gid = g.get("gamePk")
        if gid in seen:
            continue
        seen.add(gid)
        unique.append(g)

    # Strict ET window filter to prevent stale entries
    win_start_utc, win_end_utc = _et_window()
    filtered: List[Dict[str, Any]] = []
    for g in unique:
        start_dt = _start_dt_of(g)
        if win_start_utc <= start_dt <= win_end_utc:
            filtered.append(g)

    # Optional: trim Finals older than N hours (server-side)
    if RECENT_FINAL_MAX_HOURS > 0:
        trimmed: List[Dict[str, Any]] = []
        for g in filtered:
            state = (g.get("status") or {}).get("abstractGameState", "")
            if state != "Final":
                trimmed.append(g); continue
            age_h = _hours_since(_start_dt_of(g), now_utc)
            if age_h <= RECENT_FINAL_MAX_HOURS:
                trimmed.append(g)
        filtered = trimmed

    # If window leaves nothing, write empty cleanly
    if not filtered:
        write_empty()
        return

    # Sort: Live → Preview (start asc) → Final (start desc)
    def _sort_key(g: Dict[str, Any]):
        state = (g.get("status") or {}).get("abstractGameState", "")
        rank = _state_rank(state)
        start_dt = _start_dt_of(g)
        if state == "Final":
            return (rank, -start_dt.timestamp())
    # earlier first for Live/Preview
        return (rank, start_dt.timestamp())

    filtered.sort(key=_sort_key)

    relay = {
        "generated_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "dates": [{"games": [{k: v for k, v in g.items() if k != "_start_dt"} for g in filtered]}],
        "_meta": {
            "source": "espn",
            "urls": [yday_url, today_url],
            "games_count": len(filtered),
            "http_timeout_sec": HTTP_TIMEOUT,
            "recent_final_max_hours": RECENT_FINAL_MAX_HOURS,
            "window_start_utc": win_start_utc.isoformat().replace("+00:00", "Z"),
            "window_end_utc": win_end_utc.isoformat().replace("+00:00", "Z"),
            "version": "cfl-relay-1.2",
        }
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump(relay, f, indent=2)
    print(f"[cfl] wrote {OUT} games={len(filtered)}")

if __name__ == "__main__":
    main()
