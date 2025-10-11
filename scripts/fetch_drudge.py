#!/usr/bin/env python3
"""
Builds newsriver/drudge.json from DrudgeReport (or mirrors/RSS).
- Fetches multiple sources (first that works)
- Extracts top headlines & links
- Applies a deterministic "hyperbolizer"
- Classifies flags (breaking / landmark) and swaps Bitcoin->₿ in display text
- Writes JSON ready for the front-end ticker
"""

import json, os, re, sys, hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

OUT_PATH = "newsriver/drudge.json"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121 Safari/537.36"

SOURCES = [
    "https://www.drudgereport.com/",
    "https://drudgereport.com/",
]

TIMEOUT = 10
MAX_ITEMS = 30
BTC_SIGN = "\u20BF"  # ₿

# -------- utilities --------
def get(url):
    return requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)

def normalize_url(href, base):
    if not href:
        return None
    href = href.strip()
    if href.startswith(("javascript:", "#")):
        return None
    return urljoin(base, href)

def is_probably_headline(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if len(t) < 4:
        return False
    # Filter obvious non-headline chrome
    if re.search(r"(advert|subscribe|privacy|about|terms|tip|app|share|contact)", t, re.I):
        return False
    return True

def fingerprint(s: str) -> str:
    return hashlib.md5((s or "").encode("utf-8")).hexdigest()[:10]

# -------- hyperbolizer (unchanged core behavior) --------
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
    """Deterministically add hype without changing facts."""
    t = (title or "").strip()

    # Uppercase some longer words and emphasize numbers
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

    # Stable intensifier
    idx = int(fingerprint(title), 16) % len(INTENSIFIERS)
    t = f"{INTENSIFIERS[idx]}: {t}"

    # Verb swaps
    for pattern, repl in VERB_SWAPS.items():
        t = re.sub(pattern, repl, t, flags=re.I)

    # Exclamation discipline
    if not t.endswith(("!", "?", "…")):
        t += "!"
    if len(t) < 60 and t.count("!") < 2:
        t += "!"

    return re.sub(r"\s{2,}", " ", t)

# -------- classification / display helpers --------
RE_BREAKING = re.compile(r"\b(breaking|developing|just in|urgent|alert)\b", re.I)

# Sports championships / clinchers
RE_CHAMPIONSHIP = re.compile(
    r"\b(championship|champion|clinches|clinched|wins|captures|claims|crown|title|cup|trophy|"
    r"world series|stanley cup|super bowl|grey cup|nba finals?|grand slam|world cup)\b", re.I
)

# Landmark legal/rulings
RE_LANDMARK_LEGAL = re.compile(
    r"\b(landmark|supreme court|high court|appeals court|appeals panel|"
    r"ruling|verdict|decision|opinion|overturns|overturned|upholds|strikes down|injunction)\b", re.I
)

RE_BTC = re.compile(r"(?<!\w)\$?BTC\b|\bBitcoin\b", re.I)

def classify_flags(title: str):
    text = title or ""
    is_breaking = bool(RE_BREAKING.search(text))
    is_landmark = bool(RE_CHAMPIONSHIP.search(text) or RE_LANDMARK_LEGAL.search(text))
    has_bitcoin = bool(RE_BTC.search(text))
    # Suggest front-end effects (can be ignored if you prefer CSS-only)
    effects = []
    if is_breaking:
        effects.append({"style": "breaker"})  # e.g., bleed red
    if is_landmark:
        effects.append({"style": "glow"})     # e.g., glow highlight
    return {
        "is_breaking": is_breaking,
        "is_landmark": is_landmark,
        "has_bitcoin": has_bitcoin,
        "effects": effects
    }

def with_bitcoin_symbol(s: str) -> str:
    # Replace Bitcoin/$BTC with ₿, preserving case elsewhere
    return RE_BTC.sub(BTC_SIGN, s or "")

# -------- scraping --------
def parse_drudge_links(html: str, base: str):
    soup = BeautifulSoup(html, "html.parser")
    links = []
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

# -------- payload --------
def build_payload(items):
    dedup = {}
    out = []
    for text, url in items[: MAX_ITEMS * 3]:  # collect extras before dedupe
        key = (text.lower(), url.lower())
        if key in dedup:
            continue
        dedup[key] = True

        hyped = hyperbolize(text)
        flags = classify_flags(text)

        # Pre-rendered display string (hyped, with ₿ where applicable)
        display = with_bitcoin_symbol(hyped)

        out.append({
            "title": text,                      # original
            "hyped": hyped,                     # hyperbolized text
            "display": display,                 # hyped + ₿ substitution
            "url": url,
            "source": urlparse(url).netloc,
            "flags": flags,                     # {is_breaking,is_landmark,has_bitcoin,effects:[...]}
        })
        if len(out) >= MAX_ITEMS:
            break

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
