import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  RefreshCw, Send, Mail, X, Download, Search, Calendar, ChevronDown, Copy, Check, CornerUpRight, Building2,
  CheckCircle2, MailOpen, MousePointerClick, Ban, CornerUpLeft, ShieldAlert, BellOff,
} from "lucide-react";
import { getOutreachHistory, getOutreachStats, ApiError } from "./api";
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
    case "clicked":
      return { label: "Clicked", color: "#0D9488", dot: "#0D9488" };
    case "opened":
      return { label: "Opened", color: "#0E7C5A", dot: "#0E7C5A" };
    case "delivered":
      return { label: "Delivered", color: "#1E40AF", dot: "#2563EB" };
    case "bounced":
      return { label: "Bounced", color: "#B91C1C", dot: "#B91C1C" };
    case "blocked":
      return { label: "Blocked", color: "#B91C1C", dot: "#B91C1C" };
    case "spam":
      return { label: "Spam", color: "#B91C1C", dot: "#B91C1C" };
    case "unsubscribed":
      return { label: "Unsubscribed", color: "#6D28D9", dot: "#6D28D9" };
    default:
      return { label: "Sent", color: "#64748B", dot: "#94A3B8" };
  }
}

// Delivery-state label → dot/text color, shared by the detail timeline steps.
const EVENT_COLOR = {
  Delivered: "#1E40AF", Opened: "#0E7C5A", Clicked: "#0D9488",
  Bounced: "#B91C1C", Blocked: "#B91C1C", Spam: "#B91C1C", Unsubscribed: "#6D28D9",
};

// The ordered delivery lifecycle for the detail timeline, built from the row's scalar
// timestamps + event trail (not just events[]). This surfaces states the raw trail
// misses: "Delivered" is seeded from the send's 200 response (delivered_at, no webhook),
// and a click implies an open (opened_at), so both show even when Mailjet only fired a
// click. De-duped by label in Sent → Delivered → Opened → Clicked order, with any
// terminal negative (Bounced/Blocked/Spam) or Unsubscribed appended.
function deliveryTimeline(it) {
  const steps = [];
  const add = (label, at) => { if (at && !steps.some((s) => s.label === label)) steps.push({ label, at }); };
  add("Sent", it.created_at);            // always present
  add("Delivered", it.delivered_at);     // from the 200 (or a 'sent' webhook)
  add("Opened", it.opened_at);           // open, or click implies open
  const click = (it.events || []).find((e) => (e.event || "").toLowerCase() === "click");
  if (click) add("Clicked", click.at);
  if (["bounced", "blocked", "spam"].includes(it.delivery_status)) add(engagement(it).label, it.bounced_at);
  if (it.delivery_status === "unsubscribed") {
    const un = (it.events || []).find((e) => (e.event || "").toLowerCase() === "unsub");
    add("Unsubscribed", (un && un.at) || it.created_at);
  }
  return steps;
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

// The positive delivery lifecycle, in order — the set of stages a mail progresses through.
const ENGAGEMENT_STAGES = ["Delivered", "Opened", "Clicked"];

// Per-status icon + soft-pill palette for the engagement badges.
const STATUS_META = {
  Delivered:    { Icon: CheckCircle2,      color: "#1E7F4F", bg: "#E7F7EE" },
  Opened:       { Icon: MailOpen,          color: "#0E7C5A", bg: "#E7F7F0" },
  Clicked:      { Icon: MousePointerClick, color: "#0D7D74", bg: "#E4F5F3" },
  Sent:         { Icon: Send,              color: "#64748B", bg: "#F1F5F9" },
  Failed:       { Icon: Ban,               color: "#DC2626", bg: "#FDECEC" },
  Bounced:      { Icon: CornerUpLeft,      color: "#DC2626", bg: "#FDECEC" },
  Blocked:      { Icon: Ban,               color: "#DC2626", bg: "#FDECEC" },
  Spam:         { Icon: ShieldAlert,       color: "#DC2626", bg: "#FDECEC" },
  Unsubscribed: { Icon: BellOff,           color: "#6D28D9", bg: "#F1EBFD" },
};

// The delivery states a mail has reached, as labels — one icon badge is rendered per
// entry. A failed send or terminal negative (bounced/blocked/spam/unsubscribed) is a
// single badge; a positive send shows the cumulative lifecycle up to the furthest stage
// (e.g. Delivered → Opened → Clicked).
function engagementLabels(it) {
  if (it.status === "failed") return ["Failed"];
  const ds = it.delivery_status;
  if (ds === "bounced") return ["Bounced"];
  if (ds === "blocked") return ["Blocked"];
  if (ds === "spam") return ["Spam"];
  if (ds === "unsubscribed") return ["Unsubscribed"];
  const furthest = ds === "clicked" ? "Clicked" : ds === "opened" ? "Opened" : ds === "delivered" ? "Delivered" : null;
  const idx = ENGAGEMENT_STAGES.indexOf(furthest);
  return idx === -1 ? ["Sent"] : ENGAGEMENT_STAGES.slice(0, idx + 1);
}

function StatusBadge({ label }) {
  const meta = STATUS_META[label] || STATUS_META.Sent;
  const Icon = meta.Icon;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 5, background: meta.bg, color: meta.color, borderRadius: 999, padding: "3px 10px 3px 8px", fontSize: 11.5, fontWeight: 700, whiteSpace: "nowrap" }}>
      <Icon size={13} strokeWidth={2.5} /> {label}
    </span>
  );
}

// Engagement cell: the reached delivery states as horizontal icon badges.
function EngagementCell({ it }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
      {engagementLabels(it).map((label) => <StatusBadge key={label} label={label} />)}
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

  // Filters (applied server-side across the whole dataset).
  const [search, setSearch] = useState("");        // point-of-contact name / email
  const [companyQ, setCompanyQ] = useState("");     // company
  const [rangeKey, setRangeKey] = useState("all");
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const [dateOpen, setDateOpen] = useState(false);
  const dateRef = useRef(null);

  // Debounced search values so typing doesn't fire a request per keystroke.
  const [dq, setDq] = useState("");
  const [dCompany, setDCompany] = useState("");
  useEffect(() => { const t = setTimeout(() => setDq(search.trim()), 350); return () => clearTimeout(t); }, [search]);
  useEffect(() => { const t = setTimeout(() => setDCompany(companyQ.trim()), 350); return () => clearTimeout(t); }, [companyQ]);

  // Server pagination + whole-dataset stats.
  const PAGE_SIZE = 100;
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [stats, setStats] = useState({ sent: 0, failed: 0 });
  const [exporting, setExporting] = useState(false);
  const seqRef = useRef(0);

  // The row whose full thread is open in the detail modal + that thread's messages.
  const [detail, setDetail] = useState(null);
  const [thread, setThread] = useState([]);
  const [threadLoading, setThreadLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  // Follow-up composer context ({ job, parentOutreachId }) — opens EmailComposeModal.
  const [composeFor, setComposeFor] = useState(null);

  const rangeLabel = RANGES.find((r) => r.key === rangeKey)?.label || "All time";
  const hasDateFilter = rangeKey !== "all" || !!customFrom || !!customTo;

  // Date filter → backend YYYY-MM-DD params (a custom range wins over the presets).
  const dateParams = useMemo(() => {
    if (customFrom || customTo) return { date_from: customFrom, date_to: customTo };
    const range = RANGES.find((r) => r.key === rangeKey);
    if (range && range.ms) return { date_from: new Date(Date.now() - range.ms).toISOString().slice(0, 10), date_to: "" };
    return { date_from: "", date_to: "" };
  }, [rangeKey, customFrom, customTo]);

  // Any filter change resets to page 1.
  useEffect(() => { setPage(1); }, [dq, dCompany, dateParams]);

  const load = useCallback(async () => {
    const seq = ++seqRef.current;
    setLoading(true);
    setError("");
    const filters = { search: dq, company: dCompany, ...dateParams };
    try {
      const [res, st] = await Promise.all([
        getOutreachHistory({ ...filters, page, page_size: PAGE_SIZE }),
        getOutreachStats(filters),
      ]);
      if (seq !== seqRef.current) return; // a newer request superseded this one
      setItems(res.items || []);
      setTotal(res.total || 0);
      setTotalPages(res.total_pages || 1);
      setStats(st || { sent: 0, failed: 0 });
    } catch (err) {
      if (seq !== seqRef.current) return;
      setError(err instanceof ApiError ? `Could not load mail logs: ${err.message}` : "Could not reach the harvest backend.");
      setItems([]); setTotal(0); setTotalPages(1); setStats({ sent: 0, failed: 0 });
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [page, dq, dCompany, dateParams]);

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

  const anyFilter = !!(dq || dCompany || hasDateFilter);
  const resetFilters = () => { setSearch(""); setCompanyQ(""); setRangeKey("all"); setCustomFrom(""); setCustomTo(""); };

  // Export the WHOLE filtered set (every page), not just the loaded page.
  const exportCsv = async () => {
    setExporting(true);
    try {
      const filters = { search: dq, company: dCompany, ...dateParams };
      const first = await getOutreachHistory({ ...filters, page: 1, page_size: PAGE_SIZE });
      let all = first.items || [];
      const pages = Math.min(first.total_pages || 1, 200); // safety cap (~20k rows)
      for (let p = 2; p <= pages; p += 1) {
        const res = await getOutreachHistory({ ...filters, page: p, page_size: PAGE_SIZE });
        all = all.concat(res.items || []);
      }
      const head = ["Contact", "Email", "Company", "Client", "Subject", "Tone", "Type", "Status", "Engagement", "Sent"];
      const esc = (v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`;
      const rows = all.map((it) => [
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
    } catch {
      // best-effort — surface nothing rather than a partial file
    } finally {
      setExporting(false);
    }
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
          <button className="ha-btn ha-btn-secondary" onClick={exportCsv} disabled={exporting || loading || !total}>
            <Download size={15} className={exporting ? "ha-spin" : undefined} /> {exporting ? "Exporting…" : "Export CSV"}
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

        <div style={{ display: "flex", alignItems: "center", gap: 8, border: "1px solid #CBD5E1", background: "#fff", borderRadius: 10, padding: "8px 12px", minWidth: 230, flex: "0 1 300px" }}>
          <Search size={15} color="#94A3B8" />
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search by point of contact name…"
            style={{ border: 0, outline: "none", background: "transparent", fontSize: 13, width: "100%", fontFamily: "inherit", color: "#0F172A" }} />
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8, border: "1px solid #CBD5E1", background: "#fff", borderRadius: 10, padding: "8px 12px", minWidth: 200, flex: "0 1 260px" }}>
          <Building2 size={15} color="#94A3B8" />
          <input value={companyQ} onChange={(e) => setCompanyQ(e.target.value)} placeholder="Search by company…"
            style={{ border: 0, outline: "none", background: "transparent", fontSize: 13, width: "100%", fontFamily: "inherit", color: "#0F172A" }} />
        </div>

        {anyFilter && (
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
                {["Contact", "Company", "Subject", "Engagement", "Sent"].map((h) => (
                  <th key={h} style={{ textAlign: "left", fontSize: 11.5, letterSpacing: ".04em", textTransform: "uppercase", color: "#94A3B8", fontWeight: 700, padding: "13px 18px", borderBottom: "1px solid #E2E8F0", whiteSpace: "nowrap" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr><td colSpan={5} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>Loading mail logs…</td></tr>
              )}
              {!loading && items.length === 0 && !error && (
                <tr><td colSpan={5} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                  {anyFilter ? "No mail matches your filters." : "No outreach sent yet."}
                </td></tr>
              )}
              {!loading && items.map((it) => {
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
                    <td style={{ padding: "13px 18px" }}>
                      <EngagementCell it={it} />
                    </td>
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

      {!loading && total > PAGE_SIZE && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 14, flexWrap: "wrap", gap: 10, fontSize: 13, color: "#64748B" }}>
          <span style={{ fontVariantNumeric: "tabular-nums" }}>
            Showing {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} of {total}
          </span>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <button className="ha-btn ha-btn-secondary" disabled={page <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>Prev</button>
            <span style={{ fontWeight: 600, color: "#334155" }}>Page {page} of {totalPages}</span>
            <button className="ha-btn ha-btn-secondary" disabled={page >= totalPages} onClick={() => setPage((p) => Math.min(totalPages, p + 1))}>Next</button>
          </div>
        </div>
      )}

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
                  <StatusBadge label={engagement(detail).label} />
                  <ToneChip it={detail} />
                  <span style={{ color: "#94A3B8", fontSize: 12 }}>{fmtAbs(detail.created_at)}</span>
                </div>
              </div>
              <button onClick={() => setDetail(null)} aria-label="Close" style={{ border: "none", background: "transparent", cursor: "pointer", color: "#64748B", display: "inline-flex", padding: 4 }}>
                <X size={18} />
              </button>
            </div>

            <div style={{ padding: "16px 22px", overflowY: "auto" }}>
              {/* Delivery event trail — every status this mail passed through. */}
              <div style={{ fontSize: 11, letterSpacing: ".12em", textTransform: "uppercase", color: "#94A3B8", fontWeight: 700, marginBottom: 10 }}>Delivery</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 16px", marginBottom: 18, fontSize: 12 }}>
                {deliveryTimeline(detail).map((e) => (
                  <TimelineStep key={e.label} on label={e.label} time={fmtTime(e.at)} color={EVENT_COLOR[e.label] || "#94A3B8"} />
                ))}
                {deliveryTimeline(detail).length <= 1 && (
                  <span style={{ color: "#CBD5E1" }}>Awaiting delivery events…</span>
                )}
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
                <OutreachThread messages={thread} loading={threadLoading} emptyText="No sent messages found for this contact." collapsible />
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

function TimelineStep({ on, label, time, color = "#0E7C5A" }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, color: on ? "#334155" : "#94A3B8" }}>
      <span style={{ width: 7, height: 7, borderRadius: "50%", background: on ? color : "#CBD5E1" }} />
      {label} {on && <b style={{ color: "#0F172A", fontWeight: 700 }}>{time}</b>}
    </span>
  );
}
