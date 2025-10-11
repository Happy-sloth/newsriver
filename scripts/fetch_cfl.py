#!/usr/bin/env python3
# scripts/fetch_cfl.py
# Builds newsriver/cfl.json in the same relay shape your flipboard expects.
# Uses stdlib only. Sources ESPN; merges yesterday + today so finals can linger.

import json
import sys
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta, timezone

OUT = Path("newsriver/cfl.json")

# ESPN CFL scoreboard
# Today: https://site.api.espn.com/apis/site/v2/sports/football/cfl/scoreboard
# Specific date: append ?dates=YYYYMMDD
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/cfl/scoreboard"


def map_state(status_desc: str) -> str:
    s = (status_desc or "").lower()
    if "final" in s or "post" in s or "complete" in s or "ended" in s:
        return "Final"
    if "in progress" in s or "live" in s or "status_in_progress" in s or "playing" in s:
        return "Live"
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
    if n >= 4:
        return "OT"
    return None


def abbr(team_obj: dict) -> str:
    t = team_obj or {}
    # CFL on ESPN uses abbreviations (e.g., TOR, WPG) reliably
    return (t.get("abbreviation") or t.get("shortDisplayName") or t.get("displayName") or "TEAM").upper()[:4]


def to_int(v):
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


def fetch_json(url: str, timeout: float = 12.0) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                print(f"ESPN fetch failed: HTTP {resp.status} for {url}", file=sys.stderr)
                return None
            return json.load(resp)
    except Exception as e:
        print(f"ESPN fetch error for {url}: {e}", file=sys.stderr)
        return None


def espn_url_for_date(dt: datetime | None) -> str:
    if not dt:
        return ESPN_BASE
    return f"{ESPN_BASE}?dates={dt.strftime('%Y%m%d')}"


def to_relay_from_espn(data: dict) -> list[dict]:
    """Return list of relay-format games from one ESPN payload."""
    events = (data or {}).get("events") or []
    out = []

    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        status = (ev.get("status") or {})
        status_type = status.get("type") or comp.get("status", {}).get("type") or {}
        status_desc = status_type.get("description") or status_type.get("name") or ""
        abs_state = map_state(status_desc)
        det_state = abs_state
        start_iso = ev.get("date")
        game_id = ev.get("id") or comp.get("id")

        # Period / quarter info
        period_num = status.get("period") or comp.get("status", {}).get("period")
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
                "currentPeriodOrdinal": current_ord,  # cards accept either key
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
        out.append(game)

    return out


def write_fallback():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump({"dates": [{"games": []}]}, f, indent=2)
    print(f"Wrote fallback {OUT}", file=sys.stderr)


def main():
    # We merge yesterday + today from ESPN so finals can linger client-side across midnight.
    now = datetime.now(timezone.utc)
    today_url = espn_url_for_date(now)
    yday_url  = espn_url_for_date(now - timedelta(days=1))

    games_all: list[dict] = []

    # Yesterday
    yday_data = fetch_json(yday_url)
    if yday_data:
        games_all.extend(to_relay_from_espn(yday_data))

    # Today
    today_data = fetch_json(today_url)
    if today_data:
        games_all.extend(to_relay_from_espn(today_data))

    # If both failed, try plain (no dates) once
    if not games_all:
        plain_data = fetch_json(ESPN_BASE)
        if plain_data:
            games_all.extend(to_relay_from_espn(plain_data))

    if games_all is None:
        write_fallback()
        return

    # Deduplicate by gamePk (in case overlap)
    seen = set()
    unique_games = []
    for g in games_all:
        gid = g.get("gamePk")
        if gid in seen:
            continue
        seen.add(gid)
        unique_games.append(g)

    relay = {"dates": [{"games": unique_games}]}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump(relay, f, indent=2)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
