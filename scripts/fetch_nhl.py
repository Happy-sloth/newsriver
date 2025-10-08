#!/usr/bin/env python3
# Lightweight relay: fetch NHL schedule (yesterday, today, tomorrow)
# and write it as nhl.json at repo root for the front-end widget.

import json, sys, datetime, urllib.request

def fetch(date_str: str):
    url = f"https://statsapi.web.nhl.com/api/v1/schedule?date={date_str}&expand=schedule.linescore,schedule.teams"
    req = urllib.request.Request(url, headers={"User-Agent": "mypybyte-nhl-relay"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

def main():
    base = datetime.datetime.utcnow().date()
    # Fetch yesterday, today, and tomorrow — ensures coverage across timezones
    dates = [
        (base - datetime.timedelta(days=1)).isoformat(),
        base.isoformat(),
        (base + datetime.timedelta(days=1)).isoformat()
    ]

    all_games = {"dates": []}

    for d in dates:
        try:
            data = fetch(d)
            if data.get("dates"):
                all_games["dates"].extend(data["dates"])
        except Exception as e:
            print(f"⚠️ NHL fetch failed for {d}: {e}", file=sys.stderr)
            continue

    # If still empty, output a valid empty structure so the front end doesn’t crash
    if not all_games["dates"]:
        all_games = {"dates": []}

    # Write to repo root for GitHub Pages: https://www.mypybite.com/newsriver/nhl.json
    with open("nhl.json", "w", encoding="utf-8") as f:
        json.dump(all_games, f, ensure_ascii=False, separators=(",", ":"))

if __name__ == "__main__":
    sys.exit(main())
