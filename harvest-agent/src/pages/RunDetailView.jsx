import { useCallback, useEffect, useMemo, useState } from "react";
import { Download, ArrowLeft, XCircle } from "lucide-react";
import { getRunHistoryEntry, ApiError } from "../api";
import { C } from "../theme";
import ThemeStyles from "../components/ThemeStyles";
import { SourceChip, StatusPill, Select, fmtDate } from "../components/ui";
import JobsTable, { PAGE_SIZE_OPTIONS } from "../components/JobsTable";
import {
  mapRun, fetchAllJobs, jobContact, pocKindLabel, partSourceLabel, fmtSeconds, uniqueContactStats,
} from "../lib/jobsData";

// Labels for the Run Detail classification cards / active-filter chip.
const BUCKET_LABELS = {
  // verification_status is the career-page check (job title found on the company's
  // careers page) — not a verified email/phone, so don't call it "contacts".
  verified: "Career-page verified",
  direct: "Direct clients",
  gcc: "GCC companies",
  staffing: "Staffing firms",
  ambiguous: "Needs review",
};

// One predicate per stat-card bucket (hiring_entity / verification_status). Used
// both to filter the table (bucketPredicate) and to count the cards (cardCounts),
// so a card's number always equals the rows shown when it is clicked.
const BUCKET_PREDICATES = {
  verified: (j) => j.verificationStatus === "verified",
  direct: (j) => j.hiringEntity === "Direct Client",
  gcc: (j) => j.hiringEntity === "GCC",
  staffing: (j) => j.hiringEntity === "Staffing Firm",
  ambiguous: (j) => j.hiringEntity === "Ambiguous",
};

// Last few runs' loaded data, so returning to a run (e.g. Back from one of its
// jobs) renders instantly — and at the same scroll position — then revalidates
// quietly, instead of blanking to "Loading…" while every page of its jobs refetches.
const runViewCache = new Map(); // runId → { entry?, jobs?, resdexAvailable? }
const RUN_CACHE_MAX = 5;
/** Merges `patch` into the cache entry for `runId` (most recent last, bounded). */
function cacheRun(runId, patch) {
  const next = { ...(runViewCache.get(runId) || {}), ...patch };
  runViewCache.delete(runId);
  runViewCache.set(runId, next);
  while (runViewCache.size > RUN_CACHE_MAX) runViewCache.delete(runViewCache.keys().next().value);
}

/**
 * Detail page for one harvest run: run header and summary from GET /run-history/{runId},
 * classification stat cards that filter by bucket (verified / direct / GCC / staffing /
 * ambiguous), and the run's jobs in the shared JobsTable (all pages of GET /jobs?run_id=…, with
 * a best-effort contact=resdex lookup that marks rows `hasResdex`). Exports the visible rows
 * as CSV.
 * @param {object} props
 * @param {string} props.runId - harvest run id.
 * @param {Function} props.onBack - return to Run History.
 * @param {Function} props.onView - open a job's detail view.
 */
export default function RunDetailView({ runId, onBack, onView }) {
  const cached = runViewCache.get(runId);
  const [entry, setEntry] = useState(cached?.entry || null);
  const [loading, setLoading] = useState(!cached?.entry);
  const [error, setError] = useState("");

  // This run's harvested jobs (GET /jobs?run_id=…) — same list→detail pattern
  // as the global Harvested Jobs page, scoped to this single run.
  const [jobs, setJobs] = useState(cached?.jobs || []);
  const [jobsLoading, setJobsLoading] = useState(!cached?.jobs);
  const [jobsError, setJobsError] = useState("");
  const [pageSizeSel, setPageSizeSel] = useState("100");
  // Resdex searches live server-side (resdex_searches), so the run's Resdex-selected job
  // ids are fetched separately and stamped as `hasResdex` on the rows. Only when
  // that lookup succeeds does the Contact filter offer "Resdex contacts".
  const [resdexAvailable, setResdexAvailable] = useState(!!cached?.resdexAvailable);

  useEffect(() => {
    let cancelled = false;
    // A cached run stays on screen while it revalidates (no loading flash).
    if (!runViewCache.get(runId)?.entry) setLoading(true);
    getRunHistoryEntry(runId)
      .then((res) => {
        if (cancelled) return;
        const mapped = mapRun(res);
        setEntry(mapped);
        cacheRun(runId, { entry: mapped });
      })
      .catch((err) => { if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not load this run."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [runId]);

  useEffect(() => {
    let cancelled = false;
    const hasCachedJobs = !!runViewCache.get(runId)?.jobs;
    if (!hasCachedJobs) {
      setJobsLoading(true);
      setResdexAvailable(false);
    }
    setJobsError("");
    (async () => {
      try {
        // The Resdex lookup is best-effort: a failure just hides the option.
        const [{ rows }, resdex] = await Promise.all([
          fetchAllJobs({ run_id: runId }),
          fetchAllJobs({ run_id: runId, contact: "resdex" }).catch(() => null),
        ]);
        if (cancelled) return;
        const next = resdex
          ? (() => { const matched = new Set(resdex.rows.map((r) => r.id)); return rows.map((r) => (matched.has(r.id) ? { ...r, hasResdex: true } : r)); })()
          : rows;
        setJobs(next);
        setResdexAvailable(!!resdex);
        cacheRun(runId, { jobs: next, resdexAvailable: !!resdex });
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
  // The rest of the filtering (source/company/contact/job/POC/search/sort) lives in the
  // shared JobsTable, which reports its filtered rows back via onFilteredChange
  // for the stats + CSV export below.
  const [bucket, setBucket] = useState("all");
  const [filteredRows, setFilteredRows] = useState([]);
  // Rows passing every JobsTable filter (source/company/contact/job/POC/size/
  // country/job type/search) EXCEPT the card bucket — reported by the table via
  // onBaseFilteredChange. null until its first emit.
  const [baseRows, setBaseRows] = useState(null);

  const bucketPredicate = useCallback((j) => {
    const pred = BUCKET_PREDICATES[bucket];
    return pred ? pred(j) : true;
  }, [bucket]);

  // Card counts come from the run's loaded jobs narrowed by the table's own
  // filters (baseRows), using the same predicates the table filters on — so each
  // card equals the rows shown when it is clicked, under any table filter. Not
  // the run row's counters (verified_jobs / direct_clients / …): those are a
  // snapshot written once at run end (a PATCH /jobs reclassification never
  // updates them, Home Feed runs never write the classification ones, failed runs
  // keep zeros). The counters only fill in while the jobs load (or if that load
  // failed). Before the table's first emit, fall back to all loaded jobs.
  const cardCounts = useMemo(() => {
    if (jobsLoading || jobsError) return null;
    const rows = baseRows ?? jobs;
    const count = (pred) => rows.reduce((n, j) => n + (pred(j) ? 1 : 0), 0);
    return {
      all: rows.length,
      verified: count(BUCKET_PREDICATES.verified),
      direct: count(BUCKET_PREDICATES.direct),
      gcc: count(BUCKET_PREDICATES.gcc),
      staffing: count(BUCKET_PREDICATES.staffing),
      ambiguous: count(BUCKET_PREDICATES.ambiguous),
    };
  }, [baseRows, jobs, jobsLoading, jobsError]);

  // Header stats over the rows currently shown (card bucket + every table filter,
  // incl. Source). POCs / emails / contacts (phones) are UNIQUE (uniqueContactStats:
  // a recruiter on several jobs counts once); the jobs-with-channel counts stay
  // available in each stat's tooltip.
  const jobStats = useMemo(() => {
    const contacts = filteredRows.map(jobContact);
    const unique = uniqueContactStats(filteredRows);
    return {
      total: filteredRows.length,
      companies: new Set(filteredRows.map((j) => j.company).filter((c) => c && c !== "—")).size,
      pocs: unique.pocs,
      emails: unique.emails,
      companyEmails: unique.companyEmails,
      phones: unique.phones,
      jobsWithPoc: contacts.filter((c) => c.pocName).length,
      jobsWithEmail: contacts.filter((c) => c.email).length,
      jobsWithPhone: contacts.filter((c) => c.phone).length,
    };
  }, [filteredRows]);

  /** Download the currently filtered rows as jobs_{runId}.csv (UTF-8 BOM for Excel). */
  function exportCsv() {
    const header = ["Job title", "Company", "Source", "POC", "POC type", "Posted date", "Email", "Email source", "Mobile", "Mobile source", "Job description"];
    const lines = filteredRows.map((j) => {
      const c = jobContact(j);
      return [
        j.title, j.company, c.source, c.pocName || "—", pocKindLabel(c.pocKind), j.postedDate || "—",
        c.email || "—", partSourceLabel(c.emailParts), c.phone || "—", partSourceLabel(c.phoneParts),
        j.jobDescription || "—",
      ].map((v) => '"' + String(v).replace(/"/g, '""') + '"').join(",");
    });
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
              <div className="ha-detail-card" style={{ padding: 0, overflow: "hidden" }}>
                {/* Top bar: Run ID + sources | status */}
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 24px", borderBottom: `1px solid ${C.border}` }}>
                  <div>
                    <div style={{ fontSize: 11, color: C.textSoft, fontWeight: 700, letterSpacing: ".05em", textTransform: "uppercase" }}>Run ID</div>
                    <div style={{ fontSize: 20, fontWeight: 800, color: C.secondary, letterSpacing: "-.01em", marginTop: 2 }}>{entry.runId}</div>
                    <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
                      {entry.sources.map((s) => <SourceChip key={s} source={s} />)}
                    </div>
                  </div>
                  <StatusPill status={entry.status} />
                </div>

                {/* Body: timing left | stats right */}
                <div className="ha-runhead-split">
                  {/* Left: timing + phone insights */}
                  <div className="ha-runhead-timing">
                    <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                      <div>
                        <div style={{ fontSize: 10, color: C.textSoft, fontWeight: 700, letterSpacing: ".06em", textTransform: "uppercase" }}>Started</div>
                        <div style={{ fontSize: 14, fontWeight: 600, color: C.text, marginTop: 2 }}>{fmtDate(entry.startedAt)}</div>
                      </div>
                      <div>
                        <div style={{ fontSize: 10, color: C.textSoft, fontWeight: 700, letterSpacing: ".06em", textTransform: "uppercase" }}>Completed</div>
                        <div style={{ fontSize: 14, fontWeight: 600, color: C.text, marginTop: 2 }}>{fmtDate(entry.completedAt)}</div>
                      </div>
                    </div>

                    {/* Duration with phase bar */}
                    <div>
                      <div style={{ fontSize: 10, color: C.textSoft, fontWeight: 700, letterSpacing: ".06em", textTransform: "uppercase" }}>Duration</div>
                      <div style={{ fontSize: 22, fontWeight: 800, color: C.text, marginTop: 2 }}>
                        {entry.status === "running" ? "—" : fmtSeconds(entry.durationS)}
                      </div>
                      {entry.phaseTimings.scrapeS != null && (() => {
                        const scrape = entry.phaseTimings.scrapeS || 0;
                        const post = entry.phaseTimings.postHarvestS || 0;
                        return (
                          <>
                            <div style={{ display: "flex", height: 5, borderRadius: 3, overflow: "hidden", marginTop: 6, gap: 2 }}>
                              <div style={{ flex: scrape, background: C.primary, borderRadius: 3 }} />
                              {post > 0 && <div style={{ flex: post, background: C.accent, borderRadius: 3, minWidth: 4 }} />}
                            </div>
                            <div style={{ display: "flex", gap: 10, marginTop: 4, fontSize: 11, color: C.textSoft }}>
                              <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                                <span style={{ width: 7, height: 7, borderRadius: "50%", background: C.primary, display: "inline-block" }} />
                                Scrape {fmtSeconds(scrape)}
                              </span>
                              {post > 0 && (
                                <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                                  <span style={{ width: 7, height: 7, borderRadius: "50%", background: C.accent, display: "inline-block" }} />
                                  Resdex &amp; outreach {fmtSeconds(post)}
                                </span>
                              )}
                            </div>
                          </>
                        );
                      })()}
                    </div>

                    {/* Phone Insights */}
                    {entry.runType !== "feed" && (
                      <div style={{
                        padding: "10px 14px", borderRadius: 9, border: `1px solid ${C.border}`,
                        background: C.pale, display: "flex", flexDirection: "column", gap: 4,
                      }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <span style={{ fontSize: 12, fontWeight: 700, color: C.text }}>Phone Insights</span>
                          {entry.phoneInsightsSentAt ? (
                            <span style={{
                              fontSize: 10, fontWeight: 700, letterSpacing: ".04em", textTransform: "uppercase",
                              padding: "2px 8px", borderRadius: 999,
                              background: "#ECFDF5", color: "#059669", border: "1px solid #A7F3D0",
                            }}>Sent</span>
                          ) : entry.completedAt ? (
                            <span style={{
                              fontSize: 10, fontWeight: 700, letterSpacing: ".04em", textTransform: "uppercase",
                              padding: "2px 8px", borderRadius: 999,
                              background: "#FFFBEB", color: "#D97706", border: "1px solid #FCD34D",
                            }}>Scheduled</span>
                          ) : null}
                        </div>
                        {entry.phoneInsightsSentAt ? (
                          <div style={{ fontSize: 13, fontWeight: 600, color: C.text }}>{fmtDate(entry.phoneInsightsSentAt)}</div>
                        ) : entry.completedAt ? (
                          <>
                            <div style={{ fontSize: 13, fontWeight: 600, color: C.text }}>
                              {(() => {
                                const d = new Date(entry.completedAt);
                                if (Number.isNaN(d.getTime())) return "~24h after run";
                                d.setDate(d.getDate() + 1);
                                return `~${d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}`;
                              })()}
                            </div>
                            <div style={{ fontSize: 11, color: C.textSoft }}>Phone reveal report will be emailed automatically</div>
                          </>
                        ) : (
                          <div style={{ fontSize: 13, color: C.textSoft }}>—</div>
                        )}
                      </div>
                    )}
                  </div>

                  {/* Right: stat cards */}
                  <div className="ha-runhead-stats-side">
                    <div className="ha-runstats">
                      {[
                        { bucket: "all",       value: cardCounts?.all ?? entry.jobsFound,           label: "Jobs found",        color: C.accent },
                        { bucket: "verified",  value: cardCounts?.verified ?? entry.verifiedJobs,   label: BUCKET_LABELS.verified, color: "#059669",
                          hint: "Jobs whose title was found on the company's careers page" },
                        { bucket: "direct",    value: cardCounts?.direct ?? entry.directClients,    label: "Direct clients",    color: C.primary },
                        { bucket: "gcc",       value: cardCounts?.gcc ?? entry.gcc,                 label: "GCC companies",     color: C.secondary },
                        { bucket: "staffing",  value: cardCounts?.staffing ?? entry.staffingFirms,  label: "Staffing firms",    color: "#7C3AED" },
                        { bucket: "ambiguous", value: cardCounts?.ambiguous ?? entry.ambiguous,     label: "Needs review",      color: "#D97706" },
                      ].map((card) => {
                        const active = bucket === card.bucket;
                        return (
                          <button
                            key={card.bucket}
                            type="button"
                            className={"ha-runstat ha-runstat-btn" + (active ? " ha-runstat-active" : "")}
                            aria-pressed={active}
                            title={(card.bucket === "all" ? "Show all jobs" : `Filter this run's jobs to ${card.label}`)
                              + (card.hint ? ` (${card.hint})` : "")}
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
                    <span title={`Unique POCs (name + company) · ${jobStats.jobsWithPoc} jobs have a POC`}>
                      <b style={{ color: C.text }}>{jobStats.pocs}</b> POCs
                    </span>
                    <span title={`Unique email addresses: ${jobStats.emails - jobStats.companyEmails} job/recruiter · ${jobStats.companyEmails} company contact · ${jobStats.jobsWithEmail} jobs have an email`}>
                      <b style={{ color: C.text }}>{jobStats.emails}</b> emails
                    </span>
                    <span title={`Unique phone numbers · ${jobStats.jobsWithPhone} jobs have a phone`}>
                      <b style={{ color: C.text }}>{jobStats.phones}</b> Contacts
                    </span>
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
                    onBaseFilteredChange={setBaseRows}
                    minWidth={1410}
                    pageSize={Number(pageSizeSel)}
                    resdexAvailable={resdexAvailable}
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
