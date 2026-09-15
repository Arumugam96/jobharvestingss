import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { RefreshCw, Send, Mail, X, Download, Search, Calendar, ChevronDown, Check, Building2 } from "lucide-react";
import { getOutreachHistory, getOutreachStats, ApiError } from "./api";
import { fmtAbs, fmtRel, initials, avatarColor, engagement, EngagementCell, CLIENT_LABEL } from "./components/outreachUi";

/* Mail logs (route /outreach) — the log of every outreach email sent from
 * HarvestAgent, read from the backend (GET /outreach/history). It's the DB source
 * of truth for what's been sent: initial emails, follow-ups, and manually logged
 * LinkedIn messages. Each row shows the contact, company, subject, tone, delivery
 * engagement (Mailjet events), and when it was sent; clicking a row opens the full
 * message + thread on its own page (route /mail/:id) where you can follow up.
 * Renders inside the shared layout (ha-root → Sidebar → this <main>), reusing the
 * app's ha-* container styles. */

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

// ── Page ────────────────────────────────────────────────────────────────────

export default function OutreachHistoryPage() {
  const [searchParams, setSearchParams] = useSearchParams();

  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Filters (applied server-side across the whole dataset). Initialised from the
  // URL query so a deep link — and the Back button from the thread page — restores
  // the exact filtered, paged list instead of resetting everything.
  const [search, setSearch] = useState(() => searchParams.get("search") || "");        // point-of-contact name / email
  const [companyQ, setCompanyQ] = useState(() => searchParams.get("company") || "");     // company
  const [rangeKey, setRangeKey] = useState(() => searchParams.get("range") || "all");
  const [customFrom, setCustomFrom] = useState(() => searchParams.get("from") || "");
  const [customTo, setCustomTo] = useState(() => searchParams.get("to") || "");
  const [dateOpen, setDateOpen] = useState(false);
  const dateRef = useRef(null);

  // Debounced search values so typing doesn't fire a request per keystroke. Seeded
  // from the restored filters so the first load is already filtered (no flicker).
  const [dq, setDq] = useState(() => (searchParams.get("search") || "").trim());
  const [dCompany, setDCompany] = useState(() => (searchParams.get("company") || "").trim());
  useEffect(() => { const t = setTimeout(() => setDq(search.trim()), 350); return () => clearTimeout(t); }, [search]);
  useEffect(() => { const t = setTimeout(() => setDCompany(companyQ.trim()), 350); return () => clearTimeout(t); }, [companyQ]);

  // Server pagination + whole-dataset stats.
  const PAGE_SIZE = 100;
  const [page, setPage] = useState(() => Math.max(1, parseInt(searchParams.get("page") || "1", 10) || 1));
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [stats, setStats] = useState({ sent: 0, failed: 0 });
  const [exporting, setExporting] = useState(false);
  const seqRef = useRef(0);

  const rangeLabel = RANGES.find((r) => r.key === rangeKey)?.label || "All time";
  const hasDateFilter = rangeKey !== "all" || !!customFrom || !!customTo;

  // Date filter → backend YYYY-MM-DD params (a custom range wins over the presets).
  const dateParams = useMemo(() => {
    if (customFrom || customTo) return { date_from: customFrom, date_to: customTo };
    const range = RANGES.find((r) => r.key === rangeKey);
    if (range && range.ms) return { date_from: new Date(Date.now() - range.ms).toISOString().slice(0, 10), date_to: "" };
    return { date_from: "", date_to: "" };
  }, [rangeKey, customFrom, customTo]);

  // Any filter change resets to page 1 — but not on the initial mount, so a page
  // restored from the URL survives (the deps are already at their restored values).
  const firstFilterRun = useRef(true);
  useEffect(() => {
    if (firstFilterRun.current) { firstFilterRun.current = false; return; }
    setPage(1);
  }, [dq, dCompany, dateParams]);

  // Mirror the filter/page state back to the URL (replace, so it doesn't spam the
  // history stack). This is what makes `location.pathname + location.search` — the
  // `from` handed to the thread page — restore the list on Back.
  useEffect(() => {
    const sp = new URLSearchParams();
    if (search.trim()) sp.set("search", search.trim());
    if (companyQ.trim()) sp.set("company", companyQ.trim());
    if (rangeKey && rangeKey !== "all") sp.set("range", rangeKey);
    if (customFrom) sp.set("from", customFrom);
    if (customTo) sp.set("to", customTo);
    if (page > 1) sp.set("page", String(page));
    setSearchParams(sp, { replace: true });
  }, [search, companyQ, rangeKey, customFrom, customTo, page, setSearchParams]);

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

  // Open a row → the standalone thread page in a NEW browser tab, so this list
  // stays open and untouched. The new tab has no in-memory navigation state, so
  // job_id/recruiter_id ride along in the query (?job=/?recruiter=) and the thread
  // page fetches the thread from there; the row id in the path anchors the header.
  const openDetail = useCallback((it) => {
    const q = it.job_id ? `?job=${encodeURIComponent(it.job_id)}`
      : it.recruiter_id ? `?recruiter=${encodeURIComponent(it.recruiter_id)}` : "";
    window.open(`/mail/${encodeURIComponent(it.id)}${q}`, "_blank", "noopener");
  }, []);

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
                {["To", "From", "Company", "Subject", "Engagement", "Sent"].map((h) => (
                  <th key={h} style={{ textAlign: "left", fontSize: 11.5, letterSpacing: ".04em", textTransform: "uppercase", color: "#94A3B8", fontWeight: 700, padding: "13px 18px", borderBottom: "1px solid #E2E8F0", whiteSpace: "nowrap" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr><td colSpan={6} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>Loading mail logs…</td></tr>
              )}
              {!loading && items.length === 0 && !error && (
                <tr><td colSpan={6} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                  {anyFilter ? "No mail matches your filters." : "No outreach sent yet."}
                </td></tr>
              )}
              {!loading && items.map((it) => {
                const name = it.contact_name || (it.channel === "linkedin" ? "(LinkedIn)" : it.to_email || "—");
                const client = CLIENT_LABEL[it.client_type] || "";
                return (
                  <tr key={it.id} onClick={() => openDetail(it)} title="Open message + thread in a new tab"
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
                    <td style={{ padding: "13px 18px", whiteSpace: "nowrap" }}>
                      <div style={{ fontSize: 13, color: "#334155" }}>{it.from_email || "—"}</div>
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
    </main>
  );
}
