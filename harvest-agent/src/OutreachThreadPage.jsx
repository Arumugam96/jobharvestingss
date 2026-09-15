import React, { useCallback, useEffect, useState } from "react";
import { useParams, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Copy, Check, CornerUpRight } from "lucide-react";
import { getOutreachHistory } from "./api";
import OutreachThread from "./components/OutreachThread";
import EmailComposeModal from "./components/EmailComposeModal";
import {
  fmtAbs, engagement, deliveryTimeline, EVENT_COLOR, StatusBadge, ToneChip, CLIENT_LABEL,
} from "./components/outreachUi";

/* Standalone, full-page view of one sent outreach + its full thread (route
 * /mail/:id) — the page form of what used to be the Mail logs row popup. Opened in
 * a new tab from a row click, so it resolves entirely from the URL (?job=/?recruiter=)
 * rather than navigation state. Renders inside the shared sidebar layout
 * (ha-root → Sidebar → this <main>): subject as the page title, actions top-right,
 * the conversation as the main column, and delivery + details in a right panel. */

const LABEL = { fontSize: 11, letterSpacing: ".12em", textTransform: "uppercase", color: "#94A3B8", fontWeight: 800 };
const K = { color: "#64748B" };
const V = { color: "#0F172A", fontWeight: 600, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" };

export default function OutreachThreadPage() {
  const { id } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const stateItem = location.state?.item || null;

  // The specific outreach row this page is about (header + delivery + details).
  const [detail, setDetail] = useState(stateItem);
  const [thread, setThread] = useState([]);
  const [threadLoading, setThreadLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [copied, setCopied] = useState(false);

  // Follow-up composer context ({ job, parentOutreachId }) — opens EmailComposeModal.
  const [composeFor, setComposeFor] = useState(null);

  // Fetch the whole thread for this contact. Prefer identifiers from the passed
  // row; on a fresh tab / direct link fall back to the ?job=/?recruiter= query so
  // the page resolves without any in-memory navigation state.
  const loadThread = useCallback(async () => {
    const jobId = stateItem?.job_id || searchParams.get("job") || null;
    const recruiterId = stateItem?.recruiter_id || searchParams.get("recruiter") || null;
    setThreadLoading(true);
    setNotFound(false);
    try {
      const params = jobId ? { job_id: jobId } : recruiterId ? { recruiter_id: recruiterId } : null;
      const res = params ? await getOutreachHistory(params) : { items: stateItem ? [stateItem] : [] };
      const items = res.items && res.items.length ? res.items : stateItem ? [stateItem] : [];
      setThread(items);
      const anchor = items.find((m) => String(m.id) === String(id)) || stateItem || items[0] || null;
      setDetail(anchor);
      if (!anchor) setNotFound(true);
    } catch {
      if (stateItem) {
        setThread([stateItem]);
        setDetail(stateItem);
      } else {
        setThread([]);
        setDetail(null);
        setNotFound(true);
      }
    } finally {
      setThreadLoading(false);
    }
  }, [stateItem, searchParams, id]);

  useEffect(() => { loadThread(); }, [loadThread]);

  const copyMessage = () => {
    if (!detail) return;
    navigator.clipboard?.writeText(`${detail.subject || ""}\n\n${detail.body || ""}`);
    setCopied(true);
    setTimeout(() => setCopied(false), 1600);
  };

  const canFollowUp = detail && detail.channel === "email" && detail.job_id;
  const startFollowUp = () => {
    if (!canFollowUp) return;
    setComposeFor({
      job: { id: detail.job_id, email: detail.to_email, company: detail.company },
      parentOutreachId: detail.id,
    });
  };

  // Nothing resolved (direct link with no state and no job/recruiter to fetch by).
  if (!detail && !threadLoading && notFound) {
    return (
      <main className="ha-main">
        <div className="ha-card" style={{ padding: "48px 24px", textAlign: "center", color: "#64748B", maxWidth: 560, margin: "40px auto" }}>
          <div style={{ marginBottom: 6, fontWeight: 700, color: "#334155" }}>This thread isn’t available here.</div>
          <div style={{ marginBottom: 18 }}>Open it from Mail logs to read the message and follow up.</div>
          <button className="ha-btn ha-btn-secondary" onClick={() => navigate("/outreach")}>Go to Mail logs</button>
        </div>
      </main>
    );
  }

  const timeline = detail ? deliveryTimeline(detail) : [];
  const clientLabel = (detail && CLIENT_LABEL[detail.client_type]) || "";

  return (
    <main className="ha-main">
      {/* Page header — subject as title, engagement/tone, actions top-right */}
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 20, flexWrap: "wrap", paddingBottom: 18, borderBottom: "1px solid #E2E8F0" }}>
        <div style={{ minWidth: 0 }}>
          <h1 style={{ fontSize: 24, fontWeight: 800, letterSpacing: "-.02em", margin: 0, textWrap: "balance", maxWidth: "34ch" }}>
            {detail?.subject || (detail?.channel === "linkedin" ? "LinkedIn message" : "Outreach")}
          </h1>
          <div style={{ display: "flex", alignItems: "center", gap: 9, flexWrap: "wrap", marginTop: 11 }}>
            {detail && <StatusBadge label={engagement(detail).label} />}
            {detail && <ToneChip it={detail} />}
            {detail && <span style={{ color: "#94A3B8", fontSize: 12.5 }}>{fmtAbs(detail.created_at)}</span>}
          </div>
        </div>
        <div style={{ display: "flex", gap: 9, flex: "none" }}>
          <button className="ha-btn ha-btn-secondary" onClick={copyMessage} disabled={!detail}>
            {copied ? <Check size={15} /> : <Copy size={15} />} {copied ? "Copied" : "Copy message"}
          </button>
          {canFollowUp && (
            <button className="ha-btn" onClick={startFollowUp} style={{ background: "#2563EB", color: "#fff", border: "1px solid #2563EB" }}>
              <CornerUpRight size={15} /> Follow up
            </button>
          )}
        </div>
      </div>

      {/* Body — conversation (main) + delivery/details (right panel) */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 26, marginTop: 22, alignItems: "flex-start" }}>
        <div style={{ flex: "1 1 420px", minWidth: 0 }}>
          <div style={{ ...LABEL, marginBottom: 12 }}>Thread</div>
          <OutreachThread messages={thread} loading={threadLoading} emptyText="No sent messages found for this contact." collapsible />
        </div>

        <div style={{ flex: "0 1 300px", minWidth: 260, display: "flex", flexDirection: "column", gap: 18 }}>
          <div className="ha-card" style={{ padding: "16px 16px" }}>
            <div style={{ ...LABEL, marginBottom: 14 }}>Delivery</div>
            {timeline.map((e, i) => (
              <div key={e.label} style={{ display: "flex", gap: 11, position: "relative", paddingBottom: i === timeline.length - 1 ? 0 : 16 }}>
                {i !== timeline.length - 1 && <span style={{ position: "absolute", left: 6, top: 16, bottom: 0, width: 2, background: "#E2E8F0" }} />}
                <span style={{ width: 14, height: 14, borderRadius: "50%", flex: "none", marginTop: 1, border: "3px solid #fff", background: EVENT_COLOR[e.label] || "#94A3B8" }} />
                <div>
                  <div style={{ fontSize: 13, fontWeight: 600, color: "#334155" }}>{e.label}</div>
                  <div style={{ fontSize: 12, color: "#64748B", fontVariantNumeric: "tabular-nums", marginTop: 1 }}>{fmtAbs(e.at)}</div>
                </div>
              </div>
            ))}
            {timeline.length <= 1 && (
              <div style={{ color: "#94A3B8", fontSize: 12.5, marginTop: 4 }}>Awaiting delivery events…</div>
            )}
          </div>

          <div className="ha-card" style={{ padding: "16px 16px" }}>
            <div style={{ ...LABEL, marginBottom: 14 }}>Details</div>
            <div style={{ display: "grid", gridTemplateColumns: "68px 1fr", gap: "8px 12px", fontSize: 13 }}>
              {detail?.from_email && (<><span style={K}>From</span><span style={V} title={detail.from_email}>{detail.from_email}</span></>)}
              <span style={K}>To</span><span style={V}>{detail?.contact_name || detail?.to_email || "—"}</span>
              {detail?.to_email && (<><span style={K}>Email</span><span style={V} title={detail.to_email}>{detail.to_email}</span></>)}
              {detail?.company && (<><span style={K}>Company</span><span style={V} title={detail.company}>{detail.company}</span></>)}
              {clientLabel && (<><span style={K}>Client</span><span style={V}>{clientLabel}</span></>)}
            </div>
          </div>
        </div>
      </div>

      {/* Follow-up composer (opens over the page). */}
      {composeFor && (
        <EmailComposeModal
          job={composeFor.job}
          followup
          autoDraft
          parentOutreachId={composeFor.parentOutreachId}
          onClose={() => setComposeFor(null)}
          onSent={() => { setComposeFor(null); loadThread(); }}
        />
      )}
    </main>
  );
}
