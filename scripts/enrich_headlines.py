#!/usr/bin/env python3
# scripts/enrich_headlines.py
#
# Step 2: Enrich headlines and remove obvious duplicates.
#
# What this does:
#   1) Accepts either {"items":[...]} or a bare list [...]; outputs {"items":[...]}.
#   2) Ensures each item has:
#        - title, url (kept as-is if present)
#        - published_utc (ISO string, UTC)  ← created if only other timestamp fields exist
#        - source (falls back to domain if missing)
#        - canonical_url  : cleaned URL (https, no trackers, no mobile subdomain)
#        - canonical_id   : stable hash ID from canonical_url
#        - cluster_id     : stable hash ID from normalized title
#        - paywall        : bool (domain + source heuristic)
#        - opinion        : bool (title/path heuristic)
#        - is_aggregator  : bool (e.g., news.google.com / apple.news)
#        - trust_score    : float 0..1 (simple domain/source heuristic)
#   3) De-duplicates:
#        - exact duplicates by canonical_url
#        - near duplicates by cluster_id (e.g., Google News vs original source)
#      Tie-breaker (best wins): non-aggregator > not paywalled > Canada region >
#      higher trust_score > newer published_utc
#   4) Drops known aggregator hosts early (optional, tunable).
#   5) Sorts final items by published_utc DESC.
#
# Usage:
#   python3 scripts/enrich_headlines.py newsriver/raw_headlines.json --out newsriver/headlines.json
#   python3 scripts/enrich_headlines.py newsriver/headlines.json --inplace
#
# Safe to run repeatedly.

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, List
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# ---------------- settings you can tweak ----------------

TRACKING_PARAMS = {
    # common trackers
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "utm_name","utm_id","utm_reader","utm_cid",
    "fbclid","gclid","mc_cid","mc_eid","cmpid","s_kwcid","sscid",
    "ito","ref","smid","sref","partner","ICID","ns_campaign",
    "ns_mchannel","ns_source","ns_linkname","share_type","mbid"
}

# Very light paywall heuristics (expand over time)
PAYWALL_DOMAINS = {
    "ft.com","wsj.com","theglobeandmail.com","bloomberg.com","nytimes.com",
    "economist.com","latimes.com","thelogic.co","nationalpost.com","financialpost.com"
}

# Domains we treat as "aggregators" (prefer originals over these)
AGGREGATOR_DOMAINS = {
    "news.google.com","news.yahoo.com","news.msn.com","flipboard.com",
    "apple.news"
}

# Opinion markers
OPINION_TITLE_PAT = re.compile(r"\b(opinion|op\-?ed|analysis|commentary|column)\b", re.I)
OPINION_PATH_PAT  = re.compile(r"/(opinion|commentary|analysis|column)s?/", re.I)

# Title normalization before clustering
PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

# A tiny, editable trust map (host or keywords in `source`).
# Range 0..1; unknowns default to 0.5
TRUST_DEFAULT = 0.5
TRUST_MAP = {
    # strong
    "reuters.com": 0.95, "apnews.com": 0.92, "cbc.ca": 0.90, "bbc.com": 0.90,
    "theglobeandmail.com": 0.88, "ft.com": 0.90, "aljazeera.com": 0.85,
    "bnnbloomberg.ca": 0.82, "globeandmail": 0.88, "financialpost.com": 0.72,
    "globalnews.ca": 0.78, "mining.com": 0.75, "techmeme.com": 0.70,

    # lower
    "nationalpost.com": 0.60, "westernstandard.news": 0.45, "postmillennial": 0.35,

    # aggregators (very low)
    "news.google.com": 0.10, "news.yahoo.com": 0.10, "news.msn.com": 0.10,
    "flipboard.com": 0.10, "apple.news": 0.10,
}

# Whether to drop aggregator hosts entirely before dedupe (keeps UI cleaner)
DROP_AGGREGATORS_EARLY = True

# ---------------- helpers ----------------

def _as_list(root: Any) -> list[dict]:
    if isinstance(root, list):
        return root
    if isinstance(root, dict):
        for key in ("items", "articles", "data"):
            v = root.get(key)
            if isinstance(v, list):
                return v
    return []

def parse_when(value: Any) -> float:
    """Return a POSIX timestamp (seconds) or 0 if missing/bad."""
    if not value:
        return 0.0
    # try ISO + Z
    try:
        if isinstance(value, (int, float)):
            # assume seconds (if ms were passed they’ll be huge; still OK for ordering)
            return float(value)
        return datetime.fromisoformat(str(value).replace("Z","+00:00")).timestamp()
    except Exception:
        pass
    # try RFC 1123-ish
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(str(value), fmt).timestamp()
        except Exception:
            continue
    return 0.0

def ensure_published_utc(it: dict) -> str:
    """Compute a normalized ISO UTC string in published_utc, if missing."""
    # known aliases (loosely)
    for k in ("published_utc","published_at","published","updated_at","pubDate","date","time","timestamp"):
        v = it.get(k)
        ts = parse_when(v)
        if ts:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            iso = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            it["published_utc"] = iso
            return iso
    # fallback: now (last resort; avoids missing sort key)
    iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    it["published_utc"] = iso
    return iso

def normalize_title_for_cluster(title: str) -> str:
    if not title:
        return ""
    main = title.split(" - ")[0]
    main = main.lower()
    main = PUNCT_RE.sub(" ", main)
    main = re.sub(r"\s+", " ", main).strip()
    return main

def cluster_id_from_title(title: str) -> str:
    norm = normalize_title_for_cluster(title)
    h = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]
    return f"t:{h}"

def canonicalize_url(url: str) -> str:
    """Return a cleaned https URL with tracking/query junk removed and no mobile subdomain."""
    if not url:
        return ""
    try:
        u = urlparse(url)

        # Lowercase scheme/host; force https
        scheme = "https"
        netloc = (u.netloc or "").lower()

        # Strip leading mobile subdomains (conservative)
        if netloc.startswith("m.") and "." in netloc[2:]:
            netloc = netloc[2:]
        elif netloc.startswith("mobile.") and "." in netloc[7:]:
            netloc = netloc[7:]

        # Remove fragment; rebuild query without tracking params
        path = u.path or "/"
        query_pairs = [(k, v) for (k, v) in parse_qsl(u.query, keep_blank_values=True)
                       if k not in TRACKING_PARAMS]
        query = urlencode(query_pairs, doseq=True)

        # Trim trailing slash unless path is root
        if path != "/" and path.endswith("/"):
            path = path[:-1]

        cleaned = urlunparse((scheme, netloc, path, "", query, ""))
        return cleaned
    except Exception:
        return url  # fallback to original on parse errors

def canonical_id_from_url(url: str) -> str:
    base = canonicalize_url(url)
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]
    return f"u:{h}"

def domain_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""

def is_aggregator(url: str, source: str | None = None) -> bool:
    host = domain_of(url)
    if host in AGGREGATOR_DOMAINS:
        return True
    if source:
        s = str(source).lower()
        if any(k in s for k in ("google news", "yahoo news", "msn", "flipboard", "apple news")):
            return True
    return False

def looks_paywalled(url: str, source: str | None = None) -> bool:
    host = domain_of(url)
    if any(host.endswith(d) for d in PAYWALL_DOMAINS):
        return True
    if source:
        s = str(source).lower()
        if any(k in s for k in (
            "wall street journal","financial times","globe and mail",
            "bloomberg","new york times","economist","the logic",
            "national post","financial post")):
            return True
    return False

def looks_opinion(url: str, title: str | None = None) -> bool:
    try:
        if title and OPINION_TITLE_PAT.search(title):
            return True
        path = urlparse(url).path or ""
        if OPINION_PATH_PAT.search(path):
            return True
    except Exception:
        pass
    return False

def trust_for(host: str, source: str | None = None) -> float:
    if host in TRUST_MAP:
        return float(TRUST_MAP[host])
    if source:
        s = str(source).lower()
        for key, val in TRUST_MAP.items():
            if key in s:
                return float(val)
    return float(TRUST_DEFAULT)

def coerce_source(it: dict) -> str:
    """Ensure 'source' exists; if missing, use domain of URL."""
    s = it.get("source") or it.get("publisher") or it.get("domain")
    if isinstance(s, dict):
        s = s.get("name") or s.get("domain")
    s = (s or "").strip()
    if not s:
        host = domain_of(it.get("url",""))
        s = host or ""
    it["source"] = s
    return s

# ---------------- enrichment + dedupe ----------------

def enrich_item(it: dict) -> dict:
    url = it.get("url") or it.get("link") or it.get("href") or it.get("permalink") or ""
    if not url:
        return None  # unusable, skip later

    # normalize timestamps first (also sets published_utc)
    ensure_published_utc(it)

    title = (it.get("title") or it.get("headline") or it.get("name") or it.get("text") or "").strip()
    it["title"] = title

    coerce_source(it)

    can_url = canonicalize_url(url)
    can_id  = canonical_id_from_url(url)
    cl_id   = cluster_id_from_title(title)
    host    = domain_of(can_url)

    it = dict(it)  # shallow copy
    it["url"]            = url
    it["canonical_url"]  = can_url
    it["canonical_id"]   = can_id
    it["cluster_id"]     = cl_id
    it["paywall"]        = looks_paywalled(can_url, it.get("source"))
    it["opinion"]        = looks_opinion(can_url, title)
    it["is_aggregator"]  = is_aggregator(can_url, it.get("source"))
    it["trust_score"]    = trust_for(host, it.get("source"))
    return it

def rank_key(it: dict):
    """
    Sort key for picking the 'best' representative of a cluster.
    Lower is better.
    """
    agg_penalty   = 1 if it.get("is_aggregator") else 0
    pay_penalty   = 1 if it.get("paywall") else 0
    region_bonus  = 0 if (it.get("region") == "Canada") else 1
    trust         = float(it.get("trust_score") or 0.0)
    try:
        ts = datetime.fromisoformat(str(it.get("published_utc")).replace("Z","+00:00")).timestamp()
    except Exception:
        ts = 0.0
    # We want: non-aggregator, not-paywall, Canada, higher trust, newer time
    return (
        agg_penalty,
        pay_penalty,
        region_bonus,
        -trust,
        -ts,
    )

def dedupe(items: list[dict]) -> tuple[list[dict], dict]:
    """
    1) Drop exact duplicates by canonical_url (keep best rank_key).
    2) Within each cluster_id, keep the single best item by rank_key.
    Returns (deduped_items, debug_info)
    """
    debug = {"dedup_exact": 0, "dedup_cluster": 0, "clusters": 0}

    # 1) Exact URL dedupe
    by_url: dict[str, dict] = {}
    removed_exact = 0
    for it in items:
        cu = it.get("canonical_url") or it.get("url") or it.get("canonical_id")
        if not cu:
            continue
        prev = by_url.get(cu)
        if prev is None or rank_key(it) < rank_key(prev):
            by_url[cu] = it
        else:
            removed_exact += 1
    debug["dedup_exact"] = removed_exact

    # 2) Cluster dedupe
    clusters: dict[str, list[dict]] = {}
    for it in by_url.values():
        cid = it.get("cluster_id") or ""
        clusters.setdefault(cid, []).append(it)

    debug["clusters"] = len(clusters)

    final_items: list[dict] = []
    removed_cluster = 0
    for cid, group in clusters.items():
        if not group:
            continue
        group_sorted = sorted(group, key=rank_key)
        keep = group_sorted[0]
        final_items.append(keep)
        removed_cluster += max(0, len(group_sorted) - 1)
    debug["dedup_cluster"] = removed_cluster

    return final_items, debug

# ---------------- CLI ----------------

def main():
    ap = argparse.ArgumentParser(description="Enrich headlines with canonical IDs/flags and remove duplicates.")
    ap.add_argument("input", help="Path to raw or enriched headlines JSON")
    ap.add_argument("--out", help="Output path (default: print to stdout)")
    ap.add_argument("--inplace", action="store_true", help="Write back to the same file")
    ap.add_argument("--keep-aggregators", action="store_true",
                    help="Do not drop aggregator hosts early (default: drop)")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    items_in = _as_list(data)
    # Enrich
    enriched: list[dict] = []
    for raw in items_in:
        it = enrich_item(dict(raw))
        if not it:
            continue
        # Optional early drop of aggregators (still protected by dedupe rules later)
        if DROP_AGGREGATORS_EARLY and not args.keep_aggregators and it.get("is_aggregator"):
            continue
        enriched.append(it)

    # Dedupe
    deduped, dbg = dedupe(enriched)

    # Sort newest first (what the ticker expects to maximize freshness)
    def _ts(i: dict) -> float:
        try:
            return datetime.fromisoformat(str(i.get("published_utc")).replace("Z","+00:00")).timestamp()
        except Exception:
            return 0.0
    deduped.sort(key=_ts, reverse=True)

    # Reassemble output
    out = {"items": deduped, "count": len(deduped)}
    out.setdefault("generated_utc", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    # Merge/attach debug
    out["_debug"] = {
        **(data.get("_debug") or {}),
        "dedup_exact": dbg["dedup_exact"],
        "dedup_cluster": dbg["dedup_cluster"],
        "clusters": dbg["clusters"],
        "enricher": "step2a-v0.3",
        "dropped_aggregators_early": bool(DROP_AGGREGATORS_EARLY and not args.keep_aggregators),
    }

    # Write
    if args.inplace:
        with open(args.input, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    elif args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
