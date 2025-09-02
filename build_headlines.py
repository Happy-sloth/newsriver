#!/usr/bin/env python3
# MYPYBITE NewsRiver — Step 1.2 + Step 2a (scoring scaffold)
# - Newest-first cap 40 (unchanged)
# - Adds: category, region, score, priority_reason (but DOES NOT re-sort by score yet)
# - Sports fan boosts (Leafs, Blue Jays, Dodgers, Oilers, McDavid)
# - Crypto base penalty (exceptions to be wired in later steps)
# - Debug block kept

import json, time, hashlib, pathlib, urllib.request, ssl, os, re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
import feedparser

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "headlines.json"
FEEDS_ALL = [ln.strip() for ln in (ROOT / "feeds.txt").read_text().splitlines()
             if ln.strip() and not ln.strip().startswith("#")]

# ---------- Tunables ----------
SMOKE = os.getenv("SMOKE", "0") == "1"     # CI quick-run mode
FEED_LIMIT = 3 if SMOKE else None          # only first N feeds in smoke mode
PER_FEED_ITEMS = 12 if SMOKE else 50       # fewer items per feed in smoke mode
TIMEOUT = 8 if SMOKE else 20               # shorter timeout in smoke mode
RETRIES = 1 if SMOKE else 3                # fewer retries in smoke mode

UA = "MyPyBITE-NewsRiver/1.0 (+https://mypybite.com)"
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

# Regions by domain (very rough; we’ll refine later)
CANADA_DOMAINS = (
    "cbc.ca", "radio-canada.ca", "ctvnews.ca", "globalnews.ca",
    "theglobeandmail.com", "thestar.com", "financialpost.com",
    "nationalpost.com", "bnnbloomberg.ca", "cp24.com", "torontosun.com",
)
US_DOMAINS = (
    "apnews.com", "bloomberg.com", "reuters.com", "nytimes.com",
    "washingtonpost.com", "wsj.com", "cnbc.com", "abcnews.go.com",
    "nbcnews.com", "foxnews.com", "latimes.com", "ft.com"  # FT is intl, but many US-market stories
)
CRYPTO_DOMAINS = (
    "coindesk.com", "cointelegraph.com", "news.bitcoin.com", "decrypt.co",
)

# Sports fandom keywords (boosts)
FAN_TERMS = (
    r"maple\s*leafs", r"\bleafs\b", r"toronto\s+maple\s+leafs",
    r"blue\s*jays", r"\bbluejays\b", r"toronto\s+blue\s+jays",
    r"la\s+dodgers", r"\bdodgers\b", r"los\s+angeles\s+dodgers",
    r"edmonton\s+oilers", r"\boilers\b",
    r"connor\s+mcdavid", r"\bmcdavid\b"
)
FAN_RE = re.compile("|".join(FAN_TERMS), re.IGNORECASE)

CRYPTO_TERMS = re.compile(r"\b(crypto|bitcoin|btc|ether(eum)?|eth|blockchain|xrp|ripple)\b", re.IGNORECASE)

# ---------- Helpers ----------
def to_ts(entry):
    """Best-effort UTC timestamp from feed entry."""
    for k in ("published_parsed", "updated_parsed"):
        v = entry.get(k)
        if v:
            try:
                return int(time.mktime(v))  # struct_time -> epoch seconds
            except Exception:
                pass
    return None

def iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

def get_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""

def source_of(feed, entry):
    """Prefer feed title; fallback to friendly domain name."""
    t = (getattr(feed, "feed", {}) or {}).get("title") or ""
    if t:
        return t
    link = entry.get("link", "") or ""
    host = get_domain(link)
    for dom, name in (
        ("reuters.com", "Reuters"),
        ("apnews.com", "AP"),
        ("bbc.", "BBC"),
        ("bloomberg.com", "Bloomberg"),
        ("ft.com", "FT"),
        ("cbc.ca", "CBC"),
        ("bnnbloomberg.ca", "BNN Bloomberg"),
        ("financialpost.com", "Financial Post"),
        ("theglobeandmail.com", "Globe & Mail"),
    ):
        if dom in host:
            return name
    return "News"

def fetch_feed(url: str, retries: int = RETRIES, timeout: int = TIMEOUT):
    """Fetch bytes with headers, then parse. Retries with small backoff."""
    ctx = ssl.create_default_context()
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                data = resp.read()
            return feedparser.parse(data)
        except Exception as e:
            last_err = e
            time.sleep(0.75 * attempt)
    # Final try letting feedparser fetch with headers (some CDNs prefer this)
    try:
        return feedparser.parse(url, request_headers=HEADERS)
    except Exception:
        pass
    print(f"[warn] skipping feed (errors): {url} :: {last_err}")
    return None

# ---------- Tagging + scoring (scaffold) ----------
def detect_category_and_region(title: str, url: str, src: str):
    """Very light heuristic tags we’ll refine over steps 2b..2e."""
    host = get_domain(url)
    t = title.lower()

    # Category
    if CRYPTO_TERMS.search(title) or any(d in host for d in CRYPTO_DOMAINS):
        category = "Crypto"
    elif FAN_RE.search(title):
        category = "Sports"
    else:
        category = "General"

    # Region
    if any(d in host for d in CANADA_DOMAINS) or "canada" in t or "toronto" in t:
        region = "Canada"
    elif any(d in host for d in US_DOMAINS) or "u.s." in t or "united states" in t or "america" in t:
        region = "US"
    else:
        region = "World"

    return category, region

def compute_score(item):
    """Base score + small boosts/penalties. We keep it gentle for now."""
    score = 0.0
    reasons = []

    # Recency — small nudge (we still sort by time today)
    try:
        pub_dt = datetime.fromisoformat(item["published_utc"].replace("Z","+00:00"))
    except Exception:
        pub_dt = datetime.now(timezone.utc)

    age = datetime.now(timezone.utc) - pub_dt
    if age <= timedelta(hours=3):
        score += 0.8; reasons.append("recency≤3h +0.8")
    elif age <= timedelta(hours=24):
        score += 0.4; reasons.append("recency≤24h +0.4")

    # Region preference (Canada first; US slight nudge)
    if item["region"] == "Canada":
        score += 0.9; reasons.append("Canada +0.9")
    elif item["region"] == "US":
        score += 0.25; reasons.append("US +0.25")
    else:
        score += 0.0  # World neutral for now

    # Crypto lower by default (we’ll add exceptions in a later step)
    if item["category"] == "Crypto":
        score -= 0.8; reasons.append("Crypto −0.8")

    # Sports fandom boosts (Leafs / Jays / Dodgers / Oilers / McDavid)
    title = item["title"]
    if FAN_RE.search(title):
        score += 0.9; reasons.append("fan-team +0.9")

    # Gentle source credibility nudge (tiny; we’ll expand later)
    src = (item.get("source") or "").lower()
    if "financial post" in src or "globe" in src or "cbc" in src or "reuters" in src or "ft" in src or "bbc" in src:
        score += 0.1; reasons.append("source +0.1")

    return round(score, 6), "; ".join(reasons)

# ---------- Main ----------
def main():
    feeds = FEEDS_ALL[:FEED_LIMIT] if FEED_LIMIT else FEEDS_ALL
    total = len(feeds)
    if total == 0:
        raise SystemExit("No feeds in feeds.txt")

    seen = set()
    items = []
    fetched = 0
    skipped = 0

    for i, u in enumerate(feeds, 1):
        print(f"[{i}/{total}] fetching: {u}")
        feed = fetch_feed(u)
        if not feed or not getattr(feed, "entries", None):
            print(f"[{i}/{total}] -> no entries (skipped)")
            skipped += 1
            continue

        fetched += 1
        for e in feed.entries[:PER_FEED_ITEMS]:
            title = (e.get("title") or "").strip()
            link  = (e.get("link") or "").strip()
            if not title or not link:
                continue

            # Dedup by URL
            key = hashlib.sha1(link.encode("utf-8")).hexdigest()
            if key in seen:
                continue
            seen.add(key)

            ts = to_ts(e) or int(time.time())
            src = source_of(feed, e)

            # Base record
            rec = {
                "title": title,
                "url": link,
                "source": src,
                "published_utc": iso_utc(ts),
                "_ts": ts,  # internal for sorting (newest-first)
            }

            # Tag + score
            cat, reg = detect_category_and_region(title, link, src)
            rec["category"] = cat
            rec["region"] = reg
            sc, why = compute_score(rec)
            rec["score"] = sc
            rec["priority_reason"] = why

            items.append(rec)

    # Strict newest-first and cap to 40 (ordering unchanged in Step 2a)
    items.sort(key=lambda x: x["_ts"], reverse=True)
    items = items[:40]
    for it in items:
        it.pop("_ts", None)

    out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": items,
        "_debug": {
            "feeds_total": total,
            "feeds_fetched": fetched,
            "feeds_skipped": skipped,
            "smoke_mode": SMOKE,
            "cap_items": 40,
            "version": "step2a-v0.1",
        }
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"[done] wrote {out['count']} items (fetched {fetched}/{total} feeds) at {out['generated_utc']}")

if __name__ == "__main__":
    main()
