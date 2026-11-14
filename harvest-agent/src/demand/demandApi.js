import { request, uploadRequest, ApiError } from "../api";

/* Demand Search client — every call goes to the backend's /demand/* router
 * (ai-harvest-agent/app/routes/demand_routes.py), through api.js's shared request
 * helpers (session cookie, dev-tenant header, error shape).
 *
 * Errors: the backend sends {detail: {code, message, …}} for Demand problems
 * (harvest_running, session_logged_out, resdex_disabled, file errors …).
 * demandError() unpacks that into {code, message, detail} for the UI. */

/** {code, message, detail} for any thrown error (network failures → code "network"). */
export function demandError(err) {
  if (err instanceof ApiError) {
    const d = err.body && typeof err.body.detail === "object" && err.body.detail ? err.body.detail : null;
    return { code: d?.code || `http_${err.status}`, message: d?.message || err.message, detail: d || {}, status: err.status };
  }
  return { code: "network", message: "Could not reach the harvest backend.", detail: {}, status: 0 };
}

/** POST /demand/validate — parse + check an upload; writes nothing. */
export function demandValidate(file) {
  const fd = new FormData();
  fd.append("file", file);
  return uploadRequest("/demand/validate", fd);
}

/** POST /demand/upload — start a batch; 202 {job_id, run_id}. */
export function demandUpload(file, batchName) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("batch_name", batchName || "");
  return uploadRequest("/demand/upload", fd);
}

/** Build a "?k=v" query string, skipping undefined/null/empty values and the "all" filter
 * sentinel. Returns "" when nothing remains. */
function qs(params) {
  const usp = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "" && v !== "all") usp.set(k, v);
  });
  const s = usp.toString();
  return s ? `?${s}` : "";
}

/** POST /demand/validate-json — check manually entered jobs; writes nothing.
 * body: {batch_name, jobs: [{job_title, job_description, metadata: {key: "text"}}]}.
 * Same response as demandValidate (filename "Manual entry", source_kind "manual");
 * 422 {code: "invalid_jobs", errors: [{job, field, key, message}]} for bad metadata. */
export function validateJson(body) {
  return request("/demand/validate-json", { method: "POST", body: JSON.stringify(body) });
}

/** POST /demand/create — start a batch from manual jobs; 202 {job_id, run_id}
 * (same errors as demandUpload). */
export function createManual(body) {
  return request("/demand/create", { method: "POST", body: JSON.stringify(body) });
}

/** GET /demand/batches — this tenant's batches, newest first.
 * params: q, status (running|completed|stopped|failed), source (file|manual),
 * date_from, date_to (YYYY-MM-DD), limit (≤100, default 20), offset.
 * → {total, limit, offset, batches}. */
export function listBatches(params = {}) {
  return request(`/demand/batches${qs(params)}`);
}

/** Back-compat alias (first page of batches). */
export function getDemandBatches() {
  return listBatches();
}

/** GET /demand/jobs — every Demand job across batches (batch-page row shape + run_id,
 * batch_name, source_kind, uploaded_on). params: q, status (jobs-table filter
 * values), source, date_from, date_to, limit (≤100), offset.
 * → {total, limit, offset, counts, jobs}. */
export function listJobs(params = {}) {
  return request(`/demand/jobs${qs(params)}`);
}

/** GET /demand/batches/{run_id} — header + every row's live / final status. */
export function getDemandBatch(runId) {
  return request(`/demand/batches/${encodeURIComponent(runId)}`);
}

/** POST /demand/batches/{run_id}/stop — stop after the current row. */
export function stopDemandBatch(runId) {
  return request(`/demand/batches/${encodeURIComponent(runId)}/stop`, { method: "POST" });
}

/** GET /demand/jobs/{job_id} — job, batch row, LLM context and latest Resdex search. */
export function getDemandJob(jobId) {
  return request(`/demand/jobs/${encodeURIComponent(jobId)}`);
}

/** POST /demand/jobs/{job_id}/rerun-resdex — fresh Resdex search for one job (202). */
export function rerunDemandJob(jobId) {
  return request(`/demand/jobs/${encodeURIComponent(jobId)}/rerun-resdex`, { method: "POST" });
}

/** GET /demand/resdex-usage — today's shared Resdex quota, the recorded recruiter
 * session and any running harvest / batch. */
export function getDemandUsage() {
  return request("/demand/resdex-usage");
}
