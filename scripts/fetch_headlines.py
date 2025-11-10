#!/usr/bin/env python3
# scripts/fetch_headlines.py
#
# Build headlines.json from feeds.txt with strict link verification,
# 24–69h freshness window (set MPB_MAX_AGE_HOURS in env), market sanity checks,
# and an exact 33-item guarantee when REQUIRE_EXACT_COUNT>0.
# PLUS: Safety-net fallback to guarantee at least one working headline.
#
# NEW:
# - Run-length limiter: no more than 2 in a row by domain and by cluster/topic
# - Regional scoring supports Toronto city-match bonus from weights.json5
# - Optional "Polling/Projection" category via section headers
# - Toronto-first minimum (env MPB_MIN_TORONTO, default 9) with Canadian city backfill
# - Labour tagging: category="Labour", region CA hints, priority_reason audit trail
#
# AMENDMENTS (2025-11-06):
# - Urgency/obituary signals expanded: dies, died, passes, passed away, obituary, obit, RIP
# - AM business bias (06:00–12:00 ET) with small scoring bonus
# - “Major figure” bump (tiny roster) esp. when paired with obituary terms
# - Monotonic generated_utc and itemset_hash in output for front-end freshness gating

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Tuple, Iterable, Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, urlsplit, urlunsplit, parse_qs

import feedparser  # type: ignore
import requests    # type: ignore
from email.utils import parsedate_to_datetime  # robust RFC822 parsing

try:
    import json5  # type: ignore
except Exception:
    json5 = None

try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:
    BeautifulSoup = None

try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:
    ZoneInfo = None

# ================= Labour tagging helpers (EXISTING) =================
LABOUR_DEFAULT_KEYWORDS = [
    "strike","walkout","work stoppage","lockout","wildcat","picket",
    "arbitration","mediation","collective bargaining",
    "tentative deal","ratification","binding arbitration",
    "back-to-work order","back to work","return-to-work",
    "salary increase","wage increase","cost-of-living adjustment",
    "back to school order","job action","work-to-rule","strike vote"
]
LABOUR_DEFAULT_ENTITIES = [
    "CUPE","OPSEU","PSAC","Unifor","ATU","OECTA","ETFO","OSSTF",
    "Teamsters","SEIU","UFCW","IAMAW","IBEW","UNITE HERE"
]
CA_HINT_DOMAINS = {
    "cbc.ca","globalnews.ca","ctvnews.ca","cp24.com","toronto.citynews.ca",
    "theglobeandmail.com","financialpost.com","nationalpost.com",
    "thestar.com","montrealgazette.com","vancouversun.com","calgaryherald.com",
    "edmontonjournal.com","winnipegfreepress.com","ottawacitizen.com",
    "timescolonist.com","saltwire.com","dailyhive.com","citynews.ca",
    "labourstart.org"
}

def load_labour_hints(weights: Dict[str, Any]) -> Dict[str, Any]:
    def _get(path: str, default):
        cur = weights
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur
    blk = _get("labour_keywords", {})
    kw_bonus = float(blk.get("keyword_bonus", 0.9)) if isinstance(blk, dict) else 0.9
    kws = (blk.get("keywords") if isinstance(blk, dict) else None) or LABOUR_DEFAULT_KEYWORDS
    ents = (blk.get("entities") if isinstance(blk, dict) else None) or LABOUR_DEFAULT_ENTITIES
    return {
        "keyword_bonus": kw_bonus,
        "keywords": [k.lower() for k in kws],
        "entities": [e.lower() for e in ents],
    }

def is_canadian_context(url: str, title: str, summary: str) -> bool:
    host = ""
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        pass
    if any(host.endswith(d) for d in CA_HINT_DOMAINS):
        return True
    text = f"{title} {summary}".lower()
    locality = [
        "canada","canadian","ottawa","toronto","ontario","bc","british columbia",
        "vancouver","alberta","calgary","edmonton","saskatchewan","manitoba",
        "winnipeg","quebec","montreal","lavalle","new brunswick","nova scotia",
        "halifax","pei","prince edward island","newfoundland","yukon","nunavut",
        "northwest territories"
    ]
    return any(tok in text for tok in locality)

def tag_labour_if_applicable(item: Dict[str, Any], labour_hints: Dict[str, Any]) -> None:
    title = (item.get("title") or "").lower()
    summary = (item.get("summary") or item.get("description") or "").lower()
    url = item.get("url") or item.get("link") or ""
    reasons: List[str] = []

    hits_kw = [kw for kw in labour_hints["keywords"] if kw in title or kw in summary]
    hits_en = [en for en in labour_hints["entities"] if en in title or en in summary]
    if hits_kw or hits_en:
        item["category"] = "Labour"
        if hits_kw:
            reasons.append("kw:" + ",".join(sorted(set(hits_kw))[:3]))
        if hits_en:
            reasons.append("entity:" + ",".join(sorted(set(hits_en))[:3]))

    if is_canadian_context(url, item.get("title",""), item.get("summary","")):
        item["region"] = "Canada"
        reasons.append("region:CA")

    if reasons:
        pr = item.get("priority_reason")
        if isinstance(pr, list):
            pr.extend(reasons)
        elif isinstance(pr, str):
            item["priority_reason"] = [pr] + reasons
        else:
            item["priority_reason"] = reasons
# ================= end Labour helpers =================

# ---------------- Tunables ----------------
MAX_PER_FEED      = int(os.getenv("MPB_MAX_PER_FEED", "14"))
MAX_TOTAL         = int(os.getenv("MPB_MAX_TOTAL", "320"))
BREAKER_LIMIT     = int(os.getenv("MPB_BREAKER_LIMIT", "3"))

HTTP_TIMEOUT_S    = float(os.getenv("MPB_HTTP_TIMEOUT", "18"))
SLOW_FEED_WARN_S  = float(os.getenv("MPB_SLOW_FEED_WARN", "3.5"))
GLOBAL_BUDGET_S   = float(os.getenv("MPB_GLOBAL_BUDGET", "210"))

USER_AGENT        = os.getenv(
    "MPB_UA",
    "NewsRiverBot/1.3 (+https://mypybite.github.io/newsriver/)"
)
ALT_USER_AGENT = os.getenv(
    "MPB_ALT_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
ACCEPT_HEADER = "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"
ACCEPT_LANG   = "en-US,en;q=0.8"

# Enforcers
VERIFY_LINKS                = os.getenv("MPB_VERIFY_LINKS", "1") == "1"
REJECT_REDIRECT_TO_HOMEPAGE = os.getenv("MPB_REJECT_REDIRECT_TO_HOMEPAGE", "1") == "1"
BLOCK_AGGREGATORS           = os.getenv("MPB_BLOCK_AGGREGATORS", "1") == "1"
MIN_AGE_SEC                 = int(os.getenv("MPB_MIN_AGE_SEC", "60"))
MAX_AGE_HOURS               = float(os.getenv("MPB_MAX_AGE_HOURS", "69"))
REQUIRE_EXACT_COUNT         = int(os.getenv("MPB_REQUIRE_EXACT_COUNT", "33"))

# --- Toronto-first minimum + city backfill ---
MPB_MIN_TORONTO             = int(os.getenv("MPB_MIN_TORONTO", "9"))
CITY_BACKFILL_ORDER = [
    "Vancouver", "Montreal", "Halifax", "Winnipeg",
    "Calgary", "Edmonton", "Ottawa", "Quebec City", "Hamilton"
]

# ---------- SAFETY-NET FALLBACK ----------
DEFAULT_FALLBACK_FEEDS = [
    "https://www.reuters.com/world/us/rss",
    "https://www.reuters.com/world/rss",
    "https://www.cbc.ca/cmlink/rss-topstories",
    "https://www.ctvnews.ca/rss/ctvnews-ca-top-stories-public-rss-1.822009",
    "https://globalnews.ca/feed/",
    "https://www.cp24.com"
]
FALLBACK_FEEDS = [
    u.strip() for u in os.getenv("MPB_FALLBACK_FEEDS", ",".join(DEFAULT_FALLBACK_FEEDS)).split(",")
    if u.strip()
]
FALLBACK_MAX_AGE_HOURS = float(os.getenv("MPB_FALLBACK_MAX_AGE_HOURS", "24"))
FALLBACK_MIN_ITEMS     = int(os.getenv("MPB_FALLBACK_MIN_ITEMS", "1"))

# Source hygiene limits (env-overridable)
def _load_per_host_max() -> dict[str, int]:
    raw = os.getenv("MPB_PER_HOST_MAX", "").strip()
    if not raw:
        return {
            "toronto.citynews.ca": 8,
            "financialpost.com": 6,
            "cultmtl.com": 6,
        }
    try:
        return json.loads(raw)
    except Exception:
        return {
            "toronto.citynews.ca": 8,
            "financialpost.com": 6,
            "cultmtl.com": 6,
        }

PER_HOST_MAX = _load_per_host_max()

PREFERRED_DOMAINS = {
    "cbc.ca","globalnews.ca","ctvnews.ca","blogto.com","toronto.citynews.ca",
    "nhl.com","mlbtraderumors.com","mlb.com","sportsnet.ca","tsn.ca",
    "espn.com","theathletic.com",
    "bankofcanada.ca","federalreserve.gov","bls.gov","statcan.gc.ca",
    "sec.gov","cftc.gov","marketwatch.com",
    "coindesk.com","cointelegraph.com",
    "cultmtl.com",
}

MARKET_AUTH_DOMAINS = {
    "wsj.com","ft.com","bloomberg.com","reuters.com","coindesk.com","marketwatch.com","cnbc.com","apnews.com"
}

SPORTS_PRIOR_DOMAINS = {"mlb.com","sportsnet.ca","tsn.ca","espn.com","theathletic.com","cbssports.com"}

PRESS_WIRE_DOMAINS = {
    "globenewswire.com","newswire.ca","prnewswire.com","businesswire.com","accesswire.com"
}
PRESS_WIRE_PATH_HINTS = ("/globe-newswire", "/globenewswire", "/business-wire", "/newswire/")

TRACKING_PARAMS = {
    "utm_source","utm_medium","utm_campaign","utm_term","utm_content",
    "utm_name","utm_id","utm_reader","utm_cid",
    "fbclid","gclid","mc_cid","mc_eid","cmpid","s_kwcid","sscid",
    "ito","ref","smid","sref","partner","ICID","ns_campaign",
    "ns_mchannel","ns_source","ns_linkname","share_type","mbid",
    "oc","ved","ei","spm","rb_clickid","igsh","feature","source"
}

AGGREGATOR_HINT = re.compile(r"(news\.google|news\.yahoo|apple\.news|feedproxy|flipboard)\b", re.I)

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

MPB_SUBSTACK_HOST = "mypybite.substack.com"

# --- Sports / markets patterns ---
RE_JAYS_TEAM = re.compile(r"\b(blue\s*jays|toronto\s*blue\s*jays|jays)\b", re.I)
RE_JAYS_PLAYERS = re.compile(
    r"\b("
    r"vladimir(?:\s+guerrero(?:\s+jr\.?)?)|guerrero(?:\s+jr\.?)?|"
    r"bichette|alejandro\s+kirk|kirk|"
    r"chris\s+bassitt|bassitt|"
    r"kevin\s+gausman|gausman|"
    r"eric\s+lauer|lauer|"
    r"trey\s+yesavage|yesavage"
    r")\b",
    re.I
)
RE_JAYS_WIN  = re.compile(r"\b(beat|edge|top|blank|shut\s*out|walk-?off|clinch|sweep|down|roll past)\b", re.I)
RE_JAYS_LOSS = re.compile(r"\b(lose(?:s)?\s+to|fall(?:s)?\s+to|drop(?:s)?\s+to|blown\s+save|skid|defeat(?:ed)?\s+by)\b", re.I)
RE_MLB_TEAMS = re.compile(
    r"\b("
    r"toronto\s*blue\s*jays|blue\s*jays|jays|"
    r"boston\s*red\s*sox|red\s*sox|"
    r"new\s*york\s*yankees|ny\s*yankees|yankees|"
    r"seattle\s*mariners|mariners|"
    r"los\s*angeles\s*dodgers|la\s*dodgers|dodgers|"
    r"philadelphia\s*phillies|phillies"
    r")\b",
    re.I
)
RE_MLB_FINAL_WORD = re.compile(r"\b(final|finals|post\s*game|postgame|recap)\b", re.I)
RE_SCORELINE = re.compile(r"\b\d{1,2}\s*[–-]\s*\d{1,2}\b")

# --- Toronto city cues (for regional.city_match_toronto bonus) ---
RE_TORONTO_CUES = re.compile(
    r"\b(toronto|gta|scarborough|etobicoke|north\s+york|mississauga|brampton|peel\s+region|durham\s+region|york\s+region|ttc|rogers\s+centre|scotiabank\s+arena)\b",
    re.I
)

WEAK_TO_STRONG_POLITICS = [
    (re.compile(r"\bcriticiz(?:e|es|ed|ing)\b", re.I), "slams"),
    (re.compile(r"\bcondemn(?:s|ed|ing)?\b", re.I), "lashes"),
    (re.compile(r"\bdisput(?:e|es|ed|ing)\b", re.I), "defies"),
    (re.compile(r"\bwarn(?:s|ed|ing)?\b", re.I), "warns"),
    (re.compile(r"\bcall(?:s|ed)? for\b", re.I), "demands"),
    (re.compile(r"\bpush(?:es|ed|ing)? for\b", re.I), "presses"),
]
CONFLICT_CUES = re.compile(
    r"\b(strike|strikes|missile|rocket|shelling|offensive|incursion|raid|drone|artillery|frontline|ceasefire|truce)\b",
    re.I,
)
CEASEFIRE_WEAK = re.compile(r"\b(cease[- ]?fire|truce)\s+(ends|fails|breaks? down)\b", re.I)

POS_MARKET_WORDS = re.compile(r"\b(up|rise|rises|gains?|surges?|soars?|rall(y|ies))\b", re.I)
NEG_MARKET_WORDS = re.compile(r"\b(down|fall(?:s|en)?|drops?|slumps?|slides?|plunges?|craters?|tanks?)\b", re.I)

RE_PCT = r"([+-]?\d+(?:\.\d+)?)\s?%"
RE_BTC  = re.compile(r"\b(Bitcoin|BTC)\b.*?" + RE_PCT, re.I)
RE_IDX  = re.compile(r"\b(S&P|Nasdaq|Dow|TSX|TSXV)\b.*?" + RE_PCT, re.I)
RE_NIK  = re.compile(r"\b(Nikkei(?:\s*225)?)\b.*?" + RE_PCT, re.I)
RE_TICK_PCT = re.compile(r"\b([A-Z]{2,5})\b[^%]{0,40}" + RE_PCT)

# Breaking cue (EXISTING) — keep
RE_BREAKING = re.compile(
    r"\b(breaking|developing|just in|alert|evacuate|earthquake|hurricane|wildfire|flood|tsunami|tornado|"
    r"missile|air[-\s]?strike|explosion|blast|drone|shooting|casualties?|dead|killed)\b", re.I
)

# --- NEW: obituary/urgency terms (beyond 'dead|killed') ---
RE_OBIT_URGENCY = re.compile(
    r"\b(dies|died|passes|passed away|obituary|obit|rip)\b", re.I
)

# --- Soft-404 / article detection ---
SOFT_404_PATTERNS = [
    r"\b(page not found|sorry, we (couldn'?t|cannot) find|content (is )?unavailable)\b",
    r"\b(does not exist|no longer available|moved permanently|has been removed)\b",
    r"\b(404 error|error 404)\b",
    r"\b(subscriber( |-)only|please log in to continue)\b",
]
ARTICLE_HINT_PATTERNS = [
    r"<article\b",
    r'itemtype="https?://schema\.org/Article"',
    r'property="og:type"\s+content="article"',
    r'property="og:title"\s+content="[^"]{10,}"',
]
MIN_BODY_BYTES = int(os.getenv("MPB_MIN_BODY_BYTES", "4096"))
MIN_ARTICLE_WORDS = int(os.getenv("MPB_MIN_ARTICLE_WORDS", "120"))

# --- Reject list for low-signal celebrity bait (pre-score) ---
REJECT_PATTERNS = [
    re.compile(r"\blebron\b", re.I),
    re.compile(r"\bkardashian\b", re.I),
    re.compile(r"\btmz\b", re.I),
]
ALLOW_DURING_PLAYOFFS = os.getenv("MPB_ALLOW_REJECT_DURING_PLAYOFFS", "1") == "1"

def should_reject_title(title: str, playoffs_on: bool) -> bool:
    if playoffs_on and ALLOW_DURING_PLAYOFFS:
        return False
    t = title or ""
    for pat in REJECT_PATTERNS:
        if pat.search(t):
            return True
    return False

def is_mlb_final_title(title: str) -> bool:
    if not title:
        return False
    return bool(RE_MLB_FINAL_WORD.search(title) or RE_SCORELINE.search(title) or RE_JAYS_WIN.search(title) or RE_JAYS_LOSS.search(title))

def is_sports_domain(host: str) -> bool:
    if not host:
        return False
    host = host.lower()
    return any(host.endswith(d) for d in SPORTS_PRIOR_DOMAINS)

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
    if "SPORTS" in s:                        return Tag("Sports", "Canada")
    if "POLL" in s or "ELECTION" in s:       return Tag("Polling/Projection", "World")
    return Tag("General", "World")

def canonicalize_url(url: str) -> str:
    if not url: return ""
    try:
        u = urlparse(url)
        scheme = "https" if u.scheme else "https"
        netloc = (u.netloc or "").lower()
        if netloc.startswith("m.") and "." in netloc[2:]: netloc = netloc[2:]
        elif netloc.startswith("mobile.") and "." in netloc[7:]: netloc = netloc[7:]
        path = u.path or "/"
        query_pairs = [(k, v) for (k, v) in parse_qsl(u.query, keep_blank_values=True) if k not in TRACKING_PARAMS]
        query = urlencode(query_pairs, doseq=True)
        if path != "/" and path.endswith("/"): path = path[:-1]
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception:
        return url

def canonical_id(url: str) -> str:
    base = canonicalize_url(url)
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]
    return f"u:{h}"

def host_of(url: str) -> str:
    try: return (urlparse(url).netloc or "").lower()
    except Exception: return ""

def path_of(url: str) -> str:
    try: return (urlparse(url).path or "")
    except Exception: return ""

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
    if not a or not b: return 0.0
    inter = len(a & b)
    if inter == 0: return 0.0
    union = len(a | b)
    return inter / union

def is_press_wire(url: str) -> bool:
    h = host_of(url)
    if h in PRESS_WIRE_DOMAINS: return True
    p = urlparse(url).path or ""
    return any(hint in p for hint in PRESS_WIRE_PATH_HINTS)

def looks_aggregator(source: str, link: str) -> bool:
    if not BLOCK_AGGREGATORS:
        return False
    blob = f"{source} {link}"
    if AGGREGATOR_HINT.search(blob): return True
    if is_press_wire(link): return True
    return False

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
            if not line: continue
            if line.startswith("#"):
                header = re.sub(r"^#\s*-*\s*(.*?)\s*-*\s*$", r"\1", line)
                current_tag = infer_tag(header)
                continue
            feeds.append(FeedSpec(url=line, tag=current_tag))
    return feeds

def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": ACCEPT_HEADER,
        "Accept-Language": ACCEPT_LANG,
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
    })
    adapter = requests.adapters.HTTPAdapter(pool_connections=24, pool_maxsize=48, max_retries=1)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

def _looks_like_xml(content: bytes, ctype: str) -> bool:
    if "xml" in ctype or "rss" in ctype or "atom" in ctype: return True
    head = (content[:64] or b"").lstrip()
    return head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head

def _cache_bust_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        q = parse_qs(parts.query, keep_blank_values=True)
        q["_mpb"] = [str(int(time.time() // 60))]
        new_q = urlencode(q, doseq=True)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, new_q, parts.fragment))
    except Exception:
        sep = "&" if ("?" in (url or "")) else "?"
        return f"{url}{sep}v={int(time.time() // 60)}"

def http_get(session: requests.Session, url: str) -> bytes | None:
    try:
        bust = _cache_bust_url(url)
        headers_primary = {
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
            "Accept": ACCEPT_HEADER,
            "Accept-Language": ACCEPT_LANG,
            "User-Agent": USER_AGENT,
        }
        resp = session.get(bust, timeout=HTTP_TIMEOUT_S, allow_redirects=True, headers=headers_primary)
        if getattr(resp, "ok", False) and resp.content:
            ctype = resp.headers.get("Content-Type", "").lower()
            if _looks_like_xml(resp.content, ctype): return resp.content
        alt_headers = {
            "User-Agent": ALT_USER_AGENT,
            "Accept": ACCEPT_HEADER,
            "Accept-Language": ACCEPT_LANG,
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
        }
        resp2 = session.get(_cache_bust_url(url), timeout=HTTP_TIMEOUT_S, headers=alt_headers, allow_redirects=True)
        if getattr(resp2, "ok", False) and resp2.content:
            ctype2 = resp2.headers.get("Content-Type", "").lower()
            if _looks_like_xml(resp2.content, ctype2): return resp2.content
        return getattr(resp2, "content", None)
    except Exception:
        return None

def to_iso_from_struct(t) -> str | None:
    try:
        epoch = calendar.timegm(t)
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00","Z")
    except Exception:
        return None

def _to_iso_utc(dt) -> str | None:
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00","Z")
    except Exception:
        return None

def parse_any_dt_str(s: str) -> str | None:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt:
            iso = _to_iso_utc(dt)
            if iso:
                return iso
    except Exception:
        pass
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z","+00:00"))
        else:
            dt = datetime.fromisoformat(s)
        iso = _to_iso_utc(dt)
        if iso:
            return iso
    except Exception:
        pass
    return None

def pick_published(entry) -> str | None:
    for key in ("published_parsed","updated_parsed","created_parsed"):
        t = getattr(entry, key, None)
        if t:
            iso = to_iso_from_struct(t)
            if iso:
                return iso
    for key in ("published","updated","created","issued","date"):
        val = entry.get(key)
        if isinstance(val, str):
            iso = parse_any_dt_str(val.strip())
            if iso:
                return iso
    return None

def _ts(iso: str) -> int:
    try: return int(datetime.fromisoformat(iso.replace("Z","+00:00")).timestamp())
    except Exception: return 0

def hours_since(iso: str, now_ts: float) -> float:
    t = _ts(iso)
    if t == 0: return 1e9
    return max(0.0, (now_ts - t) / 3600.0)

def iso_add_hours(iso_s: str | None, hours: float) -> str:
    base = None
    if iso_s:
        try: base = datetime.fromisoformat(iso_s.replace("Z","+00:00"))
        except Exception: base = None
    if base is None: base = datetime.now(timezone.utc)
    if base.tzinfo is None: base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(hours=hours)).astimezone(timezone.utc).isoformat().replace("+00:00","Z")

def iso_add_seconds(iso_s: str, seconds: int) -> str:
    try:
        base = datetime.fromisoformat(iso_s.replace("Z","+00:00"))
    except Exception:
        base = datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).astimezone(timezone.utc).isoformat().replace("+00:00","Z")

def now_in_tz(tz_name: str) -> datetime:
    if ZoneInfo is None: return datetime.now(timezone.utc)
    try: return datetime.now(ZoneInfo(tz_name))
    except Exception: return datetime.now(timezone.utc)

def load_weights(path: str = "config/weights.json5") -> tuple[dict, dict]:
    dbg = {"weights_loaded": False, "weights_keys": [], "weights_error": "", "path": path}
    data: dict = {}
    if not os.path.exists(path):
        dbg["weights_error"] = "missing"; return data, dbg
    try:
        if json5 is None:
            with open(path, "r", encoding="utf-8") as f: data = json.load(f)
        else:
            with open(path, "r", encoding="utf-8") as f: data = json5.load(f)
        dbg["weights_loaded"] = True
        dbg["weights_keys"] = sorted(list(data.keys()))
    except Exception as e:
        dbg["weights_error"] = f"{type(e).__name__}: {e}"
    return data, dbg

def W(d: dict, path: str, default):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur: return default
        cur = cur[p]
    return cur

# ---------------- Public-safety parsing ----------------
WORD_NUM = {"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,
            "eleven":11,"twelve":12,"thirteen":13,"fourteen":14,"fifteen":15,"sixteen":16,"seventeen":17,
            "eighteen":18,"nineteen":19,"twenty":20}
RE_DEATH = re.compile(r"\b((?:\d+|one|two|three|four|five|six|seven|eight|nine|ten))\s+(?:people\s+)?(?:dead|killed|deaths?)\b", re.I)
RE_INJ   = re.compile(r"\b((?:\d+|one|two|three|four|five|six|seven|eight|nine|ten))\s+(?:people\s+)?(?:injured|hurt)\b", re.I)
# AMENDED: include obit verbs as fatal cues too
RE_FATAL_CUE = re.compile(r"\b(dead|killed|homicide|murder|fatal|deadly|dies|died|passes|passed away|obituary|obit|rip)\b", re.I)

def word_or_int_to_int(s: str) -> int:
    s = s.lower()
    if s.isdigit(): return int(s)
    return WORD_NUM.get(s, 0)

def parse_casualties(title: str) -> tuple[int,int,bool]:
    deaths = 0; injured = 0
    for m in RE_DEATH.finditer(title): deaths += word_or_int_to_int(m.group(1))
    for m in RE_INJ.finditer(title): injured += word_or_int_to_int(m.group(1))
    has_fatal_cue = bool(RE_FATAL_CUE.search(title) or RE_OBIT_URGENCY.search(title))
    return deaths, injured, has_fatal_cue

# ---------------- Market helpers ----------------
def first_pct(m):
    try: return abs(float(m.group(2)))
    except Exception: return None

def first_pct_signed(m):
    try: return float(m.group(2))
    except Exception: return None

# ---------------- Relative "ago" parsing ----------------
REL_AGO = re.compile(r"\b(\d+)\s*(minute|minutes|hour|hours|day|days)\s+ago\b", re.I)
def _hours_from_rel(s: str) -> float | None:
    m = REL_AGO.search(s or "")
    if not m: return None
    n = int(m.group(1)); unit = m.group(2).lower()
    if unit.startswith("minute"): return n / 60.0
    if unit.startswith("hour"):   return float(n)
    if unit.startswith("day"):    return float(n) * 24.0
    return None

# ---------------- HTML scrapers (Nate/CP24) ----------------
def scrape_nate_silver(html: bytes, spec: FeedSpec, playoffs_on: bool) -> list[dict]:
    items: list[dict] = []
    text = html.decode("utf-8", errors="ignore")
    if BeautifulSoup is not None:
        soup = BeautifulSoup(text, "html.parser")
        blocks = soup.find_all(["article", "div"], attrs={"class": re.compile(r"(card|post|river|article|story)", re.I)})
        seen = set()
        for blk in blocks:
            a = blk.find("a", href=re.compile(r"https?://fivethirtyeight\.com/[^\"#]+", re.I))
            if not a: continue
            href = a.get("href") or ""
            if not href or "contributors/" in href: continue
            title = (a.get_text(" ", strip=True) or "").strip()
            if not title or len(title) < 8 or href in seen: continue
            if should_reject_title(title, playoffs_on): continue
            seen.add(href)

            age_hint = None
            tnode = blk.find("time")
            if tnode: age_hint = _hours_from_rel(tnode.get_text(" ", strip=True))
            if age_hint is None: age_hint = _hours_from_rel(blk.get_text(" ", strip=True))

            pub_dt = datetime.now(timezone.utc) - timedelta(hours=age_hint) if age_hint is not None else datetime.now(timezone.utc)
            can_url = canonicalize_url(href)
            items.append({
                "title": title,
                "url": can_url,
                "source": "FiveThirtyEight — Nate Silver",
                "published_utc": _to_iso_utc(pub_dt) or pub_dt.isoformat().replace("+00:00","Z"),
                "category": spec.tag.category,
                "region": spec.tag.region,
                "canonical_url": can_url,
                "canonical_id": canonical_id(can_url),
                "cluster_id": fuzzy_title_key(title),
                "age_hint_hours": age_hint if age_hint is not None else None,
            })
            if len(items) >= MAX_PER_FEED: break
        return items

    # Fallback regex pass
    seen = set()
    for chunk in re.split(r"(?i)<article\b", text):
        m = re.search(r'href="(https?://fivethirtyeight\.com/[^"]+)"[^>]*>([^<]{8,})</a>', chunk, flags=re.I)
        if not m: continue
        href = m.group(1)
        if "contributors/" in href: continue
        title = re.sub(r"\s+", " ", m.group(2)).strip()
        if not title or href in seen: continue
        if should_reject_title(title, playoffs_on): continue
        seen.add(href)
        age_hint = _hours_from_rel(chunk)
        pub_dt = datetime.now(timezone.utc) - timedelta(hours=age_hint) if age_hint is not None else datetime.now(timezone.utc)
        can_url = canonicalize_url(href)
        items.append({
            "title": title,
            "url": can_url,
            "source": "FiveThirtyEight — Nate Silver",
            "published_utc": _to_iso_utc(pub_dt) or pub_dt.isoformat().replace("+00:00","Z"),
            "category": spec.tag.category,
            "region": spec.tag.region,
            "canonical_url": can_url,
            "canonical_id": canonical_id(can_url),
            "cluster_id": fuzzy_title_key(title),
            "age_hint_hours": age_hint if age_hint is not None else None,
        })
        if len(items) >= MAX_PER_FEED: break
    return items

def scrape_cp24(html: bytes, spec: FeedSpec, playoffs_on: bool) -> list[dict]:
    base_host = "www.cp24.com"
    items: list[dict] = []
    text = html.decode("utf-8", errors="ignore")

    def _cp24_extract_time(node) -> str | None:
        try:
            if not node or not BeautifulSoup:
                return None
            tnode = node.find("time")
            if tnode:
                txt = (tnode.get_text(" ", strip=True) or "").strip()
                rel_h = _hours_from_rel(txt)
                if rel_h is not None:
                    dt = datetime.now(timezone.utc) - timedelta(hours=rel_h)
                    return _to_iso_utc(dt)
                dt_s = tnode.get("datetime") or txt
                iso = parse_any_dt_str(dt_s)
                if iso:
                    return iso
        except Exception:
            return None
        return None

    def make_item(href: str, title: str, pub_iso: str | None) -> dict | None:
        if not href or not title:
            return None
        url = href.strip()
        if url.startswith("//"): url = "https:" + url
        if url.startswith("/"):  url = f"https://{base_host}{url}"
        if "cp24.com" not in url.lower(): return None
        ttl = strip_source_tail(title).strip()
        if len(ttl) < 6: return None
        if should_reject_title(ttl, playoffs_on): return None
        can_url = canonicalize_url(url)
        return {
            "title": ttl,
            "url": can_url,
            "source": "CP24",
            "published_utc": pub_iso or datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
            "category": spec.tag.category,
            "region": spec.tag.region,
            "canonical_url": can_url,
            "canonical_id": canonical_id(can_url),
            "cluster_id": fuzzy_title_key(ttl),
        }

    seen = set()
    if BeautifulSoup is not None:
        soup = BeautifulSoup(text, "html.parser")
        anchors = soup.find_all("a", href=True)
        for a in anchors:
            href = (a.get("href") or "").strip()
            if not href or href.startswith("#") or href.startswith("mailto:"): continue
            title = (a.get_text(" ", strip=True) or "").strip()
            pub_iso = _cp24_extract_time(a) or _cp24_extract_time(a.parent)
            it = make_item(href, title, pub_iso)
            if not it: continue
            if it["canonical_url"] in seen: continue
            seen.add(it["canonical_url"])
            items.append(it)
            if len(items) >= MAX_PER_FEED: break
        return items

    # Fallback regex
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, flags=re.I | re.S):
        href = (m.group(1) or "").strip()
        raw = re.sub(r"<[^>]+>", " ", m.group(2) or "")
        title = re.sub(r"\s+", " ", raw).strip()
        if not href or not title: continue
        it = make_item(href, title, None)
        if not it: continue
        if it["canonical_url"] in seen: continue
        seen.add(it["canonical_url"])
        items.append(it)
        if len(items) >= MAX_PER_FEED: break
    return items
# ---------- Link verification & market sanity ----------
def is_homepage_like(url: str) -> bool:
    try:
        u = urlparse(url)
        if u.path in ("","/"): return True
        if len(u.path.strip("/")) <= 2 and (not u.query):
            return True
        return False
    except Exception:
        return False

def html_meta_times(html_bytes: bytes) -> tuple[datetime | None, datetime | None]:
    if not BeautifulSoup:
        return (None, None)
    try:
        soup = BeautifulSoup(html_bytes, "html.parser")
        pub = None; upd = None
        for tag in soup.find_all("meta"):
            n = (tag.get("name") or tag.get("property") or "").lower()
            if n in ("article:published_time","og:published_time","date") and tag.get("content"):
                pub = _parse_dt_loose(tag.get("content"))
            if n in ("article:modified_time","og:updated_time","modified_time") and tag.get("content"):
                upd = _parse_dt_loose(tag.get("content"))
        return pub, upd
    except Exception:
        return (None, None)

def _parse_dt_loose(s: str) -> datetime | None:
    if not s: return None
    try:
        if s.endswith("Z"): return datetime.fromisoformat(s.replace("Z","+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        try:
            d = parsedate_to_datetime(s)
            return d
        except Exception:
            return None

def is_same_day(a: datetime | None, b: datetime | None) -> bool:
    if not a or not b: return False
    return a.date() == b.date()

def is_market_headline_sane(title: str, url: str, published_iso: str, session: requests.Session, debug_counts: dict) -> bool:
    t = title.lower()
    milestone = bool(re.search(r"\b(all[-\s]?time high|record|hits?\s*(?:\d{2,3},?\d{3}|[1-9]\d?k))\b", t))
    btc_round = bool(re.search(r"\bbitcoin|btc\b.*\b(20k|30k|40k|50k|60k|70k|80k|90k|100k)\b", t))
    if not (milestone or btc_round):
        return True

    age_h = hours_since(published_iso, time.time())
    if age_h > 12.0:
        debug_counts["market_sanity_drops"] += 1
        return False

    h = host_of(url)
    if any(h.endswith(d) for d in MARKET_AUTH_DOMAINS):
        return True

    if not VERIFY_LINKS:
        return False

    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT_S, allow_redirects=True)
        body = getattr(resp, "content", b"") or b""
        pub_meta, upd_meta = html_meta_times(body)
        pub_iso = datetime.fromisoformat(published_iso.replace("Z","+00:00"))
        if is_same_day(pub_meta, pub_iso) or is_same_day(upd_meta, pub_iso):
            return True
    except Exception:
        pass

    debug_counts["market_sanity_drops"] += 1
    return False

def verify_link(session: requests.Session, url: str, debug_counts: dict) -> tuple[bool, str, int, str]:
    if not VERIFY_LINKS:
        return True, url, 200, "verification disabled"

    try:
        headers = {
            "User-Agent": ALT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": ACCEPT_LANG,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        resp = session.get(url, timeout=HTTP_TIMEOUT_S, allow_redirects=True, headers=headers)
    except Exception as e:
        debug_counts["link_verification_fail"] += 1
        return False, url, 0, f"exception:{type(e).__name__}"

    status = getattr(resp, "status_code", 0)
    final_url = str(getattr(resp, "url", url) or url)
    body = getattr(resp, "content", b"") or b""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    robots = (resp.headers.get("X-Robots-Tag") or "").lower()
    host_final = host_of(final_url)

    min_bytes_early = MIN_BODY_BYTES
    if is_sports_domain(host_final):
        min_bytes_early = max(1500, int(MIN_BODY_BYTES * 0.4))

    if status < 200 or status >= 300:
        debug_counts["link_verification_fail"] += 1
        return False, final_url, status, "non-2xx"
    if ("text/html" not in ctype) and ("application/xhtml" not in ctype):
        debug_counts["link_verification_fail"] += 1
        return False, final_url, status, f"bad-ctype:{ctype}"
    if len(body) < min_bytes_early:
        debug_counts["soft_404_drops"] += 1
        return False, final_url, status, "body-too-small"

    if REJECT_REDIRECT_TO_HOMEPAGE and is_homepage_like(final_url):
        debug_counts["soft_404_drops"] += 1
        return False, final_url, status, "homepage-like"

    title_text = ""
    og_url = ""
    canonical = ""
    og_type = ""
    text_for_search = ""
    word_count = 0

    soft404_regex = re.compile("|".join(SOFT_404_PATTERNS), re.I)
    hint_re = re.compile("|".join(ARTICLE_HINT_PATTERNS), re.I)

    if BeautifulSoup:
        try:
            soup = BeautifulSoup(body, "html.parser")

            if soup.title and soup.title.string:
                title_text = (soup.title.string or "").strip()

            can_tag = soup.find("link", rel=lambda v: v and "canonical" in v)
            if can_tag and can_tag.get("href"):
                canonical = can_tag.get("href").strip()
            og_tag = soup.find("meta", property="og:url")
            if og_tag and og_tag.get("content"):
                og_url = og_tag.get("content").strip()
            og_type_tag = soup.find("meta", property="og:type")
            if og_type_tag and og_type_tag.get("content"):
                og_type = (og_type_tag.get("content") or "").strip().lower()

            meta_robots = soup.find("meta", attrs={"name": re.compile(r"robots", re.I)})
            if ("noindex" in robots) or (meta_robots and "noindex" in (meta_robots.get("content") or "").lower()):
                debug_counts["soft_404_drops"] += 1
                return False, final_url, status, "robots-noindex"

            for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form"]):
                tag.decompose()
            text_for_search = " ".join((soup.get_text(" ", strip=True) or "").split())
            word_count = len(re.findall(r"\w+", text_for_search))

            if soft404_regex.search(text_for_search) or soft404_regex.search((title_text or "").lower()):
                debug_counts["soft_404_drops"] += 1
                return False, final_url, status, "soft-404-text"

            if canonical:
                cu = urlparse(canonical)
                if cu.netloc and cu.netloc != urlparse(final_url).netloc:
                    debug_counts["soft_404_drops"] += 1
                    return False, final_url, status, "canonical-cross-host"
                if is_homepage_like(canonical):
                    debug_counts["soft_404_drops"] += 1
                    return False, final_url, status, "canonical-homepage"

            if og_url:
                ou = urlparse(og_url)
                if ou.netloc and ou.netloc != urlparse(final_url).netloc:
                    debug_counts["soft_404_drops"] += 1
                    return False, final_url, status, "ogurl-cross-host"
                if is_homepage_like(og_url):
                    debug_counts["soft_404_drops"] += 1
                    return False, final_url, status, "ogurl-homepage"

            is_sports_final = is_mlb_final_title(title_text) or is_sports_domain(host_final)
            min_words = MIN_ARTICLE_WORDS
            if is_sports_final:
                min_words = max(40, int(MIN_ARTICLE_WORDS * 0.3))

            hints_ok = (og_type == "article") or bool(hint_re.search(str(body)))
            if not hints_ok or word_count < min_words:
                debug_counts["soft_404_drops"] += 1
                return False, final_url, status, f"not-article-like:{word_count}w"

        except Exception:
            if len(body) < max(MIN_BODY_BYTES * 2, 8192) or is_homepage_like(final_url):
                debug_counts["soft_404_drops"] += 1
                return False, final_url, status, "parse-fail-small-body"
    else:
        try:
            sample = (body[:120000] or b"").decode("utf-8", errors="ignore")
            if soft404_regex.search(sample):
                debug_counts["soft_404_drops"] += 1
                return False, final_url, status, "soft-404-text(minimal)"
        except Exception:
            pass

    return True, final_url, status, "ok"

# ---------- SAFETY-NET HELPERS ----------
def _fallback_feeds_iter() -> list[str]:
    return FALLBACK_FEEDS if FALLBACK_FEEDS else DEFAULT_FALLBACK_FEEDS

def _fallback_pick_from_feed(session: requests.Session, feed_url: str, debug_counts: dict) -> dict | None:
    try:
        blob = http_get(session, feed_url)
        if not blob:
            return None
        parsed = feedparser.parse(blob)
        entries = parsed.entries[:8]
    except Exception:
        return None

    for e in entries:
        title = (e.get("title") or "").strip()
        link  = (e.get("link")  or "").strip()
        if not title or not link:
            continue
        pub = pick_published(e)
        if not pub:
            continue

        age_h = hours_since(pub, time.time())
        if age_h < (MIN_AGE_SEC / 3600.0) or age_h > FALLBACK_MAX_AGE_HOURS:
            continue

        can_url = canonicalize_url(link)
        it = {
            "title": title,
            "url":   can_url or link,
            "source": (parsed.feed.get("title") or host_of(can_url or link) or "").strip(),
            "published_utc": pub,
            "category": "General",
            "region":   "World",
            "canonical_url": can_url or link,
            "canonical_id":  canonical_id(can_url or link),
            "cluster_id":    fuzzy_title_key(title),
            "score": 0.0,
            "score_components": {},
            "effects": {},
        }

        ok, final_url, status, reason = verify_link(session, it["url"], debug_counts)
        if not ok:
            continue
        if not is_market_headline_sane(it["title"], final_url, it["published_utc"], session, debug_counts):
            continue

        it["url"] = final_url
        it["canonical_url"] = final_url
        return it

    return None

def _safety_net_one_headline(session: requests.Session, debug_counts: dict) -> tuple[dict | None, dict]:
    stats = {"used": False, "feed": None, "title": None}
    for f in _fallback_feeds_iter():
        it = _fallback_pick_from_feed(session, f, debug_counts)
        if it:
            stats["used"] = True
            stats["feed"] = f
            stats["title"] = it["title"]
            return it, stats
    return None, stats

# ---------- Build ----------
def build(feeds_file: str, out_path: str) -> dict:
    start = time.time()
    weights, weights_debug = load_weights()
    labour_hints = load_labour_hints(weights or {})

    specs = parse_feeds_txt(feeds_file)

    collected: list[dict] = []
    per_host_counts: dict[str,int] = {}

    slow_domains: dict[str, int] = {}
    feed_times: list[tuple[str, float, int]] = []
    timeouts: list[str] = []
    errors: list[str] = []
    caps_hit: list[str] = []

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
        "substack_tagged": 0,
        "sports_team_hits": 0,
        "sports_player_hits": 0,
        "sports_result_win_hits": 0,
        "sports_result_loss_hits": 0,
        "sports_evening_hits": 0,
        "sports_playoff_hits": 0,
        "sports_focus_team_hits": 0,
        "sports_final_hits": 0,
        "sports_final_score_hits": 0,
        "hype_politics": 0,
        "hype_conflict": 0,
        "hype_markets": 0,
        "hype_jobs": 0,
        "hype_sports": 0,
        "hype_ceasefire": 0,
        "reject_hits": 0,
        # NEW debug:
        "obit_hits": 0,
        "major_figure_hits": 0,
        "business_am_hits": 0,
    }

    debug_counts = {
        "link_verification_fail": 0,
        "soft_404_drops": 0,
        "max_age_drops": 0,
        "min_age_drops": 0,
        "market_sanity_drops": 0,
        "backfill_steps_used": 0
    }

    session = _new_session()

    tz_name = os.getenv("NEWSRIVER_TIMEZONE", "America/Toronto")
    now_et = now_in_tz(tz_name)
    evening_start = now_et.replace(hour=18, minute=30, second=0, microsecond=0)
    evening_end   = now_et.replace(hour=22, minute=30, second=0, microsecond=0)
    in_evening = (evening_start <= now_et <= evening_end)

    # NEW: Morning business window (06:00–12:00 ET)
    morning_start = now_et.replace(hour=6,  minute=0,  second=0, microsecond=0)
    morning_end   = now_et.replace(hour=12, minute=0,  second=0, microsecond=0)
    in_morning = (morning_start <= now_et <= morning_end)

    playoffs_on = os.getenv("MPB_PLAYOFFS", "0") == "1"

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
            if dt >= HTTP_TIMEOUT_S - 0.1:
                timeouts.append(h_feed)
                print(f"[timeout] {h_feed} ({spec.url}) ~{dt:.1f}s")
            else:
                errors.append(h_feed)
                print(f"[error]   {h_feed} ({spec.url}) ~{dt:.1f}s (no content)")
            continue

        if dt > SLOW_FEED_WARN_S:
            slow_domains[h_feed] = slow_domains.get(h_feed, 0) + 1
            print(f"[slow]    {h_feed} took {dt:.2f}s")

        # ---- HTML scrapers (CP24/Nate) ----
        if h_feed.endswith("cp24.com"):
            try:
                scraped = scrape_cp24(blob, spec, playoffs_on)
                for it in scraped:
                    if should_reject_title(it["title"], playoffs_on):
                        score_dbg["reject_hits"] += 1
                        continue
                    tag_labour_if_applicable(it, labour_hints)

                    h = host_of(it["url"])
                    cap = PER_HOST_MAX.get(h, MAX_PER_FEED)
                    if in_evening and h in SPORTS_PRIOR_DOMAINS and cap < 10: cap = 10
                    if per_host_counts.get(h, 0) >= cap:
                        if h and h not in caps_hit: caps_hit.append(h)
                        continue
                    collected.append(it); per_host_counts[h] = per_host_counts.get(h, 0) + 1; kept_from_feed += 1
                feed_times.append((h_feed, dt, kept_from_feed))
                continue
            except Exception as e:
                errors.append(h_feed)
                print(f"[scrape]  error cp24 {h_feed}: {e}")

        if "fivethirtyeight.com/contributors/nate-silver" in spec.url:
            try:
                scraped = scrape_nate_silver(blob, spec, playoffs_on)
                if scraped:
                    for it in scraped:
                        if should_reject_title(it["title"], playoffs_on):
                            score_dbg["reject_hits"] += 1
                            continue
                        tag_labour_if_applicable(it, labour_hints)

                        h = host_of(it["url"])
                        cap = PER_HOST_MAX.get(h, MAX_PER_FEED)
                        if in_evening and h in SPORTS_PRIOR_DOMAINS and cap < 10: cap = 10
                        if per_host_counts.get(h, 0) >= cap:
                            if h and h not in caps_hit: caps_hit.append(h)
                            continue
                        collected.append(it); per_host_counts[h] = per_host_counts.get(h, 0) + 1; kept_from_feed += 1
                    feed_times.append((h_feed, dt, kept_from_feed))
                    continue
            except Exception as e:
                errors.append(h_feed)
                print(f"[scrape]  error nate {h_feed}: {e}")

        # ---- Normal RSS/Atom path ----
        entries = []
        parsed_ok = False
        try:
            parsed = feedparser.parse(blob)
            entries = parsed.entries[:MAX_PER_FEED]
            parsed_ok = True
        except Exception as e:
            errors.append(h_feed)
            print(f"[parse]   error {h_feed}: {e}")

        for e in entries:
            title = (e.get("title") or "").strip()
            if should_reject_title(title, playoffs_on):
                score_dbg["reject_hits"] += 1
                continue
            link  = (e.get("link")  or "").strip()
            if not title or not link: continue
            can_url = canonicalize_url(link)
            h = host_of(can_url or link)
            cap = PER_HOST_MAX.get(h, MAX_PER_FEED)
            if in_evening and h in SPORTS_PRIOR_DOMAINS and cap < 10: cap = 10
            if per_host_counts.get(h, 0) >= cap:
                if h and h not in caps_hit: caps_hit.append(h)
                continue

            source_label = (parsed.feed.get("title") or h or "").strip() if parsed_ok else (h or "").strip()
            if h == MPB_SUBSTACK_HOST: source_label = "MyPyBiTE Substack"

            pub = pick_published(e)
            if not pub:
                continue

            item = {
                "title": title,
                "url":   can_url or link,
                "source": source_label,
                "published_utc": pub,
                "category": spec.tag.category,
                "region":   spec.tag.region,
                "canonical_url": can_url or link,
                "canonical_id":  canonical_id(can_url or link),
                "cluster_id":    fuzzy_title_key(title),
            }

            if "summary" in e and isinstance(e["summary"], str):
                item["summary"] = e["summary"]
            elif "description" in e and isinstance(e["description"], str):
                item["summary"] = e["description"]
            tag_labour_if_applicable(item, labour_hints)

            collected.append(item)
            per_host_counts[h] = per_host_counts.get(h, 0) + 1
            kept_from_feed += 1

        feed_times.append((h_feed, dt, kept_from_feed))
        if (idx % 20) == 0:
            elapsed = time.time() - start
            print(f"[progress] {idx}/{len(specs)} feeds, items={len(collected)}, elapsed={elapsed:.1f}s")

    # ---- Dedup pass 1: newest/non-aggregator per cluster ----
    first_pass: dict[str,dict] = {}
    for it in collected:
        key = it["cluster_id"]
        prev = first_pass.get(key)
        if not prev:
            first_pass[key] = it; continue
        t_new, t_old = _ts(it["published_utc"]), _ts(prev["published_utc"])
        if t_new > t_old:
            first_pass[key] = it
        elif t_new == t_old:
            if looks_aggregator(prev.get("source",""), prev.get("url","")) and not looks_aggregator(it.get("source",""), it.get("url","")):
                first_pass[key] = it
    items = list(first_pass.values())

    # ---- Dedup pass 2: near-duplicate by Jaccard ----
    survivors: list[dict] = []
    token_cache: list[Tuple[set[str], dict]] = []
    THRESH = 0.82
    now_ts = time.time()

    def is_better(a: dict, b: dict) -> bool:
        ta, tb = _ts(a["published_utc"]), _ts(b["published_utc"])
        if ta != tb: return ta > tb
        a_aggr = looks_aggregator(a.get("source",""), a.get("url",""))
        b_aggr = looks_aggregator(b.get("source",""), b.get("url",""))
        if a_aggr != b_aggr: return not a_aggr
        ha, hb = host_of(a["url"]), host_of(b["url"])
        pref = lambda h: any((h or "").endswith(d) for d in PREFERRED_DOMAINS)
        if pref(ha) != pref(hb): return pref(ha)
        return len(a["url"]) < len(b["url"])

    def _is_jays_game_title(it: dict) -> bool:
        t = it.get("title","")
        team = bool(RE_JAYS_TEAM.search(t))
        resultish = bool(RE_JAYS_WIN.search(t) or RE_JAYS_LOSS.search(t) or RE_MLB_FINAL_WORD.search(t) or RE_SCORELINE.search(t))
        return bool(t) and team and resultish

    def _is_focus_mlb_final(it: dict) -> bool:
        t = it.get("title","")
        team = bool(RE_MLB_TEAMS.search(t))
        finalish = bool(RE_MLB_FINAL_WORD.search(t) or RE_SCORELINE.search(t) or RE_JAYS_WIN.search(t) or RE_JAYS_LOSS.search(t))
        return bool(t) and team and finalish

    for it in items:
        toks = set(title_tokens(it["title"]))
        merged = False
        for toks_other, rep in token_cache:
            if (_is_jays_game_title(it) and _is_jays_game_title(rep)) or (_is_focus_mlb_final(it) and _is_focus_mlb_final(rep)):
                if hours_since(it["published_utc"], now_ts) < 4.0 or hours_since(rep["published_utc"], now_ts) < 4.0:
                    continue
            if jaccard(toks, toks_other) >= THRESH:
                if is_better(it, rep):
                    survivors.remove(rep); survivors.append(it)
                    token_cache.remove((toks_other, rep)); token_cache.append((toks, it))
                merged = True
                break
        if not merged:
            survivors.append(it)
            token_cache.append((toks, it))

    # cluster metadata
    cluster_groups: dict[str, list[dict]] = {}
    for it in survivors:
        cluster_groups.setdefault(it["cluster_id"], []).append(it)
    for cid, arr in cluster_groups.items():
        arr.sort(key=lambda x: _ts(x["published_utc"]))
        for i, it in enumerate(arr):
            it["cluster_rank"] = i + 1
            it["cluster_latest"] = (i == len(arr) - 1)

    # --------- Scoring ---------
    half_life_h = float(W(weights, "recency.half_life_hours", 6.0))
    age_pen_24  = float(W(weights, "recency.age_penalty_after_24h", -0.6))
    age_pen_36  = float(W(weights, "recency.age_penalty_after_36h", -0.4))
    superseded_pen = float(W(weights, "recency.superseded_cluster_penalty", -0.9))
    cat_table   = dict(W(weights, "categories", {}))
    agg_pen     = float(W(weights, "sources.aggregator_penalty", -0.5))
    wire_pen    = float(W(weights, "sources.press_wire_penalty", -0.4))
    pref_bonus  = float(W(weights, "sources.preferred_domains_bonus", 0.25))

    ps_has_fatal = float(W(weights, "public_safety.has_fatality_points", 1.0))
    ps_per_death = float(W(weights, "public_safety.per_death_points", 0.10))
    ps_max_death = float(W(weights, "public_safety.max_death_points", 2.0))
    # FIXED: read the per-injury increment, not the cap
    ps_per_inj   = float(W(weights, "public_safety.per_injury_points", 0.05))
    ps_max_inj   = float(W(weights, "public_safety.max_injury_points", 1.0))
    ps_kw_bonus  = float(W(weights, "public_safety.violent_keywords_bonus", 0.2))
    ps_kw_list   = [k.lower() for k in W(weights, "public_safety.violent_keywords", [])]

    btc_thr   = float(W(weights, "markets.btc_abs_move_threshold_pct", 7.0))
    btc_pts   = float(W(weights, "markets.btc_points", 1.6))
    idx_thr   = float(W(weights, "markets.index_abs_move_threshold_pct", 1.0))
    idx_pts   = float(W(weights, "markets.index_points", 1.0))
    nik_thr   = float(W(weights, "markets.nikkei_abs_move_threshold_pct", 1.0))
    nik_pts   = float(W(weights, "markets.nikkei_points", 0.7))
    stk_thr   = float(W(weights, "markets.single_stock_abs_move_threshold_pct", 10.0))
    stk_pts   = float(W(weights, "markets.single_stock_points", 1.2))

    ls_min    = float(W(weights, "effects.lightsaber_min_score", 2.5))
    also_body = int(W(weights, "effects.lightsaber_also_if.body_count_ge", 5))
    also_btc  = float(W(weights, "effects.lightsaber_also_if.btc_abs_move_ge_pct", 8.0))
    also_stk  = float(W(weights, "effects.lightsaber_also_if.single_stock_abs_move_ge_pct", 15.0))
    glitch_min= float(W(weights, "effects.glitch_min_score", 1.8))

    nate_bonus           = float(W(weights, "reorder.nate_hours_hint_bonus", 0.25))
    nate_bonus_max_hours = float(W(weights, "reorder.nate_hours_hint_max_hours", 6.0))

    sp_team        = float(W(weights, "sports.team_match_points", 0.80))
    sp_player      = float(W(weights, "sports.player_match_points", 0.35))
    sp_win         = float(W(weights, "sports.result_win_points", 0.45))
    sp_loss        = float(W(weights, "sports.result_loss_points", 0.25))
    sp_evening     = float(W(weights, "sports.evening_window_points", 0.70))
    sp_playoffs    = float(W(weights, "sports.playoff_mode_points", 0.40))
    sp_focus_team  = float(W(weights, "sports.focus_team_points", 0.55))
    sp_final_story = float(W(weights, "sports.final_story_points", 0.75))
    sp_final_score = float(W(weights, "sports.final_with_score_points", 0.45))

    # Regional/Toronto weights
    regional_country_pts = float(W(weights, "regional.weights.country_match", 1.2))
    regional_city_pts    = float(W(weights, "regional.weights.city_match_toronto", 0.0))
    regional_max_bonus   = float(W(weights, "regional.max_bonus", 2.4))

    # NEW: AM business bias + major figure bump (weights with sane defaults)
    business_am_pts      = float(W(weights, "daypart.business_am_points", 0.6))  # small; ~equiv to +2–3h frozen
    major_figure_pts     = float(W(weights, "salience.major_figure_points", 0.6))
    business_kw = re.compile(
        r"\b(earnings|eps|revenue|guidance|forecast|outlook|dividend|acquisition|merger|ipo|offering|"
        r"inflation|cpi|ppi|jobs|unemployment|payrolls|bank of canada|boc|federal reserve|fed|rate|hike|cut|"
        r"tsx|tsxv|canadian dollar|loonie|cad|stock|shares|bond|yields|crypto|bitcoin|btc|ethereum|eth)\b",
        re.I
    )
    major_figures_re = re.compile(
        r"\b(joe biden|donald trump|barack obama|kamala harris|dick cheney|al gore|mike pence|"
        r"justin trudeau|stephen harper|jean chrétien|brian mulroney|paul martin|"
        r"doug ford|kathleen wynne|rachel notley|danielle smith|tony blair|gordon brown|emmanuel macron|angela merkel)\b",
        re.I
    )

    now_ts_scoring = time.time()

    def violent_kw_hit(title: str) -> bool:
        t = title.lower()
        return any(kw in t for kw in ps_kw_list)

    def apply_scoring(it: dict) -> None:
        title = it.get("title",""); url = it.get("url",""); host = host_of(url)
        category = it.get("category","General"); published = it.get("published_utc","")
        comps = {}; total = 0.0

        age_h = hours_since(published, now_ts_scoring)
        decay = 0.0
        if half_life_h > 0: decay = 1.0 * (0.5 ** (age_h / half_life_h))
        comps["recency"] = round(decay, 4); total += decay

        age_pen = 0.0
        if age_h > 24: age_pen += age_pen_24
        if age_h > 36: age_pen += age_pen_36
        if not it.get("cluster_latest", True): age_pen += superseded_pen
        if age_pen: comps["age_penalty"] = round(age_pen, 4); total += age_pen

        cat_bonus = float(cat_table.get(category, 0.0))
        if cat_bonus: comps["category"] = round(cat_bonus, 4); total += cat_bonus

        if looks_aggregator(it.get("source",""), url):
            comps["aggregator_penalty"] = agg_pen; total += agg_pen; score_dbg["agg_penalties"] += 1
        if is_press_wire(url):
            comps["press_wire_penalty"] = wire_pen; total += wire_pen; score_dbg["press_penalties"] += 1
        if any((host or "").endswith(d) for d in PREFERRED_DOMAINS):
            comps["preferred_domain"] = pref_bonus; total += pref_bonus; score_dbg["preferred_bonus"] += 1

        # Public safety + obituary urgency
        deaths, injured, has_fatal_cue = parse_casualties(title)
        obit_hit = bool(RE_OBIT_URGENCY.search(title))
        if obit_hit:
            score_dbg["obit_hits"] += 1
        it["_ps_deaths"] = deaths
        it["_ps_injured"] = injured
        it["_ps_has_fatal"] = has_fatal_cue or obit_hit
        it["is_urgent"] = bool(it["_ps_has_fatal"])  # exposes urgency to UI if needed

        ps_score = 0.0
        if it["_ps_has_fatal"]: ps_score += ps_has_fatal; score_dbg["ps_fatal_hits"] += 1
        if deaths > 0:  ps_score += min(ps_max_death, ps_per_death * deaths)
        if injured > 0: ps_score += min(ps_max_inj,   ps_per_inj   * injured); score_dbg["ps_injury_hits"] += 1
        if violent_kw_hit(title): ps_score += ps_kw_bonus
        if ps_score: comps["public_safety"] = round(ps_score, 4); total += ps_score

        btc_move = None
        m = RE_BTC.search(title)
        if m:
            v = first_pct(m)
            if v is not None: btc_move = v
            if v is not None and v >= btc_thr:
                comps["btc_trigger"] = btc_pts; total += btc_pts; score_dbg["market_btc_hits"] += 1
        it["_btc_move_abs"] = btc_move

        m = RE_IDX.search(title)
        if m:
            v = first_pct(m)
            if v is not None and v >= idx_thr:
                comps["index_trigger"] = idx_pts; total += idx_pts; score_dbg["market_index_hits"] += 1

        m = RE_NIK.search(title)
        if m:
            v = first_pct(m)
            if v is not None and v >= nik_thr:
                comps["nikkei_trigger"] = nik_pts; total += nik_pts; score_dbg["market_nikkei_hits"] += 1

        single_move = None
        m = RE_TICK_PCT.search(title)
        if m:
            try: single_move = abs(float(m.group(2)))
            except Exception: single_move = None
        if single_move is not None and single_move >= stk_thr:
            comps["single_stock_trigger"] = stk_pts; total += stk_pts; score_dbg["market_single_hits"] += 1
        it["_single_move_abs"] = single_move

        # Regional bonuses
        reg_bonus = 0.0
        if it.get("region") == "Canada":
            reg_bonus += regional_country_pts
        if regional_city_pts > 0.0:
            if RE_TORONTO_CUES.search(title) or "toronto" in (host or "") or "/toronto" in path_of(url).lower():
                reg_bonus += regional_city_pts
        if reg_bonus:
            reg_bonus = min(reg_bonus, regional_max_bonus)
            comps["regional"] = round(reg_bonus, 4); total += reg_bonus

        # Nate hours hint bonus (unchanged)
        ah = it.get("age_hint_hours", None)
        if ah is not None and ah <= nate_bonus_max_hours:
            comps["nate_hours_hint_bonus"] = nate_bonus; total += nate_bonus

        # Sports bonuses (unchanged)
        team_hit   = bool(RE_JAYS_TEAM.search(title))
        player_hit = bool(RE_JAYS_PLAYERS.search(title))
        win_hit    = bool(RE_JAYS_WIN.search(title))
        loss_hit   = bool(RE_JAYS_LOSS.search(title))

        focus_team_hit = bool(RE_MLB_TEAMS.search(title))
        final_hit      = bool(RE_MLB_FINAL_WORD.search(title) or RE_SCORELINE.search(title))
        scoreline_hit  = bool(RE_SCORELINE.search(title))

        if team_hit:
            comps["sports.team_match"] = sp_team; total += sp_team; score_dbg["sports_team_hits"] += 1
        if focus_team_hit and not team_hit:
            comps["sports.focus_team"] = sp_focus_team; total += sp_focus_team; score_dbg["sports_focus_team_hits"] += 1
        if player_hit:
            comps["sports.player_match"] = sp_player; total += sp_player; score_dbg["sports_player_hits"] += 1
        if win_hit:
            comps["sports.result_win"] = sp_win; total += sp_win; score_dbg["sports_result_win_hits"] += 1
        elif loss_hit:
            comps["sports.result_loss"] = sp_loss; total += sp_loss; score_dbg["sports_result_loss_hits"] += 1
        if focus_team_hit and final_hit:
            comps["sports.final_story"] = sp_final_story; total += sp_final_story; score_dbg["sports_final_hits"] += 1
            if scoreline_hit:
                comps["sports.final_with_score"] = sp_final_score; total += sp_final_score; score_dbg["sports_final_score_hits"] += 1
        if (team_hit or focus_team_hit) and (ah is None):
            if (now_et.hour, now_et.minute) >= (18,30) and (now_et.hour, now_et.minute) <= (22,30):
                comps["sports.evening_window"] = sp_evening; total += sp_evening; score_dbg["sports_evening_hits"] += 1
        if playoffs_on and (team_hit or focus_team_hit) and (win_hit or loss_hit or final_hit):
            comps["sports.playoff_mode"] = sp_playoffs; total += sp_playoffs; score_dbg["sports_playoff_hits"] += 1

        # NEW: Morning business bias (06:00–12:00 ET), small & meaningful
        if in_morning:
            if business_kw.search(title) or any((host or "").endswith(d) for d in ("theglobeandmail.com","financialpost.com","bloomberg.com","reuters.com","apnews.com")):
                comps["daypart.business_am"] = business_am_pts
                total += business_am_pts
                score_dbg["business_am_hits"] += 1

        # NEW: Major figure bump (tiny roster); stronger if paired with obit terms
        if major_figures_re.search(title):
            bump = major_figure_pts
            if obit_hit:
                bump += 0.25  # gentle extra nudge on obituary
            comps["salience.major_figure"] = round(bump, 4)
            total += bump
            score_dbg["major_figure_hits"] += 1

        # Effects tagging
        effects = {"lightsaber": False, "glitch": False, "reasons": []}
        if total >= ls_min: effects["lightsaber"] = True; effects["reasons"].append(f"score≥{ls_min}")
        if it.get("_ps_deaths", 0) >= also_body: effects["lightsaber"] = True; effects["reasons"].append(f"body_count≥{also_body}")
        if it.get("_btc_move_abs") is not None and it["_btc_move_abs"] >= also_btc:
            effects["lightsaber"] = True; effects["reasons"].append(f"btc_move≥{also_btc}%")
        if it.get("_single_move_abs") is not None and it["_single_move_abs"] >= also_stk:
            effects["lightsaber"] = True; effects["reasons"].append(f"single_stock_move≥{also_stk}%")
        if not effects["lightsaber"] and total >= glitch_min:
            effects["glitch"] = True; effects["reasons"].append(f"score≥{glitch_min}")

        if host_of(url) == MPB_SUBSTACK_HOST:
            effects["glitch"] = True
            if not effects["lightsaber"]: effects["reasons"].append("substack")
            effects["decay_at"] = iso_add_hours(it.get("published_utc"), 24.0)
            score_dbg["substack_tagged"] += 1

        style = "lightsaber" if effects["lightsaber"] else ("glitch" if effects["glitch"] else "")
        if style: effects["style"] = style

        it["score"] = round(total, 4)
        it["score_components"] = comps
        it["effects"] = effects

    for it in survivors:
        apply_scoring(it)

    # ---- Sort by recency then score, initial trim ----
    survivors.sort(key=lambda x: (_ts(x["published_utc"]), x.get("score", 0.0)), reverse=True)
    survivors = survivors[:MAX_TOTAL]
    # ---- BREAKERS ----
    def breaker_score(it: dict) -> tuple:
        title = it.get("title","")
        score = float(it.get("score", 0.0))
        age_h = hours_since(it.get("published_utc",""), time.time())
        recency_boost = max(0.0, 24.0 - age_h) / 24.0
        urgent = 1.0 if (RE_BREAKING.search(title) or CONFLICT_CUES.search(title) or RE_OBIT_URGENCY.search(title)) else 0.0
        safety = 1.0 if (it.get("_ps_deaths",0) > 0 or it.get("_ps_has_fatal")) else 0.0
        markets = 1.0 if ((it.get("_btc_move_abs") or 0) >= 8.0 or ((it.get("_single_move_abs") or 0) >= 15.0)) else 0.0
        saber = 1.0 if it.get("effects",{}).get("lightsaber") else 0.0
        return (urgent + safety + markets + saber + recency_boost, score, _ts(it.get("published_utc","")))
    for i, it in enumerate(sorted(survivors, key=breaker_score, reverse=True)):
        if i >= BREAKER_LIMIT: break
        if looks_aggregator(it.get("source",""), it.get("url","")):
            continue
        it.setdefault("effects", {})
        it["effects"]["style"] = "breaker"

    # ---- Hard filters & verification ----
    def within_age_bounds(it: dict) -> bool:
        age_h = hours_since(it.get("published_utc",""), time.time())
        if age_h > MAX_AGE_HOURS:
            debug_counts["max_age_drops"] += 1
            return False
        if age_h < (MIN_AGE_SEC / 3600.0):
            debug_counts["min_age_drops"] += 1
            return False
        return True

    verified: list[dict] = []
    for it in survivors:
        if not within_age_bounds(it):
            continue
        ok, final_url, status, reason = verify_link(session, it["url"], debug_counts)
        if not ok:
            continue
        if not is_market_headline_sane(it["title"], final_url, it["published_utc"], session, debug_counts):
            continue
        it["url"] = final_url
        it["canonical_url"] = final_url
        verified.append(it)

    # ---- SAFETY-NET INJECTION ----
    fallback_stats = {"used": False, "feed": None, "title": None}
    if FALLBACK_MIN_ITEMS > 0 and len(verified) < 1:
        picked, stats = _safety_net_one_headline(session, debug_counts)
        fallback_stats.update(stats)
        if picked:
            verified.insert(0, picked)

    # ---- Backfill to EXACT 33 if needed ----
    def backfill_exact(keep: list[dict], candidates: Iterable[dict]) -> list[dict]:
        want = REQUIRE_EXACT_COUNT
        if want <= 0: return keep
        if len(keep) >= want: return keep[:want]

        pool: list[dict] = []
        seen_ids = {x["canonical_id"] for x in keep}
        seen_urls = {x["canonical_url"] for x in keep}
        BF_THRESH = 0.78

        def looks_distinct(a: dict, b: dict) -> bool:
            return jaccard(set(title_tokens(a["title"])), set(title_tokens(b["title"]))) < BF_THRESH

        for it in sorted(list(candidates), key=lambda x: _ts(x.get("published_utc","")), reverse=True):
            if it["canonical_id"] in seen_ids or it["canonical_url"] in seen_urls:
                continue
            if not within_age_bounds(it):
                continue
            ok, final_url, status, reason = verify_link(session, it["url"], debug_counts)
            if not ok:
                continue
            if not is_market_headline_sane(it["title"], final_url, it["published_utc"], session, debug_counts):
                continue
            if looks_aggregator(it.get("source",""), final_url):
                continue
            it["url"] = final_url
            it["canonical_url"] = final_url

            distinct = True
            for k in keep:
                if not looks_distinct(it, k):
                    distinct = False
                    break
            if distinct:
                pool.append(it)
            if len(pool) >= (want - len(keep)) * 2:
                break

        out = keep[:]
        for it in pool:
            out.append(it)
            if len(out) >= want:
                break
        return out

    final_candidates_source = [x for x in survivors if x not in verified] + items + collected
    if REQUIRE_EXACT_COUNT > 0 and len(verified) < REQUIRE_EXACT_COUNT:
        verified = backfill_exact(verified, final_candidates_source)
        if len(verified) < REQUIRE_EXACT_COUNT:
            debug_counts["backfill_steps_used"] = len(verified)
            print(f"[finalize] could not reach {REQUIRE_EXACT_COUNT} verified items within {MAX_AGE_HOURS}h window.")
        else:
            debug_counts["backfill_steps_used"] = REQUIRE_EXACT_COUNT

    # ---- Toronto-first minimum + city backfill ----
    def toronto_hit(it: dict) -> bool:
        t = it.get("title","")
        u = it.get("url","")
        s = (it.get("source","") or "").lower()
        h = host_of(u)
        p = path_of(u).lower()
        return bool(
            RE_TORONTO_CUES.search(t) or
            "toronto" in (h or "") or "/toronto" in p or
            "cp24" in s or "citynews" in s or "blogto" in s or "thestar" in s
        )

    def city_hit(it: dict, city: str) -> bool:
        c = city.lower()
        t = (it.get("title","") or "").lower()
        u = it.get("url","") or ""
        h = (host_of(u) or "").lower()
        p = (path_of(u) or "").lower()
        return (c in t) or (f"/{c}" in p) or (c in h)

    def enforce_toronto_min(arr: list[dict], candidates: Iterable[dict]) -> list[dict]:
        want = max(0, MPB_MIN_TORONTO)
        if want == 0: return arr
        have = [it for it in arr if toronto_hit(it)]
        if len(have) >= want:
            return arr

        seen_ids = {x["canonical_id"] for x in arr}
        seen_urls = {x["canonical_url"] for x in arr}
        BF_THRESH = 0.78

        def looks_distinct(a: dict, b: dict) -> bool:
            return jaccard(set(title_tokens(a["title"])), set(title_tokens(b["title"]))) < BF_THRESH

        pool: list[dict] = []
        for it in sorted(list(candidates), key=lambda x: _ts(x.get("published_utc","")), reverse=True):
            if len(have) + len(pool) >= want:
                break
            if it["canonical_id"] in seen_ids or it["canonical_url"] in seen_urls:
                continue
            if not toronto_hit(it):
                continue
            if not within_age_bounds(it):
                continue
            ok, final_url, status, reason = verify_link(session, it["url"], debug_counts)
            if not ok:
                continue
            it["url"] = final_url
            it["canonical_url"] = final_url
            if any(not looks_distinct(it, k) for k in arr):
                continue
            pool.append(it)

        out = arr[:] + pool
        need_more = want - len([it for it in out if toronto_hit(it)])
        if need_more > 0:
            for city in CITY_BACKFILL_ORDER:
                for it in sorted(list(candidates), key=lambda x: _ts(x.get("published_utc","")), reverse=True):
                    if need_more <= 0:
                        break
                    if it["canonical_id"] in {x["canonical_id"] for x in out}:
                        continue
                    if not city_hit(it, city):
                        continue
                    if not within_age_bounds(it):
                        continue
                    ok, final_url, status, reason = verify_link(session, it["url"], debug_counts)
                    if not ok:
                        continue
                    it["url"] = final_url
                    it["canonical_url"] = final_url
                    if any(jaccard(set(title_tokens(it["title"])), set(title_tokens(k["title"]))) >= 0.78 for k in out):
                        continue
                    out.append(it)
                    need_more -= 1
                if need_more <= 0:
                    break

        return out

    if REQUIRE_EXACT_COUNT > 0:
        verified = enforce_toronto_min(verified, final_candidates_source)

    # ---- Run-length limiter: prevent >2 in a row by domain and by cluster/topic ----
    def enforce_run_length(arr: list[dict], key_fn, max_run: int = 2) -> list[dict]:
        out = arr[:]
        i = 0
        last_key = None
        run = 0
        n = len(out)
        while i < n:
            k = key_fn(out[i])
            if k == last_key:
                run += 1
            else:
                last_key = k
                run = 1
            if run > max_run:
                j = i + 1
                swapped = False
                while j < n:
                    if key_fn(out[j]) != last_key:
                        out[i], out[j] = out[j], out[i]
                        swapped = True
                        run = 1
                        last_key = key_fn(out[i])
                        break
                    j += 1
                if not swapped:
                    break
            i += 1
        return out

    verified.sort(key=lambda x: (_ts(x["published_utc"]), x.get("score", 0.0)), reverse=True)
    verified = enforce_run_length(verified, key_fn=lambda it: host_of(it.get("url","")), max_run=2)
    verified = enforce_run_length(verified, key_fn=lambda it: it.get("cluster_id",""), max_run=2)
    verified = verified[:REQUIRE_EXACT_COUNT or 69]

    elapsed_total = time.time() - start

    # NEW: itemset hash for monotonic/freshness aids
    itemset_hash = hashlib.sha1("|".join(sorted(x["canonical_id"] for x in verified)).encode("utf-8")).hexdigest()[:16]

    # Monotonic generated_utc: bump by +1s if same or older than previous file
    generated_utc = datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
    try:
        if os.path.exists(out_path):
            with open(out_path, "r", encoding="utf-8") as pf:
                prev = json.load(pf)
                prev_ts = prev.get("generated_utc")
                if isinstance(prev_ts, str):
                    if _ts(generated_utc) <= _ts(prev_ts):
                        generated_utc = iso_add_seconds(prev_ts, 1)
    except Exception:
        pass

    out = {
        "generated_utc": generated_utc,
        "itemset_hash": itemset_hash,  # NEW
        "count": len(verified),
        "items": verified,
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
            "version": "fetch-v2.7.0-obits-am-bias",  # bumped for obituary + AM bias + monotonic
            "weights_loaded": weights_debug.get("weights_loaded", False),
            "weights_keys": weights_debug.get("weights_keys", []),
            "weights_error": weights_debug.get("weights_error", None),
            "weights_path":  weights_debug.get("path", None),
            "score_stats": score_dbg,
            "sanity_stats": debug_counts,
            "require_exact_count": REQUIRE_EXACT_COUNT,
            "age_window_hours": MAX_AGE_HOURS,
            "min_age_sec": MIN_AGE_SEC,
            "verify_links": VERIFY_LINKS,
            "reject_homepage_redirect": REJECT_REDIRECT_TO_HOMEPAGE,
            "block_aggregators": BLOCK_AGGREGATORS,
            "fallback": {
                "used": fallback_stats.get("used", False),
                "feed": fallback_stats.get("feed"),
                "title": fallback_stats.get("title"),
                "feeds_considered": _fallback_feeds_iter(),
                "max_age_hours": FALLBACK_MAX_AGE_HOURS,
                "min_items": FALLBACK_MIN_ITEMS
            }
        }
    }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
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
    print("Debug:", {
        k: dbg.get(k) for k in [
            "feeds_total","collected","dedup_pass1","dedup_final","elapsed_sec",
            "slow_domains","timeouts","errors","weights_loaded","weights_keys",
            "weights_error","weights_path","score_stats","sanity_stats","require_exact_count",
            "fallback","version","itemset_hash"
        ]
    })

if __name__ == "__main__":
    main()
