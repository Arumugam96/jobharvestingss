import { useEffect, useMemo, useState } from "react";
import { SlidersHorizontal, Download, Search, RefreshCw } from "lucide-react";
import { C } from "../theme";
import {
  SourceChip, StatusPill, AnimatedNumber, SortHeader, PlainHeader, Select, MultiSelect, fmtDate,
} from "../components/ui";
import { getRunHistoryContactMatch, ApiError } from "../api";
import { CONTACT_FILTER_OPTIONS } from "../lib/contactFilter";
import { fmtSeconds } from "../lib/jobsData";
import { isPlainLeftClick } from "../components/JobsTable";
import { useHarvestData } from "../HarvestDataContext";
import { useTenant } from "../TenantContext";

/* ── Run History page ────────────────────────────────────────────────── */
export default function RunHistoryPage({ runs, runsTotal, runsAggStats, loading, error, onRefresh, onNavigate, onView }) {
  // Tenants without the rules feature (e.g. the India client) shouldn't see the
  // Rule Engine shortcut — FeatureGate would bounce the navigation anyway.
  const { hasFeature } = useTenant();
  // Seed the filter/sort UI from the persistent cache so returning from a run's
  // detail view keeps the view (the run data itself already lives in context).
  const { runHistoryViewRef } = useHarvestData();
  const saved = runHistoryViewRef.current;
  const [filters, setFilters] = useState(saved?.filters || { source: "all", status: "all" });
  const [startDate, setStartDate] = useState(saved?.startDate || "");
  const [endDate, setEndDate] = useState(saved?.endDate || "");
  const [query, setQuery] = useState(saved?.query || "");
  const [sort, setSort] = useState(saved?.sort || { col: "started", dir: "desc" });
  // Contact filter: same tokens/semantics as the jobs tables (incl. "Resdex
  // contacts"). A run passes when ≥1 of its jobs matches ANY selected token —
  // resolved server-side (GET /run-history/contact-match) since the run list
  // carries no per-job contact data.
  const [contact, setContact] = useState(saved?.contact || []);
  useEffect(() => {
    runHistoryViewRef.current = { filters, startDate, endDate, query, sort, contact };
  }, [runHistoryViewRef, filters, startDate, endDate, query, sort, contact]);

  // The run list lives in context and is otherwise loaded once at app start (plus
  // the live poll while a run is running and the Rule Engine's post-run refresh),
  // so a scheduled run or a LinkedIn Feed run started since would be missing from
  // the cards and table. Revalidate on every visit; current rows stay on screen.
  useEffect(() => {
    onRefresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Matching display run_ids for the current selection (null = no contact filter).
  const [contactMatch, setContactMatch] = useState(null);
  const [contactLoading, setContactLoading] = useState(false);
  const [contactError, setContactError] = useState("");
  const contactKey = contact.join(",");
  useEffect(() => {
    setContactError("");
    if (!contactKey) { setContactMatch(null); setContactLoading(false); return undefined; }
    let cancelled = false;
    setContactLoading(true);
    getRunHistoryContactMatch(contactKey)
      .then((res) => { if (!cancelled) setContactMatch(new Set(res?.run_ids || [])); })
      .catch((err) => {
        if (cancelled) return;
        setContactMatch(new Set());
        setContactError(err instanceof ApiError ? err.message : "Could not apply the contact filter.");
      })
      .finally(() => { if (!cancelled) setContactLoading(false); });
    return () => { cancelled = true; };
    // runs.length: re-resolve when a refresh brings in new runs.
  }, [contactKey, runs.length]);

  const filtered = useMemo(() => {
    let rows = runs.filter((r) => {
      if (filters.source !== "all" && !r.sources.some((s) => s.toLowerCase() === filters.source)) return false;
      if (filters.status !== "all" && r.status !== filters.status) return false;
      if (startDate && r.startedAt.slice(0, 10) < startDate) return false;
      if (endDate && r.startedAt.slice(0, 10) > endDate) return false;
      if (query.trim() && !r.runId.toLowerCase().includes(query.trim().toLowerCase())) return false;
      if (contactMatch && !contactMatch.has(r.runId)) return false;
      return true;
    });
    const dir = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      switch (sort.col) {
        case "runId": return a.runId.localeCompare(b.runId) * dir;
        case "status": return a.status.localeCompare(b.status) * dir;
        case "started": return a.startedAt.localeCompare(b.startedAt) * dir;
        case "completed": return a.completedAt.localeCompare(b.completedAt) * dir;
        case "duration": return ((a.durationS ?? -1) - (b.durationS ?? -1)) * dir;
        case "jobsFound": return (a.jobsFound - b.jobsFound) * dir;
        default: return 0;
      }
    });
  }, [runs, filters, startDate, endDate, query, sort, contactMatch]);

  // Stat cards follow the filters. With any filter active they're computed from
  // the filtered runs; otherwise Total runs / Jobs harvested / Direct clients use
  // the server's uncapped aggregates. GET /run-history may return only the most
  // recent runs, so per-run sums over a partial list are marked "of loaded runs".
  const filterActive = filters.source !== "all" || filters.status !== "all" || !!startDate || !!endDate
    || !!query.trim() || contact.length > 0;
  const partial = runs.length < (runsTotal ?? runs.length);
  const counts = useMemo(() => {
    const src = filterActive ? filtered : runs;
    const sum = (key) => src.reduce((acc, r) => acc + (r[key] || 0), 0);
    const byStatus = (st) => src.filter((r) => r.status === st).length;
    return {
      totalRuns: filterActive ? src.length : (runsTotal ?? runs.length),
      totalJobs: filterActive ? sum("jobsFound") : (runsAggStats?.totalJobs ?? sum("jobsFound")),
      directClients: filterActive ? sum("directClients") : (runsAggStats?.totalDirectClients ?? sum("directClients")),
      gcc: sum("gcc"),
      staffingFirms: sum("staffingFirms"),
      ambiguous: sum("ambiguous"),
      success: byStatus("success"),
      failed: byStatus("failed"),
    };
  }, [filterActive, filtered, runs, runsTotal, runsAggStats]);
  // Tooltip for counts that only cover the loaded runs (filtered, or per-run sums
  // the server doesn't aggregate) when the loaded list is shorter than the total.
  const loadedNote = partial ? `Counted over the ${runs.length} loaded runs (of ${runsTotal} total)` : undefined;
  const aggNote = filterActive ? loadedNote : undefined;

  /** Download the filtered, sorted runs as run-history.csv (UTF-8 BOM for Excel). */
  function exportCsv() {
    const header = ["Run ID", "Sources", "Status", "Started", "Completed", "Duration (s)", "Jobs found", "Verified", "Direct clients", "GCC", "Staffing firms", "Ambiguous"];
    const lines = filtered.map((r) =>
      [r.runId, r.sources.join("|"), r.status, r.startedAt, r.completedAt, r.durationS ?? "", r.jobsFound, r.verifiedJobs, r.directClients, r.gcc, r.staffingFirms, r.ambiguous]
        .map((c) => '"' + String(c).replace(/"/g, '""') + '"').join(","));
    // Prepend a UTF-8 BOM so Excel detects the encoding — without it Excel reads
    // the file as Windows-1252 and turns the "—" placeholder into "â€"".
    const blob = new Blob([String.fromCharCode(0xFEFF) + [header.join(","), ...lines].join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "run-history.csv"; a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <main className="ha-main">
      <div className="ha-pagehead">
        <div className="ha-pagehead-titles">
          <h1>Run History</h1>
          <p className="ha-sub">
            {loading ? "Loading…" : `${counts.totalRuns}${filterActive ? " matching" : ""} run${counts.totalRuns === 1 ? "" : "s"} · ${counts.totalJobs} jobs harvested${filterActive && partial ? " (of loaded runs)" : ""}`}
          </p>
        </div>
        <div className="ha-pagehead-actions">
          <button className="ha-btn ha-btn-secondary" onClick={onRefresh} disabled={loading} title="Refresh">
            <RefreshCw size={16} className={loading ? "ha-spin" : ""} /> Refresh
          </button>
          {hasFeature("rules") && (
            <button className="ha-btn ha-btn-secondary" onClick={() => onNavigate("rules")}><SlidersHorizontal size={16} /> Rule Engine</button>
          )}
          <button className="ha-btn ha-btn-primary" onClick={exportCsv}><Download size={16} /> Export CSV</button>
        </div>
      </div>

      <div className="ha-pagebody">
        {error && <div className="ha-errbanner">{error}</div>}
        {contactError && <div className="ha-errbanner">{contactError}</div>}

        <div className="ha-daterow">
          <span style={{ fontSize: 13, fontWeight: 600, color: C.text }}>Started between</span>
          <input type="date" className="ha-input" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          <span style={{ color: C.textSoft }}>to</span>
          <input type="date" className="ha-input" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          {(startDate || endDate) && (
            <button className="ha-btn ha-btn-secondary" style={{ height: 34, boxSizing: "border-box", padding: "0 14px" }} onClick={() => { setStartDate(""); setEndDate(""); }}>Clear dates</button>
          )}
        </div>
        {/* Same compact tile + one-row grid as My Workspace / Run Detail (.ha-runstats). */}
        <div className="ha-runstats">
          {[
            { value: counts.totalRuns, label: filterActive ? "Matching runs" : "Total runs", color: C.accent, title: aggNote },
            { value: counts.totalJobs, label: "Jobs harvested", color: C.primary, title: aggNote },
            { value: counts.directClients, label: "Direct clients", color: C.primary, title: aggNote },
            { value: counts.gcc, label: "GCC companies", color: C.secondary, title: loadedNote },
            { value: counts.staffingFirms, label: "Staffing firms", color: "#7C3AED", title: loadedNote },
            { value: counts.ambiguous, label: "Needs review", color: "#D97706", title: loadedNote },
            { value: counts.success, label: "Successful runs", color: "#059669", title: loadedNote },
            { value: counts.failed, label: "Failed runs", color: "#DC2626", title: loadedNote },
          ].map((card) => (
            <div key={card.label} className="ha-runstat" title={card.title || card.label}>
              <b style={{ color: card.color }}>{card.value}</b><span>{card.label}</span>
            </div>
          ))}
        </div>

        <div className="ha-card ha-filterbar" style={{ padding: "11px 14px", gridTemplateColumns: "repeat(3, minmax(0, 1fr))" }}>
          <Select label="Source" value={filters.source} onChange={(v) => setFilters((f) => ({ ...f, source: v }))}
            options={[{ value: "all", label: "All" }, { value: "linkedin", label: "LinkedIn" }, { value: "linkedin feed", label: "LinkedIn Feed" }, { value: "naukri", label: "Naukri" }, { value: "dice", label: "Dice" }]} />
          <Select label="Status" value={filters.status} onChange={(v) => setFilters((f) => ({ ...f, status: v }))}
            options={[{ value: "all", label: "All" }, { value: "success", label: "Success" }, { value: "no_results", label: "No results" }, { value: "failed", label: "Failed" }, { value: "running", label: "Running" }, { value: "stopped", label: "Stopped" }]} />
          <MultiSelect label="Contact" selected={contact} onChange={setContact} options={CONTACT_FILTER_OPTIONS} />
          <div className="ha-filter-search">
            <Search size={16} />
            <input className="ha-input" value={query}
              onChange={(e) => setQuery(e.target.value)} placeholder="Search run ID…" />
          </div>
        </div>

        <div className="ha-card" style={{ overflow: "hidden" }}>
          <div className="ha-table-scroll">
            <table className="ha-table" style={{ minWidth: 1200 }}>
              <thead className="ha-thead">
                <tr>
                  <SortHeader label="Run ID" col="runId" sort={sort} setSort={setSort} width={190} />
                  <PlainHeader label="Sources" width={170} />
                  <SortHeader label="Status" col="status" sort={sort} setSort={setSort} width={120} />
                  <SortHeader label="Started" col="started" sort={sort} setSort={setSort} width={150} />
                  <SortHeader label="Completed" col="completed" sort={sort} setSort={setSort} width={150} />
                  <SortHeader label="Duration" col="duration" sort={sort} setSort={setSort} width={110} />
                  <SortHeader label="Jobs found" col="jobsFound" sort={sort} setSort={setSort} width={100} />
                  <PlainHeader label="Breakdown" width={220} />
                </tr>
              </thead>
              <tbody>
                {/* Only the FIRST load blanks the table: a refetch (the 6s live poll
                    while a run is running, or Refresh) keeps the current rows, so the
                    list never collapses and loses its scroll position. */}
                {((loading && !runs.length) || contactLoading) && (
                  <tr><td className="ha-td" colSpan={8} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                    {loading && !runs.length ? "Loading run history…" : "Filtering runs by contact…"}
                  </td></tr>
                )}
                {!(loading && !runs.length) && !contactLoading && filtered.map((r) => {
                  const isFeed = r.runType === "feed";
                  return (
                  <tr key={r.runId} className="ha-row">
                    <td className="ha-td">
                      {/* Real link: modifier/middle-click opens the run in a new tab
                          (nginx serves /history/* as the SPA); a plain click stays in-app. */}
                      <a className="ha-link" href={"/history/" + encodeURIComponent(r.runId)}
                        onClick={(e) => { if (!isPlainLeftClick(e)) return; e.preventDefault(); onView(r.runId); }}>
                        {r.runId}
                      </a>
                    </td>
                    <td className="ha-td">
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                        {(isFeed ? ["LinkedIn Feed"] : r.sources).map((s) => <SourceChip key={s} source={s} />)}
                        {isFeed && <span style={{ fontSize: 11, fontWeight: 700, color: "#6D28D9" }}>· Home Feed leads</span>}
                      </div>
                    </td>
                    <td className="ha-td"><StatusPill status={r.status} /></td>
                    <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>{fmtDate(r.startedAt)}</td>
                    <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>{fmtDate(r.completedAt)}</td>
                    <td
                      className="ha-td"
                      style={{ whiteSpace: "nowrap", color: C.textSoft }}
                      title={r.phaseTimings.scrapeS != null
                        ? `Scrape ${fmtSeconds(r.phaseTimings.scrapeS)} · Resdex & outreach ${fmtSeconds(r.phaseTimings.postHarvestS)}`
                        : undefined}
                    >
                      {r.status === "running" ? "—" : fmtSeconds(r.durationS)}
                    </td>
                    <td className="ha-td" style={{ fontWeight: 600 }}>
                      <AnimatedNumber value={r.jobsFound} />
                      <span style={{ fontSize: 11, fontWeight: 500, color: C.textSoft, marginLeft: 4 }}>{isFeed ? "leads" : "jobs"}</span>
                      {r.status === "running" && <span style={{ color: C.accent, marginLeft: 4 }} title="climbing live">▲</span>}
                    </td>
                    <td className="ha-td">
                      {isFeed ? (
                        <span style={{ fontSize: 12.5, color: C.textSoft }}>IT hiring leads (Home Feed)</span>
                      ) : (
                        <div className="ha-breakdown">
                          <span><b>{r.directClients}</b> DC</span>
                          <span><b>{r.gcc}</b> GCC</span>
                          <span><b>{r.staffingFirms}</b> SF</span>
                          <span><b>{r.ambiguous}</b> Amb</span>
                          {r.phoneInsightsSentAt && (
                            <span title={`Phone insights sent ${fmtDate(r.phoneInsightsSentAt)}`} style={{ color: "#059669", fontSize: 13 }}>&#x260E;</span>
                          )}
                        </div>
                      )}
                    </td>
                  </tr>
                  );
                })}
                {!(loading && !runs.length) && !contactLoading && !error && !contactError && filtered.length === 0 && (
                  <tr><td className="ha-td" colSpan={8} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                    No runs match your search. Trigger a harvest from the Rule Engine to see history here.
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", padding: "12px 16px", fontSize: 12, borderTop: "1px solid #EEF2F7", color: C.textSoft }}>
            <span>Showing {filtered.length} of {runs.length} loaded runs</span>
          </div>
        </div>
      </div>
    </main>
  );
}
