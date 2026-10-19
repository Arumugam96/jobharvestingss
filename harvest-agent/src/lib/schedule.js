// Shared by both Rule Engine pages: the "Automatic scheduled runs" status line.

/** "Next run: Tue, 20 Oct, 07:00 am" in the viewer's locale/timezone, from the
 * scheduler's ISO next_run (GET /harvest-schedule/status); "" when unknown. */
export function fmtNextRun(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  });
}
