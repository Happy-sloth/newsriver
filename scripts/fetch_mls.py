#!/usr/bin/env python3
# scripts/fetch_mls.py
# Builds newsriver/mls.json in the same relay shape your flipboard expects.
# Source: ESPN soccer MLS scoreboard (usa.1)

import json
import sys
import urllib.request
from pathlib import Path

SRC = "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard"
OUT = Path("newsriver/mls.json")


def map_state(status_type: dict) -> str:
    # ESPN soccer exposes several names/descriptions; normalize to Preview/Live/Final
    name = (status_type.get("name") or status_type.get("description") or "").lower()
    if "final" in name or "post" in name or name == "status_final":
        return "Final"
    if "in progress" in name or "live" in name or name == "status_in_progress":
        return "Live"
    return "Preview"


def ord_period(n: int | None) -> str | None:
    if not n:
        return None
    if n == 1:
        return "1st"
    if n == 2:
        return "2nd"
    # Extra time / shootout appear as >2
    return "OT"


def abbr(team_obj: dict) -> str:
    # Prefer true abbreviation; fall back to shortDisplayName with spaces stripped
    a = (team_obj or {}).get("abbreviation")
    if a:
        return a.upper()
    sdn = (team_obj or {}).get("shortDisplayName") or (team_obj or {}).get("name") or "TEAM"
    return "".join(sdn.split()).upper()[:6]


def to_int(v):
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


def to_relay(data: dict) -> dict:
    events = data.get("events") or []
    games = []

    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        status = (ev.get("status") or {})
        status_type = status.get("type") or {}
        abs_state = map_state(status_type)
        det_state = abs_state
        start_iso = ev.get("date")
        game_id = ev.get("id") or comp.get("id")

        # Period / half (optional, for Live)
        period_num = status.get("period") or (comp.get("status") or {}).get("period")
        current_ord = ord_period(period_num) if abs_state == "Live" else ("Final" if abs_state == "Final" else None)

        # Teams
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
                "currentPeriodOrdinal": current_ord,  # generic cards read this
                "currentQuarter": current_ord,        # kept for shared code path
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
        games.append(game)

    return {"dates": [{"games": games}]}


def write_fallback():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump({"dates": [{"games": []}]}, f, indent=2)
    print(f"Wrote fallback {OUT}", file=sys.stderr)


def main():
    try:
        req = urllib.request.Request(SRC, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            if resp.status != 200:
                print(f"MLS fetch failed: HTTP {resp.status}", file=sys.stderr)
                write_fallback()
                return
            data = json.load(resp)
    except Exception as e:
        print(f"MLS fetch error: {e}", file=sys.stderr)
        write_fallback()
        return

    relay = to_relay(data)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump(relay, f, indent=2)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
