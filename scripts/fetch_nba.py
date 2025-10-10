#!/usr/bin/env python3
# scripts/fetch_nba.py
# Builds newsriver/nba.json in the same relay shape your flipboard expects.
# Uses stdlib only.

import json
import sys
import urllib.request
from pathlib import Path

SRC = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
OUT = Path("newsriver/nba.json")


def map_state(status_desc: str) -> str:
    """
    Map ESPN status text to our card states: Preview | Live | Final
    ESPN examples: 'Scheduled', 'Pre-Game', 'In Progress', 'Halftime', 'Final', 'Postponed'
    """
    s = (status_desc or "").lower()
    if "final" in s or "status_final" in s:
        return "Final"
    if (
        "in progress" in s
        or "live" in s
        or "halftime" in s
        or "overtime" in s
        or "status_in_progress" in s
    ):
        return "Live"
    # Treat postponed / delayed / pre-game as Preview so the card shows the start time
    return "Preview"


def ord_period(n: int | None) -> str | None:
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
    # 5+ => OT for NBA
    return "OT"


def abbr(team_obj: dict) -> str:
    return ((team_obj or {}).get("abbreviation") or "TEAM").upper()


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
        status = ev.get("status") or {}
        status_type = status.get("type") or {}
        status_desc = status_type.get("description") or status_type.get("name") or ""
        abs_state = map_state(status_desc)
        det_state = abs_state
        start_iso = ev.get("date")
        game_id = ev.get("id") or comp.get("id")

        # Period / quarter (ESPN may put it in event.status.period OR competitions[].status.period)
        period_num = status.get("period") or (comp.get("status") or {}).get("period")
        current_q = None
        if abs_state == "Live":
            current_q = ord_period(period_num)
        elif abs_state == "Final":
            current_q = "Final"

        # Teams (home/away)
        competitors = comp.get("competitors") or []
        c_away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        c_home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        # Fallback ordering just in case
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
                # Generic cards read either of these:
                "currentPeriodOrdinal": current_q,
                "currentQuarter": current_q,
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
        req = urllib.request.Request(
            SRC,
            headers={
                "Cache-Control": "no-cache",
                # Add a UA to be polite and avoid sporadic 403s
                "User-Agent": "newsriver-nba-fetch/1.0 (+https://github.com/your-org/your-repo)"
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            if resp.status != 200:
                print(f"NBA fetch failed: HTTP {resp.status}", file=sys.stderr)
                write_fallback()
                return
            data = json.load(resp)
    except Exception as e:
        print(f"NBA fetch error: {e}", file=sys.stderr)
        write_fallback()
        return

    relay = to_relay(data)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump(relay, f, indent=2)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
