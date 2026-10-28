import React, { useCallback, useEffect, useRef, useState } from "react";
import RFB from "@novnc/novnc";
import { X, Loader2, AlertTriangle, RotateCw } from "lucide-react";

// Backoff for auto-reconnecting a session that dropped after it was up (e.g. a
// proxy idle timeout). One entry per attempt; after the last one we stop and
// leave it to the Reconnect button.
const RECONNECT_DELAYS_MS = [2000, 5000, 10000];

function clearTimer(ref) {
  if (ref.current) {
    clearTimeout(ref.current);
    ref.current = null;
  }
}

const FAILED_HINT =
  "The live-view WebSocket (/vnc/) never connected. Over HTTPS this usually means the " +
  "TLS proxy isn't forwarding WebSocket upgrades (see DEPLOYMENT.md, Step 5), or your " +
  "login session expired.";

/**
 * Embeds a live, clickable/typeable view of the server-side browser
 * (Xvfb + x11vnc + websockify, see ai-harvest-agent/supervisord.conf) so a
 * human can complete LinkedIn/Naukri login — including CAPTCHA/OTP — from
 * the React UI instead of needing a physical monitor on the host.
 */
export default function LiveBrowserView({ title, onClose }) {
  const containerRef = useRef(null);
  // connecting | connected | reconnecting | dropped | failed
  const [status, setStatus] = useState("connecting");
  // Bumping this tears down the current RFB and builds a fresh one.
  const [connKey, setConnKey] = useState(0);
  const [retryAttempt, setRetryAttempt] = useState(0); // 1..N while reconnecting
  const retriesRef = useRef(0); // auto-reconnect attempts used since the last good connect
  const timerRef = useRef(null);

  useEffect(() => {
    let everConnected = false;
    let disposed = false;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${window.location.host}/vnc/`;
    const rfb = new RFB(containerRef.current, url);
    rfb.scaleViewport = true;
    rfb.resizeSession = false;
    rfb.viewOnly = false;
    const onConnect = () => {
      everConnected = true;
      retriesRef.current = 0; // a good connect re-arms the auto-reconnect budget
      setRetryAttempt(0);
      setStatus("connected");
      // Give the canvas keyboard focus so typing (credentials/OTP) works
      // without an extra click; mouse input is captured regardless.
      try { rfb.focus(); } catch { /* not focusable yet */ }
    };
    const onDisconnect = (e) => {
      if (disposed) return; // our own teardown (unmount / reconnect)
      // "connect" having fired is the reliable signal: e.detail.clean is false
      // both for a refused handshake and for a proxy cutting a live session, so
      // it only refines the log line below.
      const clean = Boolean(e && e.detail && e.detail.clean);
      if (!everConnected && retriesRef.current === 0) {
        // First (or manual) attempt never got through → almost always the proxy
        // path or auth, not a transient blip. Don't hammer it; show the hint.
        console.warn("[LiveBrowserView] could not connect to /vnc/ (clean=%s)", clean);
        setRetryAttempt(0);
        setStatus("failed");
        return;
      }
      // A live session dropped, or an auto-reconnect attempt failed → back off.
      const attempt = retriesRef.current;
      if (attempt >= RECONNECT_DELAYS_MS.length) {
        console.warn("[LiveBrowserView] connection dropped; giving up after %d retries", attempt);
        setRetryAttempt(0);
        setStatus("dropped");
        return;
      }
      const delay = RECONNECT_DELAYS_MS[attempt];
      retriesRef.current = attempt + 1;
      setRetryAttempt(attempt + 1);
      setStatus("reconnecting");
      clearTimer(timerRef);
      timerRef.current = setTimeout(() => {
        timerRef.current = null;
        setConnKey((k) => k + 1);
      }, delay);
    };
    rfb.addEventListener("connect", onConnect);
    rfb.addEventListener("disconnect", onDisconnect);

    return () => {
      disposed = true;
      rfb.removeEventListener("connect", onConnect);
      rfb.removeEventListener("disconnect", onDisconnect);
      try { rfb.disconnect(); } catch { /* already gone */ }
    };
  }, [connKey]);

  // Unmount: drop any pending auto-reconnect so it can't fire on a dead view.
  useEffect(() => () => clearTimer(timerRef), []);

  // Manual reconnect: cancel any pending retry and start fresh with a full
  // auto-reconnect budget.
  const reconnect = useCallback(() => {
    clearTimer(timerRef);
    retriesRef.current = 0;
    setRetryAttempt(0);
    setStatus("connecting");
    setConnKey((k) => k + 1);
  }, []);

  useEffect(() => {
    if (!onClose) return;
    const onKeyDown = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="lbv-overlay" onClick={(e) => { if (e.target === e.currentTarget && onClose) onClose(); }}>
      <style>{styles}</style>
      <div className="lbv-panel">
        <div className="lbv-bar">
          <span className="lbv-title">{title || "Live browser"}</span>
          <span
            className={"lbv-status lbv-status--" + status}
            title={status === "failed" ? FAILED_HINT : undefined}
          >
            {status === "connecting" && <><Loader2 size={13} className="lbv-spin" /> Connecting…</>}
            {status === "connected" && "Connected — click in to type"}
            {status === "reconnecting" && (
              <>
                <Loader2 size={13} className="lbv-spin" /> Connection dropped — reconnecting
                ({retryAttempt}/{RECONNECT_DELAYS_MS.length})…
              </>
            )}
            {status === "dropped" && <><AlertTriangle size={13} /> Connection dropped</>}
            {status === "failed" && <><AlertTriangle size={13} /> Could not connect</>}
          </span>
          {(status === "failed" || status === "dropped" || status === "reconnecting") && (
            <button type="button" className="lbv-btn" onClick={reconnect}>
              <RotateCw size={14} /> Reconnect
            </button>
          )}
          {onClose && (
            <button type="button" className="lbv-close" onClick={onClose} aria-label="Close live view">
              <X size={16} /> Close
            </button>
          )}
        </div>
        <div ref={containerRef} className="lbv-canvas" />
      </div>
    </div>
  );
}

const styles = `
  .lbv-overlay {
    position: fixed; inset: 0; z-index: 1000;
    background: rgba(15, 23, 42, 0.55);
    display: flex; align-items: center; justify-content: center;
    padding: 12px;
    overflow-y: auto;
  }
  .lbv-panel {
    width: min(1600px, 96vw);
    height: 92vh;
    max-height: calc(100vh - 24px);
    background: rgba(15,23,42,.96); border-radius: 12px;
    -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);
    border: 1px solid rgba(148,163,184,.22);
    overflow: hidden; box-shadow: 0 20px 60px rgba(0,0,0,.45);
    display: flex; flex-direction: column;
  }
  .lbv-bar {
    display: flex; align-items: center; gap: 12px; flex-shrink: 0;
    padding: 10px 14px; background: #1E293B; color: #E2E8F0;
    font-size: 13px; font-weight: 600;
  }
  .lbv-title { flex: 1; }
  .lbv-status { display: inline-flex; align-items: center; gap: 5px; font-weight: 500; color: #94A3B8; }
  .lbv-status--connected { color: #4ADE80; }
  .lbv-status--reconnecting { color: #FBBF24; }
  .lbv-status--dropped, .lbv-status--failed { color: #F87171; }
  .lbv-status--failed { cursor: help; }
  .lbv-close, .lbv-btn {
    background: transparent; border: 1px solid #475569; color: #E2E8F0; cursor: pointer;
    display: flex; align-items: center; gap: 5px; padding: 6px 10px; border-radius: 6px;
    font-size: 12.5px; font-weight: 600;
  }
  .lbv-close:hover, .lbv-btn:hover { background: rgba(148,163,184,.15); color: #fff; }
  .lbv-canvas {
    width: 100%; flex: 1 1 auto; min-height: 0;
    background: #000;
    /* Center the canvas that noVNC sizes via scaleViewport. Do NOT override the
       inner canvas width/height — noVNC computes click coordinates from the
       canvas rect, so forcing 100%/object-fit letterboxes it and makes clicks
       land off-target (esp. on small reCAPTCHA tiles). */
    display: flex; align-items: center; justify-content: center;
    overflow: hidden;
  }
  .lbv-spin { animation: lbv-rot 0.9s linear infinite; }
  @keyframes lbv-rot { to { transform: rotate(360deg); } }
`;
