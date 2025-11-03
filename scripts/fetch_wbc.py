#!/usr/bin/env python3
# scripts/fetch_wbc.py
# Builds newsriver/wbc.json (and a root copy wbc.json) in the same relay shape your flipboard expects.
# Stdlib only. Safe when the tournament is off (writes an empty payload).

import json
import sys
import urllib.request
from pathlib import Path

# --- Candidate public scoreboards ---
# Note: WBC endpoints are only hot during the tournament window. We try a couple of plausible
# ESPN slugs and gracefully fall back to an empty file if none respond.
CANDIDATE_SRCS = [
    # ESPN's international baseball tournament slug used during the event (varies by year):
    "https://site.api.espn.com/apis/site/v2/sports/baseball/world-baseball-classic/scoreboard",
    # Older/alt ESPN naming some years (kept as a backup guess):
    "https://site.api.espn.com/apis/site/v2/sports/baseball/wbc/scoreboard",
]

# Outputs (mirror your repo convention: served at root AND /newsriver/)
OUT_NEWSRIVER = Path("newsriver/wbc.json")
OUT_ROOT = Path("wbc.json")


# --- Helpers mirrored from your MLS relay style ---

def map_state(status_desc: str) -> str:
    s = (status_desc or "").lower()
    if "final" in s or "post" in s or "ended" in s or "complete" in s:
        return "Final"
    if "in progress" in s or "live" in s or "status_in_progress" in s or "ongoing" in s:
        return "Live"
    return "Preview"


def ord_period(n: int | None) -> str | None:
    # Baseball: show "OT" as "ET" style if needed; otherwise use innings if available.
    # ESPN may not expose inning as a simple integer on tournament feeds; we keep this minimal.
    if not n:
        return None
    if n == 1:
        return "1st"
    if n == 2:
        return "2nd"
    if n >= 3:
        return "OT"
    return None


# Normalize country/team labels to short, 3–4 char tags for your card.
COUNTRY_ABBR = {
    "CANADA": "CAN", "UNITED STATES": "USA", "USA": "USA", "JAPAN": "JPN", "KOREA": "KOR",
    "SOUTH KOREA": "KOR", "REPUBLIC OF KOREA": "KOR", "CHINA": "CHN", "TAIWAN": "TPE",
    "CHINESE TAIPEI": "TPE", "DOMINICAN REPUBLIC": "DOM", "PUERTO RICO": "PUR",
    "MEXICO": "MEX", "VENEZUELA": "VEN", "CUBA": "CUB", "NETHERLANDS": "NED",
    "ITALY": "ITA", "AUSTRALIA": "AUS", "ISRAEL": "ISR", "COLOMBIA": "COL",
    "PANAMA": "PAN", "CZECH REPUBLIC": "CZE", "GREAT BRITAIN": "GBR", "NICARAGUA": "NCA"
}


def abbr_from_team_obj(team_obj: dict) -> str:
    t = (team_obj or {})
    raw = (t.get("abbreviation")
           or t.get("shortDisplayName")
           or t.get("displayName")
           or "TEAM").upper().strip()
    # Map common country names to short codes; otherwise trim to 4 chars.
    return COUNTRY_ABBR.get(raw, COUNTRY_ABBR.get(raw.replace(" NATIONAL TEAM", ""), raw[:4]))


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
        status_type = (ev.get("status") or {}).get("type") or {}
        status_desc = status_type.get("description") or status_type.get("name") or ""
        abs_state = map_state(status_desc)
        det_state = abs_state
        start_iso = ev.get("date")
        game_id = ev.get("id") or comp.get("id")

        # Period/inning (ESPN may or may not expose a numeric period for WBC)
        period_num = ev.get("status", {}).get("period") or comp.get("status", {}).get("period")
        current_p = None
        if abs_state == "Live":
            current_p = ord_period(period_num)
        elif abs_state == "Final":
            current_p = "Final"

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

        games.append({
            "gamePk": game_id,
            "gameDate": start_iso,
            "status": {
                "detailedState": det_state,
                "abstractGameState": abs_state,
            },
            "linescore": {
                "currentPeriodOrdinal": current_p,   # your generic card key
                "currentQuarter": current_p,         # kept for compatibility
            },
            "teams": {
                "away": {
                    "team": {"abbreviation": abbr_from_team_obj(away_team)},
                    "score": to_int((c_away or {}).get("score")),
                },
                "home": {
                    "team": {"abbreviation": abbr_from_team_obj(home_team)},
                    "score": to_int((c_home or {}).get("score")),
                },
            },
        })

    return {"dates": [{"games": games}]}


def fetch_first_available() -> dict | None:
    # Try each candidate until one returns OK JSON.
    for url in CANDIDATE_SRCS:
        try:
            req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                if resp.status != 200:
                    print(f"WBC fetch failed: HTTP {resp.status} for {url}", file=sys.stderr)
                    continue
                return json.load(resp)
        except Exception as e:
            print(f"WBC fetch error for {url}: {e}", file=sys.stderr)
            continue
    return None


def write_payload(payload: dict):
    # Write to both /newsriver and site root to match your Pages setup.
    OUT_NEWSRIVER.parent.mkdir(parents=True, exist_ok=True)
    with OUT_NEWSRIVER.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    with OUT_ROOT.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def write_empty():
    empty = {"dates": [{"games": []}]}
    write_payload(empty)
    print(f"Wrote empty WBC payload to {OUT_NEWSRIVER} and {OUT_ROOT}", file=sys.stderr)


def main():
    data = fetch_first_available()
    if not data:
        write_empty()
        return
    relay = to_relay(data)
    write_payload(relay)
    print(f"Wrote WBC relay to {OUT_NEWSRIVER} and {OUT_ROOT}")


if __name__ == "__main__":
    main()
