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

/** GET /demand/batches — this tenant's batches, newest first. */
export function getDemandBatches() {
  return request("/demand/batches");
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
