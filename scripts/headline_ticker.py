#!/usr/bin/env python3
"""
headline_ticker.py — build a tiny 3-item ticker feed for the pill UI.

Inputs:
  - --in  path to enriched headlines JSON (default: ./public/headlines.json)
  - --out path to ticker JSON to write     (default: ./public/headline_ticker.json)

Strategy (exactly as discussed):
  1) Filter to <=30h (hard cap). Prefer <=20h (soft window).
  2) Select up to TWO region items in priority order:
     Toronto → Vancouver → Montreal → New York → London (England).
     For each city, take the freshest non-crypto item.
  3) Select ONE crypto item (BTC/ETH). Prefer <=10h, then <=20h, else <=30h.
  4) If we still have <3, backfill with the freshest remaining non-crypto items.
Output schema:
{
  "items":[
    {"title":"…","url":"…","ts":"2025-01-15T01:23:45Z","region_city":"Toronto","is_crypto":false},
    ...
  ],
    "generated_at":"2025-01-15T01:31:10Z"
}
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---- Config knobs (small & readable) -----------------------------------------

# More precise city patterns:
# - New York excludes "New York Times"
# - London skips "London, Ont/ Ontario" (common Canadian disambiguation)
CITY_PRIORITY: List[Tuple[str, re.Pattern]] = [
    ("Toronto",   re.compile(r"\btoronto\b|\bblue\s*jays\b|\bjays\b|\bmaple\s*leafs\b|\bleafs\b|\braptors\b|\bargos?\b|\btfc\b", re.I)),
    ("Vancouver", re.compile(r"\bvancouver\b|\bwhitecaps\b", re.I)),
    ("Montreal",  re.compile(r"\bmontreal\b|\bcanadiens\b|\bhabs\b|\balouettes\b", re.I)),
    ("New York",  re.compile(r"\bnew\s*york\b(?!\s*times)", re.I)),
    ("London",    re.compile(r"\blondon\b(?!,\s*ont|\s*ontario)", re.I)),  # prefer London, England
]

CRYPTO_RE = re.compile(r"\b(btc|bitcoin|eth|ethereum)\b", re.I)
CRYPTO_DOMAINS = re.compile(r"(coindesk|cointelegraph|theblock|decrypt|blockworks|coincod|coinmarketcap)", re.I)

AGGREGATOR_RE = re.compile(r"news\.google|news\.yahoo|apple\.news|bing\.com/news", re.I)

SOFT_HOURS = 20        # preferred freshness (tightened)
HARD_HOURS = 30        # absolute max (tightened)
CRYPTO_PREF_HOURS = 10 # try extra-fresh crypto first (tightened)

MAX_ITEMS = 3

# ---- Helpers -----------------------------------------------------------------

@dataclass
class Item:
    title: str
    url: str
    ts: datetime
    source: str
    region_city: Optional[str]  # for output only (detected)
    is_crypto: bool

def _to_list(root: Any) -> List[Dict[str, Any]]:
    if isinstance(root, list):
        return root
    if isinstance(root, dict):
        for key in ("items", "articles", "data"):
            if isinstance(root.get(key), list):
                return root[key]
    return []

def _pick_url(it: Dict[str, Any]) -> str:
    for k in ("url", "link", "href", "permalink"):
        v = it.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    guid = it.get("guid")
    if isinstance(guid, dict) and isinstance(guid.get("link"), str):
        return guid["link"].strip()
    return ""

def _pick_title(it: Dict[str, Any]) -> str:
    for k in ("title", "headline", "name", "text"):
        v = it.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def _pick_source(it: Dict[str, Any]) -> str:
    v = it.get("source") or it.get("publisher") or it.get("domain")
    if isinstance(v, dict):
        v = v.get("name") or v.get("domain")
    return str(v or "")

def _parse_ts(raw: Any) -> Optional[datetime]:
    # handle unix secs/ms
    if isinstance(raw, (int, float)):
        sec = raw / 1000.0 if raw > 10_000_000_000 else raw
        return datetime.fromtimestamp(sec, tz=timezone.utc)
    if not raw:
        return None
    s = str(raw).strip()
    # normalize Z
    s = s.replace("Z", "+00:00") if s.endswith("Z") else s
    # try ISO
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    # try RFC 2822/1123, etc.
    try:
        dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def _first_ts(it: Dict[str, Any]) -> Optional[datetime]:
    for k in ("published_utc", "published_at", "published", "updated_at",
              "pubDate", "date", "time", "timestamp"):
        dt = _parse_ts(it.get(k))
        if dt:
            return dt.astimezone(timezone.utc)
    return None

def _domain_from_url(url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""

def _is_agg(source: str, url: str) -> bool:
    blob = f"{source} {url}"
    return bool(AGGREGATOR_RE.search(blob))

def _detect_city(title: str) -> Optional[str]:
    for city, pat in CITY_PRIORITY:
        if pat.search(title):
            # Additional London guard: if headline explicitly says London, Ont/ Ontario, skip city tag
            if city == "London" and re.search(r"\blondon\b,\s*(ont|ontario)\b", title, re.I):
                return None
            return city
    return None

def _is_crypto(title: str, url: str) -> bool:
    return bool(CRYPTO_RE.search(title)) or bool(CRYPTO_DOMAINS.search(_domain_from_url(url)))

def _age_hours(dt: datetime, now: datetime) -> float:
    return max(0.0, (now - dt).total_seconds() / 3600.0)

def _dedupe_key(title: str, url: str) -> str:
    return f"{title.strip().lower()}|{_domain_from_url(url)}"

# ---- Core selection -----------------------------------------------------------

def load_items(path: Path) -> List[Item]:
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    now = datetime.now(timezone.utc)

    out: List[Item] = []
    for raw in _to_list(root):
        title = _pick_title(raw)
        url   = _pick_url(raw)
        if not title or not url:
            continue
        if _is_agg(_pick_source(raw), url):
            continue

        ts = _first_ts(raw)
        if not ts:
            continue

        # drop beyond hard window
        if _age_hours(ts, now) > HARD_HOURS:
            continue

        is_crypto = _is_crypto(title, url)
        city = _detect_city(title)

        out.append(Item(
            title=title,
            url=url,
            ts=ts,
            source=_pick_source(raw),
            region_city=city,
            is_crypto=is_crypto
        ))

    # freshest first
    out.sort(key=lambda i: i.ts, reverse=True)
    return out

def choose_ticker(items: List[Item]) -> List[Item]:
    now = datetime.now(timezone.utc)
    chosen: List[Item] = []
    seen = set()

    def add_if_ok(it: Item) -> bool:
        key = _dedupe_key(it.title, it.url)
        if key in seen:
            return False
        seen.add(key)
        chosen.append(it)
        return True

    # 1) Up to TWO regional items, in city priority, using <=20h if possible
    soft_ok = [i for i in items if _age_hours(i.ts, now) <= SOFT_HOURS and not i.is_crypto]
    hard_ok = [i for i in items if not i.is_crypto]  # already <=30h due to load filter

    for city, _ in CITY_PRIORITY:
        if len(chosen) >= 2:
            break
        pool = [i for i in soft_ok if i.region_city == city] or [i for i in hard_ok if i.region_city == city]
        if pool:
            add_if_ok(pool[0])

    # 2) One crypto (prefer <=10h, then <=20h, then any <=30h)
    crypto10 = [i for i in items if i.is_crypto and _age_hours(i.ts, now) <= CRYPTO_PREF_HOURS]
    crypto20 = [i for i in items if i.is_crypto and _age_hours(i.ts, now) <= SOFT_HOURS]
    crypto30 = [i for i in items if i.is_crypto]  # already <=30h due to load filter

    for pool in (crypto10, crypto20, crypto30):
        if len(chosen) >= 3:
            break
        if pool:
            add_if_ok(pool[0])

    # 3) Backfill to 3 with freshest non-crypto items (<=20h preferred)
    if len(chosen) < MAX_ITEMS:
        backfill = soft_ok + [i for i in hard_ok if _dedupe_key(i.title, i.url) not in seen]
        for it in backfill:
            if len(chosen) >= MAX_ITEMS:
                break
            add_if_ok(it)

    return chosen[:MAX_ITEMS]

def to_wire(items: List[Item]) -> Dict[str, Any]:
    def iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "items": [
            {
                "title": it.title,
                "url": it.url,
                "ts": iso(it.ts),
                "region_city": it.region_city,
                "is_crypto": it.is_crypto,
            }
            for it in items
        ],
        "generated_at": iso(datetime.now(timezone.utc)),
    }

# ---- CLI ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Build 3-item ticker JSON for pill UI.")
    ap.add_argument("--in", dest="inp", default="./public/headlines.json", help="input enriched headlines JSON")
    ap.add_argument("--out", dest="out", default="./public/headline_ticker.json", help="output ticker JSON")
    args = ap.parse_args()

    in_path = Path(args.inp)
    out_path = Path(args.out)

    items = load_items(in_path)
    selected = choose_ticker(items)
    wire = to_wire(selected)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(wire, ensure_ascii=False, separators=(",", ":"), indent=2), encoding="utf-8")
    print(f"[headline_ticker] wrote {len(wire['items'])} items → {out_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
