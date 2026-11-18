import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getContactHistory } from "../api";
import { PHONE_STATUSES, PhoneVerifyStyles, outcomeInfo } from "./PhoneVerify";

/* Per-number call history across jobs. The same recruiter posts many jobs and each
 * harvest makes new job rows, so calls are looked up by the number (its trailing 10
 * digits, server-side), every caller's included. It informs — nothing is blocked:
 * the assigned user decides whether to call, skip or release the job.
 *
 *  * ContactedBadge — compact "Contacted · N" next to the POC name on the jobs table,
 *    coloured by the latest outcome's tone, with a one-line hover summary.
 *  * ContactTimeline — the full timeline (GET /phone-verifications/contact-history),
 *    used by the job detail recruiter card and the Phone logs "By contact" rows.
 *
 * Colours reuse PhoneVerify's tone classes (pvm-hbadge / pvm-tone-*): good = green,
 * info = blue, warn = amber, bad = red. */

/** Short "3 days ago"-style age of an ISO time ("" when missing/invalid). */
export function relTime(iso) {
  const t = iso ? new Date(iso).getTime() : NaN;
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const d = Math.floor(s / 86400);
  if (d === 1) return "yesterday";
  if (d < 30) return `${d} days ago`;
  if (d < 365) return `${Math.floor(d / 30)} mo ago`;
  return `${Math.floor(d / 365)} yr ago`;
}

/** Short local date, e.g. "17 Nov" ("" when missing/invalid). */
export function shortDate(iso) {
  const d = iso ? new Date(iso) : null;
  if (!d || Number.isNaN(d.getTime())) return "";
  try { return d.toLocaleDateString(undefined, { day: "numeric", month: "short" }); } catch { return ""; }
}

/** Tone of a call (good | info | warn | bad): its outcome's, else by status — the same
 * fallback PhoneVerify's call-history sheet uses. */
export const callTone = (status, outcome) =>
  outcomeInfo(status, outcome)?.tone || (status === "answered" ? "good" : "bad");

/** Outcome label of a call, else its status label. */
export const callLabel = (status, outcome) =>
  outcomeInfo(status, outcome)?.label || PHONE_STATUSES[status]?.label || "Called";

/** "revisit in 3 mo" / "do not revisit" for a not-interested call's revisit choice. */
const revisitText = (revisit) =>
  !revisit ? "" : revisit === "Never" ? "do not revisit" : `revisit in ${revisit}`;

/** One-line summary of a call brief: "Not interested · Arun · 3 days ago · revisit in 3 mo". */
export function callSummaryLine(c) {
  if (!c) return "";
  return [callLabel(c.status, c.outcome), c.called_by?.name, relTime(c.called_at), revisitText(c.revisit)]
    .filter(Boolean).join(" · ");
}

/** True when the number has calls on a job other than `jobId` (a summary from the
 * `contacts` map of GET /phone-verifications/latest). */
export function hasEarlierCalls(summary, jobId) {
  return !!summary && (summary.jobs > 1 || (summary.latest && summary.latest.job_id !== jobId));
}

/** "Contacted · N" badge for a number's summary { calls, jobs, latest }. */
export function ContactedBadge({ summary }) {
  if (!summary?.latest) return null;
  const { latest, calls } = summary;
  const title = callSummaryLine(latest) + (latest.job_title ? ` · on ${latest.job_title}` : "");
  return (
    <span className={`pvm-hbadge ${callTone(latest.status, latest.outcome)}`} title={title}
      style={{ flex: "none", whiteSpace: "nowrap", cursor: "default" }} aria-label={`Contacted before: ${title}`}>
      Contacted{calls > 1 ? ` · ${calls}` : ""}
    </span>
  );
}

const tlStyles = `
.cht { margin-top: 6px; }
.cht-item { display: flex; gap: 9px; padding: 7px 0; }
.cht-item + .cht-item { border-top: 1px dashed #EEF2F7; }
.cht-main { flex: 1; min-width: 0; font-size: 12px; color: #334155; line-height: 1.5; }
.cht-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.cht-meta { color: #64748B; }
.cht-sub { color: #64748B; font-size: 11.5px; }
.cht-sub a { color: #2563EB; text-decoration: none; font-weight: 600; }
.cht-sub a:hover { text-decoration: underline; }
.cht-note { color: #475569; font-size: 11.5px; white-space: pre-wrap; word-break: break-word; }
.cht-empty { font-size: 12px; color: #94A3B8; padding: 4px 0; }
.cht .pvm-hdot { margin-top: 6px; }
`;

/** Timeline of every call to `phone` on any job, newest first. Loads on mount / when
 * `phone` changes (lazily — not part of any table load). Calls on `currentJobId` say
 * "on this job"; others link to their job. `showRemarks` adds each call's comments. */
export function ContactTimeline({ phone, currentJobId = null, showRemarks = false, emptyText = "No calls logged to this number yet." }) {
  const [state, setState] = useState({ loading: true, error: "", items: [] });
  useEffect(() => {
    if (!phone) { setState({ loading: false, error: "", items: [] }); return undefined; }
    let cancelled = false;
    setState((s) => ({ ...s, loading: true, error: "" }));
    getContactHistory(phone)
      .then((res) => { if (!cancelled) setState({ loading: false, error: "", items: res?.items || [] }); })
      .catch((e) => { if (!cancelled) setState({ loading: false, error: e?.message || "Could not load call history", items: [] }); });
    return () => { cancelled = true; };
  }, [phone]);

  if (state.loading) return <div className="cht-empty">Loading call history…</div>;
  if (state.error) return <div className="cht-empty">{state.error}</div>;
  return (
    <div className="cht">
      <PhoneVerifyStyles />
      <style>{tlStyles}</style>
      {!state.items.length && <div className="cht-empty">{emptyText}</div>}
      {state.items.map((c) => {
        const tone = callTone(c.status, c.outcome);
        const extra = [c.reason, revisitText(c.revisit)].filter(Boolean).join(" · ");
        const here = currentJobId && c.job_id === currentJobId;
        return (
          <div key={c.id} className="cht-item">
            <span className={`pvm-hdot pvm-tone-${tone}`} />
            <div className="cht-main">
              <div className="cht-row">
                <span className="cht-meta">{[shortDate(c.called_at), c.called_by?.name].filter(Boolean).join(" · ")}</span>
                <span className={`pvm-hbadge ${tone}`}>{callLabel(c.status, c.outcome)}</span>
              </div>
              {extra && <div className="cht-sub">{extra}</div>}
              <div className="cht-sub">
                {here ? "on this job" : c.job_id ? (
                  <>on <Link to={`/jobs/${encodeURIComponent(c.job_id)}`}>{c.job_title || "another job"}</Link>
                    {c.company ? ` · ${c.company}` : ""}</>
                ) : null}
              </div>
              {showRemarks && c.remarks && <div className="cht-note">{c.remarks}</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
