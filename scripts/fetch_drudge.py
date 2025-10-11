#!/usr/bin/env python3
"""
Builds newsriver/drudge.json from DrudgeReport (or mirrors/RSS).
- Fetches multiple sources (first that works)
- Extracts top headlines & links
- Applies a deterministic "hyperbolizer"
- Writes JSON ready for the front-end ticker
"""

import json, os, re, sys, time, random, hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

OUT_PATH = "newsriver/drudge.json"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121 Safari/537.36"

# Potential sources (some may not always work; we try in order)
SOURCES = [
    # If you have your own relay/mirror, put it first:
    # "https://your-relay.example.com/drudge.html",
    "https://www.drudgereport.com/",
    # A couple of community mirrors (not guaranteed, but as fallbacks)
    "https://drudgereport.com/",
]

TIMEOUT = 10
MAX_ITEMS = 30

def get(url):
    return requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)

def normalize_url(href, base):
    if not href:
        return None
    href = href.strip()
    if href.startswith("javascript:") or href.startswith("#"):
        return None
    return urljoin(base, href)

def is_probably_headline(text: str) -> bool:
    if not text: return False
    t = text.strip()
    if len(t) < 4: return False
    # Filter junk
    if re.search(r"(advert|subscribe|privacy|about|terms|tip|app)", t, re.I):
        return False
    return True

def fingerprint(s: str) -> str:
    return hashlib.md5((s or "").encode("utf-8")).hexdigest()[:10]

INTENSIFIERS = [
    "SHOCKING", "WILD", "STUNNING", "SURGING", "EXPLOSIVE",
    "JAW-DROPPING", "BREAKNECK", "MASSIVE", "FEROCIOUS",
    "ABSOLUTE", "OFF-THE-CHARTS", "UNREAL"
]
VERB_SWAPS = {
    r"\bsees\b": "ROCKETS",
    r"\bhits\b": "SLAMS",
    r"\bwarns\b": "BLARES",
    r"\bsays\b": "DECLARES",
    r"\breports?\b": "BOMBSHELLS",
    r"\bfalls?\b": "PLUNGES",
    r"\brises?\b": "SOARS",
    r"\bspikes?\b": "ERUPTS",
}
NUMBER_WRAP = lambda n: f"**{n}**"

def hyperbolize(title: str) -> str:
    """Deterministically add hype without hallucinating facts."""
    t = title.strip()

    # Uppercase proper “impact” words, keep rest readable
    # (avoid uppercasing the whole thing)
    words = t.split()
    boosted = []
    for w in words:
        w_clean = re.sub(r"[^\w%$-]", "", w)
        if re.fullmatch(r"\d[\d,\.]*", w_clean):
            boosted.append(NUMBER_WRAP(w))
        elif len(w_clean) >= 6 and w_clean.isalpha():
            boosted.append(w.upper())
        else:
            boosted.append(w)
    t = " ".join(boosted)

    # Intensifier prefix (pick based on fingerprint so it’s stable)
    idx = int(fingerprint(title), 16) % len(INTENSIFIERS)
    t = f"{INTENSIFIERS[idx]}: {t}"

    # Verb swaps
    for pattern, repl in VERB_SWAPS.items():
        t = re.sub(pattern, repl, t, flags=re.I)

    # Add one or two exclamation points max
    if not t.endswith(("!", "?", "…")):
        t += "!"
    if len(t) < 60 and t.count("!") < 2:
        t += "!"

    # Tighten extra spaces
    t = re.sub(r"\s{2,}", " ", t)
    return t

def parse_drudge_links(html: str, base: str):
    soup = BeautifulSoup(html, "html.parser")
    links = []
    # Drudge is basically a list of <a> links; grab a bunch of them
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if not is_probably_headline(text):
            continue
        href = normalize_url(a["href"], base)
        if not href:
            continue
        links.append((text, href))
    return links

def fetch_headlines():
    for src in SOURCES:
        try:
            res = get(src)
            if res.status_code != 200 or not res.text:
                continue
            items = parse_drudge_links(res.text, src)
            if items:
                return items
        except Exception:
            continue
    return []

def build_payload(items):
    dedup = {}
    out = []
    for text, url in items[: MAX_ITEMS * 3]:  # collect extras before dedupe
        key = (text.lower(), url.lower())
        if key in dedup: continue
        dedup[key] = True
        hyped = hyperbolize(text)
        out.append({
            "title": text,
            "hyped": hyped,
            "url": url,
            "source": urlparse(url).netloc
        })
        if len(out) >= MAX_ITEMS: break

    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "count": len(out),
        "items": out,
    }

def main():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    items = fetch_headlines()
    payload = build_payload(items)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Wrote {OUT_PATH} with {payload['count']} items.")

if __name__ == "__main__":
    sys.exit(main())
