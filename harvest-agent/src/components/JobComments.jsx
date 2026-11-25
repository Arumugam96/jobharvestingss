import { useCallback, useEffect, useState } from "react";
import { X, Check, RefreshCw, AlertCircle, Pencil, Trash2, MessageSquarePlus, PhoneCall } from "lucide-react";
import { getJobComments, addJobComment, updateJobComment, deleteJobComment, ApiError } from "../api";
import { AssignJobStyles } from "./AssignJob";
import { C } from "../theme";

/* Job comments (My Workspace "Comments" column). A job's comment history lives in
 * job_comments — no phone number needed, so email-only rows (e.g. LinkedIn Feed
 * leads) can hold notes. The cell shows the NEWEST of the latest call's remarks and
 * the latest job comment, else the job's assignment note. */

const MAX_LEN = 4000;

const fmtTime = (iso) => {
  try { return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); } catch { return ""; }
};

const errText = (err, fallback) => (err instanceof ApiError ? err.message : fallback);

const styles = `
.jcm-card { max-width: 520px !important; }
.jcm-list { display: flex; flex-direction: column; gap: 8px; max-height: 300px; overflow-y: auto; }
.jcm-item { border: 1px solid #E2E8F0; background: #F8FAFC; border-radius: 8px; padding: 9px 11px; }
.jcm-item.note { background: #FFFBEB; border-color: #FDE68A; }
.jcm-meta { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 11.5px; color: #94A3B8; margin-bottom: 4px; }
.jcm-text { font-size: 13px; color: #1E293B; white-space: pre-wrap; word-break: break-word; }
.jcm-acts { display: inline-flex; gap: 2px; }
.jcm-ibtn { width: 24px; height: 24px; border: none; background: transparent; border-radius: 6px; color: #64748B; display: inline-flex; align-items: center; justify-content: center; cursor: pointer; }
.jcm-ibtn:hover:not(:disabled) { background: #E2E8F0; color: #1E293B; }
.jcm-ibtn.del:hover:not(:disabled) { background: #FEF2F2; color: #B91C1C; }
.jcm-empty { font-size: 12.5px; color: #94A3B8; }
.jcm-item.jcm-call { background: #F5F8FF; border-color: #C7D7FE; }
.jcm-src { font-size: 11px; color: #94A3B8; margin-top: 2px; }
`;

/**
 * The My Workspace row editor: the job's call log (edit it in place via the Phone Logs
 * call editor) and its comment history — list (newest first), add, and edit / delete
 * your own (admins: any). The job's assignment note is shown last, read-only.
 * @param {{ job: {id, title, company}, call?: object|null, onEditCall?: () => void,
 *   onChanged?: () => void, onClose: () => void }} props
 *   call: the row's latest call (callFromView shape + `label`), or null when not called.
 *   onEditCall: opens the call editor; omitted for roles that can't edit calls.
 */
export default function JobCommentModal({ job, call = null, onEditCall, onChanged = () => {}, onClose = () => {} }) {
  const [data, setData] = useState({ loading: true, comments: [], note: "", error: "" });
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(null); // { id, body }
  const [busy, setBusy] = useState(""); // "" | "add" | "edit" | `del:${id}`
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const res = await getJobComments(job.id);
      setData({ loading: false, comments: res.comments || [], note: res.assignment_note || "", error: "" });
    } catch (err) {
      setData((d) => ({ ...d, loading: false, error: errText(err, "Couldn't load comments.") }));
    }
  }, [job.id]);
  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, busy]);

  const run = async (kind, fn) => {
    setBusy(kind);
    setError("");
    try {
      await fn();
      await load();
      onChanged();
      return true;
    } catch (err) {
      setError(errText(err, "Something went wrong. Please try again."));
      return false;
    } finally {
      setBusy("");
    }
  };

  const add = async () => {
    const body = draft.trim();
    if (!body) return;
    if (await run("add", () => addJobComment(job.id, body))) setDraft("");
  };
  const saveEdit = async () => {
    const body = (editing?.body || "").trim();
    if (!body) return;
    if (await run("edit", () => updateJobComment(job.id, editing.id, body))) setEditing(null);
  };
  const remove = (c) => {
    if (!window.confirm("Delete this comment?")) return;
    run(`del:${c.id}`, () => deleteJobComment(job.id, c.id));
  };

  return (
    <div className="ajm-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <AssignJobStyles />
      <style>{styles}</style>
      <div className="ajm-card jcm-card" role="dialog" aria-modal="true" aria-label="Edit call log and comments">
        <div className="ajm-head">
          <span className="ajm-title" style={{ display: "inline-flex", alignItems: "center", gap: 7 }}>
            <Pencil size={15} /> Call log &amp; comments
          </span>
          <button className="ajm-close" onClick={onClose} disabled={!!busy} aria-label="Close"><X size={17} /></button>
        </div>

        <div className="ajm-body">
          <div className="ajm-job">
            <strong>{job.title}</strong>
            <span>{job.company}</span>
          </div>

          <span className="ajm-label">Call log</span>
          {call ? (
            <div className="jcm-item jcm-call">
              <div className="jcm-meta">
                <span>
                  {[call.by && `by ${call.by}`, call.at && fmtTime(call.at), call.inherited && "on another job"].filter(Boolean).join(" · ")}
                </span>
              </div>
              <div className="jcm-text" style={{ fontWeight: 600 }}>{call.label}</div>
              {call.remarks && <div className="jcm-text" style={{ marginTop: 3 }}>{call.remarks}</div>}
              {onEditCall ? (
                <button type="button" className="ajm-fbtn ajm-ghost" style={{ marginTop: 8, padding: "6px 11px" }}
                  disabled={!!busy} onClick={onEditCall}>
                  <PhoneCall size={13} /> Edit call log
                </button>
              ) : (
                <div className="jcm-src" style={{ marginTop: 6 }}>Only admins and business leads can edit call logs.</div>
              )}
            </div>
          ) : (
            <span className="jcm-empty">Not called yet. Log a call from the phone button in the Mobile column.</span>
          )}

          <span className="ajm-label">Add a comment</span>
          <textarea className="ajm-textarea" value={draft} maxLength={MAX_LEN} disabled={!!busy}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) add(); }}
            placeholder="e.g. Emailed the recruiter, waiting for the JD" />

          {error && <div className="ajm-err"><AlertCircle size={14} /> {error}</div>}

          <span className="ajm-label">History</span>
          <div className="jcm-list">
            {data.loading && <span className="jcm-empty">Loading…</span>}
            {!data.loading && data.error && <div className="ajm-err"><AlertCircle size={14} /> {data.error}</div>}
            {!data.loading && !data.error && data.comments.length === 0 && !data.note && (
              <span className="jcm-empty">No comments yet.</span>
            )}
            {data.comments.map((c) => (
              <div key={c.id} className="jcm-item">
                <div className="jcm-meta">
                  <span>{c.author_name} · {fmtTime(c.created_at)}{c.edited ? " · edited" : ""}</span>
                  {c.can_edit && editing?.id !== c.id && (
                    <span className="jcm-acts">
                      <button className="jcm-ibtn" title="Edit" aria-label="Edit comment" disabled={!!busy}
                        onClick={() => setEditing({ id: c.id, body: c.body })}><Pencil size={13} /></button>
                      <button className="jcm-ibtn del" title="Delete" aria-label="Delete comment" disabled={!!busy}
                        onClick={() => remove(c)}>
                        {busy === `del:${c.id}` ? <RefreshCw size={13} className="ajm-spin" /> : <Trash2 size={13} />}
                      </button>
                    </span>
                  )}
                </div>
                {editing?.id === c.id ? (
                  <>
                    <textarea className="ajm-textarea" style={{ minHeight: 64 }} value={editing.body} maxLength={MAX_LEN}
                      autoFocus disabled={!!busy} onChange={(e) => setEditing({ ...editing, body: e.target.value })} />
                    <div style={{ display: "flex", justifyContent: "flex-end", gap: 6, marginTop: 6 }}>
                      <button className="ajm-fbtn ajm-ghost" style={{ padding: "6px 11px" }} disabled={!!busy}
                        onClick={() => setEditing(null)}>Cancel</button>
                      <button className="ajm-fbtn ajm-primary" style={{ padding: "6px 11px" }}
                        disabled={!!busy || !editing.body.trim()} onClick={saveEdit}>
                        {busy === "edit" ? <RefreshCw size={13} className="ajm-spin" /> : <Check size={13} />} Save
                      </button>
                    </div>
                  </>
                ) : <div className="jcm-text">{c.body}</div>}
              </div>
            ))}
            {data.note && (
              <div className="jcm-item note">
                <div className="jcm-meta"><span>Assignment note</span></div>
                <div className="jcm-text">{data.note}</div>
              </div>
            )}
          </div>
        </div>

        <div className="ajm-foot">
          <span style={{ fontSize: 11.5, color: C.textSoft }}>Ctrl+Enter to add</span>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="ajm-fbtn ajm-ghost" onClick={onClose} disabled={!!busy}>Close</button>
            <button className="ajm-fbtn ajm-primary" onClick={add} disabled={!!busy || !draft.trim()}>
              {busy === "add" ? <RefreshCw size={14} className="ajm-spin" /> : <MessageSquarePlus size={14} />} Add comment
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/** Styles for the table-cell helpers below — rendered once by the table. */
export function JobCommentStyles() {
  return <style>{styles}</style>;
}

const ts = (iso) => { const t = iso ? Date.parse(iso) : NaN; return Number.isNaN(t) ? 0 : t; };

/**
 * Which text the Comments cell shows: the newer of the latest call's remarks and the
 * latest job comment; else the assignment note. Returns { text, source, at } or null.
 */
export function pickWorkspaceComment({ call, comment, note }) {
  const fromCall = call?.remarks ? { text: call.remarks, source: "call", at: call.at } : null;
  const fromComment = comment?.body
    ? { text: comment.body, source: "comment", at: comment.updated_at || comment.created_at, by: comment.author_name }
    : null;
  if (fromCall && fromComment) return ts(fromComment.at) >= ts(fromCall.at) ? fromComment : fromCall;
  if (fromCall || fromComment) return fromCall || fromComment;
  return note ? { text: note, source: "note" } : null;
}

const SOURCE_LABEL = { call: "Call remarks", comment: "Comment", note: "Assignment note" };

/** The My Workspace Comments cell: the picked text (3 lines, full on hover) and where it
 * came from. Editing happens from the row's leading pencil (JobCommentModal). */
export function WorkspaceCommentCell({ picked }) {
  if (!picked) return <span style={{ color: "#94A3B8" }}>—</span>;
  return (
    <div style={{ whiteSpace: "normal" }}>
      <div title={picked.text} style={{
        color: C.text, fontSize: 13, overflow: "hidden",
        display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical",
      }}>{picked.text}</div>
      <div className="jcm-src">{SOURCE_LABEL[picked.source]}{picked.by ? ` · ${picked.by}` : ""}</div>
    </div>
  );
}
