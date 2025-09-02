#!/usr/bin/env python3
# Robust newest-first builder with retries & headers. No scoring yet.

import json, time, hashlib, pathlib, urllib.request, urllib.error, ssl
from datetime import datetime, timezone
import feedparser

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "headlines.json"
FEEDS = [ln.strip() for ln in (ROOT / "feeds.txt").read_text().splitlines()]
UA = "MyPyBITE-NewsRiver/1.0 (+https://mypybite.com)"
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

# --- helpers -----------------------------------------------------------------
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

def fetch_feed(url, retries=3, timeout=20):
    """Fetch bytes with headers + timeout, then let feedparser parse bytes."""
    last_err = None
    ctx = ssl.create_default_context()
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                data = resp.read()
            return feedparser.parse(data)
        except Exception as e:
            last_err = e
            # gentle backoff
            time.sleep(1.25 * attempt)
    # Give feedparser one last chance directly (some CDNs behave differently)
    try:
        return feedparser.parse(url, request_headers=HEADERS)
    except Exception:
        pass
    print(f"[warn] skipping feed (errors): {url}\n  -> {last_err}")
    return None

# --- main --------------------------------------------------------------------
def main():
    seen = set()
    items = []

    for raw in FEEDS:
        u = raw.strip()
        if not u or u.startswith("#"):
            continue
        feed = fetch_feed(u)
        if not feed or not getattr(feed, "entries", None):
            continue

        for e in feed.entries[:50]:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
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
                "_ts": ts,  # internal for sorting
            })

    # Strict newest-first
    items.sort(key=lambda x: x["_ts"], reverse=True)

    # Cap to 40 and clean temp field
    items = items[:40]
    for it in items:
        it.pop("_ts", None)

    out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": items,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"Wrote {out['count']} items at {out['generated_utc']}")

if __name__ == "__main__":
    main()
