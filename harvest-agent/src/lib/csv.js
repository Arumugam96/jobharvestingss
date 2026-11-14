/* CSV cell escaping for client-side exports (JobsPage). */

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
