import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { Download, ChevronDown, Wrench, ArrowLeft } from "lucide-react";
import { getJobs, getJobsFacets, ApiError, downloadJsonUrl, downloadExcelUrl } from "../api";
import { C } from "../theme";
import { Select, StatCard } from "../components/ui";
import JobsTable, { PAGE_SIZE_OPTIONS } from "../components/JobsTable";
import { mapApiJob, fetchAllJobs, jobContact, pocKindLabel, partSourceLabel } from "../lib/jobsData";
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
  const [pageJobs, setPageJobs] = useState(rowsSeed?.jobs || []);
  const [pageTotal, setPageTotal] = useState(rowsSeed?.total || 0);
  const [pageLoading, setPageLoading] = useState(!rowsSeed);
  const [pageError, setPageError] = useState("");
  const [facets, setFacets] = useState(cache?.facets || null);
  const [exporting, setExporting] = useState(false);
  const [exportFormat, setExportFormat] = useState("csv");
  const [formatOpen, setFormatOpen] = useState(false);
  const formatRef = useRef(null);
  // Monotonic request counter — a response only lands if no newer request has
  // started since (fast typing / rapid filter clicks can overlap fetches).
  const seqRef = useRef(0);
  const lastParamsRef = useRef(null);
  // On a cache-seeded remount, keep the cached rows visible during the first
  // (revalidating) fetch instead of flashing the loading state.
  const seededRef = useRef(!!rowsSeed);
  const firstFetchRef = useRef(false);

  const handleParamsChange = useCallback(async (params) => {
    lastParamsRef.current = params;
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
  }, [jobsViewRef]);

  const fetchFacets = useCallback(async () => {
    try {
      const f = await getJobsFacets(demand ? { source: DEMAND_SOURCE } : {});
      setFacets(f);
      // Facets are whole-dataset (filter-independent), so cache them globally.
      jobsViewRef.current = { ...(jobsViewRef.current || {}), facets: f };
    } catch {
      // Dropdowns fall back to "All"-only and stats render as 0; the jobs
      // fetch's own error banner covers a backend outage.
    }
  }, [jobsViewRef, demand]);
  useEffect(() => { fetchFacets(); }, [fetchFacets]);

  const refresh = useCallback(() => {
    fetchFacets();
    if (lastParamsRef.current) handleParamsChange(lastParamsRef.current);
  }, [fetchFacets, handleParamsChange]);

  // Date range is a server-side filter now (date_from/date_to) — JobsTable
  // folds these into every emitted params object.
  const serverExtraParams = useMemo(
    () => ({
      date_from: startDate, date_to: endDate,
      ...(demand ? { source: DEMAND_SOURCE } : {}),
      ...(library ? { assigned_to_me: true } : {}),
    }),
    [startDate, endDate, demand, library]
  );

  const stats = facets?.stats || {};

  // Close format dropdown on outside click.
  useEffect(() => {
    if (!formatOpen) return;
    const close = (e) => { if (formatRef.current && !formatRef.current.contains(e.target)) setFormatOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [formatOpen]);

  function doExport() {
    if (exportFormat === "json") { window.location.href = downloadJsonUrl(); return; }
    if (exportFormat === "excel") { window.location.href = downloadExcelUrl(); return; }
    exportCsv();
  }

  // Export the FULL filtered dataset, not just the loaded page: re-run the
  // current filter params without pagination through fetchAllJobs (which pulls
  // every matching page) and build the CSV from all rows.
  async function exportCsv() {
    if (exporting) return;
    setExporting(true);
    try {
      const { page, page_size, ...filterParams } = lastParamsRef.current || {};
      const { rows } = await fetchAllJobs(filterParams);
      // Contact columns come from jobContact() — the same values the table cells
      // show, incl. the Company-tagged contact, for every source.
      const header = ["Job title", "Company", "Source", "Filter status", "Filter reason", "POC", "POC type", "Posted date", "Email", "Email source", "Mobile", "Mobile source", "WhatsApp", "LinkedIn", "Job description"];
      const lines = rows.map((j) => {
        const c = jobContact(j);
        return [
          j.title, j.company, c.source, j.passedFilter ? "Qualified" : "Flagged", j.filterReason || "—",
          c.pocName || "—", pocKindLabel(c.pocKind), j.postedDate || "—",
          c.email || "—", partSourceLabel(c.emailParts), c.phone || "—", partSourceLabel(c.phoneParts),
          c.phone || "—", c.linkedin || "—", j.jobDescription || "—",
        ].map((v) => '"' + String(v).replace(/"/g, '""') + '"').join(",");
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
              ? `${stats.total || 0} assigned job${(stats.total || 0) === 1 ? "" : "s"} awaiting outreach`
              : demand
                ? `${stats.total || 0} Demand Search job${(stats.total || 0) === 1 ? "" : "s"} from uploaded and manually added requirements`
                : `${stats.total || 0} harvested posting${(stats.total || 0) === 1 ? "" : "s"} · ${stats.qualified || 0} qualified · ${stats.flagged || 0} flagged`}
          </p>
        </div>
        <div className="ha-pagehead-actions">
          {!library && (
            <div ref={formatRef} style={{ position: "relative", display: "inline-flex", borderRadius: 8, border: `1px solid ${C.primary}` }}>
              <button
                className="ha-btn ha-btn-primary"
                style={{ border: "none", borderRadius: "7px 0 0 7px", borderRight: "1px solid rgba(255,255,255,.25)" }}
                onClick={doExport}
                disabled={exporting}
              >
                <Download size={16} /> {exporting ? "Exporting…" : "Export"}
              </button>
              <button
                style={{
                  display: "inline-flex", alignItems: "center", gap: 4,
                  background: C.primary, color: "#fff", border: "none",
                  borderRadius: "0 7px 7px 0",
                  padding: "7px 12px", fontSize: 13, fontWeight: 500,
                  cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap",
                }}
                onClick={() => setFormatOpen((v) => !v)}
                aria-expanded={formatOpen}
              >
                {exportFormat === "csv" ? "CSV" : exportFormat === "excel" ? "Excel" : "JSON"}
                <ChevronDown size={14} style={{ transition: "transform .2s", transform: formatOpen ? "rotate(180deg)" : "none" }} />
              </button>
              {formatOpen && (
                <div style={{
                  position: "absolute", top: "100%", right: 0, marginTop: 4,
                  minWidth: 140, background: "#fff", border: `1px solid ${C.border}`,
                  borderRadius: 8, boxShadow: "0 8px 24px -6px rgba(15,23,42,.18)",
                  zIndex: 10, overflow: "hidden",
                }}>
                  {["csv", "excel", "json"].map((fmt) => (
                    <button
                      key={fmt}
                      onClick={() => { setExportFormat(fmt); setFormatOpen(false); }}
                      style={{
                        display: "flex", alignItems: "center", gap: 8, width: "100%",
                        padding: "9px 14px", border: "none", background: exportFormat === fmt ? `color-mix(in srgb, ${C.primary} 8%, transparent)` : "transparent",
                        color: C.text, fontSize: 13, fontWeight: 500, cursor: "pointer",
                        fontFamily: "inherit", textAlign: "left",
                      }}
                    >
                      {fmt === "csv" ? "CSV" : fmt === "excel" ? "Excel" : "JSON"}
                    </button>
                  ))}
                </div>
              )}
            </div>
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
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          <StatCard value={stats.total || 0} label={demand ? "Total Demand jobs" : "Total harvested"} color={C.accent} />
          <StatCard value={stats.companies || 0} label="Companies sourced" color={C.primary} />
          <StatCard value={stats.pocs || 0} label="POCs identified" color={C.primary} />
        </div>

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
          <span>Showing {pageJobs.length} of {pageTotal} matching posting{pageTotal === 1 ? "" : "s"} · {stats.qualified || 0} qualified · {stats.flagged || 0} flagged</span>
          {/* with_email/with_phone include the Company-tagged contact (GET /jobs/facets). */}
          <span title={`Email: ${stats.with_poster_email || 0} job/recruiter · ${stats.with_company_email || 0} company contact`}>
            {stats.with_email || 0} email · {stats.with_phone || 0} WhatsApp · {stats.with_linkedin || 0} LinkedIn
          </span>
        </div>
      </div>
    </main>
  );
}
