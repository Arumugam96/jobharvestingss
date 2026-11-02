import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Upload, History, Search, Info, FileSpreadsheet, PencilLine, Plus, ListChecks, ChevronLeft, ChevronRight } from "lucide-react";
import LiveBrowserView from "../components/LiveBrowserView";
import { Select } from "../components/ui";
import JobsPage from "../pages/JobsPage";
import { useHarvestData } from "../HarvestDataContext";
import { createManual, demandError, listBatches /* , listJobs */ } from "./demandApi";
import {
  DemandStyles, SessionQuotaPills, BatchStatusPill, Note, useDemandUsage, fmtWhen,
  SourceKindChip,
} from "./demandUi";
import { ValidationStep, PreflightStep, ManualReviewStep } from "./DemandUploadFlow";
import DemandManualEntry, { checkDraft, newDraft } from "./DemandManualEntry";
import DemandBatchView from "./DemandBatchView";
import DemandJobDetail from "./DemandJobDetail";
import DemandProcessing from "./DemandProcessing";
// import DemandJobsTable from "./DemandJobsTable"; // All jobs tab now uses the Harvested Jobs table

/* Demand Search — client job requirements (uploaded as a spreadsheet or entered
 * manually) searched on Naukri Resdex for matching candidates. One SPA route,
 * every view keyed by query params (bare /demand/... is proxied to the API by
 * nginx, so the page itself never uses a sub-path):
 *
 *   /demand[?view=new]          landing, New search tab: upload file | enter
 *                               manually → validate → pre-flight → start
 *   /demand?view=batches        landing, Batches tab (filters, paging)
 *   /demand?view=jobs           Demand jobs: the Harvested Jobs page/table and
 *                               job detail page, reused for source=Demand only
 *   /demand?processing=<run_id> progress of a just-started batch; opens
 *                               ?view=jobs when every row has finished
 *   /demand?batch=<run_id>      batch page: processing + results on one page
 *   /demand?batch=…&job=<id>    Demand job page (Resdex candidates, re-run)
 *
 * The batch and job pages go Back to the view they were opened from
 * (location.state.from). Talks only to /demand/* (src/demand/demandApi.js) plus
 * the existing Naukri recruiter login for Connect. */
export default function DemandSearchPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const batch = params.get("batch");
  const job = params.get("job");
  const processing = params.get("processing");
  const here = location.pathname + location.search;

  const openBatch = useCallback((runId) => navigate(
    `/demand?batch=${encodeURIComponent(runId)}`, { state: { from: here } },
  ), [navigate, here]);
  const openJob = useCallback((runId, jobId) => navigate(
    `/demand?batch=${encodeURIComponent(runId)}&job=${encodeURIComponent(jobId)}`,
    { state: { from: here } },
  ), [navigate, here]);

  if (!job && !batch && !processing && params.get("view") === "jobs") return <DemandJobsView />;

  return (
    <main className="ha-main">
      <DemandStyles />
      {processing ? (
        <DemandProcessing key={processing} runId={processing} onDone={() => navigate("/demand?view=jobs", { replace: true })} />
      ) : job ? (
        <DemandJobDetail
          jobId={job}
          runId={batch}
          onBack={() => navigate(location.state?.from || (batch ? `/demand?batch=${encodeURIComponent(batch)}` : "/demand"))}
        />
      ) : batch ? (
        <DemandBatchView key={batch} runId={batch} onBack={() => navigate(location.state?.from || "/demand")} onOpenJob={openJob} />
      ) : (
        <DemandLanding onOpenBatch={openBatch} onOpenJob={openJob} />
      )}
    </main>
  );
}

// ── Demand jobs: the Harvested Jobs page + job detail page, Demand rows only ───

function DemandJobsView() {
  const { pageSizeSel, setPageSizeSel } = useHarvestData();
  const navigate = useNavigate();
  return (
    <JobsPage
      demand
      demandTabs={<><DemandStyles /><DemandTabs view="jobs" onSelect={(v) => navigate(`/demand?view=${v}`)} /></>}
      onNavigate={(key) => navigate(`/${key}`)}
      onView={(dv) => navigate(`/jobs/${encodeURIComponent(dv.job.id)}`, { state: { job: dv.job, from: "/demand?view=jobs" } })}
      pageSizeSel={pageSizeSel} onPageSizeChange={setPageSizeSel}
    />
  );
}

// ── Landing: New search | Batches | All jobs ──────────────────────────────────

const VIEWS = [
  { key: "new", label: "New search", Icon: Upload },
  // Batches tab hidden for now — processed jobs are listed in the All jobs table.
  // A separate batch record will be revisited later; BatchesTab is kept below.
  // { key: "batches", label: "Batches", Icon: Layers },  // re-add Layers to the lucide import
  // "All jobs" is the Harvested Jobs page reused for Demand (DemandJobsView).
  { key: "jobs", label: "All jobs", Icon: ListChecks },
];

/** New search | All jobs — on the landing page and on top of the Demand jobs page. */
function DemandTabs({ view, onSelect }) {
  return (
    <div className="dm-tabbar">
      <div className="dm-tabs" role="tablist" aria-label="Demand Search views">
        {VIEWS.map(({ key, label, Icon }) => (
          <button key={key} type="button" role="tab" className="dm-tab" aria-selected={view === key} onClick={() => onSelect(key)}>
            <Icon size={14} /> {label}
          </button>
        ))}
      </div>
      {view !== "new" && (
        <button type="button" className="ha-btn ha-btn-primary ha-btn-sm" onClick={() => onSelect("new")}>
          <Plus size={14} /> New batch
        </button>
      )}
    </div>
  );
}

function DemandLanding({ onOpenBatch, onOpenJob }) {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const view = VIEWS.some((v) => v.key === params.get("view")) ? params.get("view") : "new";
  const setView = (v) => navigate(`/demand?view=${v}`);
  const sess = useDemandUsage();
  const [mode, setMode] = useState("upload");      // New search: upload | manual
  // home → validate → preflight (file) | home → mreview → mpreflight (manual)
  const [step, setStep] = useState("home");
  const [file, setFile] = useState(null);
  const [preview, setPreview] = useState(null);   // POST /demand/validate result
  const [draft, setDraft] = useState(newDraft);   // manual jobs being edited
  const [payload, setPayload] = useState(null);   // the reviewed manual jobs
  const [manualPreview, setManualPreview] = useState(null); // POST /demand/validate-json result

  const chooseFile = (f) => { if (f) { setFile(f); setPreview(null); setStep("validate"); } };
  const reset = () => { setFile(null); setPreview(null); setStep("home"); };
  const reviewManual = () => {
    setPayload({ batch_name: draft.batchName.trim(), jobs: checkDraft(draft).payloadJobs });
    setManualPreview(null);
    setStep("mreview");
  };
  // Once a batch (file or manual) is started, show its progress; when every row
  // has finished, the Demand jobs table opens. Previously: onOpenBatch(res.run_id).
  const started = (res) => {
    if (step === "mpreflight") { setDraft(newDraft()); setPayload(null); setManualPreview(null); }
    reset();
    navigate(`/demand?processing=${encodeURIComponent(res.run_id)}`);
  };

  const onNew = view === "new";
  return (
    <>
      {sess.live && <LiveBrowserView title="Naukri recruiter login — live browser" onClose={() => { sess.setLive(false); sess.refresh(); }} />}
      {(!onNew || step === "home") && (
        <>
          <div className="ha-pagehead">
            <div className="ha-pagehead-titles">
              <h1>Demand Search</h1>
              <p className="ha-sub">Upload or enter client job requirements and find matching Naukri Resdex candidates for each role.</p>
            </div>
            <div className="ha-pagehead-actions"><SessionQuotaPills usage={sess.usage} /></div>
          </div>
          <div className="ha-pagebody">
            <DemandTabs view={view} onSelect={setView} />
            {sess.usage && !sess.usage.resdex_enabled && (
              <div className="ha-errbanner">Resdex is disabled on this server (RESDEX_ENABLED=false), so Demand Search can't search for candidates.</div>
            )}
            {onNew && (
              <UploadCard
                onFile={chooseFile} mode={mode} setMode={setMode}
                manual={<DemandManualEntry draft={draft} setDraft={setDraft} onReview={reviewManual} />}
              />
            )}
            {/* Batches tab hidden for now (see VIEWS).
            {view === "batches" && <BatchesTab onOpenBatch={onOpenBatch} />} */}
            {/* Demand "All jobs" table replaced by the Harvested Jobs page (DemandJobsView).
            {view === "jobs" && <JobsTab onOpenBatch={onOpenBatch} onOpenJob={onOpenJob} />} */}
          </div>
        </>
      )}
      {onNew && step === "validate" && file && (
        <ValidationStep
          file={file} initial={preview} onReplace={chooseFile} onCancel={reset}
          onContinue={(p) => { setPreview(p); setStep("preflight"); }}
        />
      )}
      {onNew && step === "preflight" && file && preview && (
        <PreflightStep
          file={file} preview={preview} sess={sess}
          onBack={() => setStep("validate")} onStarted={started}
        />
      )}
      {onNew && step === "mreview" && payload && (
        <ManualReviewStep
          payload={payload} initial={manualPreview} onEdit={() => setStep("home")}
          onContinue={(p) => { setManualPreview(p); setStep("mpreflight"); }}
        />
      )}
      {onNew && step === "mpreflight" && payload && manualPreview && (
        <PreflightStep
          manual preview={manualPreview} sess={sess} defaultName={payload.batch_name}
          onStart={(name) => createManual({ ...payload, batch_name: name })}
          onBack={() => setStep("mreview")} onStarted={started}
        />
      )}
    </>
  );
}

function UploadCard({ onFile, mode, setMode, manual }) {
  const [dragging, setDragging] = useState(false);
  const inputId = "dm-file-input";
  const pick = () => document.getElementById(inputId)?.click();
  return (
    <div className="ha-card dm-card">
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <div className="dm-seg" role="radiogroup" aria-label="How to add jobs">
          <button type="button" role="radio" aria-checked={mode !== "manual"} onClick={() => setMode("upload")}><Upload size={13} /> Upload file</button>
          <button type="button" role="radio" aria-checked={mode === "manual"} onClick={() => setMode("manual")}><PencilLine size={13} /> Add manually</button>
        </div>
      </div>
      {mode === "manual" ? manual : (
      <div className="dm-up-grid">
        <button
          type="button"
          className={"dm-drop" + (dragging ? " is-drag" : "")}
          onClick={pick}
          onDragOver={(e) => { e.preventDefault(); if (e.dataTransfer) e.dataTransfer.dropEffect = "copy"; setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => { e.preventDefault(); setDragging(false); const f = e.dataTransfer?.files?.[0]; if (f) onFile(f); }}
        >
          <span className="dm-drop-ic"><Upload size={24} /></span>
          <h4>Drop an Excel or CSV file here</h4>
          <p>or <span className="u">browse your computer</span>. The file needs a <b>Job Title</b> and a <b>Job Description</b> column.</p>
        </button>
        <input id={inputId} type="file" accept=".xlsx,.xls,.csv" style={{ display: "none" }}
          onChange={(e) => { onFile(e.target.files?.[0]); e.target.value = ""; }} />
        <div className="dm-req">
          <div className="dm-req-row">
            <span>Required</span>
            <div className="dm-chips">
              <span className="dm-colchip req">Job Title <span className="syn">· Role, Position, Designation</span></span>
              <span className="dm-colchip req">Job Description <span className="syn">· Description, JD</span></span>
            </div>
          </div>
          <div className="dm-req-row">
            <span>Optional</span>
            <div>
              <div className="dm-chips">
                {["Skills", "Experience", "Location", "Client"].map((c) => <span key={c} className="dm-colchip">{c}</span>)}
              </div>
            </div>
          </div>
          <div className="dm-req-row">
            <span>Limits</span>
            <div className="dm-limits">
              <span><b>.xlsx .xls .csv</b></span><span>up to <b>10 MB</b></span><span>up to <b>50 rows</b> per batch</span><span>JD of <b>50+ characters</b></span>
            </div>
          </div>
          <Note icon={Info}>
            Each row uses 1 to 3 Resdex searches and up to 3 candidates from today's quota, which is shared with harvest outreach.
          </Note>
        </div>
      </div>
      )}
    </div>
  );
}

// ── Shared list helpers ───────────────────────────────────────────────────────

function useDebounced(value, ms = 300) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

const SOURCE_OPTIONS = [
  { value: "all", label: "All" }, { value: "file", label: "File" }, { value: "manual", label: "Manual" },
];

function DateField({ label, value, onChange, min, max }) {
  return (
    <label className="dm-date">
      <span>{label}</span>
      <input type="date" className="ha-input" value={value} min={min} max={max} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

function PageNav({ total, offset, size, onPage, unit }) {
  if (!total) return null;
  const from = offset + 1;
  const to = Math.min(total, offset + size);
  return (
    <div className="dm-pagenav">
      <span className="left dm-num">Showing {from}–{to} of {total} {unit}</span>
      <button type="button" className="ha-btn ha-btn-secondary ha-btn-sm" disabled={offset === 0} onClick={() => onPage(Math.max(0, offset - size))}>
        <ChevronLeft size={14} /> Previous
      </button>
      <button type="button" className="ha-btn ha-btn-secondary ha-btn-sm" disabled={to >= total} onClick={() => onPage(offset + size)}>
        Next <ChevronRight size={14} />
      </button>
    </div>
  );
}

/** Filter state shared by the two list tabs: q (debounced), source, date range,
 * offset (reset to 0 whenever a filter changes). */
function useListFilters() {
  const [q, setQ] = useState("");
  const [source, setSource] = useState("all");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [offset, setOffset] = useState(0);
  const dq = useDebounced(q.trim());
  useEffect(() => { setOffset(0); }, [dq, source, from, to]);
  const params = { q: dq, source, date_from: from, date_to: to };
  const active = !!(dq || source !== "all" || from || to);
  const clear = () => { setQ(""); setSource("all"); setFrom(""); setTo(""); };
  return { q, setQ, source, setSource, from, setFrom, to, setTo, offset, setOffset, params, active, clear };
}

function ListFilters({ f, searchLabel, placeholder, children }) {
  return (
    <div className="dm-filters">
      <div className="ha-filter-search">
        <Search size={15} />
        <input className="ha-input" aria-label={searchLabel} placeholder={placeholder} value={f.q} onChange={(e) => f.setQ(e.target.value)} />
      </div>
      {children}
      <Select label="Source" value={f.source} onChange={f.setSource} options={SOURCE_OPTIONS} />
      <DateField label="Uploaded from" value={f.from} max={f.to || undefined} onChange={f.setFrom} />
      <DateField label="Uploaded to" value={f.to} min={f.from || undefined} onChange={f.setTo} />
      {f.active && <button type="button" className="ha-btn ha-btn-secondary ha-btn-sm" onClick={f.clear}>Clear filters</button>}
    </div>
  );
}

// ── Batches tab ───────────────────────────────────────────────────────────────

const BATCH_PAGE = 20;
const BATCH_STATUS_OPTIONS = [
  { value: "all", label: "All" }, { value: "running", label: "Running" }, { value: "completed", label: "Completed" },
  { value: "stopped", label: "Stopped" }, { value: "failed", label: "Failed" },
];

// eslint-disable-next-line no-unused-vars -- Batches tab hidden for now (see VIEWS)
function BatchesTab({ onOpenBatch }) {
  const f = useListFilters();
  const [status, setStatus] = useState("all");
  const [data, setData] = useState(null);         // GET /demand/batches result
  const [error, setError] = useState("");
  const { setOffset } = f;
  useEffect(() => { setOffset(0); }, [status, setOffset]);
  const key = JSON.stringify({ ...f.params, status, offset: f.offset });

  useEffect(() => {
    let alive = true;
    setError("");
    listBatches({ ...JSON.parse(key), limit: BATCH_PAGE })
      .then((res) => { if (alive) setData(res); })
      .catch((err) => { if (alive) { setData({ total: 0, batches: [] }); setError(demandError(err).message); } });
    return () => { alive = false; };
  }, [key]);

  const batches = data?.batches || [];
  const filtered = f.active || status !== "all";
  return (
    <div className="ha-card dm-card">
      <div className="dm-sec-title">
        <h3><History size={16} /> Batches <span className="ha-pill" style={{ background: "#F1F5F9", color: "#475569" }}>{data ? data.total : "…"}</span></h3>
      </div>
      <ListFilters f={f} searchLabel="Search batches" placeholder="Search batch, file, person or batch ID">
        <Select label="Status" value={status} onChange={setStatus} options={BATCH_STATUS_OPTIONS} />
      </ListFilters>
      {error && <div className="ha-errbanner" style={{ marginBottom: 10 }}>{error}</div>}
      <div className="dm-tbl-wrap">
        <div className="dm-tbl-scroll">
          <table className="ha-table">
            <thead>
              <tr>
                <th className="ha-th" style={{ width: 290 }}>Batch</th>
                <th className="ha-th" style={{ width: 100 }}>Source</th>
                <th className="ha-th" style={{ width: 170 }}>Uploaded by</th>
                <th className="ha-th" style={{ width: 170 }}>Uploaded</th>
                <th className="ha-th" style={{ width: 110 }}>Rows valid</th>
                <th className="ha-th" style={{ width: 110 }}>Matched</th>
                <th className="ha-th" style={{ width: 170 }}>Status</th>
              </tr>
            </thead>
            <tbody>
              {data === null ? (
                <tr><td className="ha-td dm-empty" colSpan={7}>Loading…</td></tr>
              ) : batches.length === 0 ? (
                <tr><td className="ha-td dm-empty" colSpan={7}>
                  {filtered ? "No batches match your filters. Clear a filter to see more." : "No batches yet. Start one from the New search tab."}
                </td></tr>
              ) : batches.map((b) => (
                <tr key={b.run_id} className="ha-row">
                  <td className="ha-td">
                    <button type="button" className="ha-link" onClick={() => onOpenBatch(b.run_id)} style={{ display: "inline-flex", alignItems: "center", gap: 6, textAlign: "left" }}>
                      {b.source_kind === "manual" ? <PencilLine size={14} /> : <FileSpreadsheet size={14} />} {b.batch_name || b.filename || b.run_id}
                    </button>
                    <div className="dm-sub">{b.source_kind !== "manual" && b.filename && b.filename !== b.batch_name ? `${b.filename} · ` : ""}{b.run_id}</div>
                  </td>
                  <td className="ha-td"><SourceKindChip kind={b.source_kind} /></td>
                  <td className="ha-td">{b.uploaded_by || <span className="dm-muted">—</span>}</td>
                  <td className="ha-td dm-soft dm-num">{fmtWhen(b.started_at)}</td>
                  <td className="ha-td dm-num"><b>{b.valid_rows}</b> <span className="dm-muted">/ {b.total_rows}</span></td>
                  <td className="ha-td dm-num"><b>{(b.counts?.matched || 0) + (b.counts?.reused || 0)}</b> <span className="dm-muted">jobs</span></td>
                  <td className="ha-td">
                    <BatchStatusPill status={b.status} />
                    {b.status === "failed" && b.message && <div className="dm-sub">{b.message}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <PageNav total={data?.total || 0} offset={f.offset} size={BATCH_PAGE} onPage={f.setOffset} unit="batches" />
      <p className="dm-soft" style={{ fontSize: 12, margin: "10px 2px 0" }}>
        Click a batch name to open its batch page. Demand jobs are not shown in Harvested Jobs by default.
      </p>
    </div>
  );
}

// ── All jobs tab (commented out) ──────────────────────────────────────────────
// Demand jobs are now shown on the Harvested Jobs page reused for Demand
// (DemandJobsView). The Demand-specific jobs table below is kept for later.
// // ── All jobs tab ──────────────────────────────────────────────────────────────

// const JOB_PAGE = 50;
// const POLL_MS = 8000;

// /** Consecutive jobs of the same batch → [{run_id, batch_name, …, rows}] (the API
//  * returns newest batch first, then row order). Each group renders the batch
//  * page's own DemandJobsTable, so the Uploaded column and "in this batch" heading
//  * stay true. */
// function groupByBatch(jobs) {
//   const groups = [];
//   jobs.forEach((j) => {
//     const last = groups[groups.length - 1];
//     if (last && last.run_id === j.run_id) last.rows.push(j);
//     else groups.push({ run_id: j.run_id, batch_name: j.batch_name, source_kind: j.source_kind, batch_status: j.batch_status,
//       uploaded_on: j.uploaded_on, started_at: j.started_at, rows: [j] });
//   });
//   return groups;
// }

// function JobsTab({ onOpenBatch, onOpenJob }) {
//   const f = useListFilters();
//   const [status, setStatus] = useState("all");
//   const [data, setData] = useState(null);         // GET /demand/jobs result
//   const [error, setError] = useState("");
//   const [tick, setTick] = useState(0);
//   const { setOffset } = f;
//   useEffect(() => { setOffset(0); }, [status, setOffset]);
//   const key = JSON.stringify({ ...f.params, status, offset: f.offset });

//   useEffect(() => {
//     let alive = true;
//     setError("");
//     listJobs({ ...JSON.parse(key), limit: JOB_PAGE })
//       .then((res) => { if (alive) setData(res); })
//       .catch((err) => { if (alive) { setData({ total: 0, jobs: [], counts: {} }); setError(demandError(err).message); } });
//     return () => { alive = false; };
//   }, [key, tick]);

//   const jobs = useMemo(() => data?.jobs || [], [data]);
//   // Live statuses while any listed job is still queued / in progress.
//   const live = jobs.some(isActiveRow);
//   useEffect(() => {
//     if (!live) return undefined;
//     const t = setTimeout(() => setTick((n) => n + 1), POLL_MS);
//     return () => clearTimeout(t);
//   }, [live, data]);

//   const groups = useMemo(() => groupByBatch(jobs), [jobs]);
//   const c = data?.counts || {};
//   const stats = [
//     ["all", c.total, "All jobs", "#0F172A"],
//     ["matched", c.matched, "Matched", "#047857"],
//     ["reused", c.reused, "Reused (no quota)", "#2563EB"],
//     ["no_results", c.no_results, "No match", "#475569"],
//     ["attention", c.attention, "Needs attention", "#B91C1C"],
//   ];

//   return (
//     <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
//       <div className="ha-card dm-card">
//         <div className="dm-jobstats">
//           {stats.map(([k, value, label, color]) => (
//             <button
//               key={k} type="button"
//               className={"ha-runstat ha-runstat-btn" + (status === k && k !== "all" ? " ha-runstat-active" : "")}
//               aria-pressed={status === k && k !== "all"}
//               onClick={() => setStatus(status === k || k === "all" ? "all" : k)}
//             >
//               <b style={{ color }}>{value ?? 0}</b><span>{label}</span>
//             </button>
//           ))}
//         </div>
//         <ListFilters f={f} searchLabel="Search all Demand jobs" placeholder="Search title, client, skill or batch…" />
//         {error && <div className="ha-errbanner">{error}</div>}
//         <div className="dm-soft" style={{ fontSize: 12 }}>
//           {data === null ? "Loading…" : `${data.total} ${data.total === 1 ? "job" : "jobs"} across ${groups.length} ${groups.length === 1 ? "batch" : "batches"} on this page.`}
//           {live && " Statuses refresh while jobs are running."}
//         </div>
//       </div>

//       {data !== null && jobs.length === 0 && !error && (
//         <div className="ha-card dm-card dm-empty">
//           {f.active || status !== "all" ? "No jobs match your filters. Clear a filter to see more." : "No Demand jobs yet. Start a batch from the New search tab."}
//         </div>
//       )}

//       {groups.map((g) => (
//         <div key={`${g.run_id}-${g.rows[0]?.job_id || g.rows[0]?.row}`} className="dm-group">
//           <div className="dm-group-h">
//             <button type="button" className="ha-link" onClick={() => onOpenBatch(g.run_id)}>{g.batch_name || g.run_id}</button>
//             <SourceKindChip kind={g.source_kind} />
//             <BatchStatusPill status={g.batch_status} />
//             <span className="dm-num">{fmtWhen(g.started_at)}</span>
//           </div>
//           <DemandJobsTable
//             rows={g.rows} uploadedOn={g.uploaded_on} status={status} onStatusChange={setStatus}
//             onOpenJob={(r) => onOpenJob(r.run_id, r.job_id)}
//           />
//         </div>
//       ))}

//       <PageNav total={data?.total || 0} offset={f.offset} size={JOB_PAGE} onPage={f.setOffset} unit="jobs" />
//     </div>
//   );
// }
