import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  SlidersHorizontal, Download, LayoutList, History, Send, BarChart3,
  Search, Mail, ArrowUpDown, ArrowUp, ArrowDown, Eye, Pencil, RefreshCw, ArrowLeft,
  CheckCircle2, XCircle, Loader2, HelpCircle, Radar, UserSearch, Play, FileJson, FileSpreadsheet,
  AlertTriangle,
} from "lucide-react";
import JobDetailsView from "./JobDetailsView";
import RuleEngineConfig from "./RuleEngineConfig";
import HealthBadge from "./HealthBadge";
import {
  getJobs, getRunHistory, getRunHistoryEntry, getActiveRun, ApiError,
  runLinkedinAgent, getLinkedinResults, getLinkedinResult,
  runNaukriAgent, getNaukriResults, getNaukriResult,
  runDiceAgent, getDiceResults, getDiceResult,
  runProspectIntelligence, getProspectResults, getProspectResult,
  runRecruiterDiscovery,
  downloadJsonUrl, downloadExcelUrl,
} from "./api";

/* Palette: Primary #2563EB · Secondary #1E40AF · Accent #F59E0B · BG #F8FAFC · Text #1E293B */
const C = {
  primary: "#2563EB", secondary: "#1E40AF", accent: "#F59E0B",
  bg: "#F8FAFC", text: "#1E293B", textSoft: "#64748B",
  border: "#E2E8F0", sidebar: "#1E293B", pale: "#EFF6FF",
};

const WhatsAppIcon = ({ size = 16 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
    <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51l-.57-.01c-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 0 1-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 0 1-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 0 1 2.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0 0 12.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 0 0 5.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 0 0-3.48-8.413Z"/>
  </svg>
);
const LinkedInIcon = ({ size = 16 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
    <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.225 0z"/>
  </svg>
);

const ThemeStyles = () => (
  <style>{`
    .ha-root{display:flex;min-height:100vh;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:${C.bg};color:${C.text};}
    .ha-main{flex:1;overflow-x:hidden;}
    .ha-sidebar{display:none;width:240px;flex-shrink:0;flex-direction:column;padding:20px 12px;background:${C.sidebar};}
    @media(min-width:768px){.ha-sidebar{display:flex;}}
    .ha-logo{font-size:18px;font-weight:700;color:#fff;letter-spacing:-.01em;}
    .ha-logo span{color:${C.primary};}
    .ha-tagline{margin-top:4px;font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:#94A3B8;}
    .ha-navhead{padding:0 12px 8px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.14em;color:#64748B;}
    .ha-nav{display:flex;width:100%;align-items:center;gap:12px;border:0;background:transparent;cursor:pointer;border-radius:8px;padding:8px 12px;font-size:14px;color:#CBD5E1;transition:.15s;}
    .ha-nav:hover{background:rgba(255,255,255,.06);color:#fff;}
    .ha-nav-active{background:rgba(37,99,235,.28);color:#fff;box-shadow:inset 0 0 0 1px rgba(37,99,235,.55);}
    .ha-badge{background:${C.accent};color:#1E293B;border-radius:999px;padding:2px 8px;font-size:12px;font-weight:700;}
    .ha-card{background:#fff;border:1px solid ${C.border};border-radius:12px;box-shadow:0 1px 2px rgba(15,23,42,.04);}
    .ha-input{border:1px solid #CBD5E1;background:#fff;color:${C.text};border-radius:8px;font-size:14px;}
    .ha-input:focus{outline:none;border-color:${C.primary};box-shadow:0 0 0 2px rgba(37,99,235,.25);}
    .ha-btn{display:inline-flex;align-items:center;gap:8px;border-radius:8px;padding:8px 16px;font-size:14px;cursor:pointer;transition:.15s;}
    .ha-btn-primary{border:0;font-weight:600;background:${C.primary};color:#fff;box-shadow:0 2px 6px rgba(37,99,235,.35);}
    .ha-btn-primary:hover{background:${C.secondary};}
    .ha-btn-secondary{font-weight:500;background:#fff;border:1px solid #CBD5E1;color:${C.primary};}
    .ha-btn-secondary:hover{background:${C.pale};}
    .ha-btn-secondary:disabled{opacity:.6;cursor:default;}
    .ha-table{width:100%;min-width:1080px;border-collapse:collapse;font-size:14px;table-layout:fixed;}
    .ha-table-scroll{overflow-x:auto;overflow-y:auto;max-height:400px;scrollbar-width:thin;scrollbar-color:#CBD5E1 transparent;}
    .ha-table-scroll::-webkit-scrollbar{height:9px;width:9px;}
    .ha-table-scroll::-webkit-scrollbar-track{background:transparent;}
    .ha-table-scroll::-webkit-scrollbar-thumb{background:#CBD5E1;border-radius:99px;}
    .ha-table-scroll::-webkit-scrollbar-thumb:hover{background:#94A3B8;}
    .ha-thead{background:${C.pale};text-align:left;position:sticky;top:0;z-index:1;}
    .ha-th{padding:12px 16px;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:${C.textSoft};position:sticky;top:0;background:${C.pale};z-index:1;}
    .ha-sortbtn{display:inline-flex;align-items:center;gap:4px;border:0;background:transparent;cursor:pointer;font:inherit;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;}
    .ha-td{padding:14px 16px;vertical-align:middle;}
    .ha-row{border-top:1px solid #EEF2F7;}
    .ha-row:hover{background:#F1F5F9;}
    .ha-link{color:${C.primary};font-weight:600;background:none;border:0;padding:0;cursor:pointer;font-size:inherit;font-family:inherit;}
    .ha-statnum{font-size:30px;font-weight:700;line-height:1;}
    .ha-statlbl{margin-top:8px;font-size:14px;color:${C.textSoft};}
    .ha-select{padding:6px 12px;}
    .ha-pill{display:inline-block;border-radius:6px;padding:2px 10px;font-size:12px;font-weight:600;}
    .ha-act{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:8px;border:1px solid ${C.border};background:#fff;color:${C.textSoft};cursor:pointer;transition:.15s;}
    .ha-act:hover{border-color:${C.primary};color:${C.primary};background:${C.pale};}
    .ha-mail{color:${C.primary};text-decoration:none;}
    .ha-mail:hover{text-decoration:underline;}
    .ha-tel{color:${C.text};text-decoration:none;}
    .ha-tel:hover{text-decoration:underline;}
    .ha-cbtn{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:8px;border:1px solid;transition:.15s;text-decoration:none;}
    .ha-cbtn-on{border-color:${C.primary};color:${C.primary};background:#fff;cursor:pointer;}
    .ha-cbtn-on:hover{background:${C.primary};color:#fff;}
    .ha-cbtn-off{border-color:${C.border};color:#CBD5E1;background:${C.bg};cursor:not-allowed;}
    .ha-spin{animation:ha-rot .9s linear infinite;}
    @keyframes ha-rot{to{transform:rotate(360deg);}}
    .ha-errbanner{background:#FEF2F2;border:1px solid #FCA5A5;color:#B91C1C;border-radius:10px;padding:10px 16px;font-size:13px;font-weight:500;}
    .ha-breakdown{display:flex;flex-wrap:wrap;gap:6px;font-size:12px;color:${C.textSoft};}
    .ha-breakdown b{color:${C.text};}
    .ha-detail-page{padding:24px;max-width:900px;}
    .ha-detail-back{display:inline-flex;align-items:center;gap:7px;cursor:pointer;background:#fff;border:1px solid ${C.border};color:#334155;font-size:13px;font-weight:600;padding:8px 14px;border-radius:8px;margin-bottom:18px;}
    .ha-detail-back:hover{background:#F1F5F9;}
    .ha-detail-card{background:#fff;border:1px solid ${C.border};border-radius:14px;padding:24px;margin-bottom:16px;}
    .ha-detail-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px;margin-top:16px;}
    .ha-detail-stat{background:${C.pale};border-radius:10px;padding:14px 16px;}
    .ha-detail-stat b{display:block;font-size:22px;color:${C.text};}
    .ha-detail-stat span{font-size:12px;color:${C.textSoft};}
  `}</style>
);

const SRC_TONES = {
  Naukri: { background: "#EFF6FF", color: "#1E40AF" },
  Dice: { background: "#FEF3C7", color: "#92400E" },
  LinkedIn: { background: "#E0E7FF", color: "#3730A3" },
};
const SourceChip = ({ source }) => (
  <span className="ha-pill" style={SRC_TONES[source] || { background: "#F1F5F9", color: "#475569" }}>{source}</span>
);

const STATUS_TONES = {
  success:    { background: "#ECFDF5", color: "#047857", Icon: CheckCircle2 },
  no_results: { background: "#F1F5F9", color: "#475569", Icon: HelpCircle },
  failed:     { background: "#FEF2F2", color: "#B91C1C", Icon: XCircle },
  running:    { background: "#FFFBEB", color: "#B45309", Icon: Loader2 },
};
const StatusPill = ({ status }) => {
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
const FilterStatusBadge = ({ passed, reason }) => {
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

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/* ── API job → table row shape ─────────────────────────────────────────── */
function mapApiJob(j) {
  return {
    id: j.id,
    title: j.job_title || "Untitled role",
    company: j.company || "—",
    poc: j.job_poster_name || null,
    postedDate: j.posted_date || "",
    email: j.email_id || null,
    whatsapp: j.contact_number || null,
    mobile: j.contact_number || null,
    linkedin: j.linkedin_profile_url || null,
    source: j.source || "—",
    jobDescription: j.job_description || "",
    location: j.location || "",
    salary: j.salary || "",
    jobType: j.job_type || "",
    workMode: j.work_mode || "",
    applyLink: j.job_url || "",
    companyUrl: j.company_url || "",
    posterTitle: j.job_poster_designation || "",
    domain: j.domain || "",
    hiringEntity: j.hiring_entity || "",
    isGcc: !!j.is_gcc,
    // Business-filter annotation — the backend no longer drops jobs, it flags
    // them. passed_filter defaults to true for legacy rows lacking the column.
    passedFilter: j.passed_filter !== false,
    filterReason: j.filter_reason || "",
  };
}

function mapJobToDetail(j) {
  return {
    jobTitle: j.title,
    jd: j.jobDescription,
    company: j.company,
    location: j.location,
    jobType: j.jobType,
    salary: j.salary,
    postedDate: j.postedDate,
    applyLink: j.applyLink,
    companyUrl: j.companyUrl || "",
    posterName: j.poc || "",
    posterLinkedIn: j.linkedin || "",
    posterTitle: j.posterTitle || "",
    posterContact: { email: j.email || "", mobile: j.mobile || "" },
    source: j.source,
    domain: j.domain || "",
    hiringEntity: j.hiringEntity || "",
    passedFilter: j.passedFilter,
    filterReason: j.filterReason || "",
  };
}

function mapRun(entry) {
  return {
    runId: entry.run_id,
    sources: entry.sources || [],
    status: entry.status || "no_results",
    startedAt: entry.started_at || "",
    completedAt: entry.completed_at || "",
    jobsFound: entry.jobs_found ?? 0,
    verifiedJobs: entry.verified_jobs ?? 0,
    directClients: entry.direct_clients ?? 0,
    gcc: entry.gcc ?? 0,
    staffingFirms: entry.staffing_firms ?? 0,
    ambiguous: entry.ambiguous ?? 0,
  };
}

const StatCard = ({ value, label, color }) => (
  <div className="ha-card" style={{ flex: 1, minWidth: 160, padding: "16px 20px" }}>
    <div className="ha-statnum" style={{ color }}>{value}</div>
    <div className="ha-statlbl">{label}</div>
  </div>
);

function SortHeader({ label, col, sort, setSort, width }) {
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
const PlainHeader = ({ label, align = "left", width }) => (
  <th className="ha-th" style={{ textAlign: align, ...(width ? { width, minWidth: width } : null) }}>{label}</th>
);

function Select({ label, value, onChange, options }) {
  return (
    <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 14, color: C.textSoft }}>
      <span style={{ fontWeight: 500 }}>{label}</span>
      <select className="ha-input ha-select" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  );
}

function ContactIcon({ glyph: Glyph, href, title }) {
  if (href) {
    return (
      <a className="ha-cbtn ha-cbtn-on" href={href} target="_blank" rel="noreferrer" title={title}>
        <Glyph size={16} />
      </a>
    );
  }
  return (
    <span className="ha-cbtn ha-cbtn-off" title={title + " not available"} aria-disabled="true">
      <Glyph size={16} />
    </span>
  );
}

/* Sidebar */
function NavItem({ glyph: Glyph, children, active, badge, onClick }) {
  return (
    <button className={"ha-nav" + (active ? " ha-nav-active" : "")} onClick={onClick}>
      <Glyph size={18} />
      <span style={{ flex: 1, textAlign: "left" }}>{children}</span>
      {badge != null && <span className="ha-badge">{badge}</span>}
    </button>
  );
}
function Sidebar({ activePage, onNavigate, jobsCount, runsCount }) {
  return (
    <aside className="ha-sidebar">
      <div style={{ padding: "0 12px" }}>
        <div className="ha-logo">Harvest<span>Agent</span></div>
        <div className="ha-tagline">Contract Sourcing Automation</div>
      </div>
      <nav style={{ marginTop: 32, display: "flex", flexDirection: "column", gap: 24, flex: 1 }}>
        <div><div className="ha-navhead">Configuration</div>
          <NavItem glyph={SlidersHorizontal} active={activePage === "rules"} onClick={() => onNavigate("rules")}>Rule Engine</NavItem></div>
        <div><div className="ha-navhead">Operations</div>
          <NavItem glyph={LayoutList} active={activePage === "jobs"} badge={jobsCount} onClick={() => onNavigate("jobs")}>Harvested Jobs</NavItem>
          <NavItem glyph={History} active={activePage === "history"} badge={runsCount} onClick={() => onNavigate("history")}>Run History</NavItem>
          <NavItem glyph={Radar} active={activePage === "sources"} onClick={() => onNavigate("sources")}>Source Runs</NavItem>
          <NavItem glyph={UserSearch} active={activePage === "leads"} onClick={() => onNavigate("leads")}>Lead Intelligence</NavItem>
          <NavItem glyph={Send}>Outreach</NavItem></div>
        <div><div className="ha-navhead">Reports</div>
          <NavItem glyph={BarChart3}>Analytics</NavItem></div>
      </nav>
      <div style={{ padding: "0 12px" }}><HealthBadge /></div>
    </aside>
  );
}

/* Run detail page */
function RunDetailView({ runId, onBack }) {
  const [entry, setEntry] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getRunHistoryEntry(runId)
      .then((res) => { if (!cancelled) setEntry(mapRun(res)); })
      .catch((err) => { if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not load this run."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [runId]);

  return (
    <div className="ha-root">
      <ThemeStyles />
      <main className="ha-main">
        <div className="ha-detail-page">
          <button className="ha-detail-back" onClick={onBack}><ArrowLeft size={16} /> Back to Run History</button>
          {loading && <div className="ha-detail-card">Loading run {runId}…</div>}
          {error && <div className="ha-errbanner">{error}</div>}
          {entry && !loading && (
            <>
              <div className="ha-detail-card">
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
                  <div>
                    <div style={{ fontSize: 12, color: C.textSoft, fontWeight: 600, letterSpacing: ".04em", textTransform: "uppercase" }}>Run ID</div>
                    <div style={{ fontSize: 20, fontWeight: 700 }}>{entry.runId}</div>
                  </div>
                  <StatusPill status={entry.status} />
                </div>
                <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
                  {entry.sources.map((s) => <SourceChip key={s} source={s} />)}
                </div>
                <div style={{ display: "flex", gap: 24, marginTop: 16, fontSize: 13.5, color: C.textSoft, flexWrap: "wrap" }}>
                  <span>Started: <b style={{ color: C.text }}>{fmtDate(entry.startedAt)}</b></span>
                  <span>Completed: <b style={{ color: C.text }}>{fmtDate(entry.completedAt)}</b></span>
                </div>
              </div>
              <div className="ha-detail-card">
                <div style={{ fontSize: 12, color: C.textSoft, fontWeight: 700, letterSpacing: ".05em", textTransform: "uppercase" }}>Results</div>
                <div className="ha-detail-grid">
                  <div className="ha-detail-stat"><b>{entry.jobsFound}</b><span>Jobs found</span></div>
                  <div className="ha-detail-stat"><b>{entry.verifiedJobs}</b><span>Verified</span></div>
                  <div className="ha-detail-stat"><b>{entry.directClients}</b><span>Direct clients</span></div>
                  <div className="ha-detail-stat"><b>{entry.gcc}</b><span>GCC</span></div>
                  <div className="ha-detail-stat"><b>{entry.staffingFirms}</b><span>Staffing firms</span></div>
                  <div className="ha-detail-stat"><b>{entry.ambiguous}</b><span>Ambiguous</span></div>
                </div>
              </div>
            </>
          )}
        </div>
      </main>
    </div>
  );
}

/* ── Harvested Jobs page ─────────────────────────────────────────────── */
function JobsPage({ jobs, total, loading, error, onRefresh, onNavigate, onView }) {
  const [filters, setFilters] = useState({ company: "all", contact: "all", job: "all", poc: "all", status: "all" });
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState({ col: "posted", dir: "desc" });

  const counts = useMemo(() => ({
    all: jobs.length,
    email: jobs.filter((j) => j.email).length,
    whatsapp: jobs.filter((j) => j.whatsapp).length,
    linkedin: jobs.filter((j) => j.linkedin).length,
    companies: new Set(jobs.map((j) => j.company)).size,
    pocs: jobs.filter((j) => j.poc).length,
    qualified: jobs.filter((j) => j.passedFilter).length,
    flagged: jobs.filter((j) => !j.passedFilter).length,
  }), [jobs]);

  const companies = useMemo(() => Array.from(new Set(jobs.map((j) => j.company))).sort(), [jobs]);
  const jobTitles = useMemo(() => Array.from(new Set(jobs.map((j) => j.title))).sort(), [jobs]);
  const pocNames = useMemo(() => Array.from(new Set(jobs.filter((j) => j.poc).map((j) => j.poc))).sort(), [jobs]);

  const filtered = useMemo(() => {
    let rows = jobs.filter((j) => {
      if (filters.company !== "all" && j.company !== filters.company) return false;
      if (filters.job !== "all" && j.title !== filters.job) return false;
      if (filters.poc !== "all" && j.poc !== filters.poc) return false;
      if (filters.contact === "email" && !j.email) return false;
      if (filters.contact === "mobile" && !j.mobile) return false;
      if (filters.contact === "linkedin" && !j.linkedin) return false;
      if (filters.status === "qualified" && !j.passedFilter) return false;
      if (filters.status === "flagged" && j.passedFilter) return false;
      if (startDate && j.postedDate.slice(0, 10) < startDate) return false;
      if (endDate && j.postedDate.slice(0, 10) > endDate) return false;
      if (query.trim()) {
        const q = query.toLowerCase();
        if (!((j.title + " " + j.company + " " + j.source + " " + (j.poc || "") + " " + (j.email || "") + " " + (j.mobile || "")).toLowerCase().includes(q))) return false;
      }
      return true;
    });
    const dir = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      switch (sort.col) {
        case "title": return a.title.localeCompare(b.title) * dir;
        case "company": return a.company.localeCompare(b.company) * dir;
        case "poc": return (a.poc || "").localeCompare(b.poc || "") * dir;
        case "source": return a.source.localeCompare(b.source) * dir;
        case "posted": return a.postedDate.localeCompare(b.postedDate) * dir;
        default: return 0;
      }
    });
  }, [jobs, filters, startDate, endDate, query, sort]);

  function exportCsv() {
    const header = ["Job title", "Company", "Source", "Filter status", "Filter reason", "POC", "Posted date", "Email", "Mobile", "WhatsApp", "LinkedIn"];
    const lines = filtered.map((j) =>
      [j.title, j.company, j.source, j.passedFilter ? "Qualified" : "Flagged", j.filterReason || "—", j.poc || "—", j.postedDate || "—", j.email || "—", j.mobile || "—", j.whatsapp || "—", j.linkedin || "—"]
        .map((c) => '"' + String(c).replace(/"/g, '""') + '"').join(","));
    const blob = new Blob([[header.join(","), ...lines].join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "harvested-jobs.csv"; a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <main className="ha-main">
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", justifyContent: "space-between", gap: 16, padding: "24px 24px 0" }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, color: C.text }}>Harvested Jobs</h1>
          <p style={{ margin: "4px 0 0", fontSize: 14, color: C.textSoft }}>
            {loading ? "Loading…" : `${total} harvested posting${total === 1 ? "" : "s"} · ${counts.qualified} qualified · ${counts.flagged} flagged`}
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button className="ha-btn ha-btn-secondary" onClick={onRefresh} disabled={loading} title="Refresh">
            <RefreshCw size={16} className={loading ? "ha-spin" : ""} /> Refresh
          </button>
          <button className="ha-btn ha-btn-secondary" onClick={() => onNavigate("rules")}><SlidersHorizontal size={16} /> Rule Engine</button>
          <a className="ha-btn ha-btn-secondary" href={downloadJsonUrl()} title="GET /download/json — latest combined harvest JSON">
            <FileJson size={16} /> JSON
          </a>
          <a className="ha-btn ha-btn-secondary" href={downloadExcelUrl()} title="GET /download/excel — latest combined harvest Excel">
            <FileSpreadsheet size={16} /> Excel
          </a>
          <button className="ha-btn ha-btn-primary" onClick={exportCsv}><Download size={16} /> Export CSV</button>
        </div>
      </div>
      <div style={{ marginTop: 20, borderBottom: "1px solid " + C.border }} />

      <div style={{ display: "flex", flexDirection: "column", gap: 16, padding: 24 }}>
        {error && <div className="ha-errbanner">{error}</div>}

        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 14, fontWeight: 600, color: C.text }}>Posted between</span>
          <input type="date" className="ha-input" style={{ padding: "7px 10px" }} value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          <span style={{ color: C.textSoft }}>to</span>
          <input type="date" className="ha-input" style={{ padding: "7px 10px" }} value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          {(startDate || endDate) && (
            <button className="ha-btn ha-btn-secondary" style={{ padding: "6px 12px" }} onClick={() => { setStartDate(""); setEndDate(""); }}>Clear dates</button>
          )}
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 16 }}>
          <StatCard value={counts.all} label="Total harvested" color={C.accent} />
          <StatCard value={counts.companies} label="Companies sourced" color={C.primary} />
          <StatCard value={counts.pocs} label="POCs identified" color={C.primary} />
        </div>

        <div className="ha-card" style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 16, padding: "16px 20px" }}>
          <Select label="Company" value={filters.company} onChange={(v) => setFilters((f) => ({ ...f, company: v }))}
            options={[{ value: "all", label: "All" }, ...companies.map((c) => ({ value: c, label: c }))]} />
          <Select label="Contact" value={filters.contact} onChange={(v) => setFilters((f) => ({ ...f, contact: v }))}
            options={[{ value: "all", label: "All" }, { value: "email", label: "Email" }, { value: "mobile", label: "Mobile" }, { value: "linkedin", label: "LinkedIn" }]} />
          <Select label="Job" value={filters.job} onChange={(v) => setFilters((f) => ({ ...f, job: v }))}
            options={[{ value: "all", label: "All" }, ...jobTitles.map((t) => ({ value: t, label: t }))]} />
          <Select label="POC" value={filters.poc} onChange={(v) => setFilters((f) => ({ ...f, poc: v }))}
            options={[{ value: "all", label: "All" }, ...pocNames.map((p) => ({ value: p, label: p }))]} />
          <Select label="Filter status" value={filters.status} onChange={(v) => setFilters((f) => ({ ...f, status: v }))}
            options={[{ value: "all", label: "All" }, { value: "qualified", label: `Qualified (${counts.qualified})` }, { value: "flagged", label: `Flagged (${counts.flagged})` }]} />
          <div style={{ position: "relative", marginLeft: "auto", minWidth: 220, flex: 1 }}>
            <span style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8", display: "flex" }}>
              <Search size={16} />
            </span>
            <input className="ha-input" style={{ width: "100%", padding: "8px 12px 8px 34px" }} value={query}
              onChange={(e) => setQuery(e.target.value)} placeholder="Search…" />
          </div>
        </div>

        <div className="ha-card" style={{ overflow: "hidden" }}>
          <div className="ha-table-scroll">
            <table className="ha-table" style={{ minWidth: 1340 }}>
              <thead className="ha-thead">
                <tr>
                  <SortHeader label="Job title" col="title" sort={sort} setSort={setSort} width={230} />
                  <SortHeader label="Company" col="company" sort={sort} setSort={setSort} width={170} />
                  <SortHeader label="Source" col="source" sort={sort} setSort={setSort} width={100} />
                  <PlainHeader label="Filter status" align="center" width={120} />
                  <SortHeader label="POC" col="poc" sort={sort} setSort={setSort} width={150} />
                  <SortHeader label="Posted date" col="posted" sort={sort} setSort={setSort} width={130} />
                  <PlainHeader label="Email" width={200} />
                  <PlainHeader label="Mobile" width={140} />
                  <PlainHeader label="Contact" align="center" width={130} />
                  <PlainHeader label="Action" align="center" width={90} />
                </tr>
              </thead>
              <tbody>
                {loading && (
                  <tr><td className="ha-td" colSpan={10} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                    Loading harvested jobs…
                  </td></tr>
                )}
                {!loading && filtered.map((j) => (
                  <tr key={j.id} className="ha-row">
                    <td className="ha-td"><span className="ha-link">{j.title}</span></td>
                    <td className="ha-td" style={{ color: C.text }}>{j.company}</td>
                    <td className="ha-td"><SourceChip source={j.source} /></td>
                    <td className="ha-td" style={{ textAlign: "center" }}>
                      <FilterStatusBadge passed={j.passedFilter} reason={j.filterReason} />
                    </td>
                    <td className="ha-td" style={{ color: C.text }}>
                      {j.poc || <span style={{ color: "#94A3B8" }}>—</span>}
                    </td>
                    <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>{j.postedDate || "—"}</td>
                    <td className="ha-td">
                      {j.email
                        ? <a className="ha-mail" href={"mailto:" + j.email}>{j.email}</a>
                        : <span style={{ color: "#94A3B8" }}>—</span>}
                    </td>
                    <td className="ha-td" style={{ whiteSpace: "nowrap" }}>
                      {j.mobile
                        ? <a className="ha-tel" href={"tel:" + j.mobile}>{j.mobile}</a>
                        : <span style={{ color: "#94A3B8" }}>—</span>}
                    </td>
                    <td className="ha-td">
                      <div style={{ display: "flex", gap: 8, justifyContent: "center" }}>
                        <ContactIcon glyph={WhatsAppIcon} title="WhatsApp"
                          href={j.whatsapp ? "https://wa.me/" + j.whatsapp.replace(/[^0-9]/g, "") : null} />
                        <ContactIcon glyph={Mail} title="Email"
                          href={j.email ? "mailto:" + j.email : null} />
                        <ContactIcon glyph={LinkedInIcon} title="LinkedIn" href={j.linkedin || null} />
                      </div>
                    </td>
                    <td className="ha-td">
                      <div style={{ display: "flex", gap: 8, justifyContent: "center" }}>
                        <button className="ha-act" title="View" onClick={() => onView({ mode: "view", job: mapJobToDetail(j) })}><Eye size={16} /></button>
                        <button className="ha-act" title="Edit" onClick={() => onView({ mode: "edit", job: mapJobToDetail(j) })}><Pencil size={16} /></button>
                      </div>
                    </td>
                  </tr>
                ))}
                {!loading && !error && filtered.length === 0 && (
                  <tr><td className="ha-td" colSpan={10} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                    No postings match your search. Run a harvest from the Rule Engine to collect jobs.
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", padding: "12px 16px", fontSize: 12, borderTop: "1px solid #EEF2F7", color: C.textSoft }}>
            <span>Showing {filtered.length} of {jobs.length} loaded postings · {counts.qualified} qualified · {counts.flagged} flagged</span>
            <span>{counts.email} email · {counts.whatsapp} WhatsApp · {counts.linkedin} LinkedIn</span>
          </div>
        </div>
      </div>
    </main>
  );
}

/* ── Run History page ────────────────────────────────────────────────── */
function RunHistoryPage({ runs, loading, error, onRefresh, onNavigate, onView }) {
  const [filters, setFilters] = useState({ source: "all", status: "all" });
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState({ col: "started", dir: "desc" });

  const counts = useMemo(() => ({
    totalRuns: runs.length,
    totalJobs: runs.reduce((sum, r) => sum + r.jobsFound, 0),
    directClients: runs.reduce((sum, r) => sum + r.directClients, 0),
  }), [runs]);

  const filtered = useMemo(() => {
    let rows = runs.filter((r) => {
      if (filters.source !== "all" && !r.sources.some((s) => s.toLowerCase() === filters.source)) return false;
      if (filters.status !== "all" && r.status !== filters.status) return false;
      if (startDate && r.startedAt.slice(0, 10) < startDate) return false;
      if (endDate && r.startedAt.slice(0, 10) > endDate) return false;
      if (query.trim() && !r.runId.toLowerCase().includes(query.trim().toLowerCase())) return false;
      return true;
    });
    const dir = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      switch (sort.col) {
        case "runId": return a.runId.localeCompare(b.runId) * dir;
        case "status": return a.status.localeCompare(b.status) * dir;
        case "started": return a.startedAt.localeCompare(b.startedAt) * dir;
        case "completed": return a.completedAt.localeCompare(b.completedAt) * dir;
        case "jobsFound": return (a.jobsFound - b.jobsFound) * dir;
        default: return 0;
      }
    });
  }, [runs, filters, startDate, endDate, query, sort]);

  function exportCsv() {
    const header = ["Run ID", "Sources", "Status", "Started", "Completed", "Jobs found", "Verified", "Direct clients", "GCC", "Staffing firms", "Ambiguous"];
    const lines = filtered.map((r) =>
      [r.runId, r.sources.join("|"), r.status, r.startedAt, r.completedAt, r.jobsFound, r.verifiedJobs, r.directClients, r.gcc, r.staffingFirms, r.ambiguous]
        .map((c) => '"' + String(c).replace(/"/g, '""') + '"').join(","));
    const blob = new Blob([[header.join(","), ...lines].join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "run-history.csv"; a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <main className="ha-main">
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", justifyContent: "space-between", gap: 16, padding: "24px 24px 0" }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, color: C.text }}>Run History</h1>
          <p style={{ margin: "4px 0 0", fontSize: 14, color: C.textSoft }}>
            {loading ? "Loading…" : `${counts.totalRuns} run${counts.totalRuns === 1 ? "" : "s"} · ${counts.totalJobs} jobs harvested`}
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button className="ha-btn ha-btn-secondary" onClick={onRefresh} disabled={loading} title="Refresh">
            <RefreshCw size={16} className={loading ? "ha-spin" : ""} /> Refresh
          </button>
          <button className="ha-btn ha-btn-secondary" onClick={() => onNavigate("rules")}><SlidersHorizontal size={16} /> Rule Engine</button>
          <button className="ha-btn ha-btn-primary" onClick={exportCsv}><Download size={16} /> Export CSV</button>
        </div>
      </div>
      <div style={{ marginTop: 20, borderBottom: "1px solid " + C.border }} />

      <div style={{ display: "flex", flexDirection: "column", gap: 16, padding: 24 }}>
        {error && <div className="ha-errbanner">{error}</div>}

        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 14, fontWeight: 600, color: C.text }}>Started between</span>
          <input type="date" className="ha-input" style={{ padding: "7px 10px" }} value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          <span style={{ color: C.textSoft }}>to</span>
          <input type="date" className="ha-input" style={{ padding: "7px 10px" }} value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          {(startDate || endDate) && (
            <button className="ha-btn ha-btn-secondary" style={{ padding: "6px 12px" }} onClick={() => { setStartDate(""); setEndDate(""); }}>Clear dates</button>
          )}
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 16 }}>
          <StatCard value={counts.totalRuns} label="Total runs" color={C.accent} />
          <StatCard value={counts.totalJobs} label="Jobs harvested" color={C.primary} />
          <StatCard value={counts.directClients} label="Direct clients" color={C.primary} />
        </div>

        <div className="ha-card" style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 16, padding: "16px 20px" }}>
          <Select label="Source" value={filters.source} onChange={(v) => setFilters((f) => ({ ...f, source: v }))}
            options={[{ value: "all", label: "All" }, { value: "linkedin", label: "LinkedIn" }, { value: "naukri", label: "Naukri" }, { value: "dice", label: "Dice" }]} />
          <Select label="Status" value={filters.status} onChange={(v) => setFilters((f) => ({ ...f, status: v }))}
            options={[{ value: "all", label: "All" }, { value: "success", label: "Success" }, { value: "no_results", label: "No results" }, { value: "failed", label: "Failed" }, { value: "running", label: "Running" }]} />
          <div style={{ position: "relative", marginLeft: "auto", minWidth: 220, flex: 1 }}>
            <span style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8", display: "flex" }}>
              <Search size={16} />
            </span>
            <input className="ha-input" style={{ width: "100%", padding: "8px 12px 8px 34px" }} value={query}
              onChange={(e) => setQuery(e.target.value)} placeholder="Search run ID…" />
          </div>
        </div>

        <div className="ha-card" style={{ overflow: "hidden" }}>
          <div className="ha-table-scroll">
            <table className="ha-table" style={{ minWidth: 1190,}}>
              <thead className="ha-thead">
                <tr>
                  <SortHeader label="Run ID" col="runId" sort={sort} setSort={setSort} width={190} />
                  <PlainHeader label="Sources" width={170} />
                  <SortHeader label="Status" col="status" sort={sort} setSort={setSort} width={120} />
                  <SortHeader label="Started" col="started" sort={sort} setSort={setSort} width={150} />
                  <SortHeader label="Completed" col="completed" sort={sort} setSort={setSort} width={150} />
                  <SortHeader label="Jobs found" col="jobsFound" sort={sort} setSort={setSort} width={100} />
                  <PlainHeader label="Breakdown" width={220} />
                  <PlainHeader label="Action" align="center" width={90} />
                </tr>
              </thead>
              <tbody>
                {loading && (
                  <tr><td className="ha-td" colSpan={8} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                    Loading run history…
                  </td></tr>
                )}
                {!loading && filtered.map((r) => (
                  <tr key={r.runId} className="ha-row">
                    <td className="ha-td"><button className="ha-link" onClick={() => onView(r.runId)}>{r.runId}</button></td>
                    <td className="ha-td">
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                        {r.sources.map((s) => <SourceChip key={s} source={s} />)}
                      </div>
                    </td>
                    <td className="ha-td"><StatusPill status={r.status} /></td>
                    <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>{fmtDate(r.startedAt)}</td>
                    <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>{fmtDate(r.completedAt)}</td>
                    <td className="ha-td" style={{ fontWeight: 600 }}>{r.jobsFound}</td>
                    <td className="ha-td">
                      <div className="ha-breakdown">
                        <span><b>{r.directClients}</b> DC</span>
                        <span><b>{r.gcc}</b> GCC</span>
                        <span><b>{r.staffingFirms}</b> SF</span>
                        <span><b>{r.ambiguous}</b> Amb</span>
                      </div>
                    </td>
                    <td className="ha-td">
                      <div style={{ display: "flex", gap: 8, justifyContent: "center" }}>
                        <button className="ha-act" title="View" onClick={() => onView(r.runId)}><Eye size={16} /></button>
                      </div>
                    </td>
                  </tr>
                ))}
                {!loading && !error && filtered.length === 0 && (
                  <tr><td className="ha-td" colSpan={8} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                    No runs match your search. Trigger a harvest from the Rule Engine to see history here.
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", padding: "12px 16px", fontSize: 12, borderTop: "1px solid #EEF2F7", color: C.textSoft }}>
            <span>Showing {filtered.length} of {runs.length} loaded runs</span>
          </div>
        </div>
      </div>
    </main>
  );
}

/* ── Source Runs page (single-source trigger + results, per source) ─────── */
const SOURCE_TABS = [
  { key: "linkedin", label: "LinkedIn", run: runLinkedinAgent, list: getLinkedinResults, one: getLinkedinResult },
  { key: "naukri",   label: "Naukri",   run: runNaukriAgent,   list: getNaukriResults,   one: getNaukriResult },
  { key: "dice",     label: "Dice",     run: runDiceAgent,     list: getDiceResults,     one: getDiceResult },
];

function SourceRunsPage({ harvestRunning, setHarvestRunning }) {
  const [tab, setTab] = useState("linkedin");
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [runMessage, setRunMessage] = useState("");
  const [viewing, setViewing] = useState(null); // { runId, jobs } | null

  const current = SOURCE_TABS.find((t) => t.key === tab);

  const fetchResults = useCallback(async (t) => {
    setLoading(true);
    setError("");
    try {
      const res = await t.list();
      setResults(res.results || []);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the harvest backend.");
      setResults([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { setViewing(null); fetchResults(current); }, [tab]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleRun = async () => {
    if (harvestRunning) {
      setRunMessage("Another harvest is already running (Rule Engine or another source tab) — wait for it to finish. Running two at once collides on the shared browser profile and both fail.");
      return;
    }
    setRunning(true);
    setHarvestRunning(true);
    setRunMessage(`Running ${current.label} harvest — this calls a synchronous endpoint and can take several minutes. Don't close this tab.`);
    let conflict = false;
    try {
      const res = await current.run();
      if (res.status === "failed") {
        setRunMessage(`Failed: ${res.reason || res.message}`);
      } else {
        setRunMessage(`${res.status === "success" ? "Success" : "No results"} — ${res.total_found ?? 0} jobs found (run_id: ${res.run_id}).`);
        fetchResults(current);
      }
    } catch (err) {
      // 409 = a harvest is already running — keep controls frozen.
      conflict = err instanceof ApiError && err.status === 409;
      setRunMessage(err instanceof ApiError ? `Failed: ${err.message}` : "Could not reach the harvest backend.");
    } finally {
      setRunning(false);
      setHarvestRunning(conflict);
    }
  };

  const handleView = async (runId) => {
    setViewing({ runId, jobs: null, loading: true });
    try {
      const res = await current.one(runId);
      setViewing({ runId, jobs: res.jobs || [], loading: false });
    } catch (err) {
      setViewing({ runId, jobs: [], loading: false, error: err instanceof ApiError ? err.message : "Could not load this run." });
    }
  };

  return (
    <main className="ha-main">
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", justifyContent: "space-between", gap: 16, padding: "24px 24px 0" }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, color: C.text }}>Source Runs</h1>
          <p style={{ margin: "4px 0 0", fontSize: 14, color: C.textSoft }}>
            Trigger a single-source harvest (POST /run-{tab}-agent) and browse its saved results.
          </p>
        </div>
        <button className="ha-btn ha-btn-primary" onClick={handleRun} disabled={running || harvestRunning}
          style={harvestRunning && !running ? { background: "#94A3B8", borderColor: "#94A3B8" } : undefined}
          title={harvestRunning && !running ? "A harvest is already running — controls locked until it finishes" : undefined}>
          {(running || harvestRunning) ? <Loader2 size={16} className="ha-spin" /> : <Play size={16} />}
          {running ? "Running…" : harvestRunning ? "Running elsewhere…" : `Run ${current.label} Only`}
        </button>
      </div>
      <div style={{ marginTop: 20, borderBottom: "1px solid " + C.border }} />

      <div style={{ display: "flex", flexDirection: "column", gap: 16, padding: 24 }}>
        <div style={{ display: "flex", gap: 8 }}>
          {SOURCE_TABS.map((t) => (
            <button key={t.key} className="ha-btn" style={{
              background: tab === t.key ? C.primary : "#fff",
              color: tab === t.key ? "#fff" : C.textSoft,
              border: "1px solid " + (tab === t.key ? C.primary : C.border),
            }} onClick={() => setTab(t.key)}>{t.label}</button>
          ))}
        </div>

        {runMessage && <div className="ha-card" style={{ padding: "12px 16px", fontSize: 13.5, color: C.text }}>{runMessage}</div>}
        {error && <div className="ha-errbanner">{error}</div>}

        {viewing ? (
          <div className="ha-card" style={{ overflow: "hidden" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "14px 16px", borderBottom: "1px solid #EEF2F7" }}>
              <b>{viewing.runId}</b>
              <button className="ha-btn ha-btn-secondary" onClick={() => setViewing(null)}>Back to results</button>
            </div>
            {viewing.loading && <div style={{ padding: 24, textAlign: "center", color: "#94A3B8" }}>Loading…</div>}
            {viewing.error && <div className="ha-errbanner" style={{ margin: 16 }}>{viewing.error}</div>}
            {!viewing.loading && !viewing.error && (
              <div className="ha-table-scroll">
                <table className="ha-table" style={{ minWidth: 900 }}>
                  <thead className="ha-thead"><tr>
                    <PlainHeader label="Job title" width={320} /><PlainHeader label="Company" width={220} />
                    <PlainHeader label="Location" width={210} /><PlainHeader label="Posted" width={150} />
                  </tr></thead>
                  <tbody>
                    {viewing.jobs.map((j, i) => (
                      <tr key={i} className="ha-row">
                        <td className="ha-td">{j.job_title}</td>
                        <td className="ha-td">{j.company}</td>
                        <td className="ha-td">{j.location}</td>
                        <td className="ha-td">{j.posted_date}</td>
                      </tr>
                    ))}
                    {viewing.jobs.length === 0 && (
                      <tr><td className="ha-td" colSpan={4} style={{ textAlign: "center", padding: 24, color: "#94A3B8" }}>No jobs in this run.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ) : (
          <div className="ha-card" style={{ overflow: "hidden" }}>
            <div className="ha-table-scroll">
              <table className="ha-table" style={{ minWidth: 1080 }}>
                <thead className="ha-thead"><tr>
                  <PlainHeader label="Run ID" width={190} /><PlainHeader label="Status" width={120} /><PlainHeader label="Executed" width={160} />
                  <PlainHeader label="Total found" width={110} /><PlainHeader label="Keyword" width={150} /><PlainHeader label="Location" width={150} />
                  <PlainHeader label="Action" align="center" width={90} />
                </tr></thead>
                <tbody>
                  {loading && <tr><td className="ha-td" colSpan={7} style={{ textAlign: "center", padding: 24, color: "#94A3B8" }}>Loading…</td></tr>}
                  {!loading && results.map((r) => (
                    <tr key={r.run_id} className="ha-row">
                      <td className="ha-td">{r.run_id}</td>
                      <td className="ha-td"><StatusPill status={r.status} /></td>
                      <td className="ha-td" style={{ color: C.textSoft }}>{fmtDate(r.executed_at)}</td>
                      <td className="ha-td" style={{ fontWeight: 600 }}>{r.total_found}</td>
                      <td className="ha-td">{r.keyword}</td>
                      <td className="ha-td">{r.location}</td>
                      <td className="ha-td"><div style={{ display: "flex", justifyContent: "center" }}>
                        <button className="ha-act" title="View" onClick={() => handleView(r.run_id)}><Eye size={16} /></button>
                      </div></td>
                    </tr>
                  ))}
                  {!loading && !error && results.length === 0 && (
                    <tr><td className="ha-td" colSpan={7} style={{ textAlign: "center", padding: 24, color: "#94A3B8" }}>
                      No {current.label} runs yet — click "Run {current.label} Only" above.
                    </td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </main>
  );
}

/* ── Lead Intelligence page (Prospect Intelligence + Recruiter Discovery) ── */
function LeadIntelligencePage() {
  const [inputFile, setInputFile] = useState("data/prospects/input/prospects.xlsx");
  const [piConcurrency, setPiConcurrency] = useState(2);
  const [piRunning, setPiRunning] = useState(false);
  const [piResult, setPiResult] = useState(null);
  const [piError, setPiError] = useState("");
  const [piResults, setPiResults] = useState([]);
  const [piViewing, setPiViewing] = useState(null);

  const [sourceFilter, setSourceFilter] = useState("all");
  const [runIds, setRunIds] = useState("");
  const [maxFiles, setMaxFiles] = useState(10);
  const [rdConcurrency, setRdConcurrency] = useState(2);
  const [rdRunning, setRdRunning] = useState(false);
  const [rdResult, setRdResult] = useState(null);
  const [rdError, setRdError] = useState("");

  const loadPiResults = useCallback(async () => {
    try {
      const res = await getProspectResults();
      setPiResults(res.runs || []);
    } catch {
      setPiResults([]);
    }
  }, []);
  useEffect(() => { loadPiResults(); }, [loadPiResults]);

  const handleRunProspect = async () => {
    setPiRunning(true);
    setPiError("");
    setPiResult(null);
    try {
      const res = await runProspectIntelligence({ input_file: inputFile, concurrency: Number(piConcurrency) });
      if (res.status === "failed") setPiError(res.message + (res.hint ? ` — ${res.hint}` : ""));
      else { setPiResult(res); loadPiResults(); }
    } catch (err) {
      setPiError(err instanceof ApiError ? err.message : "Could not reach the harvest backend.");
    } finally {
      setPiRunning(false);
    }
  };

  const handleViewProspectRun = async (runId) => {
    setPiViewing({ runId, loading: true });
    try {
      const res = await getProspectResult(runId);
      setPiViewing({ runId, loading: false, data: res });
    } catch (err) {
      setPiViewing({ runId, loading: false, error: err instanceof ApiError ? err.message : "Could not load this run." });
    }
  };

  const handleRunRecruiterDiscovery = async () => {
    setRdRunning(true);
    setRdError("");
    setRdResult(null);
    try {
      const res = await runRecruiterDiscovery({
        source_filter: sourceFilter,
        run_ids: runIds.trim() ? runIds.split(",").map((s) => s.trim()).filter(Boolean) : [],
        max_files: Number(maxFiles),
        concurrency: Number(rdConcurrency),
      });
      if (res.status === "failed" || res.status === "no_data") setRdError(res.message + (res.hint ? ` — ${res.hint}` : ""));
      else setRdResult(res);
    } catch (err) {
      setRdError(err instanceof ApiError ? err.message : "Could not reach the harvest backend.");
    } finally {
      setRdRunning(false);
    }
  };

  return (
    <main className="ha-main">
      <div style={{ padding: "24px 24px 0" }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, color: C.text }}>Lead Intelligence</h1>
        <p style={{ margin: "4px 0 0", fontSize: 14, color: C.textSoft }}>
          Two separate enrichment pipelines — prospect list enrichment and automatic recruiter discovery from harvested jobs.
        </p>
      </div>
      <div style={{ marginTop: 20, borderBottom: "1px solid " + C.border }} />

      <div style={{ display: "flex", flexDirection: "column", gap: 24, padding: 24 }}>
        {/* Prospect Intelligence */}
        <div className="ha-card" style={{ padding: 20 }}>
          <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 4 }}>Prospect Intelligence</div>
          <p style={{ fontSize: 13, color: C.textSoft, margin: "0 0 14px" }}>
            POST /run-prospect-intelligence — enriches a manually-prepared prospects.xlsx (columns: Client Name, Poc Name, Designation) with predicted contact data. This does <b>not</b> read from harvested jobs.
          </p>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5, color: C.textSoft, flex: 1, minWidth: 260 }}>
              Input file
              <input className="ha-input" style={{ padding: "8px 10px" }} value={inputFile} onChange={(e) => setInputFile(e.target.value)} />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5, color: C.textSoft }}>
              Concurrency (1–5)
              <input className="ha-input" type="number" min={1} max={5} style={{ padding: "8px 10px", width: 90 }} value={piConcurrency} onChange={(e) => setPiConcurrency(e.target.value)} />
            </label>
            <button className="ha-btn ha-btn-primary" onClick={handleRunProspect} disabled={piRunning}>
              {piRunning ? <Loader2 size={16} className="ha-spin" /> : <Play size={16} />} {piRunning ? "Running…" : "Run"}
            </button>
          </div>
          {piError && <div className="ha-errbanner" style={{ marginTop: 14 }}>{piError}</div>}
          {piResult && (
            <div className="ha-breakdown" style={{ marginTop: 14, fontSize: 13 }}>
              <span><b>{piResult.total_prospects}</b> prospects</span>
              <span><b>{piResult.enriched}</b> enriched</span>
              <span><b>{piResult.high_confidence}</b> high</span>
              <span><b>{piResult.medium_confidence}</b> medium</span>
              <span><b>{piResult.low_confidence}</b> low</span>
              <span>run_id: <b>{piResult.run_id}</b></span>
            </div>
          )}

          {piResults.length > 0 && (
            <div style={{ marginTop: 16, borderTop: "1px solid #EEF2F7", paddingTop: 14 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: C.textSoft, textTransform: "uppercase", marginBottom: 8 }}>Past runs</div>
              {piResults.map((r) => (
                <div key={r.run_id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 0", fontSize: 13 }}>
                  <span>{r.run_id} — {r.enriched}/{r.total} enriched</span>
                  <button className="ha-act" title="View" onClick={() => handleViewProspectRun(r.run_id)}><Eye size={16} /></button>
                </div>
              ))}
            </div>
          )}
          {piViewing && (
            <div style={{ marginTop: 12, background: C.pale, borderRadius: 8, padding: 12, fontSize: 12.5 }}>
              {piViewing.loading ? "Loading…" : piViewing.error ? piViewing.error : (
                <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{JSON.stringify(piViewing.data, null, 2).slice(0, 2000)}</pre>
              )}
            </div>
          )}
        </div>

        {/* Recruiter Discovery */}
        <div className="ha-card" style={{ padding: 20 }}>
          <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 4 }}>Recruiter Contact Discovery</div>
          <p style={{ fontSize: 13, color: C.textSoft, margin: "0 0 14px" }}>
            POST /run-recruiter-discovery — reads job posters from completed harvest runs and enriches them via company site / LinkedIn / Naukri lookup. Requires jobs with a named poster (see Rule Engine notes).
          </p>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
            <Select label="Source filter" value={sourceFilter} onChange={setSourceFilter} options={[
              { value: "all", label: "All" }, { value: "combined", label: "Combined" },
              { value: "linkedin", label: "LinkedIn" }, { value: "naukri", label: "Naukri" }, { value: "dice", label: "Dice" },
            ]} />
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5, color: C.textSoft, flex: 1, minWidth: 220 }}>
              Run IDs (optional, comma-separated)
              <input className="ha-input" style={{ padding: "8px 10px" }} value={runIds} onChange={(e) => setRunIds(e.target.value)} placeholder="e.g. 20260706_100116" />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5, color: C.textSoft }}>
              Max files (1–50)
              <input className="ha-input" type="number" min={1} max={50} style={{ padding: "8px 10px", width: 90 }} value={maxFiles} onChange={(e) => setMaxFiles(e.target.value)} />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5, color: C.textSoft }}>
              Concurrency (1–5)
              <input className="ha-input" type="number" min={1} max={5} style={{ padding: "8px 10px", width: 90 }} value={rdConcurrency} onChange={(e) => setRdConcurrency(e.target.value)} />
            </label>
            <button className="ha-btn ha-btn-primary" onClick={handleRunRecruiterDiscovery} disabled={rdRunning}>
              {rdRunning ? <Loader2 size={16} className="ha-spin" /> : <Play size={16} />} {rdRunning ? "Running…" : "Run"}
            </button>
          </div>
          {rdError && <div className="ha-errbanner" style={{ marginTop: 14 }}>{rdError}</div>}
          {rdResult && (
            <div className="ha-breakdown" style={{ marginTop: 14, fontSize: 13 }}>
              <span><b>{rdResult.total_recruiters}</b> recruiters</span>
              <span><b>{rdResult.enriched}</b> enriched</span>
              <span><b>{rdResult.contact_discovery?.verified_emails ?? 0}</b> verified emails</span>
              <span><b>{rdResult.contact_discovery?.public_emails ?? 0}</b> public emails</span>
              <span>run_id: <b>{rdResult.run_id}</b></span>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}

/* Page */
export default function HarvestAgent() {
  const [activePage, setActivePage] = useState("jobs");
  const [detailView, setDetailView] = useState(null); // null | { mode: "view"|"edit", job }
  const [viewingRunId, setViewingRunId] = useState(null);
  // Shared across Rule Engine + Source Runs so two pages can't both launch a
  // harvest at once — Playwright can't open two browsers on the same
  // persistent Chrome profile, and doing so fails the whole run.
  const [harvestRunning, setHarvestRunning] = useState(false);

  const [jobs, setJobs] = useState([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobsLoading, setJobsLoading] = useState(true);
  const [jobsError, setJobsError] = useState("");

  const [runs, setRuns] = useState([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [runsError, setRunsError] = useState("");

  const fetchJobs = useCallback(async () => {
    setJobsLoading(true);
    setJobsError("");
    try {
      const PAGE_SIZE = 500;
      const first = await getJobs({ page: 1, page_size: PAGE_SIZE, sort_by: "posted_date", sort_order: "desc" });
      let allJobs = first.jobs || [];
      for (let page = 2; page <= (first.total_pages || 1); page++) {
        const next = await getJobs({ page, page_size: PAGE_SIZE, sort_by: "posted_date", sort_order: "desc" });
        allJobs = allJobs.concat(next.jobs || []);
      }
      setJobs(allJobs.map(mapApiJob));
      setJobsTotal(first.total || 0);
    } catch (err) {
      setJobsError(
        err instanceof ApiError
          ? `Could not load jobs: ${err.message}`
          : "Could not reach the harvest backend. Is it running on the configured API URL?"
      );
      setJobs([]);
      setJobsTotal(0);
    } finally {
      setJobsLoading(false);
    }
  }, []);

  const fetchRuns = useCallback(async () => {
    setRunsLoading(true);
    setRunsError("");
    try {
      const res = await getRunHistory();
      setRuns((res.runs || []).map(mapRun));
    } catch (err) {
      setRunsError(
        err instanceof ApiError
          ? `Could not load run history: ${err.message}`
          : "Could not reach the harvest backend. Is it running on the configured API URL?"
      );
      setRuns([]);
    } finally {
      setRunsLoading(false);
    }
  }, []);

  const refreshAll = useCallback(() => { fetchJobs(); fetchRuns(); }, [fetchJobs, fetchRuns]);

  // Freeze the Run controls if a harvest is already running — including one
  // started in another tab or before this page loaded. Event-driven (mount +
  // window focus), so no always-on polling loop.
  const syncActiveRun = useCallback(async () => {
    try {
      const res = await getActiveRun();
      setHarvestRunning(Boolean(res?.active));
    } catch {
      // Backend unreachable — leave the flag as-is; HealthBadge surfaces the outage.
    }
  }, []);

  useEffect(() => { fetchJobs(); }, [fetchJobs]);
  useEffect(() => { fetchRuns(); }, [fetchRuns]);
  useEffect(() => {
    syncActiveRun();
    const onFocus = () => syncActiveRun();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [syncActiveRun]);

  if (activePage === "rules") {
    return (
      <RuleEngineConfig
        onNavigate={setActivePage} jobsCount={jobsTotal} runsCount={runs.length} onRunComplete={refreshAll}
        harvestRunning={harvestRunning} setHarvestRunning={setHarvestRunning}
      />
    );
  }

  if (detailView?.mode === "view") {
    return (
      <JobDetailsView
        job={detailView.job}
        onBack={() => setDetailView(null)}
        onEdit={() => setDetailView({ mode: "edit", job: detailView.job })}
      />
    );
  }

  if (viewingRunId) {
    return <RunDetailView runId={viewingRunId} onBack={() => setViewingRunId(null)} />;
  }

  return (
    <div className="ha-root">
      <ThemeStyles />
      <Sidebar activePage={activePage} onNavigate={setActivePage} jobsCount={jobsTotal} runsCount={runs.length} />
      {activePage === "history" ? (
        <RunHistoryPage runs={runs} loading={runsLoading} error={runsError} onRefresh={fetchRuns} onNavigate={setActivePage} onView={setViewingRunId} />
      ) : activePage === "sources" ? (
        <SourceRunsPage harvestRunning={harvestRunning} setHarvestRunning={setHarvestRunning} />
      ) : activePage === "leads" ? (
        <LeadIntelligencePage />
      ) : (
        <JobsPage jobs={jobs} total={jobsTotal} loading={jobsLoading} error={jobsError} onRefresh={fetchJobs} onNavigate={setActivePage} onView={setDetailView} />
      )}
    </div>
  );
}
