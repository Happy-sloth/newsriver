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
# - Shared requests.Session + slow feed detector + global time budget
# - Loads config/weights.json5 and applies a server-side score per item
# - Score components: recency, category, sources, public_safety, markets, (regional placeholder)
# - Effects flags: lightsaber/glitch + reasons for the front-end
# - Debug includes weights status and score trigger counts

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
except Exception:
    json5 = None  # ok locally; CI installs json5


# ---------------- Tunables ----------------
MAX_PER_FEED      = int(os.getenv("MPB_MAX_PER_FEED", "14"))
MAX_TOTAL         = int(os.getenv("MPB_MAX_TOTAL", "320"))

HTTP_TIMEOUT_S    = float(os.getenv("MPB_HTTP_TIMEOUT", "10"))
SLOW_FEED_WARN_S  = float(os.getenv("MPB_SLOW_FEED_WARN", "3.5"))
GLOBAL_BUDGET_S   = float(os.getenv("MPB_GLOBAL_BUDGET", "210"))

USER_AGENT        = os.getenv(
    "MPB_UA",
    "NewsRiverBot/1.3 (+https://mypybite.github.io/newsriver/)"
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
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "utm_name","utm_id","utm_reader","utm_cid",
    "fbclid","gclid","mc_cid","mc_eid","cmpid","s_kwcid","sscid",
    "ito","ref","smid","sref","partner","ICID","ns_campaign",
    "ns_mchannel","ns_source","ns_linkname","share_type","mbid",
    "oc","ved","ei","spm","rb_clickid","igsh","feature","source"
}

AGGREGATOR_HINT = re.compile(r"(news\.google|news\.yahoo|apple\.news|feedproxy|flipboard)\b", re.I)

# stopwords for title-token signatures
TITLE_STOPWORDS = {
    "the","a","an","and","or","but","of","for","with","without","in","on","at",
    "to","from","by","as","into","over","under","than","about","after","before",
    "due","will","still","just","not","is","are","was","were","be","being","been",
    "it","its","this","that","these","those",
    "live","update","updates","breaking","video","photos","report","reports","says","say","said",
    "vs","vs.","game","games","preview","recap","season","start","starts","starting","lineup",
    "dead","killed","kills","kill","dies","die","injured","injures","injury",
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
    if "SPORT" in s:                         return Tag("Sports", "World")  # NEW: tag Sports sections
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

def host_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


# ---------------- Title signatures ----------------
def strip_source_tail(title: str) -> str:
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
    """Parse feeds.txt, tolerating headers like '# --- Section --- #' (trailing '#')."""
    feeds: list[FeedSpec] = []
    current_tag = Tag("General", "World")
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                # Allow '# --- Section ---' as well as '# --- Section --- #' (tolerate trailing '#')
                header = re.sub(r"^#\s*-*\s*(.*?)\s*-*\s*#*\s*$", r"\1", line)
                current_tag = infer_tag(header)
                continue
            feeds.append(FeedSpec(url=line, tag=current_tag))
    return feeds


# ---------------- HTTP & date helpers ----------------
def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
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
            return datetime.now(timezone.utc).isoformat()
    return None

def _ts(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso.replace("Z","+00:00")).timestamp())
    except Exception:
        return 0

def hours_since(iso: str, now_ts: float) -> float:
    t = _ts(iso)
    if t == 0:
        return 1e9
    return max(0.0, (now_ts - t) / 3600.0)


# ---------------- Weights loader (json5) ----------------
def load_weights(path: str = "config/weights.json5") -> tuple[dict, dict]:
    """Load JSON5 weights; return (weights, debug_meta)."""
    dbg = {
        "weights_loaded": False,
        "weights_keys": [],
        "weights_error": "",
        "path": path,
    }
    data: dict = {}
    if not os.path.exists(path):
        dbg["weights_error"] = "missing"
        return data, dbg
    try:
        if json5 is None:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)  # best-effort fallback
        else:
            with open(path, "r", encoding="utf-8") as f:
                data = json5.load(f)
        dbg["weights_loaded"] = True
        dbg["weights_keys"] = sorted(list(data.keys()))
    except Exception as e:
        dbg["weights_error"] = f"{type(e).__name__}: {e}"
    return data, dbg

def W(d: dict, path: str, default):
    """Traverse nested dict by 'a.b.c' with a default."""
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


# ---------------- Public-safety parsing ----------------
WORD_NUM = {
    "one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,
    "eleven":11,"twelve":12,"thirteen":13,"fourteen":14,"fifteen":15,"sixteen":16,"seventeen":17,
    "eighteen":18,"nineteen":19,"twenty":20
}
RE_DEATH = re.compile(r"\b((?:\d+|one|two|three|four|five|six|seven|eight|nine|ten))\s+(?:people\s+)?(?:dead|killed|deaths?)\b", re.I)
RE_INJ   = re.compile(r"\b((?:\d+|one|two|three|four|five|six|seven|eight|nine|ten))\s+(?:people\s+)?(?:injured|hurt)\b", re.I)
RE_FATAL_CUE = re.compile(r"\b(dead|killed|homicide|murder|fatal|deadly)\b", re.I)

def word_or_int_to_int(s: str) -> int:
    s = s.lower()
    if s.isdigit():
        return int(s)
    return WORD_NUM.get(s, 0)

def parse_casualties(title: str) -> tuple[int,int,bool]:
    deaths = 0
    injured = 0
    for m in RE_DEATH.finditer(title):
        deaths += word_or_int_to_int(m.group(1))
    for m in RE_INJ.finditer(title):
        injured += word_or_int_to_int(m.group(1))
    has_fatal_cue = bool(RE_FATAL_CUE.search(title))
    return deaths, injured, has_fatal_cue


# ---------------- Market parsing from headline text ----------------
RE_PCT = r"([+-]?\d+(?:\.\d+)?)\s?%"

RE_BTC  = re.compile(r"\b(Bitcoin|BTC)\b.*?" + RE_PCT, re.I)
RE_IDX  = re.compile(r"\b(S&P|Nasdaq|Dow|TSX|TSXV)\b.*?" + RE_PCT, re.I)
RE_NIK  = re.compile(r"\b(Nikkei(?:\s*225)?)\b.*?" + RE_PCT, re.I)

# Stock move like: AEO +12% | TICKER jumps 10% | Company (AEO) up 11%
RE_TICK_PCT = re.compile(r"\b([A-Z]{2,5})\b[^%]{0,40}" + RE_PCT)

def first_pct(m):
    try:
        return abs(float(m.group(2)))
    except Exception:
        return None


# ---------------- Build ----------------
def build(feeds_file: str, out_path: str) -> dict:
    start = time.time()
    weights, weights_debug = load_weights()
    specs = parse_feeds_txt(feeds_file)

    collected: list[dict] = []
    per_host_counts: dict[str,int] = {}

    # debug collectors
    slow_domains: dict[str, int] = {}
    feed_times: list[tuple[str, float, int]] = []
    timeouts: list[str] = []
    errors: list[str] = []
    caps_hit: list[str] = []

    # scoring debug counters
    score_dbg = {
        "effects_lightsaber": 0,
        "effects_glitch": 0,
        "ps_fatal_hits": 0,
        "ps_injury_hits": 0,
        "market_btc_hits": 0,
        "market_index_hits": 0,
        "market_nikkei_hits": 0,
        "market_single_hits": 0,
        "agg_penalties": 0,
        "press_penalties": 0,
        "preferred_bonus": 0,
    }

    session = _new_session()

    print(f"[fetch] feeds={len(specs)} max_per_feed={MAX_PER_FEED} global_cap={MAX_TOTAL}")

    for idx, spec in enumerate(specs, 1):
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
        return len(a["url"]) < len(b["url"])

    for it in items:
        toks = set(title_tokens(it["title"]))
        merged = False
        for toks_other, rep in token_cache:
            if jaccard(toks, toks_other) >= THRESH:
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

    # Cluster lineage (rank within each cluster by time among survivors)
    cluster_groups: dict[str, list[dict]] = {}
    for it in survivors:
        cluster_groups.setdefault(it["cluster_id"], []).append(it)
    for cid, arr in cluster_groups.items():
        arr.sort(key=lambda x: _ts(x["published_utc"]))
        for i, it in enumerate(arr):
            it["cluster_rank"] = i + 1
            it["cluster_latest"] = (i == len(arr) - 1)

    # --------- Scoring ---------
    now_ts = time.time()

    # quick helpers from weights
    half_life_h = float(W(weights, "recency.half_life_hours", 6.0))
    age_pen_24  = float(W(weights, "recency.age_penalty_after_24h", -0.6))
    age_pen_36  = float(W(weights, "recency.age_penalty_after_36h", -0.4))
    superseded_pen = float(W(weights, "recency.superseded_cluster_penalty", -0.9))
    cat_table   = dict(W(weights, "categories", {}))
    agg_pen     = float(W(weights, "sources.aggregator_penalty", -0.5))
    wire_pen    = float(W(weights, "sources.press_wire_penalty", -0.4))
    pref_bonus  = float(W(weights, "sources.preferred_domains_bonus", 0.25))

    # public safety weights
    ps_has_fatal = float(W(weights, "public_safety.has_fatality_points", 1.0))
    ps_per_death = float(W(weights, "public_safety.per_death_points", 0.10))
    ps_max_death = float(W(weights, "public_safety.max_death_points", 2.0))
    ps_per_inj   = float(W(weights, "public_safety.per_injured_points", 0.02))
    ps_max_inj   = float(W(weights, "public_safety.max_injury_points", 0.6))
    ps_kw_bonus  = float(W(weights, "public_safety.violent_keywords_bonus", 0.2))
    ps_kw_list   = [k.lower() for k in W(weights, "public_safety.violent_keywords", [])]

    # markets
    btc_thr   = float(W(weights, "markets.btc_abs_move_threshold_pct", 7.0))
    btc_pts   = float(W(weights, "markets.btc_points", 1.6))
    idx_thr   = float(W(weights, "markets.index_abs_move_threshold_pct", 1.0))
    idx_pts   = float(W(weights, "markets.index_points", 1.0))
    nik_thr   = float(W(weights, "markets.nikkei_abs_move_threshold_pct", 1.0))
    nik_pts   = float(W(weights, "markets.nikkei_points", 0.7))
    stk_thr   = float(W(weights, "markets.single_stock_abs_move_threshold_pct", 10.0))
    stk_pts   = float(W(weights, "markets.single_stock_points", 1.2))

    # effects thresholds
    ls_min    = float(W(weights, "effects.lightsaber_min_score", 2.5))
    also_body = int(W(weights, "effects.lightsaber_also_if.body_count_ge", 5))
    also_btc  = float(W(weights, "effects.lightsaber_also_if.btc_abs_move_ge_pct", 8.0))
    also_stk  = float(W(weights, "effects.lightsaber_also_if.single_stock_abs_move_ge_pct", 15.0))
    glitch_min= float(W(weights, "effects.glitch_min_score", 1.8))

    def violent_kw_hit(title: str) -> bool:
        t = title.lower()
        return any(kw in t for kw in ps_kw_list)

    def apply_scoring(it: dict) -> None:
        title = it.get("title","")
        url   = it.get("url","")
        host  = host_of(url)
        category = it.get("category","General")
        published = it.get("published_utc","")

        comps = {}
        total = 0.0

        # Recency decay
        age_h = hours_since(published, now_ts)
        decay = 0.0
        if half_life_h > 0:
            decay = 1.0 * (0.5 ** (age_h / half_life_h))
        comps["recency"] = round(decay, 4)
        total += decay

        # Age penalties
        age_pen = 0.0
        if age_h > 24: age_pen += age_pen_24
        if age_h > 36: age_pen += age_pen_36
        if not it.get("cluster_latest", True):
            age_pen += superseded_pen
        if age_pen:
            comps["age_penalty"] = round(age_pen, 4)
            total += age_pen

        # Category nudge
        cat_bonus = float(cat_table.get(category, 0.0))
        if cat_bonus:
            comps["category"] = round(cat_bonus, 4)
            total += cat_bonus

        # Sources
        if looks_aggregator(it.get("source",""), url):
            comps["aggregator_penalty"] = agg_pen
            total += agg_pen
            score_dbg["agg_penalties"] += 1
        if is_press_wire(url):
            comps["press_wire_penalty"] = wire_pen
            total += wire_pen
            score_dbg["press_penalties"] += 1
        if host in PREFERRED_DOMAINS:
            comps["preferred_domain"] = pref_bonus
            total += pref_bonus
            score_dbg["preferred_bonus"] += 1

        # Public safety severity
        deaths, injured, has_fatal_cue = parse_casualties(title)
        ps_score = 0.0
        if has_fatal_cue or deaths > 0:
            ps_score += ps_has_fatal
            score_dbg["ps_fatal_hits"] += 1
        if deaths > 0:
            ps_score += min(ps_max_death, ps_per_death * deaths)
        if injured > 0:
            ps_score += min(ps_max_inj, ps_per_inj * injured)
            score_dbg["ps_injury_hits"] += 1
        if violent_kw_hit(title):
            ps_score += ps_kw_bonus
        if ps_score:
            comps["public_safety"] = round(ps_score, 4)
            total += ps_score

        # Markets (headline-derived)
        m = RE_BTC.search(title)
        btc_move = None
        if m:
            v = first_pct(m)
            if v is not None:
                btc_move = v
                if v >= btc_thr:
                    comps["btc_trigger"] = btc_pts
                    total += btc_pts
                    score_dbg["market_btc_hits"] += 1

        m = RE_IDX.search(title)
        if m:
            v = first_pct(m)
            if v is not None and v >= idx_thr:
                comps["index_trigger"] = idx_pts
                total += idx_pts
                score_dbg["market_index_hits"] += 1

        m = RE_NIK.search(title)
        if m:
            v = first_pct(m)
            if v is not None and v >= nik_thr:
                comps["nikkei_trigger"] = nik_pts
                total += nik_pts
                score_dbg["market_nikkei_hits"] += 1

        m = RE_TICK_PCT.search(title)
        single_move = None
        if m:
            try:
                single_move = abs(float(m.group(2)))
            except Exception:
                single_move = None
        if single_move is not None and single_move >= stk_thr:
            comps["single_stock_trigger"] = stk_pts
            total += stk_pts
            score_dbg["market_single_hits"] += 1

        # Regional bias placeholder (server-only knows "Canada"/"World")
        reg_bonus = 0.0
        if it.get("region") == "Canada":
            reg_bonus += float(W(weights, "regional.weights.country_match", 1.2))
        if reg_bonus:
            max_b = float(W(weights, "regional.max_bonus", 2.4))
            reg_bonus = min(reg_bonus, max_b)
            comps["regional"] = round(reg_bonus, 4)
            total += reg_bonus

        # Effects (lightsaber/glitch)
        effects = {"lightsaber": False, "glitch": False, "reasons": []}
        if total >= ls_min:
            effects["lightsaber"] = True
            effects["reasons"].append(f"score≥{ls_min}")
        if deaths >= also_body:
            effects["lightsaber"] = True
            effects["reasons"].append(f"body_count≥{also_body}")
        if btc_move is not None and btc_move >= also_btc:
            effects["lightsaber"] = True
            effects["reasons"].append(f"btc_move≥{also_btc}%")
        if single_move is not None and single_move >= also_stk:
            effects["lightsaber"] = True
            effects["reasons"].append(f"single_stock_move≥{also_stk}%")
        if not effects["lightsaber"] and total >= glitch_min:
            effects["glitch"] = True
            effects["reasons"].append(f"score≥{glitch_min}")

        if effects["lightsaber"]:
            score_dbg["effects_lightsaber"] += 1
        elif effects["glitch"]:
            score_dbg["effects_glitch"] += 1

        it["score"] = round(total, 4)
        it["score_components"] = comps
        it["effects"] = effects

    for it in survivors:
        apply_scoring(it)

    # Final sort: newest-first (primary), score as secondary (stable feel)
    survivors.sort(key=lambda x: (_ts(x["published_utc"]), x.get("score", 0.0)), reverse=True)

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
            "version": "fetch-v1.4-score",
            # weights status
            "weights_loaded": weights_debug.get("weights_loaded", False),
            "weights_keys": weights_debug.get("weights_keys", []),
            "weights_error": weights_debug.get("weights_error", None),
            "weights_path":  weights_debug.get("path", None),
            # scoring stats
            "score_stats": score_dbg,
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
    dbg = out.get("_debug", {})
    print(
        "Debug:",
        {
            k: dbg.get(k)
            for k in [
                "feeds_total","collected","dedup_pass1","dedup_final",
                "elapsed_sec","slow_domains","timeouts","errors",
                "weights_loaded","weights_keys","weights_error","weights_path","score_stats"
            ]
        }
    )

if __name__ == "__main__":
    main()
