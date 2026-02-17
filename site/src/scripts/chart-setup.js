/**
 * Shared Chart.js setup — palette and canvas helper.
 * Each page imports only the Chart.js components it needs.
 */
export const palette = [
  "#4e79a7","#f28e2b","#e15759","#76b7b2","#59a14f",
  "#edc948","#b07aa1","#ff9da7","#9c755f","#bab0ac",
  "#86bcb6","#8cd17d","#b6992d","#499894","#d37295",
];

/** Safely grab a canvas element — returns null if missing. */
export function getCanvas(id) {
  return document.getElementById(id);
}
