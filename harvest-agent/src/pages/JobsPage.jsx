import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { SlidersHorizontal, Download, RefreshCw, FileJson, FileSpreadsheet } from "lucide-react";
import { getJobs, getJobsFacets, ApiError, downloadJsonUrl, downloadExcelUrl } from "../api";
import { C } from "../theme";
import { Select, StatCard } from "../components/ui";
import JobsTable, { PAGE_SIZE_OPTIONS } from "../components/JobsTable";
import { mapApiJob, fetchAllJobs } from "../lib/jobsData";
import { useHarvestData } from "../HarvestDataContext";
import { useTenant } from "../TenantContext";

/* ── Harvested Jobs page ─────────────────────────────────────────────── */
// Server-side pagination: only the current page of rows is ever loaded. The
// JobsTable (in serverMode) emits its filter/sort/page state via
// handleParamsChange, which fetches exactly that slice from GET /jobs; dropdown
// options and the header/footer stat counts come from GET /jobs/facets so they
// still span the whole dataset.
export default function JobsPage({ onNavigate, onView, pageSizeSel, onPageSizeChange, urlState = false }) {
  const { jobsViewRef } = useHarvestData();
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
    try {
      const res = await getJobs(params);
      if (seq !== seqRef.current) return; // superseded by a newer request
      const mapped = (res.jobs || []).map(mapApiJob);
      setPageJobs(mapped);
      setPageTotal(res.total || 0);
      setPageLoading(false);
      // Cache this exact view so returning to it is instant. Keyed by the URL
      // search (filters/page/sort) + the date range that produced these rows.
      jobsViewRef.current = {
        ...(jobsViewRef.current || {}),
        search: window.location.search,
        jobs: mapped,
        total: res.total || 0,
        dateFrom: params.date_from || "",
        dateTo: params.date_to || "",
      };
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
      const f = await getJobsFacets();
      setFacets(f);
      // Facets are whole-dataset (filter-independent), so cache them globally.
      jobsViewRef.current = { ...(jobsViewRef.current || {}), facets: f };
    } catch {
      // Dropdowns fall back to "All"-only and stats render as 0; the jobs
      // fetch's own error banner covers a backend outage.
    }
  }, [jobsViewRef]);
  useEffect(() => { fetchFacets(); }, [fetchFacets]);

  const refresh = useCallback(() => {
    fetchFacets();
    if (lastParamsRef.current) handleParamsChange(lastParamsRef.current);
  }, [fetchFacets, handleParamsChange]);

  // Date range is a server-side filter now (date_from/date_to) — JobsTable
  // folds these into every emitted params object.
  const serverExtraParams = useMemo(
    () => ({ date_from: startDate, date_to: endDate }),
    [startDate, endDate]
  );

  const stats = facets?.stats || {};

  // Export the FULL filtered dataset, not just the loaded page: re-run the
  // current filter params without pagination through fetchAllJobs (which pulls
  // every matching page) and build the CSV from all rows.
  async function exportCsv() {
    if (exporting) return;
    setExporting(true);
    try {
      const { page, page_size, ...filterParams } = lastParamsRef.current || {};
      const { rows } = await fetchAllJobs(filterParams);
      const header = ["Job title", "Company", "Source", "Filter status", "Filter reason", "POC", "Posted date", "Email", "Mobile", "WhatsApp", "LinkedIn", "Job description"];
      const lines = rows.map((j) =>
        [j.title, j.company, j.source, j.passedFilter ? "Qualified" : "Flagged", j.filterReason || "—", j.poc || "—", j.postedDate || "—", j.email || "—", j.mobile || "—", j.whatsapp || "—", j.linkedin || "—", j.jobDescription || "—"]
          .map((c) => '"' + String(c).replace(/"/g, '""') + '"').join(","));
      // Prepend a UTF-8 BOM so Excel detects the encoding — without it Excel reads
      // the file as Windows-1252 and turns the "—" placeholder into "â€"".
      const blob = new Blob([String.fromCharCode(0xFEFF) + [header.join(","), ...lines].join("\n")], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = "harvested-jobs.csv"; a.click();
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
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", justifyContent: "space-between", gap: 16, padding: "24px 24px 0" }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, color: C.text }}>Harvested Jobs</h1>
          <p style={{ margin: "4px 0 0", fontSize: 14, color: C.textSoft }}>
            {pageLoading && !facets ? "Loading…" : `${stats.total || 0} harvested posting${(stats.total || 0) === 1 ? "" : "s"} · ${stats.qualified || 0} qualified · ${stats.flagged || 0} flagged`}
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button className="ha-btn ha-btn-secondary" onClick={refresh} disabled={pageLoading} title="Refresh">
            <RefreshCw size={16} className={pageLoading ? "ha-spin" : ""} /> Refresh
          </button>
          {hasFeature("rules") && (
            <button className="ha-btn ha-btn-secondary" onClick={() => onNavigate("rules")}><SlidersHorizontal size={16} /> Rule Engine</button>
          )}
          <a className="ha-btn ha-btn-secondary" href={downloadJsonUrl()} title="GET /download/json — latest combined harvest JSON">
            <FileJson size={16} /> JSON
          </a>
          <a className="ha-btn ha-btn-secondary" href={downloadExcelUrl()} title="GET /download/excel — latest combined harvest Excel">
            <FileSpreadsheet size={16} /> Excel
          </a>
          <button className="ha-btn ha-btn-primary" onClick={exportCsv} disabled={exporting}>
            <Download size={16} /> {exporting ? "Exporting…" : "Export CSV"}
          </button>
        </div>
      </div>
      <div style={{ marginTop: 20, borderBottom: "1px solid " + C.border }} />

      <div style={{ display: "flex", flexDirection: "column", gap: 16, padding: 24 }}>
        {pageError && <div className="ha-errbanner">{pageError}</div>}

        <div className="ha-daterow">
          <span style={{ fontSize: 14, fontWeight: 600, color: C.text }}>Posted between</span>
          <input type="date" className="ha-input" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          <span style={{ color: C.textSoft }}>to</span>
          <input type="date" className="ha-input" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          {(startDate || endDate) && (
            <button className="ha-btn ha-btn-secondary" style={{ height: 38, boxSizing: "border-box", padding: "0 14px" }} onClick={() => { setStartDate(""); setEndDate(""); }}>Clear dates</button>
          )}
          <div style={{ marginLeft: "auto" }}>
            <Select label="Rows per page" value={pageSizeSel} onChange={onPageSizeChange} options={PAGE_SIZE_OPTIONS} />
          </div>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 16 }}>
          <StatCard value={stats.total || 0} label="Total harvested" color={C.accent} />
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
          emptyMessage="No postings match your search. Run a harvest from the Rule Engine to collect jobs."
        />

        <div style={{ display: "flex", justifyContent: "space-between", padding: "0 4px", fontSize: 12, color: C.textSoft }}>
          <span>Showing {pageJobs.length} of {pageTotal} matching posting{pageTotal === 1 ? "" : "s"} · {stats.qualified || 0} qualified · {stats.flagged || 0} flagged</span>
          <span>{stats.with_email || 0} email · {stats.with_phone || 0} WhatsApp · {stats.with_linkedin || 0} LinkedIn</span>
        </div>
      </div>
    </main>
  );
}
