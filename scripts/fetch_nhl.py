#!/usr/bin/env python3
import json, sys, os, datetime, urllib.request, urllib.error

# Output file at repo root (the GitHub Pages site points at this)
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "nhl.json")

API_BASE = "https://statsapi.web.nhl.com/api/v1/schedule"

# How many days to include (today + N more)
RANGE_DAYS = int(os.environ.get("NHL_RANGE_DAYS", "7"))  # 7 = today + 6

def fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent":"nhl-schedule-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)

def coerce_game(game):
    """Map NHL API game to your schema (gamePk, gameDate, status, teams, linescore)."""
    # Base fields
    gamePk   = game.get("gamePk")
    gameDate = game.get("gameDate")  # UTC ISO8601
    status   = game.get("status", {})
    detailed = status.get("detailedState", "")
    abstract = status.get("abstractGameState", "")

    # Teams (abbreviation/triCode are available on team objects)
    teams = game.get("teams", {})
    awayT = (teams.get("away", {}) or {})
    homeT = (teams.get("home", {}) or {})

    def team_block(side):
        t = (teams.get(side, {}) or {})
        team = (t.get("team", {}) or {})
        # NHL Stats API exposes team.abbreviation; triCode is common in other NHL feeds
        return {
            "team": {
                "abbreviation": team.get("abbreviation") or "",
                "triCode":     team.get("abbreviation") or ""
            },
            "score": t.get("score", 0)
        }

    # Linescore-like hints (period/time are available only on live games via linescore hydrate,
    # but schedule gives minimal info. We keep placeholders here.)
    linescore = {
        "currentPeriod": 0,
        "currentPeriodOrdinal": "",
        "currentPeriodTimeRemaining": ""
    }

    return {
        "gamePk": gamePk,
        "gameDate": gameDate,
        "status": {
            "abstractGameState": abstract,
            "detailedState": detailed
        },
        "teams": {
            "away": team_block("away"),
            "home": team_block("home")
        },
        "linescore": linescore
    }

def main():
    today = datetime.date.today()
    end   = today + datetime.timedelta(days=max(1, RANGE_DAYS - 1))

    params = f"?startDate={today.isoformat()}&endDate={end.isoformat()}"
    url    = API_BASE + params

    try:
        data = fetch_json(url)
    except urllib.error.URLError as e:
        print(f"ERROR: Failed to fetch NHL schedule: {e}", file=sys.stderr)
        sys.exit(1)

    out_dates = []
    for day in data.get("dates", []):
        date_str = day.get("date")
        games_in = day.get("games", [])
        games_out = [coerce_game(g) for g in games_in]
        # keep only real games (skip empty days)
        if games_out:
            out_dates.append({
                "date": date_str,
                "games": games_out
            })

    # If API returns nothing, keep old file to avoid blanking the site
    if not out_dates:
        print("WARN: NHL API returned no games for the window; leaving existing nhl.json unchanged.")
        return

    payload = { "dates": out_dates }

    # Write pretty + stable
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",",": "))

    print(f"Wrote {OUT_PATH} with {sum(len(d['games']) for d in out_dates)} games across {len(out_dates)} day(s).")

if __name__ == "__main__":
    main()
