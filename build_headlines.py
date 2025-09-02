#!/usr/bin/env python3
# Fast, noisy builder with SMOKE mode. Newest-first + cap 40. No scoring yet.

import json, time, hashlib, pathlib, urllib.request, ssl, os
from datetime import datetime, timezone
import feedparser

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "headlines.json"
FEEDS_ALL = [ln.strip() for ln in (ROOT / "feeds.txt").read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]

# --- Tunables ---
SMOKE = os.getenv("SMOKE", "0") == "1"          # set to 1 in CI for fast runs
FEED_LIMIT = 3 if SMOKE else None               # only first N feeds in smoke
PER_FEED_ITEMS = 12 if SMOKE else 50            # fewer per feed in smoke
TIMEOUT = 8 if SMOKE else 20                    # shorter timeout in smoke
RETRIES = 1 if SMOKE else 3                     # fewer retries in smoke

UA = "MyPyBITE-NewsRiver/1.0 (+https://mypybite.com)"
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

def to_ts(entry):
    for k in ("published_parsed", "updated_parsed"):
        v = entry.get(k)
        if v:
            try:
                return int(time.mktime(v))
            except Exception:
                pass
    return None

def iso_utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

def source_of(feed, entry):
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

def fetch_feed(url, retries=RETRIES, timeout=TIMEOUT):
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
    # last try directly through feedparser with headers
    try:
        return feedparser.parse(url, request_headers=HEADERS)
    except Exception:
        pass
    print(f"[warn] skipping feed (errors): {url} :: {last_err}")
    return None

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
                "_ts": ts,
            })

    # Strict newest-first and cap 40
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
        }
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"[done] wrote {out['count']} items (fetched {fetched}/{total} feeds) at {out['generated_utc']}")

if __name__ == "__main__":
    main()
