/**
 * Diff two inventory snapshots by VIN.
 *
 * Returns { added, removed, modified } where:
 *   added    – items in snapshotB but not snapshotA
 *   removed  – items in snapshotA but not snapshotB
 *   modified – items present in both with at least one field change
 */

/** Fields excluded from field-level comparison (change every scrape). */
const IGNORED_FIELDS = new Set(["detail_url", "dealer_url"]);

/** Fields that contain arrays of {name, price} objects. */
const ARRAY_FIELDS = new Set(["packages", "dealer_accessories"]);

/**
 * Serialize a value for deep comparison.
 * Arrays/objects are JSON-stringified; primitives are compared directly.
 */
function canonicalize(val) {
  if (val === null || val === undefined) return null;
  if (Array.isArray(val) || typeof val === "object") return JSON.stringify(val);
  return val;
}

/**
 * Diff two arrays of {name, price} objects (packages or dealer_accessories).
 * Returns { added, removed, priceChanged } or null if arrays are identical.
 */
function diffArrayField(arrA, arrB) {
  const listA = arrA || [];
  const listB = arrB || [];

  const mapA = new Map();
  for (const item of listA) mapA.set(item.name, item);
  const mapB = new Map();
  for (const item of listB) mapB.set(item.name, item);

  const added = [];
  const removed = [];
  const priceChanged = [];

  for (const [name, item] of mapA) {
    if (!mapB.has(name)) {
      removed.push(item);
    } else {
      const itemB = mapB.get(name);
      if (item.price !== itemB.price) {
        priceChanged.push({ name, oldPrice: item.price, newPrice: itemB.price });
      }
    }
  }
  for (const [name, item] of mapB) {
    if (!mapA.has(name)) added.push(item);
  }

  if (added.length === 0 && removed.length === 0 && priceChanged.length === 0) return null;
  return { added, removed, priceChanged };
}

/**
 * Human-readable label for a field name (snake_case → Title Case).
 */
export function fieldLabel(field) {
  return field
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/**
 * @param {Object[]} itemsA – vehicles from the "from" date
 * @param {Object[]} itemsB – vehicles from the "to" date
 * @returns {{ added: Object[], removed: Object[], modified: Object[] }}
 */
export function diffSnapshots(itemsA, itemsB) {
  const mapA = new Map();
  const mapB = new Map();
  for (const item of itemsA) mapA.set(item.vin, item);
  for (const item of itemsB) mapB.set(item.vin, item);

  const added = [];
  const removed = [];
  const modified = [];

  // Removed: in A but not B
  for (const [vin, item] of mapA) {
    if (!mapB.has(vin)) removed.push(item);
  }

  // Added or Modified: iterate B
  for (const [vin, itemB] of mapB) {
    const itemA = mapA.get(vin);
    if (!itemA) {
      added.push(itemB);
      continue;
    }

    // Compare fields
    const changes = [];
    const allKeys = new Set([...Object.keys(itemA), ...Object.keys(itemB)]);
    for (const key of allKeys) {
      if (IGNORED_FIELDS.has(key)) continue;

      // Smart sub-diff for array fields (packages, dealer_accessories)
      if (ARRAY_FIELDS.has(key)) {
        const arrayDiff = diffArrayField(itemA[key], itemB[key]);
        if (arrayDiff) {
          changes.push({ field: key, type: "array", diff: arrayDiff });
        }
        continue;
      }

      const valA = canonicalize(itemA[key]);
      const valB = canonicalize(itemB[key]);
      if (valA !== valB) {
        changes.push({ field: key, oldValue: itemA[key], newValue: itemB[key] });
      }
    }

    if (changes.length > 0) {
      modified.push({
        vin,
        dealer_name: itemB.dealer_name,
        trim: itemB.trim,
        changes,
      });
    }
  }

  // Sort for stable display
  added.sort((a, b) => (a.dealer_name || "").localeCompare(b.dealer_name || "") || (a.vin || "").localeCompare(b.vin || ""));
  removed.sort((a, b) => (a.dealer_name || "").localeCompare(b.dealer_name || "") || (a.vin || "").localeCompare(b.vin || ""));
  modified.sort((a, b) => (a.dealer_name || "").localeCompare(b.dealer_name || "") || (a.vin || "").localeCompare(b.vin || ""));

  return { added, removed, modified };
}
