#!/usr/bin/env python3
# scripts/fetch_headlines.py
#
# Build headlines.json from feeds.txt
# - Reads feeds.txt (grouped with "# --- Section ---" headers)
# - Fetches RSS/Atom feeds
# - Normalizes & de-duplicates:
#     * fuzzy title hash (collapses obvious dupes)
#     * near-duplicate similarity (Jaccard + containment on tokens)
#     * per-source cap (e.g., only 1 item per source)
# - Tags items with {category, region} inferred from section header
# - Sorts newest-first and writes headlines.json
#
# Requires: feedparser, requests

from __future__ import annotations
import argparse
import calendar
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import feedparser  # type: ignore
import requests    # type: ignore

# ---------------- Config ----------------
MAX_PER_FEED = 20          # cap per feed (keeps noise down)
MAX_TOTAL    = 300         # cap before de-dupe/sort (final will be smaller)
HTTP_TIMEOUT = 12          # seconds
USER_AGENT   = "NewsRiverBot/1.0 (+https://mypybite.github.io/newsriver/)"

# Strong duplicate control:
CAP_PER_SOURCE_DEFAULT = 1     # <= change to 2 later if you want more variety
CAP_PER_SOURCE_OVERRIDES = {
  # Example:
  # "cbc news": 3,
  # "reuters": 3,
}

# Near-duplicate thresholds (title tokens):
JACCARD_THRESHOLD     = 0.72   # share ~72% of unique tokens
CONTAINMENT_THRESHOLD = 0.88   # smaller set is 88% contained in the other

TRACKING_PARAMS = {
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "utm_name","utm_id","utm_reader","utm_cid","fbclid","gclid","mc_cid",
    "mc_eid","cmpid","s_kwcid","sscid","ito","ref","smid","sref","partner",
    "ICID","ns_campaign","ns_mchannel","ns_source","ns_linkname","share_type",
    "mbid"
}

AGGREGATOR_HINT = re.compile(r"(news\.google|news\.yahoo|apple\.news|feedproxy|flipboard)\b", re.I)

TITLE_STOPWORDS = {
  "the","a","an","and","or","but","of","for","with","without","in","on","at",
  "to","from","by","as","into","over","under","than","about","after","before",
  "due","will","still","just","not","is","are","was","were","be","being","been",
  "it","its","this","that","these","those","live","update","updates","breaking",
  "video","photos","report","reports","says","say","said","vs","vs.","game",
  "games","preview","recap","season","start","starts","starting","lineup",
  "dead","killed","kills","kill","dies","die","injured","injures","injury",
  "los","angeles","new","york","la"
}

PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

# ------------- helpers: category/region from section header -------------
@dataclass
class Tag:
    category: str
    region: str

def infer_tag(section_header: str) -> Tag:
    s = section_header.upper()
    if "TORONTO LOCAL" in s:                return Tag("Local", "Canada")
    if "BUSINESS" in s or "MARKET" in s or "CRYPTO" in s:  return Tag("Business", "World")
    if "MUSIC" in s or "CULTURE" in s:      return Tag("Culture", "World")
    if "YOUTH" in s or "POP" in s:          return Tag("Youth", "World")
    if "HOUSING" in s or "REAL ESTATE" in s:return Tag("Real Estate", "Canada")
    if "ENERGY" in s or "RESOURCES" in s:   return Tag("Energy", "Canada")
    if "TECH" in s:                         return Tag("Tech", "Canada")
    if "WEATHER" in s or "EMERGENCY" in s:  return Tag("Weather", "Canada")
    if "TRANSIT" in s or "CITY SERVICE" in s:return Tag("Transit", "Canada")
    if "COURTS" in s or "CRIME" in s or "PUBLIC SAFETY" in s: return Tag("Public Safety", "Canada")
    return Tag("General", "World")

# ---------------- URL canonicalization & keys ----------------
def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        u = urlparse(url)
        scheme = "https" if u.scheme else "https"
        netloc = (u.netloc or "").lower()
        if netloc.startswith("m.") and "." in netloc[2:]:
            netloc = netloc[2:]
        elif netloc.startswith("mobile.") and "." in netloc[7:]:
            netloc = netloc[7:]
        path = u.path or "/"
        query_pairs = [(k, v) for (k, v) in parse_qsl(u.query, keep_blank_values=True)
                       if k not in TRACKING_PARAMS]
        query = urlencode(query_pairs, doseq=True)
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return url

def canonical_id(url: str) -> str:
    base = canonicalize_url(url)
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]
    return f"u:{h}"

def strip_source_tail(title: str) -> str:
    return (title or "").replace("\u2013", "-").replace("\u2014", "-").split(" | ")[0].split(" - ")[0]

def fuzzy_title_key(title: str) -> str:
    base = strip_source_tail(title).lower()
    base = PUNCT_RE.sub(" ", base)
    toks = [t for t in base.split() if len(t) > 1 and t not in TITLE_STOPWORDS]
    if not toks:
        toks = base.split()
    sig = "|".join(sorted(set(toks))[:10])
    h = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12]
    return f"t:{h}"

def looks_aggregator(source: str, link: str) -> bool:
    return bool(AGGREGATOR_HINT.search(f"{source} {link}"))

def host_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""

# ---------------- feeds.txt parsing ----------------
@dataclass
class FeedSpec:
    url: str
    tag: Tag

def parse_feeds_txt(path: str) -> List[FeedSpec]:
    feeds: List[FeedSpec] = []
    current_tag = Tag("General", "World")
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                header = re.sub(r"^#\s*-+\s*|\s*-+\s*$", "", line.lstrip("#")).strip()
                current_tag = infer_tag(header or "General")
                continue
            feeds.append(FeedSpec(url=line, tag=current_tag))
    return feeds

# ---------------- HTTP + time helpers ----------------
def http_get(url: str) -> bytes | None:
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        if resp.ok:
            return resp.content
    except Exception:
        return None
    return None

def to_iso_from_struct(t) -> str | None:
    try:
        epoch = calendar.timegm(t)  # treat as UTC
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except Exception:
        return None

def pick_published(entry) -> str | None:
    for key in ("published_parsed","updated_parsed","created_parsed"):
        v = getattr(entry, key, None)
        if v:
            iso = to_iso_from_struct(v)
            if iso: return iso
    for key in ("published","updated","created","date","issued"):
        if entry.get(key):
            # fall back to "now" if only a raw string is present
            return datetime.now(timezone.utc).isoformat()
    return None

def _ts(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso.replace("Z","+00:00")).timestamp())
    except Exception:
        return 0

# ---------------- similarity dedupe ----------------
def title_tokens(s: str) -> set[str]:
    s = strip_source_tail(s or "").lower()
    s = PUNCT_RE.sub(" ", s)
    toks = [t for t in s.split() if len(t) > 1 and t not in TITLE_STOPWORDS]
    return set(toks) if toks else set(s.split())

def is_near_duplicate(a_title: str, b_title: str) -> bool:
    A = title_tokens(a_title)
    B = title_tokens(b_title)
    if not A or not B:
        return False
    inter = len(A & B)
    uni   = len(A | B)
    j = inter / uni if uni else 0.0
    c = inter / min(len(A), len(B))
    return j >= JACCARD_THRESHOLD or c >= CONTAINMENT_THRESHOLD

def sort_preference_key(item: dict) -> tuple:
    # Prefer originals over aggregators; then newest first
    agg = 1 if looks_aggregator(item.get("source",""), item.get("url","")) else 0
    return (agg, -_ts(item.get("published_utc","0")))

def prune_near_duplicates(items: list[dict]) -> list[dict]:
    # Sort by preference so the first kept is the best candidate
    items_sorted = sorted(items, key=sort_preference_key)
    kept: list[dict] = []
    for it in items_sorted:
        dup = False
        for k in kept:
            if is_near_duplicate(it.get("title",""), k.get("title","")):
                dup = True
                break
        if not dup:
            kept.append(it)
    return kept

# ---------------- per-source cap ----------------
def apply_per_source_cap(items: list[dict]) -> list[dict]:
    out = []
    counts: dict[str,int] = {}
    for it in items:
        src_norm = (it.get("source","") or "").strip().lower()
        cap = CAP_PER_SOURCE_OVERRIDES.get(src_norm, CAP_PER_SOURCE_DEFAULT)
        n = counts.get(src_norm, 0)
        if n >= cap:
            continue
        counts[src_norm] = n + 1
        out.append(it)
    return out

# ---------------- main build ----------------
def build(feeds_file: str, out_path: str) -> dict:
    specs = parse_feeds_txt(feeds_file)
    collected: list[dict] = []

    for spec in specs:
        blob = http_get(spec.url)
        if not blob:
            continue
        parsed = feedparser.parse(blob)
        feed_title = (parsed.feed.get("title") or host_of(spec.url) or "").strip()
        entries = parsed.entries[:MAX_PER_FEED]
        for e in entries:
            title = (e.get("title") or "").strip()
            link  = (e.get("link") or "").strip()
            if not title or not link:
                continue
            can_url = canonicalize_url(link)
            item = {
                "title": title,
                "url":   can_url or link,
                "source": feed_title or host_of(link),
                "published_utc": pick_published(e) or datetime.now(timezone.utc).isoformat(),
                "category": spec.tag.category,
                "region":   spec.tag.region,
                "canonical_url": can_url or link,
                "canonical_id":  canonical_id(can_url or link),
                "cluster_id":    fuzzy_title_key(title),
            }
            collected.append(item)
            if len(collected) >= MAX_TOTAL:
                break
        if len(collected) >= MAX_TOTAL:
            break

    # 1) Collapse by fuzzy title key (keep newest; prefer non-aggregator on ties)
    keyed: dict[str,dict] = {}
    for it in collected:
        key = it["cluster_id"]
        prev = keyed.get(key)
        if not prev:
            keyed[key] = it
            continue
        t_new = _ts(it["published_utc"])
        t_old = _ts(prev["published_utc"])
        if t_new > t_old:
            keyed[key] = it
        elif t_new == t_old:
            if looks_aggregator(prev.get("source",""), prev.get("url","")) and not looks_aggregator(it.get("source",""), it.get("url","")):
                keyed[key] = it

    items = list(keyed.values())

    # 2) Extra pass: near-duplicate similarity across remaining titles
    items = prune_near_duplicates(items)

    # 3) Enforce per-source cap (e.g., only 1 per source)
    items = apply_per_source_cap(items)

    # 4) Sort newest-first for the river
    items.sort(key=lambda x: _ts(x["published_utc"]), reverse=True)

    out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": items,
        "_debug": {
            "feeds_total": len(specs),
            "cap_items": MAX_TOTAL,
            "cap_per_source_default": CAP_PER_SOURCE_DEFAULT,
            "jaccard_threshold": JACCARD_THRESHOLD,
            "containment_threshold": CONTAINMENT_THRESHOLD,
            "version": "fetch-v1.2-dedupe++"
        }
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return out

def main():
    ap = argparse.ArgumentParser(description="Build headlines.json from feeds.txt")
    ap.add_argument("--feeds-file", default="feeds.txt", help="Path to feeds.txt")
    ap.add_argument("--out", default="headlines.json", help="Output JSON file")
    args = ap.parse_args()
    out = build(args.feeds_file, args.out)
    print(f"Wrote {args.out} with {out['count']} items at {out['generated_utc']}")

if __name__ == "__main__":
    main()
