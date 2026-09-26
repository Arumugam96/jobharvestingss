import { useCallback, useEffect, useState } from "react";
import { Eye, Loader2, Play } from "lucide-react";
import {
  runProspectIntelligence, getProspectResults, getProspectResult,
  runRecruiterDiscovery, ApiError,
} from "../api";
import { C } from "../theme";
import { Select } from "../components/ui";

/* ── Lead Intelligence page (Prospect Intelligence + Recruiter Discovery) ── */
export default function LeadIntelligencePage() {
  const [inputFile, setInputFile] = useState("data/prospects/input/prospects.xlsx");
  const [piConcurrency, setPiConcurrency] = useState(2);
  const [piRunning, setPiRunning] = useState(false);
  const [piResult, setPiResult] = useState(null);
  const [piError, setPiError] = useState("");
  const [piResults, setPiResults] = useState([]);
  const [piViewing, setPiViewing] = useState(null);

  const [sourceFilter, setSourceFilter] = useState("all");
  const [runIds, setRunIds] = useState("");
  const [maxFiles, setMaxFiles] = useState(10);
  const [rdConcurrency, setRdConcurrency] = useState(2);
  const [rdRunning, setRdRunning] = useState(false);
  const [rdResult, setRdResult] = useState(null);
  const [rdError, setRdError] = useState("");

  const loadPiResults = useCallback(async () => {
    try {
      const res = await getProspectResults();
      setPiResults(res.runs || []);
    } catch {
      setPiResults([]);
    }
  }, []);
  useEffect(() => { loadPiResults(); }, [loadPiResults]);

  const handleRunProspect = async () => {
    setPiRunning(true);
    setPiError("");
    setPiResult(null);
    try {
      const res = await runProspectIntelligence({ input_file: inputFile, concurrency: Number(piConcurrency) });
      if (res.status === "failed") setPiError(res.message + (res.hint ? ` — ${res.hint}` : ""));
      else { setPiResult(res); loadPiResults(); }
    } catch (err) {
      setPiError(err instanceof ApiError ? err.message : "Could not reach the harvest backend.");
    } finally {
      setPiRunning(false);
    }
  };

  const handleViewProspectRun = async (runId) => {
    setPiViewing({ runId, loading: true });
    try {
      const res = await getProspectResult(runId);
      setPiViewing({ runId, loading: false, data: res });
    } catch (err) {
      setPiViewing({ runId, loading: false, error: err instanceof ApiError ? err.message : "Could not load this run." });
    }
  };

  const handleRunRecruiterDiscovery = async () => {
    setRdRunning(true);
    setRdError("");
    setRdResult(null);
    try {
      const res = await runRecruiterDiscovery({
        source_filter: sourceFilter,
        run_ids: runIds.trim() ? runIds.split(",").map((s) => s.trim()).filter(Boolean) : [],
        max_files: Number(maxFiles),
        concurrency: Number(rdConcurrency),
      });
      if (res.status === "failed" || res.status === "no_data") setRdError(res.message + (res.hint ? ` — ${res.hint}` : ""));
      else setRdResult(res);
    } catch (err) {
      setRdError(err instanceof ApiError ? err.message : "Could not reach the harvest backend.");
    } finally {
      setRdRunning(false);
    }
  };

  return (
    <main className="ha-main">
      <div style={{ padding: "24px 24px 0" }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, color: C.text }}>Lead Intelligence</h1>
        <p style={{ margin: "4px 0 0", fontSize: 14, color: C.textSoft }}>
          Two separate enrichment pipelines — prospect list enrichment and automatic recruiter discovery from harvested jobs.
        </p>
      </div>
      <div style={{ marginTop: 20, borderBottom: "1px solid " + C.border }} />

      <div style={{ display: "flex", flexDirection: "column", gap: 24, padding: 24 }}>
        {/* Prospect Intelligence */}
        <div className="ha-card" style={{ padding: 20 }}>
          <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 4 }}>Prospect Intelligence</div>
          <p style={{ fontSize: 13, color: C.textSoft, margin: "0 0 14px" }}>
            POST /run-prospect-intelligence — enriches a manually-prepared prospects.xlsx (columns: Client Name, Poc Name, Designation) with predicted contact data. This does <b>not</b> read from harvested jobs.
          </p>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5, color: C.textSoft, flex: 1, minWidth: 260 }}>
              Input file
              <input className="ha-input" style={{ padding: "8px 10px" }} value={inputFile} onChange={(e) => setInputFile(e.target.value)} />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5, color: C.textSoft }}>
              Concurrency (1–5)
              <input className="ha-input" type="number" min={1} max={5} style={{ padding: "8px 10px", width: 90 }} value={piConcurrency} onChange={(e) => setPiConcurrency(e.target.value)} />
            </label>
            <button className="ha-btn ha-btn-primary" onClick={handleRunProspect} disabled={piRunning}>
              {piRunning ? <Loader2 size={16} className="ha-spin" /> : <Play size={16} />} {piRunning ? "Running…" : "Run"}
            </button>
          </div>
          {piError && <div className="ha-errbanner" style={{ marginTop: 14 }}>{piError}</div>}
          {piResult && (
            <div className="ha-breakdown" style={{ marginTop: 14, fontSize: 13 }}>
              <span><b>{piResult.total_prospects}</b> prospects</span>
              <span><b>{piResult.enriched}</b> enriched</span>
              <span><b>{piResult.high_confidence}</b> high</span>
              <span><b>{piResult.medium_confidence}</b> medium</span>
              <span><b>{piResult.low_confidence}</b> low</span>
              <span>run_id: <b>{piResult.run_id}</b></span>
            </div>
          )}

          {piResults.length > 0 && (
            <div style={{ marginTop: 16, borderTop: "1px solid #EEF2F7", paddingTop: 14 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: C.textSoft, textTransform: "uppercase", marginBottom: 8 }}>Past runs</div>
              {piResults.map((r) => (
                <div key={r.run_id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 0", fontSize: 13 }}>
                  <span>{r.run_id} — {r.enriched}/{r.total} enriched</span>
                  <button className="ha-act" title="View" onClick={() => handleViewProspectRun(r.run_id)}><Eye size={16} /></button>
                </div>
              ))}
            </div>
          )}
          {piViewing && (
            <div style={{ marginTop: 12, background: C.pale, borderRadius: 8, padding: 12, fontSize: 12.5 }}>
              {piViewing.loading ? "Loading…" : piViewing.error ? piViewing.error : (
                <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{JSON.stringify(piViewing.data, null, 2).slice(0, 2000)}</pre>
              )}
            </div>
          )}
        </div>

        {/* Recruiter Discovery */}
        <div className="ha-card" style={{ padding: 20 }}>
          <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 4 }}>Recruiter Contact Discovery</div>
          <p style={{ fontSize: 13, color: C.textSoft, margin: "0 0 14px" }}>
            POST /run-recruiter-discovery — reads job posters from completed harvest runs and enriches them via company site / LinkedIn / Naukri lookup. Requires jobs with a named poster (see Rule Engine notes).
          </p>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
            <Select label="Source filter" value={sourceFilter} onChange={setSourceFilter} options={[
              { value: "all", label: "All" }, { value: "combined", label: "Combined" },
              { value: "linkedin", label: "LinkedIn" }, { value: "naukri", label: "Naukri" }, { value: "dice", label: "Dice" },
            ]} />
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5, color: C.textSoft, flex: 1, minWidth: 220 }}>
              Run IDs (optional, comma-separated)
              <input className="ha-input" style={{ padding: "8px 10px" }} value={runIds} onChange={(e) => setRunIds(e.target.value)} placeholder="e.g. 20260706_100116" />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5, color: C.textSoft }}>
              Max files (1–50)
              <input className="ha-input" type="number" min={1} max={50} style={{ padding: "8px 10px", width: 90 }} value={maxFiles} onChange={(e) => setMaxFiles(e.target.value)} />
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12.5, color: C.textSoft }}>
              Concurrency (1–5)
              <input className="ha-input" type="number" min={1} max={5} style={{ padding: "8px 10px", width: 90 }} value={rdConcurrency} onChange={(e) => setRdConcurrency(e.target.value)} />
            </label>
            <button className="ha-btn ha-btn-primary" onClick={handleRunRecruiterDiscovery} disabled={rdRunning}>
              {rdRunning ? <Loader2 size={16} className="ha-spin" /> : <Play size={16} />} {rdRunning ? "Running…" : "Run"}
            </button>
          </div>
          {rdError && <div className="ha-errbanner" style={{ marginTop: 14 }}>{rdError}</div>}
          {rdResult && (
            <div className="ha-breakdown" style={{ marginTop: 14, fontSize: 13 }}>
              <span><b>{rdResult.total_recruiters}</b> recruiters</span>
              <span><b>{rdResult.enriched}</b> enriched</span>
              <span><b>{rdResult.contact_discovery?.verified_emails ?? 0}</b> verified emails</span>
              <span><b>{rdResult.contact_discovery?.public_emails ?? 0}</b> public emails</span>
              <span>run_id: <b>{rdResult.run_id}</b></span>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
