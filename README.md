# NewsRiver

[![Build headlines.json](https://github.com/MyPyBiTE/newsriver/actions/workflows/build_headlines.yml/badge.svg)](https://github.com/MyPyBiTE/newsriver/actions/workflows/build_headlines.yml)

Newest-first headline river focused on Toronto, business/markets, culture, youth, housing, energy, tech, and alerts.  
Live site: https://mypybite.github.io/newsriver/

---

## How it works
- **Sources:** `feeds.txt` (grouped by section headers like `# --- Toronto Local ---`).
- **Builder:** `scripts/fetch_headlines.py` fetches RSS/Atom, canonicalizes URLs, fuzzy-dedupes near-identical titles, tags `category`/`region`, sorts newest-first, and writes **`headlines.json`**.
- **Automation:** GitHub Actions workflow **`build_headlines.yml`** runs on changes to `feeds.txt` / `scripts/**` or when manually triggered, then commits the updated `headlines.json`.
- **Front-end:** `index.html` + `assets/app.js` render cards, filters, and the “Last updated” chip.

## Update the feed list
1. Edit **`feeds.txt`** (keep the header lines; add/remove one feed URL per line).
2. Commit. The workflow will rebuild **`headlines.json`** automatically.
3. (Optional) Trigger a manual run: **Actions → Build headlines.json → Run workflow**.

## Run locally (optional)
```bash
python -m venv .venv && source .venv/bin/activate
pip install feedparser requests
python scripts/fetch_headlines.py --feeds-file feeds.txt --out headlines.json

.
├── assets/                 # styles & client JS
├── scripts/fetch_headlines.py   # builder
├── feeds.txt               # your curated RSS/Atom sources
├── headlines.json          # generated output
├── index.html              # UI
└── .github/workflows/build_headlines.yml
MyPyBiTE Public Benefit Notice (Non-Binding)

MyPyBiTE exists to advance worker rights through decentralized technology,
resist the concentration of power sometimes called “tech feudalism,” and
promote open, interoperable tools that serve the public. Contributors and
users are encouraged to align with these values. This statement is purely
informational and does not alter the legal terms of the license below.

---

MIT License

Copyright (c) 2025 MyPyBiTE

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the “Software”), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
