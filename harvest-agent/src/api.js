// `??` (not `||`) so an explicit empty string — same-origin deployment behind
// nginx, see harvest-agent/Dockerfile — isn't overridden by the dev default.
const API_BASE = process.env.REACT_APP_API_BASE_URL ?? "http://localhost:8000";

class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    // Send the HttpOnly session cookie on every call — this is how the browser
    // authenticates now (no bearer token is stored in JS). Required for the
    // cross-origin dev setup (localhost:3000 → :8000) to include credentials.
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });
  let body = null;
  try {
    body = await res.json();
  } catch {
    // no JSON body
  }
  if (!res.ok) {
    // A 401 on any non-auth endpoint means the session expired or was revoked.
    // Signal App.js to fall back to login; the /auth/* calls handle their own
    // 401s (wrong OTP / unauthenticated /auth/me on startup) inline.
    if (res.status === 401 && !path.startsWith("/auth/")) {
      window.dispatchEvent(new Event("auth:logout"));
    }
    // FastAPI uses {detail}; our HarvestException handler nests it under
    // {error: {message}} (e.g. the 409 "a harvest is already running").
    const message =
      (body && (body.detail || body.message || (body.error && body.error.message))) ||
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

// ── Auth (OTP email login) ─────────────────────────────────────────────────────

/** POST /auth/request-otp — email a login OTP. 429 (with Retry-After) if on cooldown; 422 if not an @sightspectrum.com address. */
export function requestOtp(email) {
  return request("/auth/request-otp", { method: "POST", body: JSON.stringify({ email }) });
}

/** POST /auth/verify-otp — exchange the OTP for a JWT; returns {access_token, token_type}. Generic 401 on wrong/expired/consumed/too-many. */
export function verifyOtp(email, otp) {
  return request("/auth/verify-otp", { method: "POST", body: JSON.stringify({ email, otp }) });
}

/** GET /auth/me — the current authenticated user; used to validate the session cookie on load. */
export function getMe() {
  return request("/auth/me");
}

/** POST /auth/logout — revoke the current session and clear its cookie. */
export function logout() {
  return request("/auth/logout", { method: "POST" });
}

// ── Jobs ─────────────────────────────────────────────────────────────────────

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

/** POST /run-harvest-agent/stop — cooperatively stop the in-flight harvest. The
 * run saves the jobs gathered so far, is marked "stopped", and its report email
 * is deferred (merged into the next successful run). Returns {status, job_id,
 * run_id} — status "stopping" when a run was signalled, "idle" when none ran. */
export function stopHarvest() {
  return request("/run-harvest-agent/stop", { method: "POST" });
}

/** GET /run-history — all past harvest runs, newest first. */
export function getRunHistory() {
  return request("/run-history");
}

/** GET /active-run — whether a harvest is currently running (any source/tab).
 * Returns {active, job_id, run_id, source}. Used to freeze the Run controls. */
export function getActiveRun() {
  return request("/active-run");
}

/** GET /harvest-status/{jobId} — live progress for an in-flight background harvest job. */
export function getHarvestStatus(jobId) {
  return request(`/harvest-status/${jobId}`);
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

// ── Outreach (LLM email / LinkedIn generation + send) ──────────────────────────

/** POST /outreach/generate-email — draft a recruiter email; returns {subject, body, from_email, to_email, client_type, tone, fallback_used, attachment_name}. */
export function generateOutreachEmail({ job_id, mode, regenerate } = {}) {
  return request("/outreach/generate-email", {
    method: "POST",
    body: JSON.stringify({ job_id, mode, regenerate: !!regenerate }),
  });
}

/** POST /outreach/generate-linkedin — draft a LinkedIn outreach message; returns {message, fallback_used}. */
export function generateLinkedinMessage({ job_id, regenerate } = {}) {
  return request("/outreach/generate-linkedin", {
    method: "POST",
    body: JSON.stringify({ job_id, regenerate: !!regenerate }),
  });
}

/** POST /outreach/send-email — send the (possibly edited) email with the pptx attached; returns {status, error}. */
export function sendOutreachEmail({ job_id, to_email, from_email, subject, body, tone, client_type, fallback_used } = {}) {
  return request("/outreach/send-email", {
    method: "POST",
    body: JSON.stringify({ job_id, to_email, from_email, subject, body, tone, client_type, fallback_used: !!fallback_used }),
  });
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
