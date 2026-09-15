import React, { useCallback, useEffect, useState } from "react";
import { useParams, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, Copy, Check, CornerUpRight } from "lucide-react";
import { getOutreachHistory } from "./api";
import OutreachThread from "./components/OutreachThread";
import EmailComposeModal from "./components/EmailComposeModal";
import {
  fmtAbs, fmtTime, engagement, deliveryTimeline, EVENT_COLOR, StatusBadge, ToneChip, TimelineStep,
} from "./components/outreachUi";

/* Standalone view of one sent outreach + its full thread (route /mail/:id) — the
 * page form of what used to be the Mail logs row popup. Reached from a row click
 * (carrying the row + the list's URL as navigation state) and refresh-safe: on a
 * direct load/refresh it re-fetches the thread from the ?job=/?recruiter= query.
 * Renders inside the shared sidebar layout (ha-root → Sidebar → this <main>). */

export default function OutreachThreadPage() {
  const { id } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const stateItem = location.state?.item || null;
  const backTo = location.state?.from || "/outreach";

  // The specific outreach row this page is about (header + delivery + message).
  const [detail, setDetail] = useState(stateItem);
  const [thread, setThread] = useState(stateItem ? [] : []);
  const [threadLoading, setThreadLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [copied, setCopied] = useState(false);

  // Follow-up composer context ({ job, parentOutreachId }) — opens EmailComposeModal.
  const [composeFor, setComposeFor] = useState(null);

  // Fetch the whole thread for this contact. Prefer identifiers from the passed
  // row; on a refresh/direct link fall back to the ?job=/?recruiter= query so the
  // page still resolves without any in-memory navigation state.
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
      // Anchor the header/delivery/message to the row named in the URL; fall back
      // to the passed row, then the first thread message.
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

  const goBack = () => navigate(backTo);

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

  const BackButton = (
    <button
      className="ha-btn ha-btn-secondary"
      onClick={goBack}
      style={{ padding: "8px 14px" }}
    >
      <ArrowLeft size={15} /> Back to Mail logs
    </button>
  );

  // No row could be resolved (direct link with no state and no job/recruiter).
  if (!detail && !threadLoading && notFound) {
    return (
      <main className="ha-main">
        <div style={{ marginBottom: 18 }}>{BackButton}</div>
        <div className="ha-card" style={{ padding: "48px 24px", textAlign: "center", color: "#64748B" }}>
          <div style={{ marginBottom: 6, fontWeight: 600, color: "#334155" }}>This thread isn’t available here.</div>
          <div>Open it from Mail logs to read the message and follow up.</div>
        </div>
      </main>
    );
  }

  return (
    <main className="ha-main">
      <div style={{ marginBottom: 18 }}>{BackButton}</div>

      <div className="ha-card" style={{ overflow: "hidden", maxWidth: 720 }}>
        {/* Header — subject + engagement/tone + timestamp */}
        <div style={{ padding: "18px 22px", borderBottom: "1px solid #E2E8F0" }}>
          <div style={{ fontSize: 18, fontWeight: 800, letterSpacing: "-.01em", textWrap: "balance" }}>
            {detail?.subject || (detail?.channel === "linkedin" ? "LinkedIn message" : "Outreach")}
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 10, flexWrap: "wrap" }}>
            {detail && <StatusBadge label={engagement(detail).label} />}
            {detail && <ToneChip it={detail} />}
            {detail && <span style={{ color: "#94A3B8", fontSize: 12 }}>{fmtAbs(detail.created_at)}</span>}
          </div>
        </div>

        <div style={{ padding: "16px 22px" }}>
          {/* Delivery event trail — every status this mail passed through. */}
          <div style={{ fontSize: 11, letterSpacing: ".12em", textTransform: "uppercase", color: "#94A3B8", fontWeight: 700, marginBottom: 10 }}>Delivery</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 16px", marginBottom: 18, fontSize: 12 }}>
            {detail && deliveryTimeline(detail).map((e) => (
              <TimelineStep key={e.label} on label={e.label} time={fmtTime(e.at)} color={EVENT_COLOR[e.label] || "#94A3B8"} />
            ))}
            {(!detail || deliveryTimeline(detail).length <= 1) && (
              <span style={{ color: "#CBD5E1" }}>Awaiting delivery events…</span>
            )}
          </div>

          <div style={{ fontSize: 11, letterSpacing: ".12em", textTransform: "uppercase", color: "#94A3B8", fontWeight: 700, marginBottom: 12 }}>Message</div>
          <div style={{ display: "grid", gridTemplateColumns: "72px 1fr", gap: "6px 14px", fontSize: 13.5, marginBottom: 4 }}>
            {detail?.from_email && (<><div style={{ color: "#64748B" }}>From</div><div style={{ color: "#0F172A", fontWeight: 600 }}>{detail.from_email}</div></>)}
            <div style={{ color: "#64748B" }}>To</div>
            <div style={{ color: "#0F172A", fontWeight: 600 }}>
              {detail?.contact_name ? `${detail.contact_name} · ` : ""}<span style={{ color: "#64748B", fontWeight: 500 }}>{detail?.to_email || (detail?.channel === "linkedin" ? "(LinkedIn)" : "—")}</span>
            </div>
            {detail?.company && (<><div style={{ color: "#64748B" }}>Company</div><div style={{ color: "#0F172A", fontWeight: 600 }}>{detail.company}</div></>)}
          </div>

          <div style={{ marginTop: 16 }}>
            <div style={{ fontSize: 11, letterSpacing: ".12em", textTransform: "uppercase", color: "#94A3B8", fontWeight: 700, marginBottom: 10 }}>Thread</div>
            <OutreachThread messages={thread} loading={threadLoading} emptyText="No sent messages found for this contact." collapsible />
          </div>
        </div>

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, padding: "14px 22px", borderTop: "1px solid #E2E8F0", background: "#F8FAFC" }}>
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

      {/* Follow-up composer (opens over the page). */}
      {composeFor && (
        <EmailComposeModal
          job={composeFor.job}
          followup
          parentOutreachId={composeFor.parentOutreachId}
          onClose={() => setComposeFor(null)}
          onSent={() => { setComposeFor(null); loadThread(); }}
        />
      )}
    </main>
  );
}
