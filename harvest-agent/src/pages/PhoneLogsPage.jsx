import { useEffect, useId, useRef, useState } from "react";
import { ChevronDown, Lock, RefreshCw, Search, X } from "lucide-react";
import { getPhoneLogs, getPhoneInventory, getAssignedJobs, exportPhoneLogsXlsx, getPhoneLogAssignees } from "../api";
import { useTenant } from "../TenantContext";
import { C } from "../theme";
import { StatCard, Select, PlainHeader } from "../components/ui";
import ExportButton from "../components/ExportButton";
import { PHONE_STATUSES, outcomeInfo, outcomeDetailText } from "../components/PhoneVerify";
import { downloadCsv, downloadJson, saveBlob, fetchAllPages } from "../lib/csv";

/* Phone logs (admins + Business Leads): admins see every call logged in
 * the tenant, including jobs self-assigned by someone else, and can narrow it to one
 * business lead (the Assignee filter, GET /phone-verifications/assignees); a business
 * lead sees only their own calls (the backend forces scope "own" and 403s the admin-only
 * drill-downs, so those tiles aren't clickable for them). The route sits behind
 * PhoneLogsGate; the backend 403s everyone else. Filters and paging run
 * server-side; the tiles ignore the status filter so the split stays visible.
 * The tiles double as view switches: the call tiles show the call log (filtered by
 * status), "Total phones" lists every number held (/logs/phones) and
 * "Self-assigned jobs" lists every assigned job, called or not (/logs/assigned-jobs).
 * "Export" (CSV / Excel / JSON, the Harvested Jobs button) saves whichever list is on
 * screen: every page, with its filters. Each view builds { name, sheet, head, rows };
 * Excel goes through POST /phone-verifications/logs/xlsx with those same rows. */

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
const REMINDER_OPTIONS = [
  { value: "", label: "All reminders" },
  { value: "scheduled", label: "Scheduled" },
  { value: "delivered", label: "Delivered" },
  { value: "dismissed", label: "Dismissed" },
  { value: "called_again", label: "Called again" },
  { value: "cancelled", label: "Cancelled" },
  { value: "none", label: "No reminder" },
];
/* Reminder state -> chip. No message is sent: a reminder shows in the caller's bell once
 * due ("Delivered" = due, still open). The time under the chip is the due time while
 * open, the close time once closed (see list_logs in phone_verification_service). */
const REMINDERS = {
  scheduled: { label: "Scheduled", tone: "info" },
  delivered: { label: "Delivered", tone: "warn" },
  dismissed: { label: "Dismissed", tone: "good" },
  called_again: { label: "Called again", tone: "good" },
  cancelled: { label: "Cancelled", tone: "muted" },
};
const TONES = {
  good: { color: "#047857", bg: "#ECFDF3" },
  info: { color: "#1D4ED8", bg: "#EFF6FF" },
  warn: { color: "#B45309", bg: "#FFFBEB" },
  bad: { color: "#DC2626", bg: "#FEF2F2" },
  muted: { color: "#64748B", bg: "#F1F5F9" },
};
const PHONE_SOURCES = {
  job: { label: "Job poster", color: "#2563EB", bg: "#EFF6FF" },
  company: { label: "Company HR", color: "#B45309", bg: "#FFFBEB" },
  recruiter: { label: "Recruiter", color: "#7C3AED", bg: "#F5F3FF" },
};
/** Labels of every source holding an inventory row's number (older payloads: its own source). */
const sourceLabels = (r) => (r.sources?.length ? r.sources : [r.source]).map((s) => PHONE_SOURCES[s]?.label || s);
const EMPTY_CELL = { textAlign: "center", padding: "48px 16px", color: "#94A3B8" };
const SUBLINE = { marginTop: 3, fontSize: 12, color: C.textSoft };
const statusLabel = (s) => PHONE_STATUSES[s]?.label || s || "";
const today = () => new Date().toISOString().slice(0, 10);

/** Locale date + time for an ISO timestamp; "—" when empty. */
const fmtTime = (iso) => {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); } catch { return "—"; }
};

/** "Answered" / "Unanswered" pill in the PHONE_STATUSES colours; nothing for unknown statuses. */
function StatusPill({ status }) {
  const st = PHONE_STATUSES[status];
  if (!st) return null;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 6, padding: "3px 10px", borderRadius: 999,
      fontSize: 12, fontWeight: 700, whiteSpace: "nowrap", color: st.color, background: st.bg, border: `1px solid ${st.border}`,
    }}>
      <span style={{ width: 7, height: 7, borderRadius: 999, background: "currentColor" }} />{st.label}
    </span>
  );
}

/** Small tinted label (TONES colours). */
function Chip({ tone, children }) {
  const t = TONES[tone] || TONES.muted;
  return (
    <span style={{ display: "inline-block", padding: "2px 8px", borderRadius: 6, fontSize: 12.5, fontWeight: 600, color: t.color, background: t.bg }}>
      {children}
    </span>
  );
}

/** The call's outcome label with its details underneath; a dash for calls without one. */
function OutcomeCell({ status, outcome, details }) {
  const info = outcomeInfo(status, outcome);
  if (!info) return <span style={{ color: "#94A3B8" }}>—</span>;
  const detail = outcomeDetailText(details);
  return (
    <>
      <Chip tone={info.tone}>{info.label}</Chip>
      {detail && <div style={SUBLINE}>{detail}</div>}
    </>
  );
}

/** The reminder's state chip with its one time underneath. */
function ReminderCell({ reminder }) {
  const r = REMINDERS[reminder?.state];
  if (!r) return <span style={{ color: "#94A3B8", fontSize: 12.5 }}>No reminder</span>;
  return (
    <>
      <Chip tone={r.tone}>{r.label}</Chip>
      <div style={{ ...SUBLINE, whiteSpace: "nowrap" }}>{fmtTime(reminder.at)}</div>
    </>
  );
}

/** Grey lock pill with the assignee's name (email on hover). */
function AssigneePill({ user }) {
  if (!user) return <span style={{ color: "#94A3B8", fontSize: 12.5 }}>Unassigned</span>;
  return (
    <span title={user.email} style={{
      display: "inline-flex", alignItems: "center", gap: 5, padding: "2px 9px", borderRadius: 6,
      background: "#F1F5F9", color: "#475569", fontSize: 12.5, fontWeight: 600, whiteSpace: "nowrap",
    }}>
      <Lock size={11} /> {user.name}
    </span>
  );
}

/** Job title over company, the table's two-line job cell. */
function JobCell({ title, company }) {
  return (
    <>
      <div style={{ fontWeight: 600, color: C.text }}>{title || "—"}</div>
      <div style={{ fontSize: 12.5, color: C.textSoft }}>{company}</div>
    </>
  );
}

/** Search box for a filter bar. */
function SearchBox({ value, onChange, placeholder, label }) {
  return (
    <div className="ha-filter-search">
      <Search size={16} />
      <input className="ha-input" value={value} onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder} aria-label={label} />
    </div>
  );
}

/** "Showing a–b of n" + Prev/Next; renders nothing when everything fits on one page. */
function Pager({ page, total, loading, setPage }) {
  if (total <= PAGE_SIZE) return null;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12, padding: "0 4px", fontSize: 13, color: C.textSoft }}>
      <span>Showing {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} of {total}</span>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <button className="ha-btn ha-btn-secondary" disabled={page <= 1 || loading} onClick={() => setPage((p) => Math.max(1, p - 1))}>Prev</button>
        <span>Page {page} of {totalPages}</span>
        <button className="ha-btn ha-btn-secondary" disabled={page >= totalPages || loading} onClick={() => setPage((p) => Math.min(totalPages, p + 1))}>Next</button>
      </div>
    </div>
  );
}

/**
 * Admin-only "Assignee" filter: a typeable combobox whose options come from
 * GET /phone-verifications/assignees?q=… (server-side, debounced). Reuses the
 * SearchableSelect (.ha-combo) styling. `value` is the selected {id, name, email} or
 * null ("All assignees"); `onChange` gets the same.
 */
function AssigneeFilter({ value, onChange }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [highlight, setHighlight] = useState(0);
  const rootRef = useRef(null);
  const inputRef = useRef(null);
  const listboxId = useId();

  // Fetch matching business leads while the panel is open (debounced on the typed text).
  useEffect(() => {
    if (!open) return undefined;
    let cancelled = false;
    setLoading(true);
    const t = setTimeout(() => {
      getPhoneLogAssignees(query.trim())
        .then((res) => { if (!cancelled) { setItems(res?.items || []); setError(""); } })
        .catch((err) => { if (!cancelled) { setItems([]); setError(err?.message || "Couldn't load assignees."); } })
        .finally(() => { if (!cancelled) setLoading(false); });
    }, 300);
    return () => { cancelled = true; clearTimeout(t); };
  }, [open, query]);

  useEffect(() => {
    if (!open) return undefined;
    /** Closes the panel on a mousedown outside the field. */
    function onDocMouseDown(e) {
      if (rootRef.current && !rootRef.current.contains(e.target)) { setOpen(false); setQuery(""); }
    }
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, [open]);

  // Option 0 is "All assignees" (null); the rest are the fetched business leads.
  const options = [null, ...items];
  /** Open the panel with an empty query. */
  const openPanel = () => { if (!open) { setOpen(true); setQuery(""); setHighlight(0); } };
  /** Pick an assignee (or null for all) and close. */
  const choose = (opt) => {
    onChange(opt);
    setOpen(false);
    setQuery("");
    if (inputRef.current) inputRef.current.blur();
  };
  /** Keyboard: ↓/Enter opens; when open ↑/↓ move, Enter selects, Escape closes. */
  const onKeyDown = (e) => {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "Enter") { e.preventDefault(); openPanel(); }
      return;
    }
    if (e.key === "ArrowDown") { e.preventDefault(); setHighlight((h) => Math.min(options.length - 1, h + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setHighlight((h) => Math.max(0, h - 1)); }
    else if (e.key === "Enter") { e.preventDefault(); if (highlight < options.length) choose(options[highlight]); }
    else if (e.key === "Escape") { e.preventDefault(); setOpen(false); setQuery(""); }
  };
  const selectedLabel = value ? value.name || value.email : "";

  return (
    <div className="ha-filter-field ha-combo" ref={rootRef}>
      <span>Assignee</span>
      <div className="ha-combo-control">
        <input
          ref={inputRef}
          className="ha-input ha-combo-input"
          type="text"
          role="combobox"
          aria-expanded={open}
          aria-controls={listboxId}
          aria-autocomplete="list"
          aria-label="Filter by assignee"
          autoComplete="off"
          placeholder={selectedLabel || "All assignees"}
          value={open ? query : selectedLabel}
          title={value?.email || undefined}
          onChange={(e) => { openPanel(); setQuery(e.target.value); setHighlight(0); }}
          onFocus={openPanel}
          onMouseDown={openPanel}
          onKeyDown={onKeyDown}
        />
        {value && !open && (
          <button type="button" className="ha-combo-clear" title="Clear" aria-label="Clear assignee"
            onMouseDown={(e) => { e.preventDefault(); onChange(null); }}>
            <X size={13} />
          </button>
        )}
        <ChevronDown size={14} className="ha-combo-caret" />
      </div>
      {open && (
        <div className="ha-combo-panel" id={listboxId} role="listbox">
          <div role="option" aria-selected={!value} data-idx={0}
            className={"ha-combo-opt" + (highlight === 0 ? " is-active" : "") + (!value ? " is-selected" : "")}
            onMouseEnter={() => setHighlight(0)}
            onMouseDown={(e) => { e.preventDefault(); choose(null); }}>
            All assignees
          </div>
          {loading ? (
            <div className="ha-combo-empty">Loading…</div>
          ) : error ? (
            <div className="ha-combo-empty" style={{ color: "#B91C1C" }}>{error}</div>
          ) : items.length === 0 ? (
            <div className="ha-combo-empty" style={{ whiteSpace: "normal" }}>
              {query.trim() ? "No business leads match." : "No business leads yet — assign the role on User roles."}
            </div>
          ) : (
            items.map((u, i) => (
              <div key={u.id} role="option" aria-selected={value?.id === u.id} data-idx={i + 1} title={u.email}
                className={"ha-combo-opt" + (highlight === i + 1 ? " is-active" : "") + (value?.id === u.id ? " is-selected" : "")}
                onMouseEnter={() => setHighlight(i + 1)}
                onMouseDown={(e) => { e.preventDefault(); choose(u); }}>
                {u.name || u.email}
                {u.name && u.email && <span style={{ marginLeft: 6, fontSize: 12, color: "#94A3B8", fontWeight: 400 }}>{u.email}</span>}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

/** Debounced search text: returns [input value, setter, committed value]; `onCommit` runs
 * (e.g. reset to page 1) whenever the committed value changes. */
function useDebouncedSearch(onCommit) {
  const [query, setQuery] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const t = setTimeout(() => { setQ(query.trim()); onCommit(); }, 350);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);
  return [query, setQuery, q];
}

/** Fetch `fetcher(params)` whenever `params` (compared as JSON) or `reloadKey` change. */
function usePagedFetch(fetcher, params, reloadKey, errorText) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const key = JSON.stringify(params);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetcher(JSON.parse(key))
      .then((res) => { if (!cancelled) { setData(res); setError(""); } })
      .catch((err) => { if (!cancelled) setError(err?.message || errorText); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [fetcher, key, reloadKey, errorText]);
  return { data, loading, error };
}

/** "Total contacts" drill-down: one row per unique number across job poster /
 *  company HR / recruiter fields, with every source holding it and its job count. */
function PhonesPanel({ reloadKey, exportRef }) {
  const [source, setSource] = useState("");
  const [page, setPage] = useState(1);
  const [query, setQuery, q] = useDebouncedSearch(() => setPage(1));
  const { data, loading, error } = usePagedFetch(
    getPhoneInventory, { source, q, page, page_size: PAGE_SIZE }, reloadKey, "Couldn't load phone numbers.");
  const items = data?.items || [];
  const by = data?.by_source || {};
  const sourceOptions = [
    { value: "", label: "All sources" },
    ...Object.entries(PHONE_SOURCES).map(([value, s]) => ({ value, label: `${s.label} (${by[value] ?? 0})` })),
  ];
  exportRef.current = async () => {
    const all = await fetchAllPages(getPhoneInventory, { source, q });
    return {
      name: `phone-contacts${source ? `-${source}` : ""}-${today()}`, sheet: "Contacts",
      head: ["Phone", "Sources", "Jobs", "Name", "Job title", "Company", "Added"],
      rows: all.map((r) => [r.phone, sourceLabels(r).join(", "), r.jobs ?? "", r.name, r.job_title, r.company, fmtTime(r.added_at)]),
    };
  };

  return (
    <>
      {error && <div className="ha-errbanner">{error}</div>}
      <div className="ha-card ha-filterbar" style={{ padding: "11px 14px", gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
        <Select label="Source" value={source} onChange={(v) => { setSource(v); setPage(1); }} options={sourceOptions} />
        <SearchBox value={query} onChange={setQuery} placeholder="Search number, name, company, job…" label="Search phone numbers" />
      </div>
      <div className="ha-card" style={{ overflow: "hidden" }}>
        <div className="ha-table-scroll">
          <table className="ha-table" style={{ minWidth: 860 }}>
            <thead className="ha-thead">
              <tr>
                <PlainHeader label="Phone" width={170} />
                <PlainHeader label="Source" width={130} />
                <PlainHeader label="Name" width={180} />
                <PlainHeader label="Job / Company" />
                <PlainHeader label="Added" width={160} />
              </tr>
            </thead>
            <tbody>
              {loading && !data && <tr><td className="ha-td" colSpan={5} style={EMPTY_CELL}>Loading phone numbers…</td></tr>}
              {!loading && items.length === 0 && <tr><td className="ha-td" colSpan={5} style={EMPTY_CELL}>No phone numbers match these filters.</td></tr>}
              {items.map((row, i) => {
                const sources = row.sources?.length ? row.sources : [row.source];
                return (
                  <tr key={`${row.source}-${row.job_id || row.recruiter_id}-${i}`} className="ha-row">
                    <td className="ha-td" style={{ whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums", color: C.text }}>
                      {row.phone}
                      {row.jobs > 1 && <div style={SUBLINE} title="Distinct jobs that show this number">on {row.jobs} jobs</div>}
                    </td>
                    <td className="ha-td">
                      <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                        {sources.map((s) => {
                          const src = PHONE_SOURCES[s] || {};
                          return (
                            <span key={s} style={{ padding: "2px 9px", borderRadius: 6, fontSize: 12, fontWeight: 600, color: src.color, background: src.bg, whiteSpace: "nowrap" }}>
                              {src.label || s}
                            </span>
                          );
                        })}
                      </div>
                    </td>
                    <td className="ha-td" style={{ color: C.text }}>{row.name || "—"}</td>
                    <td className="ha-td"><JobCell title={row.job_title || (row.source === "recruiter" ? "" : "—")} company={row.company} /></td>
                    <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>{fmtTime(row.added_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
      <Pager page={page} total={data?.total || 0} loading={loading} setPage={setPage} />
    </>
  );
}

/** "Self-assigned jobs" drill-down: every currently assigned job, called or not. */
function AssignedJobsPanel({ reloadKey, exportRef }) {
  const [page, setPage] = useState(1);
  const [query, setQuery, q] = useDebouncedSearch(() => setPage(1));
  const { data, loading, error } = usePagedFetch(
    getAssignedJobs, { q, page, page_size: PAGE_SIZE }, reloadKey, "Couldn't load self-assigned jobs.");
  const items = data?.items || [];
  exportRef.current = async () => {
    const all = await fetchAllPages(getAssignedJobs, { q });
    return {
      name: `self-assigned-jobs-${today()}`, sheet: "Self-assigned jobs",
      head: ["Job title", "Company", "Assigned to", "Assigned on", "Phone", "Calls", "Last call status", "Last call outcome", "Last call at"],
      rows: all.map((r) => [r.job_title, r.company, r.assigned_to?.name, fmtTime(r.assigned_at), r.phone, r.calls,
        r.last_call ? statusLabel(r.last_call.status) : "Not called yet",
        r.last_call ? outcomeInfo(r.last_call.status, r.last_call.outcome)?.label || "" : "",
        r.last_call ? fmtTime(r.last_call.called_at) : ""]),
    };
  };

  return (
    <>
      {error && <div className="ha-errbanner">{error}</div>}
      <div className="ha-card ha-filterbar" style={{ padding: "11px 14px", gridTemplateColumns: "minmax(0, 1fr)" }}>
        <SearchBox value={query} onChange={setQuery} placeholder="Search job, company, assignee…" label="Search self-assigned jobs" />
      </div>
      <div className="ha-card" style={{ overflow: "hidden" }}>
        <div className="ha-table-scroll">
          <table className="ha-table" style={{ minWidth: 980 }}>
            <thead className="ha-thead">
              <tr>
                <PlainHeader label="Job / Company" width={260} />
                <PlainHeader label="Assigned to" width={160} />
                <PlainHeader label="Assigned on" width={160} />
                <PlainHeader label="Phone" width={150} />
                <PlainHeader label="Calls" align="center" width={70} />
                <PlainHeader label="Last call" />
              </tr>
            </thead>
            <tbody>
              {loading && !data && <tr><td className="ha-td" colSpan={6} style={EMPTY_CELL}>Loading self-assigned jobs…</td></tr>}
              {!loading && items.length === 0 && <tr><td className="ha-td" colSpan={6} style={EMPTY_CELL}>No self-assigned jobs match.</td></tr>}
              {items.map((row) => (
                <tr key={row.job_id} className="ha-row">
                  <td className="ha-td"><JobCell title={row.job_title} company={row.company} /></td>
                  <td className="ha-td"><AssigneePill user={row.assigned_to} /></td>
                  <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>{fmtTime(row.assigned_at)}</td>
                  <td className="ha-td" style={{ whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>{row.phone || "—"}</td>
                  <td className="ha-td" style={{ textAlign: "center", fontWeight: 600, color: row.calls ? C.text : "#94A3B8" }}>{row.calls}</td>
                  <td className="ha-td">
                    {row.last_call ? (
                      <>
                        <StatusPill status={row.last_call.status} />
                        <div style={SUBLINE}>
                          {[outcomeInfo(row.last_call.status, row.last_call.outcome)?.label, fmtTime(row.last_call.called_at),
                            row.last_call.called_by?.name].filter(Boolean).join(" · ")}
                        </div>
                      </>
                    ) : <span style={{ color: "#94A3B8", fontSize: 12.5 }}>Not called yet</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <Pager page={page} total={data?.total || 0} loading={loading} setPage={setPage} />
    </>
  );
}

/**
 * Phone logs page: summary tiles, status / assignment / reminder / date (+ assignee, admins
 * only) filters, a debounced search box and a paged table of logged calls — every call in
 * the tenant for admins, only their own for a Business Lead. Clicking a tile switches
 * the table: call tiles → the call log at that status, "Total phones" → every number held,
 * "Self-assigned jobs" → every assigned job (both drill-downs admin only). The call log is fetched whenever filters,
 * search, page or Refresh change (it also feeds the tiles, so it loads in every view).
 */
export default function PhoneLogsPage() {
  const { isAdmin } = useTenant();
  const [view, setView] = useState("calls");
  // `assignee` is a business lead's user id (admins only; "" = everyone).
  const [filters, setFilters] = useState({ status: "", assignment: "", reminder: "", days: "7", assignee: "" });
  const [assignee, setAssignee] = useState(null); // the picked {id, name, email}, for the picker label
  const [page, setPage] = useState(1);
  const [query, setQuery, q] = useDebouncedSearch(() => setPage(1));
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const [exporting, setExporting] = useState(false);
  const [exportFormat, setExportFormat] = useState("csv");
  const exportRef = useRef(null); // the on-screen list's CSV exporter (set by the active view)

  /** Update one filter and jump back to page 1. */
  const setFilter = (key, value) => { setFilters((f) => ({ ...f, [key]: value })); setPage(1); };
  /** Pick (or clear, with null) the Assignee filter. */
  const pickAssignee = (user) => { setAssignee(user); setFilter("assignee", user ? String(user.id) : ""); };
  // Never send `assignee` for a non-admin: the backend 403s a business lead passing one.
  const logParams = isAdmin ? filters : { ...filters, assignee: "" };
  /** Show the call log filtered to `status` ("" = all). */
  const showCalls = (status) => { setView("calls"); if (filters.status !== status) setFilter("status", status); };

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getPhoneLogs({ ...logParams, q, page, page_size: PAGE_SIZE })
      .then((res) => { if (!cancelled) { setData(res); setError(""); } })
      .catch((err) => { if (!cancelled) setError(err?.message || "Couldn't load phone logs."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // logParams is derived from filters + isAdmin.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters, isAdmin, q, page, reloadKey]);

  const items = data?.items || [];
  const total = data?.total || 0;
  const summary = data?.summary || {
    total: 0, answered: 0, unanswered: 0, assigned_jobs: 0, called_assigned_jobs: 0, total_phones: 0,
  };
  const callsAt = (status) => view === "calls" && filters.status === status;
  // "own" = a business lead's own calls (no tenant-wide drill-downs). Until the first
  // response lands, fall back to the role.
  const ownScope = data?.scope ? data.scope === "own" : !isAdmin;

  if (view === "calls" || ownScope) {
    exportRef.current = async () => {
      const all = await fetchAllPages(getPhoneLogs, { ...logParams, q });
      const days = filters.days === "0" ? "all-time" : `last-${filters.days}-days`;
      const who = assignee && logParams.assignee ? `-${String(assignee.name || assignee.email || assignee.id).toLowerCase().replace(/[^a-z0-9]+/g, "-")}` : "";
      return {
        name: `phone-logs-${filters.status || "all"}${who}-${days}-${today()}`, sheet: "Phone logs",
        head: ["Status", "Call outcome", "Outcome details", "Job title", "Company", "Phone", "POC", "Called by",
          "Assigned to", "Remarks", "Called at", "Reminder", "Reminder time"],
        rows: all.map((r) => [statusLabel(r.status), outcomeInfo(r.status, r.outcome)?.label || "", outcomeDetailText(r.details),
          r.job_title, r.company, r.phone, r.contact_name, r.owner?.name || r.called_by?.name, r.assigned_to?.name || "", r.remarks,
          fmtTime(r.called_at), REMINDERS[r.reminder?.state]?.label || "No reminder",
          r.reminder?.at ? fmtTime(r.reminder.at) : ""]),
      };
    };
  }

  /** Save the list on screen (every page, current filters) in the selected format. */
  const doExport = async () => {
    if (!exportRef.current || exporting) return;
    setExporting(true);
    try {
      const { name, sheet, head, rows } = await exportRef.current();
      if (exportFormat === "json") downloadJson(`${name}.json`, head, rows);
      else if (exportFormat === "excel") saveBlob(`${name}.xlsx`, await exportPhoneLogsXlsx({ sheet, filename: `${name}.xlsx`, head, rows }));
      else downloadCsv(`${name}.csv`, head, rows);
    } catch (err) {
      setError(err?.message || "Couldn't build the export. Try again.");
    } finally {
      setExporting(false);
    }
  };

  return (
    <main className="ha-main">
      <div className="ha-pagehead">
        <div className="ha-pagehead-titles">
          <h1>Phone logs</h1>
          <p className="ha-sub">
            {ownScope
              ? "The calls you've logged, and the jobs you've self-assigned."
              : "Every logged call in this workspace, including jobs self-assigned by others. Visible to admins only."}
          </p>
        </div>
        <div className="ha-pagehead-actions">
          <ExportButton format={exportFormat} onFormatChange={setExportFormat} onExport={doExport} exporting={exporting}
            title="Export the list below, with its filters" />
          <button className="ha-btn ha-btn-secondary" onClick={() => setReloadKey((k) => k + 1)} disabled={loading} title="Refresh">
            <RefreshCw size={16} className={loading ? "ha-spin" : ""} /> Refresh
          </button>
        </div>
      </div>

      <div className="ha-pagebody">
        {error && <div className="ha-errbanner">{error}</div>}

        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          {!ownScope && summary.total_phones != null && (
            <StatCard value={summary.total_phones} label="Total contacts" color="#0891B2" sub="unique numbers"
              onClick={() => setView("phones")} active={view === "phones"} />
          )}
          <StatCard value={summary.total} label="Total calls" color={C.primary}
            onClick={() => showCalls("")} active={callsAt("")} />
          <StatCard value={summary.answered} label="Answered" color="#047857"
            onClick={() => showCalls("answered")} active={callsAt("answered")} />
          <StatCard value={summary.unanswered} label="Unanswered" color="#DC2626"
            onClick={() => showCalls("unanswered")} active={callsAt("unanswered")} />
          <StatCard value={summary.assigned_jobs} label={ownScope ? "My self-assigned jobs" : "Self-assigned jobs"} color="#7C3AED"
            sub={`${summary.called_assigned_jobs ?? 0} called`}
            onClick={ownScope ? undefined : () => setView("assigned")} active={!ownScope && view === "assigned"} />
        </div>

        {!ownScope && view === "phones" && <PhonesPanel reloadKey={reloadKey} exportRef={exportRef} />}
        {!ownScope && view === "assigned" && <AssignedJobsPanel reloadKey={reloadKey} exportRef={exportRef} />}

        {(view === "calls" || ownScope) && (
          <>
            <div className="ha-card ha-filterbar" style={{ padding: "11px 14px", gridTemplateColumns: `repeat(${isAdmin ? 5 : 4}, minmax(0, 1fr))` }}>
              <Select label="Status" value={filters.status} onChange={(v) => setFilter("status", v)} options={STATUS_OPTIONS} />
              <Select label="Assignment" value={filters.assignment} onChange={(v) => setFilter("assignment", v)} options={ASSIGNMENT_OPTIONS} />
              <Select label="Reminder" value={filters.reminder} onChange={(v) => setFilter("reminder", v)} options={REMINDER_OPTIONS} />
              <Select label="Date" value={filters.days} onChange={(v) => setFilter("days", v)} options={DAYS_OPTIONS} />
              {isAdmin && <AssigneeFilter value={assignee} onChange={pickAssignee} />}
              <SearchBox value={query} onChange={setQuery} placeholder="Search job, company, phone, POC…" label="Search phone logs" />
            </div>

            <div className="ha-card" style={{ overflow: "hidden" }}>
              <div className="ha-table-scroll">
                <table className="ha-table" style={{ minWidth: 1320 }}>
                  <thead className="ha-thead">
                    <tr>
                      <PlainHeader label="Status" width={120} />
                      <PlainHeader label="Job / Company" width={230} />
                      <PlainHeader label="Phone" width={140} />
                      <PlainHeader label="POC" width={130} />
                      <PlainHeader label="Call outcome" width={240} />
                      <PlainHeader label="Reminder" width={170} />
                      <PlainHeader label="Assigned to" width={140} />
                      <PlainHeader label="Remarks" />
                      <PlainHeader label="Date" width={160} />
                    </tr>
                  </thead>
                  <tbody>
                    {loading && !data && <tr><td className="ha-td" colSpan={9} style={EMPTY_CELL}>Loading phone logs…</td></tr>}
                    {!loading && items.length === 0 && <tr><td className="ha-td" colSpan={9} style={EMPTY_CELL}>No calls match these filters.</td></tr>}
                    {items.map((row) => (
                      <tr key={row.id} className="ha-row">
                        <td className="ha-td"><StatusPill status={row.status} /></td>
                        <td className="ha-td"><JobCell title={row.job_title} company={row.company} /></td>
                        <td className="ha-td" style={{ whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>{row.phone}</td>
                        <td className="ha-td" style={{ color: C.text }}>{row.contact_name || "—"}</td>
                        <td className="ha-td"><OutcomeCell status={row.status} outcome={row.outcome} details={row.details} /></td>
                        <td className="ha-td"><ReminderCell reminder={row.reminder} /></td>
                        <td className="ha-td"><AssigneePill user={row.assigned_to} /></td>
                        <td className="ha-td" style={{ color: C.textSoft, maxWidth: 260 }}>
                          <div title={row.remarks} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{row.remarks || "—"}</div>
                        </td>
                        <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>
                          {fmtTime(row.called_at)}
                          {!ownScope && (row.owner?.name || row.called_by?.name) && (
                            <div style={SUBLINE} title={row.owner?.email || row.called_by?.email}>by {row.owner?.name || row.called_by?.name}</div>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            <Pager page={page} total={total} loading={loading} setPage={setPage} />
          </>
        )}
      </div>
    </main>
  );
}
