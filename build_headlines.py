#!/usr/bin/env python3
# MYPYBITE NewsRiver — Step 1 (newest-first + cap 40) with SMOKE mode + _debug
# - Robust fetch (headers, retries, timeouts)
# - Dedup by URL
# - Sorted by published time desc
# - Writes _debug with smoke_mode so you can verify test runs

import json, time, hashlib, pathlib, urllib.request, ssl, os
from datetime import datetime, timezone
import feedparser

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "headlines.json"
FEEDS_ALL = [ln.strip() for ln in (ROOT / "feeds.txt").read_text().splitlines()
             if ln.strip() and not ln.strip().startswith("#")]

# ---------- Tunables ----------
# Default OFF; set SMOKE=1 in the workflow env to enable quick test runs
SMOKE = os.getenv("SMOKE", "0").strip() == "1"

CAP_ITEMS = 24 if SMOKE else 40            # total items kept
FEED_LIMIT = 3 if SMOKE else None          # only first N feeds in smoke mode
PER_FEED_ITEMS = 12 if SMOKE else 50       # fewer items per feed in smoke mode
TIMEOUT = 8 if SMOKE else 20               # shorter timeout in smoke mode
RETRIES = 1 if SMOKE else 3                # fewer retries in smoke mode

UA = "MyPyBITE-NewsRiver/1.0 (+https://mypybite.com)"
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

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

def source_of(feed, entry):
    """Prefer feed title; fallback to friendly domain name."""
    t = (getattr(feed, "feed", {}) or {}).get("title") or ""
    if t:
        return t
    link = entry.get("link", "") or ""
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
        if dom in link:
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
            items.append({
                "title": title,
                "url": link,
                "source": source_of(feed, e),
                "published_utc": iso_utc(ts),
                "_ts": ts,  # internal for sorting
            })

    # Strict newest-first and cap
    items.sort(key=lambda x: x["_ts"], reverse=True)
    items = items[:CAP_ITEMS]
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
            "cap_items": CAP_ITEMS,
            "version": "step1-v1.2",
        }
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"[done] wrote {out['count']} items (fetched {fetched}/{total} feeds) at {out['generated_utc']}")

if __name__ == "__main__":
    main()
