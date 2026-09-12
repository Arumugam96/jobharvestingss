import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { X, RefreshCw, Copy, Check, Send, Sparkles, Link, Loader2, AlertCircle, CornerUpRight } from "lucide-react";
import { generateOutreachEmail, generateFollowupEmail, sendOutreachEmail, getOutreachHistory, checkSuppressed, ApiError } from "../api";
import OutreachBodyField from "./OutreachBodyField";
import OutreachThread from "./OutreachThread";

/* Recruiter outreach email composer. Opens from the Email icon on a Harvested
 * Jobs row (only when that row has a recruiter email). Generates a tone- and
 * audience-aware draft via the LLM (POST /outreach/generate-email), lets the
 * user edit From/To/Subject/Body, Regenerate, and Send (POST /outreach/send-email);
 * a link to the SightSpectrum corporate deck is included in the body when the
 * backend has OUTREACH_DECK_URL configured. Styling mirrors the app's
 * self-contained-overlay pattern (LiveBrowserView.jsx) with an `ecm-` prefix. */

const styles = `
.ecm-overlay { position: fixed; inset: 0; background: rgba(15,23,42,.45); display: flex; align-items: center; justify-content: center; padding: 24px; z-index: 1000; }
.ecm-card { width: 100%; max-width: 680px; max-height: calc(100vh - 48px); background: #FFFFFF; border-radius: 12px; box-shadow: 0 24px 48px rgba(15,23,42,.18); display: flex; flex-direction: column; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1E293B; }
.ecm-head { display: flex; align-items: center; justify-content: space-between; padding: 16px 22px; border-bottom: 1px solid #E2E8F0; }
.ecm-title { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; display: flex; align-items: center; gap: 10px; }
.ecm-badge { font-size: 11px; font-weight: 600; border-radius: 999px; padding: 2px 9px; }
.ecm-badge-active { background: #ECFDF5; color: #047857; }
.ecm-badge-new { background: #EFF6FF; color: #1E40AF; }
.ecm-badge-unknown { background: #F1F5F9; color: #475569; }
.ecm-close { width: 30px; height: 30px; border: none; background: transparent; border-radius: 6px; color: #64748B; display: flex; align-items: center; justify-content: center; cursor: pointer; }
.ecm-close:hover { background: #F1F5F9; color: #1E293B; }
.ecm-body { padding: 18px 22px; overflow-y: auto; flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; gap: 14px; }
.ecm-field { display: flex; flex-direction: column; gap: 6px; }
.ecm-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: #94A3B8; display: flex; align-items: center; gap: 8px; }
.ecm-input { width: 100%; border: 1px solid #E2E8F0; border-radius: 8px; padding: 9px 11px; font-size: 14px; color: #1E293B; font-family: inherit; background: #FFFFFF; box-sizing: border-box; }
.ecm-input:focus { outline: none; border-color: #2563EB; box-shadow: 0 0 0 3px rgba(37,99,235,.12); }
.ecm-textarea { width: 100%; min-height: 240px; border: 1px solid #E2E8F0; border-radius: 8px; padding: 12px; font-size: 14px; line-height: 1.6; color: #1E293B; font-family: inherit; resize: vertical; box-sizing: border-box; }
.ecm-textarea:focus { outline: none; border-color: #2563EB; box-shadow: 0 0 0 3px rgba(37,99,235,.12); }
.ecm-aitag { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 600; color: #B45309; background: #FEF3C7; border-radius: 999px; padding: 2px 8px; text-transform: none; letter-spacing: 0; }
.ecm-tones { display: flex; gap: 6px; flex-wrap: wrap; }
.ecm-tone { border: 1px solid #E2E8F0; background: #FFFFFF; color: #64748B; font-size: 12px; font-weight: 500; padding: 5px 12px; border-radius: 999px; cursor: pointer; font-family: inherit; }
.ecm-tone:hover:not(:disabled) { border-color: #CBD5E1; color: #1E293B; }
.ecm-tone.is-active { background: #EFF6FF; border-color: #2563EB; color: #1E40AF; }
.ecm-tone:disabled { opacity: .6; cursor: default; }
.ecm-select { cursor: pointer; appearance: none; -webkit-appearance: none; background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%2364748B' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><polyline points='6 9 12 15 18 9'/></svg>"); background-repeat: no-repeat; background-position: right 10px center; padding-right: 34px; }
.ecm-attach { display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px; color: #475569; background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; padding: 8px 11px; }
.ecm-note { display: flex; align-items: center; gap: 7px; font-size: 12.5px; border-radius: 8px; padding: 8px 11px; }
.ecm-note-warn { background: #FFFBEB; border: 1px solid #FDE68A; color: #92400E; }
.ecm-note-err { background: #FEF2F2; border: 1px solid #FCA5A5; color: #B91C1C; }
.ecm-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 13px 22px; border-top: 1px solid #E2E8F0; background: #F8FAFC; }
.ecm-foot-left, .ecm-foot-right { display: flex; align-items: center; gap: 8px; }
.ecm-btn { display: inline-flex; align-items: center; gap: 6px; border-radius: 8px; font-size: 13px; font-weight: 600; padding: 9px 14px; cursor: pointer; font-family: inherit; border: 1px solid transparent; transition: background .12s, border-color .12s; }
.ecm-btn:disabled { opacity: .5; cursor: not-allowed; }
.ecm-btn-ghost { background: transparent; border-color: #E2E8F0; color: #475569; }
.ecm-btn-ghost:hover:not(:disabled) { background: #FFFFFF; border-color: #CBD5E1; color: #1E293B; }
.ecm-btn-primary { background: #2563EB; color: #FFFFFF; }
.ecm-btn-primary:hover:not(:disabled) { background: #1E40AF; }
.ecm-btn-plain { background: transparent; color: #64748B; border-color: transparent; }
.ecm-btn-plain:hover:not(:disabled) { color: #1E293B; }
.ecm-spin { animation: ecm-rotate .8s linear infinite; }
@keyframes ecm-rotate { to { transform: rotate(360deg); } }
@media (max-width: 640px) { .ecm-overlay { padding: 0; align-items: flex-end; } .ecm-card { max-width: 100%; max-height: 100vh; height: 100%; border-radius: 0; } .ecm-foot { flex-direction: column-reverse; align-items: stretch; } .ecm-foot-left, .ecm-foot-right { justify-content: space-between; } }
`;

const TONES = ["Formal", "Friendly", "Direct"];

const CLIENT_LABELS = {
  active: { label: "Active client", cls: "ecm-badge-active" },
  new: { label: "New client", cls: "ecm-badge-new" },
  unknown: { label: "Unknown company", cls: "ecm-badge-unknown" },
};

export default function EmailComposeModal({
  job = {},
  followup = false,
  parentOutreachId = null,
  onClose = () => {},
  onSent = () => {},
}) {
  const [tone, setTone] = useState("Formal");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [fromEmail, setFromEmail] = useState("");
  const [toEmail, setToEmail] = useState(job.email || "");
  const [clientType, setClientType] = useState("");
  const [fallbackUsed, setFallbackUsed] = useState(false);
  const [deckUrl, setDeckUrl] = useState("");
  // The prior outreach this follow-up threads onto (may be resolved server-side
  // when the caller didn't pass an explicit parent).
  const [resolvedParentId, setResolvedParentId] = useState(parentOutreachId);
  // Set when an initial send is blocked as a duplicate — holds the existing send
  // so the user can confirm a re-send.
  const [duplicate, setDuplicate] = useState(null);
  // The posting's title + URL, so the body Preview can render the same bold blue
  // new-tab job-title link the delivered email gets.
  const [jobTitle, setJobTitle] = useState("");
  const [jobUrl, setJobUrl] = useState("");

  const [generating, setGenerating] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);
  const [copied, setCopied] = useState(false);
  // True when the current recipient has unsubscribed — blocks Generate/Send. The
  // send route enforces it regardless; this is the up-front UX.
  const [suppressed, setSuppressed] = useState(false);
  const metaLoaded = useRef(false);

  // Follow-up mode shows the previously sent email(s) first and only drafts a new
  // follow-up once the user clicks "Generate follow-up" (draftStarted). Initial
  // mode drafts immediately, so draftStarted starts true there.
  const [history, setHistory] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(followup);
  const [draftStarted, setDraftStarted] = useState(!followup);

  // Check the recipient against the do-not-contact list (debounced on the To field).
  useEffect(() => {
    const email = (toEmail || "").trim();
    if (!email) { setSuppressed(false); return undefined; }
    let cancelled = false;
    const t = setTimeout(async () => {
      try {
        const res = await checkSuppressed(email);
        if (!cancelled) setSuppressed(!!res.suppressed);
      } catch { /* best-effort; the send route still enforces it */ }
    }, 300);
    return () => { cancelled = true; clearTimeout(t); };
  }, [toEmail]);

  // Candidate recipient addresses for this job, labeled by origin to match the
  // table's Job/Recruiter split (emailScraped = the address on the job post,
  // emailRecruiter = the recruiter's own). Deduped by address; when more than
  // one exists the user gets quick-pick chips under the To field. Also reads the
  // detail-view's nested posterContact shape, and the merged `email` fallback
  // (JSON-read rows carry only that).
  const toOptions = useMemo(() => {
    const pc = job.posterContact || {};
    const raw = [
      { label: "Job", email: job.emailScraped || pc.emailScraped || "" },
      { label: "Recruiter", email: job.emailRecruiter || pc.emailRecruiter || "" },
    ];
    const seen = new Set();
    const opts = [];
    for (const o of raw) {
      const email = (o.email || "").trim();
      if (!email || seen.has(email.toLowerCase())) continue;
      seen.add(email.toLowerCase());
      opts.push({ label: o.label, email });
    }
    if (opts.length === 0 && (job.email || "").trim()) {
      opts.push({ label: "Contact", email: job.email.trim() });
    }
    return opts;
  }, [job]);

  const generate = useCallback(async (nextTone, regenerate) => {
    setGenerating(true);
    setError("");
    try {
      // Follow-up mode drafts from the prior outreach (server supplies the
      // context) so it reads as a second-touch nudge, not a fresh introduction.
      const res = followup
        ? await generateFollowupEmail({ job_id: job.id, parent_outreach_id: parentOutreachId, mode: nextTone, regenerate })
        : await generateOutreachEmail({ job_id: job.id, mode: nextTone, regenerate });
      setSubject(res.subject || "");
      setBody(res.body || "");
      setClientType(res.client_type || "");
      setFallbackUsed(!!res.fallback_used);
      setDeckUrl(res.deck_url || "");
      setJobTitle(res.job_title || "");
      setJobUrl(res.job_url || "");
      if (followup && res.parent_outreach_id) setResolvedParentId(res.parent_outreach_id);
      // Seed the editable From/To only on the first successful draft, so a user's
      // manual edits to those fields survive a tone change / regenerate.
      if (!metaLoaded.current) {
        if (res.from_email) setFromEmail(res.from_email);
        if (res.to_email) setToEmail(res.to_email);
        metaLoaded.current = true;
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not generate the email draft.");
    } finally {
      setGenerating(false);
    }
  }, [job.id, followup, parentOutreachId]);

  // On open: initial mode drafts immediately; follow-up mode instead loads the
  // already-sent email thread and waits for the user to ask for a new draft.
  useEffect(() => {
    if (!followup) { generate("Formal", false); return undefined; }
    let cancelled = false;
    setHistoryLoading(true);
    (async () => {
      try {
        const res = await getOutreachHistory({ job_id: job.id });
        if (cancelled) return;
        const emails = (res.items || [])
          .filter((m) => m.channel === "email" && m.status === "sent")
          .sort((a, b) => new Date(a.created_at) - new Date(b.created_at));
        setHistory(emails);
      } catch {
        // non-fatal — the user can still generate a follow-up
      } finally {
        if (!cancelled) setHistoryLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [followup, job.id, generate]);

  // Kick off the follow-up draft on demand, then reveal the editable compose UI.
  const startFollowup = () => {
    setDraftStarted(true);
    generate("Formal", false);
  };

  // Escape closes the modal.
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const pickTone = (t) => {
    if (t === tone || generating) return;
    setTone(t);
    generate(t, false);
  };

  const copyAll = () => {
    navigator.clipboard?.writeText(`${subject}\n\n${body}`);
    setCopied(true);
    setTimeout(() => setCopied(false), 1600);
  };

  const handleSend = async (force = false) => {
    setSending(true);
    setError("");
    setDuplicate(null);
    try {
      const res = await sendOutreachEmail({
        job_id: job.id,
        to_email: toEmail,
        from_email: fromEmail,
        subject,
        body,
        tone,
        client_type: clientType,
        fallback_used: fallbackUsed,
        parent_outreach_id: followup ? (resolvedParentId || parentOutreachId) : null,
        force,
      });
      if (res.status === "sent") {
        setSent(true);
        onSent();                 // let the caller refresh the DB-backed icon state
        setTimeout(onClose, 900);
      } else if (res.status === "duplicate") {
        // An initial email was already sent to this recruiter/job — ask to confirm.
        setDuplicate(res.existing || {});
      } else if (res.status === "suppressed") {
        setSuppressed(true);
        setError(res.error || "This recipient has unsubscribed and cannot be emailed.");
      } else {
        setError(res.error || "The email could not be sent.");
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The email could not be sent.");
    } finally {
      setSending(false);
    }
  };

  const badge = CLIENT_LABELS[clientType];
  const canSend = !generating && !sending && !sent && !suppressed && toEmail.trim() && subject.trim() && body.trim();

  return (
    <div className="ecm-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <style>{styles}</style>
      <div className="ecm-card" role="dialog" aria-modal="true" aria-label="Compose outreach email">
        <div className="ecm-head">
          <span className="ecm-title">
            {followup ? "Compose follow-up" : "Compose email"}
            {followup && (
              <span className="ecm-badge" style={{ background: "#FFF7EC", color: "#92580B", display: "inline-flex", alignItems: "center", gap: 4 }}>
                <CornerUpRight size={11} /> Follow-up
              </span>
            )}
            {badge && <span className={`ecm-badge ${badge.cls}`}>{badge.label}</span>}
          </span>
          <button className="ecm-close" onClick={onClose} aria-label="Close"><X size={17} /></button>
        </div>

        <div className="ecm-body">
          {followup && (
            <div className="ecm-field">
              <span className="ecm-label">Previously sent</span>
              {/* Collapsible cards — click one to read it (opens from above); they
                  all collapse once the user starts a draft (draftStarted). */}
              <OutreachThread messages={history} loading={historyLoading}
                emptyText="No previously sent email found for this job."
                collapsible forceCollapsed={draftStarted} />
            </div>
          )}
          {suppressed && (
            <div className="ecm-note ecm-note-err">
              <AlertCircle size={14} /> This contact has unsubscribed — sending is disabled.
            </div>
          )}
          {/* The compose editor. In initial mode it's shown immediately; in follow-up
              mode it stays collapsed until "Generate follow-up" flips draftStarted,
              then reveals from below. */}
          <div
            style={{
              display: "flex", flexDirection: "column", gap: 14,
              // flexShrink:0 keeps the wrapper at its full content height so the
              // scrollable .ecm-body overflows (and shows its scrollbar) instead
              // of this overflow:hidden wrapper silently clipping the bottom of
              // the form on short viewports.
              flexShrink: 0,
              maxHeight: (!followup || draftStarted) ? 2000 : 0,
              opacity: (!followup || draftStarted) ? 1 : 0,
              overflow: "hidden",
              transition: "max-height .4s ease, opacity .3s ease .05s",
            }}
          >
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            <div className="ecm-field" style={{ flex: 1, minWidth: 220 }}>
              <span className="ecm-label">From</span>
              <input className="ecm-input" value={fromEmail} onChange={(e) => setFromEmail(e.target.value)} placeholder="you@sightspectrum.com" />
            </div>
            <div className="ecm-field" style={{ flex: 1, minWidth: 220 }}>
              <span className="ecm-label">
                To
                {toOptions.length > 1 && <span style={{ color: "#94A3B8", fontWeight: 500, textTransform: "none", letterSpacing: 0 }}>· pick a recipient</span>}
              </span>
              {toOptions.length > 1 ? (
                <select className="ecm-input ecm-select" value={toEmail} onChange={(e) => setToEmail(e.target.value)} aria-label="Recipient email">
                  {/* Keep any seeded/edited address that isn't one of the labeled options selectable. */}
                  {toEmail.trim() && !toOptions.some((o) => o.email.toLowerCase() === toEmail.trim().toLowerCase()) && (
                    <option value={toEmail}>{toEmail}</option>
                  )}
                  {toOptions.map((o) => (
                    <option key={o.email} value={o.email}>{o.label} — {o.email}</option>
                  ))}
                </select>
              ) : (
                <input className="ecm-input" value={toEmail} onChange={(e) => setToEmail(e.target.value)} placeholder="recruiter@company.com" />
              )}
            </div>
          </div>

          <div className="ecm-field">
            <span className="ecm-label">Tone</span>
            <div className="ecm-tones">
              {TONES.map((t) => (
                <button key={t} className={`ecm-tone${t === tone ? " is-active" : ""}`} onClick={() => pickTone(t)} disabled={generating}>
                  {t}
                </button>
              ))}
            </div>
          </div>

          <div className="ecm-field">
            <span className="ecm-label">Subject</span>
            <input className="ecm-input" value={subject} onChange={(e) => setSubject(e.target.value)}
              disabled={generating} placeholder={generating ? "Generating…" : ""} />
          </div>

          <div className="ecm-field">
            <span className="ecm-label">
              Message
              <span className="ecm-aitag"><Sparkles size={11} /> AI draft</span>
            </span>
            <OutreachBodyField value={body} onChange={setBody} disabled={generating}
              placeholder={generating ? "Generating draft…" : ""} textareaClassName="ecm-textarea"
              jobTitle={jobTitle} jobUrl={jobUrl} />
          </div>

          {deckUrl && (
            <div className="ecm-attach"><Link size={14} /> Company overview link <span style={{ color: "#94A3B8" }}>· included in the message</span></div>
          )}

          {fallbackUsed && !error && (
            <div className="ecm-note ecm-note-warn"><AlertCircle size={14} /> AI generation was unavailable — a standard template was used. Please review before sending.</div>
          )}
          {duplicate && (
            <div className="ecm-note ecm-note-warn" style={{ alignItems: "center" }}>
              <AlertCircle size={14} />
              <span>
                An initial email was already sent to this recruiter
                {duplicate.created_at ? ` on ${new Date(duplicate.created_at).toLocaleDateString()}` : ""}.
                Send it again?
              </span>
              <button className="ecm-btn ecm-btn-ghost" style={{ marginLeft: "auto" }}
                onClick={() => handleSend(true)} disabled={sending}>
                Send anyway
              </button>
            </div>
          )}
          {error && <div className="ecm-note ecm-note-err"><AlertCircle size={14} /> {error}</div>}
          {sent && <div className="ecm-note" style={{ background: "#ECFDF5", border: "1px solid #A7F3D0", color: "#047857" }}><Check size={14} /> {followup ? "Follow-up sent." : "Email sent."}</div>}
          </div>
        </div>

        <div className="ecm-foot">
          {followup && !draftStarted ? (
            <>
              <div className="ecm-foot-left" />
              <div className="ecm-foot-right">
                <button className="ecm-btn ecm-btn-plain" onClick={onClose}>Cancel</button>
                <button className="ecm-btn ecm-btn-primary" onClick={startFollowup} disabled={generating || historyLoading || suppressed}>
                  {generating ? <Loader2 size={14} className="ecm-spin" /> : <Sparkles size={14} />} Generate follow-up
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="ecm-foot-left">
                <button className="ecm-btn ecm-btn-ghost" onClick={() => generate(tone, true)} disabled={generating || sending || suppressed}>
                  <RefreshCw size={14} className={generating ? "ecm-spin" : undefined} /> Regenerate
                </button>
                <button className="ecm-btn ecm-btn-ghost" onClick={copyAll} disabled={generating || !body} title={copied ? "Copied" : "Copy"}>
                  {copied ? <Check size={14} /> : <Copy size={14} />} {copied ? "Copied" : "Copy"}
                </button>
              </div>
              <div className="ecm-foot-right">
                <button className="ecm-btn ecm-btn-plain" onClick={onClose}>Cancel</button>
                <button className="ecm-btn ecm-btn-primary" onClick={() => handleSend(false)} disabled={!canSend}>
                  {sending ? <Loader2 size={14} className="ecm-spin" /> : <Send size={14} />} {sending ? "Sending…" : followup ? "Send follow-up" : "Send email"}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
