import React, { useEffect, useRef, useState } from "react";
import RFB from "@novnc/novnc";
import { X, Loader2, AlertTriangle } from "lucide-react";

/**
 * Embeds a live, clickable/typeable view of the server-side browser
 * (Xvfb + x11vnc + websockify, see ai-harvest-agent/supervisord.conf) so a
 * human can complete LinkedIn/Naukri login — including CAPTCHA/OTP — from
 * the React UI instead of needing a physical monitor on the host.
 */
export default function LiveBrowserView({ title, onClose }) {
  const containerRef = useRef(null);
  const [status, setStatus] = useState("connecting"); // connecting | connected | error

  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${window.location.host}/vnc/`;
    const rfb = new RFB(containerRef.current, url);
    rfb.scaleViewport = true;
    rfb.resizeSession = false;
    const onConnect = () => setStatus("connected");
    const onDisconnect = () => setStatus("error");
    rfb.addEventListener("connect", onConnect);
    rfb.addEventListener("disconnect", onDisconnect);

    return () => {
      rfb.removeEventListener("connect", onConnect);
      rfb.removeEventListener("disconnect", onDisconnect);
      try { rfb.disconnect(); } catch { /* already gone */ }
    };
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
          <span className={"lbv-status lbv-status--" + status}>
            {status === "connecting" && <><Loader2 size={13} className="lbv-spin" /> Connecting…</>}
            {status === "connected" && "Connected — click in to type"}
            {status === "error" && <><AlertTriangle size={13} /> Disconnected</>}
          </span>
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
    background: #0F172A; border-radius: 12px;
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
  .lbv-status--error { color: #F87171; }
  .lbv-close {
    background: transparent; border: 1px solid #475569; color: #E2E8F0; cursor: pointer;
    display: flex; align-items: center; gap: 5px; padding: 6px 10px; border-radius: 6px;
    font-size: 12.5px; font-weight: 600;
  }
  .lbv-close:hover { background: rgba(148,163,184,.15); color: #fff; }
  .lbv-canvas {
    width: 100%; flex: 1 1 auto; min-height: 0;
    background: #000;
  }
  .lbv-canvas canvas { width: 100% !important; height: 100% !important; object-fit: contain; }
  .lbv-spin { animation: lbv-rot 0.9s linear infinite; }
  @keyframes lbv-rot { to { transform: rotate(360deg); } }
`;
