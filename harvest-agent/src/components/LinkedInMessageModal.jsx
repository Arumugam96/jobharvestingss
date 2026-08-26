import React, { useState, useEffect, useCallback } from "react";
import { X, RefreshCw, Copy, Check, Sparkles, AlertCircle } from "lucide-react";
import { generateLinkedinMessage, ApiError } from "../api";

/* LinkedIn outreach message generator. Opens from the LinkedIn icon on a
 * Harvested Jobs row (only when that row has a LinkedIn URL). Generates a single
 * generic message via the LLM (POST /outreach/generate-linkedin); the user can
 * edit, regenerate, and copy it. Copy-only — no send, no attachment. Styling
 * mirrors the self-contained-overlay pattern with an `lmm-` prefix. */

const styles = `
.lmm-overlay { position: fixed; inset: 0; background: rgba(15,23,42,.45); display: flex; align-items: center; justify-content: center; padding: 24px; z-index: 1000; }
.lmm-card { width: 100%; max-width: 560px; max-height: calc(100vh - 48px); background: #FFFFFF; border-radius: 12px; box-shadow: 0 24px 48px rgba(15,23,42,.18); display: flex; flex-direction: column; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1E293B; }
.lmm-head { display: flex; align-items: center; justify-content: space-between; padding: 16px 22px; border-bottom: 1px solid #E2E8F0; }
.lmm-title { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
.lmm-sub { font-size: 12.5px; color: #64748B; font-weight: 400; margin-left: 8px; }
.lmm-close { width: 30px; height: 30px; border: none; background: transparent; border-radius: 6px; color: #64748B; display: flex; align-items: center; justify-content: center; cursor: pointer; }
.lmm-close:hover { background: #F1F5F9; color: #1E293B; }
.lmm-body { padding: 18px 22px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 12px; }
.lmm-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: #94A3B8; display: flex; align-items: center; gap: 8px; }
.lmm-aitag { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 600; color: #B45309; background: #FEF3C7; border-radius: 999px; padding: 2px 8px; text-transform: none; letter-spacing: 0; }
.lmm-textarea { width: 100%; min-height: 190px; border: 1px solid #E2E8F0; border-radius: 8px; padding: 12px; font-size: 14px; line-height: 1.6; color: #1E293B; font-family: inherit; resize: vertical; box-sizing: border-box; }
.lmm-textarea:focus { outline: none; border-color: #2563EB; box-shadow: 0 0 0 3px rgba(37,99,235,.12); }
.lmm-count { font-size: 12px; color: #94A3B8; text-align: right; }
.lmm-note { display: flex; align-items: center; gap: 7px; font-size: 12.5px; border-radius: 8px; padding: 8px 11px; }
.lmm-note-warn { background: #FFFBEB; border: 1px solid #FDE68A; color: #92400E; }
.lmm-note-err { background: #FEF2F2; border: 1px solid #FCA5A5; color: #B91C1C; }
.lmm-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 13px 22px; border-top: 1px solid #E2E8F0; background: #F8FAFC; }
.lmm-btn { display: inline-flex; align-items: center; gap: 6px; border-radius: 8px; font-size: 13px; font-weight: 600; padding: 9px 14px; cursor: pointer; font-family: inherit; border: 1px solid transparent; transition: background .12s, border-color .12s; }
.lmm-btn:disabled { opacity: .5; cursor: not-allowed; }
.lmm-btn-ghost { background: transparent; border-color: #E2E8F0; color: #475569; }
.lmm-btn-ghost:hover:not(:disabled) { background: #FFFFFF; border-color: #CBD5E1; color: #1E293B; }
.lmm-btn-primary { background: #2563EB; color: #FFFFFF; }
.lmm-btn-primary:hover:not(:disabled) { background: #1E40AF; }
.lmm-spin { animation: lmm-rotate .8s linear infinite; }
@keyframes lmm-rotate { to { transform: rotate(360deg); } }
@media (max-width: 640px) { .lmm-overlay { padding: 0; align-items: flex-end; } .lmm-card { max-width: 100%; max-height: 100vh; height: 100%; border-radius: 0; } }
`;

export default function LinkedInMessageModal({ job = {}, onClose = () => {} }) {
  const [message, setMessage] = useState("");
  const [fallbackUsed, setFallbackUsed] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  const generate = useCallback(async (regenerate) => {
    setGenerating(true);
    setError("");
    try {
      const res = await generateLinkedinMessage({ job_id: job.id, regenerate });
      setMessage(res.message || "");
      setFallbackUsed(!!res.fallback_used);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not generate the LinkedIn message.");
    } finally {
      setGenerating(false);
    }
  }, [job.id]);

  useEffect(() => { generate(false); }, [generate]);

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const copy = () => {
    navigator.clipboard?.writeText(message);
    setCopied(true);
    setTimeout(() => setCopied(false), 1600);
  };

  return (
    <div className="lmm-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <style>{styles}</style>
      <div className="lmm-card" role="dialog" aria-modal="true" aria-label="LinkedIn message">
        <div className="lmm-head">
          <span className="lmm-title">
            LinkedIn message
            {job.company && <span className="lmm-sub">· {job.company}</span>}
          </span>
          <button className="lmm-close" onClick={onClose} aria-label="Close"><X size={17} /></button>
        </div>

        <div className="lmm-body">
          <span className="lmm-label">
            Message
            <span className="lmm-aitag"><Sparkles size={11} /> AI draft</span>
          </span>
          <textarea className="lmm-textarea" value={message} onChange={(e) => setMessage(e.target.value)}
            disabled={generating} placeholder={generating ? "Generating message…" : ""} />
          <div className="lmm-count">{message.length} characters</div>

          {fallbackUsed && !error && (
            <div className="lmm-note lmm-note-warn"><AlertCircle size={14} /> AI generation was unavailable — a standard template was used.</div>
          )}
          {error && <div className="lmm-note lmm-note-err"><AlertCircle size={14} /> {error}</div>}
        </div>

        <div className="lmm-foot">
          <button className="lmm-btn lmm-btn-ghost" onClick={() => generate(true)} disabled={generating}>
            <RefreshCw size={14} className={generating ? "lmm-spin" : undefined} /> Regenerate
          </button>
          <button className="lmm-btn lmm-btn-primary" onClick={copy} disabled={generating || !message}>
            {copied ? <Check size={14} /> : <Copy size={14} />} {copied ? "Copied" : "Copy message"}
          </button>
        </div>
      </div>
    </div>
  );
}
