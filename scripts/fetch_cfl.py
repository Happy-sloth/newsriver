#!/usr/bin/env python3
# scripts/fetch_cfl.py
# Builds newsriver/cfl.json in the same relay shape your flipboard expects.
# Stdlib only.

import json
import os
import sys
import urllib.request
from pathlib import Path

# Primary: ESPN CFL scoreboard
SRC_ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/cfl/scoreboard"
OUT = Path("newsriver/cfl.json")

def map_state(status_obj: dict) -> str:
    """
    Robust state mapping:
      1) If ESPN marks the event completed -> Final
      2) Else use name/description heuristics
    """
    t = (status_obj or {}).get("type") or {}
    if t.get("completed") is True:
        return "Final"

    s = (t.get("state") or t.get("name") or t.get("description") or "").lower()
    # Normalize common ESPN variants safely
    if ("final" in s) or ("post" in s) or ("complete" in s) or ("status_final" in s):
        return "Final"
    if ("in progress" in s) or ("status_in_progress" in s) or ("live" in s) or ("playing" in s):
        return "Live"
    if ("pre" in s) or ("scheduled" in s) or ("pre_game" in s) or ("future" in s):
        return "Preview"
    return "Preview"

def ord_period(n: int | None) -> str | None:
    if not n: return None
    return {1:"1st", 2:"2nd", 3:"3rd", 4:"4th"}.get(int(n), "OT")

def abbr(team_obj: dict) -> str:
    t = team_obj or {}
    return (t.get("abbreviation") or t.get("shortDisplayName") or t.get("displayName") or "TEAM").upper()[:4]

def to_int(v):
    try:
        return int(v) if v is not None else None
    except Exception:
        return None

def safe_score(c: dict) -> int | None:
    """
    ESPN sometimes omits/empties score strings on stale 'in progress' flags.
    Convert '', None -> None. Otherwise int.
    """
    raw = (c or {}).get("score")
    if raw in ("", None): return None
    return to_int(raw)

def to_relay(data: dict) -> dict:
    events = data.get("events") or []
    games = []

    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        status = ev.get("status") or {}
        status_type = status.get("type") or comp.get("status", {}).get("type") or {}
        abs_state = map_state({"type": status_type})
        det_state = abs_state

        start_iso = ev.get("date")
        game_id = ev.get("id") or comp.get("id")

        # Period / quarter
        period_num = status.get("period") or comp.get("status", {}).get("period")
        current_ord = None
        if abs_state == "Live":
            current_ord = ord_period(period_num)
        elif abs_state == "Final":
            current_ord = "Final"

        # Teams
        competitors = comp.get("competitors") or []
        c_away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        c_home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        if c_away is None and len(competitors) >= 2: c_away = competitors[1]
        if c_home is None and len(competitors) >= 1: c_home = competitors[0]

        away_team = (c_away or {}).get("team") or {}
        home_team = (c_home or {}).get("team") or {}

        # Scores (guard against ESPN stale “in progress” zeros)
        away_score = safe_score(c_away)
        home_score = safe_score(c_home)

        # If ESPN says Live but both scores are missing/zero and the type.completed is True,
        # force Final to avoid the stuck "0-0 Live" bug.
        completed = bool((status_type or {}).get("completed"))
        if abs_state == "Live" and completed and (away_score in (None, 0)) and (home_score in (None, 0)):
            abs_state = det_state = "Final"
            current_ord = "Final"

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
                    "score": away_score,
                },
                "home": {
                    "team": {"abbreviation": abbr(home_team)},
                    "score": home_score,
                },
            },
        }
        games.append(game)

    return {"dates": [{"games": games}]}

def write_fallback():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump({"dates": [{"games": []}]}, f, indent=2)
    print(f"Wrote fallback {OUT}", file=sys.stderr)

def fetch_json(url: str, timeout: int = 12) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                print(f"Fetch failed: HTTP {resp.status} for {url}", file=sys.stderr)
                return None
            return json.load(resp)
    except Exception as e:
        print(f"Fetch error for {url}: {e}", file=sys.stderr)
        return None

def main():
    # (Optional) If you later add CFL official API, you can branch here via env var.
    data = fetch_json(SRC_ESPN)
    if not data:
        write_fallback()
        return

    relay = to_relay(data)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump(relay, f, indent=2)
    print(f"Wrote {OUT}")

if __name__ == "__main__":
    main()
