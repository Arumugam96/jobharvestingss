import { useEffect, useMemo, useState } from "react";
import { SlidersHorizontal, Download, Search, RefreshCw } from "lucide-react";
import { C } from "../theme";
import {
  SourceChip, StatusPill, AnimatedNumber, StatCard, SortHeader, PlainHeader, Select, fmtDate,
} from "../components/ui";
import { useHarvestData } from "../HarvestDataContext";
import { useTenant } from "../TenantContext";

/* ── Run History page ────────────────────────────────────────────────── */
export default function RunHistoryPage({ runs, loading, error, onRefresh, onNavigate, onView }) {
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
  useEffect(() => {
    runHistoryViewRef.current = { filters, startDate, endDate, query, sort };
  }, [runHistoryViewRef, filters, startDate, endDate, query, sort]);

  const counts = useMemo(() => ({
    totalRuns: runs.length,
    totalJobs: runs.reduce((sum, r) => sum + r.jobsFound, 0),
    directClients: runs.reduce((sum, r) => sum + r.directClients, 0),
  }), [runs]);

  const filtered = useMemo(() => {
    let rows = runs.filter((r) => {
      if (filters.source !== "all" && !r.sources.some((s) => s.toLowerCase() === filters.source)) return false;
      if (filters.status !== "all" && r.status !== filters.status) return false;
      if (startDate && r.startedAt.slice(0, 10) < startDate) return false;
      if (endDate && r.startedAt.slice(0, 10) > endDate) return false;
      if (query.trim() && !r.runId.toLowerCase().includes(query.trim().toLowerCase())) return false;
      return true;
    });
    const dir = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      switch (sort.col) {
        case "runId": return a.runId.localeCompare(b.runId) * dir;
        case "status": return a.status.localeCompare(b.status) * dir;
        case "started": return a.startedAt.localeCompare(b.startedAt) * dir;
        case "completed": return a.completedAt.localeCompare(b.completedAt) * dir;
        case "jobsFound": return (a.jobsFound - b.jobsFound) * dir;
        default: return 0;
      }
    });
  }, [runs, filters, startDate, endDate, query, sort]);

  function exportCsv() {
    const header = ["Run ID", "Sources", "Status", "Started", "Completed", "Jobs found", "Verified", "Direct clients", "GCC", "Staffing firms", "Ambiguous"];
    const lines = filtered.map((r) =>
      [r.runId, r.sources.join("|"), r.status, r.startedAt, r.completedAt, r.jobsFound, r.verifiedJobs, r.directClients, r.gcc, r.staffingFirms, r.ambiguous]
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
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", justifyContent: "space-between", gap: 16, padding: "24px 24px 0" }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, color: C.text }}>Run History</h1>
          <p style={{ margin: "4px 0 0", fontSize: 14, color: C.textSoft }}>
            {loading ? "Loading…" : `${counts.totalRuns} run${counts.totalRuns === 1 ? "" : "s"} · ${counts.totalJobs} jobs harvested`}
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button className="ha-btn ha-btn-secondary" onClick={onRefresh} disabled={loading} title="Refresh">
            <RefreshCw size={16} className={loading ? "ha-spin" : ""} /> Refresh
          </button>
          {hasFeature("rules") && (
            <button className="ha-btn ha-btn-secondary" onClick={() => onNavigate("rules")}><SlidersHorizontal size={16} /> Rule Engine</button>
          )}
          <button className="ha-btn ha-btn-primary" onClick={exportCsv}><Download size={16} /> Export CSV</button>
        </div>
      </div>
      <div style={{ marginTop: 20, borderBottom: "1px solid " + C.border }} />

      <div style={{ display: "flex", flexDirection: "column", gap: 16, padding: 24 }}>
        {error && <div className="ha-errbanner">{error}</div>}

        <div className="ha-daterow">
          <span style={{ fontSize: 14, fontWeight: 600, color: C.text }}>Started between</span>
          <input type="date" className="ha-input" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
          <span style={{ color: C.textSoft }}>to</span>
          <input type="date" className="ha-input" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          {(startDate || endDate) && (
            <button className="ha-btn ha-btn-secondary" style={{ height: 38, boxSizing: "border-box", padding: "0 14px" }} onClick={() => { setStartDate(""); setEndDate(""); }}>Clear dates</button>
          )}
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 16 }}>
          <StatCard value={counts.totalRuns} label="Total runs" color={C.accent} />
          <StatCard value={counts.totalJobs} label="Jobs harvested" color={C.primary} />
          <StatCard value={counts.directClients} label="Direct clients" color={C.primary} />
        </div>

        <div className="ha-card ha-filterbar" style={{ padding: "16px 20px", gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
          <Select label="Source" value={filters.source} onChange={(v) => setFilters((f) => ({ ...f, source: v }))}
            options={[{ value: "all", label: "All" }, { value: "linkedin", label: "LinkedIn" }, { value: "linkedin feed", label: "LinkedIn Feed" }, { value: "naukri", label: "Naukri" }, { value: "dice", label: "Dice" }]} />
          <Select label="Status" value={filters.status} onChange={(v) => setFilters((f) => ({ ...f, status: v }))}
            options={[{ value: "all", label: "All" }, { value: "success", label: "Success" }, { value: "no_results", label: "No results" }, { value: "failed", label: "Failed" }, { value: "running", label: "Running" }, { value: "stopped", label: "Stopped" }]} />
          <div className="ha-filter-search">
            <Search size={16} />
            <input className="ha-input" value={query}
              onChange={(e) => setQuery(e.target.value)} placeholder="Search run ID…" />
          </div>
        </div>

        <div className="ha-card" style={{ overflow: "hidden" }}>
          <div className="ha-table-scroll">
            <table className="ha-table" style={{ minWidth: 1100 }}>
              <thead className="ha-thead">
                <tr>
                  <SortHeader label="Run ID" col="runId" sort={sort} setSort={setSort} width={190} />
                  <PlainHeader label="Sources" width={170} />
                  <SortHeader label="Status" col="status" sort={sort} setSort={setSort} width={120} />
                  <SortHeader label="Started" col="started" sort={sort} setSort={setSort} width={150} />
                  <SortHeader label="Completed" col="completed" sort={sort} setSort={setSort} width={150} />
                  <SortHeader label="Jobs found" col="jobsFound" sort={sort} setSort={setSort} width={100} />
                  <PlainHeader label="Breakdown" width={220} />
                </tr>
              </thead>
              <tbody>
                {loading && (
                  <tr><td className="ha-td" colSpan={7} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
                    Loading run history…
                  </td></tr>
                )}
                {!loading && filtered.map((r) => {
                  const isFeed = r.runType === "feed";
                  return (
                  <tr key={r.runId} className="ha-row">
                    <td className="ha-td"><button className="ha-link" onClick={() => onView(r.runId)}>{r.runId}</button></td>
                    <td className="ha-td">
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                        {(isFeed ? ["LinkedIn Feed"] : r.sources).map((s) => <SourceChip key={s} source={s} />)}
                        {isFeed && <span style={{ fontSize: 11, fontWeight: 700, color: "#6D28D9" }}>· Home Feed leads</span>}
                      </div>
                    </td>
                    <td className="ha-td"><StatusPill status={r.status} /></td>
                    <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>{fmtDate(r.startedAt)}</td>
                    <td className="ha-td" style={{ whiteSpace: "nowrap", color: C.textSoft }}>{fmtDate(r.completedAt)}</td>
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
                        </div>
                      )}
                    </td>
                  </tr>
                  );
                })}
                {!loading && !error && filtered.length === 0 && (
                  <tr><td className="ha-td" colSpan={7} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>
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
