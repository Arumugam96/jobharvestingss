import React, { useEffect, useState } from "react";
import { Loader2, Square, AlertTriangle } from "lucide-react";
import { stopHarvest, ApiError } from "../api";

/**
 * Stop-harvest control: a button shown while a harvest is running that opens a
 * type-"stop"-to-confirm dialog. Confirming asks the backend to cooperatively
 * stop the in-flight run — the jobs gathered so far are saved to the database,
 * the browser closes cleanly (no profile corruption), and the report email is
 * deferred (merged into the next successful run). The button shows "Stopping…"
 * until the run actually ends and the parent clears `harvestRunning`.
 *
 * Self-contained (all dialog styling is inline) so it can be dropped into either
 * run-control page — the Rule Engine (rec-* styles) or Source Runs (ha-* styles).
 * `className` styles the trigger button to match the host page.
 */
export default function StopHarvestButton({ harvestRunning, onStopped, className = "", style }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState("");

  // When the run finally ends (parent clears harvestRunning), reset local state.
  useEffect(() => {
    if (!harvestRunning) { setStopping(false); setOpen(false); setText(""); setError(""); }
  }, [harvestRunning]);

  if (!harvestRunning) return null;

  const canConfirm = text.trim().toLowerCase() === "stop" && !busy;
  const close = () => { if (!busy) { setOpen(false); setText(""); setError(""); } };

  const confirm = async () => {
    if (!canConfirm) return;
    setBusy(true);
    setError("");
    try {
      const res = await stopHarvest();
      setStopping(true);
      setOpen(false);
      setText("");
      if (onStopped) onStopped(res);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the backend to stop the run.");
    } finally {
      setBusy(false);
    }
  };

  const overlay = {
    position: "fixed", inset: 0, background: "rgba(15,23,42,.45)",
    display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000, padding: 16,
  };
  const card = {
    background: "#fff", borderRadius: 14, maxWidth: 460, width: "100%",
    padding: 24, boxShadow: "0 20px 50px rgba(0,0,0,.25)",
  };
  const inputStyle = {
    width: "100%", boxSizing: "border-box", padding: "10px 12px", fontSize: 14,
    border: "1px solid #CBD5E1", borderRadius: 8, outline: "none",
  };
  const btnBase = {
    display: "inline-flex", alignItems: "center", gap: 7, cursor: "pointer",
    fontSize: 13, fontWeight: 600, padding: "9px 16px", borderRadius: 8, border: "1px solid transparent",
  };

  return (
    <>
      <button
        type="button"
        className={className}
        onClick={() => setOpen(true)}
        disabled={stopping}
        style={{ ...btnBase, background: "#DC2626", borderColor: "#DC2626", color: "#fff", opacity: stopping ? 0.85 : 1, ...(style || {}) }}
        title="Stop the running harvest — saves the jobs collected so far"
      >
        {stopping ? <Loader2 size={16} className="ha-spin rec-spin" /> : <Square size={16} />}
        {stopping ? "Stopping…" : "Stop harvest"}
      </button>

      {open && (
        <div role="dialog" aria-modal="true" onMouseDown={close} style={overlay}>
          <div onMouseDown={(e) => e.stopPropagation()} style={card}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
              <span style={{ width: 38, height: 38, borderRadius: 10, background: "#FEF2F2", color: "#DC2626", display: "inline-flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                <AlertTriangle size={20} />
              </span>
              <h3 style={{ margin: 0, fontSize: 18, fontWeight: 700, color: "#0F172A" }}>Stop the running harvest?</h3>
            </div>
            <p style={{ margin: "0 0 16px", fontSize: 14, lineHeight: 1.6, color: "#475569" }}>
              The harvest finishes its current step and stops. Jobs collected so far are
              <b> saved to the database</b>, the browser closes cleanly, and <b> no report email is sent now</b> —
              it goes out with the next successful run. This can take a few moments to take effect.
            </p>
            <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#334155", marginBottom: 6 }}>
              Type <b>stop</b> to confirm
            </label>
            <input
              autoFocus
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") confirm(); if (e.key === "Escape") close(); }}
              placeholder="stop"
              style={{ ...inputStyle, marginBottom: error ? 8 : 18 }}
            />
            {error && (
              <div style={{ marginBottom: 16, padding: "8px 12px", background: "#FEF2F2", border: "1px solid #FECACA", color: "#B91C1C", borderRadius: 8, fontSize: 13 }}>
                {error}
              </div>
            )}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
              <button type="button" onClick={close} disabled={busy}
                style={{ ...btnBase, background: "#fff", borderColor: "#E2E8F0", color: "#334155", cursor: busy ? "not-allowed" : "pointer" }}>
                Cancel
              </button>
              <button type="button" onClick={confirm} disabled={!canConfirm}
                style={{ ...btnBase, background: canConfirm ? "#DC2626" : "#FCA5A5", borderColor: canConfirm ? "#DC2626" : "#FCA5A5", color: "#fff", cursor: canConfirm ? "pointer" : "not-allowed" }}>
                {busy ? <Loader2 size={16} className="ha-spin rec-spin" /> : <Square size={16} />}
                Stop harvest
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
