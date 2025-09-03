// Minimal client for NewsRiver
// - fetches headlines.json (no-cache, cache-busted)
// - renders cards newest-first (with stable, monotonic ordering via first-seen sequence)
// - de-dupes items (cluster_id or normalized title fallback; prefers non-aggregators)
// - adds "Last updated" chip (safe fallback if .toolbar not present)
// - supports category + region filters
// - auto-refreshes every 5 minutes (tweak REFRESH_MS as needed)

"use strict";

// ---- Configurable JSON endpoint (works on GitHub Pages and Zoho embeds) ----
// If the page hasn't set this already, default to your GitHub Pages JSON:
if (!window.NEWSRIVER_JSON_URL) {
  window.NEWSRIVER_JSON_URL = "https://mypybite.github.io/newsriver/headlines.json";
}
// (If you prefer to set it from the HTML instead, add BEFORE this script:
//   <script>window.NEWSRIVER_JSON_URL="https://mypybite.github.io/newsriver/headlines.json";</script>
window.NEWSRIVER_JSON_URL = window.NEWSRIVER_JSON_URL || null;

// IDs / constants
const GRID_ID = "grid";
const FILTERS_ID = "filters";
const LAST_UPDATED_ID = "last-updated";
const JSON_URL = window.NEWSRIVER_JSON_URL || "headlines.json";
const REFRESH_MS = 5 * 60 * 1000; // 5 minutes

let rawData = null;
let activeCategory = "all";  // "all" | "Sports" | "General" | ...
let activeRegion = null;     // null = any | "Canada" | "US" | "World"
let lastStamp = null;

// ---------- Step 1: stable, monotonic ordering (first-seen sequence) ----------
const SEQ_STORAGE_KEY = "nr_seq_v1";

function loadSeqState() {
  try {
    const raw = localStorage.getItem(SEQ_STORAGE_KEY);
    if (!raw) return { counter: 0, map: {} };
    const parsed = JSON.parse(raw);
    return {
      counter: Number(parsed?.counter || 0),
      map: parsed?.map && typeof parsed.map === "object" ? parsed.map : {}
    };
  } catch {
    return { counter: 0, map: {} };
  }
}

function saveSeqState(state) {
  try {
    localStorage.setItem(SEQ_STORAGE_KEY, JSON.stringify(state));
  } catch {}
}

// ---------- Dedupe helpers ----------

// Strip " - Site" tails, normalize punctuation/spacing, lowercase.
// This collapses Google News vs original-source duplicates.
function normalizeTitle(t) {
  return (t || "")
    .replace(/\s+[-–—]\s+[^|]+$/u, "")       // drop trailing " - Source"
    .toLowerCase()
    .replace(/[\u2010-\u2015]/g, "-")        // unify dashes
    .replace(/[^\p{L}\p{N}]+/gu, " ")        // remove punctuation
    .trim()
    .replace(/\s+/g, " ");
}

// Use cluster_id when present (from enrichment), else normalized title.
function dedupeKey(it) {
  if (it && it.cluster_id) return `c:${it.cluster_id}`;
  return `t:${normalizeTitle(it?.title || "")}`;
}

// Prefer non-aggregator sources when times are tied.
function isAggregator(it) {
  const src = `${it?.source || ""} ${it?.url || ""}`.toLowerCase();
  return /news\.google|google news|yahoo\.com\/news|apple\.news/.test(src);
}

// Keep only the newest item per key; if tie, prefer non-aggregator.
function dedupeItems(items) {
  const byKey = new Map();
  for (const it of items) {
    const key = dedupeKey(it);
    const prev = byKey.get(key);
    if (!prev) {
      byKey.set(key, it);
      continue;
    }
    const tNew = Date.parse(it.published_utc || it.published || 0) || 0;
    const tOld = Date.parse(prev.published_utc || prev.published || 0) || 0;

    if (tNew > tOld) {
      byKey.set(key, it);
    } else if (tNew === tOld) {
      // tie-breaker: non-aggregator wins
      if (isAggregator(prev) && !isAggregator(it)) byKey.set(key, it);
    }
  }
  return Array.from(byKey.values());
}

// Use the dedupe identity for seq, so dupes don’t create multiple slots.
function getItemId(it) {
  return dedupeKey(it);
}

/**
 * Assign a monotonic first-seen sequence to each item.
 * - Existing items keep original seq.
 * - New items get ++counter so they float above older ones.
 * Mutates items to attach `__seq`.
 */
function assignStableSeq(items) {
  const state = loadSeqState();
  let touched = false;

  for (const it of items) {
    const id = getItemId(it);
    if (state.map[id] == null) {
      state.counter += 1;
      state.map[id] = state.counter;
      touched = true;
    }
    it.__seq = state.map[id];
  }

  if (touched) saveSeqState(state);
  return items;
}

// ---------- helpers ----------
function $(sel, root = document) { return root.querySelector(sel); }
function $all(sel, root = document) { return [...root.querySelectorAll(sel)]; }

function fmtRelative(isoString) {
  try {
    const d = new Date(isoString);
    const diffMs = d - new Date();
    const abs = Math.abs(diffMs);
    const UNITS = [
      ["year", 365*24*3600*1000],
      ["month", 30*24*3600*1000],
      ["day", 24*3600*1000],
      ["hour", 3600*1000],
      ["minute", 60*1000],
      ["second", 1000],
    ];
    const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    for (const [unit, ms] of UNITS) {
      if (abs >= ms || unit === "second") {
        const value = Math.round(diffMs / ms);
        return rtf.format(value, unit);
      }
    }
  } catch {}
  return isoString;
}

function escapeHtml(s) {
  return (s || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function makeCard(item) {
  const card = document.createElement("article");
  card.className = "card";
  const category = item.category || "General";
  const region = item.region || "World";

  card.innerHTML = `
    <a class="card-link" href="${item.url}" target="_blank" rel="noopener">
      <h3 class="card-title">${escapeHtml(item.title)}</h3>
      <div class="card-meta">
        <span class="badge source">${escapeHtml(item.source || "News")}</span>
        <span class="dot">•</span>
        <time datetime="${item.published_utc}">${fmtRelative(item.published_utc)}</time>
      </div>
      <div class="card-tags">
        <span class="tag">${escapeHtml(category)}</span>
        <span class="tag subtle">${escapeHtml(region)}</span>
      </div>
    </a>
  `;
  return card;
}

function applyFilters(items) {
  return items.filter(it => {
    const cat = (it.category || "General");
    const reg = (it.region || "World");
    const catOk = activeCategory === "all" ? true : cat === activeCategory;
    const regOk = activeRegion ? reg === activeRegion : true;
    return catOk && regOk;
  });
}

function render() {
  const grid = $(`#${GRID_ID}`);
  grid.innerHTML = "";

  if (!rawData || !Array.isArray(rawData.items)) {
    grid.innerHTML = `<p class="empty">No headlines yet.</p>`;
    return;
  }

  const items = applyFilters(rawData.items);
  if (items.length === 0) {
    grid.innerHTML = `<p class="empty">No headlines match the current filters.</p>`;
    return;
  }

  const frag = document.createDocumentFragment();
  for (const it of items) frag.appendChild(makeCard(it));
  grid.appendChild(frag);
}

function setActiveChip(groupSelector, valueToActivate, attrName) {
  // attrName is 'data-filter' (category) or 'data-region'
  const chips = $all(`${groupSelector} .chip[${attrName}]`);
  for (const chip of chips) {
    const isActive =
      chip.getAttribute(attrName) === valueToActivate ||
      (valueToActivate === null && !chip.hasAttribute(attrName));
    chip.classList.toggle("chip-active", isActive);
    chip.setAttribute("aria-pressed", String(isActive));
  }
}

function bindFilters() {
  const filtersEl = $(`#${FILTERS_ID}`);
  if (!filtersEl) return;
  filtersEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".chip");
    if (!btn) return;

    if (btn.hasAttribute("data-filter")) {
      activeCategory = btn.getAttribute("data-filter");
      setActiveChip(`#${FILTERS_ID}`, activeCategory, "data-filter");
      render();
    } else if (btn.hasAttribute("data-region")) {
      activeRegion = btn.getAttribute("data-region");
      // toggle behavior: clicking active region clears it
      if ($(`.chip.chip-active[data-region="${activeRegion}"]`)) {
        activeRegion = null;
        $all(`#${FILTERS_ID} .chip[data-region]`).forEach(c => {
          c.classList.remove("chip-active");
          c.setAttribute("aria-pressed", "false");
        });
      } else {
        $all(`#${FILTERS_ID} .chip[data-region]`).forEach(c => {
          const on = c.getAttribute("data-region") === activeRegion;
          c.classList.toggle("chip-active", on);
          c.setAttribute("aria-pressed", String(on));
        });
      }
      render();
    }
  });
}

function showLastUpdated(iso) {
  let chip = $(`#${LAST_UPDATED_ID}`);
  if (!chip) {
    chip = document.createElement("div");
    chip.id = LAST_UPDATED_ID;
    chip.className = "chip";
    const host =
      document.querySelector(".toolbar") ||
      document.querySelector(".news-toolbar") ||
      document.querySelector(".news-header") ||
      document.getElementById("news-panel") ||
      document.body;
    host.appendChild(chip);
  }
  chip.textContent = `Last updated: ${fmtRelative(iso)}`;
  chip.title = new Date(iso).toLocaleString();
}

async function loadJson() {
  const url = `${JSON_URL}?t=${Date.now()}`; // cache-buster
  const resp = await fetch(url, { cache: "no-store" });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return await resp.json();
}

function normalize(data) {
  if (!data || !Array.isArray(data.items)) return { items: [] };

  // Step A: remove duplicates first
  const deduped = dedupeItems(data.items.slice());

  // Step B: assign stable first-seen sequence using the dedupe identity
  assignStableSeq(deduped);

  // Step C: sort by first-seen sequence (desc), tiebreaker by published time
  deduped.sort((a, b) => {
    const d = (b.__seq || 0) - (a.__seq || 0);
    if (d !== 0) return d;
    const tb = Date.parse(b.published_utc || b.published || 0) || 0;
    const ta = Date.parse(a.published_utc || a.published || 0) || 0;
    return tb - ta;
  });

  return { ...data, items: deduped };
}

function flashUpdated() {
  const chip = $(`#${LAST_UPDATED_ID}`);
  if (!chip) return;
  chip.classList.add("pulse");
  setTimeout(() => chip.classList.remove("pulse"), 1200);
}

// ---------- bootstrap + refresh ----------
async function bootstrap() {
  try {
    const data = await loadJson();
    rawData = normalize(data);
    render();
    if (rawData.generated_utc) {
      showLastUpdated(rawData.generated_utc);
      lastStamp = rawData.generated_utc;
    }
  } catch (err) {
    console.error("Failed to load headlines.json", err);
    $(`#${GRID_ID}`).innerHTML = `
      <p class="empty">Couldn’t load headlines. <a href="${JSON_URL}" target="_blank" rel="noopener">Check JSON</a>.</p>
    `;
  }
}

function autoRefresh() {
  setInterval(async () => {
    try {
      const data = await loadJson();
      if (data.generated_utc && data.generated_utc !== lastStamp) {
        rawData = normalize(data);
        render();
        showLastUpdated(data.generated_utc);
        lastStamp = data.generated_utc;
        flashUpdated();
      }
    } catch (e) {
      console.warn("Auto-refresh failed:", e.message);
    }
  }, REFRESH_MS);
}

// Init
bindFilters();
bootstrap();
autoRefresh();
