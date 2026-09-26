import { useEffect, useRef, useState } from "react";
import {
  ArrowUpDown, ArrowUp, ArrowDown, CheckCircle2, AlertTriangle, ChevronDown, MapPin,
} from "lucide-react";
import useCountUp from "../useCountUp";
import { C, SRC_TONES, STATUS_TONES, SIZE_TIER_STYLE } from "../theme";

export const WhatsAppIcon = ({ size = 16 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
    <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51l-.57-.01c-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 0 1-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 0 1-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 0 1 2.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0 0 12.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 0 0 5.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 0 0-3.48-8.413Z"/>
  </svg>
);
export const LinkedInIcon = ({ size = 16 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
    <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.225 0z"/>
  </svg>
);

export const SourceChip = ({ source }) => (
  <span className="ha-pill" style={SRC_TONES[source] || { background: "#F1F5F9", color: "#475569" }}>{source}</span>
);

export const StatusPill = ({ status }) => {
  const tone = STATUS_TONES[status] || STATUS_TONES.no_results;
  const Icon = tone.Icon;
  return (
    <span className="ha-pill" style={{ ...tone, display: "inline-flex", alignItems: "center", gap: 5 }}>
      <Icon size={12} className={status === "running" ? "ha-spin" : ""} /> {status.replace("_", " ")}
    </span>
  );
};

// Business-filter annotation badge. The backend no longer drops jobs — it
// flags the ones that failed a filter rule (passed_filter=false) and records
// the reason. "Qualified" = passed every active rule; "Flagged" = shown but
// did not match (hover for the stage + offending value).
export const FilterStatusBadge = ({ passed, reason }) => {
  const tone = passed
    ? { background: "#ECFDF5", color: "#047857", Icon: CheckCircle2, label: "Qualified" }
    : { background: "#FFFBEB", color: "#B45309", Icon: AlertTriangle, label: "Flagged" };
  const Icon = tone.Icon;
  return (
    <span className="ha-pill" title={passed ? "Passed every active filter rule" : (reason || "Did not match the active filters")}
      style={{ background: tone.background, color: tone.color, display: "inline-flex", alignItems: "center", gap: 5, cursor: reason ? "help" : "default" }}>
      <Icon size={12} /> {tone.label}
    </span>
  );
};

export function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// A number that eases toward its value (used for the live "Jobs found" count on
// a running Run History row; finished rows have a stable value so it stays put).
export const AnimatedNumber = ({ value }) => useCountUp(Number(value) || 0);

export const StatCard = ({ value, label, color }) => (
  <div className="ha-card" style={{ flex: 1, minWidth: 160, padding: "16px 20px" }}>
    <div className="ha-statnum" style={{ color }}>{value}</div>
    <div className="ha-statlbl">{label}</div>
  </div>
);

export function SortHeader({ label, col, sort, setSort, width }) {
  const active = sort.col === col;
  const Glyph = !active ? ArrowUpDown : sort.dir === "asc" ? ArrowUp : ArrowDown;
  return (
    <th className="ha-th" style={width ? { width, minWidth: width } : undefined}>
      <button className="ha-sortbtn" style={{ color: active ? C.primary : C.textSoft }}
        onClick={() => setSort((s) => s.col === col ? { col, dir: s.dir === "asc" ? "desc" : "asc" } : { col, dir: "asc" })}>
        {label}<Glyph size={12} />
      </button>
    </th>
  );
}
export const PlainHeader = ({ label, align = "left", width }) => (
  <th className="ha-th" style={{ textAlign: align, ...(width ? { width, minWidth: width } : null) }}>{label}</th>
);

export function Select({ label, value, onChange, options, variant }) {
  const isLoc = variant === "location";
  return (
    <label className={"ha-filter-field" + (isLoc ? " ha-filter-loc" : "")}>
      <span>{isLoc && <MapPin size={13} />}{label}</span>
      <select className="ha-input ha-select" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  );
}

// Multi-select filter field: checkboxes for each option plus an "All" option
// that clears the selection (empty array == no filter == "All").
export function MultiSelect({ label, options, selected, onChange }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    function onDocMouseDown(e) {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, [open]);

  const summary = selected.length === 0
    ? "All"
    : options.filter((o) => o.value && selected.includes(o.value)).map((o) => o.summaryLabel || o.label).join(", ");

  function toggle(value) {
    onChange(selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value]);
  }

  return (
    <div className="ha-filter-field" ref={rootRef} style={{ position: "relative" }}>
      <span>{label}</span>
      <button type="button" className="ha-input ha-select ha-multiselect-btn" onClick={() => setOpen((o) => !o)}>
        <span className="ha-multiselect-summary">{summary}</span>
        <ChevronDown size={14} />
      </button>
      {open && (
        <div className="ha-multiselect-panel">
          <label className="ha-multiselect-opt">
            <input type="checkbox" checked={selected.length === 0} onChange={() => onChange([])} />
            <span>All</span>
          </label>
          {options.map((o, i) => (
            o.group ? (
              <div key={"grp-" + i} className="ha-multiselect-group"
                style={{ padding: "8px 10px 2px", fontSize: 11, fontWeight: 700, letterSpacing: ".04em", textTransform: "uppercase", color: C.textSoft }}>
                {o.group}
              </div>
            ) : (
              <label key={o.value} className="ha-multiselect-opt">
                <input type="checkbox" checked={selected.includes(o.value)} onChange={() => toggle(o.value)} />
                <span>{o.label}</span>
              </label>
            )
          ))}
        </div>
      )}
    </div>
  );
}

// Contact action in the jobs table. When `href` is set (WhatsApp) it's a link
// that opens in a new tab. Otherwise it's a button: `available` rows call
// `onClick` (open the Email/LinkedIn composer); unavailable rows also call
// `onClick` (to surface the "no data" inline message) but render greyed out.
export function ContactActionBtn({ glyph: Glyph, title, available, href, onClick, sent = false, sentTitle }) {
  if (href) {
    return (
      <a className="ha-cbtn ha-cbtn-on" href={href} target="_blank" rel="noreferrer" title={title}>
        <Glyph size={16} />
      </a>
    );
  }
  // Sent state wins the styling (a filled green pill) even when the channel value
  // is otherwise "unavailable", so a contacted recruiter always reads as sent.
  const cls = sent ? "ha-cbtn-sent" : available ? "ha-cbtn-on" : "ha-cbtn-off";
  return (
    <button
      type="button"
      className={"ha-cbtn " + cls}
      style={available || sent ? undefined : { cursor: "pointer" }}
      title={sent ? (sentTitle || title + " — sent") : available ? title : title + " not available"}
      onClick={onClick}
    >
      <Glyph size={16} />
    </button>
  );
}

/* Sidebar lives in Sidebar.jsx — the single shared nav used by every page. */

/**
 * Renders a contact value that may come from two sources — the scraped job and
 * the enriched recruiter record. When both exist (and differ) they are shown as
 * two labeled lines (Job: / Recruiter:); a single source renders unlabeled, and
 * nothing renders an em-dash. `link` is "mailto:" or "tel:"; `cls` the anchor
 * class (ha-mail / ha-tel). Used by the jobs table and the job-details view.
 */
export function DualContact({ scraped, recruiter, fallback, link, cls }) {
  const s = (scraped || "").trim();
  const r = (recruiter || "").trim();
  const dash = <span style={{ color: "#94A3B8" }}>—</span>;
  const anchor = (val) => <a className={cls} href={link + val}>{val}</a>;

  // JSON read-path rows carry only the merged value (no split source fields) —
  // fall back to it so they still render a single unlabeled contact.
  if (!s && !r) {
    const f = (fallback || "").trim();
    return f ? anchor(f) : dash;
  }

  // Both present and distinct → label each by origin.
  if (s && r && s !== r) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
        <span style={{ display: "flex", gap: 5, alignItems: "baseline" }}>
          <span style={{ fontSize: 10, fontWeight: 600, color: "#64748B", textTransform: "uppercase", letterSpacing: ".03em" }}>Job</span>
          {anchor(s)}
        </span>
        <span style={{ display: "flex", gap: 5, alignItems: "baseline" }}>
          <span style={{ fontSize: 10, fontWeight: 600, color: "#64748B", textTransform: "uppercase", letterSpacing: ".03em" }}>Recruiter</span>
          {anchor(r)}
        </span>
      </div>
    );
  }
  const single = s || r;
  return single ? anchor(single) : dash;
}

// Company-size table cell: a coloured tier badge (Small/Medium/Large/Enterprise)
// with the employee-count band in a small font beneath it. "—" when unknown.
export function CompanySizeCell({ band, tier }) {
  const count = (band || "").replace(/\s*employees?\s*$/i, "").trim();
  if (!tier && !count) return <span style={{ color: "#94A3B8" }}>—</span>;
  const s = SIZE_TIER_STYLE[tier] || { bg: "#F1F5F9", fg: "#475569" };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3, alignItems: "flex-start" }}>
      <span style={{
        background: s.bg, color: s.fg, fontSize: 11, fontWeight: 700,
        padding: "2px 9px", borderRadius: 999, letterSpacing: ".02em", whiteSpace: "nowrap",
      }}>
        {tier || "Unknown"}
      </span>
      {count && <span style={{ fontSize: 11, color: "#94A3B8", whiteSpace: "nowrap" }}>{count}</span>}
    </div>
  );
}
