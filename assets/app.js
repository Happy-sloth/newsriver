// Minimal client for NewsRiver
// - fetches headlines.json (no-cache)
// - renders cards newest-first
// - adds "Last updated" chip
// - supports category + region filters
// - auto-refreshes every 5 minutes (can change REFRESH_MS)

const GRID_ID = "grid";
const FILTERS_ID = "filters";
const LAST_UPDATED_ID = "last-updated";
const JSON_URL = "headlines.json";
const REFRESH_MS = 5 * 60 * 1000; // 5 minutes

let rawData = null;
let activeCategory = "all";  // "all" | "Sports" | "General" | anything else if you add later
let activeRegion = null;     // null = any | "Canada" | "US" | "World"

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

function escapeHtml(s) {
  return (s || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
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
  const grid = $( `#${GRID_ID}` );
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
    const isActive = chip.getAttribute(attrName) === valueToActivate ||
                     (valueToActivate === null && !chip.hasAttribute(attrName)); // not used here
    chip.classList.toggle("chip-active", isActive);
    chip.setAttribute("aria-pressed", String(isActive));
  }
}

function bindFilters() {
  const filtersEl = $(`#${FILTERS_ID}`);
  filtersEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".chip");
    if (!btn) return;

    if (btn.hasAttribute("data-filter")) {
      activeCategory = btn.getAttribute("data-filter");
      setActiveChip(`#${FILTERS_ID}`, activeCategory, "data-filter");
      render();
    } else if (btn.hasAttribute("data-region")) {
      activeRegion = btn.getAttribute("data-region");
      // Make only one region chip active at a time; clicking the same chip again clears it
      if ($(`.chip.chip-active[data-region="${activeRegion}"]`)) {
        // toggle off
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
    $(".toolbar").appendChild(chip);
  }
  chip.textContent = `Last updated: ${fmtRelative(iso)}`;
  chip.title = new Date(iso).toLocaleString();
}

async function loadJson() {
  const url = `${JSON_URL}?t=${Date.now()}`; // cache-buster
  const resp = await fetch(url, { cache: "no-store" });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const data = await resp.json();
  return data;
}

let lastStamp = null;

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
    $( `#${GRID_ID}` ).innerHTML = `
      <p class="empty">Couldn’t load headlines. <a href="${JSON_URL}" target="_blank" rel="noopener">Check JSON</a>.</p>
    `;
  }
}

function normalize(data) {
  // Keep structure flexible across Step 1/2/3
  if (!data || !Array.isArray(data.items)) return { items: [] };
  // Ensure newest first just in case
  const items = [...data.items].sort((a, b) => {
    return new Date(b.published_utc) - new Date(a.published_utc);
  });
  return { ...data, items };
}

function autoRefresh() {
  setInterval(async () => {
    try {
      const data = await loadJson();
      // Only re-render if the generated timestamp changed
      if (data.generated_utc && data.generated_utc !== lastStamp) {
        rawData = normalize(data);
        render();
        showLastUpdated(data.generated_utc);
        lastStamp = data.generated_utc;
        flashUpdated();
      }
    } catch (e) {
      // Silently ignore transient fetch failures
      console.warn("Auto-refresh failed:", e.message);
    }
  }, REFRESH_MS);
}

function flashUpdated() {
  const chip = $(`#${LAST_UPDATED_ID}`);
  if (!chip) return;
  chip.classList.add("pulse");
  setTimeout(() => chip.classList.remove("pulse"), 1200);
}

// Init
bindFilters();
bootstrap();
autoRefresh();
