/**
 * Snapshot charts — bar and doughnut charts for a single inventory snapshot.
 * Used on per-date inventory pages.
 */
import {
  Chart,
  BarController,
  DoughnutController,
  CategoryScale,
  LinearScale,
  BarElement,
  ArcElement,
  Legend,
  Tooltip,
} from "chart.js";
import ChartDataLabels from "chartjs-plugin-datalabels";
import { palette, getCanvas } from "./chart-setup.js";

Chart.register(
  BarController,
  DoughnutController,
  CategoryScale,
  LinearScale,
  BarElement,
  ArcElement,
  Legend,
  Tooltip,
  ChartDataLabels
);

const { byDealer, byTrim, priceDist } = window.__CHART_DATA__ || {};

// ── Inventory by Dealer ──
if (byDealer?.labels?.length) {
  new Chart(getCanvas("chart-by-dealer"), {
    type: "bar",
    data: {
      labels: byDealer.labels,
      datasets: [{
        label: "Vehicles",
        data: byDealer.data,
        backgroundColor: palette.slice(0, byDealer.labels.length),
      }],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      plugins: {
        legend: { display: false },
        datalabels: {
          anchor: "end",
          align: "end",
          color: "#333",
          font: { weight: "bold" },
        },
      },
      scales: { x: { beginAtZero: true, ticks: { precision: 0 } } },
    },
  });
}

// ── Inventory by Trim ──
if (byTrim?.labels?.length) {
  new Chart(getCanvas("chart-by-trim"), {
    type: "doughnut",
    data: {
      labels: byTrim.labels,
      datasets: [{
        data: byTrim.data,
        backgroundColor: palette.slice(0, byTrim.labels.length),
      }],
    },
    options: {
      responsive: true,
      plugins: {
        datalabels: {
          color: "#fff",
          font: { weight: "bold", size: 11 },
          formatter: (value, ctx) => {
            const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
            const pct = ((value / total) * 100).toFixed(0);
            return value > 0 ? `${value} (${pct}%)` : "";
          },
        },
      },
    },
  });
}

// ── Price Distribution ──
if (priceDist?.labels?.length) {
  new Chart(getCanvas("chart-price-dist"), {
    type: "bar",
    data: {
      labels: priceDist.labels,
      datasets: [{
        label: "Vehicles",
        data: priceDist.data,
        backgroundColor: "#4e79a7",
      }],
    },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false },
        datalabels: {
          anchor: "end",
          align: "end",
          color: "#333",
          font: { weight: "bold" },
          display: (ctx) => ctx.dataset.data[ctx.dataIndex] > 0,
        },
      },
      scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
    },
  });
}
