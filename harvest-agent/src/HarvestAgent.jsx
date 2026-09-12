import React, { createContext, useContext, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Routes, Route, Navigate, Outlet, useNavigate, useLocation, useParams, useSearchParams,
} from "react-router-dom";
import {
  SlidersHorizontal, Download,
  Search, Mail, ArrowUpDown, ArrowUp, ArrowDown, Eye, RefreshCw, ArrowLeft,
  CheckCircle2, XCircle, Loader2, HelpCircle, Play, FileJson, FileSpreadsheet,
  AlertTriangle, ChevronDown, Square,
} from "lucide-react";
import JobDetailsView from "./JobDetailsView";
import RuleEngineConfig from "./RuleEngineConfig";
import OutreachHistoryPage from "./OutreachHistoryPage";
import Sidebar from "./components/Sidebar";
import EmailComposeModal from "./components/EmailComposeModal";
import LinkedInMessageModal from "./components/LinkedInMessageModal";
import StopHarvestButton from "./components/StopHarvestModal";
import useCountUp from "./useCountUp";
import { makeStallWatch, STALL_WARN_MS, STALL_WARN_MSG } from "./stallWatch";
import {
  getJobs, getJob, getJobsFacets, getRunHistory, getRunHistoryEntry, getActiveRun, getHarvestStatus, ApiError,
  runLinkedinAgent, getLinkedinResults, getLinkedinResult,
  runNaukriAgent, getNaukriResults, getNaukriResult,
  runDiceAgent, getDiceResults, getDiceResult,
  runLinkedinFeedAgent, getLinkedinFeedResults, getLinkedinFeedResult,
  runProspectIntelligence, getProspectResults, getProspectResult,
  runRecruiterDiscovery,
  getOutreachStatus,
  downloadJsonUrl, downloadExcelUrl,
} from "./api";

/* Palette: Primary #2563EB · Secondary #1E40AF · Accent #F59E0B · BG #F8FAFC · Text #1E293B */
const C = {
  primary: "#2563EB", secondary: "#1E40AF", accent: "#F59E0B",
  bg: "#F8FAFC", text: "#1E293B", textSoft: "#64748B",
  border: "#E2E8F0", sidebar: "#1E293B", pale: "#EFF6FF",
};

// Labels for the Run Detail classification cards / active-filter chip.
const BUCKET_LABELS = {
  verified: "Verified contacts",
  direct: "Direct clients",
  gcc: "GCC companies",
  staffing: "Staffing firms",
  ambiguous: "Needs review",
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
    .ha-main{flex:1;min-width:0;overflow-x:hidden;}
    /* Sidebar styles now live in Sidebar.jsx (the shared component). */
    .ha-card{background:#fff;border:1px solid ${C.border};border-radius:12px;box-shadow:0 1px 2px rgba(15,23,42,.04);}
    .ha-input{box-sizing:border-box;height:38px;padding:0 12px;border:1px solid #CBD5E1;background:#fff;color:${C.text};border-radius:8px;font-size:14px;}
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
    .ha-th{padding:12px 16px;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:${C.textSoft};text-align:left;position:sticky;top:0;background:${C.pale};z-index:1;}
    .ha-sortbtn{display:inline-flex;align-items:center;gap:4px;border:0;background:transparent;cursor:pointer;font:inherit;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;}
    .ha-td{padding:14px 16px;vertical-align:middle;}
    .ha-row{border-top:1px solid #EEF2F7;}
    .ha-row:hover{background:#F1F5F9;}
    .ha-link{color:${C.primary};font-weight:600;background:none;border:0;padding:0;cursor:pointer;font-size:inherit;font-family:inherit;text-align:left;}
    .ha-statnum{font-size:30px;font-weight:700;line-height:1;}
    .ha-statlbl{margin-top:8px;font-size:14px;color:${C.textSoft};}
    .ha-select{cursor:pointer;flex:1 1 auto;min-width:0;box-sizing:border-box;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
    .ha-multiselect-btn{display:flex;align-items:center;justify-content:space-between;gap:8px;font:inherit;color:inherit;text-align:left;}
    .ha-multiselect-btn svg{flex-shrink:0;color:#94A3B8;}
    .ha-multiselect-summary{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
    .ha-multiselect-panel{position:absolute;top:calc(100% + 6px);left:0;z-index:10;min-width:100%;width:max-content;max-width:260px;max-height:240px;overflow-y:auto;background:#fff;border:1px solid ${C.border};border-radius:8px;box-shadow:0 8px 20px rgba(15,23,42,.12);padding:6px;}
    .ha-multiselect-opt{display:flex;align-items:center;gap:8px;padding:7px 8px;border-radius:6px;font-size:14px;color:${C.text};cursor:pointer;white-space:nowrap;}
    .ha-multiselect-opt:hover{background:${C.pale};}
    .ha-multiselect-opt input{cursor:pointer;}
    .ha-filterbar{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px 16px;align-items:center;width:100%;max-width:100%;box-sizing:border-box;}
    .ha-daterow{display:flex;flex-wrap:wrap;align-items:center;gap:12px;max-width:100%;box-sizing:border-box;}
    .ha-filter-field{display:flex;align-items:center;gap:8px;width:100%;min-width:0;box-sizing:border-box;}
    .ha-filter-field>span{font-size:14px;font-weight:500;color:${C.textSoft};white-space:nowrap;flex-shrink:0;}
    .ha-filter-search{position:relative;min-width:0;box-sizing:border-box;grid-column:1/-1;}
    .ha-filter-search .ha-input{width:100%;min-width:0;max-width:100%;padding-left:34px;box-sizing:border-box;}
    .ha-filter-search svg{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:#94A3B8;pointer-events:none;}
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
    /* Outreach already sent — a filled green state so a contacted recruiter is
       obvious at a glance (backed by the DB, so it survives a refresh). */
    .ha-cbtn-sent{border-color:#86EFAC;color:#047857;background:#ECFDF5;cursor:pointer;}
    .ha-cbtn-sent:hover{background:#047857;color:#fff;}
    .ha-spin{animation:ha-rot .9s linear infinite;}
    @keyframes ha-rot{to{transform:rotate(360deg);}}
    .ha-errbanner{background:#FEF2F2;border:1px solid #FCA5A5;color:#B91C1C;border-radius:10px;padding:10px 16px;font-size:13px;font-weight:500;}
    .ha-breakdown{display:flex;flex-wrap:wrap;gap:6px;font-size:12px;color:${C.textSoft};}
    .ha-breakdown b{color:${C.text};}
    .ha-detail-page{padding:24px;width:100%;box-sizing:border-box;}
    .ha-detail-back{display:inline-flex;align-items:center;gap:7px;cursor:pointer;background:#fff;border:1px solid ${C.border};color:#334155;font-size:13px;font-weight:600;padding:8px 14px;border-radius:8px;margin-bottom:18px;}
    .ha-detail-back:hover{background:#F1F5F9;}
    .ha-detail-card{background:#fff;border:1px solid ${C.border};border-radius:14px;padding:24px;margin-bottom:16px;}
    .ha-detail-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px;margin-top:16px;}
    .ha-detail-stat{background:${C.pale};border-radius:10px;padding:14px 16px;}
    .ha-detail-stat b{display:block;font-size:22px;color:${C.text};}
    .ha-detail-stat span{font-size:12px;color:${C.textSoft};}
    .ha-runhead{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;flex-wrap:wrap;}
    .ha-runhead-meta{flex:0 1 auto;min-width:220px;}
    .ha-runhead-right{flex:1 1 440px;min-width:280px;display:flex;flex-direction:column;align-items:flex-end;gap:12px;}
    .ha-runstats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;width:100%;}
    .ha-runstat{background:${C.pale};border:1px solid ${C.border};border-radius:10px;padding:11px 14px;}
    .ha-runstat b{display:block;font-size:22px;font-weight:700;line-height:1.1;color:${C.text};}
    .ha-runstat span{display:block;margin-top:3px;font-size:11.5px;font-weight:500;color:${C.textSoft};}
    .ha-runstat-btn{border:1px solid ${C.border};font:inherit;text-align:left;width:100%;box-sizing:border-box;cursor:pointer;transition:box-shadow .15s,background .15s,transform .05s;}
    .ha-runstat-btn:hover{background:#E0EAFF;}
    .ha-runstat-btn:active{transform:translateY(1px);}
    .ha-runstat-active{background:#fff;}
    @media(max-width:720px){.ha-runhead-right{align-items:stretch;flex-basis:100%;}.ha-runhead-right .ha-pill{align-self:flex-start;}}
    @media(max-width:520px){.ha-runstats{grid-template-columns:repeat(2,minmax(0,1fr));}}
  `}</style>
);

const SRC_TONES = {
  Naukri: { background: "#EFF6FF", color: "#1E40AF" },
  Dice: { background: "#FEF3C7", color: "#92400E" },
  LinkedIn: { background: "#E0E7FF", color: "#3730A3" },
  // Home Feed leads — a distinct purple tone so they read differently from the
  // LinkedIn Jobs source everywhere a SourceChip appears (Jobs table, Run History).
  "LinkedIn Feed": { background: "#F5F3FF", color: "#6D28D9" },
};
const SourceChip = ({ source }) => (
  <span className="ha-pill" style={SRC_TONES[source] || { background: "#F1F5F9", color: "#475569" }}>{source}</span>
);

const STATUS_TONES = {
  success:    { background: "#ECFDF5", color: "#047857", Icon: CheckCircle2 },
  no_results: { background: "#F1F5F9", color: "#475569", Icon: HelpCircle },
  failed:     { background: "#FEF2F2", color: "#B91C1C", Icon: XCircle },
  running:    { background: "#FFFBEB", color: "#B45309", Icon: Loader2 },
  stopped:    { background: "#FFF7ED", color: "#9A3412", Icon: Square },
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
    // Source-split contact values so the UI can label Job: vs Recruiter: when
    // both exist. email/mobile above stay the merged value used for search,
    // sort, mailto:/tel: links and the contact-action icons.
    emailScraped: j.email_scraped || null,
    emailRecruiter: j.email_recruiter || null,
    mobileScraped: j.phone_scraped || null,
    mobileRecruiter: j.phone_recruiter || null,
    linkedin: j.linkedin_profile_url || null,
    source: j.source || "—",
    jobDescription: j.job_description || "",
    jobDescriptionHtml: j.job_description_html || "",
    location: j.location || "",
    salary: j.salary || "",
    jobType: j.job_type || "",
    workMode: j.work_mode || "",
    applyLink: j.job_url || "",
    companyUrl: j.company_url || "",
    // Company-size band + friendly tier (Small/Medium/Large/Enterprise or "" ⇒
    // Unknown) captured at harvest — drives the display-only Company-size filter.
    companySize: j.company_size || "",
    companySizeTier: j.company_size_tier || "",
    // Job-location country (parsed from the free-text `location`) and the
    // company's HQ country/state (enrichment waterfall) — power the two Country
    // filters and the job-detail Company card.
    country: j.country || "",
    companyCountry: j.company_country || "",
    companyState: j.company_state || "",
    posterTitle: j.job_poster_designation || "",
    domain: j.domain || "",
    hiringEntity: j.hiring_entity || "",
    verificationStatus: j.verification_status || "",
    isGcc: !!j.is_gcc,
    // Business-filter annotation — the backend no longer drops jobs, it flags
    // them. passed_filter defaults to true for legacy rows lacking the column.
    passedFilter: j.passed_filter !== false,
    filterReason: j.filter_reason || "",
  };
}

function mapJobToDetail(j) {
  return {
    // id/email/linkedin are carried through so the detail view's Email/LinkedIn
    // icons can open the same LLM outreach composers the table rows use
    // (EmailComposeModal/LinkedInMessageModal read job.id + job.email + job.company).
    id: j.id,
    email: j.email || "",
    linkedin: j.linkedin || "",
    jobTitle: j.title,
    jd: j.jobDescription,
    jdHtml: j.jobDescriptionHtml || "",
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
    posterContact: {
      email: j.email || "",
      mobile: j.mobile || "",
      emailScraped: j.emailScraped || "",
      emailRecruiter: j.emailRecruiter || "",
      mobileScraped: j.mobileScraped || "",
      mobileRecruiter: j.mobileRecruiter || "",
    },
    source: j.source,
    domain: j.domain || "",
    hiringEntity: j.hiringEntity || "",
    // Company-size band + friendly tier, so the detail view can show them. Accept
    // either the mapped (companySize) or raw (company_size) shape defensively.
    companySize: j.companySize || j.company_size || "",
    companySizeTier: j.companySizeTier || j.company_size_tier || "",
    // Company HQ location (enrichment waterfall) for the detail Company card.
    companyCountry: j.companyCountry || j.company_country || "",
    companyState: j.companyState || j.company_state || "",
    passedFilter: j.passedFilter,
    filterReason: j.filterReason || "",
  };
}

function mapRun(entry) {
  return {
    runId: entry.run_id,
    sources: entry.sources || [],
    // run_type distinguishes a LinkedIn Home Feed lead run ("feed") from a
    // job-harvest run ("harvest") so the Run History page renders them apart.
    runType: entry.run_type || "harvest",
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

// A number that eases toward its value (used for the live "Jobs found" count on
// a running Run History row; finished rows have a stable value so it stays put).
const AnimatedNumber = ({ value }) => useCountUp(Number(value) || 0);

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
    <label className="ha-filter-field">
      <span>{label}</span>
      <select className="ha-input ha-select" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    </label>
  );
}

// Multi-select filter field: checkboxes for each option plus an "All" option
// that clears the selection (empty array == no filter == "All").
function MultiSelect({ label, options, selected, onChange }) {
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
function ContactActionBtn({ glyph: Glyph, title, available, href, onClick, sent = false, sentTitle }) {
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

/* Run detail page */
// Contact filter options — grouped for a professional dropdown. The option
// VALUES and the OR-combined predicate (jobMatchesContact) are unchanged; only
// the presentation is grouped. `summaryLabel` keeps the collapsed chip
// unambiguous ("Has email" vs "No email") while the open list stays short
// under its group header.
// Company-size filter options. Named tiers map to the LinkedIn employee bands
// captured per job (backend app/core/company_size.py); "unknown" catches jobs
// with no detected band (and non-LinkedIn sources). Display-only — filters which
// harvested rows are shown, never drops data.
const COMPANY_SIZE_FILTER_OPTIONS = [
  { value: "all", label: "All sizes" },
  { value: "Small", label: "Small · 2–200" },
  { value: "Medium", label: "Medium · 201–1,000" },
  { value: "Large", label: "Large · 1,001–10,000" },
  { value: "Enterprise", label: "Enterprise · 10,001+" },
  { value: "unknown", label: "Unknown" },
];

const CONTACT_FILTER_OPTIONS = [
  { group: "With contact" },
  { value: "email",    label: "Email",    summaryLabel: "Has email" },
  { value: "mobile",   label: "Phone",    summaryLabel: "Has phone" },
  { value: "linkedin", label: "LinkedIn", summaryLabel: "Has LinkedIn" },
  { group: "Without contact" },
  { value: "no_email",    label: "Email",    summaryLabel: "No email" },
  { value: "no_mobile",   label: "Phone",    summaryLabel: "No phone" },
  { value: "no_linkedin", label: "LinkedIn", summaryLabel: "No LinkedIn" },
  { value: "none", label: "None on file", summaryLabel: "No contact" },
];

// Positive tokens match rows that HAVE that channel; "no_*" tokens match rows
// MISSING it; "none" matches rows with no contact at all. Tokens are OR-combined
// (a row passes if it satisfies any selected one). Empty selection = no filter.
function jobMatchesContact(j, contact) {
  if (!contact || contact.length === 0) return true;
  const has = { email: !!j.email, mobile: !!j.mobile, linkedin: !!j.linkedin };
  const none = !has.email && !has.mobile && !has.linkedin;
  return contact.some((c) =>
    c === "email" ? has.email
      : c === "mobile" ? has.mobile
      : c === "linkedin" ? has.linkedin
      : c === "no_email" ? !has.email
      : c === "no_mobile" ? !has.mobile
      : c === "no_linkedin" ? !has.linkedin
      : c === "none" ? none
      : false);
}

/**
 * Renders a contact value that may come from two sources — the scraped job and
 * the enriched recruiter record. When both exist (and differ) they are shown as
 * two labeled lines (Job: / Recruiter:); a single source renders unlabeled, and
 * nothing renders an em-dash. `link` is "mailto:" or "tel:"; `cls` the anchor
 * class (ha-mail / ha-tel). Used by the jobs table and the job-details view.
 */
function DualContact({ scraped, recruiter, fallback, link, cls }) {
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

// Company-size tier → badge colours (bg tint + text). The tier is the headline
// value; the raw employee band renders small underneath. Unknown → neutral.
const SIZE_TIER_STYLE = {
  Small:      { bg: "#ECFDF5", fg: "#047857" }, // emerald
  Medium:     { bg: "#EFF6FF", fg: "#1D4ED8" }, // blue
  Large:      { bg: "#FFFBEB", fg: "#B45309" }, // amber
  Enterprise: { bg: "#F5F3FF", fg: "#6D28D9" }, // violet
};

// Company-size table cell: a coloured tier badge (Small/Medium/Large/Enterprise)
// with the employee-count band in a small font beneath it. "—" when unknown.
function CompanySizeCell({ band, tier }) {
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

/**
 * Shared jobs table used by both the Harvested Jobs page (JobsPage) and the
 * run-history per-run view (RunDetailView). Owns the Company/Contact/Job/POC/
 * search filter bar, sorting, and the WhatsApp/Email/LinkedIn contact-action
 * icons (with the LLM Email + LinkedIn composer modals). `preFilter` lets the
 * parent add its own predicate (date range on the Harvested Jobs page; classification
 * bucket in the run view). The filtered+sorted rows are reported back via
 * `onFilteredChange` so the parent can drive its own stats/export/footer.
 */
// Options for the "Rows per page" selector on the Harvested Jobs and per-run
// views. On the Harvested Jobs page this is the server page size (each page is
// fetched on demand); on the per-run view it's the client-side display page
// size over the run's fully-loaded rows.
const PAGE_SIZE_OPTIONS = [
  { value: "50", label: "50" },
  { value: "100", label: "100" },
  { value: "200", label: "200" },
  { value: "500", label: "500" },
];

// Fallback display page size when no explicit "Rows per page" is provided.
const DEFAULT_DISPLAY_PAGE_SIZE = 100;

// Load EVERY row matching `baseParams` in 100-row pages — page 1 first to learn
// total_pages, then the remaining pages in parallel. Used where the full result
// set is genuinely needed: the run-detail view (one run's jobs) and the Jobs
// page's CSV export (all rows matching the active filters). baseParams may
// override the page_size/sort defaults. Returns the mapped rows plus the
// server's total for that filter set.
async function fetchAllJobs(baseParams) {
  const base = { page_size: 100, sort_by: "posted_date", sort_order: "desc", ...baseParams };
  const first = await getJobs({ ...base, page: 1 });
  const totalPages = first.total_pages || 1;
  let rows = first.jobs || [];
  if (totalPages > 1) {
    const rest = [];
    for (let page = 2; page <= totalPages; page++) rest.push(getJobs({ ...base, page }));
    for (const res of await Promise.all(rest)) rows = rows.concat(res.jobs || []);
  }
  return { rows: rows.map(mapApiJob), total: first.total || 0 };
}

function JobsTable({
  jobs,
  onView,
  preFilter = null,
  loading = false,
  emptyMessage = "No jobs match your filters.",
  minWidth = 1180,
  onFilteredChange = null,
  pageSize = DEFAULT_DISPLAY_PAGE_SIZE,
  urlState = false,
  // Server mode (the Harvested Jobs page): `jobs` is just the current server
  // page — filtering/sorting/pagination happen in the backend. The table emits
  // its filter/sort/page state via onParamsChange (as GET /jobs query params)
  // instead of filtering in-memory, dropdown options come from `facets`
  // (GET /jobs/facets, whole-dataset distincts), and the footer counts against
  // `serverTotal`. Client mode (RunDetailView) is unchanged.
  serverMode = false,
  facets = null,
  serverTotal = 0,
  onParamsChange = null,
  serverExtraParams = null,
}) {
  // When `urlState`, the filter/search/sort/page state is mirrored to the URL
  // query string (so /jobs?company=…&page=2 is shareable and survives a refresh).
  // Initialised from the URL on mount; written back on change. Non-urlState
  // instances (e.g. the per-run table) keep purely-local state, unchanged.
  const [searchParams, setSearchParams] = useSearchParams();
  const [filters, setFilters] = useState(() =>
    urlState
      ? {
          company: searchParams.get("company") || "all",
          contact: (searchParams.get("contact") || "").split(",").filter(Boolean),
          job: searchParams.get("job") || "all",
          poc: searchParams.get("poc") || "all",
          size: searchParams.get("size") || "all",
          country: searchParams.get("country") || "all",
          companyCountry: searchParams.get("companyCountry") || "all",
        }
      : { company: "all", contact: [], job: "all", poc: "all", size: "all", country: "all", companyCountry: "all" }
  );
  const [query, setQuery] = useState(() => (urlState ? searchParams.get("q") || "" : ""));
  const [sort, setSort] = useState(() =>
    urlState
      ? { col: searchParams.get("sort") || "posted", dir: searchParams.get("dir") || "desc" }
      : { col: "posted", dir: "desc" }
  );
  const [page, setPage] = useState(() =>
    urlState ? Math.max(1, parseInt(searchParams.get("page") || "1", 10) || 1) : 1
  );

  // Outreach modals + the per-row "no data on this channel" inline message.
  // emailModal carries follow-up context: { job, followup, parentOutreachId }.
  const [emailModal, setEmailModal] = useState(null);
  const [linkedinModalJob, setLinkedinModalJob] = useState(null);
  const [noDataMsg, setNoDataMsg] = useState(null); // { jobId, channel }
  // DB-backed "already contacted" state per job id: { [jobId]: { email?, linkedin? } }.
  // Fetched for the visible page rows so a sent recruiter shows a green icon that
  // survives refresh; clicking a sent email icon opens the follow-up composer.
  const [outreachStatus, setOutreachStatus] = useState({});
  const noDataTimer = useRef(null);
  const showNoData = useCallback((jobId, channel) => {
    setNoDataMsg({ jobId, channel });
    if (noDataTimer.current) clearTimeout(noDataTimer.current);
    noDataTimer.current = setTimeout(() => setNoDataMsg(null), 3000);
  }, []);
  useEffect(() => () => { if (noDataTimer.current) clearTimeout(noDataTimer.current); }, []);

  // Server mode gets its dropdown options from GET /jobs/facets (they must span
  // the whole dataset, not just the loaded page); client mode derives them from
  // the fully-loaded rows as before.
  const companies = useMemo(
    () => (serverMode ? (facets?.companies || []) : Array.from(new Set(jobs.map((j) => j.company))).sort()),
    [serverMode, facets, jobs]
  );
  const jobTitles = useMemo(
    () => (serverMode ? (facets?.job_titles || []) : Array.from(new Set(jobs.map((j) => j.title))).sort()),
    [serverMode, facets, jobs]
  );
  const pocNames = useMemo(
    () => (serverMode ? (facets?.poc_names || []) : Array.from(new Set(jobs.filter((j) => j.poc).map((j) => j.poc))).sort()),
    [serverMode, facets, jobs]
  );
  // Job-location country and company-HQ country dropdown options — whole-dataset
  // facets in server mode, distinct-from-loaded-rows in client mode (Run Detail).
  const countries = useMemo(
    () => (serverMode ? (facets?.countries || []) : Array.from(new Set(jobs.map((j) => j.country).filter(Boolean))).sort()),
    [serverMode, facets, jobs]
  );
  const companyCountries = useMemo(
    () => (serverMode ? (facets?.company_countries || []) : Array.from(new Set(jobs.map((j) => j.companyCountry).filter(Boolean))).sort()),
    [serverMode, facets, jobs]
  );

  const filtered = useMemo(() => {
    // Server mode: rows arrive already filtered/sorted/paginated by the backend.
    if (serverMode) return jobs;
    const rows = jobs.filter((j) => {
      if (filters.company !== "all" && j.company !== filters.company) return false;
      if (filters.job !== "all" && j.title !== filters.job) return false;
      if (filters.poc !== "all" && j.poc !== filters.poc) return false;
      if (filters.country !== "all" && j.country !== filters.country) return false;
      if (filters.companyCountry !== "all" && j.companyCountry !== filters.companyCountry) return false;
      // Company-size is a display-only filter over the captured band's tier.
      // "unknown" matches rows with no detected size; a named tier matches exactly.
      if (filters.size && filters.size !== "all") {
        const tier = j.companySizeTier || "";
        if (filters.size === "unknown" ? tier !== "" : tier !== filters.size) return false;
      }
      if (!jobMatchesContact(j, filters.contact)) return false;
      if (preFilter && !preFilter(j)) return false;
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
  }, [serverMode, jobs, filters, query, sort, preFilter]);

  useEffect(() => { if (onFilteredChange) onFilteredChange(filtered); }, [filtered, onFilteredChange]);

  // Reset to the first display page whenever the filtered result changes shape
  // (new load, filter, search, sort, or a page-size change) so the user is
  // never stranded on an out-of-range page. Client mode only — in server mode
  // `jobs` changes after every fetch, so this would bounce the page back to 1
  // (and re-fetch) each time; the param-emission effect below owns the reset.
  useEffect(() => {
    if (!serverMode) setPage(1);
  }, [serverMode, filters, query, sort, preFilter, jobs, pageSize]);

  // Debounced free-text search — the input stays immediate, but server fetches
  // wait 350 ms after the last keystroke. Harmless (unused) in client mode.
  const [debouncedQuery, setDebouncedQuery] = useState(query);
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query), 350);
    return () => clearTimeout(t);
  }, [query]);

  // Server mode: emit the current filter/sort/page state as GET /jobs params.
  // On a filter/sort/size change while past page 1, reset to page 1 FIRST and
  // let the effect re-run — so each state change produces exactly one fetch.
  const serverParamsSig = JSON.stringify({
    filters, q: debouncedQuery.trim(), sort, pageSize, extra: serverExtraParams || null,
  });
  const sigRef = useRef(serverParamsSig);
  useEffect(() => {
    if (!serverMode || !onParamsChange) return;
    if (sigRef.current !== serverParamsSig) {
      sigRef.current = serverParamsSig;
      if (page !== 1) { setPage(1); return; }
    }
    const sortByMap = { title: "job_title", company: "company", poc: "job_poster_name", source: "source", posted: "posted_date" };
    onParamsChange({
      page,
      page_size: pageSize,
      sort_by: sortByMap[sort.col] || "posted_date",
      sort_order: sort.dir === "asc" ? "asc" : "desc",
      keyword: debouncedQuery.trim(),
      company_exact: filters.company !== "all" ? filters.company : "",
      job_title: filters.job !== "all" ? filters.job : "",
      poc: filters.poc !== "all" ? filters.poc : "",
      size_tier: filters.size && filters.size !== "all" ? filters.size : "",
      country: filters.country !== "all" ? filters.country : "",
      company_country: filters.companyCountry !== "all" ? filters.companyCountry : "",
      contact: filters.contact.join(","),
      ...(serverExtraParams || {}),
    });
    // The emitted params are fully captured by serverParamsSig + page; listing
    // the individual pieces here would only duplicate the signature.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverMode, onParamsChange, serverParamsSig, page]);

  // Server mode: if a deep-linked page is out of range for the (possibly
  // filtered) result set, the backend returns an empty page — snap back to 1.
  useEffect(() => {
    if (serverMode && !loading && jobs.length === 0 && serverTotal > 0 && page > 1) setPage(1);
  }, [serverMode, loading, jobs, serverTotal, page]);

  // Mirror the current filter/search/sort/page to the URL query string (only for
  // the URL-synced instance). Uses replace so it doesn't spam the history stack.
  useEffect(() => {
    if (!urlState) return;
    const sp = new URLSearchParams();
    if (filters.company !== "all") sp.set("company", filters.company);
    if (filters.contact.length) sp.set("contact", filters.contact.join(","));
    if (filters.job !== "all") sp.set("job", filters.job);
    if (filters.poc !== "all") sp.set("poc", filters.poc);
    if (filters.size && filters.size !== "all") sp.set("size", filters.size);
    if (filters.country !== "all") sp.set("country", filters.country);
    if (filters.companyCountry !== "all") sp.set("companyCountry", filters.companyCountry);
    if (query.trim()) sp.set("q", query.trim());
    if (sort.col !== "posted" || sort.dir !== "desc") { sp.set("sort", sort.col); sp.set("dir", sort.dir); }
    if (page > 1) sp.set("page", String(page));
    setSearchParams(sp, { replace: true });
  }, [urlState, filters, query, sort, page, setSearchParams]);

  // Server mode paginates against the backend's filtered total — `jobs` already
  // is the current page. Client mode slices the filtered in-memory rows.
  const effectiveTotal = serverMode ? serverTotal : filtered.length;
  const totalPages = Math.max(1, Math.ceil(effectiveTotal / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pageRows = serverMode ? filtered : filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  // Fetch outreach status for just the visible rows (keeps the query small vs.
  // the whole dataset). Merged into prior results so paged-away rows stay marked.
  const pageRowIdsKey = pageRows.map((j) => j.id).join(",");
  const refreshOutreachStatus = useCallback(async () => {
    const ids = pageRowIdsKey ? pageRowIdsKey.split(",") : [];
    if (!ids.length) return;
    try {
      const map = await getOutreachStatus(ids);
      setOutreachStatus((prev) => ({ ...prev, ...(map || {}) }));
    } catch {
      // best-effort — icons fall back to the default (not-sent) state
    }
  }, [pageRowIdsKey]);
  useEffect(() => { refreshOutreachStatus(); }, [refreshOutreachStatus]);

  // Columns: Job title, Company, Company size, POC, Email,
  // Mobile, Contact,Source, Posted date
  const colCount = 9;

  return (
    <>
      <div className="ha-card ha-filterbar" style={{ padding: "16px 20px", gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
        <Select label="Company" value={filters.company} onChange={(v) => setFilters((f) => ({ ...f, company: v }))}
          options={[{ value: "all", label: "All" }, ...companies.map((c) => ({ value: c, label: c }))]} />
        <Select label="Company size" value={filters.size || "all"} onChange={(v) => setFilters((f) => ({ ...f, size: v }))}
          options={COMPANY_SIZE_FILTER_OPTIONS} />
        <Select label="Company country" value={filters.companyCountry} onChange={(v) => setFilters((f) => ({ ...f, companyCountry: v }))}
          options={[{ value: "all", label: "All" }, ...companyCountries.map((c) => ({ value: c, label: c }))]} />
        <MultiSelect label="Contact" selected={filters.contact} onChange={(v) => setFilters((f) => ({ ...f, contact: v }))}
          options={CONTACT_FILTER_OPTIONS} />
        <Select label="Job" value={filters.job} onChange={(v) => setFilters((f) => ({ ...f, job: v }))}
          options={[{ value: "all", label: "All" }, ...jobTitles.map((t) => ({ value: t, label: t }))]} />
        <Select label="Job country" value={filters.country} onChange={(v) => setFilters((f) => ({ ...f, country: v }))}
          options={[{ value: "all", label: "All" }, ...countries.map((c) => ({ value: c, label: c }))]} />
        <Select label="POC" value={filters.poc} onChange={(v) => setFilters((f) => ({ ...f, poc: v }))}
          options={[{ value: "all", label: "All" }, ...pocNames.map((p) => ({ value: p, label: p }))]} />
        <div className="ha-filter-search">
          <Search size={16} />
          <input className="ha-input" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search…" />
        </div>
      </div>

      <div className="ha-card" style={{ overflow: "hidden", marginTop: 14 }}>
        <div className="ha-table-scroll">
          <table className="ha-table" style={{ minWidth }}>
            <thead className="ha-thead">
              <tr>
                <SortHeader label="Job title" col="title" sort={sort} setSort={setSort} width={230} />
                <SortHeader label="Company" col="company" sort={sort} setSort={setSort} width={170} />
                <PlainHeader label="Company size" width={160} />
                <SortHeader label="POC" col="poc" sort={sort} setSort={setSort} width={150} />
                <PlainHeader label="Email" width={200} />
                <PlainHeader label="Mobile" width={140} />
                <PlainHeader label="Contact" align="center" width={130} />
                <SortHeader label="Source" col="source" sort={sort} setSort={setSort} width={100} />
                <SortHeader label="Posted date" col="posted" sort={sort} setSort={setSort} width={130} />
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr><td className="ha-td" colSpan={colCount} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                  Loading harvested jobs…
                </td></tr>
              )}
              {!loading && pageRows.map((j) => (
                <tr key={j.id} className="ha-row">
                  <td className="ha-td">
                    <span className="ha-link" role="button" tabIndex={0} style={{ cursor: "pointer" }}
                      title="View details"
                      onClick={() => onView({ mode: "view", job: mapJobToDetail(j) })}
                      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onView({ mode: "view", job: mapJobToDetail(j) }); } }}>
                      {j.title}
                    </span>
                  </td>
                  <td className="ha-td" style={{ color: C.text }}>{j.company}</td>
                  <td className="ha-td">
                    <CompanySizeCell band={j.companySize} tier={j.companySizeTier} />
                  </td>
                  <td className="ha-td" style={{ color: C.text }}>
                    {j.poc || <span style={{ color: "#94A3B8" }}>—</span>}
                  </td>
                  <td className="ha-td">
                    <DualContact scraped={j.emailScraped} recruiter={j.emailRecruiter} fallback={j.email} link="mailto:" cls="ha-mail" />
                  </td>
                  <td className="ha-td" style={{ whiteSpace: "nowrap" }}>
                    <DualContact scraped={j.mobileScraped} recruiter={j.mobileRecruiter} fallback={j.mobile} link="tel:" cls="ha-tel" />
                  </td>
                  <td className="ha-td">
                    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 4 }}>
                      <div style={{ display: "flex", gap: 8, justifyContent: "center" }}>
                        <ContactActionBtn glyph={WhatsAppIcon} title="WhatsApp" available={!!j.whatsapp}
                          href={j.whatsapp ? "https://wa.me/" + j.whatsapp.replace(/[^0-9]/g, "") : null}
                          onClick={() => showNoData(j.id, "WhatsApp")} />
                        {(() => {
                          const st = outreachStatus[j.id] || {};
                          const emailSent = !!st.email;
                          const liSent = !!st.linkedin;
                          const emailSentTitle = emailSent
                            ? `Email sent${st.email.followup_count ? ` (+${st.email.followup_count} follow-up${st.email.followup_count > 1 ? "s" : ""})` : ""} — click to follow up`
                            : undefined;
                          return (
                            <>
                              <ContactActionBtn glyph={Mail} title="Email" available={!!j.email}
                                sent={emailSent} sentTitle={emailSentTitle}
                                onClick={() =>
                                  emailSent
                                    ? setEmailModal({ job: j, followup: true, parentOutreachId: st.email.outreach_id })
                                    : j.email
                                      ? setEmailModal({ job: j, followup: false })
                                      : showNoData(j.id, "email")
                                } />
                              <ContactActionBtn glyph={LinkedInIcon} title="LinkedIn" available={!!j.linkedin}
                                sent={liSent} sentTitle={liSent ? "LinkedIn message sent" : undefined}
                                onClick={() => (j.linkedin ? setLinkedinModalJob(j) : showNoData(j.id, "LinkedIn"))} />
                            </>
                          );
                        })()}
                      </div>
                      {noDataMsg && noDataMsg.jobId === j.id && (
                        <small style={{ color: "#B91C1C", fontSize: 11, whiteSpace: "nowrap" }}>
                          No {noDataMsg.channel} available
                        </small>
                      )}
                    </div>
                  </td>
                  <td className="ha-td"><SourceChip source={j.source} /></td>
                  <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>{j.postedDate || "—"}</td>
                </tr>
              ))}
              {!loading && filtered.length === 0 && (
                <tr><td className="ha-td" colSpan={colCount} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                  {emptyMessage}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {!loading && effectiveTotal > pageSize && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12, marginTop: 12, padding: "0 4px", fontSize: 13, color: C.textSoft }}>
          <span>
            Showing {(currentPage - 1) * pageSize + 1}–{Math.min(currentPage * pageSize, effectiveTotal)} of {effectiveTotal}
          </span>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <button className="ha-btn ha-btn-secondary" disabled={currentPage <= 1} onClick={() => setPage(Math.max(1, currentPage - 1))}>Prev</button>
            <span>Page {currentPage} of {totalPages}</span>
            <button className="ha-btn ha-btn-secondary" disabled={currentPage >= totalPages} onClick={() => setPage(Math.min(totalPages, currentPage + 1))}>Next</button>
          </div>
        </div>
      )}

      {emailModal && (
        <EmailComposeModal
          job={emailModal.job}
          followup={emailModal.followup}
          parentOutreachId={emailModal.parentOutreachId}
          onClose={() => setEmailModal(null)}
          onSent={refreshOutreachStatus}
        />
      )}
      {linkedinModalJob && (
        <LinkedInMessageModal
          job={linkedinModalJob}
          onClose={() => setLinkedinModalJob(null)}
          onLogged={refreshOutreachStatus}
        />
      )}
    </>
  );
}

function RunDetailView({ runId, onBack, onView }) {
  const [entry, setEntry] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // This run's harvested jobs (GET /jobs?run_id=…) — same list→detail pattern
  // as the global Harvested Jobs page, scoped to this single run.
  const [jobs, setJobs] = useState([]);
  const [jobsLoading, setJobsLoading] = useState(true);
  const [jobsError, setJobsError] = useState("");
  const [pageSizeSel, setPageSizeSel] = useState("100");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getRunHistoryEntry(runId)
      .then((res) => { if (!cancelled) setEntry(mapRun(res)); })
      .catch((err) => { if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not load this run."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [runId]);

  useEffect(() => {
    let cancelled = false;
    setJobsLoading(true);
    setJobsError("");
    (async () => {
      try {
        const { rows } = await fetchAllJobs({ run_id: runId });
        if (!cancelled) setJobs(rows);
      } catch (err) {
        if (!cancelled) { setJobsError(err instanceof ApiError ? err.message : "Could not load this run's jobs."); setJobs([]); }
      } finally {
        if (!cancelled) setJobsLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [runId]);

  // The classification bucket is driven by the clickable stat cards in the run
  // header ("all" | "verified" | "direct" | "gcc" | "staffing" | "ambiguous").
  // The rest of the filtering (company/contact/job/POC/search/sort) lives in the
  // shared JobsTable, which reports its filtered rows back via onFilteredChange
  // for the stats + CSV export below.
  const [bucket, setBucket] = useState("all");
  const [filteredRows, setFilteredRows] = useState([]);

  const bucketPredicate = useCallback((j) => {
    // Predicates mirror the backend run-summary counts (hiring_entity /
    // verification_status), so a card's number matches the rows shown.
    if (bucket === "verified" && j.verificationStatus !== "verified") return false;
    if (bucket === "direct" && j.hiringEntity !== "Direct Client") return false;
    if (bucket === "gcc" && j.hiringEntity !== "GCC") return false;
    if (bucket === "staffing" && j.hiringEntity !== "Staffing Firm") return false;
    if (bucket === "ambiguous" && j.hiringEntity !== "Ambiguous") return false;
    return true;
  }, [bucket]);

  const jobStats = useMemo(() => ({
    total: filteredRows.length,
    companies: new Set(filteredRows.map((j) => j.company)).size,
    pocs: filteredRows.filter((j) => j.poc).length,
  }), [filteredRows]);

  function exportCsv() {
    const header = ["Job title", "Company", "Source", "POC", "Posted date", "Email", "Mobile", "Job description"];
    const lines = filteredRows.map((j) =>
      [j.title, j.company, j.source, j.poc || "—", j.postedDate || "—", j.email || "—", j.mobile || "—", j.jobDescription || "—"]
        .map((c) => '"' + String(c).replace(/"/g, '""') + '"').join(","));
    // Prepend a UTF-8 BOM so Excel detects the encoding — without it Excel reads
    // the file as Windows-1252 and turns the "—" placeholder into "â€"".
    const blob = new Blob([String.fromCharCode(0xFEFF) + [header.join(","), ...lines].join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `jobs_${runId}.csv`; a.click();
    URL.revokeObjectURL(url);
  }

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
                <div className="ha-runhead">
                  <div className="ha-runhead-meta">
                    <div style={{ fontSize: 12, color: C.textSoft, fontWeight: 600, letterSpacing: ".04em", textTransform: "uppercase" }}>Run ID</div>
                    <div style={{ fontSize: 22, fontWeight: 700, color: C.secondary, letterSpacing: "-.01em" }}>{entry.runId}</div>
                    <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
                      {entry.sources.map((s) => <SourceChip key={s} source={s} />)}
                    </div>
                    <div style={{ display: "flex", gap: 32, marginTop: 18, flexWrap: "wrap" }}>
                      <div>
                        <div style={{ fontSize: 11, color: C.textSoft, fontWeight: 600, letterSpacing: ".05em", textTransform: "uppercase" }}>Started</div>
                        <div style={{ fontSize: 14, fontWeight: 600, color: C.text, marginTop: 3 }}>{fmtDate(entry.startedAt)}</div>
                      </div>
                      <div>
                        <div style={{ fontSize: 11, color: C.textSoft, fontWeight: 600, letterSpacing: ".05em", textTransform: "uppercase" }}>Completed</div>
                        <div style={{ fontSize: 14, fontWeight: 600, color: C.text, marginTop: 3 }}>{fmtDate(entry.completedAt)}</div>
                      </div>
                    </div>
                  </div>
                  <div className="ha-runhead-right">
                    <StatusPill status={entry.status} />
                    <div className="ha-runstats">
                      {[
                        { bucket: "all",       value: entry.jobsFound,     label: "Jobs found",        color: C.accent },
                        { bucket: "verified",  value: entry.verifiedJobs,  label: "Verified contacts", color: "#059669" },
                        { bucket: "direct",    value: entry.directClients, label: "Direct clients",    color: C.primary },
                        { bucket: "gcc",       value: entry.gcc,           label: "GCC companies",     color: C.secondary },
                        { bucket: "staffing",  value: entry.staffingFirms, label: "Staffing firms",    color: "#7C3AED" },
                        { bucket: "ambiguous", value: entry.ambiguous,     label: "Needs review",      color: "#D97706" },
                      ].map((card) => {
                        const active = bucket === card.bucket;
                        return (
                          <button
                            key={card.bucket}
                            type="button"
                            className={"ha-runstat ha-runstat-btn" + (active ? " ha-runstat-active" : "")}
                            aria-pressed={active}
                            title={card.bucket === "all" ? "Show all jobs" : `Filter this run's jobs to ${card.label}`}
                            onClick={() => setBucket((b) => b === card.bucket ? "all" : card.bucket)}
                            style={active ? { boxShadow: `inset 0 0 0 2px ${card.color}` } : undefined}
                          >
                            <b style={{ color: card.color }}>{card.value}</b><span>{card.label}</span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </div>
              </div>

              <div className="ha-detail-card">
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                    <div style={{ fontSize: 12, color: C.textSoft, fontWeight: 700, letterSpacing: ".05em", textTransform: "uppercase" }}>
                      Harvested jobs in this run
                    </div>
                    {bucket !== "all" && (
                      <button className="ha-pill" onClick={() => setBucket("all")}
                        title="Clear card filter"
                        style={{ background: C.pale, color: C.secondary, border: "1px solid " + C.border, display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                        {BUCKET_LABELS[bucket]} <XCircle size={12} />
                      </button>
                    )}
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 16, fontSize: 13, color: C.textSoft }}>
                    <span><b style={{ color: C.text }}>{jobStats.total}</b> jobs</span>
                    <span><b style={{ color: C.text }}>{jobStats.companies}</b> companies</span>
                    <span><b style={{ color: C.text }}>{jobStats.pocs}</b> POCs</span>
                    <Select label="Rows per page" value={pageSizeSel} onChange={setPageSizeSel} options={PAGE_SIZE_OPTIONS} />
                    <button className="ha-btn ha-btn-primary" onClick={exportCsv} disabled={filteredRows.length === 0}>
                      <Download size={16} /> Export CSV
                    </button>
                  </div>
                </div>

                {jobsError && <div className="ha-errbanner" style={{ marginTop: 14 }}>{jobsError}</div>}

                <div style={{ marginTop: 14 }}>
                  <JobsTable
                    jobs={jobs}
                    onView={onView}
                    loading={jobsLoading}
                    preFilter={bucketPredicate}
                    onFilteredChange={setFilteredRows}
                    minWidth={1410}
                    pageSize={Number(pageSizeSel)}
                    emptyMessage={jobs.length === 0 ? "No jobs recorded for this run." : "No jobs match your filters."}
                  />
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
// Server-side pagination: only the current page of rows is ever loaded. The
// JobsTable (in serverMode) emits its filter/sort/page state via
// handleParamsChange, which fetches exactly that slice from GET /jobs; dropdown
// options and the header/footer stat counts come from GET /jobs/facets so they
// still span the whole dataset.
function JobsPage({ onNavigate, onView, pageSizeSel, onPageSizeChange, urlState = false }) {
  const { jobsViewRef } = useHarvestData();
  const location = useLocation();
  // Seed from the persistent cache when we're returning to the SAME view (its URL
  // search matches what the cached rows were fetched for) — so the list renders
  // instantly and revalidates quietly, instead of blanking to a spinner. Dates
  // live in JobsPage state (not the URL), so they ride along in the cache too.
  const cache = jobsViewRef.current;
  const rowsSeed = cache && cache.search === location.search ? cache : null;

  const [startDate, setStartDate] = useState(rowsSeed?.dateFrom || "");
  const [endDate, setEndDate] = useState(rowsSeed?.dateTo || "");
  const [pageJobs, setPageJobs] = useState(rowsSeed?.jobs || []);
  const [pageTotal, setPageTotal] = useState(rowsSeed?.total || 0);
  const [pageLoading, setPageLoading] = useState(!rowsSeed);
  const [pageError, setPageError] = useState("");
  const [facets, setFacets] = useState(cache?.facets || null);
  const [exporting, setExporting] = useState(false);
  // Monotonic request counter — a response only lands if no newer request has
  // started since (fast typing / rapid filter clicks can overlap fetches).
  const seqRef = useRef(0);
  const lastParamsRef = useRef(null);
  // On a cache-seeded remount, keep the cached rows visible during the first
  // (revalidating) fetch instead of flashing the loading state.
  const seededRef = useRef(!!rowsSeed);
  const firstFetchRef = useRef(false);

  const handleParamsChange = useCallback(async (params) => {
    lastParamsRef.current = params;
    const seq = ++seqRef.current;
    const quiet = !firstFetchRef.current && seededRef.current;
    firstFetchRef.current = true;
    setPageLoading(!quiet);
    setPageError("");
    try {
      const res = await getJobs(params);
      if (seq !== seqRef.current) return; // superseded by a newer request
      const mapped = (res.jobs || []).map(mapApiJob);
      setPageJobs(mapped);
      setPageTotal(res.total || 0);
      setPageLoading(false);
      // Cache this exact view so returning to it is instant. Keyed by the URL
      // search (filters/page/sort) + the date range that produced these rows.
      jobsViewRef.current = {
        ...(jobsViewRef.current || {}),
        search: window.location.search,
        jobs: mapped,
        total: res.total || 0,
        dateFrom: params.date_from || "",
        dateTo: params.date_to || "",
      };
    } catch (err) {
      if (seq !== seqRef.current) return;
      setPageError(
        err instanceof ApiError
          ? `Could not load jobs: ${err.message}`
          : "Could not reach the harvest backend. Is it running on the configured API URL?"
      );
      setPageJobs([]);
      setPageTotal(0);
      setPageLoading(false);
    }
  }, [jobsViewRef]);

  const fetchFacets = useCallback(async () => {
    try {
      const f = await getJobsFacets();
      setFacets(f);
      // Facets are whole-dataset (filter-independent), so cache them globally.
      jobsViewRef.current = { ...(jobsViewRef.current || {}), facets: f };
    } catch {
      // Dropdowns fall back to "All"-only and stats render as 0; the jobs
      // fetch's own error banner covers a backend outage.
    }
  }, [jobsViewRef]);
  useEffect(() => { fetchFacets(); }, [fetchFacets]);

  const refresh = useCallback(() => {
    fetchFacets();
    if (lastParamsRef.current) handleParamsChange(lastParamsRef.current);
  }, [fetchFacets, handleParamsChange]);

  // Date range is a server-side filter now (date_from/date_to) — JobsTable
  // folds these into every emitted params object.
  const serverExtraParams = useMemo(
    () => ({ date_from: startDate, date_to: endDate }),
    [startDate, endDate]
  );

  const stats = facets?.stats || {};

  // Export the FULL filtered dataset, not just the loaded page: re-run the
  // current filter params without pagination through fetchAllJobs (which pulls
  // every matching page) and build the CSV from all rows.
  async function exportCsv() {
    if (exporting) return;
    setExporting(true);
    try {
      const { page, page_size, ...filterParams } = lastParamsRef.current || {};
      const { rows } = await fetchAllJobs(filterParams);
      const header = ["Job title", "Company", "Source", "Filter status", "Filter reason", "POC", "Posted date", "Email", "Mobile", "WhatsApp", "LinkedIn", "Job description"];
      const lines = rows.map((j) =>
        [j.title, j.company, j.source, j.passedFilter ? "Qualified" : "Flagged", j.filterReason || "—", j.poc || "—", j.postedDate || "—", j.email || "—", j.mobile || "—", j.whatsapp || "—", j.linkedin || "—", j.jobDescription || "—"]
          .map((c) => '"' + String(c).replace(/"/g, '""') + '"').join(","));
      // Prepend a UTF-8 BOM so Excel detects the encoding — without it Excel reads
      // the file as Windows-1252 and turns the "—" placeholder into "â€"".
      const blob = new Blob([String.fromCharCode(0xFEFF) + [header.join(","), ...lines].join("\n")], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = "harvested-jobs.csv"; a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setPageError(
        err instanceof ApiError
          ? `Could not export jobs: ${err.message}`
          : "Could not reach the harvest backend to export. Is it running on the configured API URL?"
      );
    } finally {
      setExporting(false);
    }
  }

  return (
    <main className="ha-main">
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", justifyContent: "space-between", gap: 16, padding: "24px 24px 0" }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, color: C.text }}>Harvested Jobs</h1>
          <p style={{ margin: "4px 0 0", fontSize: 14, color: C.textSoft }}>
            {pageLoading && !facets ? "Loading…" : `${stats.total || 0} harvested posting${(stats.total || 0) === 1 ? "" : "s"} · ${stats.qualified || 0} qualified · ${stats.flagged || 0} flagged`}
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button className="ha-btn ha-btn-secondary" onClick={refresh} disabled={pageLoading} title="Refresh">
            <RefreshCw size={16} className={pageLoading ? "ha-spin" : ""} /> Refresh
          </button>
          <button className="ha-btn ha-btn-secondary" onClick={() => onNavigate("rules")}><SlidersHorizontal size={16} /> Rule Engine</button>
          <a className="ha-btn ha-btn-secondary" href={downloadJsonUrl()} title="GET /download/json — latest combined harvest JSON">
            <FileJson size={16} /> JSON
          </a>
          <a className="ha-btn ha-btn-secondary" href={downloadExcelUrl()} title="GET /download/excel — latest combined harvest Excel">
            <FileSpreadsheet size={16} /> Excel
          </a>
          <button className="ha-btn ha-btn-primary" onClick={exportCsv} disabled={exporting}>
            <Download size={16} /> {exporting ? "Exporting…" : "Export CSV"}
          </button>
        </div>
      </div>
      <div style={{ marginTop: 20, borderBottom: "1px solid " + C.border }} />

      <div style={{ display: "flex", flexDirection: "column", gap: 16, padding: 24 }}>
        {pageError && <div className="ha-errbanner">{pageError}</div>}

        <div className="ha-daterow">
          <span style={{ fontSize: 14, fontWeight: 600, color: C.text }}>Posted between</span>
          <input type="date" className="ha-input" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          <span style={{ color: C.textSoft }}>to</span>
          <input type="date" className="ha-input" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          {(startDate || endDate) && (
            <button className="ha-btn ha-btn-secondary" style={{ height: 38, boxSizing: "border-box", padding: "0 14px" }} onClick={() => { setStartDate(""); setEndDate(""); }}>Clear dates</button>
          )}
          <div style={{ marginLeft: "auto" }}>
            <Select label="Rows per page" value={pageSizeSel} onChange={onPageSizeChange} options={PAGE_SIZE_OPTIONS} />
          </div>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 16 }}>
          <StatCard value={stats.total || 0} label="Total harvested" color={C.accent} />
          <StatCard value={stats.companies || 0} label="Companies sourced" color={C.primary} />
          <StatCard value={stats.pocs || 0} label="POCs identified" color={C.primary} />
        </div>

        <JobsTable
          jobs={pageJobs}
          onView={onView}
          loading={pageLoading}
          serverMode
          serverTotal={pageTotal}
          facets={facets}
          onParamsChange={handleParamsChange}
          serverExtraParams={serverExtraParams}
          minWidth={1410}
          pageSize={Number(pageSizeSel)}
          urlState={urlState}
          emptyMessage="No postings match your search. Run a harvest from the Rule Engine to collect jobs."
        />

        <div style={{ display: "flex", justifyContent: "space-between", padding: "0 4px", fontSize: 12, color: C.textSoft }}>
          <span>Showing {pageJobs.length} of {pageTotal} matching posting{pageTotal === 1 ? "" : "s"} · {stats.qualified || 0} qualified · {stats.flagged || 0} flagged</span>
          <span>{stats.with_email || 0} email · {stats.with_phone || 0} WhatsApp · {stats.with_linkedin || 0} LinkedIn</span>
        </div>
      </div>
    </main>
  );
}

/* ── Run History page ────────────────────────────────────────────────── */
function RunHistoryPage({ runs, loading, error, onRefresh, onNavigate, onView }) {
  // Seed the filter/sort UI from the persistent cache so returning from a run's
  // detail view keeps the view (the run data itself already lives in context).
  const { runHistoryViewRef } = useHarvestData();
  const saved = runHistoryViewRef.current;
  const [filters, setFilters] = useState(saved?.filters || { source: "all", status: "all" });
  const [startDate, setStartDate] = useState(saved?.startDate || "");
  const [endDate, setEndDate] = useState(saved?.endDate || "");
  const [query, setQuery] = useState(saved?.query || "");
  const [sort, setSort] = useState(saved?.sort || { col: "started", dir: "desc" });
  useEffect(() => {
    runHistoryViewRef.current = { filters, startDate, endDate, query, sort };
  }, [runHistoryViewRef, filters, startDate, endDate, query, sort]);

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
    // Prepend a UTF-8 BOM so Excel detects the encoding — without it Excel reads
    // the file as Windows-1252 and turns the "—" placeholder into "â€"".
    const blob = new Blob([String.fromCharCode(0xFEFF) + [header.join(","), ...lines].join("\n")], { type: "text/csv;charset=utf-8;" });
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

        <div className="ha-daterow">
          <span style={{ fontSize: 14, fontWeight: 600, color: C.text }}>Started between</span>
          <input type="date" className="ha-input" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          <span style={{ color: C.textSoft }}>to</span>
          <input type="date" className="ha-input" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          {(startDate || endDate) && (
            <button className="ha-btn ha-btn-secondary" style={{ height: 38, boxSizing: "border-box", padding: "0 14px" }} onClick={() => { setStartDate(""); setEndDate(""); }}>Clear dates</button>
          )}
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 16 }}>
          <StatCard value={counts.totalRuns} label="Total runs" color={C.accent} />
          <StatCard value={counts.totalJobs} label="Jobs harvested" color={C.primary} />
          <StatCard value={counts.directClients} label="Direct clients" color={C.primary} />
        </div>

        <div className="ha-card ha-filterbar" style={{ padding: "16px 20px", gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
          <Select label="Source" value={filters.source} onChange={(v) => setFilters((f) => ({ ...f, source: v }))}
            options={[{ value: "all", label: "All" }, { value: "linkedin", label: "LinkedIn" }, { value: "linkedin feed", label: "LinkedIn Feed" }, { value: "naukri", label: "Naukri" }, { value: "dice", label: "Dice" }]} />
          <Select label="Status" value={filters.status} onChange={(v) => setFilters((f) => ({ ...f, status: v }))}
            options={[{ value: "all", label: "All" }, { value: "success", label: "Success" }, { value: "no_results", label: "No results" }, { value: "failed", label: "Failed" }, { value: "running", label: "Running" }, { value: "stopped", label: "Stopped" }]} />
          <div className="ha-filter-search">
            <Search size={16} />
            <input className="ha-input" value={query}
              onChange={(e) => setQuery(e.target.value)} placeholder="Search run ID…" />
          </div>
        </div>

        <div className="ha-card" style={{ overflow: "hidden" }}>
          <div className="ha-table-scroll">
            <table className="ha-table" style={{ minWidth: 1100 }}>
              <thead className="ha-thead">
                <tr>
                  <SortHeader label="Run ID" col="runId" sort={sort} setSort={setSort} width={190} />
                  <PlainHeader label="Sources" width={170} />
                  <SortHeader label="Status" col="status" sort={sort} setSort={setSort} width={120} />
                  <SortHeader label="Started" col="started" sort={sort} setSort={setSort} width={150} />
                  <SortHeader label="Completed" col="completed" sort={sort} setSort={setSort} width={150} />
                  <SortHeader label="Jobs found" col="jobsFound" sort={sort} setSort={setSort} width={100} />
                  <PlainHeader label="Breakdown" width={220} />
                </tr>
              </thead>
              <tbody>
                {loading && (
                  <tr><td className="ha-td" colSpan={7} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                    Loading run history…
                  </td></tr>
                )}
                {!loading && filtered.map((r) => {
                  const isFeed = r.runType === "feed";
                  return (
                  <tr key={r.runId} className="ha-row">
                    <td className="ha-td"><button className="ha-link" onClick={() => onView(r.runId)}>{r.runId}</button></td>
                    <td className="ha-td">
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                        {(isFeed ? ["LinkedIn Feed"] : r.sources).map((s) => <SourceChip key={s} source={s} />)}
                        {isFeed && <span style={{ fontSize: 11, fontWeight: 700, color: "#6D28D9" }}>· Home Feed leads</span>}
                      </div>
                    </td>
                    <td className="ha-td"><StatusPill status={r.status} /></td>
                    <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>{fmtDate(r.startedAt)}</td>
                    <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>{fmtDate(r.completedAt)}</td>
                    <td className="ha-td" style={{ fontWeight: 600 }}>
                      <AnimatedNumber value={r.jobsFound} />
                      <span style={{ fontSize: 11, fontWeight: 500, color: C.textSoft, marginLeft: 4 }}>{isFeed ? "leads" : "jobs"}</span>
                      {r.status === "running" && <span style={{ color: C.accent, marginLeft: 4 }} title="climbing live">▲</span>}
                    </td>
                    <td className="ha-td">
                      {isFeed ? (
                        <span style={{ fontSize: 12.5, color: C.textSoft }}>IT hiring leads (Home Feed)</span>
                      ) : (
                        <div className="ha-breakdown">
                          <span><b>{r.directClients}</b> DC</span>
                          <span><b>{r.gcc}</b> GCC</span>
                          <span><b>{r.staffingFirms}</b> SF</span>
                          <span><b>{r.ambiguous}</b> Amb</span>
                        </div>
                      )}
                    </td>
                  </tr>
                  );
                })}
                {!loading && !error && filtered.length === 0 && (
                  <tr><td className="ha-td" colSpan={7} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
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
  // LinkedIn Home Feed leads — async run (202 + poll), unlike the synchronous
  // job-board runs above; `async: true` switches SourceRunsPage to the poll flow.
  { key: "feed",     label: "LinkedIn Feed", run: runLinkedinFeedAgent, list: getLinkedinFeedResults, one: getLinkedinFeedResult, async: true },
];

function SourceRunsPage({ harvestRunning, setHarvestRunning }) {
  const [tab, setTab] = useState("linkedin");
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [runMessage, setRunMessage] = useState("");
  const [stallWarn, setStallWarn] = useState(""); // amber "no progress" notice while a run is stalled
  const [viewing, setViewing] = useState(null); // { runId, jobs } | null
  const pollTimer = useRef(null); // async feed run: recursive setTimeout handle
  const stallWatch = useRef(makeStallWatch());
  const syncStallTimer = useRef(null); // synchronous single-source run: one-shot stall notice

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

  useEffect(() => { clearTimeout(pollTimer.current); clearTimeout(syncStallTimer.current); setStallWarn(""); setViewing(null); fetchResults(current); }, [tab]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => { clearTimeout(pollTimer.current); clearTimeout(syncStallTimer.current); }, []); // clear timers on unmount

  // Poll a background feed run to completion (reuses GET /harvest-status; the run
  // carries a job_id). action_required = LinkedIn session not authenticated.
  const pollFeed = (jobId, feedTab) => {
    const tick = async () => {
      try {
        const st = await getHarvestStatus(jobId);
        if (st.status === "running") {
          setRunMessage(st.message || "Harvesting Home Feed — collecting & classifying posts…");
          // Watchdog: warn (without failing) if message + count freeze for 2 min —
          // e.g. the extraction LLM is down and each call is waiting out its timeout.
          const { stalled } = stallWatch.current.note(`${st.message || ""}|${st.combined ?? 0}`);
          setStallWarn(stalled ? STALL_WARN_MSG : "");
          pollTimer.current = setTimeout(tick, 6000);
          return;
        }
        setStallWarn("");
        if (st.status === "action_required") {
          setRunMessage(st.error || st.message || "LinkedIn session not authenticated — connect LinkedIn (Rule Engine) and retry.");
        } else if (st.status === "failed") {
          setRunMessage(`Failed: ${st.error || st.message}`);
        } else {
          setRunMessage(`${st.status === "success" ? "Success" : "No results"} — ${st.combined ?? 0} IT hiring leads extracted (run_id: ${st.run_id}).`);
          fetchResults(feedTab);
        }
      } catch (err) {
        setRunMessage(err instanceof ApiError ? `Failed: ${err.message}` : "Lost contact with the backend while polling.");
      }
      setStallWarn("");
      setRunning(false);
      setHarvestRunning(false);
    };
    tick();
  };

  const handleRun = async () => {
    if (harvestRunning) {
      setRunMessage("Another harvest is already running (Rule Engine or another source tab) — wait for it to finish. Running two at once collides on the shared browser profile and both fail.");
      return;
    }
    const isAsync = current.async;
    const feedTab = current;
    setRunning(true);
    setHarvestRunning(true);
    setStallWarn("");
    stallWatch.current.reset();
    setRunMessage(isAsync
      ? `Starting ${current.label} harvest…`
      : `Running ${current.label} harvest — this calls a synchronous endpoint and can take several minutes. Don't close this tab.`);
    // Synchronous single-source run: the UI awaits one long request, so there's
    // no poll to hang a watchdog on. Arm a one-shot notice that fires if the
    // request runs past the stall threshold, so a hung backend (e.g. LLM down,
    // waiting out its timeout) doesn't just look like a frozen "don't close" line.
    if (!isAsync) {
      clearTimeout(syncStallTimer.current);
      syncStallTimer.current = setTimeout(
        () => setStallWarn("Still running — the LLM server may be slow or down; you can keep waiting or check the server."),
        STALL_WARN_MS,
      );
    }
    try {
      const res = await current.run();
      if (res.status === "failed") {
        setRunMessage(`Failed: ${res.reason || res.message}`);
        clearTimeout(syncStallTimer.current);
        setStallWarn("");
        setRunning(false);
        setHarvestRunning(false);
        return;
      }
      if (isAsync) {
        // 202 accepted — keep controls frozen and poll the background run to done.
        setRunMessage(`Home-Feed harvest started (run_id: ${res.run_id}) — collecting & classifying posts…`);
        pollFeed(res.job_id, feedTab);
        return;
      }
      setRunMessage(`${res.status === "success" ? "Success" : "No results"} — ${res.total_found ?? 0} jobs found (run_id: ${res.run_id}).`);
      fetchResults(current);
      setRunning(false);
      setHarvestRunning(false);
    } catch (err) {
      // 409 = a harvest is already running — keep controls frozen.
      const conflict = err instanceof ApiError && err.status === 409;
      setRunMessage(err instanceof ApiError ? `Failed: ${err.message}` : "Could not reach the harvest backend.");
      setRunning(false);
      setHarvestRunning(conflict);
    } finally {
      clearTimeout(syncStallTimer.current);
      setStallWarn("");
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
            Trigger a single-source harvest and browse its saved results. "LinkedIn Feed" scrolls the
            authenticated Home Feed and extracts genuine IT hiring leads.
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <StopHarvestButton harvestRunning={harvestRunning}
            onStopped={() => setRunMessage("Stop requested — the run will halt shortly and save its jobs. The report email is deferred to the next successful run.")} />
          <button className="ha-btn ha-btn-primary" onClick={handleRun} disabled={running || harvestRunning}
            style={harvestRunning && !running ? { background: "#94A3B8", borderColor: "#94A3B8" } : undefined}
            title={harvestRunning && !running ? "A harvest is already running — controls locked until it finishes" : undefined}>
            {(running || harvestRunning) ? <Loader2 size={16} className="ha-spin" /> : <Play size={16} />}
            {running ? "Running…" : harvestRunning ? "Running elsewhere…" : `Run ${current.label} Only`}
          </button>
        </div>
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
        {stallWarn && (
          <div className="ha-card" style={{ padding: "12px 16px", fontSize: 13.5, color: "#B45309", display: "flex", alignItems: "center", gap: 8 }}>
            <AlertTriangle size={16} /> {stallWarn}
          </div>
        )}
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

/* ── Shared app-data context ──────────────────────────────────────────────────
 * Holds the genuinely app-level state that used to live in the root component:
 * the harvested-jobs and run-history datasets (which also feed the Sidebar
 * badges and the live-run poll) and the cross-page harvestRunning mutex. Pages
 * read it via useHarvestData() instead of receiving it all as props. */
const HarvestDataContext = createContext(null);

export function useHarvestData() {
  const ctx = useContext(HarvestDataContext);
  if (!ctx) throw new Error("useHarvestData must be used within HarvestAgent");
  return ctx;
}

// Map the current pathname to the Sidebar's active nav key.
function activeKeyFromPath(pathname) {
  if (pathname.startsWith("/rules")) return "rules";
  if (pathname.startsWith("/history")) return "history";
  if (pathname.startsWith("/sources")) return "sources";
  if (pathname.startsWith("/leads")) return "leads";
  if (pathname.startsWith("/outreach")) return "outreach";
  return "jobs"; // "/", "/jobs", "/jobs/:id"
}

/* Shared layout for the sidebar-chrome pages (jobs/history/sources/leads/rules/
 * outreach): renders the single Sidebar + the routed page via <Outlet/>. The
 * full-page detail views (JobDetailsView, RunDetailView) render their own root
 * and sit OUTSIDE this layout, exactly as before. */
function AppLayout({ onLogout }) {
  const { jobsTotal, runs } = useHarvestData();
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <div className="ha-root">
      <ThemeStyles />
      <Sidebar
        activePage={activeKeyFromPath(location.pathname)}
        onNavigate={(key) => navigate(`/${key}`)}
        jobsCount={jobsTotal}
        runsCount={runs.length}
        onLogout={onLogout}
      />
      <Outlet />
    </div>
  );
}

// ── Route wrappers: read shared context + wire router navigation into the
// (otherwise unchanged) page components. onView/onNavigate become route pushes.
function JobsRoute() {
  const { pageSizeSel, setPageSizeSel } = useHarvestData();
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <JobsPage
      onNavigate={(key) => navigate(`/${key}`)}
      // Carry the current jobs URL (with its ?company=…&page=… query) as `from`, so
      // the detail view's Back returns to the exact filtered/paged list instead of
      // a bare /jobs that resets everything.
      onView={(dv) => navigate(`/jobs/${encodeURIComponent(dv.job.id)}`, { state: { job: dv.job, from: location.pathname + location.search } })}
      pageSizeSel={pageSizeSel} onPageSizeChange={setPageSizeSel}
      urlState
    />
  );
}

function JobDetailRoute() {
  const { jobId } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  // Prefer the object passed via navigation state (instant); on a refresh/direct
  // link there's no in-memory list anymore (the jobs page is server-paginated),
  // so fetch the single record from GET /jobs/{id}.
  const stateJob = location.state?.job;
  const [fetchedJob, setFetchedJob] = useState(null);
  const [loadState, setLoadState] = useState(stateJob ? "done" : "loading");

  useEffect(() => {
    if (stateJob) return undefined;
    let cancelled = false;
    setLoadState("loading");
    setFetchedJob(null);
    (async () => {
      try {
        const res = await getJob(jobId);
        if (!cancelled) {
          setFetchedJob(mapJobToDetail(mapApiJob(res)));
          setLoadState("done");
        }
      } catch {
        if (!cancelled) setLoadState("error"); // 404 or backend unreachable
      }
    })();
    return () => { cancelled = true; };
  }, [jobId, stateJob]);

  const job = stateJob || fetchedJob;

  if (!job) {
    // Rendered outside the shared layout (no ThemeStyles here), so keep it
    // self-contained with inline styles.
    return (
      <div style={{ minHeight: "100vh", background: "#F8FAFC", padding: 40, color: "#64748B", fontFamily: 'ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif' }}>
        {loadState === "loading" ? "Loading job…" : (
          <div>
            <div style={{ marginBottom: 12 }}>This job isn’t in the current list.</div>
            <button
              onClick={() => navigate("/jobs")}
              style={{ background: "#fff", border: "1px solid #CBD5E1", color: "#2563EB", borderRadius: 8, padding: "8px 16px", fontSize: 14, cursor: "pointer" }}
            >
              Back to Harvested Jobs
            </button>
          </div>
        )}
      </div>
    );
  }
  // Detail views are full-page (own root, no sidebar) — render outside the layout
  // chrome by returning them directly from a route mounted above AppLayout.
  return <JobDetailsView job={job} onBack={() => navigate(location.state?.from || "/jobs")} />;
}

function HistoryRoute() {
  const { runs, runsLoading, runsError, fetchRuns } = useHarvestData();
  const navigate = useNavigate();
  return (
    <RunHistoryPage
      runs={runs} loading={runsLoading} error={runsError} onRefresh={fetchRuns}
      onNavigate={(key) => navigate(`/${key}`)}
      onView={(runId) => navigate(`/history/${encodeURIComponent(runId)}`)}
    />
  );
}

function RunDetailRoute() {
  const { runId } = useParams();
  const navigate = useNavigate();
  return (
    <RunDetailView
      runId={runId}
      onBack={() => navigate("/history")}
      onView={(dv) => navigate(`/jobs/${encodeURIComponent(dv.job.id)}`, { state: { job: dv.job } })}
    />
  );
}

function SourcesRoute() {
  const { harvestRunning, setHarvestRunning } = useHarvestData();
  return <SourceRunsPage harvestRunning={harvestRunning} setHarvestRunning={setHarvestRunning} />;
}

function RulesRoute() {
  const { jobsTotal, runs, refreshAll, harvestRunning, setHarvestRunning } = useHarvestData();
  const navigate = useNavigate();
  return (
    <RuleEngineConfig
      onNavigate={(key) => navigate(`/${key}`)} jobsCount={jobsTotal} runsCount={runs.length}
      onRunComplete={refreshAll} harvestRunning={harvestRunning} setHarvestRunning={setHarvestRunning}
    />
  );
}

function NotFound() {
  const navigate = useNavigate();
  return (
    <main className="ha-main">
      <div style={{ padding: "64px 40px", textAlign: "center", color: "#64748B" }}>
        <div style={{ fontSize: 40, fontWeight: 800, color: "#1E293B" }}>404</div>
        <div style={{ marginTop: 8, marginBottom: 18 }}>That page doesn’t exist.</div>
        <button className="ha-btn ha-btn-secondary" onClick={() => navigate("/jobs")}>Go to Harvested Jobs</button>
      </div>
    </main>
  );
}

/* Page — owns the shared datasets/effects, provides them via context, and maps
 * URLs to pages via React Router. Auth gating stays in App.js (this whole tree
 * only mounts once authenticated), and the <BrowserRouter> lives there too. */
export default function HarvestAgent({ onLogout }) {
  // Shared across Rule Engine + Source Runs so two pages can't both launch a
  // harvest at once — Playwright can't open two browsers on the same
  // persistent Chrome profile, and doing so fails the whole run.
  const [harvestRunning, setHarvestRunning] = useState(false);

  const [jobsTotal, setJobsTotal] = useState(0);
  const [pageSizeSel, setPageSizeSel] = useState("100");

  // Persisted across navigation (this provider never unmounts, unlike the pages
  // under it) so returning from a full-page detail view restores the list
  // instantly instead of refetching + resetting filters. jobsViewRef caches the
  // last Harvested-Jobs page — the mapped rows/total + facets and the URL search
  // they were fetched for; runHistoryViewRef keeps the Run History filter/sort UI
  // state (its data already persists in `runs` above).
  const jobsViewRef = useRef(null);
  const runHistoryViewRef = useRef(null);

  const [runs, setRuns] = useState([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [runsError, setRunsError] = useState("");

  // The jobs dataset itself is no longer loaded here — the Jobs page fetches
  // its own server-side pages (see JobsPage). This only reads the grand total
  // for the Sidebar badge and the Rule Engine header count.
  const fetchJobs = useCallback(async () => {
    try {
      const res = await getJobs({ page: 1, page_size: 1 });
      setJobsTotal(res.total || 0);
    } catch {
      setJobsTotal(0); // badge only — the Jobs page surfaces backend errors
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

  // While any run is in progress, re-fetch the run list every 6s so the Run
  // History page reflects the running row's jobs_found (= live combined_count)
  // climbing. Polling stops automatically once no run is "running".
  const anyRunning = runs.some((r) => r.status === "running");
  useEffect(() => {
    if (!anyRunning) return;
    const id = setInterval(() => { fetchRuns(); }, 6000);
    return () => clearInterval(id);
  }, [anyRunning, fetchRuns]);

  const ctx = useMemo(() => ({
    jobsTotal, fetchJobs,
    runs, runsLoading, runsError, fetchRuns,
    refreshAll, harvestRunning, setHarvestRunning,
    pageSizeSel, setPageSizeSel,
    jobsViewRef, runHistoryViewRef,   // stable refs — a persistent view cache
  }), [
    jobsTotal, fetchJobs,
    runs, runsLoading, runsError, fetchRuns,
    refreshAll, harvestRunning, pageSizeSel,
  ]);

  return (
    <HarvestDataContext.Provider value={ctx}>
      <Routes>
        {/* Full-page detail views — own root/chrome, no shared sidebar (as before). */}
        <Route path="/jobs/:jobId" element={<JobDetailRoute />} />
        <Route path="/history/:runId" element={<RunDetailRoute />} />

        {/* Sidebar-chrome pages share one layout via <Outlet/>. */}
        <Route element={<AppLayout onLogout={onLogout} />}>
          <Route index element={<Navigate to="/jobs" replace />} />
          <Route path="/jobs" element={<JobsRoute />} />
          <Route path="/history" element={<HistoryRoute />} />
          <Route path="/sources" element={<SourcesRoute />} />
          <Route path="/leads" element={<LeadIntelligencePage />} />
          <Route path="/rules" element={<RulesRoute />} />
          <Route path="/outreach" element={<OutreachHistoryPage />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </HarvestDataContext.Provider>
  );
}
