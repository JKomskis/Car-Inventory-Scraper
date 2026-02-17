/**
 * Eleventy global data file.
 *
 * Reads all inventory snapshots from inventory/<year>/<month>/*.json.gz
 * and exposes them to templates as:
 *
 *   inventory.latest   – array of items from the most recent snapshot
 *   inventory.snapshots – array of { date, items } sorted chronologically
 */

import fs from "node:fs/promises";
import path from "node:path";
import zlib from "node:zlib";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const INVENTORY_DIR = path.resolve(__dirname, "../../../inventory");
const SNAPSHOT_RE = /inventory_(\d{4})_(\d{2})_(\d{2})\.json\.gz$/;

async function loadSnapshots() {
  const snapshots = [];

  let entries;
  try {
    entries = await fs.readdir(INVENTORY_DIR, { recursive: true });
  } catch {
    console.warn(`[inventory data] Directory not found: ${INVENTORY_DIR}`);
    return { latest: [], snapshots: [], trimByDealer: buildTrimByDealer([]) };
  }

  const reads = entries
    .map((entry) => ({ entry, match: entry.match(SNAPSHOT_RE) }))
    .filter(({ match }) => match != null)
    .map(async ({ entry, match }) => {
      const date = `${match[1]}-${match[2]}-${match[3]}`;
      const filePath = path.join(INVENTORY_DIR, entry);
      try {
        const compressed = await fs.readFile(filePath);
        const json = zlib.gunzipSync(compressed).toString("utf-8");
        return { date, items: JSON.parse(json) };
      } catch (err) {
        console.warn(`[inventory data] Failed to read ${filePath}: ${err.message}`);
        return null;
      }
    });

  const results = await Promise.all(reads);
  for (const snap of results) {
    if (snap) snapshots.push(snap);
  }

  snapshots.sort((a, b) => a.date.localeCompare(b.date));

  const latest = snapshots.length > 0 ? snapshots[snapshots.length - 1].items : [];
  const trimByDealer = buildTrimByDealer(latest);

  // Count unique VINs seen across all snapshots
  const allVins = new Set();
  for (const snap of snapshots) {
    for (const item of snap.items) {
      if (item.vin) allVins.add(item.vin);
    }
  }
  const uniqueVinCount = allVins.size;

  // Pre-compute chart datasets so the HTML only carries minimal JSON
  const charts = buildChartData(latest, snapshots);

  // Enrich each snapshot with its own trimByDealer and snapshot-level charts
  for (const snap of snapshots) {
    snap.trimByDealer = buildTrimByDealer(snap.items);
    snap.charts = buildSnapshotChartData(snap.items);
  }

  return { latest, snapshots, trimByDealer, uniqueVinCount, charts };
}

/**
 * Build a pivot structure for the trim × dealer summary table.
 * Returns { dealers: [...], trims: [...], counts: { "Dealer||Trim": N, ... },
 *           dealerTotals: { dealer: N }, trimTotals: { trim: N }, grandTotal: N }
 */
function buildTrimByDealer(items) {
  const counts = {};
  const dealersSet = new Set();
  const trimMinPrice = {};

  for (const item of items) {
    const dealer = item.dealer_name || "Unknown";
    const trim = item.trim || "Unknown";
    const key = `${dealer}||${trim}`;
    counts[key] = (counts[key] || 0) + 1;
    dealersSet.add(dealer);

    const bp = item.base_price;
    if (typeof bp === "number") {
      if (!(trim in trimMinPrice) || bp < trimMinPrice[trim]) {
        trimMinPrice[trim] = bp;
      }
    }
  }

  const dealers = Array.from(dealersSet).sort();
  const trims = Array.from(
    new Set(Object.keys(counts).map((k) => k.split("||")[1]))
  ).sort((a, b) => {
    const aHas = a in trimMinPrice ? 0 : 1;
    const bHas = b in trimMinPrice ? 0 : 1;
    if (aHas !== bHas) return aHas - bHas;
    return (trimMinPrice[a] || 0) - (trimMinPrice[b] || 0) || a.localeCompare(b);
  });

  // Row and column totals
  const dealerTotals = {};
  const trimTotals = {};
  let grandTotal = 0;

  for (const dealer of dealers) {
    dealerTotals[dealer] = 0;
    for (const trim of trims) {
      const n = counts[`${dealer}||${trim}`] || 0;
      dealerTotals[dealer] += n;
      trimTotals[trim] = (trimTotals[trim] || 0) + n;
      grandTotal += n;
    }
  }

  return { dealers, trims, counts, dealerTotals, trimTotals, grandTotal };
}

/**
 * Pre-compute time-series chart datasets for the index/dashboard page.
 */
function buildChartData(items, snapshots) {
  // Inventory count over time
  const history = {
    labels: snapshots.map((s) => s.date),
    data: snapshots.map((s) => s.items.length),
  };

  // Dealer count over time
  const allDealers = Array.from(
    new Set(snapshots.flatMap((s) => s.items.map((it) => it.dealer_name || "Unknown")))
  ).sort();
  const dealerHistory = {
    labels: snapshots.map((s) => s.date),
    datasets: allDealers.map((dealer) => ({
      label: dealer,
      data: snapshots.map((s) =>
        s.items.filter((it) => (it.dealer_name || "Unknown") === dealer).length
      ),
    })),
  };

  // Trim count over time
  const allTrims = Array.from(
    new Set(snapshots.flatMap((s) => s.items.map((it) => it.trim || "Unknown")))
  ).sort();
  const trimHistory = {
    labels: snapshots.map((s) => s.date),
    datasets: allTrims.map((trim) => ({
      label: trim,
      data: snapshots.map((s) =>
        s.items.filter((it) => (it.trim || "Unknown") === trim).length
      ),
    })),
  };

  // Status count over time
  const allStatuses = Array.from(
    new Set(snapshots.flatMap((s) => s.items.map((it) => it.status || "Unknown")))
  ).sort();
  const statusHistory = {
    labels: snapshots.map((s) => s.date),
    datasets: allStatuses.map((status) => ({
      label: status,
      data: snapshots.map((s) =>
        s.items.filter((it) => (it.status || "Unknown") === status).length
      ),
    })),
  };

  return { history, dealerHistory, trimHistory, statusHistory };
}

/**
 * Build only the snapshot-level chart datasets (no time-series).
 * Used to enrich individual snapshot pages.
 */
function buildSnapshotChartData(items) {
  function countBy(arr, key) {
    const counts = {};
    for (const item of arr) {
      const val = item[key] || "Unknown";
      counts[val] = (counts[val] || 0) + 1;
    }
    const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    return {
      labels: sorted.map((e) => e[0]),
      data: sorted.map((e) => e[1]),
    };
  }

  const byDealer = countBy(items, "dealer_name");
  const byTrim = countBy(items, "trim");

  const prices = items
    .map((it) => it.total_price)
    .filter((p) => p != null)
    .sort((a, b) => a - b);

  let priceDist = { labels: [], data: [] };
  if (prices.length > 0) {
    const minP = Math.floor(prices[0] / 1000) * 1000;
    const maxP = Math.ceil(prices[prices.length - 1] / 1000) * 1000;
    const labels = [];
    const data = [];
    for (let b = minP; b < maxP; b += 1000) {
      labels.push("$" + b.toLocaleString("en-US"));
      data.push(prices.filter((p) => p >= b && p < b + 1000).length);
    }
    priceDist = { labels, data };
  }

  return { byDealer, byTrim, priceDist };
}

export default loadSnapshots;
