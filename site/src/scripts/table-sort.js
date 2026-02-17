/**
 * Client-side column sorting for HTML tables.
 *
 * Attach to a table by calling:
 *   initTableSort("my-table-id");
 *
 * Cells with a `data-sort-value` attribute are sorted numerically by that
 * value; all other cells are sorted lexicographically by text content.
 */

export function initTableSort(tableId) {
  var table = document.getElementById(tableId);
  if (!table) return;

  var thead = table.querySelector("thead");
  var tbody = table.querySelector("tbody");
  if (!thead || !tbody) return;

  var ths = thead.querySelectorAll("th");

  // Add sort arrows to each header
  ths.forEach(function (th) {
    var arrow = document.createElement("span");
    arrow.className = "sort-arrow";
    arrow.textContent = "\u25B2";
    th.appendChild(arrow);
  });

  var curCol = -1;
  var asc = true;

  ths.forEach(function (th, idx) {
    th.addEventListener("click", function () {
      if (curCol === idx) {
        asc = !asc;
      } else {
        ths.forEach(function (h) {
          h.classList.remove("sort-asc", "sort-desc");
        });
        curCol = idx;
        asc = true;
      }

      th.classList.toggle("sort-asc", asc);
      th.classList.toggle("sort-desc", !asc);
      th.querySelector(".sort-arrow").textContent = asc ? "\u25B2" : "\u25BC";

      var rows = Array.from(tbody.querySelectorAll("tr"));
      rows.sort(function (a, b) {
        var cA = a.children[idx],
          cB = b.children[idx];
        var sA = cA.getAttribute("data-sort-value");
        var sB = cB.getAttribute("data-sort-value");
        if (sA !== null && sB !== null) {
          var nA = parseFloat(sA) || 0,
            nB = parseFloat(sB) || 0;
          return asc ? nA - nB : nB - nA;
        }
        var tA = (cA.textContent || "").trim().toLowerCase();
        var tB = (cB.textContent || "").trim().toLowerCase();
        if (tA < tB) return asc ? -1 : 1;
        if (tA > tB) return asc ? 1 : -1;
        return 0;
      });
      rows.forEach(function (r) {
        tbody.appendChild(r);
      });
    });
  });
}
