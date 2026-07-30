import React, { useEffect, useState } from "react";
import { Circle } from "lucide-react";
import { getHealth } from "./api";

const POLL_MS = 30000;

/** Small backend-liveness pill for GET /health — polls every 30s. */
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
    const id = setInterval(tick, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
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
