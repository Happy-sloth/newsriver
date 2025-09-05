#!/usr/bin/env python3
# scripts/fetch_headlines.py
#
# Build headlines.json from feeds.txt (+ optional feeds_local.txt site seeds)
# - Reads feeds.txt (grouped with "# --- Section ---" headers)
# - Optionally reads feeds_local.txt (one site URL per line) and
#   auto-discovers RSS/Atom feeds from those sites
# - Fetches RSS/Atom feeds
# - Normalizes & aggressively de-duplicates:
#     1) fuzzy title hash
#     2) near-duplicate pass (Jaccard on title tokens)
# - Demotes aggregators/press wires; small per-domain caps
# - Tags items with {category, region} inferred from section header
#   (locals discovered from feeds_local.txt are tagged Local/Canada)
# - Sorts newest-first and writes headlines.json
#
# Requires: feedparser, requests

from __future__ import annotations
import argparse
import calendar
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, urljoin

import feedparser  # type: ignore
import requests    # type: ignore


# ---------------- Tunables ----------------
MAX_PER_FEED = 14          # cap per feed before global caps
MAX_TOTAL    = 320         # overall cap before de-dupe/sort
HTTP_TIMEOUT = 12
USER_AGENT   = "NewsRiverBot/1.2 (+https://mypybite.github.io/newsriver/)"

# Per-host caps to prevent any one domain flooding the river
PER_HOST_MAX = {
    "toronto.citynews.ca": 8,
    "financialpost.com": 6,  # guard future adds
}

# Prefer these domains when breaking ties (primary/original/regulators)
PREFERRED_DOMAINS = {
    "cbc.ca","globalnews.ca","ctvnews.ca","blogto.com","toronto.citynews.ca",
    "nhl.com","mlbtraderumors.com",
    "bankofcanada.ca","federalreserve.gov","bls.gov","statcan.gc.ca",
    "sec.gov","cftc.gov","marketwatch.com",
    "coindesk.com","cointelegraph.com",
}

# Press-wire domains & path hints (often duplicate across outlets)
PRESS_WIRE_DOMAINS = {
    "globenewswire.com","newswire.ca","prnewswire.com","businesswire.com","accesswire.com"
}
PRESS_WIRE_PATH_HINTS = ("/globe-newswire", "/globenewswire", "/business-wire", "/newswire/")

# Tracking params to strip
TRACKING_PARAMS = {
    # common trackers
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "utm_name","utm_id","utm_reader","utm_cid",
    "fbclid","gclid","mc_cid","mc_eid","cmpid","s_kwcid","sscid",
    "ito","ref","smid","sref","partner","ICID","ns_campaign",
    "ns_mchannel","ns_source","ns_linkname","share_type","mbid",
    # misc
    "oc","ved","ei","spm","rb_clickid","igsh","feature","source"
}

AGGREGATOR_HINT = re.compile(r"(news\.google|news\.yahoo|apple\.news|feedproxy|flipboard)\b", re.I)

# stopwords for title-token signatures
TITLE_STOPWORDS = {
    # glue
    "the","a","an","and","or","but","of","for","with","without","in","on","at",
    "to","from","by","as","into","over","under","than","about","after","before",
    "due","will","still","just","not","is","are","was","were","be","being","been",
    "it","its","this","that","these","those",
    # newsy fluff
    "live","update","updates","breaking","video","photos","report","reports","says","say","said",
    # sports glue
    "vs","vs.","game","games","preview","recap","season","start","starts","starting","lineup",
    # casualty words (avoid splitting by wording choice)
    "dead","killed","kills","kill","dies","die","injured","injures","injury",
    # frequent city tokens
    "los","angeles","new","york","la"
}
PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

# --- Local feed discovery (for feeds_local.txt) ---
FEEDS_LOCAL_DEFAULT = "feeds_local.txt"
FEED_DISCOVERY_MAX_PER_SITE = 4  # be polite
FEED_DISCOVERY_PATHS = [
    "/feed/", "/rss", "/rss/", "/rss.xml", "/feed.xml", "/feeds",
    "/category/news/feed/", "/news/feed/",
    "/en/news/rss.aspx", "/rss.aspx", "/news/rss.aspx", "/city-hall/rss.aspx",
    "/Modules/News/rss", "/Modules/News/Feed.aspx", "/modules/news/rss",
]


# ---------------- Section → tag ----------------
@dataclass
class Tag:
    category: str
    region: str

def infer_tag(section_header: str) -> Tag:
    s = section_header.upper()
    if "TORONTO LOCAL" in s:                 return Tag("Local", "Canada")
    if "BUSINESS" in s or "MARKET" in s or "CRYPTO" in s:
                                             return Tag("Business", "World")
    if "MUSIC" in s or "CULTURE" in s:       return Tag("Culture", "World")
    if "YOUTH" in s or "POP" in s:           return Tag("Youth", "World")
    if "HOUSING" in s or "REAL ESTATE" in s: return Tag("Real Estate", "Canada")
    if "ENERGY" in s or "RESOURCES" in s:    return Tag("Energy", "Canada")
    if "TECH" in s:                          return Tag("Tech", "Canada")
    if "WEATHER" in s or "EMERGENCY" in s:   return Tag("Weather", "Canada")
    if "TRANSIT" in s or "CITY SERVICE" in s:return Tag("Transit", "Canada")
    if "COURTS" in s or "CRIME" in s or "PUBLIC SAFETY" in s:
                                             return Tag("Public Safety", "Canada")
    return Tag("General", "World")


# ---------------- URL & identity ----------------
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

def host_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


# ---------------- Title signatures ----------------
def strip_source_tail(title: str) -> str:
    # drop ' - Site' / ' | Site' suffixes; normalize mdash to hyphen
    return (title or "").replace("\u2013", "-").replace("\u2014", "-").split(" | ")[0].split(" - ")[0]

def title_tokens(title: str) -> list[str]:
    base = strip_source_tail(title).lower()
    base = PUNCT_RE.sub(" ", base)
    toks = [t for t in base.split() if len(t) > 1 and t not in TITLE_STOPWORDS]
    return toks or base.split()

def fuzzy_title_key(title: str) -> str:
    toks = title_tokens(title)
    uniq = sorted(set(toks))
    sig = "|".join(uniq[:10])
    h = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:12]
    return f"t:{h}"

def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    union = len(a | b)
    return inter / union


# ---------------- Aggregator / wire heuristics ----------------
def is_press_wire(url: str) -> bool:
    h = host_of(url)
    if h in PRESS_WIRE_DOMAINS:
        return True
    p = urlparse(url).path or ""
    return any(hint in p for hint in PRESS_WIRE_PATH_HINTS)

def looks_aggregator(source: str, link: str) -> bool:
    blob = f"{source} {link}"
    if AGGREGATOR_HINT.search(blob):
        return True
    if is_press_wire(link):
        return True
    return False


# ---------------- feeds.txt parsing ----------------
@dataclass
class FeedSpec:
    url: str
    tag: Tag

def parse_feeds_txt(path: str) -> list[FeedSpec]:
    feeds: list[FeedSpec] = []
    current_tag = Tag("General", "World")
    if not os.path.exists(path):
        return feeds
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                header = re.sub(r"^#\s*-*\s*(.*?)\s*-*\s*$", r"\1", line)
                current_tag = infer_tag(header)
                continue
            feeds.append(FeedSpec(url=line, tag=current_tag))
    return feeds


# ---------------- Local site seeds (feeds_local.txt) ----------------
def parse_local_sites(path: str) -> list[str]:
    """Return list of site/homepage URLs from feeds_local.txt (ignore comments/blank lines)."""
    sites: list[str] = []
    if not os.path.exists(path):
        return sites
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            sites.append(line)
    return sites

LINK_TAG_RE = re.compile(r"<link\b[^>]*?>", re.I)
HREF_RE     = re.compile(r'href=["\']([^"\']+)["\']', re.I)

def _discover_via_html(html: str, base: str) -> list[str]:
    """Find <link rel=alternate type=rss/atom ... href=...> from HTML."""
    out: list[str] = []
    for m in LINK_TAG_RE.finditer(html):
        tag = m.group(0)
        low = tag.lower()
        if "rel=" not in low or "alternate" not in low:
            continue
        if ("rss" not in low) and ("atom" not in low) and ("xml" not in low):
            continue
        hrefm = HREF_RE.search(tag)
        if not hrefm:
            continue
        href = hrefm.group(1)
        out.append(urljoin(base, href))
    return out

def _looks_like_feed_bytes(b: bytes) -> bool:
    try:
        parsed = feedparser.parse(b)
        # treat as feed if we have at least one entry OR a feed title
        return bool(parsed.entries) or bool(parsed.feed and parsed.feed.get("title"))
    except Exception:
        return False

def discover_feed_urls(site_url: str) -> list[str]:
    """Given a site URL, try to discover RSS/Atom feed URLs."""
    found: list[str] = []
    seen: set[str] = set()

    # 1) Parse HTML for <link rel="alternate" ...>
    html_bytes = http_get(site_url)
    if html_bytes:
        html = ""
        try:
            html = html_bytes.decode("utf-8", "ignore")
        except Exception:
            html = ""
        for u in _discover_via_html(html, site_url):
            cu = canonicalize_url(u)
            if cu not in seen:
                seen.add(cu)
                found.append(cu)
            if len(found) >= FEED_DISCOVERY_MAX_PER_SITE:
                return found

    # 2) Try common feed paths
    for suffix in FEED_DISCOVERY_PATHS:
        cand = canonicalize_url(urljoin(site_url, suffix))
        if cand in seen:
            continue
        blob = http_get(cand)
        if not blob:
            continue
        if _looks_like_feed_bytes(blob):
            seen.add(cand)
            found.append(cand)
            if len(found) >= FEED_DISCOVERY_MAX_PER_SITE:
                break

    return found


# ---------------- HTTP & date helpers ----------------
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
        if getattr(entry, key, None):
            iso = to_iso_from_struct(getattr(entry, key))
            if iso:
                return iso
    for key in ("published","updated","created","date","issued"):
        val = entry.get(key)
        if val:
            # fall back to "now" (feeds lacking parsed dates)
            return datetime.now(timezone.utc).isoformat()
    return None

def _ts(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso.replace("Z","+00:00")).timestamp())
    except Exception:
        return 0


# ---------------- Build ----------------
def build(feeds_file: str, out_path: str, feeds_local_file: str | None = FEEDS_LOCAL_DEFAULT) -> dict:
    # 1) sectioned feeds
    specs = parse_feeds_txt(feeds_file)

    # 2) local seeds → discover feed URLs → tag Local/Canada
    local_sites = parse_local_sites(feeds_local_file) if feeds_local_file else []
    discovered: list[str] = []
    for site in local_sites:
        try:
            for f in discover_feed_urls(site):
                discovered.append(f)
        except Exception:
            continue

    # de-dupe discovered + existing
    known_urls = {canonicalize_url(s.url) for s in specs}
    new_specs: list[FeedSpec] = []
    for u in discovered:
        cu = canonicalize_url(u)
        if cu and cu not in known_urls:
            new_specs.append(FeedSpec(url=cu, tag=Tag("Local", "Canada")))
            known_urls.add(cu)

    # combine
    specs.extend(new_specs)

    collected: list[dict] = []
    per_host_counts: dict[str,int] = {}

    for spec in specs:
        blob = http_get(spec.url)
        if not blob:
            continue
        parsed = feedparser.parse(blob)
        entries = parsed.entries[:MAX_PER_FEED]

        for e in entries:
            title = (e.get("title") or "").strip()
            link  = (e.get("link") or "").strip()
            if not title or not link:
                continue

            can_url = canonicalize_url(link)
            h = host_of(can_url or link)
            # per-host cap
            cap = PER_HOST_MAX.get(h, MAX_PER_FEED)
            if per_host_counts.get(h, 0) >= cap:
                continue

            item = {
                "title": title,
                "url":   can_url or link,
                "source": (parsed.feed.get("title") or h or "").strip(),
                "published_utc": pick_published(e) or datetime.now(timezone.utc).isoformat(),
                "category": spec.tag.category,
                "region":   spec.tag.region,
                # enrich
                "canonical_url": can_url or link,
                "canonical_id":  canonical_id(can_url or link),
                "cluster_id":    fuzzy_title_key(title),
            }
            collected.append(item)
            per_host_counts[h] = per_host_counts.get(h, 0) + 1

            if len(collected) >= MAX_TOTAL:
                break
        if len(collected) >= MAX_TOTAL:
            break

    # Pass 1: collapse exact fuzzy clusters (keep newest; prefer non-aggregator)
    first_pass: dict[str,dict] = {}
    for it in collected:
        key = it["cluster_id"]
        prev = first_pass.get(key)
        if not prev:
            first_pass[key] = it
            continue
        t_new, t_old = _ts(it["published_utc"]), _ts(prev["published_utc"])
        if t_new > t_old:
            first_pass[key] = it
        elif t_new == t_old:
            if looks_aggregator(prev.get("source",""), prev.get("url","")) and not looks_aggregator(it.get("source",""), it.get("url","")):
                first_pass[key] = it

    items = list(first_pass.values())

    # Pass 2: near-duplicate collapse using Jaccard on title tokens
    survivors: list[dict] = []
    token_cache: list[Tuple[set[str], dict]] = []
    THRESH = 0.82

    def is_better(a: dict, b: dict) -> bool:
        """Return True if a is preferred over b."""
        ta, tb = _ts(a["published_utc"]), _ts(b["published_utc"])
        if ta != tb:
            return ta > tb
        a_aggr = looks_aggregator(a.get("source",""), a.get("url",""))
        b_aggr = looks_aggregator(b.get("source",""), b.get("url",""))
        if a_aggr != b_aggr:
            return not a_aggr
        ha, hb = host_of(a["url"]), host_of(b["url"])
        if (ha in PREFERRED_DOMAINS) != (hb in PREFERRED_DOMAINS):
            return ha in PREFERRED_DOMAINS
        # last resort: shorter URL path wins
        return len(a["url"]) < len(b["url"])

    for it in items:
        toks = set(title_tokens(it["title"]))
        merged = False
        for toks_other, rep in token_cache:
            if jaccard(toks, toks_other) >= THRESH:
                # same story cluster → keep the better one
                if is_better(it, rep):
                    survivors.remove(rep)
                    survivors.append(it)
                    token_cache.remove((toks_other, rep))
                    token_cache.append((toks, it))
                merged = True
                break
        if not merged:
            survivors.append(it)
            token_cache.append((toks, it))

    # Final sort newest-first
    survivors.sort(key=lambda x: _ts(x["published_utc"]), reverse=True)

    out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(survivors),
        "items": survivors,
        "_debug": {
            "feeds_total_from_file": len(parse_feeds_txt(feeds_file)),
            "local_seeds": len(local_sites),
            "local_feeds_discovered": len(discovered),
            "feeds_total_all": len(specs),
            "cap_items": MAX_TOTAL,
            "collected": len(collected),
            "dedup_pass1": len(items),
            "dedup_final": len(survivors),
            "version": "fetch-v1.3-local-discovery"
        }
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return out


def main():
    ap = argparse.ArgumentParser(description="Build headlines.json from feeds.txt (+ optional feeds_local.txt)")
    ap.add_argument("--feeds-file", default="feeds.txt", help="Path to feeds.txt")
    ap.add_argument("--feeds-local-file", default=FEEDS_LOCAL_DEFAULT, help="Optional path to feeds_local.txt (site seeds)")
    ap.add_argument("--out", default="headlines.json", help="Output JSON file")
    args = ap.parse_args()
    out = build(args.feeds_file, args.out, args.feeds_local_file)
    print(f"Wrote {args.out} with {out['count']} items at {out['generated_utc']}")
    dbg = out.get("_debug", {})
    print("Debug:", dbg)

if __name__ == "__main__":
    main()
