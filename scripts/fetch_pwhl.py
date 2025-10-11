#!/usr/bin/env python3
# scripts/fetch_pwhl.py
# Builds newsriver/pwhl.json in the same relay shape your flipboard expects.
# Stdlib only. Starts with a safe fallback; swap SRC when you have a stable API.

import json
import sys
import urllib.request
from pathlib import Path

# TODO: update SRC to a real scoreboard endpoint when available.
SRC = None  # e.g. "https://example.com/pwhl/scoreboard.json"
OUT = Path("newsriver/pwhl.json")


def map_state(s: str | None) -> str:
    s = (s or "").lower()
    if "final" in s or "post" in s or "complete" in s:
        return "Final"
    if "in progress" in s or "live" in s or "playing" in s:
        return "Live"
    return "Preview"


def ord_period(n: int | None) -> str | None:
    if not n:
        return None
    if n == 1: return "1st"
    if n == 2: return "2nd"
    if n == 3: return "3rd"
    return "OT"


def to_int(v):
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


def to_relay_from_source(data: dict) -> dict:
    """
    Shape this function to your real source once chosen.
    For now, accept an empty/events-like structure to remain compatible.
    Expected output shape:
    {"dates":[{"games":[ {gamePk, gameDate, status{...}, linescore{...}, teams{away{team{abbreviation},score},home{...}} } ]}]}
    """
    events = data.get("events") or []
    games = []
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        status = ev.get("status") or {}
        status_type = status.get("type") or comp.get("status", {}).get("type") or {}
        desc = status_type.get("description") or status_type.get("name") or ""
        abs_state = map_state(desc)
        det_state = abs_state
        start_iso = ev.get("date")
        game_id = ev.get("id") or comp.get("id")

        period_num = status.get("period") or comp.get("status", {}).get("period")
        current_ord = "Final" if abs_state == "Final" else (ord_period(period_num) if abs_state == "Live" else None)

        competitors = comp.get("competitors") or []
        c_away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        c_home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        if c_away is None and len(competitors) >= 2: c_away = competitors[1]
        if c_home is None and len(competitors) >= 1: c_home = competitors[0]

        def abbr(team_obj: dict) -> str:
            t = team_obj or {}
            return (t.get("abbreviation") or t.get("shortDisplayName") or t.get("displayName") or "TEAM").upper()[:4]

        away_team = (c_away or {}).get("team") or {}
        home_team = (c_home or {}).get("team") or {}

        game = {
            "gamePk": game_id,
            "gameDate": start_iso,
            "status": {"detailedState": det_state, "abstractGameState": abs_state},
            "linescore": {"currentPeriodOrdinal": current_ord, "currentQuarter": current_ord},
            "teams": {
                "away": {"team": {"abbreviation": abbr(away_team)}, "score": to_int((c_away or {}).get("score"))},
                "home": {"team": {"abbreviation": abbr(home_team)}, "score": to_int((c_home or {}).get("score"))},
            },
        }
        games.append(game)

    return {"dates": [{"games": games}]}


def write_empty():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump({"dates": [{"games": []}]}, f, indent=2)
    print(f"Wrote fallback {OUT}", file=sys.stderr)


def main():
    # Until a real SRC exists, always write an empty but valid file
    if not SRC:
        write_empty()
        return

    try:
        req = urllib.request.Request(SRC, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            if resp.status != 200:
                print(f"PWHL fetch failed: HTTP {resp.status}", file=sys.stderr)
                write_empty()
                return
            data = json.load(resp)
    except Exception as e:
        print(f"PWHL fetch error: {e}", file=sys.stderr)
        write_empty()
        return

    relay = to_relay_from_source(data)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump(relay, f, indent=2)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
