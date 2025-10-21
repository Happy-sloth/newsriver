#!/usr/bin/env python3
"""
fetch_tickerlines.py — build a hyperbolic-only ticker payload for the pill UI.

- Reads  : ./headlines.json                         (single source of truth, repo root)
- Writes : ./newsriver/newsriver/dredge_heds.json   (front-end ticker reads this)

Selection (primary):
  • SPORTS: Toronto teams + relaxed Jays/ALCS detection.
  • CASUALTY: death/mass-casualty cues within 1200 km of Toronto.

Guarantee:
  • Always emit 3 items. If strict selection <3, perform controlled backfill:
    - recent Toronto-local items
    - recent SPORTS-adjacent titles (looser keywords)
    - as a last resort, widen SPORTS time window slightly

Exit codes:
  0 = success; 1 = input missing/invalid
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ------------------- Config knobs -------------------

SOFT_HOURS       = 20
HARD_HOURS       = 30
MAX_ITEMS        = 3
PER_DOMAIN_CAP   = 1     # prevent multiple from same outlet
BACKFILL_HOURS   = 36    # extra window for backfill if needed
SPORTS_WIDEN_HRS = 48    # widen for SPORTS-only, last resort

# Toronto ref (CN Tower-ish)
TOR_LAT, TOR_LON = 43.6532, -79.3832
CASUALTY_MAX_KM  = 1200.0

# Casualty (mass-incident) cues
CASUALTY_RE = re.compile(
    r"\b(dead|deaths?|killed|killing|fatal(ity|ities)?|mass\s+shooting|shooting|"
    r"explosion|blast|bomb(ing)?|missile|air[-\s]?strike|"
    r"earthquake|tornado|hurricane|wildfire|flood|tsunami|derailment|casualties?)\b",
    re.I
)

# Obvious "Breaking" words (for flags only)
BREAKING_HINTRE = re.compile(r"\b(breaking|developing|just in|alert)\b", re.I)

# Treat these Toronto outlets as local-to-Toronto even without city mention
TORONTO_LOCAL_DOMAINS = (
    "toronto.citynews.ca",
    "www.cp24.com",
    "www.thestar.com",
    "www.blogto.com",
    "www.cbc.ca",
    "globalnews.ca",
    "toronto.ctvnews.ca",
    "toronto.citynews.ca",
)

# Gazetteer (name -> (lat, lon))
GAZETTEER: Dict[str, Tuple[float, float]] = {
    # GTA / Ontario (subset sufficient for proximity)
    "toronto": (43.6532, -79.3832), "mississauga": (43.5890, -79.6441), "brampton": (43.7315, -79.7624),
    "vaughan": (43.8372, -79.5083), "markham": (43.8561, -79.3370), "richmond hill": (43.8828, -79.4403),
    "scarborough": (43.7731, -79.2578), "oakville": (43.4675, -79.6877), "burlington": (43.3255, -79.7990),
    "oshawa": (43.8971, -78.8658), "pickering": (43.8384, -79.0868), "ajax": (43.8509, -79.0204),
    "whitby": (43.8971, -78.9429), "hamilton": (43.2557, -79.8711),
    "ottawa": (45.4215, -75.6972), "kingston": (44.2312, -76.4860), "london, ontario": (42.9849, -81.2453),
    # US northeast / GL (enough for 1200km)
    "buffalo": (42.8864, -78.8784), "rochester": (43.1566, -77.6088), "syracuse": (43.0481, -76.1474),
    "new york": (40.7128, -74.0060), "boston": (42.3601, -71.0589), "detroit": (42.3314, -83.0458),
    "cleveland": (41.4993, -81.6944), "chicago": (41.8781, -87.6298), "pittsburgh": (40.4406, -79.9959),
}

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi  = math.radians(lat2 - lat1)
    dlmb  = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return 2*R*math.asin(math.sqrt(a))

def km_to_toronto(city: str) -> Optional[float]:
    c = city.lower()
    if c not in GAZETTEER: return None
    lat, lon = GAZETTEER[c]
    return haversine_km(TOR_LAT, TOR_LON, lat, lon)

# Sports team → city mapping (regex → canonical city name)
SPORTS_TEAMS: List[Tuple[re.Pattern, str, str]] = [
    # Toronto teams
    (re.compile(r"\btoronto\s+maple\s*leafs\b|\bmaple\s*leafs\b|\bleafs\b", re.I), "Toronto", "NHL"),
    (re.compile(r"\btoronto\s+blue\s*jays\b|\bblue\s*jays\b|\bjays\b", re.I), "Toronto", "MLB"),
    (re.compile(r"\btoronto\s+argos?\b|\bargos?\b", re.I), "Toronto", "CFL"),
    (re.compile(r"\btoronto\s+raptors\b|\braptors\b", re.I), "Toronto", "NBA"),
    (re.compile(r"\btoronto\s+fc\b|\btfc\b", re.I), "Toronto", "MLS"),
]

AGGREGATOR_RE  = re.compile(r"news\.google|news\.yahoo|apple\.news|bing\.com/news|msn\.com/en-", re.I)
CRYPTO_DOMAINS = re.compile(r"(coindesk|cointelegraph|theblock|decrypt|blockworks|coinmarketcap)", re.I)

# Relaxed ALCS / Jays helpers
SPORTS_DOMAINS_HINTS = (
    ("mlb.com", "/bluejays/"),
    ("sportsnet.ca", ""),
    ("tsn.ca", ""),
    ("theathletic.com", ""),
)
ALCS_TITLE_RE = re.compile(
    r"\b(ALCS|American League Championship Series|Game\s?\d+|walk-?off|home\s?run|homer|grand\s?slam)\b",
    re.I
)
BASEBALL_SOFT_RE = re.compile(
    r"\b(ALCS|ALDS|American League|Game\s?\d+|walk-?off|home\s?run|homer|grand\s?slam|extra\s+innings)\b",
    re.I
)

# ------------------- Types -------------------

@dataclass
class RawItem:
    title: str
    url: str
    ts: datetime
    source: str
    domain: str

@dataclass
class Cand:
    item: RawItem
    kind: str           # "SPORTS" | "CASUALTY" | "BACKFILL"
    city: Optional[str] # detected city
    km: Optional[float] # distance to Toronto
    score: float

# ------------------- Core helpers -------------------

def _to_list(root: Any) -> List[Dict[str, Any]]:
    if isinstance(root, list): return root
    if isinstance(root, dict):
        for k in ("items","articles","data"):
            v = root.get(k)
            if isinstance(v, list): return v
    return []

def _pick_url(d: Dict[str, Any]) -> str:
    for k in ("canonical_url","url","link","href","permalink"):
        v = d.get(k)
        if isinstance(v, str) and v.strip(): return v.strip()
    g = d.get("guid")
    if isinstance(g, dict) and isinstance(g.get("link"), str): return g["link"].strip()
    return ""

def _pick_title(d: Dict[str, Any]) -> str:
    for k in ("title","headline","name","text"):
        v = d.get(k)
        if isinstance(v, str) and v.strip(): return v.strip()
    return ""

def _pick_source(d: Dict[str, Any]) -> str:
    v = d.get("source") or d.get("publisher") or d.get("domain")
    if isinstance(v, dict): v = v.get("name") or v.get("domain")
    return str(v or "")

def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        h = urlparse(url).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""

def _parse_ts(raw: Any) -> Optional[datetime]:
    if isinstance(raw, (int, float)):
        sec = raw/1000.0 if raw > 10_000_000_000 else raw
        return datetime.fromtimestamp(sec, tz=timezone.utc)
    if not raw: return None
    s = str(raw).strip()
    s = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(s);  return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(s);   return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def _first_ts(d: Dict[str, Any]) -> Optional[datetime]:
    for k in ("published_utc","published_at","published","updated_at","pubDate","date","time","timestamp"):
        dt = _parse_ts(d.get(k))
        if dt: return dt.astimezone(timezone.utc)
    return None

def _age_hours(dt: datetime) -> float:
    now = datetime.now(timezone.utc)
    return max(0.0, (now - dt).total_seconds()/3600.0)

def _dedupe_key(title: str, url: str) -> str:
    return f"{title.strip().lower()}|{_domain(url)}"

def load_raw(path: Path, max_hours: int = HARD_HOURS) -> List[RawItem]:
    if not path.exists():
        print(f"[fetch_tickerlines] ERROR: input not found: {path}", file=sys.stderr)
        return []
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[fetch_tickerlines] ERROR: cannot parse JSON: {path} ({e})", file=sys.stderr)
        return []
    out: List[RawItem] = []
    for r in _to_list(root):
        title = _pick_title(r); url = _pick_url(r)
        if not title or not url: continue
        src = _pick_source(r)
        if AGGREGATOR_RE.search(f"{src} {url}"): continue
        ts = _first_ts(r)
        if not ts or _age_hours(ts) > max_hours: continue
        out.append(RawItem(title=title, url=url, ts=ts, source=src, domain=_domain(url)))
    out.sort(key=lambda x: x.ts, reverse=True)
    return out

# ------------------- Detection -------------------

def is_crypto_like(title: str, domain: str) -> bool:
    return bool(re.search(r"\b(btc|bitcoin|eth|ethereum)\b", title, re.I)) or bool(CRYPTO_DOMAINS.search(domain))

def infer_toronto_from_domain(domain: str) -> bool:
    return domain in TORONTO_LOCAL_DOMAINS

CITY_NAME_RE = re.compile("|".join(
    sorted((re.escape(n) for n in GAZETTEER.keys()), key=len, reverse=True)
), re.I)

def detect_city_from_title(title: str) -> Optional[str]:
    m = CITY_NAME_RE.search(title)
    if not m: return None
    name = m.group(0).lower()
    if name == "london, ontario": return "london, ontario"
    return name

def is_jays_by_domain(title: str, domain: str, url: str) -> bool:
    d = (domain or "").lower()
    u = (url or "").lower()
    tl = title.lower()
    for host, path_hint in SPORTS_DOMAINS_HINTS:
        if host in d and (not path_hint or path_hint in u):
            if "toronto" in tl or "blue jays" in tl or re.search(r"\bjays\b", tl, re.I):
                return True
    return False

def detect_sports_city(title: str, domain: str, url: str) -> Optional[str]:
    tl = title.lower()
    # direct team-name match
    for pat, city, _league in SPORTS_TEAMS:
        if pat.search(tl):
            return city
    # Jays by domain + Toronto mention
    if is_jays_by_domain(title, domain, url):
        return "Toronto"
    # Jays by ALCS keywords + "Toronto"
    if "toronto" in tl and ALCS_TITLE_RE.search(tl):
        return "Toronto"
    return None

# ------------------- Candidate building -------------------

def build_candidates(rows: List[RawItem]) -> List[Cand]:
    cands: List[Cand] = []
    for r in rows:
        title = r.title
        tl = title.lower()

        # SPORTS (strict)
        sport_city = detect_sports_city(title, r.domain, r.url)
        if sport_city and not is_crypto_like(tl, r.domain):
            km = km_to_toronto(sport_city.lower())
            cands.append(Cand(item=r, kind="SPORTS", city=sport_city, km=km, score=0.0))
            continue

        # CASUALTY
        if CASUALTY_RE.search(tl):
            city = detect_city_from_title(title)
            if not city and infer_toronto_from_domain(r.domain):
                city = "toronto"
            if city:
                km = km_to_toronto(city)
                if km is not None and km <= CASUALTY_MAX_KM:
                    cands.append(Cand(item=r, kind="CASUALTY", city=city, km=km, score=0.0))
    return cands

# ------------------- Scoring & selection -------------------

def score_candidate(c: Cand) -> float:
    age_h = _age_hours(c.item.ts)
    recency = max(0.0, (HARD_HOURS - age_h))
    if age_h <= SOFT_HOURS:
        recency += 10.0
    prox = 0.0
    if c.km is not None:
        prox = max(0.0, 1000.0 - c.km) / 10.0
    tor_bonus = 40.0 if (c.kind == "SPORTS" and (c.city or "").lower() == "toronto") else 0.0
    return recency + prox + tor_bonus

def select_top(cands: List[Cand]) -> List[Cand]:
    for c in cands:
        c.score = score_candidate(c)
    cands.sort(key=lambda x: (x.score, x.item.ts), reverse=True)
    out: List[Cand] = []
    seen_keys = set()
    per_domain: Dict[str, int] = {}
    for c in cands:
        key = _dedupe_key(c.item.title, c.item.url)
        if key in seen_keys:
            continue
        dom = c.item.domain or ""
        if PER_DOMAIN_CAP > 0 and per_domain.get(dom, 0) >= PER_DOMAIN_CAP:
            continue
        seen_keys.add(key)
        per_domain[dom] = per_domain.get(dom, 0) + 1
        out.append(c)
        if len(out) >= MAX_ITEMS:
            break
    return out

# ------------------- Backfill -------------------

def backfill(rows: List[RawItem], picked: List[Cand]) -> List[Cand]:
    """Fill to MAX_ITEMS with Toronto-local recency and baseball-adjacent titles."""
    already = {_dedupe_key(c.item.title, c.item.url) for c in picked}
    per_domain: Dict[str, int] = {}
    for c in picked:
        d = c.item.domain or ""
        per_domain[d] = per_domain.get(d, 0) + 1

    def try_add(r: RawItem, kind: str) -> Optional[Cand]:
        key = _dedupe_key(r.title, r.url)
        if key in already: return None
        d = r.domain or ""
        if PER_DOMAIN_CAP > 0 and per_domain.get(d, 0) >= PER_DOMAIN_CAP:
            return None
        c = Cand(item=r, kind=kind, city=None, km=None, score=0.0)
        c.score = score_candidate(c)
        already.add(key)
        per_domain[d] = per_domain.get(d, 0) + 1
        return c

    # 1) Recent Toronto-local outlets (non-crypto), within BACKFILL_HOURS
    cutoff = datetime.now(timezone.utc) - timedelta(hours=BACKFILL_HOURS)
    for r in rows:
        if r.ts < cutoff: break
        if r.domain in TORONTO_LOCAL_DOMAINS and not is_crypto_like(r.title.lower(), r.domain):
            c = try_add(r, "BACKFILL")
            if c:
                picked.append(c)
                if len(picked) >= MAX_ITEMS: return picked

    # 2) Baseball-adjacent titles that mention Toronto (looser)
    for r in rows:
        if r.ts < cutoff: break
        tl = r.title.lower()
        if "toronto" in tl and BASEBALL_SOFT_RE.search(tl):
            c = try_add(r, "BACKFILL")
            if c:
                picked.append(c)
                if len(picked) >= MAX_ITEMS: return picked

    # 3) Last resort: widen window for SPORTS-only to SPORTS_WIDEN_HRS
    if len(picked) < MAX_ITEMS:
        wider = load_raw(Path(INPUT_PATH), max_hours=SPORTS_WIDEN_HRS)
        cands_extra: List[Cand] = []
        for r in wider:
            sc = detect_sports_city(r.title, r.domain, r.url)
            if sc and not is_crypto_like(r.title.lower(), r.domain):
                km = km_to_toronto(sc.lower())
                cands_extra.append(Cand(item=r, kind="SPORTS", city=sc, km=km, score=0.0))
        for c in select_top(cands_extra):
            added = try_add(c.item, c.kind)
            if added:
                picked.append(added)
                if len(picked) >= MAX_ITEMS: return picked

    return picked

# ------------------- Wire -------------------

def to_ticker_wire(picked: List[Cand]) -> Dict[str, Any]:
    def iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items = []
    for c in picked:
        flags = {
            "is_breaking": bool(BREAKING_HINTRE.search(c.item.title)),
            "is_landmark": (c.kind == "SPORTS" and (c.city or "").lower() == "toronto"),
            "has_bitcoin": False,
        }
        items.append({
            "text": c.item.title,
            "display": c.item.title,
            "url":  c.item.url,
            "flags": flags
        })
    return {
        "items": items,
        "generated_utc": iso(datetime.now(timezone.utc)),
        "meta": {
            "selected_kinds": [c.kind for c in picked]
        }
    }

# ------------------- CLI -------------------

# Global so backfill() can reload with widened hours
INPUT_PATH = "headlines.json"

def main() -> int:
    ap = argparse.ArgumentParser(description="Build hyperbolic-only 3-item ticker JSON for pill UI.")
    ap.add_argument("--in",  dest="inp", default="./headlines.json",
                    help="input enriched headlines JSON (repo root)")
    ap.add_argument("--out", dest="out", default="./newsriver/newsriver/dredge_heds.json",
                    help="output ticker JSON (front-end reads this)")
    args = ap.parse_args()

    global INPUT_PATH
    INPUT_PATH = args.inp

    rows = load_raw(Path(args.inp), max_hours=HARD_HOURS)
    if not rows:
        print("[fetch_tickerlines] ERROR: no usable input rows from headlines.json", file=sys.stderr)
        return 1

    cands  = build_candidates(rows)
    picked = select_top(cands)

    if len(picked) < MAX_ITEMS:
        print(f"[fetch_tickerlines] WARN: only {len(picked)} strict items; backfilling…", file=sys.stderr)
        picked = backfill(rows, picked)

    # final clamp (paranoia)
    picked = picked[:MAX_ITEMS]

    wire = to_ticker_wire(picked)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(wire, ensure_ascii=False, separators=(",", ":"), indent=2), encoding="utf-8")
    print(f"[fetch_tickerlines] {len(picked)} items → {out_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
