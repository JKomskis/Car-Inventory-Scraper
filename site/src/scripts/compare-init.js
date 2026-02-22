/**
 * Compare page entry-point.
 *
 * Sets date pickers to yesterday / today, then fetches and diffs snapshots
 * whenever either date changes. Aborts in-flight requests on new changes.
 */

import { diffSnapshots } from "./compare-diff.js";
import {
  renderSummary,
  renderAdded,
  renderRemoved,
  renderModified,
} from "./compare-render.js";

const REPO_BASE =
  "https://raw.githubusercontent.com/JKomskis/Car-Inventory-Scraper/main/inventory";

/* ── Date helpers ────────────────────────────────────────── */

function pad(n) {
  return String(n).padStart(2, "0");
}

function dateToPath(dateStr) {
  const [y, m, d] = dateStr.split("-");
  return `${REPO_BASE}/${y}/${m}/inventory_${y}_${m}_${d}.json.gz`;
}

function formatIso(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/* ── Fetch + decompress ──────────────────────────────────── */

async function fetchSnapshot(dateStr, signal) {
  const url = dateToPath(dateStr);
  const res = await fetch(url, { signal });
  if (!res.ok) {
    if (res.status === 404) throw new Error(`No snapshot found for ${dateStr}`);
    throw new Error(`Failed to fetch ${dateStr}: ${res.status} ${res.statusText}`);
  }
  const ds = new DecompressionStream("gzip");
  const decompressed = res.body.pipeThrough(ds);
  const text = await new Response(decompressed).text();
  return JSON.parse(text);
}

/* ── Main ────────────────────────────────────────────────── */

const dateA = document.getElementById("date-a");
const dateB = document.getElementById("date-b");
const statusEl = document.getElementById("compare-status");
const summaryEl = document.getElementById("compare-summary");
const addedSection = document.getElementById("added-section");
const removedSection = document.getElementById("removed-section");
const modifiedSection = document.getElementById("modified-section");

// Default dates: yesterday and today
const today = new Date();
const yesterday = new Date(today);
yesterday.setDate(yesterday.getDate() - 1);
dateA.value = formatIso(yesterday);
dateB.value = formatIso(today);

let controller = null;

async function runCompare() {
  // Abort any in-flight request
  if (controller) controller.abort();
  controller = new AbortController();
  const { signal } = controller;

  const valA = dateA.value;
  const valB = dateB.value;
  if (!valA || !valB) return;

  // Show loading state
  statusEl.textContent = "Loading snapshots…";
  statusEl.className = "compare-status loading";
  summaryEl.innerHTML = "";
  addedSection.querySelector(".section-body").innerHTML = "";
  removedSection.querySelector(".section-body").innerHTML = "";
  modifiedSection.querySelector(".section-body").innerHTML = "";

  try {
    const [itemsA, itemsB] = await Promise.all([
      fetchSnapshot(valA, signal),
      fetchSnapshot(valB, signal),
    ]);

    // Compute diff
    const diff = diffSnapshots(itemsA, itemsB);

    // Count unchanged
    const vinsA = new Set(itemsA.map((i) => i.vin));
    const vinsB = new Set(itemsB.map((i) => i.vin));
    let unchangedCount = 0;
    for (const vin of vinsA) {
      if (vinsB.has(vin)) unchangedCount++;
    }
    unchangedCount -= diff.modified.length;
    diff.unchanged = unchangedCount;

    // Render
    renderSummary(diff, summaryEl);
    renderAdded(diff.added, addedSection);
    renderRemoved(diff.removed, removedSection);
    renderModified(diff.modified, modifiedSection);

    statusEl.textContent = "";
    statusEl.className = "compare-status";
  } catch (err) {
    if (err.name === "AbortError") return; // superseded by a newer request
    statusEl.textContent = err.message;
    statusEl.className = "compare-status error";
  }
}

// Fire on any date change — no button needed
dateA.addEventListener("change", runCompare);
dateB.addEventListener("change", runCompare);

// Run immediately on page load
runCompare();
