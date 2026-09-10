import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  RefreshCw, Send, Mail, X, Download, Search, Calendar, ChevronDown, Copy, Check, CornerUpRight,
} from "lucide-react";
import { getOutreachHistory, ApiError } from "./api";
import OutreachThread from "./components/OutreachThread";
import EmailComposeModal from "./components/EmailComposeModal";

/* Mail logs (route /outreach) — the log of every outreach email sent from
 * HarvestAgent, read from the backend (GET /outreach/history). It's the DB source
 * of truth for what's been sent: initial emails, follow-ups, and manually logged
 * LinkedIn messages. Each row shows the contact, company, subject, tone, delivery
 * engagement (Mailjet events), and when it was sent; opening a row reads the full
 * message + thread and offers a follow-up. Renders inside the shared layout
 * (ha-root → Sidebar → this <main>), reusing the app's ha-* container styles. */

// ── Formatting helpers ──────────────────────────────────────────────────────

function fmtAbs(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

// Compact "3h ago" / "2d ago" relative label for the Sent column.
function fmtRel(iso) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return fmtAbs(iso);
}

const AVATAR_COLORS = ["#6366F1", "#0EA5E9", "#8B5CF6", "#10B981", "#F43F5E", "#F59E0B", "#14B8A6", "#EC4899"];
function initials(name, email) {
  const base = (name || email || "?").trim();
  const parts = base.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return base.slice(0, 2).toUpperCase();
}
function avatarColor(seed) {
  const s = (seed || "?");
  let n = 0;
  for (let i = 0; i < s.length; i += 1) n = (n + s.charCodeAt(i)) % AVATAR_COLORS.length;
  return AVATAR_COLORS[n];
}

// Delivery engagement — derived from the send status + latest Mailjet event.
function engagement(it) {
  if (it.status === "failed") return { label: "Failed", color: "#B91C1C", dot: "#B91C1C" };
  switch (it.delivery_status) {
    case "opened":
    case "clicked":
      return { label: "Opened", color: "#0E7C5A", dot: "#0E7C5A" };
    case "delivered":
      return { label: "Delivered", color: "#1E40AF", dot: "#2563EB" };
    case "bounced":
      return { label: "Bounced", color: "#B91C1C", dot: "#B91C1C" };
    case "blocked":
      return { label: "Blocked", color: "#B91C1C", dot: "#B91C1C" };
    case "spam":
      return { label: "Spam", color: "#B91C1C", dot: "#B91C1C" };
    default:
      return { label: "Sent", color: "#64748B", dot: "#94A3B8" };
  }
}

const TONE_STYLE = {
  Formal: { color: "#3730A3", bg: "#EEF0FF" },
  Friendly: { color: "#0E7C5A", bg: "#E7F7F0" },
  Direct: { color: "#9A3412", bg: "#FFF1E8" },
  "Follow-up": { color: "#92580B", bg: "#FFF6E9" },
};
function toneMeta(it) {
  if (it.tone && TONE_STYLE[it.tone]) return { label: it.tone, ...TONE_STYLE[it.tone] };
  if (it.tone) return { label: it.tone, color: "#475569", bg: "#F1F5F9" };
  if (it.outreach_kind === "followup") return { label: "Follow-up", ...TONE_STYLE["Follow-up"] };
  return null;
}

const CLIENT_LABEL = { active: "Active client", new: "New client", unknown: "" };

const RANGES = [
  { key: "24h", label: "Last 24 hours", ms: 24 * 3600e3 },
  { key: "7d", label: "Last 7 days", ms: 7 * 24 * 3600e3 },
  { key: "30d", label: "Last 30 days", ms: 30 * 24 * 3600e3 },
  { key: "all", label: "All time", ms: null },
];

// ── Small presentational bits ───────────────────────────────────────────────

function StatCard({ icon, label, value, tone }) {
  const color = tone === "bad" ? "#B91C1C" : "#0F172A";
  const labelColor = tone === "bad" ? "#B91C1C" : "#64748B";
  return (
    <div style={{ background: "#fff", border: "1px solid #E2E8F0", borderRadius: 14, padding: "16px 18px", minWidth: 180, boxShadow: "0 1px 2px rgba(15,23,42,.04)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 9, color: labelColor, fontSize: 13, fontWeight: 600 }}>
        {icon} {label}
      </div>
      <div style={{ fontSize: 28, fontWeight: 800, marginTop: 8, letterSpacing: "-.02em", color, fontVariantNumeric: "tabular-nums" }}>{value}</div>
    </div>
  );
}

function EngagementDot({ it }) {
  const e = engagement(it);
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, fontWeight: 700, color: e.color }}>
      <span style={{ width: 7, height: 7, borderRadius: "50%", background: e.dot }} /> {e.label}
    </span>
  );
}

function ToneChip({ it }) {
  const t = toneMeta(it);
  if (!t) return <span style={{ color: "#CBD5E1" }}>—</span>;
  return <span style={{ fontSize: 11.5, fontWeight: 700, color: t.color, background: t.bg, borderRadius: 999, padding: "3px 10px", whiteSpace: "nowrap" }}>{t.label}</span>;
}

// ── Page ────────────────────────────────────────────────────────────────────

export default function OutreachHistoryPage() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Filters (all client-side over the loaded list).
  const [search, setSearch] = useState("");
  const [rangeKey, setRangeKey] = useState("all");
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const [dateOpen, setDateOpen] = useState(false);
  const dateRef = useRef(null);

  // The row whose full thread is open in the detail modal + that thread's messages.
  const [detail, setDetail] = useState(null);
  const [thread, setThread] = useState([]);
  const [threadLoading, setThreadLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  // Follow-up composer context ({ job, parentOutreachId }) — opens EmailComposeModal.
  const [composeFor, setComposeFor] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await getOutreachHistory({ limit: 200 });
      setItems(res.items || []);
    } catch (err) {
      setError(err instanceof ApiError ? `Could not load mail logs: ${err.message}` : "Could not reach the harvest backend.");
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Close the date dropdown on an outside click.
  useEffect(() => {
    if (!dateOpen) return undefined;
    const onDown = (e) => { if (dateRef.current && !dateRef.current.contains(e.target)) setDateOpen(false); };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [dateOpen]);

  // Open a row → fetch the whole thread for its job (or recruiter).
  const openDetail = useCallback(async (it) => {
    setDetail(it);
    setThread([]);
    setThreadLoading(true);
    try {
      const params = it.job_id ? { job_id: it.job_id } : it.recruiter_id ? { recruiter_id: it.recruiter_id } : null;
      const res = params ? await getOutreachHistory(params) : { items: [it] };
      setThread(res.items && res.items.length ? res.items : [it]);
    } catch {
      setThread([it]);
    } finally {
      setThreadLoading(false);
    }
  }, []);

  // Escape closes the detail modal (but not while the composer is up).
  useEffect(() => {
    if (!detail || composeFor) return undefined;
    const onKey = (e) => { if (e.key === "Escape") setDetail(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [detail, composeFor]);

  const rangeLabel = RANGES.find((r) => r.key === rangeKey)?.label || "All time";
  const hasDateFilter = rangeKey !== "all" || !!customFrom || !!customTo;

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const now = Date.now();
    const range = RANGES.find((r) => r.key === rangeKey);
    const fromMs = customFrom ? new Date(customFrom).getTime() : (range && range.ms ? now - range.ms : null);
    const toMs = customTo ? new Date(customTo).getTime() + 24 * 3600e3 : null; // inclusive end-of-day
    return items.filter((it) => {
      if (q && !((it.contact_name || "").toLowerCase().includes(q) || (it.to_email || "").toLowerCase().includes(q))) return false;
      const t = it.created_at ? new Date(it.created_at).getTime() : null;
      if (fromMs != null && (t == null || t < fromMs)) return false;
      if (toMs != null && (t == null || t > toMs)) return false;
      return true;
    });
  }, [items, search, rangeKey, customFrom, customTo]);

  const stats = useMemo(() => {
    let sent = 0; let failed = 0;
    for (const it of filtered) {
      if (it.status === "failed" || it.delivery_status === "bounced" || it.delivery_status === "blocked") failed += 1;
      else sent += 1;
    }
    return { sent, failed };
  }, [filtered]);

  const resetFilters = () => { setSearch(""); setRangeKey("all"); setCustomFrom(""); setCustomTo(""); };

  const exportCsv = () => {
    const head = ["Contact", "Email", "Company", "Client", "Subject", "Tone", "Type", "Status", "Engagement", "Sent"];
    const esc = (v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`;
    const rows = filtered.map((it) => [
      it.contact_name || "", it.to_email || "", it.company || "", it.client_type || "",
      it.subject || "", it.tone || "", it.outreach_kind || "", it.status || "",
      engagement(it).label, it.created_at || "",
    ].map(esc).join(","));
    const blob = new Blob([[head.map(esc).join(","), ...rows].join("\r\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `mail-logs-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const copyMessage = () => {
    if (!detail) return;
    navigator.clipboard?.writeText(`${detail.subject || ""}\n\n${detail.body || ""}`);
    setCopied(true);
    setTimeout(() => setCopied(false), 1600);
  };

  const canFollowUp = detail && detail.channel === "email" && detail.job_id;
  const startFollowUp = () => {
    if (!canFollowUp) return;
    setComposeFor({
      job: { id: detail.job_id, email: detail.to_email, company: detail.company },
      parentOutreachId: detail.id,
    });
    setDetail(null);
  };

  return (
    <main className="ha-main">
      <div className="ha-page-head" style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginBottom: 8 }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 800, margin: 0, letterSpacing: "-.02em" }}>Mail logs</h1>
          <div style={{ color: "#64748B", fontSize: 13, marginTop: 6, maxWidth: "56ch" }}>
            Every outreach email sent from HarvestAgent, with delivery status and engagement per point of contact. Open a row to read the message and follow up.
          </div>
        </div>
        <div style={{ display: "flex", gap: 9 }}>
          <button className="ha-btn ha-btn-secondary" onClick={exportCsv} disabled={!filtered.length}>
            <Download size={15} /> Export CSV
          </button>
          <button className="ha-btn ha-btn-secondary" onClick={load} disabled={loading} title="Refresh" style={{ padding: "8px 10px" }}>
            <RefreshCw size={15} className={loading ? "ha-spin" : undefined} />
          </button>
        </div>
      </div>

      <div style={{ display: "flex", gap: 14, margin: "18px 0 16px", flexWrap: "wrap" }}>
        <StatCard icon={<Send size={16} color="#2563EB" />} label="Emails sent" value={stats.sent} />
        <StatCard icon={<Mail size={16} />} label="Failed" value={stats.failed} tone="bad" />
      </div>

      {/* Filter bar */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16, flexWrap: "wrap" }}>
        <div ref={dateRef} style={{ position: "relative" }}>
          <button
            className="ha-btn ha-btn-secondary"
            onClick={() => setDateOpen((v) => !v)}
            style={hasDateFilter ? { borderColor: "#2563EB", color: "#1E40AF", boxShadow: "0 0 0 2px #EAF1FF" } : undefined}
          >
            <Calendar size={15} /> {rangeLabel}
            {hasDateFilter && <span style={{ background: "#2563EB", color: "#fff", fontSize: 11, fontWeight: 800, borderRadius: 999, minWidth: 18, height: 18, display: "inline-grid", placeItems: "center", padding: "0 5px" }}>1</span>}
            <ChevronDown size={14} />
          </button>
          {dateOpen && (
            <div style={{ position: "absolute", top: "calc(100% + 8px)", left: 0, background: "#fff", border: "1px solid #E2E8F0", borderRadius: 12, boxShadow: "0 20px 40px rgba(15,23,42,.16)", padding: 8, minWidth: 240, zIndex: 50 }}>
              {RANGES.map((r) => (
                <button key={r.key} onClick={() => { setRangeKey(r.key); setCustomFrom(""); setCustomTo(""); setDateOpen(false); }}
                  style={{ display: "flex", alignItems: "center", gap: 9, width: "100%", border: 0, background: "transparent", cursor: "pointer", padding: "9px 10px", borderRadius: 8, fontSize: 13.5, color: "#334155", fontFamily: "inherit", textAlign: "left" }}
                  onMouseEnter={(e) => { e.currentTarget.style.background = "#F8FAFC"; }}
                  onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}>
                  <Check size={15} style={{ color: "#2563EB", opacity: rangeKey === r.key && !customFrom && !customTo ? 1 : 0 }} /> {r.label}
                </button>
              ))}
              <div style={{ height: 1, background: "#E2E8F0", margin: "8px 4px" }} />
              <div style={{ fontSize: 11, letterSpacing: ".1em", textTransform: "uppercase", color: "#94A3B8", fontWeight: 700, padding: "2px 10px 6px" }}>Custom range</div>
              <div style={{ display: "flex", gap: 8, padding: "0 8px 6px" }}>
                <label style={{ flex: 1, fontSize: 11, color: "#64748B" }}>From
                  <input type="date" value={customFrom} onChange={(e) => { setCustomFrom(e.target.value); setRangeKey("all"); }}
                    style={{ width: "100%", marginTop: 3, border: "1px solid #CBD5E1", borderRadius: 7, padding: "6px 8px", fontSize: 12, fontFamily: "inherit" }} />
                </label>
                <label style={{ flex: 1, fontSize: 11, color: "#64748B" }}>To
                  <input type="date" value={customTo} onChange={(e) => { setCustomTo(e.target.value); setRangeKey("all"); }}
                    style={{ width: "100%", marginTop: 3, border: "1px solid #CBD5E1", borderRadius: 7, padding: "6px 8px", fontSize: 12, fontFamily: "inherit" }} />
                </label>
              </div>
            </div>
          )}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8, border: "1px solid #CBD5E1", background: "#fff", borderRadius: 10, padding: "8px 12px", minWidth: 260, flex: "0 1 360px" }}>
          <Search size={15} color="#94A3B8" />
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search by point of contact name…"
            style={{ border: 0, outline: "none", background: "transparent", fontSize: 13, width: "100%", fontFamily: "inherit", color: "#0F172A" }} />
        </div>

        {(search || hasDateFilter) && (
          <button onClick={resetFilters} style={{ border: 0, background: "transparent", color: "#64748B", fontSize: 13, fontWeight: 600, cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6, fontFamily: "inherit" }}>
            <X size={14} /> Reset filters
          </button>
        )}
      </div>

      {error && <div className="ha-errbanner">{error}</div>}

      <div className="ha-card" style={{ overflow: "hidden" }}>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 860 }}>
            <thead>
              <tr>
                {["Contact", "Company", "Subject", "Tone", "Engagement", "Sent"].map((h) => (
                  <th key={h} style={{ textAlign: "left", fontSize: 11.5, letterSpacing: ".04em", textTransform: "uppercase", color: "#94A3B8", fontWeight: 700, padding: "13px 18px", borderBottom: "1px solid #E2E8F0", whiteSpace: "nowrap" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr><td colSpan={6} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>Loading mail logs…</td></tr>
              )}
              {!loading && filtered.length === 0 && !error && (
                <tr><td colSpan={6} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                  {items.length ? "No mail matches your filters." : "No outreach sent yet."}
                </td></tr>
              )}
              {!loading && filtered.map((it) => {
                const name = it.contact_name || (it.channel === "linkedin" ? "(LinkedIn)" : it.to_email || "—");
                const client = CLIENT_LABEL[it.client_type] || "";
                return (
                  <tr key={it.id} onClick={() => openDetail(it)} title="Read message + thread"
                    style={{ cursor: "pointer", borderBottom: "1px solid #E2E8F0" }}
                    onMouseEnter={(e) => { e.currentTarget.style.background = "#F8FAFC"; }}
                    onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}>
                    <td style={{ padding: "13px 18px" }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 11 }}>
                        <div style={{ width: 38, height: 38, borderRadius: "50%", flex: "none", display: "grid", placeItems: "center", fontWeight: 700, fontSize: 13, color: "#fff", background: avatarColor(name) }}>
                          {initials(it.contact_name, it.to_email)}
                        </div>
                        <div style={{ minWidth: 0 }}>
                          <div style={{ fontWeight: 700, fontSize: 14, color: "#0F172A" }}>{name}</div>
                          <div style={{ color: "#64748B", fontSize: 12.5 }}>{it.to_email || (it.channel === "linkedin" ? "(LinkedIn)" : "—")}</div>
                        </div>
                      </div>
                    </td>
                    <td style={{ padding: "13px 18px" }}>
                      <div style={{ fontWeight: 600, fontSize: 13.5, color: "#1E293B" }}>{it.company || "—"}</div>
                      {client && <div style={{ color: "#64748B", fontSize: 12, marginTop: 2 }}>{client}</div>}
                    </td>
                    <td style={{ padding: "13px 18px" }}>
                      <div style={{ fontWeight: 600, fontSize: 13.5, color: "#0F172A", maxWidth: "34ch", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {it.subject || (it.channel === "linkedin" ? "(LinkedIn message)" : "—")}
                      </div>
                    </td>
                    <td style={{ padding: "13px 18px" }}><ToneChip it={it} /></td>
                    <td style={{ padding: "13px 18px" }}><EngagementDot it={it} /></td>
                    <td style={{ padding: "13px 18px", whiteSpace: "nowrap" }}>
                      <div style={{ fontWeight: 700, fontSize: 13, color: "#334155", fontVariantNumeric: "tabular-nums" }}>{fmtRel(it.created_at)}</div>
                      <div style={{ color: "#94A3B8", fontSize: 12, marginTop: 2, fontVariantNumeric: "tabular-nums" }}>{fmtAbs(it.created_at)}</div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Detail modal */}
      {detail && !composeFor && (
        <div
          onMouseDown={(e) => { if (e.target === e.currentTarget) setDetail(null); }}
          style={{ position: "fixed", inset: 0, background: "rgba(15,23,42,.45)", display: "flex", alignItems: "center", justifyContent: "center", padding: 24, zIndex: 1000 }}
        >
          <div role="dialog" aria-modal="true" aria-label="Sent outreach"
            style={{ width: "100%", maxWidth: 620, maxHeight: "calc(100vh - 48px)", background: "#fff", borderRadius: 12, boxShadow: "0 24px 48px rgba(15,23,42,.2)", display: "flex", flexDirection: "column", overflow: "hidden", fontFamily: "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif" }}>
            <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12, padding: "18px 22px", borderBottom: "1px solid #E2E8F0" }}>
              <div>
                <div style={{ fontSize: 16, fontWeight: 800, letterSpacing: "-.01em", textWrap: "balance" }}>
                  {detail.subject || (detail.channel === "linkedin" ? "LinkedIn message" : "Outreach")}
                </div>
                <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 10, flexWrap: "wrap" }}>
                  <EngagementDot it={detail} />
                  <ToneChip it={detail} />
                  <span style={{ color: "#94A3B8", fontSize: 12 }}>{fmtAbs(detail.created_at)}</span>
                </div>
              </div>
              <button onClick={() => setDetail(null)} aria-label="Close" style={{ border: "none", background: "transparent", cursor: "pointer", color: "#64748B", display: "inline-flex", padding: 4 }}>
                <X size={18} />
              </button>
            </div>

            <div style={{ padding: "16px 22px", overflowY: "auto" }}>
              {/* Delivery timeline */}
              <div style={{ fontSize: 11, letterSpacing: ".12em", textTransform: "uppercase", color: "#94A3B8", fontWeight: 700, marginBottom: 10 }}>Delivery</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 16px", marginBottom: 18, fontSize: 12 }}>
                <TimelineStep on label="Sent" time={fmtTime(detail.created_at)} />
                <TimelineStep on={!!detail.delivered_at} label="Delivered" time={detail.delivered_at ? fmtTime(detail.delivered_at) : "—"} />
                <TimelineStep on={!!detail.opened_at} label="Opened" time={detail.opened_at ? fmtTime(detail.opened_at) : "—"} />
                <span style={{ color: "#CBD5E1" }}>Replied · <i>tracking added later</i></span>
              </div>

              <div style={{ fontSize: 11, letterSpacing: ".12em", textTransform: "uppercase", color: "#94A3B8", fontWeight: 700, marginBottom: 12 }}>Message</div>
              <div style={{ display: "grid", gridTemplateColumns: "72px 1fr", gap: "6px 14px", fontSize: 13.5, marginBottom: 4 }}>
                {detail.from_email && (<><div style={{ color: "#64748B" }}>From</div><div style={{ color: "#0F172A", fontWeight: 600 }}>{detail.from_email}</div></>)}
                <div style={{ color: "#64748B" }}>To</div>
                <div style={{ color: "#0F172A", fontWeight: 600 }}>
                  {detail.contact_name ? `${detail.contact_name} · ` : ""}<span style={{ color: "#64748B", fontWeight: 500 }}>{detail.to_email || (detail.channel === "linkedin" ? "(LinkedIn)" : "—")}</span>
                </div>
                {detail.company && (<><div style={{ color: "#64748B" }}>Company</div><div style={{ color: "#0F172A", fontWeight: 600 }}>{detail.company}</div></>)}
              </div>

              <div style={{ marginTop: 16 }}>
                <div style={{ fontSize: 11, letterSpacing: ".12em", textTransform: "uppercase", color: "#94A3B8", fontWeight: 700, marginBottom: 10 }}>Thread</div>
                <OutreachThread messages={thread} loading={threadLoading} emptyText="No sent messages found for this contact." />
              </div>
            </div>

            <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, padding: "14px 22px", borderTop: "1px solid #E2E8F0", background: "#F8FAFC" }}>
              <button className="ha-btn ha-btn-secondary" onClick={copyMessage}>
                {copied ? <Check size={15} /> : <Copy size={15} />} {copied ? "Copied" : "Copy message"}
              </button>
              {canFollowUp && (
                <button className="ha-btn" onClick={startFollowUp} style={{ background: "#2563EB", color: "#fff", border: "1px solid #2563EB" }}>
                  <CornerUpRight size={15} /> Follow up
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Follow-up composer (opens over the page; the detail modal is closed first) */}
      {composeFor && (
        <EmailComposeModal
          job={composeFor.job}
          followup
          parentOutreachId={composeFor.parentOutreachId}
          onClose={() => setComposeFor(null)}
          onSent={() => { setComposeFor(null); load(); }}
        />
      )}
    </main>
  );
}

function TimelineStep({ on, label, time }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, color: on ? "#334155" : "#94A3B8" }}>
      <span style={{ width: 7, height: 7, borderRadius: "50%", background: on ? "#0E7C5A" : "#CBD5E1" }} />
      {label} {on && <b style={{ color: "#0F172A", fontWeight: 700 }}>{time}</b>}
    </span>
  );
}
