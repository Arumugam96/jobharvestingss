import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { Wrench, ArrowLeft, XCircle } from "lucide-react";
import { getJobs, getJobsFacets, ApiError, downloadJsonUrl, downloadExcelUrl } from "../api";
import { C } from "../theme";
import { Select /* , StatCard */ } from "../components/ui";
import ExportButton from "../components/ExportButton";
import JobsTable, { PAGE_SIZE_OPTIONS } from "../components/JobsTable";
import { mapApiJob, fetchAllJobs, jobContact, pocKindLabel, partSourceLabel } from "../lib/jobsData";
import { csvCell } from "../lib/csv";
import { useHarvestData } from "../HarvestDataContext";
import { useTenant } from "../TenantContext";

/* ── Harvested Jobs page ─────────────────────────────────────────────── */
// Server-side pagination: only the current page of rows is ever loaded. The
// JobsTable (in serverMode) emits its filter/sort/page state via
// handleParamsChange, which fetches exactly that slice from GET /jobs; dropdown
// options and the header/footer stat counts come from GET /jobs/facets so they
// still span the whole dataset.
//
// `demand` reuses this page for Demand Search jobs (/demand?view=jobs): rows and
// facets are fetched with source=Demand, the Source filter is pinned to Demand,
// the harvest-only header actions are hidden and `demandTabs` (the Demand Search
// New search | All jobs tabs) render at the top. It keeps its own view cache,
// apart from Harvested Jobs'.
const DEMAND_SOURCE = "Demand";

// Classification stat cards. Each bucket maps to the GET /jobs server filter it
// applies; counts come from GET /jobs/facets stats. (Run Detail additionally shows
// a "Verified contacts" card — career-page verification_status — which is
// deliberately not offered here: the label read as "verified emails/phones".)
const BUCKETS = {
  direct:    { label: "Direct clients",    color: C.primary,   params: { hiring_entity: "Direct Client" } },
  gcc:       { label: "GCC companies",     color: C.secondary, params: { hiring_entity: "GCC" } },
  staffing:  { label: "Staffing firms",    color: "#7C3AED",   params: { hiring_entity: "Staffing Firm" } },
  ambiguous: { label: "Needs review",      color: "#D97706",   params: { hiring_entity: "Ambiguous" } },
};

/**
 * Harvested Jobs page (also reused as My Workspace and Demand Jobs): date range, stat cards and
 * a server-paginated JobsTable fed by GET /jobs, with whole-dataset dropdowns/stats from
 * GET /jobs/facets, plus CSV / Excel / JSON export. Views are cached in HarvestDataContext so
 * returning to the same URL renders instantly and revalidates quietly.
 * @param {object} props
 * @param {Function} props.onNavigate - navigate by page key ("jobs", "library").
 * @param {Function} props.onView - open a job's detail view (passed to JobsTable).
 * @param {string|number} props.pageSizeSel - rows per page; onPageSizeChange sets it.
 * @param {boolean} [props.urlState] - let JobsTable sync its filters to the URL.
 * @param {boolean} [props.demand] - Demand Jobs mode: source pinned to "Demand", own cache.
 * @param {React.ReactNode} [props.demandTabs] - Demand Search tabs rendered in demand mode.
 * @param {boolean} [props.library] - My Workspace mode: only jobs assigned to the current user.
 */
export default function JobsPage({ onNavigate, onView, pageSizeSel, onPageSizeChange, urlState = false, demand = false, demandTabs = null, library = false }) {
  const { jobsViewRef: sharedViewRef } = useHarvestData();
  const demandViewRef = useRef(null);
  const jobsViewRef = demand ? demandViewRef : sharedViewRef;
  // Tenants without the rules feature (e.g. the India client) shouldn't see the
  // Rule Engine shortcut — FeatureGate would bounce the navigation anyway.
  const { hasFeature } = useTenant();
  const location = useLocation();
  // Seed from the persistent cache when we're returning to the SAME view (its URL
  // search matches what the cached rows were fetched for) — so the list renders
  // instantly and revalidates quietly, instead of blanking to a spinner. Dates
  // live in JobsPage state (not the URL), so they ride along in the cache too.
  const cache = jobsViewRef.current;
  const rowsSeed = cache && cache.search === location.search ? cache : null;

  const [startDate, setStartDate] = useState(rowsSeed?.dateFrom || "");
  const [endDate, setEndDate] = useState(rowsSeed?.dateTo || "");
  // Active classification card ("all" | a BUCKETS key). Like the dates it lives in
  // page state (not the URL), so it rides along in the view cache.
  const [bucket, setBucket] = useState(rowsSeed?.bucket || "all");
  const [pageJobs, setPageJobs] = useState(rowsSeed?.jobs || []);
  const [pageTotal, setPageTotal] = useState(rowsSeed?.total || 0);
  const [pageLoading, setPageLoading] = useState(!rowsSeed);
  const [pageError, setPageError] = useState("");
  const [facets, setFacets] = useState(cache?.facets || null);
  // Stat counts for the CURRENT filters (GET /jobs/facets with the filter params,
  // minus the active bucket's own params so every bucket card keeps its count).
  // null = use the unfiltered facets' stats (no filters, or not fetched yet).
  const [filteredStats, setFilteredStats] = useState(rowsSeed?.filteredStats?.data ?? null);
  const [filtersActive, setFiltersActive] = useState(!!rowsSeed?.filteredStats?.active);
  const [exporting, setExporting] = useState(false);
  const [exportFormat, setExportFormat] = useState("csv");
  // Monotonic request counter — a response only lands if no newer request has
  // started since (fast typing / rapid filter clicks can overlap fetches).
  const seqRef = useRef(0);
  const lastParamsRef = useRef(null);
  // On a cache-seeded remount, keep the cached rows visible during the first
  // (revalidating) fetch instead of flashing the loading state.
  const seededRef = useRef(!!rowsSeed);
  const firstFetchRef = useRef(false);
  // Stats fetch bookkeeping: own seq counter (stale-response guard), the
  // signature of the params last requested (a pure page/size/sort change keeps
  // it, so no refetch) and the pending debounce timer.
  const statsSeqRef = useRef(0);
  const statsSigRef = useRef(null);
  const statsTimerRef = useRef(null);
  useEffect(() => () => clearTimeout(statsTimerRef.current), []);

  // The facets params that carry no user filter for this page mode — the
  // unfiltered dropdown fetch uses `facetBase`; `statsBase` is what the stats
  // params reduce to when the user hasn't filtered anything.
  const facetBase = useMemo(() => (demand ? { source: DEMAND_SOURCE } : {}), [demand]);
  const statsBase = useMemo(
    () => ({ ...facetBase, ...(library ? { assigned_to_me: true } : {}) }),
    [facetBase, library]
  );

  /** Fetch filter-scoped stats for the params JobsTable emitted (debounced, seq-guarded).
   * Drops pagination/sort and the active bucket's own params; skips the request when
   * nothing changed since the last one (page flips) unless `force`. */
  const updateStats = useCallback((params, force = false) => {
    const { page, page_size, sort_by, sort_order, ...rest } = params || {};
    const bucketKey = Object.keys(BUCKETS).find((k) =>
      Object.entries(BUCKETS[k].params).every(([pk, pv]) => rest[pk] === pv));
    if (bucketKey) Object.keys(BUCKETS[bucketKey].params).forEach((pk) => { delete rest[pk]; });
    // Normalise: drop empty values and sort keys so the signature is stable.
    const clean = {};
    Object.keys(rest).sort().forEach((k) => {
      const v = rest[k];
      if (v !== undefined && v !== null && v !== "" && v !== false) clean[k] = v;
    });
    const sig = JSON.stringify(clean);
    if (!force && sig === statsSigRef.current) return;
    statsSigRef.current = sig;
    const active = sig !== JSON.stringify(Object.fromEntries(Object.entries(statsBase).sort()));
    setFiltersActive(active);
    clearTimeout(statsTimerRef.current);
    const seq = ++statsSeqRef.current;
    // Same params as the unfiltered facets fetch → its stats already cover it.
    if (sig === JSON.stringify(Object.fromEntries(Object.entries(facetBase).sort()))) {
      setFilteredStats(null);
      jobsViewRef.current = { ...(jobsViewRef.current || {}), filteredStats: { sig, data: null, active } };
      return;
    }
    statsTimerRef.current = setTimeout(async () => {
      try {
        const f = await getJobsFacets(clean);
        if (seq !== statsSeqRef.current) return; // superseded by a newer filter set
        const data = f?.stats || null;
        setFilteredStats(data);
        jobsViewRef.current = { ...(jobsViewRef.current || {}), filteredStats: { sig, data, active } };
      } catch {
        // Keep the previous counts; the jobs fetch's own banner covers an outage.
        if (seq === statsSeqRef.current) statsSigRef.current = null; // retry next change
      }
    }, 300);
  }, [jobsViewRef, facetBase, statsBase]);

  const handleParamsChange = useCallback(async (params) => {
    lastParamsRef.current = params;
    updateStats(params);
    const seq = ++seqRef.current;
    const quiet = !firstFetchRef.current && seededRef.current;
    firstFetchRef.current = true;
    setPageLoading(!quiet);
    setPageError("");
    // The URL search these params belong to. JobsTable syncs the URL in an
    // effect that runs right after the one calling us, so read it a microtask
    // later (once that effect flush is done) — still well before the response.
    let requestSearch = null;
    Promise.resolve().then(() => { requestSearch = window.location.search; });
    try {
      const res = await getJobs(params);
      if (seq !== seqRef.current) return; // superseded by a newer request
      const mapped = (res.jobs || []).map(mapApiJob);
      setPageJobs(mapped);
      setPageTotal(res.total || 0);
      setPageLoading(false);
      // Cache this exact view so returning to it is instant. Keyed by the URL
      // search (filters/page/sort) + the date range that produced these rows —
      // skipped if the URL moved on meanwhile (debounced keyword typed further,
      // page reset pending, navigated away), so rows never land under another
      // view's key.
      if (requestSearch === window.location.search) {
        jobsViewRef.current = {
          ...(jobsViewRef.current || {}),
          search: requestSearch,
          jobs: mapped,
          total: res.total || 0,
          dateFrom: params.date_from || "",
          dateTo: params.date_to || "",
          bucket: Object.keys(BUCKETS).find((k) =>
            Object.entries(BUCKETS[k].params).every(([pk, pv]) => params[pk] === pv)) || "all",
        };
      }
    } catch (err) {
      if (seq !== seqRef.current) return;
      setPageError(
        err instanceof ApiError
          ? `Could not load jobs: ${err.message}`
          : "Could not reach the harvest backend. Is it running on the configured API URL?"
      );
      setPageJobs([]);
      setPageTotal(0);
      setPageLoading(false);
    }
  }, [jobsViewRef, updateStats]);

  const fetchFacets = useCallback(async () => {
    try {
      const f = await getJobsFacets(facetBase);
      setFacets(f);
      // Unfiltered facets (dropdown options + whole-dataset stats) are
      // filter-independent, so cache them globally.
      jobsViewRef.current = { ...(jobsViewRef.current || {}), facets: f };
    } catch {
      // Dropdowns fall back to "All"-only and stats render as 0; the jobs
      // fetch's own error banner covers a backend outage.
    }
  }, [jobsViewRef, facetBase]);
  useEffect(() => { fetchFacets(); }, [fetchFacets]);

  // eslint-disable-next-line no-unused-vars
  const refresh = useCallback(() => {
    fetchFacets();
    if (lastParamsRef.current) {
      updateStats(lastParamsRef.current, true);
      handleParamsChange(lastParamsRef.current);
    }
  }, [fetchFacets, handleParamsChange, updateStats]);

  // Date range is a server-side filter now (date_from/date_to) — JobsTable
  // folds these into every emitted params object.
  const serverExtraParams = useMemo(
    () => ({
      date_from: startDate, date_to: endDate,
      ...(demand ? { source: DEMAND_SOURCE } : {}),
      ...(library ? { assigned_to_me: true } : {}),
      ...(BUCKETS[bucket]?.params || {}),
    }),
    [startDate, endDate, demand, library, bucket]
  );

  // Filter-scoped stats when filters are active (falls back to the unfiltered
  // facets' stats — also what an older backend that ignores the params returns).
  const stats = filteredStats || facets?.stats || {};
  // Header count: the exact GET /jobs total (incl. an active bucket card) once
  // filters are applied; the stats total otherwise.
  const isFiltered = filtersActive || bucket !== "all";
  const headTotal = isFiltered ? pageTotal : (stats.total || 0);
  const plural = (n) => (n === 1 ? "" : "s");

  /** Export in the selected format: JSON / Excel navigate to the backend download URLs; CSV is
   * built client-side by exportCsv(). */
  function doExport() {
    if (exportFormat === "json") { window.location.href = downloadJsonUrl(); return; }
    if (exportFormat === "excel") { window.location.href = downloadExcelUrl(); return; }
    exportCsv();
  }

  // Export the FULL filtered dataset, not just the loaded page: re-run the
  // current filter params without pagination through fetchAllJobs (which pulls
  // every matching page) and build the CSV from all rows.
  /** Fetch every row matching the current filters and download them as a CSV file
   * (cells escaped by csvCell, so formula-like values are neutralised). */
  async function exportCsv() {
    if (exporting) return;
    setExporting(true);
    try {
      const { page, page_size, ...filterParams } = lastParamsRef.current || {};
      const { rows } = await fetchAllJobs(filterParams);
      // Contact columns come from jobContact() — the same values the table cells
      // show, incl. the Company-tagged contact, for every source.
      const header = ["Job title", "Company", "Source", "Filter status", "Filter reason", "POC", "POC type", "Posted date", "Email", "Email source", "Mobile", "Mobile source", "Contact", "LinkedIn", "Job description"];
      const lines = rows.map((j) => {
        const c = jobContact(j);
        return [
          j.title, j.company, c.source, j.passedFilter ? "Qualified" : "Flagged", j.filterReason || "—",
          c.pocName || "—", pocKindLabel(c.pocKind), j.postedDate || "—",
          c.email || "—", partSourceLabel(c.emailParts), c.phone || "—", partSourceLabel(c.phoneParts),
          c.phone || "—", c.linkedin || "—", j.jobDescription || "—",
        ].map(csvCell).join(","); // csvCell neutralises =, +, -, @ formula injection
      });
      // Prepend a UTF-8 BOM so Excel detects the encoding — without it Excel reads
      // the file as Windows-1252 and turns the "—" placeholder into "â€"".
      const blob = new Blob([String.fromCharCode(0xFEFF) + [header.join(","), ...lines].join("\n")], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = demand ? "demand-jobs.csv" : "harvested-jobs.csv"; a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setPageError(
        err instanceof ApiError
          ? `Could not export jobs: ${err.message}`
          : "Could not reach the harvest backend to export. Is it running on the configured API URL?"
      );
    } finally {
      setExporting(false);
    }
  }

  return (
    <main className="ha-main">
      <div className="ha-pagehead">
        <div className="ha-pagehead-titles">
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {library && (
              <button
                className="ha-btn ha-btn-secondary"
                style={{ display: "inline-flex", alignItems: "center", gap: 6, height: 34, padding: "0 14px" }}
                onClick={() => onNavigate("jobs")}
              >
                <ArrowLeft size={15} /> Back
              </button>
            )}
            <h1>{library ? "My Workspace" : demand ? "Demand Jobs" : "Harvested Jobs"}</h1>
          </div>
          <p className="ha-sub">
            {pageLoading && !facets ? "Loading…" : library
              ? isFiltered
                ? `${headTotal} matching assigned job${plural(headTotal)}`
                : `${headTotal} assigned job${plural(headTotal)} awaiting outreach`
              : demand
                ? isFiltered
                  ? `${headTotal} matching Demand Search job${plural(headTotal)}`
                  : `${headTotal} Demand Search job${plural(headTotal)} from uploaded and manually added requirements`
                : isFiltered
                  ? `${headTotal} matching posting${plural(headTotal)}`
                  : `${headTotal} harvested posting${plural(headTotal)} · ${stats.qualified || 0} qualified · ${stats.flagged || 0} flagged`}
          </p>
        </div>
        <div className="ha-pagehead-actions">
          {!library && !demand && (
            <button
              className="ha-btn ha-btn-secondary"
              style={{
                display: "inline-flex", alignItems: "center", gap: 6,
                height: 34, padding: "0 14px", borderRadius: 999,
                fontSize: 13, fontWeight: 600, color: C.primary,
                border: `1px solid ${C.primary}33`, background: `${C.primary}0A`,
                cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap",
              }}
              onClick={() => onNavigate("library")}
            >
              <Wrench size={14} /> My Workspace
            </button>
          )}
          {!library && (
            <ExportButton format={exportFormat} onFormatChange={setExportFormat} onExport={doExport} exporting={exporting} />
          )}
        </div>
      </div>

      <div className="ha-pagebody">
        {demand && demandTabs}
        {pageError && <div className="ha-errbanner">{pageError}</div>}

        <div className="ha-daterow">
          <span style={{ fontSize: 13, fontWeight: 600, color: C.text }}>Posted between</span>
          <input type="date" className="ha-input" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          <span style={{ color: C.textSoft }}>to</span>
          <input type="date" className="ha-input" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          {(startDate || endDate) && (
            <button className="ha-btn ha-btn-secondary" style={{ height: 34, boxSizing: "border-box", padding: "0 14px" }} onClick={() => { setStartDate(""); setEndDate(""); }}>Clear dates</button>
          )}
          <div style={{ marginLeft: "auto" }}>
            <Select label="Rows per page" value={pageSizeSel} onChange={onPageSizeChange} options={PAGE_SIZE_OPTIONS} />
          </div>
        </div>
        {/* Previous stat cards — replaced by the clickable classification cards below.
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          <StatCard value={stats.total || 0} label={demand ? "Total Demand jobs" : "Total harvested"} color={C.accent} />
          <StatCard value={stats.companies || 0} label="Companies sourced" color={C.primary} />
          <StatCard value={stats.pocs || 0} label="POCs identified" color={C.primary} />
        </div>
        */}
        {/* Classification cards: clicking one filters the jobs to that bucket
            server-side (hiring_entity); clicking it again — or the chip — clears it.
            Counts follow the user's filters (GET /jobs/facets with the filter params;
            each bucket's own filter is left out so every card keeps its count). */}
        <style>{`
          .ha-runstats.ha-jobstats{grid-template-columns:repeat(5,minmax(0,1fr));}
          @media(max-width:900px){.ha-runstats.ha-jobstats{grid-template-columns:repeat(3,minmax(0,1fr));}}
          @media(max-width:520px){.ha-runstats.ha-jobstats{grid-template-columns:repeat(2,minmax(0,1fr));}}
        `}</style>
        <div className="ha-runstats ha-jobstats">
          {[
            { key: "all", value: stats.total || 0, label: demand ? "Total Demand jobs" : "Jobs found", color: C.accent },
            ...Object.entries(BUCKETS).map(([key, b]) => ({ key, value: stats[key] || 0, label: b.label, color: b.color })),
          ].map((card) => {
            const active = bucket === card.key;
            return (
              <button
                key={card.key}
                type="button"
                className={"ha-runstat ha-runstat-btn" + (active ? " ha-runstat-active" : "")}
                aria-pressed={active}
                title={card.key === "all" ? "Show all jobs" : `Filter jobs to ${card.label}`}
                onClick={() => setBucket((b) => (b === card.key ? "all" : card.key))}
                style={active ? { boxShadow: `inset 0 0 0 2px ${card.color}` } : undefined}
              >
                <b style={{ color: card.color }}>{card.value}</b><span>{card.label}</span>
              </button>
            );
          })}
        </div>
        {/* Contact / filter-status counts for the same filtered rows (informational). */}
        <div className="ha-runstats ha-jobstats">
          {[
            { key: "qualified", value: stats.qualified || 0, label: "Qualified", color: "#059669",
              title: "Jobs that passed the business filter" },
            { key: "flagged", value: stats.flagged || 0, label: "Flagged", color: "#DC2626",
              title: "Jobs flagged by the business filter" },
            { key: "emails", value: stats.unique_emails ?? stats.with_email ?? 0, label: "Emails", color: C.primary,
              title: `Distinct email addresses on these jobs. Jobs with email: ${stats.with_email || 0} (${stats.with_poster_email || 0} job/recruiter · ${stats.with_company_email || 0} company contact)` },
            { key: "contacts", value: stats.phone_contacts ?? stats.unique_phones ?? stats.with_phone ?? 0, label: "Contacts", color: C.secondary,
              title: `Contacts = phone numbers on these jobs, counted like Phone logs → Total contacts. Jobs with: ${stats.with_email || 0} email (${stats.with_poster_email || 0} job/recruiter · ${stats.with_company_email || 0} company contact) · ${stats.with_phone || 0} phone · ${stats.with_linkedin || 0} LinkedIn` },
            { key: "linkedin", value: stats.unique_linkedin ?? stats.with_linkedin ?? 0, label: "LinkedIn", color: "#0A66C2",
              title: `Distinct LinkedIn profiles on these jobs. Jobs with LinkedIn: ${stats.with_linkedin || 0}` },
          ].map((card) => (
            <div key={card.key} className="ha-runstat" title={card.title}>
              <b style={{ color: card.color }}>{card.value}</b><span>{card.label}</span>
            </div>
          ))}
        </div>
        {bucket !== "all" && (
          <div>
            <button className="ha-pill" onClick={() => setBucket("all")} title="Clear card filter"
              style={{ background: C.pale, color: C.secondary, border: "1px solid " + C.border, display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
              {BUCKETS[bucket].label} <XCircle size={12} />
            </button>
          </div>
        )}

        <JobsTable
          jobs={pageJobs}
          onView={onView}
          loading={pageLoading}
          serverMode
          serverTotal={pageTotal}
          facets={facets}
          onParamsChange={handleParamsChange}
          serverExtraParams={serverExtraParams}
          minWidth={1410}
          pageSize={Number(pageSizeSel)}
          urlState={urlState}
          lockedSource={demand ? DEMAND_SOURCE : ""}
          emptyMessage={library
            ? "Your workspace is empty. Self-assign jobs from the Harvested Jobs page to add them here."
            : demand
              ? "No Demand jobs match your search. Upload or add requirements from New search."
              : "No postings match your search. Run a harvest from the Rule Engine to collect jobs."}
        />

        <div style={{ display: "flex", justifyContent: "space-between", padding: "0 4px", fontSize: 12, color: C.textSoft }}>
          <span>Showing {pageJobs.length} of {pageTotal} matching posting{plural(pageTotal)}</span>
        </div>
      </div>
    </main>
  );
}
