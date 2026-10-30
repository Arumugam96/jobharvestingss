import { getJobs } from "../api";

/* ── Source + contact normalization ────────────────────────────────────── */
// The backend persists every source (LinkedIn / Naukri / Dice / Home Feed) into
// one ScrapedJob shape: the poster/recruiter lands in job_poster_name / email_id /
// contact_number, and the inferred company(+location) HR contact in the separate
// company_contact_* fields. Everything that reads a row's contact info — the jobs
// table cells, the filters, the run-view header stats and the CSV exports — goes
// through jobContact() below, so what is counted/filtered/exported is exactly
// what the table shows, for every source.

// Canonical display names (matched case-insensitively), so the Source filter,
// SourceChip tones and sorting see one spelling per source.
const SOURCE_DISPLAY = {
  linkedin: "LinkedIn",
  naukri: "Naukri",
  dice: "Dice",
  "linkedin feed": "LinkedIn Feed",
  demand: "Demand",
};
// Preferred order of the Source filter's options; any other source sorts after, A→Z.
// "Demand" (Demand Search uploads) is hidden from GET /jobs unless picked here.
export const SOURCE_ORDER = ["LinkedIn", "Naukri", "Dice", "LinkedIn Feed", "Demand"];

export function canonicalSource(raw) {
  const s = raw == null ? "" : String(raw).trim();
  if (!s) return "—";
  return SOURCE_DISPLAY[s.toLowerCase()] || s;
}

export function sortSources(list) {
  const rank = (s) => {
    const i = SOURCE_ORDER.indexOf(s);
    return i === -1 ? SOURCE_ORDER.length : i;
  };
  return [...list].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

const clean = (v) => (v == null ? "" : String(v).trim());

/**
 * Distinct, present values for one contact channel in display priority order:
 * the value scraped from the job post ("Job"), the enriched recruiter record's
 * ("Recruiter"), then the inferred company(+job-location) HR contact
 * ("Apollo Resolved"). Identical strings are deduped. Older JSON-read rows carry only
 * the merged poster value (no split fields) — it stands in for Job/Recruiter
 * (labeled "Contact" when shown next to a Company value). Mirrored server-side
 * by job_dict_contact()/_contact_presence_exprs() in harvest_run_service.py.
 */
export function contactParts(scraped, recruiter, company, fallback) {
  const s = clean(scraped);
  const r = clean(recruiter);
  const c = clean(company);
  const parts = [];
  if (s) parts.push({ variant: "job", label: "Job", val: s });
  if (r && r !== s) parts.push({ variant: "recruiter", label: "Recruiter", val: r });
  if (parts.length === 0) {
    const f = clean(fallback);
    if (f) parts.push({ variant: "contact", label: "Contact", val: f });
  }
  if (c && !parts.some((p) => p.val === c)) parts.push({ variant: "company", label: "Enriched", val: c });
  return parts;
}

// "poster" = the job's own POC (scraped from the posting or the poster's
// enriched recruiter record); "company" = only the inferred company contact.
const contactKind = (parts) =>
  parts.length === 0 ? null : parts[0].variant === "company" ? "company" : "poster";

function deriveJobContact(j) {
  const emailParts = contactParts(j.emailScraped, j.emailRecruiter, j.emailCompany, j.email);
  const phoneParts = contactParts(j.mobileScraped, j.mobileRecruiter, j.mobileCompany, j.mobile);
  const posterName = clean(j.poc);
  const companyName = clean(j.companyContactName);
  return {
    source: canonicalSource(j.source),
    // The poster is the POC; the company contact stands in only when no poster
    // name was captured (the backend attaches it only to email-less jobs).
    pocName: posterName || companyName,
    pocKind: posterName ? "poster" : companyName ? "company" : null,
    email: emailParts.length ? emailParts[0].val : "",
    emailKind: contactKind(emailParts),
    hasPosterEmail: emailParts.some((p) => p.variant !== "company"),
    hasCompanyEmail: emailParts.some((p) => p.variant === "company"),
    emailParts,
    phone: phoneParts.length ? phoneParts[0].val : "",
    phoneKind: contactKind(phoneParts),
    phoneParts,
    linkedin: clean(j.linkedin),
  };
}

/**
 * Source-agnostic contact view of a mapped job row:
 *   { source, pocName, pocKind, email, emailKind, hasPosterEmail,
 *     hasCompanyEmail, emailParts, phone, phoneKind, phoneParts, linkedin }
 * Kinds are "poster" | "company" | null. Rows from mapApiJob carry it
 * precomputed as `row.contact`; anything else is derived on the fly.
 */
export function jobContact(j) {
  return (j && j.contact) || deriveJobContact(j || {});
}

// CSV label for a POC kind / the source tag of a channel's primary value.
export const pocKindLabel = (kind) => (kind === "poster" ? "Job poster" : kind === "company" ? "Enriched contact" : "—");
export const partSourceLabel = (parts) => (parts.length && parts[0].label) || "—";

/* ── API job → table row shape ─────────────────────────────────────────── */
// `poc` / `email` / `mobile` / `whatsapp` / `linkedin` stay the poster/recruiter
// values (they feed the outreach composers and the job-detail Point of Contact);
// `contact` is the normalized all-sources view (see jobContact above).
export function mapApiJob(j) {
  const row = mapApiJobFields(j);
  row.contact = deriveJobContact(row);
  return row;
}

function mapApiJobFields(j) {
  return {
    id: j.id,
    title: j.job_title || "Untitled role",
    company: j.company || "—",
    poc: j.job_poster_name || null,
    postedDate: j.posted_date || "",
    email: j.email_id || null,
    whatsapp: j.contact_number || null,
    mobile: j.contact_number || null,
    // Source-split contact values so the UI can label Job: vs Recruiter: when
    // both exist. email/mobile above stay the merged value used for search,
    // sort, mailto:/tel: links and the contact-action icons.
    emailScraped: j.email_scraped || null,
    emailRecruiter: j.email_recruiter || null,
    // "apollo" when Apollo revealed the recruiter email (else null/""), and the
    // resolved outreach contact's origin (job_post | recruiter_profile | apollo | apollo_hr).
    emailRecruiterSource: j.email_recruiter_source || null,
    outreachToSource: j.outreach_to_source || null,
    mobileScraped: j.phone_scraped || null,
    mobileRecruiter: j.phone_recruiter || null,
    // Inferred HR contact found by (company + job location) via Apollo, attached
    // only to jobs with no scraped-poster and no enriched-recruiter email. Shown
    // inline with an "Apollo Resolved" tag in the table; its own section in the detail view.
    emailCompany: j.company_contact_email || null,
    mobileCompany: j.company_contact_phone || null,
    companyContactName: j.company_contact_name || null,
    companyContactTitle: j.company_contact_title || null,
    companyContactLocation: j.company_contact_location || null,
    companyContactSource: j.company_contact_source || null,
    linkedin: j.linkedin_profile_url || null,
    source: canonicalSource(j.source),
    jobDescription: j.job_description || "",
    jobDescriptionHtml: j.job_description_html || "",
    location: j.location || "",
    salary: j.salary || "",
    jobType: j.job_type || "",
    workMode: j.work_mode || "",
    // Only a real web link is an apply link — Demand Search rows carry a synthetic
    // demand://jd/… key in job_url.
    applyLink: /^https?:\/\//i.test(j.job_url || "") ? j.job_url : "",
    companyUrl: j.company_url || "",
    // Company-size band + friendly tier (Small/Medium/Large/Enterprise or "" ⇒
    // Unknown) captured at harvest — drives the display-only Company-size filter.
    companySize: j.company_size || "",
    companySizeTier: j.company_size_tier || "",
    // Job-location country (parsed from the free-text `location`) and the
    // company's HQ country/state (enrichment waterfall) — power the two Country
    // filters and the job-detail Company card.
    country: j.country || "",
    companyCountry: j.company_country || "",
    companyState: j.company_state || "",
    posterTitle: j.job_poster_designation || "",
    domain: j.domain || "",
    hiringEntity: j.hiring_entity || "",
    verificationStatus: j.verification_status || "",
    isGcc: !!j.is_gcc,
    // Business-filter annotation — the backend no longer drops jobs, it flags
    // them. passed_filter defaults to true for legacy rows lacking the column.
    passedFilter: j.passed_filter !== false,
    filterReason: j.filter_reason || "",
  };
}

export function mapJobToDetail(j) {
  return {
    // id/email/linkedin are carried through so the detail view's Email/LinkedIn
    // icons can open the same LLM outreach composers the table rows use
    // (EmailComposeModal/LinkedInMessageModal read job.id + job.email + job.company).
    id: j.id,
    email: j.email || "",
    linkedin: j.linkedin || "",
    jobTitle: j.title,
    jd: j.jobDescription,
    jdHtml: j.jobDescriptionHtml || "",
    company: j.company,
    location: j.location,
    jobType: j.jobType,
    salary: j.salary,
    postedDate: j.postedDate,
    applyLink: j.applyLink,
    companyUrl: j.companyUrl || "",
    posterName: j.poc || "",
    posterLinkedIn: j.linkedin || "",
    posterTitle: j.posterTitle || "",
    posterContact: {
      email: j.email || "",
      mobile: j.mobile || "",
      emailScraped: j.emailScraped || "",
      emailRecruiter: j.emailRecruiter || "",
      emailRecruiterSource: j.emailRecruiterSource || "",
      mobileScraped: j.mobileScraped || "",
      mobileRecruiter: j.mobileRecruiter || "",
    },
    // Inferred (company + job-location) HR contact — rendered as its own detail
    // section, kept out of posterContact so Point of Contact stays untouched.
    companyContact: {
      name: j.companyContactName || "",
      title: j.companyContactTitle || "",
      email: j.emailCompany || "",
      phone: j.mobileCompany || "",
      location: j.companyContactLocation || "",
      source: j.companyContactSource || "",
    },
    source: j.source,
    domain: j.domain || "",
    hiringEntity: j.hiringEntity || "",
    // Company-size band + friendly tier, so the detail view can show them. Accept
    // either the mapped (companySize) or raw (company_size) shape defensively.
    companySize: j.companySize || j.company_size || "",
    companySizeTier: j.companySizeTier || j.company_size_tier || "",
    // Company HQ location (enrichment waterfall) for the detail Company card.
    companyCountry: j.companyCountry || j.company_country || "",
    companyState: j.companyState || j.company_state || "",
    passedFilter: j.passedFilter,
    filterReason: j.filterReason || "",
  };
}

export function mapRun(entry) {
  return {
    runId: entry.run_id,
    sources: entry.sources || [],
    // run_type distinguishes a LinkedIn Home Feed lead run ("feed") from a
    // job-harvest run ("harvest") so the Run History page renders them apart.
    runType: entry.run_type || "harvest",
    status: entry.status || "no_results",
    startedAt: entry.started_at || "",
    completedAt: entry.completed_at || "",
    jobsFound: entry.jobs_found ?? 0,
    verifiedJobs: entry.verified_jobs ?? 0,
    directClients: entry.direct_clients ?? 0,
    gcc: entry.gcc ?? 0,
    staffingFirms: entry.staffing_firms ?? 0,
    ambiguous: entry.ambiguous ?? 0,
  };
}

// Load EVERY row matching `baseParams` in 100-row pages — page 1 first to learn
// total_pages, then the remaining pages in parallel. Used where the full result
// set is genuinely needed: the run-detail view (one run's jobs) and the Jobs
// page's CSV export (all rows matching the active filters). baseParams may
// override the page_size/sort defaults. Returns the mapped rows plus the
// server's total for that filter set.
export async function fetchAllJobs(baseParams) {
  const base = { page_size: 100, sort_by: "posted_date", sort_order: "desc", ...baseParams };
  const first = await getJobs({ ...base, page: 1 });
  const totalPages = first.total_pages || 1;
  let rows = first.jobs || [];
  if (totalPages > 1) {
    const rest = [];
    for (let page = 2; page <= totalPages; page++) rest.push(getJobs({ ...base, page }));
    for (const res of await Promise.all(rest)) rows = rows.concat(res.jobs || []);
  }
  return { rows: rows.map(mapApiJob), total: first.total || 0 };
}
