#!/usr/bin/env python3
# scripts/fetch_headlines.py
#
# Build headlines.json from feeds.txt
# - Reads your feeds.txt (grouped with "# --- Section ---" headers)
# - Fetches RSS/Atom feeds
# - Normalizes & de-duplicates items (title-based fuzzy key + canonical URL)
# - Tags items with a coarse {category, region} inferred from the section
# - Sorts newest-first and writes headlines.json
#
# Requires: feedparser, requests
# (The GitHub Actions workflow we add next will install these automatically.)

from __future__ import annotations
import argparse
import hashlib
import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import feedparser  # type: ignore
import requests    # type: ignore


# ---------------- Config ----------------
MAX_PER_FEED = 20         # cap per feed (keeps noise down)
MAX_TOTAL    = 300        # overall cap before dedupe/sort (final file will likely be smaller)

# Strict connect/read timeouts so a single slow feed can't stall the job
TIMEOUT      = (5, 8)     # (connect, read) seconds
HEADERS      = {"User-Agent": "NewsRiverBot/1.0 (+https://mypybite.github.io/newsriver/)"}

TRACKING_PARAMS = {
    # common trackers
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "utm_name","utm_id","utm_reader","utm_cid",
    "fbclid","gclid","mc_cid","mc_eid","cmpid","s_kwcid","sscid",
    "ito","ref","smid","sref","partner","ICID","ns_campaign",
    "ns_mchannel","ns_source","ns_linkname","share_type","mbid"
}

AGGREGATOR_HINT = re.compile(r"(news\.google|news\.yahoo|apple\.news|feedproxy|flipboard)\b", re.I)

TITLE_STOPWORDS = {
  # glue words
  "the","a","an","and","or","but","of","for","with","without","in","on","at","to","from","by","as","into","over","under","than","about",
  "after","before","due","will","still","just","not","is","are","was","were","be","being","been","it","its","this","that","these","those",
  # newsy fluff
  "live","update","updates","breaking","video","photos","report","reports","says","say","said",
  # sports glue
  "vs","vs.","game","games","preview","recap","season","start","starts","starting","lineup",
  # casualty words (to avoid splitting by wording choice)
  "dead","killed","kills","kill","dies","die","injured","injures","injury",
  # frequent city tokens
  "los","angeles","new","york","la"
}


# ------------- helpers: category/region from section header -------------
@dataclass
class Tag:
    category: str
    region: str

def infer_tag(section_header: str) -> Tag:
    s = section_header.upper()
    if "TORONTO LOCAL" in s:               return Tag("Local", "Canada")
    if "BUSINESS" in s or "MARKET" in s or "CRYPTO" in s:
                                            return Tag("Business", "World")
    if "MUSIC" in s or "CULTURE" in s:     return Tag("Culture", "World")
    if "YOUTH" in s or "POP" in s:         return Tag("Youth", "World")
    if "HOUSING" in s or "REAL ESTATE" in s:return Tag("Real Estate", "Canada")
    if "ENERGY" in s or "RESOURCES" in s:  return Tag("Energy", "Canada")
    if "TECH" in s:                         return Tag("Tech", "Canada")
    if "WEATHER" in s or "EMERGENCY" in s:  return Tag("Weather", "Canada")
    if "TRANSIT" in s or "CITY SERVICE" in s:
                                            return Tag("Transit", "Canada")
    if "COURTS" in s or "CRIME" in s or "PUBLIC SAFETY" in s:
                                            return Tag("Public Safety", "Canada")
    return Tag("General", "World")


# ---------------- URL canonicalization & keys ----------------
def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        u = urlparse(url)
        scheme = "https" if u.scheme else "https"
        netloc = (u.netloc or "").lower()
        # strip common mobile subdomains
        if netloc.startswith("m.") and "." in netloc[2:]:
            netloc = netloc[2:]
        elif netloc.startswith("mobile.") and "." in netloc[7:]:
            netloc = netloc[7:]
        # optionally trim www. for stability
        if netloc.startswith("www.") and len(netloc) > 4:
            netloc = netloc[4:]
        path = u.path or "/"
        # drop fragment; rebuild query without tracking params
        query_pairs = [(k, v) for (k, v) in parse_qsl(u.query, keep_blank_values=True)
                       if k not in TRACKING_PARAMS]
        query = urlencode(query_pairs, doseq=True)
        # trim trailing slash (except root)
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return url

def canonical_id(url: str) -> str:
    base = canonicalize_url(url)
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]
    return f"u:{h}"

PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

def strip_source_tail(title: str) -> str:
    # drop " | Source" or " - Source" tail bits
    t = (title or "").replace("\u2013", "-").replace("\u2014", "-")
    t = t.split(" | ")[0]
    t = t.split(" - ")[0]
    return t

def fuzzy_title_key(title: str) -> str:
    base = strip_source_tail(title).lower()
    base = PUNCT_RE.sub(" ", base)
    toks = [t for t in base.split() if len(t) > 1 and t not in TITLE_STOPWORDS]
    if not toks:
        toks = base.split()
    uniq = sorted(set(toks))
    sig = "|".join(uniq[:10])
    h = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12]
    return f"t:{h}"

def looks_aggregator(source: str, link: str) -> bool:
    blob = f"{source} {link}"
    return bool(AGGREGATOR_HINT.search(blob))


# ---------------- feeds.txt parsing ----------------
@dataclass
class FeedSpec:
    url: str
    tag: Tag

def parse_feeds_txt(path: str) -> List[FeedSpec]:
    feeds: List[FeedSpec] = []
    current_tag = Tag("General", "World")
    current_header = "General"
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                # update section-derived tag
                m = re.match(r"#\s*-+\s*(.*?)\s*-+\s*", line)
                header = m.group(1) if m else line.lstrip("#").strip()
                current_header = header
                current_tag = infer_tag(header)
                continue
            # it's a URL
            feeds.append(FeedSpec(url=line, tag=current_tag))
    return feeds


# ---------------- fetch & transform ----------------
def http_get(url: str) -> bytes | None:
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
        if resp.ok:
            return resp.content
    except Exception:
        return None
    return None

def to_iso_from_struct(t) -> str | None:
    try:
        # treat struct_time as UTC (common in RSS)
        import calendar
        epoch = calendar.timegm(t)
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except Exception:
        return None

def pick_published(entry) -> str | None:
    for key in ("published_parsed","updated_parsed","created_parsed"):
        if getattr(entry, key, None):
            iso = to_iso_from_struct(getattr(entry, key))
            if iso:
                return iso
    # fallback to text values (as last resort, use "now" so we don't drop the item)
    for key in ("published","updated","created","date","issued"):
        val = entry.get(key)
        if val:
            try:
                return datetime.now(timezone.utc).isoformat()
            except Exception:
                pass
    return None

def host_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


# ---------------- main build ----------------
def build(feeds_file: str, out_path: str) -> dict:
    specs = parse_feeds_txt(feeds_file)
    collected = []

    print(f"Found {len(specs)} feeds in {feeds_file}", flush=True)

    for i, spec in enumerate(specs, start=1):
        host = host_of(spec.url) or spec.url
        print(f"→ [{i}/{len(specs)}] Fetch {host}", flush=True)
        try:
            blob = http_get(spec.url)
            if not blob:
                print(f"  ✗ {host}: no response (timeout or HTTP error)", flush=True)
                continue

            parsed = feedparser.parse(blob)
            feed_title = (parsed.feed.get("title") or host).strip()
            entries = parsed.entries[:MAX_PER_FEED]

            added_here = 0
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
                    # enrich for client-side dedupe
                    "canonical_url": can_url or link,
                    "canonical_id":  canonical_id(can_url or link),
                    "cluster_id":    fuzzy_title_key(title),
                }
                collected.append(item)
                added_here += 1
                if len(collected) >= MAX_TOTAL:
                    break

            print(f"  ✓ {host}: {added_here} items", flush=True)
            if len(collected) >= MAX_TOTAL:
                print("Reached MAX_TOTAL, stopping early.", flush=True)
                break

        except Exception as ex:
            print(f"  ✗ {host}: {ex}", flush=True)
            continue

    # De-dupe newest-per-key; tie-break: prefer non-aggregator
    by_key = {}
    for it in collected:
        key = it["cluster_id"]  # fuzzy title hash
        prev = by_key.get(key)
        if not prev:
            by_key[key] = it
            continue
        t_new = _ts(it["published_utc"])
        t_old = _ts(prev["published_utc"])
        if t_new > t_old:
            by_key[key] = it
        elif t_new == t_old:
            if looks_aggregator(prev.get("source",""), prev.get("url","")) and not looks_aggregator(it.get("source",""), it.get("url","")):
                by_key[key] = it

    items = list(by_key.values())

    # Sort newest-first
    items.sort(key=lambda x: _ts(x["published_utc"]), reverse=True)

    out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": items,
        "_debug": {
            "feeds_total": len(specs),
            "cap_items": MAX_TOTAL,
            "version": "fetch-v1.1-timeouts-logs"
        }
    }
    # write
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"Wrote {out_path} with {out['count']} items at {out['generated_utc']}", flush=True)
    return out

def _ts(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso.replace("Z","+00:00")).timestamp())
    except Exception:
        return 0

def main():
    ap = argparse.ArgumentParser(description="Build headlines.json from feeds.txt")
    ap.add_argument("--feeds-file", default="feeds.txt", help="Path to feeds.txt")
    ap.add_argument("--out", default="headlines.json", help="Output JSON file")
    args = ap.parse_args()
    out = build(args.feeds_file, args.out)

if __name__ == "__main__":
    main()
