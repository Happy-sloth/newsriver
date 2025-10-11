#!/usr/bin/env python3
# scripts/fetch_cfl.py
# Primary: CFL official API (requires key). Fallback: ESPN.
# Outputs newsriver/cfl.json in your relay shape. Stdlib only.

import json, os, sys, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path("newsriver/cfl.json")

# ---- Sources ----
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/cfl/scoreboard"

# CFL official API. Commonly: https://api.cfl.ca/v1/games?date=YYYY-MM-DD&include=scores
# If the league’s path differs, set CFL_API_BASE via env. Only the key is required.
CFL_KEY  = os.environ.get("CFL_API_KEY", "").strip()
CFL_BASE = os.environ.get("CFL_API_BASE", "https://api.cfl.ca/v1/games")

# Keep “yesterday late finals” visible if their API only lists same-day games
def today_iso_tz(tz="America/Toronto"):
    # naive local ISO (yyyy-mm-dd) without tz for APIs that expect calendar date
    # Using Toronto hard-coded to match your site; if you prefer UTC, change here.
    # We don’t import pytz—keep it simple and use ET by offset approximation.
    # During DST ET=UTC-4, otherwise UTC-5. Good enough for daily scoreboards.
    now_utc = datetime.now(timezone.utc)
    # crude DST guess: between Mar 8 and Nov 7 use -4, else -5
    m, d = now_utc.month, now_utc.day
    off = -4 if (m>3 and m<11) or (m==3 and d>=8) or (m==11 and d<=7) else -5
    local = now_utc + timedelta(hours=off)
    return local.strftime("%Y-%m-%d")

def http_json(url, timeout=12):
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent":"mypybite-cfl/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200: return None
            return json.load(r)
    except Exception as e:
        print(f"[fetch] {url} -> {e}", file=sys.stderr)
        return None

# ---------- Mapping helpers ----------
def ord_period(n):
    if not n: return None
    try: n = int(n)
    except: return None
    return {1:"1st",2:"2nd",3:"3rd",4:"4th"}.get(n, "OT")

def to_int(v):
    try: return int(v) if v not in (None,"") else None
    except: return None

def abbr_any(team):
    t = team or {}
    return (t.get("abbreviation") or t.get("shortDisplayName") or t.get("displayName") or t.get("name") or "TEAM").upper()[:4]

# ---------- CFL official → relay ----------
def map_state_cfl(item):
    # Try a few common fields the CFL API exposes
    s = (str(item.get("status") or item.get("game_status") or item.get("event_status") or "")).lower()
    comp = item.get("completed")
    if comp is True: return "Final"
    if "final" in s or "complete" in s or "post" in s: return "Final"
    if "progress" in s or "live" in s or "playing" in s: return "Live"
    if "sched" in s or "pre" in s or "future" in s: return "Preview"
    return "Preview"

def from_cfl_api(data):
    # Accept common shapes: { "data":[...]} or { "games":[...] } or bare list
    rows = data.get("data") if isinstance(data, dict) else None
    if rows is None and isinstance(data, dict): rows = data.get("games")
    if rows is None and isinstance(data, list): rows = data
    rows = rows or []

    games = []
    for g in rows:
        # Identify date and IDs (field names vary; be defensive)
        start = g.get("date_start") or g.get("start_time") or g.get("game_date") or g.get("date") or ""
        gid   = g.get("game_id")_
