import { useEffect, useRef, useState } from "react";
import {
  FileSpreadsheet, FileText, CheckCircle2, XCircle, RotateCcw, Check, X, AlertTriangle, Upload,
  ChevronRight, KeyRound, Gauge, Clock, Lock, Monitor, Search, Loader2, Info,
} from "lucide-react";
import { demandError, demandUpload, demandValidate } from "./demandApi";
import {
  Stepper, Note, HarvestRunningModal, sessionState, fmtDuration, fmtAgo, fmtDay, SESSION_TIP,
} from "./demandUi";

/* Steps 2 and 3 of a new Demand Search batch: validation (POST /demand/validate —
 * nothing is saved) and pre-flight (session, shared quota, duration, Start →
 * POST /demand/upload). */

const FIELD_LABELS = {
  job_title: "Job Title", job_description: "Job Description", skills: "Skills",
  experience: "Experience", location: "Location", client: "Client",
};
const REQUIRED = ["job_title", "job_description"];
const CHECK_CHIP = {
  ok: ["done", "Ready"], reuse: ["reuse", "Reuse match"], no_title: ["fail", "Title missing"],
  short_jd: ["fail", "JD too short"], duplicate: ["fail", "Duplicate"],
};

function PageHead({ sub }) {
  return (
    <div className="ha-pagehead">
      <div className="ha-pagehead-titles">
        <h1>Demand Search <span className="ha-h1-soft">/ New batch</span></h1>
        <p className="ha-sub">{sub}</p>
      </div>
    </div>
  );
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many || `${one}s`}`;

// ── Step 2: validation ────────────────────────────────────────────────────────

export function ValidationStep({ file, initial, onReplace, onCancel, onContinue }) {
  const [preview, setPreview] = useState(initial || null);
  const [fileError, setFileError] = useState(null);
  const [error, setError] = useState("");
  const replaceRef = useRef(null);

  useEffect(() => {
    if (initial) return undefined;
    let alive = true;
    setPreview(null); setFileError(null); setError("");
    demandValidate(file)
      .then((res) => { if (alive) setPreview(res); })
      .catch((err) => {
        if (!alive) return;
        const e = demandError(err);
        if (e.status === 422 && e.detail?.title) setFileError(e.detail);
        else setError(e.message);
      });
    return () => { alive = false; };
  }, [file, initial]);

  const replaceInput = (
    <input ref={replaceRef} type="file" accept=".xlsx,.xls,.csv" style={{ display: "none" }}
      onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) onReplace(f); }} />
  );
  const pickAnother = () => replaceRef.current?.click();

  let body;
  if (error) {
    body = <div className="ha-errbanner">{error}</div>;
  } else if (fileError) {
    body = <FileErrorState file={file} err={fileError} onChoose={pickAnother} />;
  } else if (!preview) {
    body = <div className="dm-soft" style={{ display: "flex", gap: 8, alignItems: "center" }}><Loader2 size={16} className="ha-spin" /> Checking {file.name}…</div>;
  } else {
    body = <ValidationSummary preview={preview} onReplace={pickAnother} onCancel={onCancel} onContinue={() => onContinue(preview)} />;
  }

  return (
    <>
      <PageHead sub="Check the file before anything is searched. Nothing is saved until you start the batch." />
      <div className="ha-pagebody">
        <div className="ha-card dm-card">
          {replaceInput}
          <Stepper active={2} />
          {body}
        </div>
      </div>
    </>
  );
}

function FileErrorState({ file, err, onChoose }) {
  const headers = err.headers_found || [];
  const mapping = err.mapping || {};
  return (
    <div className="dm-summary">
      <div className="dm-filechip">
        <div className="dm-fic bad">{err.code === "unsupported_type" ? <FileText size={18} /> : <FileSpreadsheet size={18} />}</div>
        <div><div className="dm-fname">{err.filename || file.name}</div><div className="dm-fmeta">{err.size_label || ""}</div></div>
      </div>
      <div className="dm-state bad" role="alert">
        <span className="dm-state-ico"><AlertTriangle size={18} /></span>
        <h4>{err.title}</h4>
        <p>{err.message}</p>
        {err.code === "missing_column" && headers.length > 0 && (
          <div style={{ marginTop: 8 }}>
            <div className="dm-blk-lbl">Headers found in the file</div>
            <div className="dm-maprow">{headers.map((h) => <span key={h} className="dm-mapchip">{h}</span>)}</div>
            <div className="dm-maprow" style={{ marginTop: 8 }}>
              {REQUIRED.map((f) => (mapping[f]
                ? <span key={f} className="dm-mapchip ok"><Check size={13} />{FIELD_LABELS[f]} ← "{mapping[f]}"</span>
                : <span key={f} className="dm-mapchip off"><X size={13} />{FIELD_LABELS[f]}: missing</span>))}
            </div>
          </div>
        )}
        <div className="dm-state-acts">
          <button type="button" className="ha-btn ha-btn-primary ha-btn-sm" onClick={onChoose}><Upload size={13} /> Choose another file</button>
          <span>Nothing was saved. Accepted: .xlsx, .xls, .csv · up to 10 MB · up to 50 rows</span>
        </div>
      </div>
    </div>
  );
}

function ValidationSummary({ preview, onReplace, onCancel, onContinue }) {
  const p = preview;
  const mapping = p.mapping || {};
  const reuse = p.reuse || [];
  const invalid = p.invalid || [];
  const optional = Object.keys(FIELD_LABELS).filter((f) => !REQUIRED.includes(f) && mapping[f]);
  return (
    <div className="dm-summary">
      <div className="dm-filechip">
        <div className="dm-fic"><FileSpreadsheet size={18} /></div>
        <div>
          <div className="dm-fname">{p.filename}</div>
          <div className="dm-fmeta dm-num">
            {p.size_label}{p.sheet ? ` · Sheet "${p.sheet}"` : ""} · {plural(p.total_rows, "row")} parsed (max 50)
          </div>
        </div>
        <button type="button" className="ha-btn ha-btn-secondary ha-btn-sm" onClick={onReplace}>Replace file</button>
      </div>

      <div className="dm-valrow" aria-label="Validation summary">
        <span className="dm-valchip total dm-num">{plural(p.total_rows, "row")}</span>
        <span className="dm-valchip ready dm-num"><CheckCircle2 size={14} /> {p.valid_rows} valid</span>
        {invalid.length > 0 && <span className="dm-valchip miss dm-num"><XCircle size={14} /> {invalid.length} invalid, will be skipped</span>}
        {reuse.length > 0 && <span className="dm-valchip reuse dm-num"><RotateCcw size={14} /> {reuse.length} will reuse an existing match</span>}
      </div>

      <div>
        <div className="dm-blk-lbl">
          Column mapping <span className="lc" style={{ color: "#047857" }}>· detected automatically</span>
          <span className="lc">· optional columns are sent to the LLM as extra context</span>
        </div>
        <div className="dm-maprow">
          {REQUIRED.map((f) => <span key={f} className="dm-mapchip req"><Check size={13} />{FIELD_LABELS[f]} <span className="from">← "{mapping[f]}"</span></span>)}
          {optional.map((f) => <span key={f} className="dm-mapchip ok"><Check size={13} />{FIELD_LABELS[f]} <span className="from">← "{mapping[f]}"</span></span>)}
          {(p.ignored_headers || []).length > 0 && (
            <span className="dm-mapchip off">Ignored: {p.ignored_headers.map((h) => `"${h}"`).join(", ")}</span>
          )}
        </div>
      </div>

      {invalid.length > 0 && (
        <div>
          <div className="dm-blk-lbl">
            {plural(invalid.length, "row")} will be skipped
            <span className="lc">· fix them in the file and upload it again, or continue without them</span>
          </div>
          <div className="dm-issues">
            {invalid.map((i) => (
              <div key={i.row} className="dm-issue">
                <span className="rn">Row {i.row}</span>
                <span><b>{i.reason}</b>{i.fix && <span className="fix">{i.fix}</span>}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {reuse.length > 0 && (
        <Note icon={RotateCcw}>
          <b>Will reuse existing match (no quota used):</b>{" "}
          {reuse.map((r, i) => (
            <span key={r.row}>
              {i ? (i === reuse.length - 1 ? " and " : ", ") : ""}row {r.row} {r.job_title} (searched {fmtDay(r.searched_at)}, {plural(r.candidates, "candidate")})
            </span>
          ))}
          . Each was searched in the last {p.reuse_days || 14} days. To search again, open the job after the batch finishes and choose "Re-run Resdex search".
        </Note>
      )}

      <div>
        <div className="dm-blk-lbl">Preview <span className="lc">· first {(p.preview || []).length} rows</span></div>
        <div className="dm-tbl-wrap">
          <div className="dm-tbl-scroll">
            <table className="dm-mini">
              <thead>
                <tr>
                  <th style={{ width: 56 }}>Row</th><th style={{ width: 210 }}>Job title</th><th style={{ width: 130 }}>Client</th>
                  <th style={{ width: 110 }}>Location</th><th style={{ width: 70 }}>Exp</th><th>Job description</th>
                  <th style={{ width: 170 }}>Skills</th><th style={{ width: 130 }}>Check</th>
                </tr>
              </thead>
              <tbody>
                {(p.preview || []).map((r) => {
                  const [cls, lbl] = CHECK_CHIP[r.check] || CHECK_CHIP.ok;
                  return (
                    <tr key={r.row} className={cls === "fail" ? "is-bad" : ""}>
                      <td className="dm-num dm-soft">{r.row}</td>
                      <td>{r.job_title ? <b>{r.job_title}</b> : <span className="dm-muted">(empty)</span>}</td>
                      <td>{r.client || <span className="dm-muted">—</span>}</td>
                      <td>{r.location || <span className="dm-muted">—</span>}</td>
                      <td className="dm-num">{r.experience || <span className="dm-muted">—</span>}</td>
                      <td><span className="dm-clip dm-soft" title={r.job_description}>{r.job_description}</span></td>
                      <td><span className="dm-clip" title={r.skills}>{r.skills || <span className="dm-muted">—</span>}</span></td>
                      <td><span className={`dm-chip ${cls}`}><span className="d" />{lbl}</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div className="dm-foot">
        <span className="left">{plural(p.new_searches ?? p.valid_rows, "new search", "new searches")} + {reuse.length} reused. Invalid rows are not saved.</span>
        <button type="button" className="ha-btn ha-btn-secondary" onClick={onCancel}>Cancel</button>
        <button type="button" className="ha-btn ha-btn-primary" onClick={onContinue} disabled={!p.valid_rows}>
          Continue with {plural(p.valid_rows, "row")} <ChevronRight size={16} />
        </button>
      </div>
    </div>
  );
}

// ── Step 3: pre-flight ────────────────────────────────────────────────────────

const WHY = (
  <p className="dm-pf-why">
    Green only when the last recorded check says <code>logged_in</code>. Connect (Naukri Launcher login), verify and harvest
    pre-checks write that record. Recruiter sessions last only about 5 to 18 hours, so the batch checks again when it starts.
  </p>
);

export function PreflightStep({ file, preview, sess, onBack, onStarted }) {
  const { usage, refresh, connect, connecting, note } = sess;
  const [name, setName] = useState((preview.filename || "").replace(/\.[^.]+$/, ""));
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const [modal, setModal] = useState(null);
  const startRef = useRef(null);

  useEffect(() => { refresh(); }, [refresh]);

  const st = sessionState(usage);
  const ok = st.key === "ok";
  // Logged out still starts — in queries-only mode (LLM requirements + T1–T3 queries,
  // no Resdex search). Only an open login window blocks Start (the backend refuses it).
  const busy = st.key === "busy";
  const enabled = usage ? usage.resdex_enabled !== false : true;
  const u = usage?.usage || {};
  const newRows = preview.new_searches ?? preview.valid_rows;
  const reused = (preview.reuse || []).length;
  const sCap = u.search_cap || 0;
  const cCap = u.candidate_cap || 0;
  const sLeft = sCap > 0 && u.searches != null ? Math.max(0, sCap - u.searches) : null;
  const cLeft = cCap > 0 && u.candidates != null ? Math.max(0, cCap - u.candidates) : null;
  const need = newRows * 3;
  const leftMin = [sLeft, cLeft].filter((v) => v != null);
  const tight = leftMin.length > 0 && need > Math.min(...leftMin);

  const start = async () => {
    setStarting(true);
    setError("");
    try {
      const res = await demandUpload(file, name);
      onStarted(res);
    } catch (err) {
      const e = demandError(err);
      if (e.code === "harvest_running") setModal(e.detail.harvest || {});
      else {
        setError(e.message);
        if (e.code.startsWith("session")) refresh();
      }
      setStarting(false);
    }
  };

  let sessCard;
  if (ok) {
    sessCard = (
      <div className="dm-pf" title={SESSION_TIP}>
        <div className="dm-pf-h"><KeyRound size={14} /> Naukri recruiter session</div>
        <div className="dm-pf-big good"><span className="dm-dot-s good" /> Connected</div>
        <p>{usage?.session?.checked_at ? `Last checked ${fmtAgo(usage.session.checked_at)}.` : ""} Checked again when the batch starts.</p>
        {WHY}
      </div>
    );
  } else {
    const expired = st.key === "exp";
    sessCard = (
      <div className={"dm-pf" + (expired ? " bad" : "")} title={SESSION_TIP}>
        <div className="dm-pf-h"><KeyRound size={14} /> Naukri recruiter session</div>
        <div className={"dm-pf-big " + (expired ? "bad" : "off")}><span className={"dm-dot-s " + st.dot} /> {st.label}</div>
        <p>
          {expired
            ? `The last check${usage?.session?.checked_at ? ` (${fmtAgo(usage.session.checked_at)})` : ""} found the recruiter logged out. Resdex cannot be searched without the login.`
            : st.key === "busy" ? "A recruiter login window is open. Finish logging in in the Live Browser." : "There is no recruiter check on record yet."}
          {" "}Connect opens the Live Browser, where you log in once.
        </p>
        <div>
          <button type="button" className="ha-btn ha-btn-primary ha-btn-sm" onClick={connect} disabled={connecting}>
            {connecting ? <Loader2 size={14} className="ha-spin" /> : <Monitor size={14} />} {connecting ? "Waiting for login…" : "Connect"}
          </button>
        </div>
        {note && <p className="dm-soft" style={{ fontSize: 12 }}>{note}</p>}
        {WHY}
      </div>
    );
  }

  return (
    <>
      {modal && <HarvestRunningModal info={modal} onClose={() => { setModal(null); startRef.current?.focus(); }} />}
      <PageHead sub="Check the session and today's Resdex quota before starting." />
      <div className="ha-pagebody">
        <div className="ha-card dm-card">
          <Stepper active={3} />
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {!enabled && <div className="ha-errbanner">Resdex is disabled on this server (RESDEX_ENABLED=false), so Demand Search can't start.</div>}
            <div className="dm-pf-grid">
              {sessCard}
              <div className="dm-pf">
                <div className="dm-pf-h"><Gauge size={14} /> Today's Resdex quota</div>
                <div>
                  <div className="dm-meter-top"><span>Searches</span><b>{u.searches ?? "—"} / {sCap || "∞"}</b></div>
                  <div className="dm-track"><i style={{ width: sCap ? `${Math.min(100, ((u.searches || 0) / sCap) * 100)}%` : "0%" }} /></div>
                </div>
                <div>
                  <div className="dm-meter-top"><span>Candidates kept</span><b>{u.candidates ?? "—"} / {cCap || "∞"}</b></div>
                  <div className="dm-track"><i className="amber" style={{ width: cCap ? `${Math.min(100, ((u.candidates || 0) / cCap) * 100)}%` : "0%" }} /></div>
                </div>
                <p>
                  {sLeft != null && cLeft != null ? `${sLeft} searches and ${cLeft} candidates left. ` : ""}
                  This is one account-wide quota per UTC day (resets 5:30 AM IST), <b>shared with harvest outreach</b>.
                  Harvest auto-outreach may use some of it while this batch runs.
                </p>
              </div>
              <div className="dm-pf">
                <div className="dm-pf-h"><Clock size={14} /> Estimated duration</div>
                <div className="dm-pf-big">{newRows && ok ? fmtDuration(newRows) : newRows ? "A few minutes" : "Under a minute"}</div>
                <p>
                  {ok ? `${plural(newRows, "job")} to search at 1 to 3 minutes each.` : `${plural(newRows, "job")} to build queries for (LLM only, no Resdex search).`}{reused ? ` ${plural(reused, "reused job")} finish instantly.` : ""} You can
                  leave this page; the batch keeps running on the server.
                </p>
              </div>
            </div>
            {!ok && !busy && (
              <Note icon={Info}>
                <b>Not logged in:</b> Demand Search will extract requirements and build T1/T2/T3 queries for each job,
                but won't search Resdex. Connect to find candidates.
              </Note>
            )}
            {ok && tight && (
              <Note tone="warn" icon={AlertTriangle}>
                <b>This batch may need more quota than is left today.</b> {plural(newRows, "job")} can use up to {need} searches and {need} candidates.
                With {Math.min(...leftMin)} left, about {Math.floor(Math.min(...leftMin) / 3)} jobs will be fully searched. Rows beyond the cap will
                end as <b>Daily cap reached</b>, and you can re-run them after 5:30 AM IST.
              </Note>
            )}
            <div className="dm-field">
              <label htmlFor="dm-batch-name">Batch name</label>
              <input id="dm-batch-name" className="ha-input" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
            </div>
            {error && <div className="ha-errbanner">{error}</div>}
            <div className="dm-foot">
              <span className="left">
                {busy ? (
                  <span style={{ color: "#B91C1C", fontWeight: 600, display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <Lock size={14} /> Finish the recruiter login in the Live Browser to start.
                  </span>
                ) : !ok ? `${plural(preview.valid_rows, "row")} · queries only: no Resdex searches, no quota used.`
                  : `${plural(preview.valid_rows, "row")} · the batch re-checks the recruiter session when it starts.`}
              </span>
              <button type="button" className="ha-btn ha-btn-secondary" onClick={onBack}>Back</button>
              <button type="button" ref={startRef} className="ha-btn ha-btn-primary" onClick={start} disabled={busy || !enabled || starting}>
                {starting ? <Loader2 size={16} className="ha-spin" /> : <Search size={16} />}
                {ok ? ` Start Demand Search (${plural(preview.valid_rows, "row")})` : " Start (queries only)"}
              </button>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
