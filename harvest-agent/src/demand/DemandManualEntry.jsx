import { useMemo, useRef } from "react";
import { Plus, Trash2, Braces, ListPlus, ChevronRight, Info, X } from "lucide-react";
import { Note } from "./demandUi";

/* Manual entry for a new Demand Search batch: a batch name and 1–50 job cards
 * (title, JD, metadata). Metadata is a flat dictionary of text fields edited as
 * key–value rows or as pasted JSON; the dictionary rules below mirror the
 * backend's demand_import.metadata_errors (the server enforces them again).
 * "Review N jobs" hands {batch_name, jobs: [{job_title, job_description,
 * metadata}]} to the review step (POST /demand/validate-json). */

export const MAX_JOBS = 50;
export const MIN_JD = 50;
const MAX_KEYS = 20;
const MAX_VALUE = 500;
const KEY_RE = /^[a-z][a-z0-9_]{0,39}$/;
const RESERVED = ["job_title", "job_description"];
export const KNOWN_KEYS = ["skills", "experience", "location", "client"];
const QUICK_KEYS = [
  "skills", "experience", "location", "client", "budget", "work_mode", "employment_type",
  "notice_period", "positions", "requirement_id",
];

let seq = 0;
/** Module-unique id for job cards / metadata rows: the prefix plus an incrementing counter. */
const uid = (p) => `${p}${(seq += 1)}`;

/** Empty job card state (title, JD, key–value metadata mode, no errors). */
export function newJob() {
  return { id: uid("j"), title: "", jd: "", mode: "kv", kv: [], json: "", modeError: "" };
}

/** Default batch name for manual entry, e.g. "Manual – 13 Nov 2026". */
export function defaultBatchName() {
  const d = new Date().toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
  return `Manual – ${d}`;
}

/** Fresh manual-entry draft: default batch name and one empty job card. */
export function newDraft() {
  return { batchName: defaultBatchName(), jobs: [newJob()] };
}

// ── Rules ─────────────────────────────────────────────────────────────────────

/** Validation message for a metadata key (format, reserved names), or "" when valid. */
function keyError(key) {
  if (!key) return "Enter a key.";
  if (!KEY_RE.test(key)) return "Use lowercase letters, digits and _ ; start with a letter; max 40 characters.";
  if (RESERVED.includes(key)) return `${key} has its own field above.`;
  return "";
}

/** Validation message for a metadata value (must be non-empty text ≤ MAX_VALUE chars), or "". */
function valueError(value) {
  if (value === null || typeof value === "boolean" || typeof value === "number") return "Must be text — put it in quotes.";
  if (typeof value === "object") return "Nested objects and arrays are not allowed — use plain text.";
  if (typeof value !== "string") return "Must be text.";
  if (!value.trim()) return "Enter a value.";
  if (value.trim().length > MAX_VALUE) return `${value.trim().length} characters — the limit is ${MAX_VALUE}.`;
  return "";
}

/** [{key, value}] → {fieldErrs: [{key, value}] aligned to entries, general: [msg]}. */
function checkEntries(entries) {
  const seen = new Map();
  const fieldErrs = entries.map((e) => {
    const k = (e.key || "").trim();
    let kErr = keyError(k);
    const lower = k.toLowerCase();
    if (!kErr && seen.has(lower)) kErr = "Duplicate key (keys are case-insensitive).";
    if (k) seen.set(lower, true);
    return { key: kErr, value: valueError(e.value) };
  });
  const general = entries.length > MAX_KEYS ? [`${entries.length} fields — the limit is ${MAX_KEYS}.`] : [];
  return { fieldErrs, general };
}

/** True when a key–value metadata row has neither a key nor a value (ignored by validation). */
const isBlankRow = (r) => !(r.key || "").trim() && !(r.value || "").trim();

/** Parse pasted metadata JSON. Blank text → {obj: {}}; invalid JSON or a non-object →
 * {obj: null, error}. */
function parseJson(text) {
  if (!(text || "").trim()) return { obj: {}, error: "" };
  let obj;
  try {
    obj = JSON.parse(text);
  } catch (err) {
    return { obj: null, error: `Invalid JSON: ${err.message}` };
  }
  if (obj === null || typeof obj !== "object" || Array.isArray(obj)) {
    return { obj: null, error: 'Metadata must be a JSON object, like {"location": "Pune"}.' };
  }
  return { obj, error: "" };
}

/** Per-card validation: {title, jd, kvErrs, metaGeneral, jsonErrs, total, metadata}. */
function checkJob(job, dupOf) {
  const title = job.title.trim() ? "" : "Enter a job title.";
  const jdLen = job.jd.trim().length;
  let jd = "";
  if (!jdLen) jd = "Enter the job description.";
  else if (jdLen < MIN_JD) jd = `At least ${MIN_JD} characters (${MIN_JD - jdLen} more).`;
  else if (dupOf) jd = `Same title and description as Job ${dupOf} — only one is searched.`;

  let kvErrs = [];
  let metaGeneral = [];
  let jsonErrs = [];
  let metadata = {};
  let metaCount = 0;
  if (job.mode === "kv") {
    const rows = job.kv.filter((r) => !isBlankRow(r));
    const { fieldErrs, general } = checkEntries(rows);
    const byId = new Map(rows.map((r, i) => [r.id, fieldErrs[i]]));
    kvErrs = job.kv.map((r) => byId.get(r.id) || { key: "", value: "" });
    metaGeneral = general;
    metaCount = fieldErrs.filter((e) => e.key || e.value).length + general.length;
    rows.forEach((r) => { metadata[r.key.trim()] = typeof r.value === "string" ? r.value.trim() : r.value; });
  } else {
    const { obj, error } = parseJson(job.json);
    if (error) {
      jsonErrs = [error];
    } else {
      const entries = Object.entries(obj).map(([key, value]) => ({ key, value }));
      const { fieldErrs, general } = checkEntries(entries);
      jsonErrs = [
        ...general,
        ...entries.flatMap((e, i) => [fieldErrs[i].key && `${e.key || "(empty key)"}: ${fieldErrs[i].key}`,
          fieldErrs[i].value && `${e.key}: ${fieldErrs[i].value}`].filter(Boolean)),
      ];
      metadata = Object.fromEntries(entries.map((e) => [e.key, typeof e.value === "string" ? e.value.trim() : e.value]));
    }
    metaCount = jsonErrs.length;
  }
  const total = (title ? 1 : 0) + (jd ? 1 : 0) + metaCount;
  return { title, jd, kvErrs, metaGeneral, jsonErrs, total, metadata };
}

/** Validate every card; returns {checks: [checkJob], issues, payloadJobs}. */
export function checkDraft(draft) {
  const firstOf = new Map();
  const checks = draft.jobs.map((j, i) => {
    let dupOf = 0;
    if (j.title.trim() && j.jd.trim().length >= MIN_JD) {
      const key = `${j.title.trim().toLowerCase()}\n${j.jd.trim()}`;
      if (firstOf.has(key)) dupOf = firstOf.get(key);
      else firstOf.set(key, i + 1);
    }
    return checkJob(j, dupOf);
  });
  return {
    checks,
    issues: checks.reduce((n, c) => n + c.total, 0),
    payloadJobs: draft.jobs.map((j, i) => ({
      job_title: j.title.trim(), job_description: j.jd.trim(), metadata: checks[i].metadata,
    })),
  };
}

// ── Mode switching ────────────────────────────────────────────────────────────

/** Convert a card's key–value rows to pretty JSON text for the JSON editor.
 * @returns {{json: string}|{error: string}} error when keys are duplicated. */
function toJson(job) {
  const rows = job.kv.filter((r) => !isBlankRow(r));
  const keys = rows.map((r) => r.key.trim().toLowerCase());
  if (new Set(keys).size !== keys.length) return { error: "Remove the duplicate keys before switching to JSON." };
  const obj = Object.fromEntries(rows.map((r) => [r.key.trim(), r.value]));
  return { json: rows.length ? JSON.stringify(obj, null, 2) : "" };
}

/** Convert a card's JSON text back to key–value rows.
 * @returns {{kv: Array}|{error: string}} error for invalid JSON or non-string values. */
function toKv(job) {
  const { obj, error } = parseJson(job.json);
  if (error) return { error: `Fix the JSON before switching back. ${error}` };
  const bad = Object.entries(obj).filter(([, v]) => typeof v !== "string").map(([k]) => k);
  if (bad.length) return { error: `Make these values text before switching back: ${bad.join(", ")}.` };
  return { kv: Object.entries(obj).map(([key, value]) => ({ id: uid("m"), key, value })) };
}

// ── UI ────────────────────────────────────────────────────────────────────────

/**
 * Manual-entry form for a new Demand Search batch: batch name, 1–50 job cards (title, JD and
 * metadata edited as key–value rows or pasted JSON) with live validation, and a rules aside.
 * State is owned by the parent (draft/setDraft); nothing is sent to the backend here.
 * @param {object} props
 * @param {{batchName: string, jobs: Array}} props.draft - draft from newDraft().
 * @param {Function} props.setDraft - React state setter for the draft.
 * @param {Function} props.onReview - called by the "Start search" button once there are no issues.
 * @param {boolean} [props.starting] - disables the button and shows "Starting…".
 */
export default function DemandManualEntry({ draft, setDraft, onReview, starting = false }) {
  const { checks, issues } = useMemo(() => checkDraft(draft), [draft]);
  const valueRefs = useRef({});

  /** Merge the patch returned by fn(job) into the job card with this id. */
  const setJob = (id, fn) => setDraft((d) => ({ ...d, jobs: d.jobs.map((j) => (j.id === id ? { ...j, ...fn(j) } : j)) }));
  /** Append an empty job card (no-op at MAX_JOBS). */
  const addJob = () => setDraft((d) => (d.jobs.length >= MAX_JOBS ? d : { ...d, jobs: [...d.jobs, newJob()] }));
  /** Remove a job card, always keeping at least one. */
  const removeJob = (id) => setDraft((d) => ({ ...d, jobs: d.jobs.length > 1 ? d.jobs.filter((j) => j.id !== id) : d.jobs }));

  /** Add a metadata row (optionally pre-filled key) to a card and focus its value input. */
  const addField = (job, key = "") => {
    const row = { id: uid("m"), key, value: "" };
    setJob(job.id, (j) => ({ kv: [...j.kv, row] }));
    setTimeout(() => valueRefs.current[row.id]?.focus(), 0);
  };
  /** Switch a card's metadata editor between key–value and JSON, converting the content;
   * a conversion error is shown on the card and the mode stays unchanged. */
  const switchMode = (job, mode) => {
    if (mode === job.mode) return;
    const res = mode === "json" ? toJson(job) : toKv(job);
    if (res.error) { setJob(job.id, () => ({ modeError: res.error })); return; }
    setJob(job.id, () => ({ ...res, mode, modeError: "" }));
  };

  const n = draft.jobs.length;
  return (
    <div className="dm-man-grid">
      <div className="dm-man-list">
        <div className="dm-field">
          <label htmlFor="dm-man-batch">Batch name</label>
          <input id="dm-man-batch" className="ha-input" value={draft.batchName} maxLength={200}
            onChange={(e) => setDraft((d) => ({ ...d, batchName: e.target.value }))} />
        </div>

        {draft.jobs.map((job, i) => {
          const c = checks[i];
          const jdLen = job.jd.trim().length;
          const tId = `dm-man-t-${job.id}`;
          const dId = `dm-man-d-${job.id}`;
          const usedKeys = new Set(job.kv.map((r) => (r.key || "").trim().toLowerCase()));
          return (
            <section key={job.id} className={"dm-jobcard" + (c.total ? " has-err" : "")} aria-label={`Job ${i + 1}`}>
              <div className="dm-jobcard-h">
                <span className="n">Job {i + 1}</span>
                <span className={`dm-chip ${c.total ? "fail" : "done"}`}><span className="d" />{c.title || c.jd ? "Required fields" : c.total ? "Check metadata" : "Ready"}</span>
                <div className="acts">
                  {n > 1 && (
                    <button type="button" className="ha-btn ha-btn-secondary ha-btn-sm" onClick={() => removeJob(job.id)}>
                      <Trash2 size={13} /> Remove
                    </button>
                  )}
                </div>
              </div>

              <div className="dm-field wide">
                <label htmlFor={tId}>Job title *</label>
                <input id={tId} className={"ha-input" + (c.title && job.touched ? " is-err" : "")} value={job.title} maxLength={300}
                  placeholder="e.g. Senior Java Developer" aria-invalid={!!c.title}
                  onBlur={() => setJob(job.id, () => ({ touched: true }))}
                  onChange={(e) => setJob(job.id, () => ({ title: e.target.value }))} />
                {c.title && job.touched && <span className="dm-ferr">{c.title}</span>}
              </div>

              <div className="dm-field wide">
                <label htmlFor={dId}>Job description *</label>
                <textarea id={dId} className={"ha-input" + (c.jd && (job.jd || job.jdTouched) ? " is-err" : "")} value={job.jd}
                  placeholder="Paste the full JD — responsibilities, must-have skills, experience…" aria-invalid={!!c.jd}
                  onBlur={() => setJob(job.id, () => ({ jdTouched: true }))}
                  onChange={(e) => setJob(job.id, () => ({ jd: e.target.value }))} />
                <div className="dm-fhint">
                  <span className={c.jd && (job.jd || job.jdTouched) ? "bad" : ""}>{c.jd && (job.jd || job.jdTouched) ? c.jd : `Minimum ${MIN_JD} characters.`}</span>
                  <span className={jdLen >= MIN_JD ? "good" : jdLen ? "bad" : ""}>{jdLen} / {MIN_JD}+</span>
                </div>
              </div>

              <div className="dm-meta">
                <div className="dm-meta-h">
                  <span className="t">Metadata <span style={{ textTransform: "none", letterSpacing: 0, fontWeight: 500 }}>· optional</span></span>
                  <div className="dm-seg" role="radiogroup" aria-label={`Job ${i + 1} metadata editor`}>
                    <button type="button" role="radio" aria-checked={job.mode === "kv"} onClick={() => switchMode(job, "kv")}><ListPlus size={13} /> Key–value</button>
                    <button type="button" role="radio" aria-checked={job.mode === "json"} onClick={() => switchMode(job, "json")}><Braces size={13} /> Paste JSON</button>
                  </div>
                </div>
                {job.modeError && <span className="dm-ferr" role="alert">{job.modeError}</span>}

                {job.mode === "kv" ? (
                  <>
                    {job.kv.length > 0 && (
                      <div className="dm-kv">
                        {job.kv.map((r, ri) => {
                          const e = c.kvErrs[ri] || {};
                          const blank = isBlankRow(r);
                          return (
                            <div key={r.id} style={{ display: "contents" }}>
                              <input className={"ha-input" + (!blank && e.key ? " is-err" : "")} value={r.key} placeholder="key (e.g. work_mode)"
                                aria-label={`Job ${i + 1} field ${ri + 1} key`} maxLength={60}
                                onChange={(ev) => setJob(job.id, (j) => ({ kv: j.kv.map((x) => (x.id === r.id ? { ...x, key: ev.target.value } : x)) }))} />
                              <input className={"ha-input" + (!blank && e.value ? " is-err" : "")} value={r.value} placeholder="value"
                                aria-label={`Job ${i + 1} field ${ri + 1} value`} maxLength={MAX_VALUE + 100}
                                ref={(el) => { valueRefs.current[r.id] = el; }}
                                onChange={(ev) => setJob(job.id, (j) => ({ kv: j.kv.map((x) => (x.id === r.id ? { ...x, value: ev.target.value } : x)) }))} />
                              <button type="button" className="dm-x sm" aria-label={`Remove field ${ri + 1}`}
                                onClick={() => setJob(job.id, (j) => ({ kv: j.kv.filter((x) => x.id !== r.id) }))}><X size={15} /></button>
                              {!blank && (e.key || e.value) && <span className="dm-ferr">{[e.key && `Key: ${e.key}`, e.value && `Value: ${e.value}`].filter(Boolean).join(" ")}</span>}
                            </div>
                          );
                        })}
                      </div>
                    )}
                    {c.metaGeneral.map((m) => <span key={m} className="dm-ferr">{m}</span>)}
                    <div>
                      <button type="button" className="ha-btn ha-btn-secondary ha-btn-sm" onClick={() => addField(job)} disabled={job.kv.length >= MAX_KEYS}>
                        <Plus size={13} /> Add field
                      </button>
                    </div>
                    <div className="dm-quick">
                      <span>Quick add:</span>
                      {QUICK_KEYS.map((k) => (
                        <button key={k} type="button" disabled={usedKeys.has(k) || job.kv.length >= MAX_KEYS} onClick={() => addField(job, k)}>{k}</button>
                      ))}
                    </div>
                  </>
                ) : (
                  <>
                    <textarea className={"ha-input" + (c.jsonErrs.length ? " is-err" : "")} value={job.json} spellCheck={false}
                      aria-label={`Job ${i + 1} metadata JSON`}
                      placeholder={'{\n  "location": "Pune",\n  "work_mode": "Hybrid"\n}'}
                      onChange={(e) => setJob(job.id, () => ({ json: e.target.value, modeError: "" }))} />
                    {c.jsonErrs.length > 0 && (
                      <ul className="dm-errlist">{c.jsonErrs.map((m, mi) => <li key={mi}>{m}</li>)}</ul>
                    )}
                  </>
                )}
              </div>
            </section>
          );
        })}

        <div>
          <button type="button" className="ha-btn ha-btn-secondary" onClick={addJob} disabled={n >= MAX_JOBS}>
            <Plus size={15} /> Add another job ({n} of {MAX_JOBS})
          </button>
        </div>

        <div className="dm-foot">
          <span className="left">
            {issues ? <span style={{ color: "#B91C1C", fontWeight: 600 }}>Fill in the required fields to continue</span>
              : `${n} ${n === 1 ? "job" : "jobs"} ready. Nothing is saved until you start the search.`}
          </span>
          {/* Was "Review N jobs" (review + pre-flight pages, skipped for now). */}
          <button type="button" className="ha-btn ha-btn-primary" disabled={issues > 0 || starting} onClick={onReview}>
            {starting ? "Starting…" : `Start search (${n} ${n === 1 ? "job" : "jobs"})`} <ChevronRight size={16} />
          </button>
        </div>
      </div>

      <aside className="dm-pf dm-aside" aria-label="Metadata rules">
        <h4>Metadata rules</h4>
        <ul>
          <li>A flat JSON object of text fields — no nested objects or arrays.</li>
          <li>Keys: lowercase letters, digits and <code>_</code>, starting with a letter, up to 40 characters (e.g. <code>work_mode</code>). Each key once.</li>
          <li>Values: non-empty text up to {MAX_VALUE} characters. Numbers and true/false must be written as text (<code>"3"</code>).</li>
          <li>Up to {MAX_KEYS} fields per job.</li>
        </ul>
        <Note icon={Info}>
          <b>{KNOWN_KEYS.join(", ")}</b> fill the job's own fields. Any other key is sent to the LLM as extra context
          when it extracts the requirements.
        </Note>
        <p className="dm-soft" style={{ margin: 0 }}>Every job needs a title and a description of {MIN_JD}+ characters. Up to {MAX_JOBS} jobs per batch.</p>
      </aside>
    </div>
  );
}
