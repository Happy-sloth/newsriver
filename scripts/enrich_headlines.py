#!/usr/bin/env python3
# scripts/enrich_headlines.py
#
# Step 2: enrich headlines.json with canonical_url, canonical_id, cluster_id,
#         paywall, and opinion flags. Safe to run repeatedly.
#
# Usage:
#   python scripts/enrich_headlines.py headlines.json --inplace
#   python scripts/enrich_headlines.py headlines.json --out headlines.enriched.json

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# --- settings you can tweak ---

TRACKING_PARAMS = {
    # common trackers
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "utm_name","utm_id","utm_reader","utm_cid",
    "fbclid","gclid","mc_cid","mc_eid","cmpid","s_kwcid","sscid",
    "ito","ref","smid","sref","partner","ICID","ns_campaign",
    "ns_mchannel","ns_source","ns_linkname","share_type","mbid"
}

# Very light paywall heuristics (expand as you learn)
PAYWALL_DOMAINS = {
    "ft.com","wsj.com","theglobeandmail.com","bloomberg.com","nytimes.com",
    "economist.com","latimes.com","thelogic.co","nationalpost.com","financialpost.com"
}

# Opinion markers
OPINION_TITLE_PAT = re.compile(r"\b(opinion|op-ed|analysis|commentary|column)\b", re.I)
OPINION_PATH_PAT  = re.compile(r"/(opinion|commentary|analysis|column)s?/", re.I)

# Normalize title before clustering
PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

def normalize_title_for_cluster(title: str) -> str:
    if not title:
        return ""
    # Drop trailing " - Source" if present — often aggregator adds it
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
    if not url:
        return ""
    try:
        u = urlparse(url)

        # Lowercase scheme/host; force https
        scheme = "https"
        netloc = (u.netloc or "").lower()

        # Strip common mobile subdomain prefixes like m., mobile.
        # (Be conservative; only strip a leading 'm.' or 'mobile.')
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

        # Rebuild
        cleaned = urlunparse((scheme, netloc, path, "", query, ""))
        return cleaned
    except Exception:
        return url  # fallback to original on any parse error

def canonical_id_from_url(url: str) -> str:
    base = canonicalize_url(url)
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]
    return f"u:{h}"

def domain_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""

def looks_paywalled(url: str, source: str | None = None) -> bool:
    host = domain_of(url)
    # direct domain hit
    for d in PAYWALL_DOMAINS:
        if host.endswith(d):
            return True
    # hint via source string
    if source:
        s = source.lower()
        if any(k in s for k in ("wall street journal","financial times","globe and mail","bloomberg","new york times","economist","the logic","national post","financial post")):
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

def enrich_item(it: dict) -> dict:
    url = it.get("url","")
    title = it.get("title","")
    source = it.get("source","")

    can_url = canonicalize_url(url)
    can_id  = canonical_id_from_url(url)
    cl_id   = cluster_id_from_title(title)

    # add/overwrite new fields
    it["canonical_url"] = can_url
    it["canonical_id"]  = can_id
    it["cluster_id"]    = cl_id
    it["paywall"]       = looks_paywalled(can_url, source)
    it["opinion"]       = looks_opinion(can_url, title)

    return it

def main():
    ap = argparse.ArgumentParser(description="Enrich headlines.json with canonical IDs and flags.")
    ap.add_argument("input", help="Path to headlines.json")
    ap.add_argument("--out", help="Output path (default: print to stdout)")
    ap.add_argument("--inplace", action="store_true", help="Write back to the same file")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data.get("items", [])
    enriched = [enrich_item(dict(it)) for it in items]

    # Ensure fields at top level stay sane
    out = dict(data)
    out["items"] = enriched
    out["count"] = len(enriched)
    out.setdefault("generated_utc", datetime.now(timezone.utc).isoformat())

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
