// `??` (not `||`) so an explicit empty string — same-origin deployment behind
// nginx, see harvest-agent/Dockerfile — isn't overridden by the dev default.
const API_BASE = process.env.REACT_APP_API_BASE_URL ?? "http://localhost:8001";

class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let body = null;
  try {
    body = await res.json();
  } catch {
    // no JSON body
  }
  if (!res.ok) {
    const message =
      (body && (body.detail || body.message)) ||
      `Request to ${path} failed with status ${res.status}`;
    throw new ApiError(typeof message === "string" ? message : JSON.stringify(message), res.status, body);
  }
  return body;
}

/*
 * Full client covering every endpoint in the FastAPI Integration Guide (api.txt),
 * plus GET /jobs (real, live, not in the doc, but the only endpoint that returns
 * individual job records — needed for the Harvested Jobs table).
 */

function qs(params) {
  const usp = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") usp.set(k, v);
  });
  const s = usp.toString();
  return s ? `?${s}` : "";
}

/** GET /jobs — paginated, filterable, sortable list of harvested jobs. */
export function getJobs(params = {}) {
  return request(`/jobs${qs(params)}`);
}

/** GET /harvest-config — current Rule Engine configuration. */
export function getHarvestConfig() {
  return request("/harvest-config");
}

/** PUT /harvest-config — persist a full HarvestConfig object. */
export function saveHarvestConfig(config) {
  return request("/harvest-config", { method: "PUT", body: JSON.stringify(config) });
}

/** POST /run-harvest-agent — kicks off a background harvest run; returns {job_id, run_id, status}. */
export function runHarvestAgent() {
  return request("/run-harvest-agent", { method: "POST", body: JSON.stringify({ config_id: "active" }) });
}

/** GET /run-history — all past harvest runs, newest first. */
export function getRunHistory() {
  return request("/run-history");
}

/** GET /run-history/{runId} — a single run's summary. */
export function getRunHistoryEntry(runId) {
  return request(`/run-history/${runId}`);
}

/** POST /linkedin-setup-session — opens a Chrome window for one-time manual LinkedIn login. Blocks up to 10 min. */
export function setupLinkedinSession() {
  return request("/linkedin-setup-session", { method: "POST" });
}

/** POST /naukri-setup-session — opens a Chrome window for one-time manual Naukri login. Blocks up to 10 min. */
export function setupNaukriSession() {
  return request("/naukri-setup-session", { method: "POST" });
}

// ── Health ───────────────────────────────────────────────────────────────────

/** GET /health — liveness check. */
export function getHealth() {
  return request("/health");
}

/** GET /health/ready — readiness check. */
export function getHealthReady() {
  return request("/health/ready");
}

/** GET /health/live — Kubernetes liveness probe. */
export function getHealthLive() {
  return request("/health/live");
}

// ── Single-source harvest agents (synchronous — block until the run completes) ─

/** POST /run-linkedin-agent — synchronous LinkedIn-only harvest. Can take minutes. */
export function runLinkedinAgent() {
  return request("/run-linkedin-agent", { method: "POST" });
}

/** GET /linkedin-results — list all saved LinkedIn run files. */
export function getLinkedinResults() {
  return request("/linkedin-results");
}

/** GET /linkedin-results/{runId} — one saved LinkedIn run's full jobs payload. */
export function getLinkedinResult(runId) {
  return request(`/linkedin-results/${runId}`);
}

/** POST /run-naukri-agent — synchronous Naukri-only harvest. Can take minutes. */
export function runNaukriAgent() {
  return request("/run-naukri-agent", { method: "POST" });
}

/** GET /naukri-results — list all saved Naukri run files. */
export function getNaukriResults() {
  return request("/naukri-results");
}

/** GET /naukri-results/{runId} — one saved Naukri run's full jobs payload. */
export function getNaukriResult(runId) {
  return request(`/naukri-results/${runId}`);
}

/** POST /run-dice-agent — synchronous Dice-only harvest. Can take minutes. */
export function runDiceAgent() {
  return request("/run-dice-agent", { method: "POST" });
}

/** GET /dice-results — list all saved Dice run files. */
export function getDiceResults() {
  return request("/dice-results");
}

/** GET /dice-results/{runId} — one saved Dice run's full jobs payload. */
export function getDiceResult(runId) {
  return request(`/dice-results/${runId}`);
}

// ── Prospect Intelligence (manual prospects.xlsx enrichment) ───────────────────

/** POST /run-prospect-intelligence — enrich a prospects.xlsx file. Synchronous. */
export function runProspectIntelligence({ input_file, concurrency } = {}) {
  const body = {};
  if (input_file) body.input_file = input_file;
  if (concurrency) body.concurrency = concurrency;
  return request("/run-prospect-intelligence", { method: "POST", body: JSON.stringify(body) });
}

/** GET /prospect-results — list all past prospect intelligence runs. */
export function getProspectResults() {
  return request("/prospect-results");
}

/** GET /prospect-results/{runId} — one prospect intelligence run's full output. */
export function getProspectResult(runId) {
  return request(`/prospect-results/${runId}`);
}

// ── Recruiter Contact Discovery (auto-enrich from harvest results) ─────────────

/** POST /run-recruiter-discovery — enrich recruiters found in past harvest runs. Synchronous. */
export function runRecruiterDiscovery({ source_filter, run_ids, max_files, concurrency } = {}) {
  const body = {};
  if (source_filter) body.source_filter = source_filter;
  if (run_ids && run_ids.length) body.run_ids = run_ids;
  if (max_files) body.max_files = max_files;
  if (concurrency) body.concurrency = concurrency;
  return request("/run-recruiter-discovery", { method: "POST", body: JSON.stringify(body) });
}

// ── Downloads ────────────────────────────────────────────────────────────────

/** GET /download/json — latest combined harvest JSON (URL for direct download). */
export function downloadJsonUrl() {
  return `${API_BASE}/download/json`;
}

/** GET /download/excel — latest combined harvest Excel report (URL for direct download). */
export function downloadExcelUrl() {
  return `${API_BASE}/download/excel`;
}

export { ApiError, API_BASE };
