import { useEffect, useState } from "react";
import { Check, X, Lock, RefreshCw, Search } from "lucide-react";
import { getPhoneLogs } from "../api";
import { C } from "../theme";
import { StatCard, Select, PlainHeader } from "../components/ui";
import { PHONE_STATUSES } from "../components/PhoneVerify";

/* Admin-only Phone logs: every call logged in the tenant, including jobs
 * self-assigned by someone else (GET /phone-verifications/logs — the backend
 * 403s non-admins; the route is also behind AdminGate). Filters and paging run
 * server-side; the tiles ignore the status filter so the split stays visible. */

const PAGE_SIZE = 50;
const STATUS_OPTIONS = [
  { value: "", label: "All statuses" },
  { value: "answered", label: "Answered" },
  { value: "unanswered", label: "Unanswered" },
];
const ASSIGNMENT_OPTIONS = [
  { value: "", label: "All jobs" },
  { value: "assigned", label: "Self-assigned" },
  { value: "unassigned", label: "Unassigned" },
];
const DAYS_OPTIONS = [
  { value: "1", label: "Last 24 hours" },
  { value: "7", label: "Last 7 days" },
  { value: "30", label: "Last 30 days" },
  { value: "0", label: "All time" },
];

/** Locale date + time for an ISO timestamp; "—" when empty. */
const fmtTime = (iso) => {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); } catch { return "—"; }
};

/** Round check / cross badge for a call's answered/unanswered status (PHONE_STATUSES colours);
 * renders nothing for unknown statuses. */
function StatusMark({ status }) {
  const st = PHONE_STATUSES[status];
  if (!st) return null;
  const Glyph = status === "answered" ? Check : X;
  return (
    <span title={st.label} aria-label={st.label} style={{
      width: 24, height: 24, borderRadius: 999, display: "inline-flex", alignItems: "center", justifyContent: "center",
      color: st.color, background: st.bg, border: `1px solid ${st.border}`,
    }}>
      <Glyph size={13} strokeWidth={3} />
    </span>
  );
}

/**
 * Admin-only Phone logs page: summary tiles, status / assignment / date filters, a debounced
 * search box and a paged table of every logged call in the tenant.
 * Fetches GET /phone-verifications/logs whenever filters, search, page or Refresh change.
 */
export default function PhoneLogsPage() {
  const [filters, setFilters] = useState({ status: "", assignment: "", days: "7" });
  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);

  /** Update one filter and jump back to page 1. */
  const setFilter = (key, value) => { setFilters((f) => ({ ...f, [key]: value })); setPage(1); };

  useEffect(() => {
    const t = setTimeout(() => { setQ(query.trim()); setPage(1); }, 350);
    return () => clearTimeout(t);
  }, [query]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getPhoneLogs({ ...filters, q, page, page_size: PAGE_SIZE })
      .then((res) => { if (!cancelled) { setData(res); setError(""); } })
      .catch((err) => { if (!cancelled) setError(err?.message || "Couldn't load phone logs."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [filters, q, page, reloadKey]);

  const items = data?.items || [];
  const total = data?.total || 0;
  const summary = data?.summary || { total: 0, answered: 0, unanswered: 0, assigned_jobs: 0 };
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <main className="ha-main">
      <div className="ha-pagehead">
        <div className="ha-pagehead-titles">
          <h1>Phone logs</h1>
          <p className="ha-sub">Every logged call in this workspace, including jobs self-assigned by others. Visible to admins only.</p>
        </div>
        <div className="ha-pagehead-actions">
          <button className="ha-btn ha-btn-secondary" onClick={() => setReloadKey((k) => k + 1)} disabled={loading} title="Refresh">
            <RefreshCw size={16} className={loading ? "ha-spin" : ""} /> Refresh
          </button>
        </div>
      </div>

      <div className="ha-pagebody">
        {error && <div className="ha-errbanner">{error}</div>}

        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          <StatCard value={summary.total} label="Total calls" color={C.primary} />
          <StatCard value={summary.answered} label="Answered" color="#047857" />
          <StatCard value={summary.unanswered} label="Unanswered" color="#DC2626" />
          <StatCard value={summary.assigned_jobs} label="Self-assigned jobs" color="#7C3AED" />
        </div>

        <div className="ha-card ha-filterbar" style={{ padding: "11px 14px", gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
          <Select label="Status" value={filters.status} onChange={(v) => setFilter("status", v)} options={STATUS_OPTIONS} />
          <Select label="Assignment" value={filters.assignment} onChange={(v) => setFilter("assignment", v)} options={ASSIGNMENT_OPTIONS} />
          <Select label="Date" value={filters.days} onChange={(v) => setFilter("days", v)} options={DAYS_OPTIONS} />
          <div className="ha-filter-search">
            <Search size={16} />
            <input className="ha-input" value={query} onChange={(e) => setQuery(e.target.value)}
              placeholder="Search job, company, phone, POC…" aria-label="Search phone logs" />
          </div>
        </div>

        <div className="ha-card" style={{ overflow: "hidden" }}>
          <div className="ha-table-scroll">
            <table className="ha-table" style={{ minWidth: 980 }}>
              <thead className="ha-thead">
                <tr>
                  <PlainHeader label="Status" align="center" width={70} />
                  <PlainHeader label="Job / Company" width={240} />
                  <PlainHeader label="Phone" width={150} />
                  <PlainHeader label="POC" width={150} />
                  <PlainHeader label="Assigned to" width={160} />
                  <PlainHeader label="Remarks" />
                  <PlainHeader label="Date" width={160} />
                </tr>
              </thead>
              <tbody>
                {loading && !data && (
                  <tr><td className="ha-td" colSpan={7} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>Loading phone logs…</td></tr>
                )}
                {!loading && items.length === 0 && (
                  <tr><td className="ha-td" colSpan={7} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>No calls match these filters.</td></tr>
                )}
                {items.map((row) => (
                  <tr key={row.id} className="ha-row">
                    <td className="ha-td" style={{ textAlign: "center" }}><StatusMark status={row.status} /></td>
                    <td className="ha-td">
                      <div style={{ fontWeight: 600, color: C.text }}>{row.job_title || "—"}</div>
                      <div style={{ fontSize: 12.5, color: C.textSoft }}>{row.company}</div>
                    </td>
                    <td className="ha-td" style={{ whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>{row.phone}</td>
                    <td className="ha-td" style={{ color: C.text }}>{row.contact_name || "—"}</td>
                    <td className="ha-td">
                      {row.assigned_to ? (
                        <span title={row.assigned_to.email} style={{
                          display: "inline-flex", alignItems: "center", gap: 5, padding: "2px 9px", borderRadius: 6,
                          background: "#F1F5F9", color: "#475569", fontSize: 12.5, fontWeight: 600, whiteSpace: "nowrap",
                        }}>
                          <Lock size={11} /> {row.assigned_to.name}
                        </span>
                      ) : <span style={{ color: "#94A3B8", fontSize: 12.5 }}>Unassigned</span>}
                    </td>
                    <td className="ha-td" style={{ color: C.textSoft, maxWidth: 320 }}>
                      <div title={row.remarks} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{row.remarks || "—"}</div>
                    </td>
                    <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>{fmtTime(row.called_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {total > PAGE_SIZE && (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12, padding: "0 4px", fontSize: 13, color: C.textSoft }}>
            <span>Showing {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} of {total}</span>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <button className="ha-btn ha-btn-secondary" disabled={page <= 1 || loading} onClick={() => setPage((p) => Math.max(1, p - 1))}>Prev</button>
              <span>Page {page} of {totalPages}</span>
              <button className="ha-btn ha-btn-secondary" disabled={page >= totalPages || loading} onClick={() => setPage((p) => Math.min(totalPages, p + 1))}>Next</button>
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
