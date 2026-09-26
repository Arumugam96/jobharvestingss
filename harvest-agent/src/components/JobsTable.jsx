import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Search, Mail } from "lucide-react";
import EmailComposeModal from "./EmailComposeModal";
import LinkedInMessageModal from "./LinkedInMessageModal";
import { getOutreachStatus } from "../api";
import { C } from "../theme";
import {
  WhatsAppIcon, LinkedInIcon, SourceChip, SortHeader, PlainHeader,
  Select, MultiSelect, ContactActionBtn, DualContact, CompanySizeCell,
} from "./ui";
import { mapJobToDetail } from "../lib/jobsData";

/* Run detail page */
// Contact filter options — grouped for a professional dropdown. The option
// VALUES and the OR-combined predicate (jobMatchesContact) are unchanged; only
// the presentation is grouped. `summaryLabel` keeps the collapsed chip
// unambiguous ("Has email" vs "No email") while the open list stays short
// under its group header.
// Company-size filter options. Named tiers map to the LinkedIn employee bands
// captured per job (backend app/core/company_size.py); "unknown" catches jobs
// with no detected band (and non-LinkedIn sources). Display-only — filters which
// harvested rows are shown, never drops data.
const COMPANY_SIZE_FILTER_OPTIONS = [
  { value: "all", label: "All sizes" },
  { value: "Small", label: "Small · 2–200" },
  { value: "Medium", label: "Medium · 201–1,000" },
  { value: "Large", label: "Large · 1,001–10,000" },
  { value: "Enterprise", label: "Enterprise · 10,001+" },
  { value: "unknown", label: "Unknown" },
];

const CONTACT_FILTER_OPTIONS = [
  { group: "With contact" },
  { value: "email",    label: "Email",    summaryLabel: "Has email" },
  { value: "mobile",   label: "Phone",    summaryLabel: "Has phone" },
  { value: "linkedin", label: "LinkedIn", summaryLabel: "Has LinkedIn" },
  { group: "Without contact" },
  { value: "no_email",    label: "Email",    summaryLabel: "No email" },
  { value: "no_mobile",   label: "Phone",    summaryLabel: "No phone" },
  { value: "no_linkedin", label: "LinkedIn", summaryLabel: "No LinkedIn" },
  { value: "none", label: "None on file", summaryLabel: "No contact" },
];

// Positive tokens match rows that HAVE that channel; "no_*" tokens match rows
// MISSING it; "none" matches rows with no contact at all. Tokens are OR-combined
// (a row passes if it satisfies any selected one). Empty selection = no filter.
function jobMatchesContact(j, contact) {
  if (!contact || contact.length === 0) return true;
  const has = { email: !!j.email, mobile: !!j.mobile, linkedin: !!j.linkedin };
  const none = !has.email && !has.mobile && !has.linkedin;
  return contact.some((c) =>
    c === "email" ? has.email
      : c === "mobile" ? has.mobile
      : c === "linkedin" ? has.linkedin
      : c === "no_email" ? !has.email
      : c === "no_mobile" ? !has.mobile
      : c === "no_linkedin" ? !has.linkedin
      : c === "none" ? none
      : false);
}

/**
 * Shared jobs table used by both the Harvested Jobs page (JobsPage) and the
 * run-history per-run view (RunDetailView). Owns the Company/Contact/Job/POC/
 * search filter bar, sorting, and the WhatsApp/Email/LinkedIn contact-action
 * icons (with the LLM Email + LinkedIn composer modals). `preFilter` lets the
 * parent add its own predicate (date range on the Harvested Jobs page; classification
 * bucket in the run view). The filtered+sorted rows are reported back via
 * `onFilteredChange` so the parent can drive its own stats/export/footer.
 */
// Options for the "Rows per page" selector on the Harvested Jobs and per-run
// views. On the Harvested Jobs page this is the server page size (each page is
// fetched on demand); on the per-run view it's the client-side display page
// size over the run's fully-loaded rows.
export const PAGE_SIZE_OPTIONS = [
  { value: "50", label: "50" },
  { value: "100", label: "100" },
  { value: "200", label: "200" },
  { value: "500", label: "500" },
];

// Fallback display page size when no explicit "Rows per page" is provided.
const DEFAULT_DISPLAY_PAGE_SIZE = 100;

export default function JobsTable({
  jobs,
  onView,
  preFilter = null,
  loading = false,
  emptyMessage = "No jobs match your filters.",
  minWidth = 1180,
  onFilteredChange = null,
  pageSize = DEFAULT_DISPLAY_PAGE_SIZE,
  urlState = false,
  // Server mode (the Harvested Jobs page): `jobs` is just the current server
  // page — filtering/sorting/pagination happen in the backend. The table emits
  // its filter/sort/page state via onParamsChange (as GET /jobs query params)
  // instead of filtering in-memory, dropdown options come from `facets`
  // (GET /jobs/facets, whole-dataset distincts), and the footer counts against
  // `serverTotal`. Client mode (RunDetailView) is unchanged.
  serverMode = false,
  facets = null,
  serverTotal = 0,
  onParamsChange = null,
  serverExtraParams = null,
}) {
  // When `urlState`, the filter/search/sort/page state is mirrored to the URL
  // query string (so /jobs?company=…&page=2 is shareable and survives a refresh).
  // Initialised from the URL on mount; written back on change. Non-urlState
  // instances (e.g. the per-run table) keep purely-local state, unchanged.
  const [searchParams, setSearchParams] = useSearchParams();
  const [filters, setFilters] = useState(() =>
    urlState
      ? {
          company: searchParams.get("company") || "all",
          contact: (searchParams.get("contact") || "").split(",").filter(Boolean),
          job: searchParams.get("job") || "all",
          poc: searchParams.get("poc") || "all",
          size: searchParams.get("size") || "all",
          country: searchParams.get("country") || "all",
          companyCountry: searchParams.get("companyCountry") || "all",
        }
      : { company: "all", contact: [], job: "all", poc: "all", size: "all", country: "all", companyCountry: "all" }
  );
  const [query, setQuery] = useState(() => (urlState ? searchParams.get("q") || "" : ""));
  const [sort, setSort] = useState(() =>
    urlState
      ? { col: searchParams.get("sort") || "posted", dir: searchParams.get("dir") || "desc" }
      : { col: "posted", dir: "desc" }
  );
  const [page, setPage] = useState(() =>
    urlState ? Math.max(1, parseInt(searchParams.get("page") || "1", 10) || 1) : 1
  );

  // Outreach modals + the per-row "no data on this channel" inline message.
  // emailModal carries follow-up context: { job, followup, parentOutreachId }.
  const [emailModal, setEmailModal] = useState(null);
  const [linkedinModalJob, setLinkedinModalJob] = useState(null);
  const [noDataMsg, setNoDataMsg] = useState(null); // { jobId, channel }
  // DB-backed "already contacted" state per job id: { [jobId]: { email?, linkedin? } }.
  // Fetched for the visible page rows so a sent recruiter shows a green icon that
  // survives refresh; clicking a sent email icon opens the follow-up composer.
  const [outreachStatus, setOutreachStatus] = useState({});
  const noDataTimer = useRef(null);
  const showNoData = useCallback((jobId, channel) => {
    setNoDataMsg({ jobId, channel });
    if (noDataTimer.current) clearTimeout(noDataTimer.current);
    noDataTimer.current = setTimeout(() => setNoDataMsg(null), 3000);
  }, []);
  useEffect(() => () => { if (noDataTimer.current) clearTimeout(noDataTimer.current); }, []);

  // Server mode gets its dropdown options from GET /jobs/facets (they must span
  // the whole dataset, not just the loaded page); client mode derives them from
  // the fully-loaded rows as before.
  const companies = useMemo(
    () => (serverMode ? (facets?.companies || []) : Array.from(new Set(jobs.map((j) => j.company))).sort()),
    [serverMode, facets, jobs]
  );
  const jobTitles = useMemo(
    () => (serverMode ? (facets?.job_titles || []) : Array.from(new Set(jobs.map((j) => j.title))).sort()),
    [serverMode, facets, jobs]
  );
  const pocNames = useMemo(
    () => (serverMode ? (facets?.poc_names || []) : Array.from(new Set(jobs.filter((j) => j.poc).map((j) => j.poc))).sort()),
    [serverMode, facets, jobs]
  );
  // Job-location country and company-HQ country dropdown options — whole-dataset
  // facets in server mode, distinct-from-loaded-rows in client mode (Run Detail).
  const countries = useMemo(
    () => (serverMode ? (facets?.countries || []) : Array.from(new Set(jobs.map((j) => j.country).filter(Boolean))).sort()),
    [serverMode, facets, jobs]
  );
  const companyCountries = useMemo(
    () => (serverMode ? (facets?.company_countries || []) : Array.from(new Set(jobs.map((j) => j.companyCountry).filter(Boolean))).sort()),
    [serverMode, facets, jobs]
  );

  const filtered = useMemo(() => {
    // Server mode: rows arrive already filtered/sorted/paginated by the backend.
    if (serverMode) return jobs;
    const rows = jobs.filter((j) => {
      if (filters.company !== "all" && j.company !== filters.company) return false;
      if (filters.job !== "all" && j.title !== filters.job) return false;
      if (filters.poc !== "all" && j.poc !== filters.poc) return false;
      if (filters.country !== "all" && j.country !== filters.country) return false;
      if (filters.companyCountry !== "all" && j.companyCountry !== filters.companyCountry) return false;
      // Company-size is a display-only filter over the captured band's tier.
      // "unknown" matches rows with no detected size; a named tier matches exactly.
      if (filters.size && filters.size !== "all") {
        const tier = j.companySizeTier || "";
        if (filters.size === "unknown" ? tier !== "" : tier !== filters.size) return false;
      }
      if (!jobMatchesContact(j, filters.contact)) return false;
      if (preFilter && !preFilter(j)) return false;
      if (query.trim()) {
        const q = query.toLowerCase();
        if (!((j.title + " " + j.company + " " + j.source + " " + (j.poc || "") + " " + (j.email || "") + " " + (j.mobile || "")).toLowerCase().includes(q))) return false;
      }
      return true;
    });
    const dir = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      switch (sort.col) {
        case "title": return a.title.localeCompare(b.title) * dir;
        case "company": return a.company.localeCompare(b.company) * dir;
        case "poc": return (a.poc || "").localeCompare(b.poc || "") * dir;
        case "source": return a.source.localeCompare(b.source) * dir;
        case "posted": return a.postedDate.localeCompare(b.postedDate) * dir;
        default: return 0;
      }
    });
  }, [serverMode, jobs, filters, query, sort, preFilter]);

  useEffect(() => { if (onFilteredChange) onFilteredChange(filtered); }, [filtered, onFilteredChange]);

  // Reset to the first display page whenever the filtered result changes shape
  // (new load, filter, search, sort, or a page-size change) so the user is
  // never stranded on an out-of-range page. Client mode only — in server mode
  // `jobs` changes after every fetch, so this would bounce the page back to 1
  // (and re-fetch) each time; the param-emission effect below owns the reset.
  useEffect(() => {
    if (!serverMode) setPage(1);
  }, [serverMode, filters, query, sort, preFilter, jobs, pageSize]);

  // Debounced free-text search — the input stays immediate, but server fetches
  // wait 350 ms after the last keystroke. Harmless (unused) in client mode.
  const [debouncedQuery, setDebouncedQuery] = useState(query);
  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query), 350);
    return () => clearTimeout(t);
  }, [query]);

  // Server mode: emit the current filter/sort/page state as GET /jobs params.
  // On a filter/sort/size change while past page 1, reset to page 1 FIRST and
  // let the effect re-run — so each state change produces exactly one fetch.
  const serverParamsSig = JSON.stringify({
    filters, q: debouncedQuery.trim(), sort, pageSize, extra: serverExtraParams || null,
  });
  const sigRef = useRef(serverParamsSig);
  useEffect(() => {
    if (!serverMode || !onParamsChange) return;
    if (sigRef.current !== serverParamsSig) {
      sigRef.current = serverParamsSig;
      if (page !== 1) { setPage(1); return; }
    }
    const sortByMap = { title: "job_title", company: "company", poc: "job_poster_name", source: "source", posted: "posted_date" };
    onParamsChange({
      page,
      page_size: pageSize,
      sort_by: sortByMap[sort.col] || "posted_date",
      sort_order: sort.dir === "asc" ? "asc" : "desc",
      keyword: debouncedQuery.trim(),
      company_exact: filters.company !== "all" ? filters.company : "",
      job_title: filters.job !== "all" ? filters.job : "",
      poc: filters.poc !== "all" ? filters.poc : "",
      size_tier: filters.size && filters.size !== "all" ? filters.size : "",
      country: filters.country !== "all" ? filters.country : "",
      company_country: filters.companyCountry !== "all" ? filters.companyCountry : "",
      contact: filters.contact.join(","),
      ...(serverExtraParams || {}),
    });
    // The emitted params are fully captured by serverParamsSig + page; listing
    // the individual pieces here would only duplicate the signature.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverMode, onParamsChange, serverParamsSig, page]);

  // Server mode: if a deep-linked page is out of range for the (possibly
  // filtered) result set, the backend returns an empty page — snap back to 1.
  useEffect(() => {
    if (serverMode && !loading && jobs.length === 0 && serverTotal > 0 && page > 1) setPage(1);
  }, [serverMode, loading, jobs, serverTotal, page]);

  // Mirror the current filter/search/sort/page to the URL query string (only for
  // the URL-synced instance). Uses replace so it doesn't spam the history stack.
  useEffect(() => {
    if (!urlState) return;
    const sp = new URLSearchParams();
    if (filters.company !== "all") sp.set("company", filters.company);
    if (filters.contact.length) sp.set("contact", filters.contact.join(","));
    if (filters.job !== "all") sp.set("job", filters.job);
    if (filters.poc !== "all") sp.set("poc", filters.poc);
    if (filters.size && filters.size !== "all") sp.set("size", filters.size);
    if (filters.country !== "all") sp.set("country", filters.country);
    if (filters.companyCountry !== "all") sp.set("companyCountry", filters.companyCountry);
    if (query.trim()) sp.set("q", query.trim());
    if (sort.col !== "posted" || sort.dir !== "desc") { sp.set("sort", sort.col); sp.set("dir", sort.dir); }
    if (page > 1) sp.set("page", String(page));
    setSearchParams(sp, { replace: true });
  }, [urlState, filters, query, sort, page, setSearchParams]);

  // Server mode paginates against the backend's filtered total — `jobs` already
  // is the current page. Client mode slices the filtered in-memory rows.
  const effectiveTotal = serverMode ? serverTotal : filtered.length;
  const totalPages = Math.max(1, Math.ceil(effectiveTotal / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pageRows = serverMode ? filtered : filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  // Fetch outreach status for just the visible rows (keeps the query small vs.
  // the whole dataset). Merged into prior results so paged-away rows stay marked.
  const pageRowIdsKey = pageRows.map((j) => j.id).join(",");
  const refreshOutreachStatus = useCallback(async () => {
    const ids = pageRowIdsKey ? pageRowIdsKey.split(",") : [];
    if (!ids.length) return;
    try {
      const map = await getOutreachStatus(ids);
      setOutreachStatus((prev) => ({ ...prev, ...(map || {}) }));
    } catch {
      // best-effort — icons fall back to the default (not-sent) state
    }
  }, [pageRowIdsKey]);
  useEffect(() => { refreshOutreachStatus(); }, [refreshOutreachStatus]);

  // Columns: Job title, Company, Company size, POC, Email,
  // Mobile, Contact,Source, Posted date
  const colCount = 9;

  return (
    <>
      <div className="ha-card ha-filterbar" style={{ padding: "16px 20px", gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
        <Select label="Company" value={filters.company} onChange={(v) => setFilters((f) => ({ ...f, company: v }))}
          options={[{ value: "all", label: "All" }, ...companies.map((c) => ({ value: c, label: c }))]} />
        <Select label="Company size" value={filters.size || "all"} onChange={(v) => setFilters((f) => ({ ...f, size: v }))}
          options={COMPANY_SIZE_FILTER_OPTIONS} />
        <Select label="Company country" variant="location" value={filters.companyCountry} onChange={(v) => setFilters((f) => ({ ...f, companyCountry: v }))}
          options={[{ value: "all", label: "All" }, ...companyCountries.map((c) => ({ value: c, label: c }))]} />
        <Select label="Job country" variant="location" value={filters.country} onChange={(v) => setFilters((f) => ({ ...f, country: v }))}
          options={[{ value: "all", label: "All" }, ...countries.map((c) => ({ value: c, label: c }))]} />
        <Select label="Job" value={filters.job} onChange={(v) => setFilters((f) => ({ ...f, job: v }))}
          options={[{ value: "all", label: "All" }, ...jobTitles.map((t) => ({ value: t, label: t }))]} />
        <MultiSelect label="Contact" selected={filters.contact} onChange={(v) => setFilters((f) => ({ ...f, contact: v }))}
          options={CONTACT_FILTER_OPTIONS} />
        <Select label="POC" value={filters.poc} onChange={(v) => setFilters((f) => ({ ...f, poc: v }))}
          options={[{ value: "all", label: "All" }, ...pocNames.map((p) => ({ value: p, label: p }))]} />
        <div className="ha-filter-search">
          <Search size={16} />
          <input className="ha-input" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search…" />
        </div>
      </div>

      <div className="ha-card" style={{ overflow: "hidden", marginTop: 14 }}>
        <div className="ha-table-scroll">
          <table className="ha-table" style={{ minWidth }}>
            <thead className="ha-thead">
              <tr>
                <SortHeader label="Job title" col="title" sort={sort} setSort={setSort} width={230} />
                <SortHeader label="Company" col="company" sort={sort} setSort={setSort} width={170} />
                <PlainHeader label="Company size" width={160} />
                <SortHeader label="POC" col="poc" sort={sort} setSort={setSort} width={150} />
                <PlainHeader label="Email" width={200} />
                <PlainHeader label="Mobile" width={140} />
                <PlainHeader label="Contact" align="center" width={130} />
                <SortHeader label="Source" col="source" sort={sort} setSort={setSort} width={100} />
                <SortHeader label="Posted date" col="posted" sort={sort} setSort={setSort} width={130} />
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr><td className="ha-td" colSpan={colCount} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                  Loading harvested jobs…
                </td></tr>
              )}
              {!loading && pageRows.map((j) => (
                <tr key={j.id} className="ha-row">
                  <td className="ha-td">
                    <span className="ha-link" role="button" tabIndex={0} style={{ cursor: "pointer" }}
                      title="View details"
                      onClick={() => onView({ mode: "view", job: mapJobToDetail(j) })}
                      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onView({ mode: "view", job: mapJobToDetail(j) }); } }}>
                      {j.title}
                    </span>
                  </td>
                  <td className="ha-td" style={{ color: C.text }}>{j.company}</td>
                  <td className="ha-td">
                    <CompanySizeCell band={j.companySize} tier={j.companySizeTier} />
                  </td>
                  <td className="ha-td" style={{ color: C.text }}>
                    {j.poc || <span style={{ color: "#94A3B8" }}>—</span>}
                  </td>
                  <td className="ha-td">
                    <DualContact scraped={j.emailScraped} recruiter={j.emailRecruiter} fallback={j.email} link="mailto:" cls="ha-mail" />
                  </td>
                  <td className="ha-td" style={{ whiteSpace: "nowrap" }}>
                    <DualContact scraped={j.mobileScraped} recruiter={j.mobileRecruiter} fallback={j.mobile} link="tel:" cls="ha-tel" />
                  </td>
                  <td className="ha-td">
                    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 4 }}>
                      <div style={{ display: "flex", gap: 8, justifyContent: "center" }}>
                        <ContactActionBtn glyph={WhatsAppIcon} title="WhatsApp" available={!!j.whatsapp}
                          href={j.whatsapp ? "https://wa.me/" + j.whatsapp.replace(/[^0-9]/g, "") : null}
                          onClick={() => showNoData(j.id, "WhatsApp")} />
                        {(() => {
                          const st = outreachStatus[j.id] || {};
                          const emailSent = !!st.email;
                          const liSent = !!st.linkedin;
                          const emailSentTitle = emailSent
                            ? `Email sent${st.email.followup_count ? ` (+${st.email.followup_count} follow-up${st.email.followup_count > 1 ? "s" : ""})` : ""} — click to follow up`
                            : undefined;
                          return (
                            <>
                              <ContactActionBtn glyph={Mail} title="Email" available={!!j.email}
                                sent={emailSent} sentTitle={emailSentTitle}
                                onClick={() =>
                                  emailSent
                                    ? setEmailModal({ job: j, followup: true, parentOutreachId: st.email.outreach_id })
                                    : j.email
                                      ? setEmailModal({ job: j, followup: false })
                                      : showNoData(j.id, "email")
                                } />
                              <ContactActionBtn glyph={LinkedInIcon} title="LinkedIn" available={!!j.linkedin}
                                sent={liSent} sentTitle={liSent ? "LinkedIn message sent" : undefined}
                                onClick={() => (j.linkedin ? setLinkedinModalJob(j) : showNoData(j.id, "LinkedIn"))} />
                            </>
                          );
                        })()}
                      </div>
                      {noDataMsg && noDataMsg.jobId === j.id && (
                        <small style={{ color: "#B91C1C", fontSize: 11, whiteSpace: "nowrap" }}>
                          No {noDataMsg.channel} available
                        </small>
                      )}
                    </div>
                  </td>
                  <td className="ha-td"><SourceChip source={j.source} /></td>
                  <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>{j.postedDate || "—"}</td>
                </tr>
              ))}
              {!loading && filtered.length === 0 && (
                <tr><td className="ha-td" colSpan={colCount} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                  {emptyMessage}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {!loading && effectiveTotal > pageSize && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12, marginTop: 12, padding: "0 4px", fontSize: 13, color: C.textSoft }}>
          <span>
            Showing {(currentPage - 1) * pageSize + 1}–{Math.min(currentPage * pageSize, effectiveTotal)} of {effectiveTotal}
          </span>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <button className="ha-btn ha-btn-secondary" disabled={currentPage <= 1} onClick={() => setPage(Math.max(1, currentPage - 1))}>Prev</button>
            <span>Page {currentPage} of {totalPages}</span>
            <button className="ha-btn ha-btn-secondary" disabled={currentPage >= totalPages} onClick={() => setPage(Math.min(totalPages, currentPage + 1))}>Next</button>
          </div>
        </div>
      )}

      {emailModal && (
        <EmailComposeModal
          job={emailModal.job}
          followup={emailModal.followup}
          parentOutreachId={emailModal.parentOutreachId}
          onClose={() => setEmailModal(null)}
          onSent={refreshOutreachStatus}
        />
      )}
      {linkedinModalJob && (
        <LinkedInMessageModal
          job={linkedinModalJob}
          onClose={() => setLinkedinModalJob(null)}
          onLogged={refreshOutreachStatus}
        />
      )}
    </>
  );
}
