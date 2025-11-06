#!/usr/bin/env python3
# scripts/fetch_ahl.py
# Build newsriver/ahl.json in the relay shape your flipboard expects.
# - Uses ESPN AHL scoreboard with explicit date(s)
# - Includes yesterday before ~04:00 America/Toronto
# - Adds top-level generated_utc and source for freshness/traceability
# Stdlib only.

from __future__ import annotations
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None  # fallback to UTC

BASE = "https://site.api.espn.com/apis/site/v2/sports/hockey/ahl/scoreboard?dates={yyyymmdd}"
OUT = Path("newsriver/ahl.json")

REQUEST_TIMEOUT = 10  # seconds
RETRY = 1             # total attempts = RETRY + 1
LOCAL_TZ = "America/Toronto"
EARLY_MORNING_HOUR = 4  # include yesterday if now < 04:00 ET


def _now_toronto() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo(LOCAL_TZ))
    # Fallback: UTC (boundary may be off, but still safe)
    return datetime.now(timezone.utc)


def _fmt_yyyymmdd(d: datetime) -> str:
    return d.strftime("%Y%m%d")


def _dates_to_fetch() -> list[str]:
    now = _now_toronto()
    today = now.date()
    want = [ _fmt_yyyymmdd(datetime(today.year, today.month, today.day)) ]
    if now.hour < EARLY_MORNING_HOUR:
        y = today - timedelta(days=1)
        want.append(_fmt_yyyymmdd(datetime(y.year, y.month, y.day)))
    return want


def _http_get_json(url: str) -> dict:
    last_err = None
    for _ in range(RETRY + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "MyPyBITE/ahl-relay",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(0.6)
    raise RuntimeError(f"GET failed: {url} :: {last_err}")


def _map_state(status_type: dict) -> str:
    # ESPN status.type has {state/name/description}, plus top-level description/completed flags
    t = (status_type or {}).get("state") or (status_type or {}).get("name") or ""
    s = (t or "").lower()
    if ("final" in s) or ("post" in s) or ("complete" in s) or ("status_final" in s):
        return "Final"
    if ("in " in s) or ("live" in s) or ("status_in_progress" in s) or ("playing" in s):
        return "Live"
    return "Preview"


def _ord_period(n: int | None) -> str | None:
    if not n:
        return None
    if n == 1:
        return "1st"
    if n == 2:
        return "2nd"
    if n == 3:
        return "3rd"
    return "OT"


def _abbr(team_obj: dict) -> str:
    t = team_obj or {}
    return (t.get("abbreviation") or t.get("shortDisplayName") or t.get("displayName") or "TEAM").upper()[:4]


def _to_int(v):
    try:
        return int(v) if v is not None else None
    except Exception:
        return None


def _normalize_events(data: dict) -> list[dict]:
    events = data.get("events") or []
    games = []
    for ev in events:
        comp = (ev.get("competitions") or [{}])[0]
        status = ev.get("status") or {}
        status_type = status.get("type") or comp.get("status", {}).get("type") or {}
        abs_state = _map_state(status_type)
        det_state = abs_state
        start_iso = ev.get("date")
        game_id = ev.get("id") or comp.get("id")

        # Period / linescore
        period_num = status.get("period") or comp.get("status", {}).get("period")
        current_ord = None
        if abs_state == "Live":
            current_ord = _ord_period(period_num)
        elif abs_state == "Final":
            current_ord = "Final"

        # Teams (ESPN puts competitors inside competition)
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
                "currentPeriodOrdinal": current_ord,  # your cards will read either of these
                "currentQuarter": current_ord,
            },
            "teams": {
                "away": {
                    "team": {"abbreviation": _abbr(away_team)},
                    "score": _to_int((c_away or {}).get("score")),
                },
                "home": {
                    "team": {"abbreviation": _abbr(home_team)},
                    "score": _to_int((c_home or {}).get("score")),
                },
            },
        }
        games.append(game)
    return games


def _write_json(obj: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUT}")


def main() -> int:
    want_dates = _dates_to_fetch()  # ['YYYYMMDD', ('YYYYMMDD' for yesterday if early)]
    all_games: list[dict] = []

    for ymd in want_dates:
        url = BASE.format(yyyymmdd=ymd)
        try:
            data = _http_get_json(url)
        except Exception as e:
            print(f"[warn] AHL fetch failed for {ymd}: {e}", file=sys.stderr)
            continue
        all_games.extend(_normalize_events(data))

    # Deduplicate by gamePk (last write wins; today takes precedence)
    uniq = {}
    for g in all_games:
        gid = g.get("gamePk")
        if gid:
            uniq[gid] = g
    games_out = list(uniq.values())

    out = {
        "generated_utc": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": ["espn ahl scoreboard"],
        "dates": [{"games": games_out}],
    }
    _write_json(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
