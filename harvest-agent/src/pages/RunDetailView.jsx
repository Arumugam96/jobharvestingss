import { useCallback, useEffect, useMemo, useState } from "react";
import { Download, ArrowLeft, XCircle } from "lucide-react";
import { getRunHistoryEntry, ApiError } from "../api";
import { C } from "../theme";
import ThemeStyles from "../components/ThemeStyles";
import { SourceChip, StatusPill, Select, fmtDate } from "../components/ui";
import JobsTable, { PAGE_SIZE_OPTIONS } from "../components/JobsTable";
import { mapRun, fetchAllJobs } from "../lib/jobsData";

// Labels for the Run Detail classification cards / active-filter chip.
const BUCKET_LABELS = {
  verified: "Verified contacts",
  direct: "Direct clients",
  gcc: "GCC companies",
  staffing: "Staffing firms",
  ambiguous: "Needs review",
};

export default function RunDetailView({ runId, onBack, onView }) {
  const [entry, setEntry] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // This run's harvested jobs (GET /jobs?run_id=…) — same list→detail pattern
  // as the global Harvested Jobs page, scoped to this single run.
  const [jobs, setJobs] = useState([]);
  const [jobsLoading, setJobsLoading] = useState(true);
  const [jobsError, setJobsError] = useState("");
  const [pageSizeSel, setPageSizeSel] = useState("100");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getRunHistoryEntry(runId)
      .then((res) => { if (!cancelled) setEntry(mapRun(res)); })
      .catch((err) => { if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not load this run."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [runId]);

  useEffect(() => {
    let cancelled = false;
    setJobsLoading(true);
    setJobsError("");
    (async () => {
      try {
        const { rows } = await fetchAllJobs({ run_id: runId });
        if (!cancelled) setJobs(rows);
      } catch (err) {
        if (!cancelled) { setJobsError(err instanceof ApiError ? err.message : "Could not load this run's jobs."); setJobs([]); }
      } finally {
        if (!cancelled) setJobsLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [runId]);

  // The classification bucket is driven by the clickable stat cards in the run
  // header ("all" | "verified" | "direct" | "gcc" | "staffing" | "ambiguous").
  // The rest of the filtering (company/contact/job/POC/search/sort) lives in the
  // shared JobsTable, which reports its filtered rows back via onFilteredChange
  // for the stats + CSV export below.
  const [bucket, setBucket] = useState("all");
  const [filteredRows, setFilteredRows] = useState([]);

  const bucketPredicate = useCallback((j) => {
    // Predicates mirror the backend run-summary counts (hiring_entity /
    // verification_status), so a card's number matches the rows shown.
    if (bucket === "verified" && j.verificationStatus !== "verified") return false;
    if (bucket === "direct" && j.hiringEntity !== "Direct Client") return false;
    if (bucket === "gcc" && j.hiringEntity !== "GCC") return false;
    if (bucket === "staffing" && j.hiringEntity !== "Staffing Firm") return false;
    if (bucket === "ambiguous" && j.hiringEntity !== "Ambiguous") return false;
    return true;
  }, [bucket]);

  const jobStats = useMemo(() => ({
    total: filteredRows.length,
    companies: new Set(filteredRows.map((j) => j.company)).size,
    pocs: filteredRows.filter((j) => j.poc).length,
    emails: filteredRows.filter((j) => j.email).length,
    whatsapp: filteredRows.filter((j) => j.whatsapp).length,
  }), [filteredRows]);

  function exportCsv() {
    const header = ["Job title", "Company", "Source", "POC", "Posted date", "Email", "Mobile", "Job description"];
    const lines = filteredRows.map((j) =>
      [j.title, j.company, j.source, j.poc || "—", j.postedDate || "—", j.email || "—", j.mobile || "—", j.jobDescription || "—"]
        .map((c) => '"' + String(c).replace(/"/g, '""') + '"').join(","));
    // Prepend a UTF-8 BOM so Excel detects the encoding — without it Excel reads
    // the file as Windows-1252 and turns the "—" placeholder into "â€"".
    const blob = new Blob([String.fromCharCode(0xFEFF) + [header.join(","), ...lines].join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `jobs_${runId}.csv`; a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="ha-root">
      <ThemeStyles />
      <main className="ha-main">
        <div className="ha-detail-page">
          <button className="ha-detail-back" onClick={onBack}><ArrowLeft size={16} /> Back to Run History</button>
          {loading && <div className="ha-detail-card">Loading run {runId}…</div>}
          {error && <div className="ha-errbanner">{error}</div>}
          {entry && !loading && (
            <>
              <div className="ha-detail-card">
                <div className="ha-runhead">
                  <div className="ha-runhead-meta">
                    <div style={{ fontSize: 12, color: C.textSoft, fontWeight: 600, letterSpacing: ".04em", textTransform: "uppercase" }}>Run ID</div>
                    <div style={{ fontSize: 22, fontWeight: 700, color: C.secondary, letterSpacing: "-.01em" }}>{entry.runId}</div>
                    <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
                      {entry.sources.map((s) => <SourceChip key={s} source={s} />)}
                    </div>
                    <div style={{ display: "flex", gap: 32, marginTop: 18, flexWrap: "wrap" }}>
                      <div>
                        <div style={{ fontSize: 11, color: C.textSoft, fontWeight: 600, letterSpacing: ".05em", textTransform: "uppercase" }}>Started</div>
                        <div style={{ fontSize: 14, fontWeight: 600, color: C.text, marginTop: 3 }}>{fmtDate(entry.startedAt)}</div>
                      </div>
                      <div>
                        <div style={{ fontSize: 11, color: C.textSoft, fontWeight: 600, letterSpacing: ".05em", textTransform: "uppercase" }}>Completed</div>
                        <div style={{ fontSize: 14, fontWeight: 600, color: C.text, marginTop: 3 }}>{fmtDate(entry.completedAt)}</div>
                      </div>
                    </div>
                  </div>
                  <div className="ha-runhead-right">
                    <StatusPill status={entry.status} />
                    <div className="ha-runstats">
                      {[
                        { bucket: "all",       value: entry.jobsFound,     label: "Jobs found",        color: C.accent },
                        { bucket: "verified",  value: entry.verifiedJobs,  label: "Verified contacts", color: "#059669" },
                        { bucket: "direct",    value: entry.directClients, label: "Direct clients",    color: C.primary },
                        { bucket: "gcc",       value: entry.gcc,           label: "GCC companies",     color: C.secondary },
                        { bucket: "staffing",  value: entry.staffingFirms, label: "Staffing firms",    color: "#7C3AED" },
                        { bucket: "ambiguous", value: entry.ambiguous,     label: "Needs review",      color: "#D97706" },
                      ].map((card) => {
                        const active = bucket === card.bucket;
                        return (
                          <button
                            key={card.bucket}
                            type="button"
                            className={"ha-runstat ha-runstat-btn" + (active ? " ha-runstat-active" : "")}
                            aria-pressed={active}
                            title={card.bucket === "all" ? "Show all jobs" : `Filter this run's jobs to ${card.label}`}
                            onClick={() => setBucket((b) => b === card.bucket ? "all" : card.bucket)}
                            style={active ? { boxShadow: `inset 0 0 0 2px ${card.color}` } : undefined}
                          >
                            <b style={{ color: card.color }}>{card.value}</b><span>{card.label}</span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </div>
              </div>

              <div className="ha-detail-card">
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                    <div style={{ fontSize: 12, color: C.textSoft, fontWeight: 700, letterSpacing: ".05em", textTransform: "uppercase" }}>
                      Harvested jobs in this run
                    </div>
                    {bucket !== "all" && (
                      <button className="ha-pill" onClick={() => setBucket("all")}
                        title="Clear card filter"
                        style={{ background: C.pale, color: C.secondary, border: "1px solid " + C.border, display: "inline-flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                        {BUCKET_LABELS[bucket]} <XCircle size={12} />
                      </button>
                    )}
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 16, fontSize: 13, color: C.textSoft }}>
                    <span><b style={{ color: C.text }}>{jobStats.total}</b> jobs</span>
                    <span><b style={{ color: C.text }}>{jobStats.companies}</b> companies</span>
                    <span><b style={{ color: C.text }}>{jobStats.pocs}</b> POCs</span>
                    <span><b style={{ color: C.text }}>{jobStats.emails}</b> emails</span>
                    <span><b style={{ color: C.text }}>{jobStats.whatsapp}</b> WhatsApp</span>
                    <Select label="Rows per page" value={pageSizeSel} onChange={setPageSizeSel} options={PAGE_SIZE_OPTIONS} />
                    <button className="ha-btn ha-btn-primary" onClick={exportCsv} disabled={filteredRows.length === 0}>
                      <Download size={16} /> Export CSV
                    </button>
                  </div>
                </div>

                {jobsError && <div className="ha-errbanner" style={{ marginTop: 14 }}>{jobsError}</div>}

                <div style={{ marginTop: 14 }}>
                  <JobsTable
                    jobs={jobs}
                    onView={onView}
                    loading={jobsLoading}
                    preFilter={bucketPredicate}
                    onFilteredChange={setFilteredRows}
                    minWidth={1410}
                    pageSize={Number(pageSizeSel)}
                    emptyMessage={jobs.length === 0 ? "No jobs recorded for this run." : "No jobs match your filters."}
                  />
                </div>
              </div>
            </>
          )}
        </div>
      </main>
    </div>
  );
}
