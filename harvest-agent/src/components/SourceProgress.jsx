import React from "react";
import { CheckCircle2, XCircle, Loader2, Clock, MinusCircle } from "lucide-react";
import { GLASS, GLASS_FALLBACK } from "../theme";
import useCountUp from "../useCountUp";

/*
 * Live per-source harvest progress — one compact row per ENABLED source, fed by
 * GET /harvest-status `sources`:
 *   { naukri: { state, count, message, reason, error, updated_at }, ... }
 * state ∈ pending | running | done | failed | skipped. Each source keeps its own
 * latest message, so Naukri and LinkedIn no longer overwrite each other, and
 * each count rises live as batches are scraped. A LinkedIn LLM outage shows here
 * as LinkedIn "Failed · LLM unavailable" while Naukri/Dice carry on.
 *
 * Renders nothing for older API responses without `sources` (or an empty map),
 * so callers can drop it in unconditionally. `bare` drops the card chrome for
 * use inside an existing card/modal (no nested glass blur).
 */

// Glyph + tint per source — same visual identity as the rule-engine source tiles.
// Shared with LiveHarvestCard (the lane layout used on the Rule Engine pages).
export const SOURCE_META = {
  naukri:   { name: "Naukri.com",    glyph: "N",  tint: "#6D28D9" },
  linkedin: { name: "LinkedIn Jobs", glyph: "in", tint: "#2563EB" },
  dice:     { name: "Dice.com",      glyph: "⬢",  tint: "#0EA5A4" },
};

export const STATE_META = {
  pending: { label: "Pending", Icon: Clock,        cls: "is-pending" },
  running: { label: "Running", Icon: Loader2,      cls: "is-running" },
  done:    { label: "Done",    Icon: CheckCircle2, cls: "is-done" },
  failed:  { label: "Failed",  Icon: XCircle,      cls: "is-failed" },
  skipped: { label: "Skipped", Icon: MinusCircle,  cls: "is-skipped" },
};

/** Normalise `status.sources` → [[key, entry], ...]; [] when absent/malformed. */
export function sourceEntries(sources) {
  if (!sources || typeof sources !== "object" || Array.isArray(sources)) return [];
  return Object.entries(sources).filter(([, v]) => v && typeof v === "object");
}

function SourceRow({ srcKey, entry }) {
  const meta = SOURCE_META[srcKey] || { name: srcKey, glyph: (srcKey || "?").slice(0, 1).toUpperCase(), tint: "#64748B" };
  const st = STATE_META[entry.state] || STATE_META.pending;
  const count = useCountUp(entry.count ?? 0);
  const failed = entry.state === "failed";
  const detail = failed
    ? [entry.reason, entry.error && entry.error !== entry.reason ? entry.error : ""].filter(Boolean).join(" — ")
    : "";
  return (
    <li className={"sp-row " + st.cls}>
      <span className="sp-ic" style={{ background: meta.tint }} aria-hidden="true">{meta.glyph}</span>
      <span className="sp-main">
        <span className="sp-top">
          <b className="sp-name">{meta.name}</b>
          <span className={"sp-badge " + st.cls}>
            <st.Icon size={12} className={entry.state === "running" ? "sp-spin" : undefined} />
            {st.label}
          </span>
        </span>
        <span className="sp-msg" title={entry.message || ""}>{entry.message || "—"}</span>
        {failed && detail && (
          <span className="sp-err" title={entry.error || entry.reason}>{detail}</span>
        )}
      </span>
      <span className="sp-count" aria-label={`${entry.count ?? 0} jobs`}>
        <b>{count}</b>
        <small>jobs</small>
      </span>
    </li>
  );
}

export default function SourceProgress({ sources, bare = false, title = "Sources" }) {
  const entries = sourceEntries(sources);
  if (entries.length === 0) return null;
  return (
    <section className={"sp-card" + (bare ? " sp-bare" : "")} aria-label="Per-source harvest progress">
      <style>{CSS}</style>
      <div className="sp-h">
        <span>{title}</span>
        <small>{entries.filter(([, e]) => e.state === "done").length} of {entries.length} done</small>
      </div>
      <ul className="sp-list">
        {entries.map(([key, entry]) => <SourceRow key={key} srcKey={key} entry={entry} />)}
      </ul>
    </section>
  );
}

const CSS = `
.sp-card{${GLASS}border-radius:14px;padding:14px 16px;margin-top:16px;}
.sp-card.sp-bare{background:none;border:0;box-shadow:none;-webkit-backdrop-filter:none;backdrop-filter:none;padding:0;margin-top:14px;}
.sp-h{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;}
.sp-h span{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#64748B;}
.sp-h small{font-size:11.5px;color:#94A3B8;}
.sp-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px;}
.sp-row{display:flex;align-items:center;gap:12px;padding:9px 12px;border:1px solid rgba(203,213,225,.7);border-radius:11px;background:rgba(255,255,255,.65);min-width:0;}
.sp-row.is-failed{border-color:#FCA5A5;background:linear-gradient(90deg,rgba(254,242,242,.9),rgba(255,255,255,.5) 60%);}
.sp-row.is-done{border-color:#BBF7D0;}
.sp-ic{width:32px;height:32px;border-radius:9px;display:grid;place-items:center;color:#fff;font-weight:800;font-size:13px;flex:none;}
.sp-main{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px;}
.sp-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.sp-name{font-size:13.5px;color:#1E293B;}
.sp-badge{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:700;padding:1px 8px;border-radius:999px;border:1px solid transparent;}
.sp-badge.is-pending{background:#F1F5F9;color:#475569;}
.sp-badge.is-running{background:#FFFBEB;color:#B45309;border-color:#FDE68A;}
.sp-badge.is-done{background:#ECFDF5;color:#047857;border-color:#86EFAC;}
.sp-badge.is-failed{background:#FEF2F2;color:#B91C1C;border-color:#FCA5A5;}
.sp-badge.is-skipped{background:#F8FAFC;color:#64748B;border-color:#E2E8F0;}
.sp-msg{font-size:12px;color:#64748B;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.sp-err{font-size:12px;font-weight:600;color:#B91C1C;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.sp-count{flex:none;display:flex;flex-direction:column;align-items:flex-end;line-height:1.05;}
.sp-count b{font-size:20px;font-weight:800;color:#1E293B;font-variant-numeric:tabular-nums;letter-spacing:-.02em;}
.sp-count small{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:#94A3B8;}
.sp-spin{animation:spRot .8s linear infinite;}
@keyframes spRot{to{transform:rotate(360deg);}}
@media (prefers-reduced-motion: reduce){.sp-spin{animation:none;}}
${GLASS_FALLBACK(".sp-card:not(.sp-bare)")}
`;
