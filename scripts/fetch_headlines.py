#!/usr/bin/env python3
# scripts/fetch_headlines.py
#
# Build headlines.json from feeds.txt
# - Reads feeds.txt (grouped with "# --- Section ---" headers)
# - Fetches RSS/Atom feeds
# - Normalizes & aggressively de-duplicates:
#     1) fuzzy title hash
#     2) near-duplicate pass (Jaccard on title tokens)
# - Demotes aggregators/press wires; small per-domain caps
# - Tags items with {category, region} inferred from section header
# - Sorts newest-first and writes headlines.json
#
# NEW (this edit):
# - Single shared requests.Session (faster, fewer sockets)
# - Slow feed detector, explicit HTTP timeouts, global time budget
# - Loads config/weights.json5 (if present) and reports keys in _debug
# - Richer debug: time stats, slow domains, timeouts/errors, caps hit
# - Cluster lineage markers: cluster_rank, cluster_latest

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
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import feedparser  # type: ignore
import requests    # type: ignore

# -------- Optional JSON5 weights (for knobs) --------
try:
    import json5  # type: ignore
except Exception:  # keep builder working even if json5 missing locally
    json5 = None  # loaded in CI via pip


# ---------------- Tunables ----------------
MAX_PER_FEED      = int(os.getenv("MPB_MAX_PER_FEED", "14"))   # cap per feed before global caps
MAX_TOTAL         = int(os.getenv("MPB_MAX_TOTAL", "320"))     # overall cap before de-dupe/sort

HTTP_TIMEOUT_S    = float(os.getenv("MPB_HTTP_TIMEOUT", "10")) # per-request timeout
SLOW_FEED_WARN_S  = float(os.getenv("MPB_SLOW_FEED_WARN", "3.5"))
GLOBAL_BUDGET_S   = float(os.getenv("MPB_GLOBAL_BUDGET", "210"))  # stop building after this many seconds

USER_AGENT        = os.getenv(
    "MPB_UA",
    "NewsRiverBot/1.2 (+https://mypybite.github.io/newsriver/)"
)

# Per-host caps to prevent any one domain flooding the river
PER_HOST_MAX = {
    "toronto.citynews.ca": 8,
    "financialpost.com": 6,
}

# Prefer these domains when breaking ties (primary/original/regulators)
PREFERRED_DOMAINS = {
    "cbc.ca","globalnews.ca","ctvnews.ca","blogto.com","toronto.citynews.ca",
    "nhl.com","mlbtraderumors.com",
    "bankofcanada.ca","federalreserve.gov","bls.gov","statcan.gc.ca",
    "sec.gov","cftc.gov","marketwatch.com",
    "coindesk.com","cointelegraph.com",
}

# Press-wire domains & path hints (these often duplicate across outlets)
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
    if "TECH" in s:                           return Tag("Tech", "Canada")
    if "WEATHER" in s or "EMERGENCY" in s:    return Tag("Weather", "Canada")
    if "TRANSIT" in s or "CITY SERVICE" in s: return Tag("Transit", "Canada")
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
    # drop ' - Site' / ' | Site' suffixes
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


# ---------------- HTTP & date helpers ----------------
def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    # keep-alive defaults are fine; explicit adapter for more conns if you like:
    adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=32)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

def http_get(session: requests.Session, url: str) -> bytes | None:
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT_S)
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


# ---------------- Weights loader (json5) ----------------
def load_weights(path: str = "config/weights.json5") -> tuple[dict, dict]:
    dbg = {"weights_loaded": False, "weights_keys": []}
    data: dict = {}
    if not os.path.exists(path):
        return data, dbg
    try:
        if json5 is None:
            # As a fallback, try regular json (in case file is valid JSON)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            with open(path, "r", encoding="utf-8") as f:
                data = json5.load(f)
        dbg["weights_loaded"] = True
        dbg["weights_keys"] = sorted(list(data.keys()))
    except Exception as e:
        dbg["weights_error"] = str(e)
    return data, dbg


# ---------------- Build ----------------
def build(feeds_file: str, out_path: str) -> dict:
    start = time.time()
    weights, weights_debug = load_weights()
    specs = parse_feeds_txt(feeds_file)

    collected: list[dict] = []
    per_host_counts: dict[str,int] = {}

    # debug collectors
    slow_domains: dict[str, int] = {}
    feed_times: list[tuple[str, float, int]] = []  # (host, seconds, entries_kept)
    timeouts: list[str] = []
    errors: list[str] = []
    caps_hit: list[str] = []

    session = _new_session()

    print(f"[fetch] feeds={len(specs)} max_per_feed={MAX_PER_FEED} global_cap={MAX_TOTAL}")

    for idx, spec in enumerate(specs, 1):
        # Global budget guard
        if time.time() - start > GLOBAL_BUDGET_S:
            print(f"[budget] global time budget {GLOBAL_BUDGET_S:.0f}s exceeded at feed {idx}/{len(specs)}")
            break

        t0 = time.time()
        blob = http_get(session, spec.url)
        dt = time.time() - t0

        h_feed = host_of(spec.url) or "(unknown)"
        kept_from_feed = 0

        if blob is None:
            if dt >= HTTP_TIMEOUT_S:
                timeouts.append(h_feed)
                print(f"[timeout] {h_feed} ({spec.url}) ~{dt:.1f}s")
            else:
                errors.append(h_feed)
                print(f"[error]   {h_feed} ({spec.url}) ~{dt:.1f}s (no content)")
            continue

        if dt > SLOW_FEED_WARN_S:
            slow_domains[h_feed] = slow_domains.get(h_feed, 0) + 1
            print(f"[slow]    {h_feed} took {dt:.2f}s")

        try:
            parsed = feedparser.parse(blob)
        except Exception as e:
            errors.append(h_feed)
            print(f"[parse]   error {h_feed}: {e}")
            continue

        feed_title = (parsed.feed.get("title") or h_feed or "").strip()
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
                if h and h not in caps_hit:
                    caps_hit.append(h)
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
            kept_from_feed += 1

            if len(collected) >= MAX_TOTAL:
                print("[cap] global MAX_TOTAL reached")
                break

        feed_times.append((h_feed, dt, kept_from_feed))
        if len(collected) >= MAX_TOTAL:
            break

        # progress ping every ~20 feeds
        if (idx % 20) == 0:
            elapsed = time.time() - start
            print(f"[progress] {idx}/{len(specs)} feeds, items={len(collected)}, elapsed={elapsed:.1f}s")

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

    # Cluster lineage (rank within each cluster by time)
    cluster_groups: dict[str, list[dict]] = {}
    for it in survivors:
        cluster_groups.setdefault(it["cluster_id"], []).append(it)
    for cid, arr in cluster_groups.items():
        arr.sort(key=lambda x: _ts(x["published_utc"]))
        for i, it in enumerate(arr):
            it["cluster_rank"] = i + 1
            it["cluster_latest"] = (i == len(arr) - 1)

    # Final sort newest-first
    survivors.sort(key=lambda x: _ts(x["published_utc"]), reverse=True)

    elapsed_total = time.time() - start

    out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(survivors),
        "items": survivors,
        "_debug": {
            "feeds_total": len(specs),
            "cap_items": MAX_TOTAL,
            "collected": len(collected),
            "dedup_pass1": len(items),
            "dedup_final": len(survivors),
            "slow_domains": sorted(list(slow_domains.keys())),
            "timeouts": sorted(set(timeouts)),
            "errors": sorted(set(errors)),
            "caps_hit": sorted(caps_hit),
            "feed_times_sample": sorted(
                [{"host": h, "sec": round(sec, 3), "kept": kept} for (h, sec, kept) in feed_times[:10]],
                key=lambda x: -x["sec"]
            ),
            "elapsed_sec": round(elapsed_total, 2),
            "http_timeout_sec": HTTP_TIMEOUT_S,
            "slow_feed_warn_sec": SLOW_FEED_WARN_S,
            "global_budget_sec": GLOBAL_BUDGET_S,
            "version": "fetch-v1.3-ndup+slow",
            # weights status
            "weights_loaded": weights_debug.get("weights_loaded", False),
            "weights_keys": weights_debug.get("weights_keys", []),
            "weights_error": weights_debug.get("weights_error", None),
        }
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[done] wrote {out_path} items={out['count']} elapsed={elapsed_total:.1f}s")
    return out


def main():
    ap = argparse.ArgumentParser(description="Build headlines.json from feeds.txt")
    ap.add_argument("--feeds-file", default="feeds.txt", help="Path to feeds.txt")
    ap.add_argument("--out", default="headlines.json", help="Output JSON file")
    args = ap.parse_args()
    out = build(args.feeds_file, args.out)
    # compact footer to help Actions logs
    dbg = out.get("_debug", {})
    print(
        "Debug:",
        {
            k: dbg.get(k)
            for k in [
                "feeds_total","collected","dedup_pass1","dedup_final",
                "elapsed_sec","slow_domains","timeouts","errors",
                "weights_loaded","weights_keys"
            ]
        }
    )

if __name__ == "__main__":
    main()
