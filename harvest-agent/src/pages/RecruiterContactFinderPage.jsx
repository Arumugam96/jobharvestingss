import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Loader2, Search, Upload, RotateCw, Download, Zap, Mail, FileSpreadsheet,
  CheckCircle2, AlertTriangle, Building2, MapPin, UserSearch, Users, Link2,
  History, Folder, LayoutList, ChevronLeft,
} from "lucide-react";
import {
  finderSearch, finderValidate, finderUpload, getFinderJob, getFinderJobItems,
  retryFinderJob, getFinderUsage, getFinderHistory, getFinderRecentContacts, ApiError,
} from "../api";

/* ── Recruiter Contact Finder — ports the approved artifact design. The whole page
 * is scoped under `.rcf` with an injected <style> so its classes never collide with
 * the app's global `.ha-*` kit. Live data comes from the /recruiter-finder API.      */

const PERSONA_OPTIONS = [
  "Recruiters & Talent Acquisition", "HR & People Ops", "Hiring Managers", "Head of Talent / CHRO",
  "Founder / CEO / Owner", "C-Suite (any)", "VP / Director (any)",
  "Sales & Business Development", "Marketing & Growth", "Revenue / RevOps",
  "Engineering leaders", "Product leaders", "IT / Security",
  "Operations", "Finance / Procurement",
];
const CAP_C = 2 * Math.PI * 40; // gauge circle circumference (r=40)

const CHIP = {
  pending:    ["pending", "Pending"],
  processing: ["proc", "Processing"],
  completed:  ["done", "Completed"],
  not_found:  ["nf", "Not found"],
  ambiguous:  ["amb", "Ambiguous"],
  failed:     ["fail", "Failed"],
  queued:     ["queue", "Queued · tomorrow"],
};

function Chip({ status }) {
  const [cls, label] = CHIP[status] || CHIP.pending;
  return (
    <span className={"chip " + cls}>
      {status === "processing" ? <Loader2 size={11} className="ha-spin" /> : <span className="d" />}
      {label}
    </span>
  );
}

function Seg({ value, onChange, options }) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button key={o.value} type="button" className={"seg-btn" + (value === o.value ? " active" : "")}
          onClick={() => onChange(o.value)}>
          {o.icon}{o.label}
        </button>
      ))}
    </div>
  );
}

function PersonaSelect({ value, onChange }) {
  return <Dropdown value={value} onChange={onChange} options={PERSONA_OPTIONS} icon={<UserSearch size={15} />} minWidth={220} />;
}

// Styled dropdown (replaces the native <select> so the options panel aligns with the design).
function Dropdown({ value, onChange, options, icon, minWidth }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);
  return (
    <div className="dd" ref={ref} style={minWidth ? { minWidth } : undefined}>
      <button type="button" className="input sel dd-btn" onClick={() => setOpen((o) => !o)} aria-haspopup="listbox" aria-expanded={open}>
        {icon}<span className="dd-val">{value}</span>
      </button>
      {open && (
        <div className="dd-panel" role="listbox">
          {options.map((o) => (
            <div key={o} role="option" aria-selected={o === value}
              className={"dd-opt" + (o === value ? " sel" : "")}
              onMouseDown={(e) => { e.preventDefault(); onChange(o); setOpen(false); }}>{o}</div>
          ))}
        </div>
      )}
    </div>
  );
}

function RecentContacts({ recent }) {
  return (
    <>
      <div className="gridtool">
        <h3><span className="ic" style={{ color: "var(--primary)" }}><Users size={16} /></span>Recent contacts</h3>
        <span className="count-pill num">{recent.length}</span>
        <span style={{ flex: 1 }} />
        <span style={{ fontSize: 12, color: "var(--ink-soft)" }}>saved to your Recruiters list</span>
      </div>
      <div className="table-scroll" style={{ maxHeight: 360 }}>
        <table className="rt">
          <thead><tr><th>Contact</th><th>Title</th><th>Company</th><th>Email</th><th>Phone</th></tr></thead>
          <tbody>{recent.map((c, i) => (
            <tr key={i}>
              <td className="contact"><span className="nm">{c.name || "—"}</span></td>
              <td>{c.title || "—"}</td>
              <td className="co"><b>{c.company || "—"}</b>{c.domain ? <span>{c.domain}</span> : null}</td>
              <td className="email">{c.email ? <span className="addr">{c.email}</span> : <span className="muted">—</span>}</td>
              <td className="phone">{c.phone || <span className="none">—</span>}</td>
            </tr>))}</tbody>
        </table>
      </div>
    </>
  );
}

function downloadCsv(rows, filename) {
  const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const header = ["Company", "Contact", "Title", "Email", "Email status", "Phone", "Confidence", "Status"];
  const lines = [header.join(",")].concat(
    rows.map((r) => [r.company, r.contact_name, r.contact_title, r.email, r.email_status, r.phone, r.confidence, r.status].map(esc).join(",")),
  );
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

export default function RecruiterContactFinderPage() {
  const [tab, setTab] = useState("single");
  const [usage, setUsage] = useState(null);
  const [collapsed, setCollapsed] = useState(false);
  const rcfRef = useRef(null);

  const loadUsage = useCallback(async () => {
    try { setUsage(await getFinderUsage()); } catch { /* keep prior */ }
  }, []);
  useEffect(() => { loadUsage(); }, [loadUsage]);

  // The page is its own scroll container (the app's .ha-main has overflow-x:hidden,
  // which breaks window-relative position:sticky), so the stat band can stick + collapse.
  return (
    <main className="ha-main" style={{ paddingBottom: 0 }}>
      <div className="rcf" ref={rcfRef}
        onScroll={(e) => { const y = e.currentTarget.scrollTop; setCollapsed((c) => (c ? y > 24 : y > 110)); }}
        style={{ padding: "0 24px 44px", height: "100vh", overflowY: "auto", overflowX: "hidden" }}>
        <style dangerouslySetInnerHTML={{ __html: CSS }} />

        <div className="pagehead">
          <div>
            <h1>Recruiter Contact Finder</h1>
            <p className="sub">Pull verified recruiter &amp; B2B contacts from Apollo — look one up, or upload a CSV/Excel
              to enrich in bulk with live progress. Runs on a separate per-workspace daily budget.</p>
          </div>
        </div>

        <StatBand usage={usage} collapsed={collapsed} />

        <section className="card">
          <div className="tabs" role="tablist">
            <button className="tab" role="tab" aria-selected={tab === "single"} onClick={() => setTab("single")}>
              <Search size={15} /> Single search
            </button>
            <button className="tab" role="tab" aria-selected={tab === "bulk"} onClick={() => setTab("bulk")}>
              <Upload size={15} /> Bulk upload <span className="kbd">CSV · XLSX</span>
            </button>
          </div>
          {tab === "single" ? <SinglePanel onSpent={loadUsage} /> : <BulkPanel onSpent={loadUsage} />}
        </section>
      </div>
    </main>
  );
}

// ── Stat band: usage card (left) + credit gauge (right), collapses on scroll ───
function StatBand({ usage, collapsed }) {
  const cap = usage?.cap ?? 50;
  const used = usage?.used ?? 0;
  const left = usage?.remaining != null ? usage.remaining : (cap > 0 ? Math.max(0, cap - used) : "∞");
  const history = usage?.history || [];
  const ratio = cap > 0 ? Math.min(1, used / cap) : 0;
  const dash = `${(ratio * CAP_C).toFixed(1)} ${CAP_C.toFixed(1)}`;
  const stroke = ratio < 0.6 ? "var(--good)" : ratio < 0.9 ? "var(--accent)" : "var(--bad)";
  const pctW = `${Math.round(ratio * 100)}%`;

  return (
    <div className={"statband" + (collapsed ? " collapsed" : "")}>
      <div className="sb-mini" aria-hidden="true">
        <span className="sb-mini-l"><Zap size={14} style={{ color: "var(--accent)" }} /> Apollo credits · today</span>
        <span className="sb-mini-bar"><i style={{ width: pctW }} /></span>
        <span className="sb-mini-r num"><b>{used}</b>/{cap} used · <b>{left}</b> left · this workspace</span>
      </div>

      <div className="sb-cards">
        <section className="card usage-card">
          <div className="card-head">
            <h2><Zap size={15} style={{ color: "var(--accent)" }} /> Daily Apollo usage</h2>
            <span className="spacer" />
            <span className="count-pill" style={{ color: "var(--ink-soft)", background: "var(--line-soft)", borderColor: "transparent" }}>
              This workspace · last 14 days
            </span>
          </div>
          <div className="usage-grid">
            <div className="chart">
              <div className="chart-top"><span className="t">Credits spent per day</span>
                <span className="r">Daily cap&nbsp;<b style={{ color: "var(--warn)" }}>{cap}</b></span></div>
              <div className="bars">
                <div className="capline"><span>{cap}</span></div>
                {history.map((d, i) => (
                  <div key={d.date} className={"bar" + (i === history.length - 1 ? " today" : "")}
                    title={`${d.date}: ${d.count}`}
                    style={{ height: `${cap > 0 ? Math.max(3, Math.min(100, (d.count / cap) * 100)) : 3}%` }} />
                ))}
                {history.length === 0 && <span style={{ fontSize: 12, color: "var(--ink-faint)", alignSelf: "center" }}>No usage yet</span>}
              </div>
              <div className="bar-x"><span>14 days ago</span><span>Today</span></div>
            </div>
            <div className="ustats">
              <div className="ustat"><div className="k">Used today</div><div className="v num">{used} <small>/ {cap}</small></div></div>
              <div className="ustat"><div className="k">Remaining</div><div className="v num">{left}<small> credits</small></div></div>
            </div>
          </div>
        </section>

        <div className="credit">
          <div className="card-head"><h2><Zap size={15} style={{ color: "var(--accent)" }} /> Today's budget</h2>
            <span className="spacer" />
            <span className="count-pill" style={{ color: "var(--ink-soft)", background: "var(--line-soft)", borderColor: "transparent" }}>Per workspace</span>
          </div>
          <div className="credit-body">
            <div className="gauge-wrap">
              <svg className="gauge" viewBox="0 0 96 96" aria-hidden="true">
                <circle className="gauge-track" cx="48" cy="48" r="40" />
                <circle className="gauge-fill" cx="48" cy="48" r="40" style={{ stroke }} strokeDasharray={dash} />
              </svg>
              <div className="gauge-center"><span className="big num">{used}</span><span className="cap num">of {cap}</span></div>
            </div>
            <div className="credit-meta">
              <span className="lbl">Credits left</span>
              <span className="bigleft num">{left}</span>
              <div className="credit-tags"><span className="ctag sep"><span className="dot" />Separate budget</span></div>
            </div>
          </div>
          <div className="footnote" style={{ justifyContent: "flex-start", padding: "10px 16px 12px" }}>Resets at 00:00 UTC · isolated from the harvest Apollo pool</div>
        </div>
      </div>
    </div>
  );
}

// ── Single lookup ─────────────────────────────────────────────────────────────
function SinglePanel({ onSpent }) {
  const [target, setTarget] = useState("company"); // company | people
  const [reveal, setReveal] = useState("email");
  const [company, setCompany] = useState("");
  const [location, setLocation] = useState("");
  const [persona, setPersona] = useState(PERSONA_OPTIONS[0]);
  const [personName, setPersonName] = useState("");
  const [linkedinUrl, setLinkedinUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [recent, setRecent] = useState([]);
  useEffect(() => {
    (async () => { try { const r = await getFinderRecentContacts(); setRecent(r.contacts || []); } catch { /* ignore */ } })();
  }, []);

  const run = async () => {
    setLoading(true); setError(""); setResult(null);
    try {
      const body = target === "company"
        ? { company, location, persona, reveal }
        : { company, reveal, person_name: personName, linkedin_url: linkedinUrl };
      setResult(await finderSearch(body));
      onSpent();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the harvest backend.");
    } finally { setLoading(false); }
  };

  const revealOpts = [
    { value: "email", label: "Email", icon: <Mail size={14} /> },
    { value: "phone", label: "Phone" },
    { value: "both", label: "Both" },
  ];

  return (
    <div className="panel">
      <div className="opts-row">
        <div className="optgrp"><span className="olbl">Look up</span>
          <Seg value={target} onChange={setTarget} options={[
            { value: "company", label: "Recruiters at a company", icon: <Building2 size={14} /> },
            { value: "people", label: "A specific person", icon: <Users size={14} /> },
          ]} />
        </div>
        <div className="optgrp"><span className="olbl">Reveal</span><Seg value={reveal} onChange={setReveal} options={revealOpts} /></div>
      </div>

      {target === "company" ? (
        <div className="fields mode-company">
          <div className="field"><label>Company</label>
            <div className="input"><Building2 size={15} /><input value={company} onChange={(e) => setCompany(e.target.value)} placeholder="e.g. Stripe" /></div></div>
          <div className="field"><label>Location <span className="opt">(optional)</span></label>
            <div className="input"><MapPin size={15} /><input value={location} onChange={(e) => setLocation(e.target.value)} placeholder="City or country" /></div></div>
          <div className="field"><label>Who to look up</label><PersonaSelect value={persona} onChange={setPersona} /></div>
          <button className="btn btn-primary" onClick={run} disabled={loading}>
            {loading ? <Loader2 size={16} className="ha-spin" /> : <Search size={16} />} Find contacts
          </button>
        </div>
      ) : (
        <div className="fields mode-people">
          <div className="field"><label>Full name</label>
            <div className="input"><Users size={15} /><input value={personName} onChange={(e) => setPersonName(e.target.value)} placeholder="e.g. Sarah Chen" /></div></div>
          <div className="field"><label>Company <span className="opt">(with name)</span></label>
            <div className="input"><Building2 size={15} /><input value={company} onChange={(e) => setCompany(e.target.value)} placeholder="e.g. Stripe" /></div></div>
          <div className="field"><label>LinkedIn URL <span className="opt">(works on its own)</span></label>
            <div className="input"><Link2 size={15} /><input value={linkedinUrl} onChange={(e) => setLinkedinUrl(e.target.value)} placeholder="linkedin.com/in/…" /></div></div>
          <button className="btn btn-primary" onClick={run} disabled={loading}>
            {loading ? <Loader2 size={16} className="ha-spin" /> : <Zap size={16} />} Enrich
          </button>
        </div>
      )}

      <div className="form-foot"><Zap size={15} />
        <span><b>1 credit</b> per revealed email ({reveal === "both" ? "2 for both" : reveal === "phone" ? "1 for phone" : "email"}) · cached contacts are free</span>
      </div>

      {error && <div className="valnote" style={{ marginTop: 16, background: "var(--bad-bg)", borderColor: "transparent" }}><AlertTriangle className="ic" style={{ color: "var(--bad)" }} /><div className="t">{error}</div></div>}
      {result && <SingleResult result={result} company={company} />}
      {recent.length > 0 && <RecentContacts recent={recent} />}
    </div>
  );
}

function SingleResult({ result, company }) {
  if (result.status === "unconfigured" || result.status === "over_budget") {
    return <div className="valnote" style={{ marginTop: 16 }}><AlertTriangle className="ic" /><div className="t">{result.message}</div></div>;
  }
  const tone = result.status === "completed" ? "done" : result.status === "ambiguous" ? "amb" : result.status === "failed" ? "fail" : "nf";
  const label = {
    completed: "Found", ambiguous: "Multiple matches — refine", not_found: "No contact found", failed: result.error || "Lookup failed",
  }[result.status] || result.status;
  return (
    <>
      <div className="gridtool"><h3><span className="ic" style={{ color: "var(--primary)" }}><Users size={16} /></span>Result</h3></div>
      <div className="table-scroll" style={{ maxHeight: "none" }}>
        <table className="rt">
          <thead><tr><th>Contact</th><th>Title</th><th>Company</th><th>Email</th><th>Phone</th><th>Status</th></tr></thead>
          <tbody>
            <tr>
              <td className="contact">{result.contact_name ? <span className="nm">{result.contact_name}</span> : <span className="intent">—</span>}</td>
              <td>{result.contact_title || "—"}</td>
              <td className="co"><b>{company || "—"}</b>{result.company_domain ? <span>{result.company_domain}</span> : null}</td>
              <td className="email">{result.email ? <span className="addr">{result.email}</span> : <span className="muted">{label}</span>}</td>
              <td className="phone">{result.phone || <span className="none">—</span>}</td>
              <td><span className={"chip " + tone}><span className="d" />{label}</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </>
  );
}

// ── Run-history drawer (Folders / Rows) ───────────────────────────────────────
// Job status → chip tone + label. Re-uses the per-item chip tones already in CSS.
const JCHIP = {
  queued: ["queue", "Queued"], running: ["proc", "Running"], completed: ["done", "Done"],
  partial: ["nf", "Partial"], failed: ["fail", "Failed"],
};
// Bucket a run by age (from created_at) — the real "folder" dimension we have
// without a backend folder concept: recency the user actually scans by.
function bucketOf(iso) {
  if (!iso) return "Earlier";
  const d = new Date(iso), now = new Date();
  if (d.toDateString() === now.toDateString()) return "Today";
  const days = (now - d) / 86400000;
  if (days < 7) return "This week";
  if (days < 30) return "This month";
  return "Earlier";
}
function relTimeFrom(iso) {
  if (!iso) return "";
  const mins = Math.max(0, Math.floor((Date.now() - new Date(iso)) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return mins + "m";
  const h = Math.floor(mins / 60); if (h < 24) return h + "h";
  const d = Math.floor(h / 24); if (d < 7) return d + "d";
  if (d < 30) return Math.floor(d / 7) + "w";
  return Math.floor(d / 30) + "mo";
}
// Per-company avatar tint (stable hash → palette) and per-row severity stripe class.
const FAV_COLORS = ["#2563EB", "#7C3AED", "#059669", "#D97706", "#DC2626", "#0891B2", "#DB2777", "#4F46E5"];
function favColor(name) { let h = 0; for (const c of (name || "?")) h = (h * 31 + c.charCodeAt(0)) >>> 0; return FAV_COLORS[h % FAV_COLORS.length]; }
function rowTone(s) {
  return s === "completed" ? "r-good" : s === "failed" ? "r-bad" : s === "not_found" ? "r-warn"
    : s === "ambiguous" ? "r-viol" : s === "processing" ? "r-pri" : "";
}

// Run view — the hero/runbar + counters + records table for ONE job. Shared by the
// live "Running" pane and the History detail pane (image-2 layout). `onReset` is
// only passed in the running pane (New-upload makes no sense on a past run).
function RunView({ job, items, filter, setFilter, running, mapping, onRetry, onExport, onReset }) {
  const filtered = useMemo(() => {
    if (filter === "successful") return items.filter((i) => i.status === "completed");
    if (filter === "ambiguous") return items.filter((i) => i.status === "ambiguous");
    if (filter === "unsuccessful") return items.filter((i) => i.status === "not_found" || i.status === "failed");
    return items;
  }, [items, filter]);
  const total = job.total || 0;
  const pct = job.progress || 0;
  const R = 34, C = 2 * Math.PI * R;
  const hasUnsuccessful = job.failed > 0 || job.not_found > 0 || job.queued > 0;

  return (
    <>
      {/* Hero card — ring + stat strip (the artifact's "image 2" header) */}
      <div className="rv-hero">
        <div className="rv-ring">
          <svg viewBox="0 0 80 80" aria-hidden="true">
            <defs>
              <linearGradient id="rvgrad" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stopColor="#2563EB" /><stop offset="1" stopColor="#7C3AED" />
              </linearGradient>
            </defs>
            <circle className="trk" cx="40" cy="40" r={R} />
            <circle className="fl" cx="40" cy="40" r={R} strokeDasharray={`${(pct / 100 * C).toFixed(1)} ${C.toFixed(1)}`} />
          </svg>
          <div className="rv-ring-c"><span className="pct num">{pct}</span><span className="lbl">% done</span></div>
        </div>
        <div className="rv-mid">
          <div className="rv-fn">
            <FileSpreadsheet size={17} />
            <span className="nm">{job.filename || "upload.csv"}</span>
            {running
              ? <span className="rv-badge live"><span className="d" />LIVE</span>
              : <span className="rv-badge done"><CheckCircle2 size={12} />DONE</span>}
          </div>
          <div className="rv-meta">
            <span>{total} rows</span><span className="dot" />
            {job.persona ? <><span>{job.persona}</span><span className="dot" /></> : null}
            <span>reveal: {job.reveal || "email"}</span><span className="dot" />
            <span>{running ? (job.message || "working…") : `finished ${relTimeFrom(job.completed_at || job.created_at)} ago`}</span>
          </div>
          <div className="rv-strip">
            <div className="rv-stat"><div className="n good num">{job.completed} <small>/ {total}</small></div><div className="l">Enriched</div></div>
            <div className="rv-stat"><div className="n viol num">{job.ambiguous}</div><div className="l">Ambiguous</div></div>
            <div className="rv-stat"><div className="n warn num">{job.not_found}</div><div className="l">Not found</div></div>
            <div className="rv-stat"><div className="n bad num">{job.failed}</div><div className="l">Failed</div></div>
            <div className="rv-stat"><div className="n num">{job.queued}</div><div className="l">Queued</div></div>
          </div>
        </div>
        <div className="rv-acts">
          {!running && onRetry && hasUnsuccessful && <button className="btn sm btn-ghost" onClick={onRetry}><RotateCw size={14} /> Retry unsuccessful</button>}
          <button className="btn sm btn-ghost" onClick={onExport}><Download size={14} /> Export CSV</button>
          {!running && onReset && <button className="btn sm btn-ghost" onClick={onReset}><Upload size={14} /> New upload</button>}
        </div>
      </div>

      {mapping && (
        <div className="rv-map">
          <span className="rv-map-l">Column map</span>
          {Object.entries(mapping).map(([field, col]) => (
            <span key={field} className={"mapchip " + (field === "company" ? "req" : col ? "ok" : "off")}>
              {field === "company" ? <span className="star">★</span> : col ? <CheckCircle2 className="ic" /> : null}
              {field} → {col || "not found"}
            </span>
          ))}
        </div>
      )}

      <div className="rv-tablehead">
        <h3><FileSpreadsheet size={15} /> Records <span className="count-pill num">{items.length}</span></h3>
        <div className="resbar">
          {[["all", "All"], ["successful", "Successful"], ["ambiguous", "Ambiguous"], ["unsuccessful", "Unsuccessful"]].map(([k, l]) => (
            <button key={k} className={"fchip" + (filter === k ? " active" : "")} onClick={() => setFilter(k)}>{l}</button>
          ))}
        </div>
      </div>

      <div className="table-scroll rv-tscroll">
        <table className="rt rv-table">
          <thead><tr><th style={{ width: 40 }}>#</th><th>Company</th><th>Contact</th><th>Title</th><th>Email</th><th>Phone</th><th>Status</th></tr></thead>
          <tbody>
            {filtered.map((it) => (
              <tr key={it.id} className={rowTone(it.status)}>
                <td className="idx">{it.row_index + 1}</td>
                <td className="co">
                  <span className="co-main"><span className="fav" style={{ background: favColor(it.company) }}>{(it.company || "?").charAt(0).toUpperCase()}</span><b>{it.company}</b></span>
                  {it.location ? <span className="co-sub">{it.location}</span> : null}
                </td>
                <td className="contact">{it.contact_name ? <span className="nm">{it.contact_name}</span> : <span className="intent">{it.person_name || "⌕ persona"}</span>}</td>
                <td>{it.contact_title || <span className="none">—</span>}</td>
                <td className="email">{it.email ? <span className="addr">{it.email}</span> : <span className="muted">{it.error ? "error" : "—"}</span>}</td>
                <td className="phone">{it.phone || <span className="none">—</span>}</td>
                <td><Chip status={it.status} /></td>
              </tr>
            ))}
            {filtered.length === 0 && <tr><td colSpan={7} style={{ textAlign: "center", color: "var(--ink-faint)", padding: 22 }}>No records in this view.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ── Bulk upload + live enrichment ─────────────────────────────────────────────
function BulkPanel({ onSpent }) {
  const [file, setFile] = useState(null);
  const [preview, setPreview] = useState(null);
  const [validating, setValidating] = useState(false);
  const [persona, setPersona] = useState(PERSONA_OPTIONS[0]);
  const [reveal, setReveal] = useState("email");
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState(null);
  const [job, setJob] = useState(null);
  const [items, setItems] = useState([]);
  const [starting, setStarting] = useState(false);
  const [filter, setFilter] = useState("all");
  const [history, setHistory] = useState([]);
  // Bulk sub-view: "running" (current/live run + upload flow) vs "history" (browse past runs)
  const [bulkTab, setBulkTab] = useState("running");
  const [histMode, setHistMode] = useState("folders"); // folders (tiles) | rows
  const [histQuery, setHistQuery] = useState("");
  const [openFolder, setOpenFolder] = useState(null);  // drilled-into recency bucket
  // Selected past run shown in the history detail pane (its own job + items, separate
  // from the live run so browsing history never disturbs a run in flight).
  const [selHistId, setSelHistId] = useState(null);
  const [histJob, setHistJob] = useState(null);
  const [histItems, setHistItems] = useState([]);
  const [histFilter, setHistFilter] = useState("all");
  const [histLoading, setHistLoading] = useState(false);
  const pollRef = useRef(null);
  const fileRef = useRef(null);

  const loadHistory = useCallback(async () => {
    try { const h = await getFinderHistory(); const jobs = (h && h.jobs) || []; setHistory(jobs); return jobs; }
    catch { return []; }
  }, []);

  const stopPoll = () => { if (pollRef.current) { clearTimeout(pollRef.current); pollRef.current = null; } };
  useEffect(() => () => stopPoll(), []);

  const onFile = async (f) => {
    setError(""); setPreview(null); setFile(f);
    if (!f) return;
    setValidating(true);
    try { setPreview(await finderValidate(f)); }
    catch (err) { setError(err instanceof ApiError ? err.message : "Could not read that file."); setFile(null); }
    finally { setValidating(false); }
  };

  const tick = useCallback(async (id) => {
    try {
      const j = await getFinderJob(id);
      setJob(j);
      const it = await getFinderJobItems(id, { pageSize: 500 });
      setItems(it.items || []);
      onSpent();
      if (j.status === "running" || j.status === "queued") pollRef.current = setTimeout(() => tick(id), 6000);
      else stopPoll();
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) pollRef.current = setTimeout(() => tick(id), 10000);
      else stopPoll();
    }
  }, [onSpent]);

  // On mount, load history (feeds the drawer) and surface the latest run so the
  // Bulk page shows real DB data and resumes a live run.
  useEffect(() => {
    (async () => {
      const jobs = await loadHistory();
      // Only resume into the Running pane if the latest run is actually live;
      // otherwise Running stays blank (finished runs live in History).
      const latest = jobs[0];
      if (latest && (latest.status === "running" || latest.status === "queued")) {
        setJobId(latest.job_id); tick(latest.job_id);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load a past run into the history DETAIL pane (its own job + items).
  const openHist = useCallback(async (id) => {
    if (!id) return;
    setSelHistId(id); setHistLoading(true); setHistFilter("all");
    try {
      const j = await getFinderJob(id);
      const it = await getFinderJobItems(id, { pageSize: 500 });
      setHistJob(j); setHistItems(it.items || []);
    } catch { setHistJob(null); setHistItems([]); }
    finally { setHistLoading(false); }
  }, []);

  // Switch to the History tab; auto-select the most recent run the first time.
  const goHistory = () => {
    setBulkTab("history");
    if (!selHistId && history.length) openHist(history[0].job_id);
  };

  const histRetry = async () => {
    if (!selHistId) return;
    try { await retryFinderJob(selHistId); openHist(selHistId); loadHistory(); }
    catch { /* surfaced on next poll */ }
  };

  const start = async () => {
    if (!file) return;
    setStarting(true); setError("");
    try { const res = await finderUpload(file, { persona, reveal }); setBulkTab("running"); setJobId(res.job_id); setJob(null); setItems([]); tick(res.job_id); loadHistory(); }
    catch (err) { setError(err instanceof ApiError ? err.message : "Could not start enrichment."); }
    finally { setStarting(false); }
  };

  const retry = async () => { if (!jobId) return; try { await retryFinderJob(jobId); tick(jobId); } catch (err) { setError(err instanceof ApiError ? err.message : "Retry failed."); } };
  const reset = () => { stopPoll(); setFile(null); setPreview(null); setJobId(null); setJob(null); setItems([]); setError(""); if (fileRef.current) fileRef.current.value = ""; };

  const running = job && (job.status === "running" || job.status === "queued");
  const sum = preview?.summary;
  const revealOpts = [{ value: "email", label: "Email", icon: <Mail size={14} /> }, { value: "phone", label: "Phone" }, { value: "both", label: "Both" }];
  // stepper state
  const st2 = !!preview, st3done = job && !running, st3active = running || (preview && !job);

  // ── History list: search → flat matches; else Folders (recency tiles → drill-in) or Rows ──
  const BUCKET_ORDER = ["Today", "This week", "This month", "Earlier"];
  const histItem = (j) => {
    const pct = j.total ? Math.round((j.completed / j.total) * 100) : 0;
    const [cls, label] = JCHIP[j.status] || JCHIP.queued;
    return (
      <div key={j.job_id} className={"hist-item" + (selHistId === j.job_id ? " sel" : "")}
        onClick={() => openHist(j.job_id)} role="button" tabIndex={0}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openHist(j.job_id); } }}>
        <div className="hi-top">
          <div className="hi-name"><FileSpreadsheet size={13} />{j.filename || "upload.csv"}</div>
          <div className="hi-time num">{relTimeFrom(j.created_at)}</div>
        </div>
        <div className="hi-bar"><i style={{ width: pct + "%" }} /></div>
        <div className="hi-meta">
          <span className="num">{j.completed} / {j.total} enriched</span>
          <span className={"chip " + cls}><span className="d" />{label}</span>
        </div>
      </div>
    );
  };
  const histListNode = () => {
    if (!history.length) return <div className="hist-empty">No runs yet.<br />Upload a file to start your first enrichment.</div>;
    const q = histQuery.trim().toLowerCase();
    const match = history.filter((j) => !q || (j.filename || "upload.csv").toLowerCase().includes(q));
    if (q) return match.length ? match.map(histItem) : <div className="hist-empty">No runs match “{histQuery}”.</div>;
    if (histMode === "rows") return match.map(histItem);
    if (!openFolder) {
      const by = {};
      match.forEach((j) => { const b = bucketOf(j.created_at); (by[b] = by[b] || []).push(j); });
      const buckets = BUCKET_ORDER.filter((b) => by[b]);
      return (
        <div className="fgrid">
          {buckets.map((b) => {
            const runs = by[b]; const en = runs.reduce((a, r) => a + (r.completed || 0), 0);
            return (
              <button type="button" key={b} className="ftile" onClick={() => setOpenFolder(b)}>
                <span className="ft-ic"><Folder size={20} /></span>
                <span className="ft-name">{b}</span>
                <span className="ft-meta num">{runs.length} run{runs.length > 1 ? "s" : ""} · {en} enriched</span>
              </button>
            );
          })}
        </div>
      );
    }
    const runs = match.filter((j) => bucketOf(j.created_at) === openFolder);
    return (
      <>
        <button type="button" className="fback" onClick={() => setOpenFolder(null)}><ChevronLeft size={14} /> All folders</button>
        <div className="hl-sec">{openFolder}<span className="c num">{runs.length}</span></div>
        {runs.map(histItem)}
      </>
    );
  };

  return (
    <div className="panel bulk">
      <div className="bulk-head">
        <div className="bulk-seg" data-active={bulkTab}>
          <span className="seg-ind" />
          <button type="button" className="bseg" data-tab="history" onClick={goHistory} aria-pressed={bulkTab === "history"}>
            <History size={15} /> History{history.length > 0 && <span className="segn num">{history.length}</span>}
          </button>
          <button type="button" className={"bseg" + (running ? "" : " idle")} data-tab="running" onClick={() => setBulkTab("running")} aria-pressed={bulkTab === "running"}>
            <span className={"rdot" + (running ? " live" : "")} /> Running
            {running ? <span className="livetag">LIVE</span> : <span className="idletag">idle</span>}
          </button>
        </div>
      </div>

      <div className="bulk-body">
        {bulkTab === "running" ? (
          <div className="pane">
      <div className="stepper">
        <div className={"step done"}><span className="dot"><CheckCircle2 size={14} /></span>Upload</div>
        <div className={"step-line" + (st2 ? " done" : "")} />
        <div className={"step" + (st2 ? " done" : "")}><span className="dot">{st2 ? <CheckCircle2 size={14} /> : "2"}</span>Validate &amp; map</div>
        <div className={"step-line" + (st3done ? " done" : "")} />
        <div className={"step" + (st3done ? " done" : st3active ? " active" : "")}><span className="dot">{st3done ? <CheckCircle2 size={14} /> : "3"}</span>Enrich</div>
        <div className="step-line" />
        <div className={"step" + (st3done ? " active" : "")}><span className="dot">4</span>Results</div>
      </div>

      {!jobId && (
        <>
          <div className="drop-wrap" onClick={() => fileRef.current && fileRef.current.click()}
            style={{ border: "2px dashed var(--pale-br)", background: "var(--pale)", borderRadius: 12, padding: "28px 20px", textAlign: "center", cursor: "pointer" }}>
            <input ref={fileRef} type="file" accept=".csv,.xlsx,.xls" style={{ display: "none" }} onChange={(e) => onFile(e.target.files && e.target.files[0])} />
            <div style={{ width: 48, height: 48, borderRadius: 13, background: "var(--glass-solid)", border: "1px solid var(--pale-br)", display: "grid", placeItems: "center", color: "var(--primary)", margin: "0 auto 8px", boxShadow: "var(--shadow-sm)" }}><Upload size={24} /></div>
            <h3 style={{ margin: "2px 0 0", fontSize: 14, fontWeight: 700 }}>Drop a CSV or Excel file here</h3>
            <p style={{ margin: 0, color: "var(--ink-soft)", fontSize: 12.5 }}>or <span style={{ color: "var(--primary)", fontWeight: 700, textDecoration: "underline" }}>browse your computer</span> — needs a <b>Company</b> column; Person / LinkedIn / Location optional</p>
          </div>

          {validating && <div className="form-foot" style={{ marginTop: 14 }}><Loader2 size={15} className="ha-spin" /> Validating…</div>}

          {preview && (
            <div className="summary" style={{ marginTop: 16 }}>
              <div className="filechip">
                <div className="fic"><FileSpreadsheet size={18} /></div>
                <div><div className="fname">{file?.name}</div><div className="fmeta num">{sum.total_rows} rows parsed</div></div>
                <span className="replace" onClick={reset}>Replace file</span>
              </div>
              <div>
                <div className="block-lbl">Column mapping <span style={{ color: "var(--good)", textTransform: "none", letterSpacing: 0, fontWeight: 600 }}>· auto-detected</span></div>
                <div className="maprow">
                  {Object.entries(preview.mapping).map(([field, col]) => (
                    <span key={field} className={"mapchip " + (field === "company" ? "req" : col ? "ok" : "off")}>
                      {field === "company" ? <span className="star">★</span> : col ? <CheckCircle2 className="ic" /> : null}
                      {field} → {col || "not found"}
                    </span>
                  ))}
                </div>
              </div>
              <div>
                <div className="block-lbl">Validation</div>
                <div className="valrow">
                  <span className="valchip ready"><CheckCircle2 className="ic sm" /><span className="n">{sum.ready}</span> ready</span>
                  {sum.missing_company > 0 && <span className="valchip miss"><AlertTriangle className="ic sm" /><span className="n">{sum.missing_company}</span> missing company</span>}
                  {sum.duplicate > 0 && <span className="valchip dup"><span className="n">{sum.duplicate}</span> duplicates</span>}
                  {sum.truncated > 0 && <span className="valchip miss"><span className="n">{sum.truncated}</span> beyond row limit</span>}
                </div>
              </div>
              <div className="valnote">
                <AlertTriangle className="ic" />
                <div className="t">Data is <b>sanitized</b> before any Apollo call (trimmed, de-duplicated, URLs normalized). <b>{sum.ready}</b> records are ready to enrich; anything missing a company is excluded. If the daily budget runs out mid-run, the rest queue for tomorrow.</div>
              </div>

              <div className="opts-row" style={{ margin: 0 }}>
                <div className="optgrp"><span className="olbl">Persona (company-only rows)</span><PersonaSelect value={persona} onChange={setPersona} /></div>
                <div className="optgrp"><span className="olbl">Reveal</span><Seg value={reveal} onChange={setReveal} options={revealOpts} /></div>
                <button className="btn btn-primary" onClick={start} disabled={starting || sum.ready === 0}>
                  {starting ? <Loader2 size={16} className="ha-spin" /> : <Zap size={16} />} Start enrichment ({sum.ready})
                </button>
              </div>
            </div>
          )}
        </>
      )}

      {error && <div className="valnote" style={{ marginTop: 14, background: "var(--bad-bg)", borderColor: "transparent" }}><AlertTriangle className="ic" style={{ color: "var(--bad)" }} /><div className="t">{error}</div></div>}

      {jobId && job ? (
        <RunView job={job} items={items} filter={filter} setFilter={setFilter} running={running}
          mapping={preview?.mapping} onRetry={retry} onExport={() => downloadCsv(items, `contacts_${jobId}.csv`)} onReset={reset} />
      ) : null}
          </div>
        ) : (
          <div className="pane">
            <div className="hist-view">
              <aside className="hist-left">
                <div className="hist-search">
                  <Search size={15} />
                  <input value={histQuery} onChange={(e) => { setHistQuery(e.target.value); setOpenFolder(null); }}
                    placeholder="Search runs…" aria-label="Search runs" />
                </div>
                <div className="hist-mode" role="tablist" aria-label="History view">
                  <button role="tab" aria-selected={histMode === "folders"} className={histMode === "folders" ? "on" : ""}
                    onClick={() => { setHistMode("folders"); setOpenFolder(null); }}><Folder size={13} /> Folders</button>
                  <button role="tab" aria-selected={histMode === "rows"} className={histMode === "rows" ? "on" : ""}
                    onClick={() => { setHistMode("rows"); setOpenFolder(null); }}><LayoutList size={13} /> Rows</button>
                </div>
                <div className="hist-list">{histListNode()}</div>
              </aside>
              <div className="hist-detail">
                {histLoading
                  ? <div className="hist-loading"><Loader2 size={22} className="ha-spin" /> Loading run…</div>
                  : histJob
                    ? <RunView job={histJob} items={histItems} filter={histFilter} setFilter={setHistFilter} running={false}
                        onRetry={histRetry} onExport={() => downloadCsv(histItems, `contacts_${selHistId}.csv`)} />
                    : <div className="hist-detail-empty"><History size={26} /><div>Select a run on the left to see its contacts.</div></div>}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* Scoped artifact styling — every selector is prefixed with `.rcf` so it can't touch
 * the app's global `.ha-*` kit. Light-theme tokens (the app renders light). */
const CSS = `
.rcf{--bg-1:#EEF2F9;--ink:#1E293B;--ink-soft:#64748B;--ink-faint:#94A3B8;--line:#E2E8F0;--line-soft:#EEF2F7;--glass:rgba(255,255,255,.72);--glass-solid:rgba(255,255,255,.93);--glass-br:rgba(255,255,255,.65);--primary:#2563EB;--primary-ink:#1E40AF;--pale:#EFF6FF;--pale-br:#DBE7FF;--accent:#F59E0B;--good:#059669;--good-bg:rgba(5,150,105,.10);--warn:#B45309;--warn-bg:rgba(217,119,6,.13);--warn-br:rgba(217,119,6,.32);--viol:#7C3AED;--viol-bg:rgba(124,58,237,.12);--bad:#DC2626;--bad-bg:rgba(220,38,38,.10);--shadow:0 10px 30px -14px rgba(15,23,42,.28);--shadow-sm:0 2px 8px -4px rgba(15,23,42,.2);--radius:14px;color:var(--ink);display:flex;flex-direction:column;gap:18px;font-size:14px;line-height:1.45}
.rcf *{box-sizing:border-box}
.rcf .num{font-variant-numeric:tabular-nums}
@keyframes rcfpulse{0%,100%{opacity:1}50%{opacity:.35}}
.rcf .ic{width:18px;height:18px;flex:none}.rcf .ic.sm{width:15px;height:15px}
.rcf .pagehead h1{margin:0;font-size:21px;font-weight:800;letter-spacing:-.025em}
.rcf .pagehead .sub{margin:6px 0 0;color:var(--ink-soft);font-size:13.5px;max-width:64ch}
.rcf .statband{position:sticky;top:0;z-index:5;padding:4px 0;background:linear-gradient(180deg,var(--bg-1) 74%,transparent);backdrop-filter:blur(7px);-webkit-backdrop-filter:blur(7px)}
.rcf .sb-cards{display:flex;gap:18px;align-items:stretch;height:clamp(158px,23vh,230px);opacity:1;overflow:hidden;transition:height .45s cubic-bezier(.4,0,.2,1),opacity .3s ease,transform .45s ease}
.rcf .sb-cards>.usage-card{flex:1;min-width:0;display:flex;flex-direction:column}
.rcf .sb-cards>.usage-card,.rcf .sb-cards>.credit{height:100%;overflow:hidden}
.rcf .sb-cards .card-head{padding:9px 14px}.rcf .sb-cards .card-head h2{font-size:13px}
.rcf .sb-cards .usage-grid{flex:1;min-height:0;padding:10px 14px;gap:14px;grid-template-columns:1fr 150px}
.rcf .sb-cards .bars{flex:1;min-height:34px}
.rcf .sb-cards .ustat{padding:5px 10px}.rcf .sb-cards .ustat .k{font-size:9.5px}.rcf .sb-cards .ustat .v{font-size:15px;margin-top:1px}
.rcf .sb-cards .footnote{display:none}
.rcf .sb-cards .credit-body{padding:8px 16px;gap:16px}
.rcf .sb-cards .gauge-wrap,.rcf .sb-cards .gauge{width:74px;height:74px}
.rcf .sb-cards .gauge-center .big{font-size:19px}.rcf .sb-cards .gauge-center .cap{font-size:9.5px}.rcf .sb-cards .bigleft{font-size:25px}
.rcf .sb-mini{display:flex;align-items:center;gap:14px;max-height:0;opacity:0;overflow:hidden;transition:max-height .4s cubic-bezier(.4,0,.2,1),opacity .3s ease}
.rcf .sb-mini-l{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;font-weight:700;color:var(--ink);white-space:nowrap}
.rcf .sb-mini-bar{flex:1;max-width:300px;height:8px;border-radius:999px;background:var(--line);overflow:hidden}
.rcf .sb-mini-bar i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,var(--accent),var(--bad));transition:width .5s ease}
.rcf .sb-mini-r{font-size:12px;color:var(--ink-soft);font-weight:600;white-space:nowrap}.rcf .sb-mini-r b{color:var(--ink)}
.rcf .statband.collapsed .sb-cards{height:0;opacity:0;transform:translateY(-8px)}
.rcf .statband.collapsed .sb-mini{max-height:54px;opacity:1;padding:8px 2px}
.rcf .card{background:var(--glass-solid);border:1px solid var(--glass-br);border-radius:var(--radius);box-shadow:var(--shadow)}
.rcf .card-head{display:flex;align-items:center;gap:12px;padding:15px 18px;border-bottom:1px solid var(--line)}
.rcf .card-head h2{margin:0;font-size:14.5px;font-weight:700;display:flex;align-items:center;gap:9px}
.rcf .card-head .spacer{flex:1}
.rcf .count-pill{font-size:11.5px;font-weight:700;color:var(--primary-ink);background:var(--pale);border:1px solid var(--pale-br);padding:3px 10px;border-radius:999px;white-space:nowrap}
.rcf .credit{background:var(--glass);border:1px solid var(--glass-br);border-radius:16px;box-shadow:var(--shadow);display:flex;flex-direction:column;min-width:300px;overflow:hidden}
.rcf .credit .card-head{border-bottom:1px solid var(--line)}
.rcf .credit-body{flex:1;display:flex;align-items:center;justify-content:center;gap:24px;padding:20px 18px}
.rcf .bigleft{font-size:34px;font-weight:800;letter-spacing:-.03em;line-height:1;color:var(--good)}
.rcf .gauge-wrap{position:relative;width:96px;height:96px;flex:none}
.rcf .gauge{width:96px;height:96px;transform:rotate(-90deg)}
.rcf .gauge-track{fill:none;stroke:var(--line);stroke-width:11}
.rcf .gauge-fill{fill:none;stroke:var(--accent);stroke-width:11;stroke-linecap:round;transition:stroke-dasharray .5s ease,stroke .5s ease}
.rcf .gauge-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.rcf .gauge-center .big{font-size:25px;font-weight:800;letter-spacing:-.03em;line-height:1}
.rcf .gauge-center .cap{font-size:11px;color:var(--ink-soft);font-weight:600;margin-top:2px}
.rcf .credit-meta{display:flex;flex-direction:column;gap:3px;min-width:0}
.rcf .credit-meta .lbl{font-size:10.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-faint)}
.rcf .credit-tags{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.rcf .ctag{font-size:10.5px;font-weight:600;color:var(--primary-ink);background:var(--pale);border:1px solid var(--pale-br);padding:3px 8px;border-radius:999px;display:inline-flex;align-items:center;gap:5px}
.rcf .ctag .dot{width:6px;height:6px;border-radius:50%;background:var(--primary)}
.rcf .usage-grid{display:grid;grid-template-columns:1fr 150px;gap:18px;padding:18px;align-items:stretch}
.rcf .chart{display:flex;flex-direction:column;gap:8px;min-width:0}
.rcf .chart-top{display:flex;align-items:baseline;justify-content:space-between;gap:10px;font-size:11px}
.rcf .chart-top .t{font-weight:700}.rcf .chart-top .r{color:var(--ink-soft)}
.rcf .bars{position:relative;height:90px;display:flex;align-items:flex-end;gap:5px;padding-top:8px;border-bottom:1px solid var(--line)}
.rcf .capline{position:absolute;left:0;right:0;top:8px;border-top:1px dashed var(--warn-br)}
.rcf .capline span{position:absolute;right:0;top:-8px;font-size:10px;font-weight:700;color:var(--warn);background:var(--glass-solid);padding:0 4px}
.rcf .bar{flex:1;border-radius:4px 4px 0 0;background:#93B4FB;opacity:.9}
.rcf .bar.today{background:var(--accent);opacity:1}
.rcf .bar-x{display:flex;justify-content:space-between;font-size:10px;color:var(--ink-faint);font-weight:600}
.rcf .ustats{display:flex;flex-direction:column;gap:10px;justify-content:center}
.rcf .ustat{background:var(--glass);border:1px solid var(--line);border-radius:11px;padding:10px 12px}
.rcf .ustat .k{font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-faint)}
.rcf .ustat .v{font-size:20px;font-weight:800;letter-spacing:-.02em;margin-top:2px}.rcf .ustat .v small{font-size:11px;font-weight:600;color:var(--ink-soft)}
.rcf .footnote{display:flex;align-items:center;gap:8px;color:var(--ink-faint);font-size:11.5px}
.rcf .tabs{display:flex;gap:4px;padding:12px 14px 0}
.rcf .tab{display:flex;align-items:center;gap:8px;padding:10px 16px;border:none;background:none;color:var(--ink-soft);font:inherit;font-weight:600;font-size:13px;cursor:pointer;border-radius:10px 10px 0 0;position:relative}
.rcf .tab:hover{color:var(--ink);background:var(--line-soft)}
.rcf .tab[aria-selected="true"]{color:var(--primary);background:var(--pale)}
.rcf .tab[aria-selected="true"]::after{content:"";position:absolute;left:12px;right:12px;bottom:-1px;height:2px;background:var(--primary);border-radius:2px}
.rcf .tab .kbd{font-size:10px;color:var(--ink-faint);font-weight:700;background:var(--line-soft);padding:1px 5px;border-radius:5px}
.rcf .panel{padding:18px}
.rcf .opts-row{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:16px}
.rcf .optgrp{display:flex;align-items:center;gap:10px}
.rcf .optgrp .olbl{font-size:10.5px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-faint)}
.rcf .seg{display:inline-flex;background:var(--line-soft);border:1px solid var(--line);border-radius:10px;padding:3px;gap:2px}
.rcf .seg-btn{border:none;background:none;font:inherit;font-weight:600;font-size:12.5px;color:var(--ink-soft);padding:7px 13px;border-radius:7px;cursor:pointer;display:inline-flex;align-items:center;gap:7px;white-space:nowrap}
.rcf .seg-btn:hover{color:var(--ink)}
.rcf .seg-btn.active{background:var(--glass-solid);color:var(--primary);box-shadow:var(--shadow-sm)}
.rcf .fields{display:grid;gap:12px;align-items:end}
.rcf .fields.mode-company{grid-template-columns:1.3fr 1fr 1.25fr auto}
.rcf .fields.mode-people{grid-template-columns:1.1fr 1.1fr 1.4fr auto}
.rcf .field{display:flex;flex-direction:column;gap:6px;min-width:0}
.rcf .field label{font-size:10.5px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-soft)}
.rcf .field label .opt{color:var(--ink-faint);font-weight:600}
.rcf .input{display:flex;align-items:center;gap:8px;background:var(--glass);border:1px solid var(--line);border-radius:10px;padding:0 11px;height:40px}
.rcf .input:focus-within{border-color:var(--primary);box-shadow:0 0 0 3px var(--pale)}
.rcf .input svg{color:var(--ink-faint);flex:none}
.rcf .input input,.rcf .input select{border:none;background:none;outline:none;font:inherit;color:var(--ink);width:100%;height:100%}
.rcf .input select{cursor:pointer;appearance:none;padding-right:16px}
.rcf .input input::placeholder{color:var(--ink-faint)}
.rcf .sel{position:relative}
.rcf .sel::after{content:"";position:absolute;right:12px;top:50%;width:7px;height:7px;border-right:2px solid var(--ink-faint);border-bottom:2px solid var(--ink-faint);transform:translateY(-70%) rotate(45deg);pointer-events:none}
.rcf .btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:40px;padding:0 18px;border-radius:10px;border:1px solid var(--line);background:var(--glass);color:var(--ink);font:inherit;font-weight:600;font-size:13px;cursor:pointer;white-space:nowrap}
.rcf .btn:hover{border-color:var(--ink-faint)}
.rcf .btn-primary{background:linear-gradient(135deg,var(--primary),var(--primary-ink));border-color:transparent;color:#fff;box-shadow:0 8px 20px -10px rgba(37,99,235,.9)}
.rcf .btn-ghost{background:none}
.rcf .btn.sm{height:34px;padding:0 13px;font-size:12.5px}
.rcf .btn[disabled]{opacity:.5;cursor:not-allowed}
.rcf .form-foot{display:flex;align-items:center;gap:8px;margin-top:14px;color:var(--ink-soft);font-size:12px}
.rcf .form-foot svg{color:var(--accent);flex:none}.rcf .form-foot b{color:var(--ink)}
.rcf .stepper{display:flex;align-items:center;gap:0;margin-bottom:18px;flex-wrap:wrap}
.rcf .step{display:flex;align-items:center;gap:9px;font-size:12.5px;font-weight:600;color:var(--ink-faint)}
.rcf .step .dot{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;background:var(--line-soft);color:var(--ink-faint);font-weight:800;font-size:12px;border:1px solid var(--line)}
.rcf .step.done{color:var(--good)}.rcf .step.done .dot{background:var(--good-bg);color:var(--good);border-color:transparent}
.rcf .step.active{color:var(--primary)}.rcf .step.active .dot{background:var(--primary);color:#fff;border-color:transparent;box-shadow:0 0 0 4px var(--pale)}
.rcf .step-line{flex:1;min-width:24px;height:2px;background:var(--line);margin:0 12px;border-radius:2px}.rcf .step-line.done{background:var(--good)}
.rcf .summary{display:flex;flex-direction:column;gap:14px;background:var(--glass);border:1px solid var(--line);border-radius:12px;padding:16px}
.rcf .filechip{display:flex;align-items:center;gap:12px}
.rcf .filechip .fic{width:38px;height:38px;border-radius:9px;background:var(--good-bg);color:var(--good);display:grid;place-items:center;flex:none}
.rcf .filechip .fname{font-weight:700;font-size:13.5px}.rcf .filechip .fmeta{font-size:11.5px;color:var(--ink-soft)}
.rcf .filechip .replace{margin-left:auto;font-size:12px;font-weight:700;color:var(--primary);cursor:pointer;background:var(--pale);border:1px solid var(--pale-br);padding:6px 11px;border-radius:9px}
.rcf .block-lbl{font-size:10.5px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-faint);margin-bottom:8px;display:flex;align-items:center;gap:7px}
.rcf .maprow{display:flex;flex-wrap:wrap;gap:8px}
.rcf .mapchip{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:600;padding:6px 11px;border-radius:999px;border:1px solid var(--line);background:var(--glass-solid);text-transform:capitalize}
.rcf .mapchip.ok{color:var(--good);background:var(--good-bg);border-color:transparent}
.rcf .mapchip.req{color:var(--primary-ink);background:var(--pale);border-color:var(--pale-br)}
.rcf .mapchip.off{color:var(--ink-faint)}
.rcf .mapchip .ic{width:14px;height:14px}.rcf .mapchip .star{color:var(--bad);font-weight:800}
.rcf .valrow{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.rcf .valchip{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;font-weight:700;padding:6px 12px;border-radius:10px}
.rcf .valchip.ready{color:var(--good);background:var(--good-bg)}
.rcf .valchip.miss{color:var(--warn);background:var(--warn-bg)}
.rcf .valchip.dup{color:var(--ink-soft);background:var(--line-soft)}
.rcf .valnote{display:flex;align-items:flex-start;gap:10px;padding:12px 14px;border-radius:11px;background:var(--warn-bg);border:1px solid var(--warn-br)}
.rcf .valnote .ic{color:var(--warn);flex:none;margin-top:1px}
.rcf .valnote .t{font-size:12.5px;line-height:1.5}.rcf .valnote .t b{color:var(--warn)}
.rcf .runbar{display:flex;flex-direction:column;gap:12px;padding:4px 0}
.rcf .runbar-top{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.rcf .livechip{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:700;padding:5px 11px;border-radius:999px;background:var(--pale);color:var(--primary-ink);border:1px solid var(--pale-br)}
.rcf .livechip .d{width:7px;height:7px;border-radius:50%;background:var(--primary);animation:rcfpulse 1.3s ease-in-out infinite}
.rcf .runbar-top .cur{font-size:13px;color:var(--ink);font-weight:600;display:flex;align-items:center;gap:8px;min-width:0}
.rcf .runbar-top .cur svg{color:var(--primary);flex:none}.rcf .runbar-top .cur b{font-weight:700}
.rcf .runbar-top .acts{margin-left:auto;display:flex;gap:8px}
.rcf .bigbar{height:12px;border-radius:999px;background:var(--line);overflow:hidden}
.rcf .bigbar i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,var(--primary),#7C3AED);transition:width .4s ease}
.rcf .bigbar-foot{display:flex;justify-content:space-between;font-size:11.5px;color:var(--ink-soft);font-weight:600}.rcf .bigbar-foot b{color:var(--ink)}
.rcf .counters{display:flex;flex-wrap:wrap;align-items:center;gap:10px 20px;margin:14px 0 2px;padding:12px 16px;background:var(--glass);border:1px solid var(--line);border-radius:12px}
.rcf .ct{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;color:var(--ink-soft);font-weight:600}
.rcf .ct b{font-size:16px;font-weight:800;color:var(--ink);font-variant-numeric:tabular-nums}
.rcf .ct .d{width:8px;height:8px;border-radius:50%;background:var(--ink-faint)}
.rcf .ct .vr{width:1px;height:16px;background:var(--line)}
.rcf .ct.done .d{background:var(--good)}.rcf .ct.done b{color:var(--good)}
.rcf .ct.nf .d{background:var(--warn)}.rcf .ct.nf b{color:var(--warn)}
.rcf .ct.amb .d{background:var(--viol)}.rcf .ct.amb b{color:var(--viol)}
.rcf .ct.fail .d{background:var(--bad)}.rcf .ct.fail b{color:var(--bad)}
.rcf .ct.queue .d{background:var(--ink-faint)}.rcf .ct.queue b{color:var(--ink-soft)}
.rcf .gridtool{display:flex;align-items:center;gap:12px;margin:18px 0 10px}
.rcf .gridtool h3{margin:0;font-size:14px;font-weight:700;display:flex;align-items:center;gap:8px}
.rcf .gridtool h3 .ic{color:var(--primary);display:inline-flex}
.rcf .resbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.rcf .fchip{font-size:12px;font-weight:700;padding:7px 13px;border-radius:999px;border:1px solid var(--line);background:var(--glass);color:var(--ink-soft);cursor:pointer}
.rcf .fchip.active{background:var(--pale);color:var(--primary);border-color:var(--pale-br)}
.rcf .table-scroll{overflow:auto;max-height:560px;border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow-sm)}
.rcf table.rt{width:100%;border-collapse:collapse;font-size:13px;min-width:760px}
.rcf .rt thead th{position:sticky;top:0;z-index:1;background:var(--glass-solid);text-align:left;font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-faint);padding:10px 14px;border-bottom:1px solid var(--line);white-space:nowrap}
.rcf .rt tbody td{padding:10px 14px;border-bottom:1px solid var(--line-soft);vertical-align:middle}
.rcf .rt tbody tr:last-child td{border-bottom:none}
.rcf .rt tbody tr:nth-child(even){background:color-mix(in srgb,var(--line-soft) 55%,transparent)}
.rcf .rt tbody td:not(:last-child),.rcf .rt thead th:not(:last-child){border-right:1px solid var(--line-soft)}
.rcf .idx{color:var(--ink-faint);font-variant-numeric:tabular-nums;font-weight:600}
.rcf .co{display:flex;flex-direction:column}.rcf .co b{font-weight:600}.rcf .co span{font-size:11px;color:var(--ink-faint)}
.rcf .contact .nm{font-weight:600}.rcf .contact .intent{color:var(--ink-faint);font-style:italic;font-size:12.5px}
.rcf .email .addr{font-weight:500}.rcf .email .muted{color:var(--ink-faint)}
.rcf .phone{font-variant-numeric:tabular-nums}.rcf .phone .none{color:var(--ink-faint)}
.rcf .chip{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:700;padding:4px 9px;border-radius:999px;white-space:nowrap}
.rcf .chip .d{width:6px;height:6px;border-radius:50%}
.rcf .chip.pending{color:var(--ink-soft);background:var(--line-soft)}.rcf .chip.pending .d{background:var(--ink-faint)}
.rcf .chip.proc{color:var(--primary);background:var(--pale)}
.rcf .chip.done{color:var(--good);background:var(--good-bg)}.rcf .chip.done .d{background:var(--good)}
.rcf .chip.nf{color:var(--warn);background:var(--warn-bg)}.rcf .chip.nf .d{background:var(--warn)}
.rcf .chip.amb{color:var(--viol);background:var(--viol-bg)}.rcf .chip.amb .d{background:var(--viol)}
.rcf .chip.fail{color:var(--bad);background:var(--bad-bg)}.rcf .chip.fail .d{background:var(--bad)}
.rcf .chip.queue{color:var(--ink-soft);background:var(--line-soft);border:1px dashed var(--line)}
.rcf .dd{position:relative}
.rcf .dd-btn{width:100%;justify-content:flex-start;cursor:pointer;text-align:left;gap:8px;font:inherit;color:var(--ink);padding:0 26px 0 11px}
.rcf .dd-val{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rcf .dd-panel{position:absolute;top:calc(100% + 6px);left:0;right:0;z-index:40;background:var(--glass-solid);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);max-height:300px;overflow:auto;padding:6px}
.rcf .dd-opt{padding:8px 11px;border-radius:7px;font-size:13px;color:var(--ink);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rcf .dd-opt:hover{background:var(--line-soft)}
.rcf .dd-opt.sel{background:var(--pale);color:var(--primary);font-weight:600}
.rcf .hist-item{padding:10px 12px;border-radius:10px;margin:3px 0;background:var(--glass-solid);border:1px solid transparent;cursor:pointer;transition:all .15s}
.rcf .hist-item:hover{border-color:var(--line);box-shadow:var(--shadow-sm);transform:translateX(2px)}
.rcf .hist-item.sel{background:var(--pale);border-color:var(--pale-br)}
.rcf .hi-top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:7px}
.rcf .hi-name{display:flex;align-items:center;gap:6px;font-size:12.5px;font-weight:600;color:var(--ink);min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rcf .hi-name svg{color:var(--primary);flex:none}
.rcf .hi-time{color:var(--ink-faint);font-size:11px;flex:none}
.rcf .hi-bar{height:4px;border-radius:999px;background:var(--line);overflow:hidden;margin:6px 0}
.rcf .hi-bar i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,var(--primary),var(--viol))}
.rcf .hi-meta{display:flex;align-items:center;justify-content:space-between;color:var(--ink-soft);font-size:11px}
.rcf .chip.proc .d{background:var(--primary)}
/* bulk: sliding History/Running toggle + panes */
.rcf .bulk-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}
.rcf .bulk-seg{position:relative;display:grid;grid-template-columns:1fr 1fr;background:var(--line-soft);border:1px solid var(--line);border-radius:12px;padding:4px;width:min(360px,100%)}
.rcf .seg-ind{position:absolute;top:4px;bottom:4px;left:4px;width:calc(50% - 4px);border-radius:9px;background:var(--glass-solid);box-shadow:var(--shadow-sm);transition:transform .3s cubic-bezier(.4,0,.2,1)}
.rcf .bulk-seg[data-active="running"] .seg-ind{transform:translateX(100%)}
.rcf .bseg{position:relative;z-index:1;display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:8px 12px;border:none;background:none;border-radius:9px;color:var(--ink-soft);font:inherit;font-weight:600;font-size:12.5px;cursor:pointer;transition:color .2s}
.rcf .bseg:hover{color:var(--ink)}
.rcf .bulk-seg[data-active="history"] .bseg[data-tab="history"],.rcf .bulk-seg[data-active="running"] .bseg[data-tab="running"]{color:var(--primary)}
.rcf .bseg svg{flex:none}
.rcf .segn{padding:1px 7px;border-radius:999px;background:var(--line);color:var(--ink-soft);font-size:10.5px;font-weight:700}
.rcf .bulk-seg[data-active="history"] .bseg[data-tab="history"] .segn{background:var(--pale);color:var(--primary-ink)}
.rcf .rdot{width:7px;height:7px;border-radius:50%;background:var(--ink-faint)}
.rcf .rdot.live{background:var(--good);box-shadow:0 0 0 3px var(--good-bg);animation:rcfpulse 1.3s ease-in-out infinite}
.rcf .livetag{font-size:9.5px;font-weight:800;letter-spacing:.05em;color:var(--good);background:var(--good-bg);padding:1px 6px;border-radius:5px}
.rcf .idletag{font-size:9.5px;font-weight:700;letter-spacing:.04em;color:var(--ink-faint);background:var(--line-soft);padding:1px 6px;border-radius:5px}
.rcf .bulk-body{display:flex;flex-direction:column}
.rcf .pane{display:flex;flex-direction:column;gap:14px}
/* history split */
.rcf .hist-view{display:flex;gap:16px;align-items:stretch;min-height:440px}
.rcf .hist-left{flex:0 0 300px;display:flex;flex-direction:column;background:var(--glass);border:1px solid var(--line);border-radius:14px;overflow:hidden;box-shadow:var(--shadow-sm)}
.rcf .hist-search{padding:12px 12px 8px;position:relative}
.rcf .hist-search svg{position:absolute;left:23px;top:calc(50% + 2px);transform:translateY(-50%);color:var(--ink-faint)}
.rcf .hist-search input{width:100%;background:var(--glass-solid);border:1px solid var(--line);color:var(--ink);padding:9px 10px 9px 33px;border-radius:9px;font:inherit;font-size:12.5px}
.rcf .hist-search input::placeholder{color:var(--ink-faint)}
.rcf .hist-search input:focus{outline:0;border-color:var(--primary);box-shadow:0 0 0 3px var(--pale)}
.rcf .hist-mode{display:flex;gap:4px;padding:2px 12px 10px;border-bottom:1px solid var(--line)}
.rcf .hist-mode button{flex:1;display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:7px;border:none;background:none;border-radius:8px;color:var(--ink-soft);font:inherit;font-weight:600;font-size:12px;cursor:pointer;transition:all .15s}
.rcf .hist-mode button:hover{color:var(--ink);background:var(--line-soft)}
.rcf .hist-mode button.on{background:var(--glass-solid);color:var(--primary);box-shadow:var(--shadow-sm)}
.rcf .hist-mode button svg{flex:none}
.rcf .hist-list{flex:1;overflow-y:auto;padding:8px 10px 14px;max-height:640px}
/* square folder tiles */
.rcf .fgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:8px 4px}
.rcf .ftile{aspect-ratio:1/1;display:flex;flex-direction:column;gap:6px;padding:13px 12px;border-radius:14px;background:var(--glass-solid);border:1px solid var(--line);cursor:pointer;transition:all .15s;text-align:left;font:inherit}
.rcf .ftile:hover{border-color:var(--pale-br);box-shadow:var(--shadow-sm);transform:translateY(-2px)}
.rcf .ft-ic{width:40px;height:40px;border-radius:11px;display:grid;place-items:center;color:var(--primary);background:var(--pale);border:1px solid var(--pale-br)}
.rcf .ft-name{font-size:12.5px;font-weight:700;color:var(--ink);line-height:1.3;margin-top:auto}
.rcf .ft-meta{font-size:10.5px;color:var(--ink-soft)}
.rcf .fback{display:inline-flex;align-items:center;gap:6px;margin:4px 4px 2px;padding:6px 10px;border:1px solid var(--line);background:var(--glass-solid);border-radius:8px;color:var(--ink-soft);font:inherit;font-weight:600;font-size:12px;cursor:pointer}
.rcf .fback:hover{color:var(--ink);background:var(--line-soft)}
.rcf .hl-sec{padding:12px 8px 6px;font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-faint);font-weight:700;display:flex;align-items:center;gap:7px}
.rcf .hl-sec .c{margin-left:auto;color:var(--ink-faint)}
.rcf .hist-detail{flex:1;min-width:0;display:flex;flex-direction:column;gap:14px;min-width:0}
.rcf .hist-loading,.rcf .hist-detail-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;flex:1;min-height:320px;color:var(--ink-faint);border:1px dashed var(--line);border-radius:14px;padding:40px;text-align:center}
.rcf .hist-loading{flex-direction:row}
.rcf .bseg.idle{color:var(--ink-faint)}
/* fluid pane transition */
@keyframes rcfPaneIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.rcf .pane{animation:rcfPaneIn .3s cubic-bezier(.32,.72,0,1)}
/* run hero (ring + stat strip) */
.rcf .rv-hero{display:grid;grid-template-columns:auto 1fr auto;gap:22px;align-items:center;padding:18px 20px;background:linear-gradient(135deg,var(--glass-solid),var(--glass));border:1px solid var(--pale-br);border-radius:16px;box-shadow:var(--shadow)}
.rcf .rv-ring{position:relative;width:84px;height:84px;flex:none}
.rcf .rv-ring svg{width:84px;height:84px;transform:rotate(-90deg)}
.rcf .rv-ring .trk{fill:none;stroke:var(--line);stroke-width:7}
.rcf .rv-ring .fl{fill:none;stroke:url(#rvgrad);stroke-width:7;stroke-linecap:round;transition:stroke-dasharray .5s ease}
.rcf .rv-ring-c{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.rcf .rv-ring-c .pct{font-size:22px;font-weight:800;letter-spacing:-.02em;line-height:1}
.rcf .rv-ring-c .lbl{font-size:8.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-faint);margin-top:2px}
.rcf .rv-mid{min-width:0}
.rcf .rv-fn{display:flex;align-items:center;gap:9px;font-size:16px;font-weight:700;letter-spacing:-.01em;margin-bottom:5px}
.rcf .rv-fn svg{color:var(--primary);flex:none}
.rcf .rv-fn .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rcf .rv-badge{display:inline-flex;align-items:center;gap:5px;padding:3px 8px;border-radius:999px;font-size:10px;font-weight:700;letter-spacing:.04em;flex:none}
.rcf .rv-badge.live{background:var(--pale);color:var(--primary);border:1px solid var(--pale-br)}
.rcf .rv-badge.live .d{width:5px;height:5px;border-radius:50%;background:var(--primary);animation:rcfpulse 1.3s ease-in-out infinite}
.rcf .rv-badge.done{background:var(--good-bg);color:var(--good)}
.rcf .rv-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;color:var(--ink-soft);font-size:12px;margin-bottom:12px}
.rcf .rv-meta .dot{width:3px;height:3px;border-radius:50%;background:var(--ink-faint);flex:none}
.rcf .rv-strip{display:flex;gap:18px;flex-wrap:wrap}
.rcf .rv-stat{display:flex;flex-direction:column;gap:1px;padding-right:18px;border-right:1px solid var(--line)}
.rcf .rv-stat:last-child{border-right:0;padding-right:0}
.rcf .rv-stat .n{font-size:18px;font-weight:800;letter-spacing:-.02em;line-height:1.1;color:var(--ink)}
.rcf .rv-stat .n small{font-size:11px;font-weight:600;color:var(--ink-faint)}
.rcf .rv-stat .n.good{color:var(--good)}.rcf .rv-stat .n.warn{color:var(--warn)}.rcf .rv-stat .n.bad{color:var(--bad)}.rcf .rv-stat .n.viol{color:var(--viol)}
.rcf .rv-stat .l{font-size:9.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--ink-faint)}
.rcf .rv-acts{display:flex;flex-direction:column;gap:8px;align-items:stretch}
.rcf .rv-map{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 14px;background:var(--glass);border:1px solid var(--line);border-radius:12px}
.rcf .rv-map-l{font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-faint);font-weight:700;padding-right:6px;border-right:1px solid var(--line)}
.rcf .rv-tablehead{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:2px}
.rcf .rv-tablehead h3{margin:0;font-size:14px;font-weight:700;display:flex;align-items:center;gap:8px}
.rcf .rv-tablehead h3 svg{color:var(--primary)}
.rcf .rv-table .co-main{display:flex;align-items:center;gap:8px}
.rcf .rv-table .fav{width:22px;height:22px;border-radius:6px;display:grid;place-items:center;font-size:11px;font-weight:800;color:#fff;flex:none}
.rcf .rv-table .co-sub{display:block;font-size:11px;color:var(--ink-faint);margin-left:30px}
.rcf .rv-table tbody td:first-child{position:relative}
.rcf .rv-table tr.r-good td:first-child::before,.rcf .rv-table tr.r-bad td:first-child::before,.rcf .rv-table tr.r-warn td:first-child::before,.rcf .rv-table tr.r-viol td:first-child::before,.rcf .rv-table tr.r-pri td:first-child::before{content:"";position:absolute;left:0;top:6px;bottom:6px;width:3px;border-radius:0 2px 2px 0}
.rcf .rv-table tr.r-good td:first-child::before{background:var(--good)}
.rcf .rv-table tr.r-bad td:first-child::before{background:var(--bad)}
.rcf .rv-table tr.r-warn td:first-child::before{background:var(--warn)}
.rcf .rv-table tr.r-viol td:first-child::before{background:var(--viol)}
.rcf .rv-table tr.r-pri td:first-child::before{background:var(--primary)}
@media(max-width:860px){.rcf .rv-hero{grid-template-columns:auto 1fr}.rcf .rv-acts{grid-column:1 / -1;flex-direction:row;flex-wrap:wrap}}
@media (max-width:860px){.rcf .hist-view{flex-direction:column}.rcf .hist-left{flex:0 0 auto;max-height:260px}.rcf .hist-list{max-height:200px}}
@media (max-width:980px){.rcf .sb-cards{flex-direction:column;height:auto}.rcf .sb-cards>.usage-card,.rcf .sb-cards>.credit{height:auto}.rcf .statband.collapsed .sb-cards{height:0}.rcf .fields.mode-company,.rcf .fields.mode-people{grid-template-columns:1fr 1fr}}
@media (prefers-reduced-motion:reduce){.rcf *{transition:none !important;animation:none !important}}
`;
