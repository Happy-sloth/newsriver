#!/usr/bin/env python3
"""
Builds newsriver/nhl.json in the *NHL Stats API* shape the page expects:
{ "dates": [ { "date": "YYYY-MM-DD", "games": [ ... ] } ] }

Sources:
  1) NHL scoreboard/now (fastest for live)
  2) NHL scoreboard/{YYYY-MM-DD} (daily)
  3) NHL stats api schedule (fallback)
"""
import json, sys, datetime, urllib.request

OUT_PATH = "nhl.json"  # write into repo root (newsriver/nhl.json in Pages)
UTC_TODAY = datetime.datetime.utcnow().date()
DATE_STR = UTC_TODAY.isoformat()

URLS = {
    "score_now": "https://api-web.nhle.com/v1/scoreboard/now",
    "score_day": f"https://api-web.nhle.com/v1/scoreboard/{DATE_STR}",
    "stats_day": f"https://statsapi.web.nhl.com/api/v1/schedule?date={DATE_STR}&expand=schedule.linescore,schedule.teams",
}

def get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent":"MyPyBITE/nhl-fetch"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def state_map(s:str) -> str:
    t = (s or "").upper()
    if "IN_PROGRESS" in t or "LIVE" in t or "STARTED" in t: return "Live"
    if "FINAL" in t or t == "OFF": return "Final"
    if "PRE" in t or "FUT" in t or "SCHEDULED" in t or "PRE_GAME" in t: return "Preview"
    return s or "Unknown"

def ord_period(p:int) -> str:
    return "1st" if p==1 else "2nd" if p==2 else "3rd" if p==3 else ("OT" if p>=4 else "")

def normalize_from_scoreboard(data:dict):
    bucket = data.get("games") or [g for d in data.get("gameWeek",[]) for g in d.get("games",[])]
    out=[]
    for g in bucket or []:
        period = g.get("period") or (g.get("periodDescriptor") or {}).get("number") or 0
        time_rem = (g.get("clock") or {}).get("timeRemaining") \
                   or (g.get("linescore") or {}).get("currentPeriodTimeRemaining")
        out.append({
            "gamePk": g.get("id") or g.get("gamePk"),
            "gameDate": g.get("startTimeUTC") or g.get("gameDate"),
            "status": {"abstractGameState": state_map(g.get("gameState") or g.get("gameStatus")),
                       "detailedState": state_map(g.get("gameState") or g.get("gameStatus"))},
            "teams": {
                "away": {
                    "team": {"abbreviation": (g.get("awayTeam") or {}).get("abbrev", "AWAY")},
                    "score": (g.get("awayTeam") or {}).get("score")
                },
                "home": {
                    "team": {"abbreviation": (g.get("homeTeam") or {}).get("abbrev", "HOME")},
                    "score": (g.get("homeTeam") or {}).get("score")
                }
            },
            "linescore": {
                "currentPeriod": period,
                "currentPeriodOrdinal": ord_period(period),
                "currentPeriodTimeRemaining": time_rem
            }
        })
    return out

def normalize_from_statsapi(data:dict):
    out=[]
    for g in (data.get("dates",[{"games":[]}])[0].get("games",[])):
        ls = g.get("linescore") or {}
        out.append({
            "gamePk": g.get("gamePk"),
            "gameDate": g.get("gameDate"),
            "status": g.get("status") or {},
            "teams": {
                "away": {
                    "team": {"abbreviation": (g.get("teams",{}).get("away",{}).get("team",{}) or {}).get("abbreviation","AWAY")},
                    "score": g.get("teams",{}).get("away",{}).get("score")
                },
                "home": {
                    "team": {"abbreviation": (g.get("teams",{}).get("home",{}).get("team",{}) or {}).get("abbreviation","HOME")},
                    "score": g.get("teams",{}).get("home",{}).get("score")
                }
            },
            "linescore": {
                "currentPeriod": ls.get("currentPeriod"),
                "currentPeriodOrdinal": ls.get("currentPeriodOrdinal"),
                "currentPeriodTimeRemaining": ls.get("currentPeriodTimeRemaining"),
            }
        })
    return out

def main():
    games=[]
    # Try scoreboard/now → scoreboard/day → stats api
    try:
        games = normalize_from_scoreboard(get(URLS["score_now"]))
        if not games:
            games = normalize_from_scoreboard(get(URLS["score_day"]))
    except Exception:
        pass
    if not games:
        try:
            games = normalize_from_statsapi(get(URLS["stats_day"]))
        except Exception:
            games = []

    payload = {"dates": [] if not games else [{"date": DATE_STR, "games": games}]}

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",",":"))
    print(f"Wrote {OUT_PATH} with {len(games)} game(s).")
    return 0

if __name__ == "__main__":
    sys.exit(main())
