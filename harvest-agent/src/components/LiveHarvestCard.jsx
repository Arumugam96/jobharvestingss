import React from "react";
import { GLASS, GLASS_FALLBACK } from "../theme";
import useCountUp from "../useCountUp";
import { SOURCE_META, STATE_META, sourceEntries } from "./SourceProgress";

/*
 * Live Harvest card — the Rule Engine's progress area while a harvest runs
 * (shared by RuleEngineConfig and the US RuleEngineRedesign).
 *
 *   ┌ LIVE HARVEST (LIVE) ─────────── Jobs saved · Daily cap · Remaining ┐
 *   │ LinkedIn lane                     │ Naukri lane                      │
 *   └───────────────────────────────────┴──────────────────────────────────┘
 *
 * Summary strip: the run-level DB-persisted total (status.combined /
 * jobs_saved_today, passed in as `saved`) and the MAX_JOBS_PER_DAY figures.
 * One lane per ENABLED source from GET /harvest-status `sources`, side by side
 * (LinkedIn | Naukri | Dice; a single lane spans the full width; lanes stack
 * when the card is narrow). Each lane, fed by the optional progress keys of
 * its `sources` entry (app/services/source_status.py PROGRESS_KEYS):
 *   corner number   reported_total (the site's own count for the window), else
 *                   `listed` with a "+" while listing is still going
 *   phase stepper   Listing → Details → Done
 *   funnel          Found / Already harvested / To fetch | Target (cap) / Saved
 *   detail bar      done / target
 *   batch meter     batch_size segments, one lit per extracted job, cleared on
 *                   each flush (HARVEST_PERSIST_BATCH_SIZE)
 *   message         the source's latest progress line
 * Failed / skipped sources keep their last numbers and show the reason + error
 * instead of the batch meter. Entries without the progress keys (older
 * backends, Dice, a source that hasn't started) render the compact
 * state · count · message lane.
 */

const LANE_ORDER = ["linkedin", "naukri", "dice"];
const MAX_SEGMENTS = 30; // batch meter rendering bound (a huge batch size scales down)

const n = (v) => (v === null || v === undefined || v === "" || Number.isNaN(Number(v)) ? null : Number(v));
const fmt = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString());

/** "Past 24h" / "Past 7d" for a search_window_hours value. */
export function windowLabel(hours) {
  const h = n(hours);
  if (!h) return "";
  if (h > 72 && h % 24 === 0) return `Past ${h / 24}d`;
  return `Past ${h}h`;
}

/** True when an entry carries the structured progress keys. */
export function hasProgress(entry) {
  return !!entry && (entry.phase != null || entry.listed != null || entry.reported_total != null);
}

function StateChip({ state }) {
  const st = STATE_META[state] || STATE_META.pending;
  return (
    <span className={"lh-chip " + st.cls}>
      {state === "running" ? <i className="lh-dot" aria-hidden="true" /> : <st.Icon size={11} />}
      {st.label}
    </span>
  );
}

function FailDetail({ entry }) {
  const detail = [entry.reason, entry.error && entry.error !== entry.reason ? entry.error : ""]
    .filter(Boolean).join(" — ");
  if (!detail) return null;
  return <div className="lh-err" title={entry.error || entry.reason}>{detail}</div>;
}

function LaneHead({ srcKey, entry, windowHours, corner, cornerLabel }) {
  const meta = SOURCE_META[srcKey] || { name: srcKey, glyph: (srcKey || "?").slice(0, 1).toUpperCase(), tint: "#64748B" };
  const win = windowLabel(entry.window_hours ?? windowHours);
  const cap = n(entry.cap);
  return (
    <div className="lh-lane-h">
      <span className="lh-glyph" style={{ background: meta.tint }} aria-hidden="true">{meta.glyph}</span>
      <div className="lh-lane-id">
        <b>{meta.name}</b>
        <div className="lh-chips">
          <StateChip state={entry.state} />
          {win && <span className="lh-chip">{win}</span>}
          {cap != null && hasProgress(entry) && <span className="lh-chip">Cap {fmt(cap)}</span>}
        </div>
      </div>
      <div className="lh-found">
        <b>{corner}</b>
        <small>{cornerLabel}</small>
      </div>
    </div>
  );
}

/** Older backends / sources without progress keys: state · count · message. */
function CompactLane({ srcKey, entry, windowHours }) {
  const count = useCountUp(entry.count ?? 0);
  return (
    <div className={"lh-lane is-compact " + (STATE_META[entry.state] || STATE_META.pending).cls}>
      <LaneHead srcKey={srcKey} entry={entry} windowHours={windowHours}
        corner={count.toLocaleString()} cornerLabel="jobs saved" />
      {entry.state === "failed" && <FailDetail entry={entry} />}
      <div className="lh-msg" title={entry.message || ""}>{entry.message || "—"}</div>
    </div>
  );
}

function ProgressLane({ srcKey, entry, windowHours }) {
  const state = entry.state || "pending";
  const running = state === "running";
  const phase = entry.phase || null;
  const reported = n(entry.reported_total);
  const listed = n(entry.listed) ?? 0;
  const skipped = n(entry.skipped) ?? 0;
  const cap = n(entry.cap);
  const doneN = n(entry.done) ?? 0;
  const target = n(entry.target);
  const page = n(entry.page);
  const totalPages = n(entry.total_pages);
  const batchSize = Math.max(1, n(entry.batch_size) ?? 10);
  const inBatch = Math.min(batchSize, n(entry.in_batch) ?? 0);
  const batches = n(entry.batches) ?? 0;

  const savedAnim = useCountUp(entry.count ?? 0);
  const doneAnim = useCountUp(doneN);
  const reportedAnim = useCountUp(reported ?? 0);
  const listedAnim = useCountUp(listed);

  const listingDone = phase === "done"
    || (reported != null && listed >= reported)
    || (totalPages != null && page != null && page >= totalPages && phase !== "listing");
  const terminal = state === "done" || state === "failed" || state === "skipped";
  const win = windowLabel(entry.window_hours ?? windowHours);

  // Corner number: the site's own count, else what we've listed so far.
  const corner = reported != null
    ? reportedAnim.toLocaleString()
    : listedAnim.toLocaleString() + (!listingDone && !terminal ? "+" : "");
  const cornerLabel = reported != null
    ? `found${win ? " · " + win.toLowerCase() : ""}`
    : (listingDone || terminal ? "listed" : "listed so far");

  // Phase stepper. Naukri (and LinkedIn page-by-page) alternate listing and
  // details, so Listing stays "now" until the listing is exhausted.
  const listingCls = listingDone ? "done" : (running ? "now" : "");
  const detailsCls = phase === "done" || state === "done" ? "done"
    : phase === "details" ? "now" : (doneN > 0 ? "done" : "");
  const doneCls = state === "done" ? "done" : (phase === "done" && running ? "now" : "");
  const listingSub = totalPages != null && page != null
    ? `page ${page} of ${fmt(totalPages)}`
    : `${page ?? 1} page${(page ?? 1) === 1 ? "" : "s"} · ${fmt(listed)} cards`;
  const detailsSub = phase === "details"
    ? (totalPages != null && !listingDone ? "per page" : "opening each job")
    : `${fmt(doneN)} fetched`;
  const doneSub = state === "done" ? `${fmt(entry.count ?? 0)} saved`
    : phase === "done" && running ? "post-processing" : "";

  const atCap = cap != null && target != null && target >= cap;
  const pct = target ? Math.min(100, (doneN / target) * 100) : (phase === "done" ? 100 : 0);

  // Batch meter: one segment per job in the pending batch (bounded for huge sizes).
  const segCount = Math.min(batchSize, MAX_SEGMENTS);
  const litSegs = Math.round((inBatch / batchSize) * segCount);
  const batchNo = running ? batches + 1 : batches;

  return (
    <div className={"lh-lane " + (STATE_META[state] || STATE_META.pending).cls}>
      <LaneHead srcKey={srcKey} entry={entry} windowHours={windowHours} corner={corner} cornerLabel={cornerLabel} />

      <ol className="lh-phases" aria-label="Phase">
        <li className={listingCls}>Listing<small>{listingSub}</small></li>
        <li className={detailsCls}>Details<small>{detailsSub}</small></li>
        <li className={doneCls}>Done<small>{doneSub || " "}</small></li>
      </ol>

      <div className="lh-funnel">
        <div>
          <b>{listingDone && reported != null ? fmt(reported) : listedAnim.toLocaleString()}</b>
          <span>{listingDone ? "Found" : "Listed so far"}</span>
        </div>
        <div><b>{fmt(skipped)}</b><span>Already harvested</span></div>
        <div><b>{fmt(target)}</b><span>{atCap ? "Target (cap)" : "To fetch"}</span></div>
        <div className="is-saved"><b>{savedAnim.toLocaleString()}</b><span>Saved</span></div>
      </div>

      <div className="lh-prog">
        <div className="lh-prog-row">
          <span>{atCap ? "Toward target" : "Detail extraction"}</span>
          <span><b>{doneAnim.toLocaleString()}</b> / {fmt(target)}</span>
        </div>
        <div className="lh-bar" role="progressbar" aria-valuemin={0} aria-valuemax={target || 0} aria-valuenow={doneN}>
          <i style={{ width: `${pct.toFixed(1)}%` }} />
        </div>
      </div>

      {state === "failed" || state === "skipped" ? (
        <FailDetail entry={{ ...entry, reason: entry.reason || (state === "skipped" ? "Skipped" : "Failed") }} />
      ) : state === "done" ? (
        <div className="lh-batch">
          <span className="lh-batch-l">
            <b>{fmt(batches)}</b> batch{batches === 1 ? "" : "es"} saved · every {fmt(batchSize)}
          </span>
        </div>
      ) : (
        <div className="lh-batch">
          <span className="lh-batch-l">Batch <b>{fmt(batchNo)}</b></span>
          <span className="lh-segs" aria-hidden="true">
            {Array.from({ length: segCount }, (_, i) => <i key={i} className={i < litSegs ? "on" : undefined} />)}
          </span>
          <span className="lh-batch-r">{inBatch} / {batchSize} · saves every {batchSize}</span>
        </div>
      )}

      <div className="lh-msg" title={entry.message || ""}>{entry.message || "—"}</div>
    </div>
  );
}

/**
 * @param sources      GET /harvest-status `sources` map
 * @param saved        jobs saved (DB-persisted) for the summary strip
 * @param maxPerDay    MAX_JOBS_PER_DAY (0 = unlimited)
 * @param live         run is in progress (pulsing LIVE tag)
 * @param windowHours  fallback "Past Nh" when an entry has no window_hours
 * @param showSummary  render the Jobs saved / Daily cap / Remaining strip
 * @param bare         drop the card chrome (inside an existing card/modal)
 */
export default function LiveHarvestCard({
  sources, saved = 0, maxPerDay = 0, live = false, windowHours = null,
  showSummary = true, bare = false, title,
}) {
  const unlimited = !(Number(maxPerDay) > 0);
  const savedCount = useCountUp(saved);
  const capCount = useCountUp(maxPerDay);
  const remainingCount = useCountUp(unlimited ? 0 : Math.max(0, Number(maxPerDay) - Number(saved || 0)));

  const entries = sourceEntries(sources).sort(
    ([a], [b]) => (LANE_ORDER.indexOf(a) + 99) % 99 - (LANE_ORDER.indexOf(b) + 99) % 99,
  );
  const cols = Math.min(3, Math.max(1, entries.length));

  return (
    <section className={"lh-card" + (bare ? " lh-bare" : "")} aria-label="Live harvest progress">
      <style>{CSS}</style>
      {showSummary && (
        <div className="lh-sum">
          <div className="lh-title">
            {title || (live ? "Live harvest" : "Harvest progress")}
            {live && <span className="lh-live"><i />LIVE</span>}
          </div>
          <div className="lh-kv"><b>{savedCount.toLocaleString()}</b><span>Jobs saved</span></div>
          <div className="lh-kv"><b>{unlimited ? "∞" : capCount.toLocaleString()}</b><span>Daily cap</span></div>
          <div className="lh-kv"><b>{unlimited ? "∞" : remainingCount.toLocaleString()}</b><span>Remaining</span></div>
        </div>
      )}
      {entries.length > 0 ? (
        <div className={"lh-lanes c" + cols}>
          {entries.map(([key, entry]) => (hasProgress(entry)
            ? <ProgressLane key={key} srcKey={key} entry={entry} windowHours={windowHours} />
            : <CompactLane key={key} srcKey={key} entry={entry} windowHours={windowHours} />))}
        </div>
      ) : (
        live && <div className="lh-empty">Waiting for the first source update…</div>
      )}
    </section>
  );
}

/** Realistic sample `sources` for the Rule Engine's PREVIEW_PROG dev flag. */
export const LIVE_HARVEST_PREVIEW = {
  linkedin: {
    state: "running", count: 70, message: "Job 7 of 25 on this page · Senior Data Engineer",
    reason: "", error: "", updated_at: "",
    phase: "details", reported_total: 200, listed: 200, skipped: 38, target: 162, cap: 500,
    done: 77, in_batch: 7, batch_size: 10, batches: 7, page: 8, total_pages: null, window_hours: 24,
  },
  naukri: {
    state: "running", count: 40, message: "Page 4 · detail 4 of 20 fetched",
    reason: "", error: "", updated_at: "",
    phase: "details", reported_total: 1284, listed: 80, skipped: 9, target: 300, cap: 300,
    done: 44, in_batch: 4, batch_size: 10, batches: 4, page: 4, total_pages: 65, window_hours: 24,
  },
};

const CSS = `
.lh-card{${GLASS}border-radius:14px;overflow:hidden;container-type:inline-size;container-name:lh;}
.lh-card.lh-bare{background:none;border:0;box-shadow:none;-webkit-backdrop-filter:none;backdrop-filter:none;border-radius:0;margin-top:14px;}
.lh-sum{display:flex;align-items:center;gap:22px;flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid #E6E3F3;}
.lh-title{font-size:12px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#6B6785;display:flex;align-items:center;gap:8px;margin-right:auto;}
.lh-live{display:inline-flex;align-items:center;gap:5px;font-size:9px;font-weight:700;letter-spacing:.09em;color:#6D28D9;background:#EDE7FF;padding:2px 7px;border-radius:99px;}
.lh-live i{width:6px;height:6px;border-radius:99px;background:#7C5CFC;animation:lhPulse 1.8s ease-out infinite;}
@keyframes lhPulse{0%{box-shadow:0 0 0 0 rgba(124,92,252,.5)}70%{box-shadow:0 0 0 6px rgba(124,92,252,0)}100%{box-shadow:0 0 0 0 rgba(124,92,252,0)}}
.lh-kv{display:flex;align-items:baseline;gap:6px;}
.lh-kv b{font-size:18px;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:-.02em;color:#1E1B2E;}
.lh-kv span{font-size:11px;font-weight:600;color:#6B6785;text-transform:uppercase;letter-spacing:.05em;}
.lh-empty{padding:14px 16px;font-size:12.5px;color:#9A96B5;}

.lh-lanes{display:grid;grid-template-columns:1fr;}
.lh-lanes.c2{grid-template-columns:repeat(2,minmax(0,1fr));}
.lh-lanes.c3{grid-template-columns:repeat(3,minmax(0,1fr));}
.lh-lane{padding:14px 16px 16px;display:flex;flex-direction:column;gap:12px;min-width:0;}
.lh-lane + .lh-lane{border-left:1px solid #E6E3F3;}
.lh-lane.is-compact{gap:8px;}
@container lh (max-width: 720px){
  .lh-lanes.c2,.lh-lanes.c3{grid-template-columns:1fr;}
  .lh-lane + .lh-lane{border-left:0;border-top:1px solid #E6E3F3;}
}

.lh-lane-h{display:flex;align-items:flex-start;gap:10px;}
.lh-glyph{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;color:#fff;font-weight:800;font-size:13px;flex:none;}
.lh-lane-id{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px;}
.lh-lane-id b{font-size:14px;color:#1E1B2E;}
.lh-chips{display:flex;gap:6px;flex-wrap:wrap;}
.lh-chip{display:inline-flex;align-items:center;gap:4px;font-size:10.5px;font-weight:700;padding:1px 8px;border-radius:999px;border:1px solid #D9D4EE;color:#6B6785;background:#F8F7FD;}
.lh-chip.is-running{background:#FFFBEB;color:#B45309;border-color:#FDE68A;}
.lh-chip.is-done{background:#ECFDF5;color:#047857;border-color:#86EFAC;}
.lh-chip.is-failed{background:#FEF2F2;color:#B91C1C;border-color:#FCA5A5;}
.lh-chip.is-skipped{background:#F8FAFC;color:#64748B;border-color:#E2E8F0;}
.lh-dot{width:6px;height:6px;border-radius:99px;background:currentColor;}
.lh-found{text-align:right;flex:none;line-height:1.05;}
.lh-found b{display:block;font-size:26px;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:-.03em;color:#1E1B2E;}
.lh-found small{font-size:10px;font-weight:600;color:#9A96B5;text-transform:uppercase;letter-spacing:.05em;}

.lh-phases{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(3,1fr);gap:4px;}
.lh-phases li{font-size:11px;font-weight:600;color:#9A96B5;padding-top:6px;border-top:3px solid #ECEAF6;min-width:0;}
.lh-phases li.done{color:#6B6785;border-top-color:#7C5CFC;}
.lh-phases li.now{color:#6D28D9;border-top-color:#6D28D9;}
.lh-phases li small{display:block;font-weight:500;color:#9A96B5;font-size:10.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}

.lh-funnel{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border:1px solid #E6E3F3;border-radius:10px;overflow:hidden;background:rgba(255,255,255,.6);}
.lh-funnel div{padding:7px 9px;display:flex;flex-direction:column;gap:1px;min-width:0;}
.lh-funnel div + div{border-left:1px solid #E6E3F3;}
.lh-funnel b{font-size:16px;font-variant-numeric:tabular-nums;color:#1E1B2E;}
.lh-funnel span{font-size:10.5px;color:#6B6785;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.lh-funnel .is-saved b{color:#6D28D9;}

.lh-prog{display:flex;flex-direction:column;gap:5px;}
.lh-prog-row{display:flex;justify-content:space-between;font-size:11.5px;color:#6B6785;}
.lh-prog-row b{color:#1E1B2E;font-variant-numeric:tabular-nums;}
.lh-bar{height:6px;border-radius:99px;background:#ECEAF6;overflow:hidden;}
.lh-bar i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#7C5CFC,#6D28D9);transition:width .4s ease;}

.lh-batch{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
.lh-batch-l{font-size:11.5px;color:#6B6785;min-width:76px;}
.lh-batch-l b{color:#1E1B2E;}
.lh-segs{display:flex;gap:3px;flex:1;min-width:140px;}
.lh-segs i{flex:1;height:12px;border-radius:3px;background:#ECEAF6;transition:background .2s;}
.lh-segs i.on{background:#7C5CFC;}
.lh-batch-r{font-size:11.5px;color:#6B6785;font-variant-numeric:tabular-nums;}
.lh-err{font-size:12px;font-weight:600;color:#B91C1C;background:#FEF2F2;border:1px solid #FCA5A5;border-radius:9px;padding:7px 10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.lh-msg{font-size:12px;color:#6B6785;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-top:1px dashed #E6E3F3;padding-top:8px;}
.lh-lane.is-compact .lh-msg{border-top:0;padding-top:0;}

@media (prefers-reduced-motion: reduce){.lh-live i{animation:none;}.lh-bar i,.lh-segs i{transition:none;}}
${GLASS_FALLBACK(".lh-card:not(.lh-bare)")}
`;
