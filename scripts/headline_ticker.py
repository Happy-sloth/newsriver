#!/usr/bin/env python3
"""
headline_ticker.py — build a tiny 3-item ticker feed for the pill UI.

Inputs:
  --in   path to enriched headlines JSON (default: ./newsriver/headlines.json)
  --out  path to ticker JSON to write     (default: ./newsriver/dredge_heds.json)

Selection strategy (same as before):
  1) Filter to <=30h (hard cap). Prefer <=20h (soft window).
  2) Up to TWO regional items, priority: Toronto → Vancouver → Montreal → New York → London (UK).
     For each city, take the freshest non-crypto item.
  3) ONE crypto (BTC/ETH). Prefer <=10h, then <=20h, else <=30h.
  4) Backfill with freshest non-crypto to reach 3.

Output schema (matches front-end ticker):
{
  "items":[
    {"text":"…","url":"…","flags":{"is_breaking":false,"is_landmark":false,"has_bitcoin":false}}
  ],
  "generated_at":"2025-01-15T01:31:10Z"
}
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---- Config knobs ------------------------------------------------------------

CITY_PRIORITY: List[Tuple[str, re.Pattern]] = [
    ("Toronto",   re.compile(r"\btoronto\b|\bblue\s*jays\b|\bjays\b|\bmaple\s*leafs\b|\bleafs\b|\braptors\b|\bargos?\b|\btfc\b", re.I)),
    ("Vancouver", re.compile(r"\bvancouver\b|\bwhitecaps\b", re.I)),
    ("Montreal",  re.compile(r"\bmontreal\b|\bcanadiens\b|\bhabs\b|\balouettes\b", re.I)),
    ("New York",  re.compile(r"\bnew\s*york\b(?!\s*times)", re.I)),
    ("London",    re.compile(r"\blondon\b(?!,\s*ont|\s*ontario)", re.I)),  # prefer London, UK
]

CRYPTO_RE       = re.compile(r"\b(btc|bitcoin|eth|ethereum)\b", re.I)
CRYPTO_DOMAINS  = re.compile(r"(coindesk|cointelegraph|theblock|decrypt|blockworks|coinmarketcap)", re.I)
AGGREGATOR_RE   = re.compile(r"news\.google|news\.yahoo|apple\.news|bing\.com/news|msn\.com/en-", re.I)
BREAKING_HINTRE = re.compile(r"\b(breaking|developing|just in|alert)\b", re.I)

SOFT_HOURS        = 20
HARD_HOURS        = 30
CRYPTO_PREF_HOURS = 10
MAX_ITEMS         = 3

# ---- Types -------------------------------------------------------------------

@dataclass
class Item:
    title: str
    url: str
    ts: datetime
    source: str
    region_city: Optional[str]
    is_crypto: bool

# ---- Helpers -----------------------------------------------------------------

def _to_list(root: Any) -> List[Dict[str, Any]]:
    if isinstance(root, list): return root
    if isinstance(root, dict):
        for k in ("items", "articles", "data"):
            v = root.get(k)
            if isinstance(v, list): return v
    return []

def _pick_url(it: Dict[str, Any]) -> str:
    for k in ("canonical_url", "url", "link", "href", "permalink"):
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
    if isinstance(raw, (int, float)):
        sec = raw / 1000.0 if raw > 10_000_000_000 else raw
        return datetime.fromtimestamp(sec, tz=timezone.utc)
    if not raw: return None
    s = str(raw).strip()
    s = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def _first_ts(it: Dict[str, Any]) -> Optional[datetime]:
    for k in ("published_utc","published_at","published","updated_at","pubDate","date","time","timestamp"):
        dt = _parse_ts(it.get(k))
        if dt: return dt.astimezone(timezone.utc)
    return None

def _domain_from_url(url: str) -> str:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""

def _is_agg(source: str, url: str) -> bool:
    return bool(AGGREGATOR_RE.search(f"{source} {url}"))

def _detect_city(title: str) -> Optional[str]:
    for city, pat in CITY_PRIORITY:
        if pat.search(title):
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
        if not title or not url: continue
        if _is_agg(_pick_source(raw), url): continue

        ts = _first_ts(raw)
        if not ts: continue
        if _age_hours(ts, now) > HARD_HOURS: continue

        out.append(Item(
            title=title,
            url=url,
            ts=ts,
            source=_pick_source(raw),
            region_city=_detect_city(title),
            is_crypto=_is_crypto(title, url),
        ))

    out.sort(key=lambda i: i.ts, reverse=True)  # freshest first
    return out

def choose_ticker(items: List[Item]) -> List[Item]:
    now = datetime.now(timezone.utc)
    chosen: List[Item] = []
    seen = set()

    def add(it: Item) -> bool:
        key = _dedupe_key(it.title, it.url)
        if key in seen: return False
        seen.add(key)
        chosen.append(it)
        return True

    soft_noncrypto = [i for i in items if not i.is_crypto and _age_hours(i.ts, now) <= SOFT_HOURS]
    hard_noncrypto = [i for i in items if not i.is_crypto]

    # 1) Up to two regional (priority order)
    for city, _ in CITY_PRIORITY:
        if len(chosen) >= 2: break
        pool = [i for i in soft_noncrypto if i.region_city == city] or [i for i in hard_noncrypto if i.region_city == city]
        if pool: add(pool[0])

    # 2) One crypto, freshest within pref windows
    crypto10 = [i for i in items if i.is_crypto and _age_hours(i.ts, now) <= CRYPTO_PREF_HOURS]
    crypto20 = [i for i in items if i.is_crypto and _age_hours(i.ts, now) <= SOFT_HOURS]
    crypto30 = [i for i in items if i.is_crypto]
    for pool in (crypto10, crypto20, crypto30):
        if len(chosen) >= 3: break
        if pool: add(pool[0])

    # 3) Backfill to 3 with freshest non-crypto
    if len(chosen) < MAX_ITEMS:
        for it in soft_noncrypto + [i for i in hard_noncrypto if _dedupe_key(i.title, i.url) not in seen]:
            if len(chosen) >= MAX_ITEMS: break
            add(it)

    return chosen[:MAX_ITEMS]

def to_ticker_wire(items: List[Item]) -> Dict[str, Any]:
    def iso(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    wire_items = []
    for it in items:
        flags = {
            "has_bitcoin": bool(CRYPTO_RE.search(it.title)),
            "is_breaking": bool(BREAKING_HINTRE.search(it.title)),
            "is_landmark": bool(it.region_city in {"Toronto"} or re.search(r"\b(blue\s*jays|maple\s*leafs|raptors)\b", it.title, re.I)),
        }
        wire_items.append({"text": it.title, "url": it.url, "flags": flags})

    return {"items": wire_items, "generated_at": iso(datetime.now(timezone.utc))}

# ---- CLI ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Build 3-item ticker JSON for pill UI.")
    ap.add_argument("--in",  dest="inp", default="./newsriver/headlines.json",     help="input enriched headlines JSON")
    ap.add_argument("--out", dest="out", default="./newsriver/dredge_heds.json",  help="output ticker JSON (front-end reads this)")
    args = ap.parse_args()

    in_path  = Path(args.inp)
    out_path = Path(args.out)

    items  = load_items(in_path)
    picked = choose_ticker(items)
    wire   = to_ticker_wire(picked)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(wire, ensure_ascii=False, separators=(",", ":"), indent=2), encoding="utf-8")
    print(f"[headline_ticker] wrote {len(wire['items'])} items → {out_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
