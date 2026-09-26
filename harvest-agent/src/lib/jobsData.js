import { getJobs } from "../api";

/* ── API job → table row shape ─────────────────────────────────────────── */
export function mapApiJob(j) {
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
    mobileScraped: j.phone_scraped || null,
    mobileRecruiter: j.phone_recruiter || null,
    linkedin: j.linkedin_profile_url || null,
    source: j.source || "—",
    jobDescription: j.job_description || "",
    jobDescriptionHtml: j.job_description_html || "",
    location: j.location || "",
    salary: j.salary || "",
    jobType: j.job_type || "",
    workMode: j.work_mode || "",
    applyLink: j.job_url || "",
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
      mobileScraped: j.mobileScraped || "",
      mobileRecruiter: j.mobileRecruiter || "",
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
