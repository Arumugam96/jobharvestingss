import { useEffect, useRef, useState } from "react";
import { ListChecks, Loader2, Square } from "lucide-react";
import { demandError, getDemandBatch, stopDemandBatch } from "./demandApi";
import { isActiveRow, subStepLabel } from "./demandUi";

/* Processing screen shown right after a Demand Search batch (file or manual) is
 * started: polls GET /demand/batches/{run_id} and, once every row has finished,
 * calls onDone() — the page then opens the Demand jobs table. "View jobs now"
 * leaves early (the batch keeps running in the background). */

const POLL_MS = 4000;

/**
 * Progress screen for a just-started Demand batch: progress bar, current row/sub-step and
 * outcome counts, with Stop (POST /demand/batches/{run_id}/stop) and "View jobs now".
 * Polls GET /demand/batches/{runId} every 4s and calls onDone() once nothing is active.
 * @param {object} props
 * @param {string} props.runId - batch run id.
 * @param {Function} props.onDone - opens the jobs table (on completion or "View jobs now").
 */
export default function DemandProcessing({ runId, onDone }) {
  const [batch, setBatch] = useState(null);
  const [error, setError] = useState("");
  const [stopping, setStopping] = useState(false);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;

  useEffect(() => {
    let alive = true;
    let timer = null;
    /** One poll: refresh the batch; call onDone when finished, else schedule the next poll. */
    const tick = async () => {
      try {
        const res = await getDemandBatch(runId);
        if (!alive) return;
        setBatch(res);
        setError("");
        const live = res.status === "running" || (res.rows || []).some(isActiveRow);
        if (!live) { doneRef.current(); return; }
      } catch (err) {
        if (!alive) return;
        setError(demandError(err).message);
      }
      timer = setTimeout(tick, POLL_MS);
    };
    tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [runId]);

  /** Ask the backend to stop the batch after the current row; errors re-enable the button. */
  const stop = async () => {
    setStopping(true);
    try {
      await stopDemandBatch(runId);
    } catch (err) {
      setError(demandError(err).message);
      setStopping(false);
    }
  };

  const rows = batch?.rows || [];
  const c = batch?.counts || {};
  const total = rows.length || batch?.valid_rows || 0;
  const done = c.finished || 0;
  const cur = rows.find((r) => r.status === "in_progress");

  return (
    <>
      <div className="ha-pagehead">
        <div className="ha-pagehead-titles">
          <h1>Demand Search <span className="ha-h1-soft">/ Processing</span></h1>
          <p className="ha-sub">The jobs table opens automatically once every row is processed.</p>
        </div>
      </div>
      <div className="ha-pagebody">
        <div className="ha-card dm-card" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <Loader2 size={18} className="ha-spin" style={{ color: "#2563EB" }} />
            <b style={{ fontSize: 15 }}>{batch?.batch_name || batch?.filename || "Starting batch…"}</b>
            {stopping && <span className="dm-soft" style={{ fontSize: 12 }}>Stopping after the current row…</span>}
          </div>
          {error && <div className="ha-errbanner">{error}</div>}
          <div>
            <div className="dm-bigbar" role="progressbar" aria-label="Batch progress" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done}>
              <i style={{ width: `${total ? Math.round((done / total) * 100) : 0}%` }} />
            </div>
            <div className="dm-bigbar-foot">
              <span><b>{done}</b> of {total} rows processed</span>
              <span>{cur ? `${cur.job_title} · ${subStepLabel(cur)}` : " "}</span>
            </div>
          </div>
          <div className="dm-soft" style={{ fontSize: 12.5 }}>
            <b>{(c.matched || 0) + (c.reused || 0)}</b> matched · <b>{c.no_results || 0}</b> no match · <b>{c.attention || 0}</b> need attention
          </div>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end", flexWrap: "wrap" }}>
            <button type="button" className="ha-btn ha-btn-secondary" onClick={stop} disabled={stopping || !batch}>
              <Square size={14} /> Stop
            </button>
            <button type="button" className="ha-btn ha-btn-primary" onClick={() => doneRef.current()}>
              <ListChecks size={15} /> View jobs now
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
