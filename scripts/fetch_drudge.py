#!/usr/bin/env python3
# scripts/fetch_drudge.py
# Scrape Drudge front page, normalize, and atomically write newsriver/dredge_heds.json
# Compatible with your ticker and river (object with "items": [...]).

import os, sys, json, re, tempfile, time
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup

DRUDGE_URL = "https://drudgereport.com/"
OUT_PATH   = os.path.join("newsriver", "dredge_heds.json")

TIMEOUT_S  = 15
MAX_ITEMS  = 40        # keep it modest and clean
MIN_ITEMS  = 3         # require at least this many to replace the file

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

NAV_EXCLUDE = re.compile(
    r"\b(archives?|columnists?|advertis(e|ing)|about|contact|tips|privacy|terms)\b",
    re.I,
)

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def normalize_url(u: str) -> str:
    try:
        p = urlparse(u)
        if not p.scheme:
            # Make relative links absolute to drudgereport.com
            p = urlparse(requests.compat.urljoin(DRUDGE_URL, u))
        # strip tracking params
        keep = []
        for k, v in parse_qsl(p.query, keep_blank_values=True):
            if re.match(r"^(utm_|fbclid$|gclid$|mc_(cid|eid)$|ref$|scid$|cmpid$|source$)", k, re.I):
                continue
            keep.append((k, v))
        q = urlencode(keep)
        p = p._replace(query=q, fragment="")
        # normalize hostname
        netloc = p.netloc.lower()
        scheme = p.scheme.lower() if p.scheme else "https"
        return urlunparse((scheme, netloc, p.path, p.params, p.query, ""))
    except Exception:
        return u.strip()

def fetch_html(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.text

def extract_items(html: str):
    soup = BeautifulSoup(html, "html.parser")
    items = []
    seen_urls = set()

    # Drudge is just a pile of anchors. We’ll pick decent-looking ones.
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        text = " ".join(a.stripped_strings) if a else ""
        if not href or not text:
            continue
        if NAV_EXCLUDE.search(text):
            continue
        # ignore tiny/boilerplate bits
        if len(text) < 15 or len(text) > 220:
            continue

        u = normalize_url(href)
        if not u.startswith(("http://", "https://")):
            continue
        # simple dedupe
        if u in seen_urls:
            continue
        seen_urls.add(u)

        items.append({
            "title": text,
            "url": u,
            "published_at": utc_now_iso(),   # Drudge has no timestamps; use scrape time
            "source": "Drudge Report",
        })
        if len(items) >= MAX_ITEMS:
            break

    return items

def atomic_write_json(path: str, data: dict):
    dstdir = os.path.dirname(path) or "."
    os.makedirs(dstdir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".drdg_", suffix=".json", dir=dstdir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        # clean up tmp if replace failed
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise

def main() -> int:
    try:
        html = fetch_html(DRUDGE_URL)
    except Exception as e:
        print(f"[drudge] fetch failed: {e}", file=sys.stderr, flush=True)
        # Do NOT clobber existing file on fetch failure
        return 0

    try:
        items = extract_items(html)
    except Exception as e:
        print(f"[drudge] parse failed: {e}", file=sys.stderr, flush=True)
        return 0

    if len(items) < MIN_ITEMS:
        print(f"[drudge] too few items ({len(items)}) — keeping previous file", flush=True)
        return 0

    payload = {"items": items}
    try:
        atomic_write_json(OUT_PATH, payload)
    except Exception as e:
        print(f"[drudge] write failed: {e}", file=sys.stderr, flush=True)
        return 1

    print(f"[drudge] wrote {len(items)} items → {OUT_PATH}", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
