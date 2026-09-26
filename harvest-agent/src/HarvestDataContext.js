import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { getJobs, getRunHistory, getActiveRun, ApiError } from "./api";
import { mapRun } from "./lib/jobsData";

/* ── Shared app-data context ──────────────────────────────────────────────────
 * Holds the genuinely app-level state that used to live in the root component:
 * the harvested-jobs and run-history datasets (which also feed the Sidebar
 * badges and the live-run poll) and the cross-page harvestRunning mutex. Pages
 * read it via useHarvestData() instead of receiving it all as props. */
const HarvestDataContext = createContext(null);

export function useHarvestData() {
  const ctx = useContext(HarvestDataContext);
  if (!ctx) throw new Error("useHarvestData must be used within HarvestAgent");
  return ctx;
}

/* Provider — owns the shared datasets/effects and provides them via context.
 * Extracted from the HarvestAgent root component; the state, fetches, and
 * polling behaviour are unchanged. */
export function HarvestDataProvider({ children }) {
  // Shared across Rule Engine + Source Runs so two pages can't both launch a
  // harvest at once — Playwright can't open two browsers on the same
  // persistent Chrome profile, and doing so fails the whole run.
  const [harvestRunning, setHarvestRunning] = useState(false);

  const [jobsTotal, setJobsTotal] = useState(0);
  const [pageSizeSel, setPageSizeSel] = useState("100");

  // Persisted across navigation (this provider never unmounts, unlike the pages
  // under it) so returning from a full-page detail view restores the list
  // instantly instead of refetching + resetting filters. jobsViewRef caches the
  // last Harvested-Jobs page — the mapped rows/total + facets and the URL search
  // they were fetched for; runHistoryViewRef keeps the Run History filter/sort UI
  // state (its data already persists in `runs` above).
  const jobsViewRef = useRef(null);
  const runHistoryViewRef = useRef(null);

  const [runs, setRuns] = useState([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [runsError, setRunsError] = useState("");

  // The jobs dataset itself is no longer loaded here — the Jobs page fetches
  // its own server-side pages (see JobsPage). This only reads the grand total
  // for the Sidebar badge and the Rule Engine header count.
  const fetchJobs = useCallback(async () => {
    try {
      const res = await getJobs({ page: 1, page_size: 1 });
      setJobsTotal(res.total || 0);
    } catch {
      setJobsTotal(0); // badge only — the Jobs page surfaces backend errors
    }
  }, []);

  const fetchRuns = useCallback(async () => {
    setRunsLoading(true);
    setRunsError("");
    try {
      const res = await getRunHistory();
      setRuns((res.runs || []).map(mapRun));
    } catch (err) {
      setRunsError(
        err instanceof ApiError
          ? `Could not load run history: ${err.message}`
          : "Could not reach the harvest backend. Is it running on the configured API URL?"
      );
      setRuns([]);
    } finally {
      setRunsLoading(false);
    }
  }, []);

  const refreshAll = useCallback(() => { fetchJobs(); fetchRuns(); }, [fetchJobs, fetchRuns]);

  // Freeze the Run controls if a harvest is already running — including one
  // started in another tab or before this page loaded. Event-driven (mount +
  // window focus), so no always-on polling loop.
  const syncActiveRun = useCallback(async () => {
    try {
      const res = await getActiveRun();
      setHarvestRunning(Boolean(res?.active));
    } catch {
      // Backend unreachable — leave the flag as-is; HealthBadge surfaces the outage.
    }
  }, []);

  useEffect(() => { fetchJobs(); }, [fetchJobs]);
  useEffect(() => { fetchRuns(); }, [fetchRuns]);
  useEffect(() => {
    syncActiveRun();
    const onFocus = () => syncActiveRun();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [syncActiveRun]);

  // While any run is in progress, re-fetch the run list every 6s so the Run
  // History page reflects the running row's jobs_found (= live combined_count)
  // climbing. Polling stops automatically once no run is "running".
  const anyRunning = runs.some((r) => r.status === "running");
  useEffect(() => {
    if (!anyRunning) return;
    const id = setInterval(() => { fetchRuns(); }, 6000);
    return () => clearInterval(id);
  }, [anyRunning, fetchRuns]);

  const ctx = useMemo(() => ({
    jobsTotal, fetchJobs,
    runs, runsLoading, runsError, fetchRuns,
    refreshAll, harvestRunning, setHarvestRunning,
    pageSizeSel, setPageSizeSel,
    jobsViewRef, runHistoryViewRef,   // stable refs — a persistent view cache
  }), [
    jobsTotal, fetchJobs,
    runs, runsLoading, runsError, fetchRuns,
    refreshAll, harvestRunning, pageSizeSel,
  ]);

  return (
    <HarvestDataContext.Provider value={ctx}>
      {children}
    </HarvestDataContext.Provider>
  );
}
