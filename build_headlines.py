#!/usr/bin/env python3
# Enforce newest-first and cap at 40. No scoring yet.

import json, time, hashlib, pathlib
from datetime import datetime, timezone
import feedparser

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "headlines.json"
FEEDS = (ROOT / "feeds.txt").read_text().splitlines()

def to_ts(entry):
    """Best-effort published timestamp (UTC, int seconds)."""
    for k in ("published_parsed", "updated_parsed"):
        v = entry.get(k)
        if v:
            try:
                return int(time.mktime(v))  # struct_time -> epoch
            except Exception:
                pass
    return None

def iso_utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

def source_of(feed, entry):
    # Prefer feed title, else domain sniff
    t = (getattr(feed, "feed", {}) or {}).get("title") or ""
    if t:
        return t
    link = entry.get("link", "") or ""
    for dom, name in (
        ("reuters.com","Reuters"),
        ("apnews.com","AP"),
        ("bbc.","BBC"),
        ("bloomberg.com","Bloomberg"),
        ("ft.com","FT"),
        ("cbc.ca","CBC"),
        ("bnnbloomberg.ca","BNN Bloomberg"),
        ("financialpost.com","Financial Post"),
        ("theglobeandmail.com","Globe & Mail"),
    ):
        if dom in link:
            return name
    return "News"

def main():
    seen = set()
    items = []

    for url in FEEDS:
        u = url.strip()
        if not u or u.startswith("#"):
            continue
        feed = feedparser.parse(u)
        for e in feed.entries[:50]:
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
                "_ts": ts,  # internal for sorting, removed before write
            })

    # Strict newest-first
    items.sort(key=lambda x: x["_ts"], reverse=True)

    # Cap to 40
    items = items[:40]

    # Clean internal field
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
