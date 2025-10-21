#!/usr/bin/env python3
"""
fetch_tickerlines.py — build a hyperbolic-only ticker payload for the pill UI.

- Reads  : ./headlines.json                  (repo root)
- Writes : ./newsriver/newsriver/dredge_heds.json  (front-end ticker reads this)

Priority:
  1) SPORTS (Toronto-heavy + MLB postseason)
  2) CASUALTY (mass-incident within ~1200km)
Backfill if needed (to always emit 3 items):
  3) LOCAL Toronto-domain items (fresh)
  4) FRESHEST general headlines (non-aggregators, non-crypto)

We still keep: ≤24h hard age, ≤12h soft, 1-per-domain.
"""
from __future__ import annotations

import argparse, json, math, re, sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ------------------- Config -------------------

SOFT_HOURS = 12
HARD_HOURS = 24
MAX_ITEMS = 3
PER_DOMAIN_CAP = 1

TOR_LAT, TOR_LON = 43.6532, -79.3832
CASUALTY_MAX_KM = 1200.0

CASUALTY_RE = re.compile(
    r"\b(dead|deaths?|killed|killing|fatal(ity|ities)?|mass\s+shooting|shooting|"
    r"explosion|blast|bomb(ing)?|missile|air[-\s]?strike|"
    r"earthquake|tornado|hurricane|wildfire|flood|tsunami|derailment|casualties?)\b",
    re.I
)

BREAKING_HINTRE = re.compile(r"\b(breaking|developing|just in|alert)\b", re.I)

TORONTO_LOCAL_DOMAINS = {
    "toronto.citynews.ca",
    "www.cp24.com",
    "www.thestar.com",
    "www.blogto.com",
    "www.cbc.ca",
    "globalnews.ca",
    "toronto.ctvnews.ca",
}

GAZETTEER: Dict[str, Tuple[float, float]] = {
    "toronto": (43.6532, -79.3832), "mississauga": (43.5890, -79.6441),
    "brampton": (43.7315, -79.7624), "vaughan": (43.8372, -79.5083),
    "markham": (43.8561, -79.3370), "scarborough": (43.7731, -79.2578),
    "oakville": (43.4675, -79.6877), "burlington": (43.3255, -79.7990),
    "oshawa": (43.8971, -78.8658), "pickering": (43.8384, -79.0868),
    "ajax": (43.8509, -79.0204), "whitby": (43.8971, -78.9429),
    "hamilton": (43.2557, -79.8711), "guelph": (43.5448, -80.2482),
    "kitchener": (43.4516, -80.4925), "waterloo": (43.4643, -80.5204),
    "cambridge": (43.3616, -80.3144), "london, ontario": (42.9849, -81.2453),
    "st. catharines": (43.1594, -79.2469), "niagara falls": (43.0896, -79.0849),
    "windsor": (42.3149, -83.0364), "barrie": (44.3894, -79.6903),
    "kingston": (44.2312, -76.4860), "ottawa": (45.4215, -75.6972),
    "buffalo": (42.8864, -78.8784), "rochester": (43.1566, -77.6088),
    "detroit": (42.3314, -83.0458), "cleveland": (41.4993, -81.6944),
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

# --- SPORTS (Toronto emphasis) ---

SPORTS_TEAMS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\btoronto\s+blue\s*jays\b|\bblue\s*jays\b|\bjays\b", re.I), "toronto"),
    (re.compile(r"\btoronto\s+maple\s*leafs\b|\bmaple\s*leafs\b|\bleafs\b", re.I), "toronto"),
    (re.compile(r"\btoronto\s+raptors\b|\braptors\b", re.I), "toronto"),
    (re.compile(r"\btoronto\s+fc\b|\btfc\b", re.I), "toronto"),
    (re.compile(r"\btoronto\s+argos?\b|\bargos?\b", re.I), "toronto"),
]

POSTSEASON_RE = re.compile(r"\b(ALCS|NLCS|ALDS|NLDS|World Series|postseason|playoffs?)\b", re.I)

AGGREGATOR_RE = re.compile(r"news\.google|news\.yahoo|apple\.news|bing\.com/news|msn\.com/en-", re.I)
CRYPTO_DOMAINS = re.compile(r"(coindesk|cointelegraph|theblock|decrypt|blockworks|coinmarketcap)", re.I)

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
    kind: str           # "SPORTS" | "CASUALTY" | "LOCAL" | "FRESH"
    city: Optional[str]
    km: Optional[float]
    score: float

# ------------------- Helpers -------------------

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

def load_raw(path: Path) -> List[RawItem]:
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
        if not ts or _age_hours(ts) > HARD_HOURS: continue
        out.append(RawItem(title=title, url=url, ts=ts, source=src, domain=_domain(url)))
    out.sort(key=lambda x: x.ts, reverse=True)
    return out

# ------------------- Detection -------------------

def detect_sports_city(title: str) -> Optional[str]:
    t = title.lower()
    for pat, city in SPORTS_TEAMS:
        if pat.search(t): return city
    if "blue jays" in t or "jays" in t:
        if POSTSEASON_RE.search(t):
            return "toronto"
    return None

CITY_NAME_RE = re.compile("|".join(
    sorted((re.escape(n) for n in GAZETTEER.keys()), key=len, reverse=True)
), re.I)

def detect_city_from_title(title: str) -> Optional[str]:
    m = CITY_NAME_RE.search(title)
    if not m: return None
    name = m.group(0).lower()
    if name == "london, ontario": return "london, ontario"
    return name

def infer_toronto_from_domain(domain: str) -> bool:
    return domain in TORONTO_LOCAL_DOMAINS

def is_crypto_like(title: str, domain: str) -> bool:
    return bool(re.search(r"\b(btc|bitcoin|eth|ethereum)\b", title, re.I)) or bool(CRYPTO_DOMAINS.search(domain))

# ------------------- Candidate building -------------------

def build_pools(rows: List[RawItem]) -> Tuple[List[Cand], List[Cand], List[Cand], List[Cand]]:
    sports: List[Cand] = []
    casualty: List[Cand] = []
    local: List[Cand] = []
    fresh: List[Cand] = []

    for r in rows:
        t = r.title.lower()

        sport_city = detect_sports_city(t)
        if sport_city and not is_crypto_like(t, r.domain):
            km = km_to_toronto(sport_city)
            sports.append(Cand(r, "SPORTS", sport_city, km, 0.0))
            continue

        if CASUALTY_RE.search(t):
            city = detect_city_from_title(t)
            if not city and infer_toronto_from_domain(r.domain):
                city = "toronto"
            if city:
                km = km_to_toronto(city)
                if km is not None and km <= CASUALTY_MAX_KM:
                    casualty.append(Cand(r, "CASUALTY", city, km, 0.0))
            continue

        if r.domain in TORONTO_LOCAL_DOMAINS:
            local.append(Cand(r, "LOCAL", "toronto", 0.0, 0.0))
        else:
            # general fresh headlines (non-aggregators, non-crypto)
            if not is_crypto_like(t, r.domain):
                fresh.append(Cand(r, "FRESH", None, None, 0.0))

    return sports, casualty, local, fresh

# ------------------- Scoring & selection -------------------

def age_boost(ts: datetime) -> float:
    age_h = _age_hours(ts)
    recency = max(0.0, (HARD_HOURS - age_h))
    if age_h <= SOFT_HOURS:
        recency += 10.0
    return recency

def score(c: Cand) -> float:
    s = age_boost(c.item.ts)
    if c.km is not None:
        s += max(0.0, 1000.0 - c.km) / 10.0
    if c.kind == "SPORTS" and (c.city or "").lower() == "toronto":
        s += 50.0
    return s

def take_with_caps(cands: List[Cand], want: int, already: List[Cand]) -> List[Cand]:
    seen_keys = { _dedupe_key(x.item.title, x.item.url) for x in already }
    per_domain: Dict[str,int] = {}
    for x in already:
        d = x.item.domain or ""
        per_domain[d] = per_domain.get(d, 0) + 1

    out: List[Cand] = []
    for c in sorted(cands, key=lambda x: (x.score, x.item.ts), reverse=True):
        key = _dedupe_key(c.item.title, c.item.url)
        if key in seen_keys: continue
        d = c.item.domain or ""
        if PER_DOMAIN_CAP and per_domain.get(d, 0) >= PER_DOMAIN_CAP: continue
        out.append(c)
        seen_keys.add(key)
        per_domain[d] = per_domain.get(d, 0) + 1
        if len(out) >= want: break
    return out

def select_top(sports: List[Cand], casualty: List[Cand], local: List[Cand], fresh: List[Cand]) -> List[Cand]:
    for bag in (sports, casualty, local, fresh):
        for c in bag: c.score = score(c)

    picked: List[Cand] = []
    picked += take_with_caps(sports, MAX_ITEMS - len(picked), picked)
    if len(picked) < MAX_ITEMS:
        picked += take_with_caps(casualty, MAX_ITEMS - len(picked), picked)
    if len(picked) < MAX_ITEMS:
        picked += take_with_caps(local, MAX_ITEMS - len(picked), picked)
    if len(picked) < MAX_ITEMS:
        picked += take_with_caps(fresh, MAX_ITEMS - len(picked), picked)

    return picked[:MAX_ITEMS]

# ------------------- Wire -------------------

def to_wire(picked: List[Cand]) -> Dict[str, Any]:
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
            "url": c.item.url,
            "flags": flags
        })
    return {"items": items, "generated_utc": iso(datetime.now(timezone.utc))}

# ------------------- CLI -------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Build 3-item ticker JSON")
    ap.add_argument("--in",  dest="inp", default="./headlines.json", help="input headlines JSON (repo root)")
    ap.add_argument("--out", dest="out", default="./newsriver/newsriver/dredge_heds.json",
                    help="output ticker JSON (front-end reads this)")
    args = ap.parse_args()

    rows = load_raw(Path(args.inp))
    if not rows:
        print("[fetch_tickerlines] ERROR: no usable input rows", file=sys.stderr)
        return 1

    sports, casualty, local, fresh = build_pools(rows)
    picked = select_top(sports, casualty, local, fresh)

    # ALWAYS emit 3 by design now (if we couldn’t, emit what we have but with exit 0)
    if not picked:
        print("[fetch_tickerlines] WARN: no candidates; emitting Loading placeholders", file=sys.stderr)
        wire = {"items":[{"text":"Loading…","display":"Loading…","url":"#","flags":{}}]*3,"generated_utc":datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    else:
        wire = to_wire(picked)
        if len(picked) < MAX_ITEMS:
            print(f"[fetch_tickerlines] WARN: only {len(picked)} items; backfilled from local/fresh pools", file=sys.stderr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(wire, ensure_ascii=False, separators=(",", ":"), indent=2), encoding="utf-8")
    print(f"[fetch_tickerlines] {len(picked)} items → {out_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
