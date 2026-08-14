import React, { useEffect, useState } from "react";
import { Circle } from "lucide-react";
import { getHealth } from "./api";

/**
 * Small backend-liveness pill for GET /health.
 *
 * Event-driven, NOT a timer: it checks once on mount and again whenever the
 * window regains focus. The old unconditional 30s setInterval fired a /health
 * request forever while the app was open, flooding the backend logs — run
 * progress is already driven off the DB-backed /harvest-status + /active-run
 * endpoints, so a constant liveness ping bought nothing.
 */
export default function HealthBadge({ dark = true }) {
  const [state, setState] = useState({ status: "checking", version: "", environment: "" });

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await getHealth();
        if (!cancelled) setState({ status: res.status || "ok", version: res.version, environment: res.environment });
      } catch {
        if (!cancelled) setState({ status: "offline", version: "", environment: "" });
      }
    };
    tick();
    const onFocus = () => tick();
    window.addEventListener("focus", onFocus);
    return () => { cancelled = true; window.removeEventListener("focus", onFocus); };
  }, []);

  const tone = state.status === "ok" ? "#22C55E" : state.status === "degraded" ? "#F59E0B" : state.status === "checking" ? "#94A3B8" : "#EF4444";
  const label = state.status === "ok" ? "Online" : state.status === "degraded" ? "Degraded" : state.status === "checking" ? "Checking…" : "Offline";
  const title = state.version ? `v${state.version} · ${state.environment}` : "Backend unreachable";

  return (
    <div
      title={title}
      style={{
        display: "flex", alignItems: "center", gap: 7, fontSize: 11.5, fontWeight: 600,
        padding: "7px 10px", borderRadius: 8,
        color: dark ? "#CBD5E1" : "#475569",
        background: dark ? "rgba(255,255,255,.05)" : "#F1F5F9",
      }}
    >
      <Circle size={8} fill={tone} color={tone} />
      Backend: {label}
    </div>
  );
}
