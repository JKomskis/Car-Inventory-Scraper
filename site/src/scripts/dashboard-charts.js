/**
 * Dashboard charts — time-series only (index page).
 * Line charts for total inventory, dealer, trim, and status over time.
 */
import {
  Chart,
  LineController,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Filler,
  Legend,
  Tooltip,
} from "chart.js";
import { palette, getCanvas, htmlLegendPlugin } from "./chart-setup.js";

Chart.register(
  LineController,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Filler,
  Legend,
  Tooltip
);
Chart.register(htmlLegendPlugin);

const { history, dealerHistory, trimHistory, statusHistory } =
  window.__CHART_DATA__ || {};

// ── Total Inventory Over Time ──
if (history?.labels?.length) {
  new Chart(getCanvas("chart-history"), {
    type: "line",
    data: {
      labels: history.labels,
      datasets: [{
        label: "Total Vehicles",
        data: history.data,
        borderColor: "#4e79a7",
        backgroundColor: "rgba(78, 121, 167, 0.1)",
        fill: true,
        tension: 0.3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      aspectRatio: 2,
      scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
      plugins: {
        legend: { display: false },
      }
    },
  });
}

// Helper for multi-series line charts
function multiLinechart(canvasId, dataset, legendContainerID) {
  if (!dataset?.labels?.length) return;
  new Chart(getCanvas(canvasId), {
    type: "line",
    data: {
      labels: dataset.labels,
      datasets: dataset.datasets.map((ds, i) => ({
        label: ds.label,
        data: ds.data,
        borderColor: palette[i % palette.length],
        backgroundColor: "transparent",
        tension: 0.3,
        pointRadius: 3,
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      aspectRatio: 2,
      scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
      plugins: {
        legend: { display: false },
        htmlLegend: { containerID: legendContainerID },
      },
    },
  });
}

// ── Dealer Inventory Over Time ──
multiLinechart("chart-dealer-history", dealerHistory, "legend-dealer-history");

// ── Trim Count Over Time ──
multiLinechart("chart-trim-history", trimHistory, "legend-trim-history");

// ── Vehicle Status Over Time ──
multiLinechart("chart-status-history", statusHistory, "legend-status-history");
