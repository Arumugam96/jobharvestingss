import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X, Check, ShieldCheck, PhoneIncoming, PhoneMissed, Pencil } from "lucide-react";
import { GLASS_MODAL, GLASS_BTN, GLASS_INPUT, GLASS_INPUT_FOCUS } from "../theme";

/* Manual mobile-number verification for a Harvested Jobs row. The action button
 * in front of the Mobile cell opens PhoneVerifyModal, where the user records the
 * call outcome (Answered / Unanswered) plus optional comments. On save,
 * PhoneVerifyBadge next to the number shows the outcome; clicking it opens a
 * popover with the comments. UI-only for now: results live in this browser's
 * localStorage, keyed by job id, and are ignored if the row's number has since
 * changed. Styling uses the `pvm-` prefix. */

const STORAGE_KEY = "ha_phone_verification";

export const PHONE_STATUSES = {
  answered: { label: "Answered", color: "#047857", bg: "#F0FDF4", border: "#86EFAC", Icon: PhoneIncoming },
  unanswered: { label: "Unanswered", color: "#DC2626", bg: "#FEF2F2", border: "#FCA5A5", Icon: PhoneMissed },
};

// Results saved by the earlier checkbox version stored `verified: bool`.
function normalize(result) {
  if (!result || result.status) return result;
  return { ...result, status: result.verified ? "answered" : "unanswered" };
}

export function loadPhoneVerifications() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}") || {};
    return Object.fromEntries(Object.entries(raw).map(([k, v]) => [k, normalize(v)]));
  } catch {
    return {};
  }
}

export function savePhoneVerifications(map) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // storage unavailable (private window etc.) — state still lives in memory
  }
}

const styles = `
.pvm-btn { width: 24px; height: 24px; flex: none; display: inline-flex; align-items: center; justify-content: center; border-radius: 999px; border: 1px solid #CBD5E1; background: rgba(255,255,255,.85); color: #64748B; cursor: pointer; padding: 0; transition: background .12s, border-color .12s, color .12s; }
.pvm-btn:hover { background: #EFF4FF; border-color: #93C5FD; color: #2563EB; }
.pvm-btn.done { background: #ECFDF5; border-color: #86EFAC; color: #047857; }
.pvm-btn.done:hover { background: #D1FAE5; border-color: #34D399; color: #047857; }
.pvm-btn:focus-visible, .pvm-badge:focus-visible { outline: 2px solid #2563EB; outline-offset: 2px; }
.pvm-cell { display: flex; align-items: center; gap: 8px; min-width: 0; }
.pvm-cell > .pvm-val { min-width: 0; overflow: hidden; }
.pvm-badge { flex: none; display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; border-radius: 999px; border: 1px solid; padding: 0; cursor: pointer; }
.pvm-pop { position: fixed; z-index: 1000; width: 260px; ${GLASS_MODAL} border-radius: 10px; padding: 12px 14px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1E293B; white-space: normal; }
.pvm-pop-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.pvm-pop-status { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 600; }
.pvm-pop-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: #94A3B8; margin-bottom: 4px; }
.pvm-pop-text { font-size: 13px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
.pvm-pop-empty { font-size: 13px; color: #94A3B8; font-style: italic; }
.pvm-pop-foot { display: flex; align-items: center; justify-content: space-between; margin-top: 10px; font-size: 11.5px; color: #94A3B8; }
.pvm-link { display: inline-flex; align-items: center; gap: 4px; border: none; background: none; color: #2563EB; font-size: 12px; font-weight: 600; cursor: pointer; padding: 0; font-family: inherit; }
.pvm-link:hover { text-decoration: underline; }
.pvm-overlay { position: fixed; inset: 0; background: rgba(15,23,42,.45); -webkit-backdrop-filter: blur(4px); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; padding: 24px; z-index: 1000; }
.pvm-card { width: 100%; max-width: 460px; max-height: calc(100vh - 48px); ${GLASS_MODAL} border-radius: 12px; display: flex; flex-direction: column; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1E293B; }
.pvm-head { display: flex; align-items: center; justify-content: space-between; padding: 16px 22px; border-bottom: 1px solid #E2E8F0; }
.pvm-title { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
.pvm-sub { font-size: 12.5px; color: #64748B; font-weight: 400; margin-left: 8px; }
.pvm-close { width: 30px; height: 30px; border: none; background: transparent; border-radius: 6px; color: #64748B; display: flex; align-items: center; justify-content: center; cursor: pointer; }
.pvm-close:hover { background: #F1F5F9; color: #1E293B; }
.pvm-body { padding: 18px 22px; overflow-y: auto; display: flex; flex-direction: column; gap: 14px; }
.pvm-number { display: flex; flex-direction: column; gap: 2px; }
.pvm-number strong { font-size: 18px; font-weight: 600; letter-spacing: 0.01em; }
.pvm-number span { font-size: 12.5px; color: #64748B; }
.pvm-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: #94A3B8; }
.pvm-radios { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.pvm-radio { display: flex; align-items: center; gap: 9px; padding: 11px 12px; border: 1px solid #E2E8F0; border-radius: 8px; cursor: pointer; background: rgba(255,255,255,.7); }
.pvm-radio input { width: 16px; height: 16px; margin: 0; cursor: pointer; flex: none; }
.pvm-radio b { display: block; font-size: 13.5px; font-weight: 600; }
.pvm-textarea { width: 100%; min-height: 96px; ${GLASS_INPUT} border-radius: 8px; padding: 10px 12px; font-size: 13.5px; line-height: 1.5; color: #1E293B; font-family: inherit; resize: vertical; box-sizing: border-box; }
.pvm-textarea:focus { outline: none; border-color: #2563EB; box-shadow: 0 0 0 3px rgba(37,99,235,.12); ${GLASS_INPUT_FOCUS} }
.pvm-foot { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 13px 22px; border-top: 1px solid rgba(226,232,240,.9); background: rgba(248,250,252,.8); }
.pvm-fbtn { display: inline-flex; align-items: center; gap: 6px; border-radius: 8px; font-size: 13px; font-weight: 600; padding: 9px 14px; cursor: pointer; font-family: inherit; border: 1px solid transparent; }
.pvm-fbtn:disabled { opacity: .5; cursor: not-allowed; }
.pvm-fbtn-ghost { ${GLASS_BTN} color: #475569; }
.pvm-fbtn-ghost:hover { background: rgba(255,255,255,.95); border-color: #CBD5E1; color: #1E293B; }
.pvm-fbtn-primary { background: #2563EB; color: #FFFFFF; }
.pvm-fbtn-primary:hover:not(:disabled) { background: #1E40AF; }
@media (max-width: 640px) { .pvm-overlay { padding: 0; align-items: flex-end; } .pvm-card { max-width: 100%; border-radius: 12px 12px 0 0; } .pvm-radios { grid-template-columns: 1fr; } }
`;

// Rendered once by the table so the per-row button/badge/popover are styled.
export function PhoneVerifyStyles() {
  return <style>{styles}</style>;
}

// Turns green once a call status has been saved for the number.
export function PhoneVerifyButton({ onClick, verified = false }) {
  const label = verified ? "Verified — click to update" : "Verify mobile number";
  return (
    <button type="button" className={"pvm-btn" + (verified ? " done" : "")} aria-label={label} onClick={onClick}>
      <ShieldCheck size={14} />
    </button>
  );
}

const fmtTime = (iso) => {
  try { return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); } catch { return ""; }
};

// Outcome icon next to the number; hovering (or clicking/focusing) it shows a
// popover with the status and comments. A short close delay lets the pointer
// cross the gap into the popover to reach Edit. The popover is portalled to
// <body>: the table card's backdrop-filter would otherwise make position:fixed
// resolve against the card and its overflow:hidden would clip it.
export function PhoneVerifyBadge({ result, onEdit }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState(null);
  const btnRef = useRef(null);
  const popRef = useRef(null);
  const hideTimer = useRef(null);
  const show = () => { clearTimeout(hideTimer.current); setOpen(true); };
  const hideSoon = () => { clearTimeout(hideTimer.current); hideTimer.current = setTimeout(() => setOpen(false), 150); };
  useEffect(() => () => clearTimeout(hideTimer.current), []);

  useLayoutEffect(() => {
    if (!open || !btnRef.current) return;
    const r = btnRef.current.getBoundingClientRect();
    const width = 260;
    const left = Math.max(8, Math.min(r.left + r.width / 2 - width / 2, window.innerWidth - width - 8));
    const popH = popRef.current?.offsetHeight || 140;
    const below = r.bottom + 8 + popH <= window.innerHeight;
    setPos({ left, top: below ? r.bottom + 8 : Math.max(8, r.top - 8 - popH) });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    const onDown = (e) => {
      if (popRef.current?.contains(e.target) || btnRef.current?.contains(e.target)) return;
      close();
    };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  const st = result && PHONE_STATUSES[result.status];
  if (!st) return null;
  const { Icon } = st;
  return (
    <>
      <button ref={btnRef} type="button" className="pvm-badge" aria-expanded={open}
        aria-label={`${st.label} — view comments`}
        style={{ color: st.color, background: st.bg, borderColor: st.border }}
        onMouseEnter={show} onMouseLeave={hideSoon} onFocus={show}
        onClick={show}>
        <Icon size={13} />
      </button>
      {open && createPortal(
        <div ref={popRef} className="pvm-pop" role="dialog" aria-label="Verification comments"
          onMouseEnter={show} onMouseLeave={hideSoon}
          style={{ borderColor: st.border, borderTop: `3px solid ${st.color}`,
            ...(pos ? { left: pos.left, top: pos.top } : { visibility: "hidden", left: 0, top: 0 }) }}>
          <div className="pvm-pop-head">
            <span className="pvm-pop-status" style={{ color: st.color }}><Icon size={14} /> {st.label}</span>
            <button className="pvm-close" style={{ width: 24, height: 24 }} onClick={() => setOpen(false)} aria-label="Close"><X size={14} /></button>
          </div>
          <div className="pvm-pop-label">Comments</div>
          {result.remarks
            ? <div className="pvm-pop-text">{result.remarks}</div>
            : <div className="pvm-pop-empty">No comments added.</div>}
          <div className="pvm-pop-foot">
            <span>{result.updatedAt ? fmtTime(result.updatedAt) : ""}</span>
            {onEdit && <button className="pvm-link" onClick={() => { setOpen(false); onEdit(); }}><Pencil size={12} /> Edit</button>}
          </div>
        </div>,
        document.body
      )}
    </>
  );
}

export default function PhoneVerifyModal({ phone, pocName, company, initial = null, onSave = () => {}, onClose = () => {} }) {
  const [status, setStatus] = useState(initial?.status || "");
  const [remarks, setRemarks] = useState(initial?.remarks || "");

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const save = () => {
    onSave({ status, remarks: remarks.trim(), phone, updatedAt: new Date().toISOString() });
    onClose();
  };
  const clear = () => {
    onSave(null);
    onClose();
  };

  return (
    <div className="pvm-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <style>{styles}</style>
      <div className="pvm-card" role="dialog" aria-modal="true" aria-label="Verify mobile number">
        <div className="pvm-head">
          <span className="pvm-title">
            Verify mobile number
            {company && <span className="pvm-sub">· {company}</span>}
          </span>
          <button className="pvm-close" onClick={onClose} aria-label="Close"><X size={17} /></button>
        </div>

        <div className="pvm-body">
          <div className="pvm-number">
            <strong>{phone}</strong>
            {pocName && <span>{pocName}</span>}
          </div>

          <span className="pvm-label">Call status</span>
          <div className="pvm-radios" role="radiogroup" aria-label="Call status">
            {Object.entries(PHONE_STATUSES).map(([key, s]) => (
              <label key={key} className="pvm-radio"
                style={status === key ? { borderColor: s.border, background: s.bg } : undefined}>
                <input type="radio" name="pvm-status" value={key} checked={status === key}
                  onChange={() => setStatus(key)} style={{ accentColor: s.color }} />
                <b style={status === key ? { color: s.color } : undefined}>{s.label}</b>
              </label>
            ))}
          </div>

          <span className="pvm-label">Comments</span>
          <textarea className="pvm-textarea" value={remarks} onChange={(e) => setRemarks(e.target.value)}
            placeholder={status === "unanswered"
              ? "e.g. Switched off, will retry tomorrow"
              : "e.g. Spoke to the recruiter, prefers calls after 2 PM"} />
        </div>

        <div className="pvm-foot">
          <span>
            {initial && <button className="pvm-fbtn pvm-fbtn-ghost" onClick={clear}>Clear status</button>}
          </span>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="pvm-fbtn pvm-fbtn-ghost" onClick={onClose}>Cancel</button>
            <button className="pvm-fbtn pvm-fbtn-primary" onClick={save} disabled={!status}
              title={status ? undefined : "Choose Answered or Unanswered"}>
              <Check size={14} /> Save
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
