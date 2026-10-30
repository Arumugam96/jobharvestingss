import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Upload, History, Search, Info, FileSpreadsheet } from "lucide-react";
import LiveBrowserView from "../components/LiveBrowserView";
import { demandError, getDemandBatches } from "./demandApi";
import {
  DemandStyles, Stepper, SessionQuotaPills, BatchStatusPill, Note, useDemandUsage, fmtWhen,
} from "./demandUi";
import { ValidationStep, PreflightStep } from "./DemandUploadFlow";
import DemandBatchView from "./DemandBatchView";
import DemandJobDetail from "./DemandJobDetail";

/* Demand Search — upload client job requirements (one role per row) and find
 * matching Naukri Resdex candidates for each. One SPA route, three views keyed by
 * query params (bare /demand/... is proxied to the API by nginx):
 *
 *   /demand                     landing: new batch (upload → validate → pre-flight)
 *                               + past batches
 *   /demand?batch=<run_id>      batch page: processing + results on one page
 *   /demand?batch=…&job=<id>    Demand job page (Resdex candidates, re-run)
 *
 * Talks only to /demand/* (src/demand/demandApi.js) plus the existing Naukri
 * recruiter login for Connect. Separate from the harvest pages by design. */
export default function DemandSearchPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const batch = params.get("batch");
  const job = params.get("job");

  const openBatch = useCallback((runId) => navigate(`/demand?batch=${encodeURIComponent(runId)}`), [navigate]);
  const openJob = useCallback((runId, jobId) => navigate(
    `/demand?batch=${encodeURIComponent(runId)}&job=${encodeURIComponent(jobId)}`,
    { state: { from: location.pathname + location.search } },
  ), [navigate, location.pathname, location.search]);

  return (
    <main className="ha-main">
      <DemandStyles />
      {job ? (
        <DemandJobDetail
          jobId={job}
          runId={batch}
          onBack={() => navigate(location.state?.from || (batch ? `/demand?batch=${encodeURIComponent(batch)}` : "/demand"))}
        />
      ) : batch ? (
        <DemandBatchView key={batch} runId={batch} onBack={() => navigate("/demand")} onOpenJob={openJob} />
      ) : (
        <DemandLanding onOpenBatch={openBatch} />
      )}
    </main>
  );
}

// ── Landing: new batch flow + past batches ────────────────────────────────────

function DemandLanding({ onOpenBatch }) {
  const sess = useDemandUsage();
  // home → validate (file chosen) → preflight
  const [step, setStep] = useState("home");
  const [file, setFile] = useState(null);
  const [preview, setPreview] = useState(null);   // POST /demand/validate result

  const chooseFile = (f) => { if (f) { setFile(f); setPreview(null); setStep("validate"); } };
  const reset = () => { setFile(null); setPreview(null); setStep("home"); };

  return (
    <>
      {sess.live && <LiveBrowserView title="Naukri recruiter login — live browser" onClose={() => { sess.setLive(false); sess.refresh(); }} />}
      {step === "home" && (
        <>
          <div className="ha-pagehead">
            <div className="ha-pagehead-titles">
              <h1>Demand Search</h1>
              <p className="ha-sub">Upload client job requirements and find matching Naukri Resdex candidates for each role.</p>
            </div>
            <div className="ha-pagehead-actions"><SessionQuotaPills usage={sess.usage} /></div>
          </div>
          <div className="ha-pagebody">
            {sess.usage && !sess.usage.resdex_enabled && (
              <div className="ha-errbanner">Resdex is disabled on this server (RESDEX_ENABLED=false), so Demand Search can't search for candidates.</div>
            )}
            <UploadCard onFile={chooseFile} />
            <PastBatches onOpenBatch={onOpenBatch} />
          </div>
        </>
      )}
      {step === "validate" && file && (
        <ValidationStep
          file={file} initial={preview} onReplace={chooseFile} onCancel={reset}
          onContinue={(p) => { setPreview(p); setStep("preflight"); }}
        />
      )}
      {step === "preflight" && file && preview && (
        <PreflightStep
          file={file} preview={preview} sess={sess}
          onBack={() => setStep("validate")} onStarted={(res) => onOpenBatch(res.run_id)}
        />
      )}
    </>
  );
}

function UploadCard({ onFile }) {
  const [dragging, setDragging] = useState(false);
  const inputId = "dm-file-input";
  const pick = () => document.getElementById(inputId)?.click();
  return (
    <div className="ha-card dm-card">
      <div className="dm-sec-title">
        <h3><Upload size={16} /> New Demand Search batch</h3>
        <span className="dm-soft" style={{ fontSize: 12 }}>One role per row · candidates come from Naukri Resdex</span>
      </div>
      <Stepper active={1} />
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
              <div className="dm-soft" style={{ fontSize: 12, marginTop: 6 }}>
                When present, these are sent to the LLM with the title and JD as extra context for extracting the requirements. They do not replace what the LLM finds.
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
            A JD that was searched in the last 14 days reuses its match and uses no quota.
          </Note>
        </div>
      </div>
    </div>
  );
}

function PastBatches({ onOpenBatch }) {
  const [batches, setBatches] = useState(null);
  const [error, setError] = useState("");
  const [q, setQ] = useState("");

  useEffect(() => {
    let alive = true;
    getDemandBatches()
      .then((res) => { if (alive) setBatches(res.batches || []); })
      .catch((err) => { if (alive) { setBatches([]); setError(demandError(err).message); } });
    return () => { alive = false; };
  }, []);

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (batches || []).filter((b) => !needle
      || `${b.filename} ${b.batch_name} ${b.uploaded_by} ${b.run_id}`.toLowerCase().includes(needle));
  }, [batches, q]);

  return (
    <div className="ha-card dm-card">
      <div className="dm-sec-title">
        <h3><History size={16} /> Past batches <span className="ha-pill" style={{ background: "#F1F5F9", color: "#475569" }}>{batches ? batches.length : "…"}</span></h3>
        <div className="ha-filter-search" style={{ width: "min(280px,100%)", gridColumn: "auto" }}>
          <Search size={15} />
          <input className="ha-input" aria-label="Search batches" placeholder="Search file, person or batch ID" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      </div>
      {error && <div className="ha-errbanner" style={{ marginBottom: 10 }}>{error}</div>}
      <div className="dm-tbl-wrap">
        <div className="dm-tbl-scroll">
          <table className="ha-table">
            <thead>
              <tr>
                <th className="ha-th" style={{ width: 290 }}>File</th>
                <th className="ha-th" style={{ width: 170 }}>Uploaded by</th>
                <th className="ha-th" style={{ width: 170 }}>Uploaded</th>
                <th className="ha-th" style={{ width: 110 }}>Rows valid</th>
                <th className="ha-th" style={{ width: 110 }}>Matched</th>
                <th className="ha-th" style={{ width: 170 }}>Status</th>
              </tr>
            </thead>
            <tbody>
              {batches === null ? (
                <tr><td className="ha-td dm-empty" colSpan={6}>Loading…</td></tr>
              ) : rows.length === 0 ? (
                <tr><td className="ha-td dm-empty" colSpan={6}>
                  {q ? `No batches match "${q}". Clear the search to see every batch.` : "No batches yet. Upload a file above to start one."}
                </td></tr>
              ) : rows.map((b) => (
                <tr key={b.run_id} className="ha-row">
                  <td className="ha-td">
                    <button type="button" className="ha-link" onClick={() => onOpenBatch(b.run_id)} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <FileSpreadsheet size={14} /> {b.filename || b.batch_name || b.run_id}
                    </button>
                    <div className="dm-sub">{b.run_id}</div>
                  </td>
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
      <p className="dm-soft" style={{ fontSize: 12, margin: "10px 2px 0" }}>
        Click a file name to open its batch page. Demand jobs are not shown in Harvested Jobs by default.
      </p>
    </div>
  );
}
