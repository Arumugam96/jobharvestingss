import { useEffect, useState } from "react";
import { X, Check, RefreshCw, AlertCircle, Pencil } from "lucide-react";
import { updateJob, ApiError } from "../api";
import { DOMAINS } from "../lib/ruleEngineOptions";
import { GLASS_MODAL, GLASS_BTN, GLASS_INPUT, GLASS_INPUT_FOCUS } from "../theme";

/* "Edit job details" for the job-detail page (admins + business leads). Edits the
 * existing columns only — hiring entity, company name/size, domain and the POC's
 * name/designation — via PATCH /jobs/{id}; only changed fields are sent. The
 * optional "apply to all jobs from {company}" copies the company-level fields
 * (hiring entity, size, domain) to every job of that company server-side.
 * Styling uses the `ejm-` prefix. */

// Mirror of the backend's accepted values (job_edit_routes.HIRING_ENTITIES /
// COMPANY_SIZE_VALUES — app/core/company_size.py BAND_TO_TIER).
const HIRING_ENTITY_OPTIONS = ["Direct Client", "GCC", "Staffing Firm", "Ambiguous"];
const COMPANY_SIZE_OPTIONS = [
  "2-10", "11-50", "51-200", "201-500", "501-1,000", "1,001-5,000", "5,001-10,000", "10,001+",
].map((band) => `${band} employees`);
const DOMAIN_SUGGESTIONS = DOMAINS.filter((d) => d !== "Any");

const styles = `
.ejm-overlay { position: fixed; inset: 0; background: rgba(15,23,42,.45); -webkit-backdrop-filter: blur(4px); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; padding: 24px; z-index: 1000; }
.ejm-card { width: 100%; max-width: 520px; max-height: calc(100vh - 48px); ${GLASS_MODAL} border-radius: 12px; display: flex; flex-direction: column; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1E293B; }
.ejm-head { display: flex; align-items: center; justify-content: space-between; padding: 16px 22px; border-bottom: 1px solid #E2E8F0; }
.ejm-title { display: inline-flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 600; }
.ejm-close { width: 30px; height: 30px; border: none; background: transparent; border-radius: 6px; color: #64748B; display: flex; align-items: center; justify-content: center; cursor: pointer; }
.ejm-close:hover { background: #F1F5F9; color: #1E293B; }
.ejm-body { padding: 18px 22px; display: grid; grid-template-columns: 1fr 1fr; gap: 12px 14px; overflow-y: auto; }
.ejm-field { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.ejm-field.full { grid-column: 1 / -1; }
.ejm-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: #94A3B8; }
.ejm-input, .ejm-select { width: 100%; ${GLASS_INPUT} border-radius: 8px; padding: 9px 11px; font-size: 13.5px; color: #1E293B; font-family: inherit; box-sizing: border-box; }
.ejm-select { cursor: pointer; }
.ejm-input:focus, .ejm-select:focus { outline: none; border-color: #2563EB; box-shadow: 0 0 0 3px rgba(37,99,235,.12); ${GLASS_INPUT_FOCUS} }
.ejm-check { grid-column: 1 / -1; display: flex; gap: 9px; align-items: flex-start; font-size: 13px; color: #334155; background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px; padding: 10px 12px; cursor: pointer; }
.ejm-check input { margin-top: 2px; accent-color: #2563EB; cursor: pointer; }
.ejm-err { grid-column: 1 / -1; display: flex; align-items: center; gap: 7px; font-size: 12.5px; border-radius: 8px; padding: 8px 11px; background: #FEF2F2; border: 1px solid #FCA5A5; color: #B91C1C; }
.ejm-foot { display: flex; align-items: center; justify-content: flex-end; gap: 8px; padding: 13px 22px; border-top: 1px solid rgba(226,232,240,.9); background: rgba(248,250,252,.8); }
.ejm-fbtn { display: inline-flex; align-items: center; gap: 6px; border-radius: 8px; font-size: 13px; font-weight: 600; padding: 9px 14px; cursor: pointer; font-family: inherit; border: 1px solid transparent; }
.ejm-fbtn:disabled { opacity: .5; cursor: not-allowed; }
.ejm-ghost { ${GLASS_BTN} color: #475569; }
.ejm-ghost:hover:not(:disabled) { background: rgba(255,255,255,.95); border-color: #CBD5E1; color: #1E293B; }
.ejm-primary { background: #2563EB; color: #fff; }
.ejm-primary:hover:not(:disabled) { background: #1E40AF; }
.ejm-spin { animation: ejm-rotate .8s linear infinite; }
@keyframes ejm-rotate { to { transform: rotate(360deg); } }
@media (max-width: 640px) { .ejm-overlay { padding: 0; align-items: flex-end; } .ejm-card { max-width: 100%; border-radius: 12px 12px 0 0; } .ejm-body { grid-template-columns: 1fr; } }
`;

/** The form's starting values from a detail-shaped job (mapJobToDetail). */
const initialForm = (job) => ({
  hiring_entity: HIRING_ENTITY_OPTIONS.includes(job.hiringEntity) ? job.hiringEntity : "",
  company: job.company && job.company !== "—" ? job.company : "",
  company_size: job.companySize || "",
  domain: job.domain && job.domain !== "Any" ? job.domain : "",
  job_poster_name: job.posterName || "",
  job_poster_designation: job.posterTitle || "",
});

/**
 * Modal editing a job's hiring entity, company name/size, domain and POC
 * name/designation. Sends only the changed fields to PATCH /jobs/{id}; with the
 * company checkbox ticked it also sends the (set) company-level fields so they are
 * copied to every job of that company. Server errors (403 / 422) are shown inline.
 * @param {object} props
 * @param {object} props.job detail-shaped job (mapJobToDetail)
 * @param {(res: {job: object, updated_other_jobs: number}) => void} props.onSaved
 *   gets the PATCH response (raw API job view) on success
 * @param {() => void} props.onClose
 */
export default function EditJobModal({ job, onSaved = () => {}, onClose = () => {} }) {
  const [initial] = useState(() => initialForm(job));
  const [form, setForm] = useState(initial);
  const [applyToCompany, setApplyToCompany] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    /** Closes the modal on Escape unless a save is in flight. */
    const onKey = (e) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, busy]);

  const set = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  // Changed fields only; with "apply to company" the set company-level fields
  // are sent even when unchanged, so they reach the company's other jobs.
  const buildBody = () => {
    const body = {};
    Object.keys(form).forEach((k) => {
      const v = form[k].trim();
      if (v !== initial[k].trim()) body[k] = v;
    });
    if (applyToCompany) {
      body.apply_to_company = true;
      ["hiring_entity", "company_size", "domain"].forEach((k) => {
        if (form[k].trim() && !(k in body)) body[k] = form[k].trim();
      });
    }
    return body;
  };
  const body = buildBody();
  const hasChanges = Object.keys(body).some((k) => k !== "apply_to_company");

  /** PATCH the job; reports the response via onSaved and closes, or shows the error. */
  const save = async () => {
    if (!hasChanges) { onClose(); return; }
    if (!form.company.trim()) { setError("Company name can't be empty."); return; }
    setBusy(true);
    setError("");
    try {
      const res = await updateJob(job.id, body);
      onSaved(res);
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  const companyName = form.company.trim() || job.company || "this company";
  // A stored size outside the canonical bands (legacy en-dash rows) stays selectable.
  const sizeOptions = initial.company_size && !COMPANY_SIZE_OPTIONS.includes(initial.company_size)
    ? [initial.company_size, ...COMPANY_SIZE_OPTIONS]
    : COMPANY_SIZE_OPTIONS;

  return (
    <div className="ejm-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <style>{styles}</style>
      <div className="ejm-card" role="dialog" aria-modal="true" aria-label="Edit job details">
        <div className="ejm-head">
          <span className="ejm-title"><Pencil size={15} /> Edit job details</span>
          <button className="ejm-close" onClick={onClose} disabled={busy} aria-label="Close"><X size={17} /></button>
        </div>

        <div className="ejm-body">
          <label className="ejm-field">
            <span className="ejm-label">Hiring entity</span>
            <select className="ejm-select" value={form.hiring_entity} onChange={set("hiring_entity")}>
              {!initial.hiring_entity && <option value="">Unclassified</option>}
              {HIRING_ENTITY_OPTIONS.map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          </label>
          <label className="ejm-field">
            <span className="ejm-label">Company size</span>
            <select className="ejm-select" value={form.company_size} onChange={set("company_size")}>
              <option value="">Unknown</option>
              {sizeOptions.map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          </label>
          <label className="ejm-field">
            <span className="ejm-label">Company name</span>
            <input className="ejm-input" value={form.company} onChange={set("company")} maxLength={500} />
          </label>
          <label className="ejm-field">
            <span className="ejm-label">Domain</span>
            <input className="ejm-input" value={form.domain} onChange={set("domain")} maxLength={50}
              list="ejm-domains" placeholder="e.g. Data Engineering" />
            <datalist id="ejm-domains">
              {DOMAIN_SUGGESTIONS.map((d) => <option key={d} value={d} />)}
            </datalist>
          </label>
          <label className="ejm-field">
            <span className="ejm-label">POC name</span>
            <input className="ejm-input" value={form.job_poster_name} onChange={set("job_poster_name")} maxLength={500} />
          </label>
          <label className="ejm-field">
            <span className="ejm-label">POC designation</span>
            <input className="ejm-input" value={form.job_poster_designation} onChange={set("job_poster_designation")} maxLength={1000} />
          </label>
          <label className="ejm-check">
            <input type="checkbox" checked={applyToCompany} onChange={(e) => setApplyToCompany(e.target.checked)} />
            <span>Apply Hiring entity, Company size and Domain to all jobs from <strong>{companyName}</strong>, including future harvests</span>
          </label>
          {error && <div className="ejm-err"><AlertCircle size={14} /> {error}</div>}
        </div>

        <div className="ejm-foot">
          <button className="ejm-fbtn ejm-ghost" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="ejm-fbtn ejm-primary" onClick={save} disabled={busy || !hasChanges}>
            {busy ? <RefreshCw size={14} className="ejm-spin" /> : <Check size={14} />} Save
          </button>
        </div>
      </div>
    </div>
  );
}
