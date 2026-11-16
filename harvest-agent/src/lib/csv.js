/* CSV helpers for client-side exports (JobsPage, PhoneLogsPage). */

// A cell starting with one of these is evaluated as a formula by Excel / Sheets /
// LibreOffice ("CSV injection"), e.g. =HYPERLINK(...) or a DDE payload.
const FORMULA_START = /^[=+\-@\t\r]/;
// A bare phone-like value ("+91 9876543210", "-1", "(555) 123-4567"): only digits
// and phone punctuation, so even if a spreadsheet evaluates it, it can't call a
// function or reach DDE. Left untouched so phone numbers read cleanly.
const PHONE_LIKE = /^[+-]?[0-9 ().-]*[0-9][0-9 ().-]*$/;

/**
 * One CSV cell: neutralises formula-like values by prefixing "'" (unless the value
 * is a plain phone number / number), doubles embedded quotes and wraps the result
 * in double quotes.
 * @param {*} value the cell value (stringified with String())
 * @returns {string} the quoted, escaped cell
 */
export function csvCell(value) {
  let s = String(value);
  if (FORMULA_START.test(s) && !PHONE_LIKE.test(s)) s = "'" + s;
  return '"' + s.replace(/"/g, '""') + '"';
}

/**
 * Save a CSV file in the browser (UTF-8 with BOM so Excel reads non-ASCII names).
 * @param {string} filename
 * @param {string[]} head column titles
 * @param {Array<Array<*>>} rows cell values (null/undefined → empty)
 */
export function downloadCsv(filename, head, rows) {
  const line = (cells) => cells.map((v) => csvCell(v == null ? "" : v)).join(",");
  saveBlob(filename, new Blob(["﻿" + [line(head), ...rows.map(line)].join("\r\n")], { type: "text/csv;charset=utf-8;" }));
}

/**
 * Save rows as a JSON array of objects keyed by the column titles.
 * @param {string} filename
 * @param {string[]} head column titles (the object keys)
 * @param {Array<Array<*>>} rows cell values
 */
export function downloadJson(filename, head, rows) {
  const items = rows.map((r) => Object.fromEntries(head.map((h, i) => [h, r[i] ?? ""])));
  saveBlob(filename, new Blob([JSON.stringify(items, null, 2)], { type: "application/json" }));
}

/** Hand a Blob to the browser as a file download. */
export function saveBlob(filename, blob) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/**
 * Every item of a paginated list endpoint ({ items, total }), fetched page by page.
 * @param {Function} fetcher (params) => Promise<{items, total}>
 * @param {object} params filters (page / page_size are set here)
 * @param {number} [maxPages=100] safety cap (100 × 200 = 20k rows)
 */
export async function fetchAllPages(fetcher, params, maxPages = 100) {
  const pageSize = 200;
  let all = [];
  for (let page = 1; page <= maxPages; page += 1) {
    const res = await fetcher({ ...params, page, page_size: pageSize });
    const items = res?.items || [];
    all = all.concat(items);
    if (!items.length || all.length >= (res?.total ?? 0)) break;
  }
  return all;
}
