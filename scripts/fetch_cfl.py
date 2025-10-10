#!/usr/bin/env python3
# scripts/fetch_cfl.py
# Builds newsriver/cfl.json in the same relay shape your flipboard expects.
# Uses stdlib only (urllib, json, pathlib) so you don't need extra deps.

import json
import sys
import urllib.request
from pathlib import Path

SRC = "https://site.api.espn.com/apis/site/v2/sports/football/cfl/scoreboard"
OUT = Path("newsriver/cfl.json")


def map_state(status_desc: str) -> str:
    """Map ESPN's status to Preview | Live | Final."""
    s = (status_desc or "").lower()
    if "final" in s:
        return "Final"
    if "in progress" in s or "live" in s or "status_in_progress" in s:
        return "Live"
    return "Preview"


def ord_quarter(n: int | None) -> str | None:
    """Return 1st/2nd/3rd/4th/OT for live games; None when unknown."""
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


def team_abbr(team_obj: dict) -> str:
    """Normalize team abbreviation to the tri-codes your UI expects."""
    abbr = (team_obj or {}).get("abbreviation") or ""
    return abbr.upper() or "TEAM"


def to_relay(data: dict) -> dict:
    events = data.get("events") or []
    games = []
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        status = (ev.get("status") or {}).get("type") or {}
        status_desc = status.get("description") or status.get("name") or ""
        abs_state = map_state(status_desc)
        det_state = abs_state

        start_iso = ev.get("date")
        game_id = ev.get("id") or comp.get("id")

        # Period/quarter info
        period_num = ev.get("status", {}).get("period") or comp.get("status", {}).get("period")
        current_q = None
        if abs_state == "Live":
            current_q = ord_quarter(period_num)
        elif abs_state == "Final":
            current_q = "Final"

        # Competitors (ESPN flags with homeAway: "home"/"away")
        competitors = comp.get("competitors") or []
        c_away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        c_home = next((c for c in competitors if c.get("homeAway") == "home"), None)

        # Fallback if flags are missing (order may be [home, away] or [away, home])
        if c_away is None and len(competitors) >= 2:
            c_away = competitors[1]
        if c_home is None and len(competitors) >= 1:
            c_home = competitors[0]

        away_team = (c_away or {}).get("team") or {}
        home_team = (c_home or {}).get("team") or {}

        def to_score(v):
            try:
                return int(v) if v is not None else None
            except Exception:
                return None

        game = {
            "gamePk": game_id,
            "gameDate": start_iso,
            "status": {
                "detailedState": det_state,
                "abstractGameState": abs_state,
            },
            "linescore": {
                "currentQuarter": current_q,
            },
            "teams": {
                "away": {
                    "team": {"abbreviation": team_abbr(away_team)},
                    "score": to_score((c_away or {}).get("score")),
                },
                "home": {
                    "team": {"abbreviation": team_abbr(home_team)},
                    "score": to_score((c_home or {}).get("score")),
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
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                print(f"CFL fetch failed: HTTP {resp.status}", file=sys.stderr)
                write_fallback()
                return
            data = json.load(resp)
    except Exception as e:
        print(f"CFL fetch error: {e}", file=sys.stderr)
        write_fallback()
        return

    relay = to_relay(data)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump(relay, f, indent=2)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
