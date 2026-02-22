/**
 * Render diff results (added / removed / modified) into the DOM.
 */

import { initTableSort } from "./table-sort.js";
import { fieldLabel } from "./compare-diff.js";

/* ── Helpers ─────────────────────────────────────────────── */

function dollar(n) {
  if (n == null) return "—";
  return "$" + Number(n).toLocaleString("en-US");
}

function escapeHtml(str) {
  if (str == null) return "—";
  const d = document.createElement("div");
  d.textContent = String(str);
  return d.innerHTML;
}

function formatValue(val) {
  if (val == null) return "—";
  if (Array.isArray(val)) {
    if (val.length === 0) return "—";
    return val
      .map((v) =>
        typeof v === "object" && v.name
          ? `${escapeHtml(v.name)}${v.price != null ? " (" + dollar(v.price) + ")" : ""}`
          : escapeHtml(String(v))
      )
      .join("<br>");
  }
  if (typeof val === "number") return escapeHtml(val.toLocaleString("en-US"));
  return escapeHtml(String(val));
}

/** Is this field a dollar-amount field? */
const PRICE_FIELDS = new Set([
  "msrp",
  "base_price",
  "total_price",
  "total_packages_price",
  "dealer_accessories_price",
  "adjustments",
]);

function formatFieldValue(field, val) {
  if (PRICE_FIELDS.has(field) && val != null) return dollar(val);
  return formatValue(val);
}

/* ── Summary ─────────────────────────────────────────────── */

export function renderSummary(diff, container) {
  container.innerHTML = `
    <span class="badge badge-added">Added: ${diff.added.length}</span>
    <span class="badge badge-removed">Removed: ${diff.removed.length}</span>
    <span class="badge badge-modified">Modified: ${diff.modified.length}</span>
    <span class="badge">Unchanged: ${diff.unchanged ?? "—"}</span>
  `;
}

/* ── Section toggle helper ───────────────────────────────── */

function setupSectionToggle(section) {
  const header = section.querySelector(".section-header");
  const body = section.querySelector(".section-body");
  if (!header || !body) return;
  header.addEventListener("click", () => {
    const collapsed = body.style.display === "none";
    body.style.display = collapsed ? "" : "none";
    header.classList.toggle("collapsed", !collapsed);
  });
}

/* ── Added / Removed tables ──────────────────────────────── */

const VEHICLE_COLUMNS = [
  { key: "vin", label: "VIN" },
  { key: "dealer_name", label: "Dealer" },
  { key: "year", label: "Year" },
  { key: "trim", label: "Trim" },
  { key: "exterior_color", label: "Ext. Color" },
  { key: "status", label: "Status" },
  { key: "msrp", label: "MSRP", dollar: true },
  { key: "total_price", label: "Total Price", dollar: true },
];

function buildVehicleTable(items, tableId) {
  if (items.length === 0) return "<p class='meta'>None</p>";

  let html = `<div style="overflow-x:auto;"><table id="${tableId}"><thead><tr>`;
  for (const col of VEHICLE_COLUMNS) {
    html += `<th>${escapeHtml(col.label)}</th>`;
  }
  html += "</tr></thead><tbody>";

  for (const item of items) {
    html += "<tr>";
    for (const col of VEHICLE_COLUMNS) {
      const val = item[col.key];
      const display = col.dollar ? dollar(val) : escapeHtml(val);
      const sortAttr = col.dollar && val != null ? ` data-sort-value="${val}"` : "";
      html += `<td${sortAttr}>${display}</td>`;
    }
    html += "</tr>";
  }

  html += "</tbody></table></div>";
  return html;
}

export function renderAdded(items, section) {
  const header = section.querySelector(".section-header");
  const body = section.querySelector(".section-body");
  header.innerHTML = `<span class="toggle-icon">▾</span> Vehicles Added <span class="badge badge-added">${items.length}</span>`;
  body.innerHTML = buildVehicleTable(items, "table-added");
  setupSectionToggle(section);
  if (items.length > 0) initTableSort("table-added");
}

export function renderRemoved(items, section) {
  const header = section.querySelector(".section-header");
  const body = section.querySelector(".section-body");
  header.innerHTML = `<span class="toggle-icon">▾</span> Vehicles Removed <span class="badge badge-removed">${items.length}</span>`;
  body.innerHTML = buildVehicleTable(items, "table-removed");
  setupSectionToggle(section);
  if (items.length > 0) initTableSort("table-removed");
}

/* ── Modified table ──────────────────────────────────────── */

function formatChange(change) {
  // Smart array diff for packages / dealer_accessories
  if (change.type === "array") {
    return formatArrayDiff(change.diff);
  }

  const oldVal = formatFieldValue(change.field, change.oldValue);
  const newVal = formatFieldValue(change.field, change.newValue);

  // Determine CSS classes for price changes
  let newClass = "diff-field-new";
  if (PRICE_FIELDS.has(change.field) && change.oldValue != null && change.newValue != null) {
    newClass += change.newValue < change.oldValue ? " adj-neg" : change.newValue > change.oldValue ? " adj-pos" : "";
  }

  return `<span class="diff-field-old">${oldVal}</span> <span class="diff-arrow">→</span> <span class="${newClass}">${newVal}</span>`;
}

function formatArrayDiff(diff) {
  const lines = [];

  for (const item of diff.added) {
    const price = item.price != null ? ` (${dollar(item.price)})` : "";
    lines.push(`<span class="diff-array-added">+ ${escapeHtml(item.name)}${price}</span>`);
  }

  for (const item of diff.removed) {
    const price = item.price != null ? ` (${dollar(item.price)})` : "";
    lines.push(`<span class="diff-array-removed">− ${escapeHtml(item.name)}${price}</span>`);
  }

  for (const item of diff.priceChanged) {
    const cls = item.newPrice < item.oldPrice ? "adj-neg" : item.newPrice > item.oldPrice ? "adj-pos" : "";
    lines.push(
      `<span class="diff-array-changed">~ ${escapeHtml(item.name)}: ` +
      `<span class="diff-field-old">${dollar(item.oldPrice)}</span> ` +
      `<span class="diff-arrow">→</span> ` +
      `<span class="diff-field-new ${cls}">${dollar(item.newPrice)}</span></span>`
    );
  }

  return lines.join("<br>");
}

export function renderModified(items, section) {
  const header = section.querySelector(".section-header");
  const body = section.querySelector(".section-body");
  header.innerHTML = `<span class="toggle-icon">▾</span> Vehicles Modified <span class="badge badge-modified">${items.length}</span>`;

  if (items.length === 0) {
    body.innerHTML = "<p class='meta'>None</p>";
    setupSectionToggle(section);
    return;
  }

  let html = `<div style="overflow-x:auto;"><table id="table-modified"><thead><tr>
    <th>VIN</th><th>Dealer</th><th>Trim</th><th>Field</th><th>Change</th>
  </tr></thead><tbody>`;

  for (const mod of items) {
    const span = mod.changes.length;
    for (let i = 0; i < mod.changes.length; i++) {
      const ch = mod.changes[i];
      html += "<tr>";
      if (i === 0) {
        html += `<td rowspan="${span}">${escapeHtml(mod.vin)}</td>`;
        html += `<td rowspan="${span}">${escapeHtml(mod.dealer_name)}</td>`;
        html += `<td rowspan="${span}">${escapeHtml(mod.trim)}</td>`;
      }
      html += `<td>${escapeHtml(fieldLabel(ch.field))}</td>`;
      html += `<td>${formatChange(ch)}</td>`;
      html += "</tr>";
    }
  }

  html += "</tbody></table></div>";
  body.innerHTML = html;
  setupSectionToggle(section);
}
