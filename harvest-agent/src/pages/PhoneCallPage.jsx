import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Briefcase, Paperclip, Pencil, RefreshCw } from "lucide-react";
import { getPhoneCallLog } from "../api";
import { C } from "../theme";
import PhoneVerifyModal, { outcomeDetailText } from "../components/PhoneVerify";
import { ContactTimeline } from "../components/ContactHistory";
import { linkProps } from "../lib/openLink";
import {
  AssigneePill, Chip, OutcomeCell, PHONE_SOURCES, ReminderCell, RepeatTag, StatusPill, fmtTime,
} from "./PhoneLogsPage";

/* One logged call on its own page (route /phone-logs/:id) — the page form of a Phone
 * logs row, opened in a new tab from the row's ↗ button (or a pasted link). It resolves
 * entirely from the URL via GET /phone-verifications/logs/{id}: admins can open any call,
 * a business lead only their own (the backend 403s the rest). Shows the call's details,
 * an Edit button (PhoneVerifyModal, same as the list) and the number's full call
 * history across jobs (ContactTimeline). */

const LABEL = { fontSize: 11, letterSpacing: ".12em", textTransform: "uppercase", color: "#94A3B8", fontWeight: 800 };
const K = { color: "#64748B", fontSize: 13 };
const V = { color: "#0F172A", fontWeight: 600, fontSize: 13.5, minWidth: 0, overflowWrap: "anywhere" };

/** One "label — value" line of the details panel. */
function Field({ label, children }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "130px minmax(0, 1fr)", gap: 10, padding: "9px 0", borderTop: "1px solid #EEF2F7" }}>
      <div style={K}>{label}</div>
      <div style={V}>{children}</div>
    </div>
  );
}

/** Route /phone-logs/:logId — one call's details, edit, and the number's history. */
export default function PhoneCallPage() {
  const { logId } = useParams();
  const navigate = useNavigate();
  const [call, setCall] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null); // { status, message }
  const [editing, setEditing] = useState(false);
  // Bumped after an edit so the history below re-fetches with the new outcome.
  const [historyKey, setHistoryKey] = useState(0);

  const load = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    getPhoneCallLog(logId)
      .then((res) => { if (!cancelled) { setCall(res); setError(null); } })
      .catch((err) => { if (!cancelled) setError({ status: err?.status, message: err?.message || "Couldn't load this call." }); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [logId]);
  useEffect(() => load(), [load]);

  // Back = history POP when opened in-app; on a fresh tab / direct link there's no
  // earlier entry → Phone logs.
  const canGoBack = (window.history.state?.idx ?? 0) > 0;
  const backButton = (
    <button className="ha-btn ha-btn-secondary" style={{ marginBottom: 14 }}
      onClick={() => (canGoBack ? navigate(-1) : navigate("/phone-logs"))}>
      <ArrowLeft size={15} /> {canGoBack ? "Back" : "Back to Phone logs"}
    </button>
  );

  if (!call) {
    return (
      <main className="ha-main" style={{ padding: "24px 24px 32px" }}>
        {backButton}
        <div className="ha-card" style={{ padding: "48px 24px", textAlign: "center", color: "#64748B", maxWidth: 560, margin: "40px auto" }}>
          {loading ? "Loading call…" : (
            <>
              <div style={{ marginBottom: 6, fontWeight: 700, color: "#334155" }}>
                {error?.status === 404 ? "This call log no longer exists."
                  : error?.status === 403 ? "You can't open this call log." : "This call couldn't be opened."}
              </div>
              <div style={{ marginBottom: 18 }}>
                {error?.status === 404 ? "It may have been cleared." : error?.status === 403 ? "Business leads can only open the calls they own." : error?.message}
              </div>
              <button className="ha-btn ha-btn-secondary" onClick={() => navigate("/phone-logs")}>Go to Phone logs</button>
            </>
          )}
        </div>
      </main>
    );
  }

  const jobHref = call.job_id ? `/jobs/${encodeURIComponent(call.job_id)}` : "";
  const source = PHONE_SOURCES[call.phone_source];
  const detail = outcomeDetailText(call.details);
  const caller = call.owner || call.called_by;
  const proof = Array.isArray(call.proof) ? call.proof : [];

  return (
    <main className="ha-main" style={{ padding: "24px 24px 32px" }}>
      {backButton}
      <div className="ha-bell-clear" style={{ paddingBottom: 18, borderBottom: "1px solid #E2E8F0" }}>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
          <div style={{ minWidth: 0 }}>
            <h1 style={{ fontSize: 24, fontWeight: 800, letterSpacing: "-.02em", margin: 0, lineHeight: 1.25 }}>
              Call to {call.contact_name || call.phone}
            </h1>
            <div style={{ display: "flex", alignItems: "center", gap: 9, flexWrap: "wrap", marginTop: 11 }}>
              <StatusPill status={call.status} />
              <span style={{ color: "#94A3B8", fontSize: 12.5 }}>{fmtTime(call.called_at)}</span>
              {caller?.name && <span style={{ color: "#94A3B8", fontSize: 12.5 }}>by {caller.name}</span>}
            </div>
          </div>
          <div style={{ display: "flex", gap: 9, flex: "none" }}>
            <button className="ha-btn ha-btn-secondary" onClick={load} disabled={loading} title="Refresh">
              <RefreshCw size={15} className={loading ? "ha-spin" : ""} /> Refresh
            </button>
            {jobHref && (
              <a className="ha-btn ha-btn-secondary" style={{ textDecoration: "none" }}
                title="Open this job (Ctrl/middle-click for a new tab)"
                {...linkProps(jobHref, () => navigate(jobHref, { state: { from: `/phone-logs/${call.id}` } }))}>
                <Briefcase size={15} /> Open job
              </a>
            )}
            {call.can_edit !== false && (
              <button className="ha-btn" onClick={() => setEditing(true)}
                style={{ background: C.primary, color: "#fff", border: `1px solid ${C.primary}` }}>
                <Pencil size={15} /> Edit call log
              </button>
            )}
          </div>
        </div>
      </div>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 26, marginTop: 22, alignItems: "flex-start" }}>
        {/* Call details */}
        <div style={{ flex: "1 1 420px", minWidth: 0 }}>
          <div style={{ ...LABEL, marginBottom: 12 }}>Call details</div>
          <div className="ha-card" style={{ padding: "6px 18px 8px" }}>
            <Field label="Job">
              {call.job_title || "—"}
              {call.company && <div style={{ fontWeight: 400, color: C.textSoft, fontSize: 12.5 }}>{call.company}</div>}
            </Field>
            <Field label="Phone">
              <span style={{ fontVariantNumeric: "tabular-nums" }}>{call.phone || "—"}</span>
              {source && (
                <span style={{ marginLeft: 8, padding: "2px 8px", borderRadius: 6, fontSize: 11.5, fontWeight: 600, color: source.color, background: source.bg }}>
                  {source.label}
                </span>
              )}
              <RepeatTag repeat={call.repeat} />
            </Field>
            <Field label="POC">{call.contact_name || "—"}</Field>
            <Field label="Call outcome">
              <OutcomeCell status={call.status} outcome={call.outcome} details={null} />
              {detail && <div style={{ marginTop: 6, fontWeight: 400, color: C.textSoft, whiteSpace: "pre-wrap" }}>{detail}</div>}
            </Field>
            <Field label="Comments">
              {call.remarks
                ? <span style={{ fontWeight: 400, whiteSpace: "pre-wrap" }}>{call.remarks}</span>
                : <span style={{ color: "#94A3B8", fontWeight: 400 }}>—</span>}
            </Field>
            <Field label="Reminder"><ReminderCell reminder={call.reminder} /></Field>
            <Field label="Called by">
              {call.called_by?.name || "—"}
              {call.called_by?.email && <div style={{ fontWeight: 400, color: C.textSoft, fontSize: 12.5 }}>{call.called_by.email}</div>}
            </Field>
            <Field label="Assigned to"><AssigneePill user={call.assigned_to} /></Field>
            <Field label="Called at">{fmtTime(call.called_at)}</Field>
            {proof.length > 0 && (
              <Field label="Proof">
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                  {proof.map((f, i) => (f?.src ? (
                    <a key={i} href={f.src} target="_blank" rel="noopener" title={f.name || "Proof"}>
                      <img src={f.src} alt={f.name || "Proof"} style={{ width: 72, height: 72, objectFit: "cover", borderRadius: 8, border: "1px solid #E2E8F0" }} />
                    </a>
                  ) : (
                    <Chip key={i} tone="muted"><Paperclip size={11} style={{ verticalAlign: "-1px" }} /> {f?.name || "Attachment"}</Chip>
                  )))}
                </div>
              </Field>
            )}
          </div>
        </div>

        {/* The number's history across jobs */}
        <div style={{ flex: "1 1 360px", minWidth: 0 }}>
          <div style={{ ...LABEL, marginBottom: 12 }}>Call history for this number</div>
          <div className="ha-card" style={{ padding: "12px 16px" }}>
            <ContactTimeline key={historyKey} phone={call.phone} currentJobId={call.job_id} showRemarks />
          </div>
        </div>
      </div>

      {editing && createPortal(
        <PhoneVerifyModal
          key={call.id}
          editCall={call}
          jobId={call.job_id}
          phone={call.phone}
          pocName={call.contact_name}
          company={call.company}
          onSave={(updated) => {
            if (updated && updated.id != null) setCall((prev) => ({ ...prev, ...updated }));
            setHistoryKey((k) => k + 1);
          }}
          onClose={() => setEditing(false)}
        />,
        document.body,
      )}
    </main>
  );
}
