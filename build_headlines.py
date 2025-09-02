#!/usr/bin/env python3
# minimal builder: pulls feeds, keeps newest stories, writes headlines.json

import json, time, pathlib, hashlib
from datetime import datetime
import feedparser

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "headlines.json"
FEEDS = (ROOT / "feeds.txt").read_text().splitlines()

def ts_of(entry):
    for k in ("published_parsed", "updated_parsed"):
        v = entry.get(k)
        if v:
            try:
                return int(time.mktime(v))
            except Exception:
                pass
    return None

def src_of(feed, entry):
    # prefer feed title; fall back to domain sniff
    t = (getattr(feed, "feed", {}) or {}).get("title") or ""
    if t: return t
    link = entry.get("link","")
    for dom,name in (
        ("reuters.com","Reuters"),("apnews.com","AP"),("bbc.","BBC"),
        ("bloomberg.com","Bloomberg"),("ft.com","FT"),
        ("cbc.ca","CBC"),("bnnbloomberg.ca","BNN Bloomberg"),
        ("financialpost.com","Financial Post"),("theglobeandmail.com","Globe & Mail")
    ):
        if dom in link: return name
    return "News"

def main():
    items = []
    seen = set()
    for url in FEEDS:
        url = url.strip()
        if not url or url.startswith("#"): 
            continue
        feed = feedparser.parse(url)
        for e in feed.entries[:30]:
            title = (e.get("title") or "").strip()
            link  = e.get("link") or ""
            if not title or not link: 
                continue
            key = hashlib.sha1(link.encode("utf-8")).hexdigest()
            if key in seen: 
                continue
            seen.add(key)
            ts = ts_of(e)
            items.append({
                "title": title,
                "url": link,
                "source": src_of(feed, e),
                "ts": ts or int(time.time())
            })

    # newest first, keep top 40
    items.sort(key=lambda x: x["ts"], reverse=True)
    out = {"generated_at": int(time.time()), "items": items[:40]}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"Wrote {len(out['items'])} items to headlines.json at {datetime.utcnow().isoformat()}Z")

if __name__ == "__main__":
    main()
