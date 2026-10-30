import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, FileSpreadsheet, Search, Info, Square, Check, Loader2, Gauge } from "lucide-react";
import { SourceChip } from "../components/ui";
import { demandError, getDemandBatch, getDemandUsage, stopDemandBatch } from "./demandApi";
import {
  BatchStatusPill, Switch, ROW_META, isActiveRow, fmtWhen, fmtDuration, QUOTA_TIP,
} from "./demandUi";
import DemandJobsTable from "./DemandJobsTable";

/* One Demand Search batch: processing and results on one page. A role="switch"
 * "Show processing details" opens the processing panel beside the jobs table
 * (stacked at ≤1000px). Default: on while the batch runs, off once it has
 * finished — the user's own last choice wins (remembered in this browser). Polls
 * GET /demand/batches/{run_id} while anything is still running. */

const POLL_MS = 4000;
const DETAILS_KEY = "demand.showProcessingDetails";

function readDetailsPref() {
  try {
    const v = window.localStorage.getItem(DETAILS_KEY);
    return v === "1" ? true : v === "0" ? false : null;
  } catch {
    return null;
  }
}

function writeDetailsPref(on) {
  try { window.localStorage.setItem(DETAILS_KEY, on ? "1" : "0"); } catch { /* storage unavailable */ }
}

export default function DemandBatchView({ runId, onBack, onOpenJob }) {
  const [batch, setBatch] = useState(null);
  const [usage, setUsage] = useState(null);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("all");
  const [pref, setPref] = useState(readDetailsPref);
  const timer = useRef(null);
  const alive = useRef(true);
  const ticks = useRef(0);

  const running = batch?.status === "running" || (batch?.rows || []).some(isActiveRow);

  const load = useCallback(async () => {
    try {
      const res = await getDemandBatch(runId);
      if (!alive.current) return null;
      setBatch(res);
      setError("");
      return res;
    } catch (err) {
      if (alive.current) setError(demandError(err).message);
      return null;
    }
  }, [runId]);

  const loadUsage = useCallback(async () => {
    try { const u = await getDemandUsage(); if (alive.current) setUsage(u); } catch { /* best-effort */ }
  }, []);

  useEffect(() => {
    alive.current = true;
    const tick = async () => {
      const res = await load();
      if (!alive.current) return;
      ticks.current += 1;
      const live = res && (res.status === "running" || (res.rows || []).some(isActiveRow));
      if (ticks.current === 1 || (live && ticks.current % 3 === 0)) loadUsage();
      if (live || !res) timer.current = setTimeout(tick, POLL_MS);
    };
    tick();
    return () => { alive.current = false; if (timer.current) clearTimeout(timer.current); };
  }, [load, loadUsage]);

  const showDetails = pref ?? running;
  const toggle = (on) => { setPref(on); writeDetailsPref(on); };

  if (!batch) {
    return (
      <div className="ha-detail-page">
        <button type="button" className="dm-back" onClick={onBack}><ArrowLeft size={16} /> Back to Demand Search</button>
        <div style={{ marginTop: 16 }} className={error ? "ha-errbanner" : "dm-soft"}>{error || "Loading batch…"}</div>
      </div>
    );
  }

  const c = batch.counts || {};
  const cards = [
    ["all", batch.valid_rows, "Valid rows", "#1E40AF"],
    ["finished", c.finished, "Finished", "#B45309"],
    ["matched", c.matched, "Matched", "#047857"],
    ["reused", c.reused, "Reused (no quota)", "#2563EB"],
    ["no_results", c.no_results, "No match", "#475569"],
    ["queries_only", c.queries_only, "Queries only", "#0369A1"],
    ["attention", c.attention, "Needs attention", "#B91C1C"],
  ];
  const completed = batch.status === "running" ? "In progress"
    : `${fmtWhen(batch.completed_at)}${batch.status === "stopped" ? " (stopped)" : ""}`;
  const table = (
    <DemandJobsTable
      rows={batch.rows || []} uploadedOn={batch.uploaded_on} status={status} onStatusChange={setStatus}
      onOpenJob={(r) => onOpenJob(batch.run_id, r.job_id)}
    />
  );

  return (
    <div className="ha-detail-page">
      <button type="button" className="dm-back" onClick={onBack}><ArrowLeft size={16} /> Back to Demand Search</button>
      {error && <div className="ha-errbanner" style={{ marginTop: 12 }}>{error}</div>}

      <div className="ha-card dm-card" style={{ marginTop: 14 }}>
        <div className="ha-runhead">
          <div className="ha-runhead-meta">
            <div className="dm-klbl">Demand Search batch</div>
            <div className="dm-runid">{batch.batch_name || batch.run_id}</div>
            {batch.batch_name && <div className="dm-sub">{batch.run_id}</div>}
            <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap", alignItems: "center" }}>
              <SourceChip source="Demand" />
              <span className="ha-pill dm-pill" style={{ background: "#F1F5F9", color: "#475569" }}><FileSpreadsheet size={12} /> {batch.filename}</span>
              <span className="dm-soft" style={{ fontSize: 12 }}>
                {batch.total_rows} rows uploaded{batch.invalid_rows ? ` · ${batch.invalid_rows} skipped` : ""}
              </span>
            </div>
            <div className="dm-meta-row">
              <div><div className="dm-klbl">Uploaded by</div><div className="v">{batch.uploaded_by || "—"}</div></div>
              <div><div className="dm-klbl">Started</div><div className="v">{fmtWhen(batch.started_at)}</div></div>
              <div><div className="dm-klbl">Completed</div><div className="v">{completed}</div></div>
            </div>
          </div>
          <div className="ha-runhead-right">
            <BatchStatusPill status={batch.status} />
            <div className="dm-runstats">
              {cards.map(([key, value, label, color]) => (
                <button
                  key={key} type="button"
                  className={"ha-runstat ha-runstat-btn" + (status === key && key !== "all" ? " ha-runstat-active" : "")}
                  aria-pressed={status === key && key !== "all"}
                  onClick={() => setStatus(status === key || key === "all" ? "all" : key)}
                >
                  <b style={{ color }}>{value ?? 0}</b><span>{label}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      <div className="dm-switch-row">
        <Switch checked={showDetails} onChange={toggle}>Show processing details</Switch>
        <span className="dm-soft" style={{ fontSize: 12 }}>
          {pref != null ? "Your last choice is remembered in this browser." : running ? "On by default while the batch is running." : "Off by default once the batch has finished."}
        </span>
      </div>

      {showDetails ? (
        <div className="dm-split">
          <ProcessingPanel batch={batch} usage={usage} running={running} onStopped={load} />
          {table}
        </div>
      ) : table}
    </div>
  );
}

// ── Processing panel ─────────────────────────────────────────────────────────

const LEGEND = [
  ["queue", "Queued", "Waiting for its turn. Rows run one at a time."],
  ["proc", "Extracting requirements", "The LLM is reading the title, JD and optional columns."],
  ["proc", "Searching Resdex", "Searching Resdex (T1, then T2, then T3)."],
  ["done", "Matched · 3 candidates", "1 to 3 profiles with 60%+ coverage."],
  ["reuse", "Reused", "Match from the last 14 days. No quota used."],
  ["nf", "No results", "No profile reached 60% at any tier."],
  ["query", "Queries only", "Recruiter not logged in: requirements and T1–T3 queries built, Resdex not searched. Connect and re-run the job to find candidates."],
  ["fail", "Session expired", "Recruiter login needed. The row waits, then is skipped."],
  ["cap", "Daily cap reached", "The shared quota ran out. Re-run after 5:30 AM IST."],
  ["fail", "Error", "Resdex failed or timed out. Re-run the job."],
  ["stop", "Stopped", "The batch was stopped before this row ran."],
];

function ListChip({ r }) {
  const meta = ROW_META[r.status] || ROW_META.error;
  let label = meta.label;
  if (r.status === "matched") label = `Matched · ${r.candidates || 0}`;
  else if (r.status === "reused") label = `Reused · ${r.candidates || 0}`;
  else if (r.status === "no_results") label = "No results";
  else if (r.status === "queries_only") label = "Queries only";
  else if (r.status === "session_expired") label = "Session expired";
  else if (r.status === "in_progress") label = r.sub_step === "searching" ? "Searching Resdex" : "Extracting requirements";
  return (
    <span className={`dm-chip ${meta.chip}`}>
      {r.status === "in_progress" ? <Loader2 size={11} className="ha-spin" /> : <span className="d" />}{label}
    </span>
  );
}

function ProcessingPanel({ batch, usage, running, onStopped }) {
  const [ask, setAsk] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [msg, setMsg] = useState("");
  const rows = batch.rows || [];
  const c = batch.counts || {};
  const total = rows.length;
  const done = c.finished || 0;
  const cur = rows.find((r) => r.status === "in_progress");
  const curIdx = cur ? rows.indexOf(cur) + 1 : 0;
  const ended = !running;
  const remaining = rows.filter((r) => r.status === "queued" && !r.reuse_expected).length + (cur ? 1 : 0);
  const u = usage?.usage || {};

  useEffect(() => { if (ended) { setAsk(false); setStopping(false); } }, [ended]);

  const stop = async () => {
    setStopping(true);
    setAsk(false);
    try {
      const res = await stopDemandBatch(batch.run_id);
      setMsg(res.status === "idle" ? res.message : "Stopping after the current row…");
      onStopped();
    } catch (err) {
      setMsg(demandError(err).message);
      setStopping(false);
    }
  };

  let chip;
  if (batch.status === "stopped") chip = <span className="dm-live ended"><span className="d" />Stopped</span>;
  else if (batch.status === "failed") chip = <span className="dm-live ended"><span className="d" />Finished early</span>;
  else if (ended) chip = <span className="dm-live ended"><span className="d" />Finished</span>;
  else if (stopping) chip = <span className="dm-live paused"><span className="d" />Stopping</span>;
  else chip = <span className="dm-live"><span className="d" />{cur?.sub_step === "requirements" ? "Extracting" : "Searching"}</span>;

  let current;
  if (batch.status === "stopped") current = `Stopped after ${done} of ${total} rows. Finished rows and their candidates are saved.`;
  else if (batch.status === "failed") current = `${batch.message || "The batch ended early."} ${c.session_expired ? `${c.session_expired} rows ended as Login needed — log in again, then re-run them from the job page.` : ""}`;
  else if (ended) current = `All ${total} rows finished${batch.completed_at ? ` at ${new Date(batch.completed_at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}` : ""}.`;
  else if (cur) current = `Row ${curIdx} of ${total} · ${cur.job_title}`;
  else current = "Starting the next row…";

  const rest = running
    ? `${c.in_progress || 0} in progress · ${c.queued || 0} queued`
    : c.stopped ? `${c.stopped} stopped` : "";

  return (
    <aside className="ha-card dm-proc" aria-label="Processing details">
      <div className="dm-proc-top">
        {chip}
        {running && !stopping && (
          <button type="button" className="ha-btn ha-btn-danger ha-btn-sm" onClick={() => setAsk(true)}><Square size={13} /> Stop</button>
        )}
      </div>
      <div className="dm-cur">{running && cur ? <Search size={15} /> : <Info size={15} />}<span>{current}</span></div>
      {running && cur && (
        <div className="dm-substeps" aria-label="Current row steps">
          {cur.sub_step === "searching" ? (
            <span className="dm-sst ok"><Check size={11} /> Requirements</span>
          ) : (
            <span className="dm-sst now"><Loader2 size={11} className="ha-spin" /> Requirements</span>
          )}
          <span className="dm-soft">›</span>
          <span className={"dm-sst" + (cur.sub_step === "searching" ? " now" : "")}>
            {cur.sub_step === "searching" && <Loader2 size={11} className="ha-spin" />} Resdex T1 → T3
          </span>
        </div>
      )}
      {msg && <div className="dm-soft" style={{ fontSize: 12 }}>{msg}</div>}
      {ask && running && (
        <div className="dm-confirm" role="alertdialog" aria-label="Stop the batch">
          <p>
            <b>Stop this batch after the current row?</b> The {done} finished rows and their candidates are kept. The rest end as
            Stopped, and you can re-run them one at a time from the job page.
          </p>
          <div className="acts">
            <button type="button" className="ha-btn ha-btn-danger ha-btn-sm" onClick={stop}>Stop batch</button>
            <button type="button" className="ha-btn ha-btn-secondary ha-btn-sm" onClick={() => setAsk(false)}>Keep running</button>
          </div>
        </div>
      )}
      <div>
        <div className="dm-bigbar" role="progressbar" aria-label="Batch progress" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done}>
          <i style={{ width: `${total ? Math.round((done / total) * 100) : 0}%` }} />
        </div>
        <div className="dm-bigbar-foot">
          <span><b>{done}</b> of {total} rows</span>
          <span>{running && remaining ? `About ${fmtDuration(remaining)} left` : " "}</span>
        </div>
      </div>
      <div className="dm-ccount">
        <span className="dm-ct done"><span className="d" /><b>{c.matched || 0}</b> matched</span>
        <span className="dm-ct reuse"><span className="d" /><b>{c.reused || 0}</b> reused</span>
        <span className="dm-ct"><span className="d" /><b>{c.no_results || 0}</b> no results</span>
        {(c.queries_only || 0) > 0 && <span className="dm-ct query"><span className="d" /><b>{c.queries_only}</b> queries only</span>}
        <span className={"dm-ct " + ((c.attention || 0) > 1 ? "cap" : "fail")}><span className="d" /><b>{c.attention || 0}</b> need attention</span>
        {rest && <span className="dm-ct" style={{ gridColumn: "1/-1" }}><span className="d" />{rest}</span>}
      </div>
      <div className="dm-quota" title={QUOTA_TIP}>
        <div className="dm-blk-lbl" style={{ margin: 0, display: "flex", alignItems: "center", gap: 6 }}><Gauge size={13} /> Quota used by this batch</div>
        <div className="dm-meter-top"><span>Searches</span><b>{c.searches_used || 0}</b></div>
        <div className="dm-meter-top"><span>Candidates kept</span><b>{c.candidates_kept || 0}</b></div>
        <div className="dm-soft" style={{ fontSize: 12 }}>
          {u.searches != null
            ? `Today's shared quota: ${u.searches}/${u.search_cap || "∞"} searches, ${u.candidates}/${u.candidate_cap || "∞"} candidates (shared with harvest outreach).`
            : "One account-wide quota per UTC day, shared with harvest outreach."}
        </div>
      </div>
      <div>
        <div className="dm-blk-lbl">Row status</div>
        <ol className="dm-rowlist" aria-label="Row status">
          {rows.map((r, i) => (
            <li key={r.job_id || r.row}>
              <span className="n">{i + 1}</span>
              <span className="tt" title={r.job_title}>{r.job_title}</span>
              <ListChip r={r} />
            </li>
          ))}
        </ol>
      </div>
      <details className="dm-legend">
        <summary>What the statuses mean</summary>
        <div className="dm-legend-body">
          {LEGEND.map(([cls, label, text]) => (
            <div key={label}><span className={`dm-chip ${cls}`}><span className="d" />{label}</span>{text}</div>
          ))}
        </div>
      </details>
    </aside>
  );
}
