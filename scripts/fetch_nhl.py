#!/usr/bin/env python3
# Lightweight relay: fetch today's NHL schedule (with linescore + teams)
# and write it as nhl.json at repo root for the front-end widget.

import json, sys, datetime, urllib.request

def fetch(date_str: str):
    url = f"https://statsapi.web.nhl.com/api/v1/schedule?date={date_str}&expand=schedule.linescore,schedule.teams"
    req = urllib.request.Request(url, headers={"User-Agent": "mypybyte-nhl-relay"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))

def main():
    # Use UTC "today" so the job is timezone-agnostic
    today = datetime.datetime.utcnow().date().isoformat()

    try:
        data = fetch(today)
    except Exception as e:
        # Fail closed with an empty, valid structure so the site doesn’t break
        data = {"dates": []}

    # Write to repo root for GitHub Pages: https://www.mypybite.com/newsriver/nhl.json
    with open("nhl.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

if __name__ == "__main__":
    sys.exit(main())
