import { jobContact } from "./jobsData";

/* ── Contact filter (shared) ───────────────────────────────────────────────
 * One token set everywhere: the Harvested Jobs page sends it as GET /jobs'
 * `contact` param, the Run History page as GET /run-history/contact-match's,
 * and the run-detail view evaluates it in-memory via jobMatchesContact(). The
 * backend understands the same tokens with the same semantics
 * (CONTACT_FILTER_TOKENS / _contact_token_exprs in harvest_run_service.py).
 * Email/Phone include the Company-tagged contact.
 *
 * Options are grouped for a professional dropdown; `summaryLabel` keeps the
 * collapsed chip unambiguous ("Has email" vs "No email") while the open list
 * stays short under its group header.
 */
export const CONTACT_FILTER_OPTIONS = [
  { group: "With contact" },
  { value: "email",         label: "Email",                         summaryLabel: "Email" },
  { value: "email_poster",  label: "Poster email (job / recruiter)", summaryLabel: "Poster email" },
  { value: "email_company", label: "Apollo Resolved email",         summaryLabel: "Apollo Resolved email" },
  { value: "mobile",        label: "Phone",                         summaryLabel: "Phone" },
  { value: "linkedin",      label: "LinkedIn",                      summaryLabel: "LinkedIn" },
  // Resdex matches live in resdex_searches, not on the job rows, so a purely
  // client-side table can only evaluate this token when the caller has stamped
  // `hasResdex` on its rows (RunDetailView does, via GET /jobs?contact=resdex).
  // Otherwise it is hidden (CLIENT_CONTACT_FILTER_OPTIONS).
  { group: "Resdex", serverOnly: true },
  { value: "resdex", label: "Resdex contacts", summaryLabel: "Resdex contacts", serverOnly: true },
  { group: "Without contact" },
  { value: "no_email",    label: "No Email",    summaryLabel: "No Email" },
  { value: "no_mobile",   label: "No Phone",    summaryLabel: "No Phone" },
  { value: "no_linkedin", label: "No LinkedIn", summaryLabel: "No LinkedIn" },
  { value: "none", label: "No contact on file", summaryLabel: "No contact" },
];
export const CLIENT_CONTACT_FILTER_OPTIONS = CONTACT_FILTER_OPTIONS.filter((o) => !o.serverOnly);

// Positive tokens match rows that HAVE that channel; "no_*" tokens match rows
// MISSING it; "none" matches rows with no contact at all. Tokens are OR-combined
// (a row passes if it satisfies any selected one). Empty selection = no filter.
// Presence comes from jobContact(), i.e. exactly what the Email/Mobile cells show
// — poster/recruiter AND Company-tagged contacts, for every source. "resdex"
// reads the `hasResdex` flag a caller stamps from the server (false if absent).
export function jobMatchesContact(j, contact) {
  if (!contact || contact.length === 0) return true;
  const c = jobContact(j);
  const has = { email: !!c.email, mobile: !!c.phone, linkedin: !!c.linkedin };
  const none = !has.email && !has.mobile && !has.linkedin;
  return contact.some((t) =>
    t === "email" ? has.email
      : t === "email_poster" ? c.hasPosterEmail
      : t === "email_company" ? c.hasCompanyEmail
      : t === "mobile" ? has.mobile
      : t === "linkedin" ? has.linkedin
      : t === "resdex" ? !!j.hasResdex
      : t === "no_email" ? !has.email
      : t === "no_mobile" ? !has.mobile
      : t === "no_linkedin" ? !has.linkedin
      : t === "none" ? none
      : false);
}
