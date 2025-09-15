// scripts/fetch-tour.mjs
// Builds tour.json from official venue pages/ICS listed in venues.json.
// Node 18+ (has global fetch). Dependencies: cheerio, luxon, node-ical.

import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { DateTime } from "luxon";
import * as ical from "node-ical";
import { load as loadHTML } from "cheerio";

/* ----------------- CLI ----------------- */
const args = Object.fromEntries(
  process.argv.slice(2).map(s => {
    const [k, v] = s.startsWith("--") ? s.slice(2).split("=") : [s, true];
    return [k, v ?? true];
  })
);

const MANIFEST_PATH = args.manifest || "venues.json";
const OUT_PATH = args.out || "tour.json";               // write at repo root by default
const WINDOW_DAYS = Number(args.window || 60);          // fetch today → +WINDOW_DAYS

/* ----------------- helpers ----------------- */
const sleep = ms => new Promise(r => setTimeout(r, ms));
const sha1 = s => crypto.createHash("sha1").update(s).digest("hex");

const nowUtc = DateTime.utc();
const windowStart = nowUtc.startOf("day");
const windowEnd = windowStart.plus({ days: WINDOW_DAYS });

function toUTC(dt, zone) {
  let d;
  if (dt instanceof Date) d = DateTime.fromJSDate(dt, { zone });
  else if (typeof dt === "string") d = DateTime.fromISO(dt, { zone });
  if (!d || !d.isValid) d = DateTime.fromJSDate(new Date(dt), { zone });
  return d && d.isValid ? d.toUTC() : null;
}

function inWindow(utcDT) {
  return utcDT && utcDT.isValid && utcDT >= windowStart && utcDT <= windowEnd;
}

function stableId({ slug, whenUtc, title }) {
  return sha1(`${slug}::${whenUtc?.toISO() ?? "na"}::${(title || "").trim().toLowerCase()}`).slice(0, 16);
}

async function fetchText(url) {
  const res = await fetch(url, {
    headers: {
      "user-agent": "mypyBITE-tour/0.1 (+contact: your@email)",
      "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    },
    redirect: "follow"
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return await res.text();
}

/* ----------------- ICS ----------------- */
async function parseICS(url, venue) {
  const raw = await fetchText(url);
  const comp = ical.parseICS(raw);
  const out = [];

  for (const key of Object.keys(comp)) {
    const ev = comp[key];
    if (!ev || ev.type !== "VEVENT") continue;

    const startUtc = toUTC(ev.start, venue.tz);
    if (!inWindow(startUtc)) continue;

    const titleRaw = (ev.summary || "").trim();
    const tickets =
      (ev.url || ev.description || "")
        .toString()
        .split(/\s+/)
        .find(u => /^https?:\/\//i.test(u)) || null;

    out.push({
      id: stableId({ slug: venue.slug, whenUtc: startUtc, title: titleRaw }),
      when: startUtc.toISO(),
      artist: titleRaw ? [titleRaw.replace(/\s+\(.*?\)\s*$/, "")] : [],
      venue: venue.name,
      city: venue.city,
      title: titleRaw ? `${titleRaw} • ${venue.name} (${venue.city})` : `${venue.name} (${venue.city})`,
      tickets_url: tickets,
      source: venue.name,
      promoted: false
    });
  }
  return out;
}

/* ----------------- HTML ----------------- */
function pickText($, node, sel) {
  if (!sel) return "";
  const el = sel === "." ? $(node) : $(node).find(sel).first();
  return (el.text() || "").trim();
}
function pickAttr($, node, sel, attr) {
  const el = sel === "." ? $(node) : $(node).find(sel).first();
  return (el.attr?.(attr) || "").trim();
}
function parseLocalDateTime({ dateStr, timeStr, datetimeAttr, zone }) {
  if (datetimeAttr) {
    const d = DateTime.fromISO(datetimeAttr, { zone });
    if (d.isValid) return d;
  }
  const dateClean = (dateStr || "").replace(/\s{2,}/g, " ").trim();
  const timeClean = (timeStr || "").trim() || "20:00";
  let dt =
    DateTime.fromISO(`${dateClean} ${timeClean}`, { zone });
  if (!dt.isValid) dt = DateTime.fromFormat(`${dateClean} ${timeClean}`, "yyyy-MM-dd HH:mm", { zone });
  if (!dt.isValid) dt = DateTime.fromFormat(`${dateClean} ${timeClean}`, "MMM d yyyy HH:mm", { zone });
  if (!dt.isValid) dt = DateTime.fromJSDate(new Date(`${dateClean} ${timeClean}`), { zone });
  return dt.isValid ? dt : null;
}

async function parseHTML(url, venue, selectors) {
  const html = await fetchText(url);
  const $ = loadHTML(html);
  const items = [];

  const itemSel = selectors.item;
  if (!itemSel) return items;

  $(itemSel).each((_, el) => {
    try {
      const title =
        selectors.title_attr
          ? pickAttr($, el, selectors.title || ".", selectors.title_attr)
          : pickText($, el, selectors.title || ".");

      const dateRaw = selectors.date_attr
        ? pickAttr($, el, selectors.date || ".", selectors.date_attr)
        : pickText($, el, selectors.date || ".");

      const timeRaw = selectors.time_attr
        ? pickAttr($, el, selectors.time || ".", selectors.time_attr)
        : pickText($, el, selectors.time || "");

      const datetimeAttr = selectors.datetime_attr
        ? pickAttr($, el, selectors.datetime || selectors.date || ".", selectors.datetime_attr)
        : null;

      const zone = venue.tz || "America/Toronto";
      const startLocal = parseLocalDateTime({ dateStr: dateRaw, timeStr: timeRaw, datetimeAttr, zone });
      if (!startLocal) return;

      const startUtc = startLocal.toUTC();
      if (!inWindow(startUtc)) return;

      // Find ticket link
      let tix = null;
      if (selectors.tickets) {
        const a = selectors.tickets === "." ? $(el) : $(el).find(selectors.tickets).first();
        const href = (a.attr?.("href") || "").trim();
        if (href) tix = href.startsWith("/") ? new URL(href, url).href : href;
      } else {
        const a = $(el).find("a[href*='ticket'],a[href*='tickets'],a[href*='billet']").first();
        const href = (a.attr?.("href") || "").trim();
        if (href) tix = href.startsWith("/") ? new URL(href, url).href : href;
      }

      const titleClean = (title || "").trim();
      const headliner = titleClean.replace(/\s+@.*$|^at\s+/i, "").replace(/\s+\|.+$/, "").trim();

      items.push({
        id: stableId({ slug: venue.slug, whenUtc: startUtc, title: titleClean }),
        when: startUtc.toISO(),
        artist: headliner ? [headliner] : [],
        venue: venue.name,
        city: venue.city,
        title: titleClean
          ? `${titleClean} • ${venue.name} (${venue.city})`
          : `${venue.name} (${venue.city})`,
        tickets_url: tix || null,
        source: venue.name,
        promoted: false
      });
    } catch {
      /* ignore malformed card */
    }
  });

  return items;
}

/* ----------------- main ----------------- */
async function main() {
  const manifest = JSON.parse(await fs.readFile(MANIFEST_PATH, "utf8"));
  const venues = manifest.venues || [];
  const collected = [];

  for (const v of venues) {
    try {
      await sleep(250); // polite spacing
      if (v.source?.type === "ics") {
        collected.push(...(await parseICS(v.source.url, v)));
      } else if (v.source?.type === "html") {
        collected.push(...(await parseHTML(v.source.url, v, v.selectors || {})));
      } else if (v.source?.type === "ics-or-html") {
        // try ICS; fall back to HTML
        try {
          const icsEvents = await parseICS(v.source.url, v);
          if (icsEvents.length) collected.push(...(icsEvents));
          else collected.push(...(await parseHTML(v.source.url, v, v.selectors || {})));
        } catch {
          collected.push(...(await parseHTML(v.source.url, v, v.selectors || {})));
        }
      } else {
        // default: HTML
        collected.push(...(await parseHTML(v.source?.url, v, v.selectors || {})));
      }
      console.log(`[ok] ${v.slug}`);
    } catch (e) {
      console.error(`[warn] ${v.slug}: ${e.message}`);
    }
  }

  // Deduplicate by id, then sort by time
  const map = new Map();
  for (const ev of collected) {
    if (ev?.id) map.set(ev.id, ev);
  }
  const items = Array.from(map.values()).sort((a, b) => Date.parse(a.when) - Date.parse(b.when));

  const out = { generated_at: nowUtc.toISO(), items };

  await fs.mkdir(path.dirname(OUT_PATH) || ".", { recursive: true });
  await fs.writeFile(OUT_PATH, JSON.stringify(out, null, 2), "utf8");
  console.log(`[done] ${items.length} events → ${OUT_PATH}`);
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
