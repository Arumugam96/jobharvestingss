import { useCallback, useEffect, useRef, useState } from "react";
import { Eye, Loader2, Play, AlertTriangle } from "lucide-react";
import StopHarvestButton from "../components/StopHarvestModal";
import { makeStallWatch, STALL_WARN_MS, STALL_WARN_MSG } from "../stallWatch";
import {
  getHarvestStatus, ApiError,
  runLinkedinAgent, getLinkedinResults, getLinkedinResult,
  runNaukriAgent, getNaukriResults, getNaukriResult,
  runDiceAgent, getDiceResults, getDiceResult,
  runLinkedinFeedAgent, getLinkedinFeedResults, getLinkedinFeedResult,
} from "../api";
import { C } from "../theme";
import { StatusPill, PlainHeader, fmtDate } from "../components/ui";

/* ── Source Runs page (single-source trigger + results, per source) ─────── */
const SOURCE_TABS = [
  { key: "linkedin", label: "LinkedIn", run: runLinkedinAgent, list: getLinkedinResults, one: getLinkedinResult },
  { key: "naukri",   label: "Naukri",   run: runNaukriAgent,   list: getNaukriResults,   one: getNaukriResult },
  { key: "dice",     label: "Dice",     run: runDiceAgent,     list: getDiceResults,     one: getDiceResult },
  // LinkedIn Home Feed leads — async run (202 + poll), unlike the synchronous
  // job-board runs above; `async: true` switches SourceRunsPage to the poll flow.
  { key: "feed",     label: "LinkedIn Feed", run: runLinkedinFeedAgent, list: getLinkedinFeedResults, one: getLinkedinFeedResult, async: true },
];

export default function SourceRunsPage({ harvestRunning, setHarvestRunning }) {
  const [tab, setTab] = useState("linkedin");
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const [runMessage, setRunMessage] = useState("");
  const [stallWarn, setStallWarn] = useState(""); // amber "no progress" notice while a run is stalled
  const [viewing, setViewing] = useState(null); // { runId, jobs } | null
  const pollTimer = useRef(null); // async feed run: recursive setTimeout handle
  const stallWatch = useRef(makeStallWatch());
  const syncStallTimer = useRef(null); // synchronous single-source run: one-shot stall notice

  const current = SOURCE_TABS.find((t) => t.key === tab);

  const fetchResults = useCallback(async (t) => {
    setLoading(true);
    setError("");
    try {
      const res = await t.list();
      setResults(res.results || []);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the harvest backend.");
      setResults([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { clearTimeout(pollTimer.current); clearTimeout(syncStallTimer.current); setStallWarn(""); setViewing(null); fetchResults(current); }, [tab]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => { clearTimeout(pollTimer.current); clearTimeout(syncStallTimer.current); }, []); // clear timers on unmount

  // Poll a background feed run to completion (reuses GET /harvest-status; the run
  // carries a job_id). action_required = LinkedIn session not authenticated.
  const pollFeed = (jobId, feedTab) => {
    const tick = async () => {
      try {
        const st = await getHarvestStatus(jobId);
        if (st.status === "running") {
          setRunMessage(st.message || "Harvesting Home Feed — collecting & classifying posts…");
          // Watchdog: warn (without failing) if message + count freeze for 2 min —
          // e.g. the extraction LLM is down and each call is waiting out its timeout.
          const { stalled } = stallWatch.current.note(`${st.message || ""}|${st.combined ?? 0}`);
          setStallWarn(stalled ? STALL_WARN_MSG : "");
          pollTimer.current = setTimeout(tick, 6000);
          return;
        }
        setStallWarn("");
        if (st.status === "action_required") {
          setRunMessage(st.error || st.message || "LinkedIn session not authenticated — connect LinkedIn (Rule Engine) and retry.");
        } else if (st.status === "failed") {
          setRunMessage(`Failed: ${st.error || st.message}`);
        } else {
          setRunMessage(`${st.status === "success" ? "Success" : "No results"} — ${st.combined ?? 0} IT hiring leads extracted (run_id: ${st.run_id}).`);
          fetchResults(feedTab);
        }
      } catch (err) {
        setRunMessage(err instanceof ApiError ? `Failed: ${err.message}` : "Lost contact with the backend while polling.");
      }
      setStallWarn("");
      setRunning(false);
      setHarvestRunning(false);
    };
    tick();
  };

  const handleRun = async () => {
    if (harvestRunning) {
      setRunMessage("Another harvest is already running (Rule Engine or another source tab) — wait for it to finish. Running two at once collides on the shared browser profile and both fail.");
      return;
    }
    const isAsync = current.async;
    const feedTab = current;
    setRunning(true);
    setHarvestRunning(true);
    setStallWarn("");
    stallWatch.current.reset();
    setRunMessage(isAsync
      ? `Starting ${current.label} harvest…`
      : `Running ${current.label} harvest — this calls a synchronous endpoint and can take several minutes. Don't close this tab.`);
    // Synchronous single-source run: the UI awaits one long request, so there's
    // no poll to hang a watchdog on. Arm a one-shot notice that fires if the
    // request runs past the stall threshold, so a hung backend (e.g. LLM down,
    // waiting out its timeout) doesn't just look like a frozen "don't close" line.
    if (!isAsync) {
      clearTimeout(syncStallTimer.current);
      syncStallTimer.current = setTimeout(
        () => setStallWarn("Still running — the LLM server may be slow or down; you can keep waiting or check the server."),
        STALL_WARN_MS,
      );
    }
    try {
      const res = await current.run();
      if (res.status === "failed") {
        setRunMessage(`Failed: ${res.reason || res.message}`);
        clearTimeout(syncStallTimer.current);
        setStallWarn("");
        setRunning(false);
        setHarvestRunning(false);
        return;
      }
      if (isAsync) {
        // 202 accepted — keep controls frozen and poll the background run to done.
        setRunMessage(`Home-Feed harvest started (run_id: ${res.run_id}) — collecting & classifying posts…`);
        pollFeed(res.job_id, feedTab);
        return;
      }
      setRunMessage(`${res.status === "success" ? "Success" : "No results"} — ${res.total_found ?? 0} jobs found (run_id: ${res.run_id}).`);
      fetchResults(current);
      setRunning(false);
      setHarvestRunning(false);
    } catch (err) {
      // 409 = a harvest is already running — keep controls frozen.
      const conflict = err instanceof ApiError && err.status === 409;
      setRunMessage(err instanceof ApiError ? `Failed: ${err.message}` : "Could not reach the harvest backend.");
      setRunning(false);
      setHarvestRunning(conflict);
    } finally {
      clearTimeout(syncStallTimer.current);
      setStallWarn("");
    }
  };

  const handleView = async (runId) => {
    setViewing({ runId, jobs: null, loading: true });
    try {
      const res = await current.one(runId);
      setViewing({ runId, jobs: res.jobs || [], loading: false });
    } catch (err) {
      setViewing({ runId, jobs: [], loading: false, error: err instanceof ApiError ? err.message : "Could not load this run." });
    }
  };

  return (
    <main className="ha-main">
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", justifyContent: "space-between", gap: 16, padding: "24px 24px 0" }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, color: C.text }}>Source Runs</h1>
          <p style={{ margin: "4px 0 0", fontSize: 14, color: C.textSoft }}>
            Trigger a single-source harvest and browse its saved results. "LinkedIn Feed" scrolls the
            authenticated Home Feed and extracts genuine IT hiring leads.
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <StopHarvestButton harvestRunning={harvestRunning}
            onStopped={() => setRunMessage("Stop requested — the run will halt shortly and save its jobs. The report email is deferred to the next successful run.")} />
          <button className="ha-btn ha-btn-primary" onClick={handleRun} disabled={running || harvestRunning}
            style={harvestRunning && !running ? { background: "#94A3B8", borderColor: "#94A3B8" } : undefined}
            title={harvestRunning && !running ? "A harvest is already running — controls locked until it finishes" : undefined}>
            {(running || harvestRunning) ? <Loader2 size={16} className="ha-spin" /> : <Play size={16} />}
            {running ? "Running…" : harvestRunning ? "Running elsewhere…" : `Run ${current.label} Only`}
          </button>
        </div>
      </div>
      <div style={{ marginTop: 20, borderBottom: "1px solid " + C.border }} />

      <div style={{ display: "flex", flexDirection: "column", gap: 16, padding: 24 }}>
        <div style={{ display: "flex", gap: 8 }}>
          {SOURCE_TABS.map((t) => (
            <button key={t.key} className="ha-btn" style={{
              background: tab === t.key ? C.primary : "#fff",
              color: tab === t.key ? "#fff" : C.textSoft,
              border: "1px solid " + (tab === t.key ? C.primary : C.border),
            }} onClick={() => setTab(t.key)}>{t.label}</button>
          ))}
        </div>

        {runMessage && <div className="ha-card" style={{ padding: "12px 16px", fontSize: 13.5, color: C.text }}>{runMessage}</div>}
        {stallWarn && (
          <div className="ha-card" style={{ padding: "12px 16px", fontSize: 13.5, color: "#B45309", display: "flex", alignItems: "center", gap: 8 }}>
            <AlertTriangle size={16} /> {stallWarn}
          </div>
        )}
        {error && <div className="ha-errbanner">{error}</div>}

        {viewing ? (
          <div className="ha-card" style={{ overflow: "hidden" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "14px 16px", borderBottom: "1px solid #EEF2F7" }}>
              <b>{viewing.runId}</b>
              <button className="ha-btn ha-btn-secondary" onClick={() => setViewing(null)}>Back to results</button>
            </div>
            {viewing.loading && <div style={{ padding: 24, textAlign: "center", color: "#94A3B8" }}>Loading…</div>}
            {viewing.error && <div className="ha-errbanner" style={{ margin: 16 }}>{viewing.error}</div>}
            {!viewing.loading && !viewing.error && (
              <div className="ha-table-scroll">
                <table className="ha-table" style={{ minWidth: 900 }}>
                  <thead className="ha-thead"><tr>
                    <PlainHeader label="Job title" width={320} /><PlainHeader label="Company" width={220} />
                    <PlainHeader label="Location" width={210} /><PlainHeader label="Posted" width={150} />
                  </tr></thead>
                  <tbody>
                    {viewing.jobs.map((j, i) => (
                      <tr key={i} className="ha-row">
                        <td className="ha-td">{j.job_title}</td>
                        <td className="ha-td">{j.company}</td>
                        <td className="ha-td">{j.location}</td>
                        <td className="ha-td">{j.posted_date}</td>
                      </tr>
                    ))}
                    {viewing.jobs.length === 0 && (
                      <tr><td className="ha-td" colSpan={4} style={{ textAlign: "center", padding: 24, color: "#94A3B8" }}>No jobs in this run.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ) : (
          <div className="ha-card" style={{ overflow: "hidden" }}>
            <div className="ha-table-scroll">
              <table className="ha-table" style={{ minWidth: 1080 }}>
                <thead className="ha-thead"><tr>
                  <PlainHeader label="Run ID" width={190} /><PlainHeader label="Status" width={120} /><PlainHeader label="Executed" width={160} />
                  <PlainHeader label="Total found" width={110} /><PlainHeader label="Keyword" width={150} /><PlainHeader label="Location" width={150} />
                  <PlainHeader label="Action" align="center" width={90} />
                </tr></thead>
                <tbody>
                  {loading && <tr><td className="ha-td" colSpan={7} style={{ textAlign: "center", padding: 24, color: "#94A3B8" }}>Loading…</td></tr>}
                  {!loading && results.map((r) => (
                    <tr key={r.run_id} className="ha-row">
                      <td className="ha-td">{r.run_id}</td>
                      <td className="ha-td"><StatusPill status={r.status} /></td>
                      <td className="ha-td" style={{ color: C.textSoft }}>{fmtDate(r.executed_at)}</td>
                      <td className="ha-td" style={{ fontWeight: 600 }}>{r.total_found}</td>
                      <td className="ha-td">{r.keyword}</td>
                      <td className="ha-td">{r.location}</td>
                      <td className="ha-td"><div style={{ display: "flex", justifyContent: "center" }}>
                        <button className="ha-act" title="View" onClick={() => handleView(r.run_id)}><Eye size={16} /></button>
                      </div></td>
                    </tr>
                  ))}
                  {!loading && !error && results.length === 0 && (
                    <tr><td className="ha-td" colSpan={7} style={{ textAlign: "center", padding: 24, color: "#94A3B8" }}>
                      No {current.label} runs yet — click "Run {current.label} Only" above.
                    </td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
