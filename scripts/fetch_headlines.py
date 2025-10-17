#!/usr/bin/env python3
# scripts/fetch_headlines.py
#
# Build headlines.json from feeds.txt with strict link verification,
# 24–69h freshness window (set MPB_MAX_AGE_HOURS in env), market sanity checks,
# and an exact 33-item guarantee when REQUIRE_EXACT_COUNT>0.
# PLUS: Safety-net fallback to guarantee at least one working headline.

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
from typing import Tuple, Iterable
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

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
ACCEPT_HEADER = "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, text/html;q=0.7, */*;q=0.5"
ACCEPT_LANG   = "en-US,en;q=0.8"

# Enforcers (configurable via env)
VERIFY_LINKS                = os.getenv("MPB_VERIFY_LINKS", "1") == "1"
REJECT_REDIRECT_TO_HOMEPAGE = os.getenv("MPB_REJECT_REDIRECT_TO_HOMEPAGE", "1") == "1"
BLOCK_AGGREGATORS           = os.getenv("MPB_BLOCK_AGGREGATORS", "1") == "1"
MIN_AGE_SEC                 = int(os.getenv("MPB_MIN_AGE_SEC", "60"))        # ≥ 1 minute old
MAX_AGE_HOURS               = float(os.getenv("MPB_MAX_AGE_HOURS", "69"))    # ≤ X hours old; set 24 in env to hard-cap
REQUIRE_EXACT_COUNT         = int(os.getenv("MPB_REQUIRE_EXACT_COUNT", "33"))

# ---------- SAFETY-NET FALLBACK ----------
DEFAULT_FALLBACK_FEEDS = [
    "https://www.reuters.com/world/us/rss",
    "https://www.reuters.com/world/rss",
    "https://www.cbc.ca/cmlink/rss-topstories",
    "https://www.ctvnews.ca/rss/ctvnews-ca-top-stories-public-rss-1.822009",
    "https://globalnews.ca/feed/"
]
FALLBACK_FEEDS = [
    u.strip() for u in os.getenv("MPB_FALLBACK_FEEDS", ",".join(DEFAULT_FALLBACK_FEEDS)).split(",")
    if u.strip()
]
FALLBACK_MAX_AGE_HOURS = float(os.getenv("MPB_FALLBACK_MAX_AGE_HOURS", "24"))
FALLBACK_MIN_ITEMS     = int(os.getenv("MPB_FALLBACK_MIN_ITEMS", "1"))

# Source hygiene limits (now env-overridable)
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
    "fivethirtyeight.com",
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

# Breaking cue
RE_BREAKING = re.compile(
    r"\b(breaking|developing|just in|alert|evacuate|earthquake|hurricane|wildfire|flood|tsunami|tornado|"
    r"missile|air[-\s]?strike|explosion|blast|drone|shooting|casualties?|dead|killed)\b", re.I
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
        # Strong hints to CDNs and proxies not to serve stale
        "Cache-Control": "no-cache",
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

def http_get(session: requests.Session, url: str) -> bytes | None:
    """
    Fetch a URL with a short-lived cache buster to avoid CDN-stale feeds.
    Uses the same busted URL for the fallback request.
    """
    try:
        # 5-minute bucket cache buster (stable for a short window to avoid thrash)
        bucket = int(time.time() // 300)
        joiner = "&" if ("?" in url) else "?"
        bust_url = f"{url}{joiner}_mpb={bucket}"

        resp = session.get(bust_url, timeout=HTTP_TIMEOUT_S, allow_redirects=True)
        if getattr(resp, "ok", False) and resp.content:
            ctype = resp.headers.get("Content-Type", "").lower()
            if _looks_like_xml(resp.content, ctype): return resp.content
        # fall back (HTML scrapers) with stronger no-cache and alt UA
        alt_headers = {
            "User-Agent": ALT_USER_AGENT,
            "Accept": ACCEPT_HEADER,
            "Accept-Language": ACCEPT_LANG,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        resp2 = session.get(bust_url, timeout=HTTP_TIMEOUT_S, headers=alt_headers, allow_redirects=True)
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
RE_FATAL_CUE = re.compile(r"\b(dead|killed|homicide|murder|fatal|deadly)\b", re.I)

def word_or_int_to_int(s: str) -> int:
    s = s.lower()
    if s.isdigit(): return int(s)
    return WORD_NUM.get(s, 0)

def parse_casualties(title: str) -> tuple[int,int,bool]:
    deaths = 0; injured = 0
    for m in RE_DEATH.finditer(title): deaths += word_or_int_to_int(m.group(1))
    for m in RE_INJ.finditer(title): injured += word_or_int_to_int(m.group(1))
    has_fatal_cue = bool(RE_FATAL_CUE.search(title))
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

    # Fallback regex pass (coarse)
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
        soup = BeautifulSoup(html_bytes, "htmlparser") if False else BeautifulSoup(html_bytes, "html.parser")
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
    """Block stale milestone/record claims unless corroborated."""
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
    """
    Returns (ok, final_url, status_code, reason)
    """
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

    # Early len(body) test — relax for sports domains
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

            # Relax article-ness for sports finals/box scores
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
        except
