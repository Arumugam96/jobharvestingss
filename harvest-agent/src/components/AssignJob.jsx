import { useEffect, useState } from "react";
import { X, Check, UserPlus, Lock, UserMinus, RefreshCw, AlertCircle } from "lucide-react";
import { assignJob, unassignJob, getMe, ApiError } from "../api";
import { GLASS_MODAL, GLASS_BTN, GLASS_INPUT, GLASS_INPUT_FOCUS } from "../theme";

/* "Self-assign" for a Harvested Jobs row (the Action column). A user claims a
 * job for themselves with an optional comment; from then on only they can open
 * it or run outreach on it — the backend enforces that (403 on job detail and
 * every outreach endpoint), the table mirrors it by locking the row for
 * everyone else. The assignee can update the comment or release the job.
 * Styling uses the `ajm-` prefix. */

// The logged-in user's email, fetched once per page load and shared by every row.
let mePromise = null;
export function useCurrentUserEmail() {
  const [email, setEmail] = useState("");
  useEffect(() => {
    let cancelled = false;
    if (!mePromise) mePromise = getMe().catch(() => { mePromise = null; return null; });
    mePromise.then((me) => { if (!cancelled && me?.email) setEmail(me.email); });
    return () => { cancelled = true; };
  }, []);
  return email;
}

export const isAssignedToOther = (assignedTo, myEmail) =>
  !!assignedTo && !!myEmail && assignedTo.email.toLowerCase() !== myEmail.toLowerCase();

const initials = (name) =>
  (name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");

const fmtTime = (iso) => {
  try { return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); } catch { return ""; }
};

const styles = `
.ajm-btn { display: inline-flex; align-items: center; gap: 5px; height: 28px; padding: 0 10px; border-radius: 999px; border: 1px solid #93C5FD; background: rgba(255,255,255,.85); color: #2563EB; font-size: 12px; font-weight: 600; cursor: pointer; white-space: nowrap; font-family: inherit; }
.ajm-btn:hover { background: #EFF4FF; }
.ajm-btn:focus-visible, .ajm-chip:focus-visible { outline: 2px solid #2563EB; outline-offset: 2px; }
.ajm-chip { display: inline-flex; align-items: center; gap: 6px; max-width: 100%; height: 28px; padding: 0 10px 0 3px; border-radius: 999px; border: 1px solid; font-size: 12px; font-weight: 600; white-space: nowrap; font-family: inherit; }
.ajm-chip.mine { background: #ECFDF5; border-color: #86EFAC; color: #047857; cursor: pointer; }
.ajm-chip.mine:hover { background: #D1FAE5; }
.ajm-chip.other { background: #F8FAFC; border-color: #E2E8F0; color: #475569; cursor: default; }
.ajm-chip span.nm { overflow: hidden; text-overflow: ellipsis; }
.ajm-av { flex: none; width: 22px; height: 22px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 700; color: #fff; }
.ajm-locked > td:not(.ajm-action) { opacity: .55; }
.ajm-overlay { position: fixed; inset: 0; background: rgba(15,23,42,.45); -webkit-backdrop-filter: blur(4px); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; padding: 24px; z-index: 1000; }
.ajm-card { width: 100%; max-width: 440px; ${GLASS_MODAL} border-radius: 12px; display: flex; flex-direction: column; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1E293B; }
.ajm-head { display: flex; align-items: center; justify-content: space-between; padding: 16px 22px; border-bottom: 1px solid #E2E8F0; }
.ajm-title { font-size: 15px; font-weight: 600; }
.ajm-close { width: 30px; height: 30px; border: none; background: transparent; border-radius: 6px; color: #64748B; display: flex; align-items: center; justify-content: center; cursor: pointer; }
.ajm-close:hover { background: #F1F5F9; color: #1E293B; }
.ajm-body { padding: 18px 22px; display: flex; flex-direction: column; gap: 12px; }
.ajm-job strong { display: block; font-size: 14.5px; font-weight: 600; }
.ajm-job span { font-size: 12.5px; color: #64748B; }
.ajm-info { font-size: 12.5px; color: #475569; background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; padding: 9px 11px; display: flex; gap: 8px; align-items: flex-start; }
.ajm-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: #94A3B8; }
.ajm-label i { text-transform: none; letter-spacing: 0; font-weight: 400; font-style: normal; }
.ajm-textarea { width: 100%; min-height: 84px; ${GLASS_INPUT} border-radius: 8px; padding: 10px 12px; font-size: 13.5px; line-height: 1.5; color: #1E293B; font-family: inherit; resize: vertical; box-sizing: border-box; }
.ajm-textarea:focus { outline: none; border-color: #2563EB; box-shadow: 0 0 0 3px rgba(37,99,235,.12); ${GLASS_INPUT_FOCUS} }
.ajm-err { display: flex; align-items: center; gap: 7px; font-size: 12.5px; border-radius: 8px; padding: 8px 11px; background: #FEF2F2; border: 1px solid #FCA5A5; color: #B91C1C; }
.ajm-foot { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 13px 22px; border-top: 1px solid rgba(226,232,240,.9); background: rgba(248,250,252,.8); }
.ajm-fbtn { display: inline-flex; align-items: center; gap: 6px; border-radius: 8px; font-size: 13px; font-weight: 600; padding: 9px 14px; cursor: pointer; font-family: inherit; border: 1px solid transparent; }
.ajm-fbtn:disabled { opacity: .5; cursor: not-allowed; }
.ajm-ghost { ${GLASS_BTN} color: #475569; }
.ajm-ghost:hover:not(:disabled) { background: rgba(255,255,255,.95); border-color: #CBD5E1; color: #1E293B; }
.ajm-danger { background: #fff; border-color: #FCA5A5; color: #B91C1C; }
.ajm-danger:hover:not(:disabled) { background: #FEF2F2; }
.ajm-primary { background: #2563EB; color: #fff; }
.ajm-primary:hover:not(:disabled) { background: #1E40AF; }
.ajm-spin { animation: ajm-rotate .8s linear infinite; }
@keyframes ajm-rotate { to { transform: rotate(360deg); } }
@media (max-width: 640px) { .ajm-overlay { padding: 0; align-items: flex-end; } .ajm-card { max-width: 100%; border-radius: 12px 12px 0 0; } }
`;

// Rendered once by the table so the per-row button/chip are styled.
export function AssignJobStyles() {
  return <style>{styles}</style>;
}

/* The Action cell: "Self-assign" when free, a green "You" chip when it's yours
 * (click to update/release), a grey lock chip with the owner's name otherwise. */
export function AssignCell({ assignedTo, myEmail, onOpen }) {
  if (!assignedTo) {
    return (
      <button type="button" className="ajm-btn" onClick={onOpen}>
        <UserPlus size={13} /> Self-assign
      </button>
    );
  }
  const tip = [
    `Assigned to ${assignedTo.name}`,
    assignedTo.assigned_at ? fmtTime(assignedTo.assigned_at) : "",
    assignedTo.note ? `“${assignedTo.note}”` : "",
  ].filter(Boolean).join("\n");
  if (!isAssignedToOther(assignedTo, myEmail)) {
    return (
      <button type="button" className="ajm-chip mine" title={`${tip}\nClick to update or unassign`} onClick={onOpen}>
        <span className="ajm-av" style={{ background: "#059669" }}>{initials(assignedTo.name)}</span>
        <span className="nm">You</span>
      </button>
    );
  }
  return (
    <span className="ajm-chip other" title={`${tip}\nOnly they can open or work on this job.`}>
      <span className="ajm-av" style={{ background: "#64748B" }}>{initials(assignedTo.name)}</span>
      <span className="nm">{assignedTo.name}</span>
      <Lock size={12} style={{ flex: "none" }} />
    </span>
  );
}

export default function AssignJobModal({ job, assignedTo, onChange = () => {}, onClose = () => {} }) {
  const mine = !!assignedTo;
  const [note, setNote] = useState(assignedTo?.note || "");
  const [busy, setBusy] = useState(""); // "" | "assign" | "unassign"
  const [error, setError] = useState("");

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, busy]);

  const run = async (kind) => {
    setBusy(kind);
    setError("");
    try {
      const res = kind === "assign" ? await assignJob(job.id, note.trim()) : await unassignJob(job.id);
      onChange(res.assigned_to || null);
      onClose();
    } catch (err) {
      // A 409 means someone else claimed it first; its message names them.
      setError(err instanceof ApiError
        ? (err.status === 409 ? `${err.message} Refresh to see the latest.` : err.message)
        : "Something went wrong. Please try again.");
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="ajm-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <style>{styles}</style>
      <div className="ajm-card" role="dialog" aria-modal="true" aria-label={mine ? "Your assignment" : "Self-assign"}>
        <div className="ajm-head">
          <span className="ajm-title">{mine ? "Assigned to you" : "Self-assign this job"}</span>
          <button className="ajm-close" onClick={onClose} disabled={!!busy} aria-label="Close"><X size={17} /></button>
        </div>

        <div className="ajm-body">
          <div className="ajm-job">
            <strong>{job.title}</strong>
            <span>{job.company}</span>
          </div>
          <div className="ajm-info">
            <Lock size={14} style={{ flex: "none", marginTop: 1 }} />
            {mine
              ? <span>Assigned {assignedTo.assigned_at ? fmtTime(assignedTo.assigned_at) : ""}. Only you can open this job or contact the POC until you unassign it.</span>
              : <span>Once assigned, only you can open this job or contact the POC. Others will see your name on it.</span>}
          </div>
          <span className="ajm-label">Comments <i>(optional)</i></span>
          <textarea className="ajm-textarea" value={note} onChange={(e) => setNote(e.target.value)} maxLength={1000}
            placeholder="e.g. Following up with the recruiter this week" />
          {error && <div className="ajm-err"><AlertCircle size={14} /> {error}</div>}
        </div>

        <div className="ajm-foot">
          <span>
            {mine && (
              <button className="ajm-fbtn ajm-danger" onClick={() => run("unassign")} disabled={!!busy}>
                {busy === "unassign" ? <RefreshCw size={14} className="ajm-spin" /> : <UserMinus size={14} />} Unassign
              </button>
            )}
          </span>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="ajm-fbtn ajm-ghost" onClick={onClose} disabled={!!busy}>Cancel</button>
            <button className="ajm-fbtn ajm-primary" onClick={() => run("assign")} disabled={!!busy}>
              {busy === "assign" ? <RefreshCw size={14} className="ajm-spin" /> : <Check size={14} />}
              {mine ? "Save" : "Self-assign"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
