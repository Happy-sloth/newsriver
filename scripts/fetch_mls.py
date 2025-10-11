#!/usr/bin/env python3
# scripts/fetch_mls.py
# Builds newsriver/mls.json in the same relay shape your flipboard expects.
# Uses ESPN's MLS scoreboard (league path: soccer/usa.1).

import json
import sys
import urllib.request
from pathlib import Path

SRC = "https://site.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard"
OUT = Path("newsriver/mls.json")


def map_state(status_desc: str) -> str:
    """
    Map ESPN status to our card states: Preview | Live | Final
    Soccer examples include: 'Scheduled', 'Pre-Game', 'First Half', 'Second Half',
    'Halftime', 'Extra Time', 'Penalties', 'Final', 'Postponed', 'Delayed'.
    """
    s = (status_desc or "").lower()
    if "final" in s or "status_final" in s:
        return "Final"
    if (
        "first half" in s
        or "second half" in s
        or "halftime" in s
        or "extra time" in s
        or "penalties" in s
        or "in progress" in s
        or "live" in s
        or "status_in_progress" in s
    ):
        return "Live"
    return "Preview"


def abbr(team_obj: dict) -> str:
    # MLS abbreviations vary but ESPN provides a short name + abbreviation field.
    # We prefer official abbreviation; fallback to shortDisplayName initials.
    a = ((team_obj or {}).get("abbreviation") or "").strip().upper()
    if a:
        return a
    short = ((team_obj or {}).get("shortDisplayName") or "").strip()
    if not short:
        return "TEAM"
    # crude initials fallback
    return "".join(w[0] for w in short.split() if w).upper()[:4] or "TEAM"


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

        # Soccer period label: we won't show "1st/2nd" in your generic card,
        # but we keep a friendly marker that some cards may read as currentPeriodOrdinal.
        period_label = None
        sdl = (status_desc or "").lower()
        if abs_state == "Live":
            if "first half" in sdl:
                period_label = "1st"
            elif "second half" in sdl:
                period_label = "2nd"
            elif "extra time" in sdl:
                period_label = "ET"
            elif "penalties" in sdl:
                period_label = "PENS"
            else:
                period_label = "LIVE"
        elif abs_state == "Final":
            period_label = "Final"

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
                # Generic cards can read either of these keys; we provide one label.
                "currentPeriodOrdinal": period_label,
                "currentQuarter": period_label,
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
                "User-Agent": "newsriver-mls-fetch/1.0 (+https://github.com/your-org/your-repo)",
            },
        )
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
