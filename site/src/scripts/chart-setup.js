/**
 * Shared Chart.js setup — palette, canvas helper, and HTML legend plugin.
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

/**
 * Chart.js plugin that renders the legend as HTML outside the canvas.
 *
 * Usage: set `options.plugins.htmlLegend.containerID` to the id of a
 * DOM element that should hold the legend.  Disable the built-in legend
 * with `options.plugins.legend.display = false`.
 */
export const htmlLegendPlugin = {
  id: "htmlLegend",
  afterUpdate(chart, _args, options) {
    const container = document.getElementById(options.containerID);
    if (!container) return;

    // Clear previous content
    container.innerHTML = "";

    const ul = document.createElement("ul");
    ul.className = "chart-legend";

    const items =
      chart.options.plugins.legend.labels.generateLabels(chart);

    items.forEach((item) => {
      const li = document.createElement("li");
      li.className = "chart-legend-item";
      if (item.hidden) li.classList.add("legend-hidden");

      // Colour swatch
      const swatch = document.createElement("span");
      swatch.className = "legend-swatch";
      const skip = new Set(["transparent", "#fff", "#ffffff", "rgba(0,0,0,0)"]);
      const usable = (c) => c && !skip.has(c.toLowerCase().replace(/\s/g, ""));
      const color = [item.fillStyle, item.strokeStyle].find(usable)
        || item.fillStyle || item.strokeStyle;
      swatch.style.background = color;

      // Label text
      const label = document.createElement("span");
      label.className = "legend-label";
      label.textContent = item.text;

      // Toggle visibility on click
      li.addEventListener("click", () => {
        const { type } = chart.config;
        if (type === "pie" || type === "doughnut") {
          chart.toggleDataVisibility(item.index);
        } else {
          chart.setDatasetVisibility(
            item.datasetIndex,
            !chart.isDatasetVisible(item.datasetIndex)
          );
        }
        chart.update();
      });

      li.appendChild(swatch);
      li.appendChild(label);
      ul.appendChild(li);
    });

    container.appendChild(ul);
  },
};
