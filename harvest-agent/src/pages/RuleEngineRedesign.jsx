import React, { useEffect, useMemo, useRef, useState } from "react";
import { Play, Save, Loader2, Clock, Info, CheckCircle2 } from "lucide-react";
import {
  getHarvestConfig, saveHarvestConfig, runHarvestAgent, getHarvestStatus, getActiveRun,
} from "../api";

/*
 * Redesigned Rule Engine — feature-flagged to tenants whose slip sets
 * features.ruleEngineRedesign (client_us today); internal keeps the classic
 * RuleEngineConfig. Same backend contract: loads the full HarvestConfig via
 * GET /harvest-config, edits only the fields it owns, and PUTs the merged
 * object back (mirrors RuleEngineConfig's merge-on-save so untouched fields
 * survive). Run Now = POST /run-harvest-agent + GET /harvest-status polling.
 *
 * Design per the approved mockup
 * (https://claude.ai/code/artifact/5f2e8f21-13eb-4a5f-b266-229000ed8ccc):
 * source priority rows with spring toggles, schedule summary banner, sliding
 * tab indicator, segmented frequency control, animated run-progress overlay.
 */

const SOURCES = [
  { key: "linkedin", name: "LinkedIn Jobs", glyph: "in", tint: "#2563EB", priority: 1 },
  { key: "naukri", name: "Naukri.com", glyph: "N", tint: "#6D28D9", priority: 2 },
  { key: "dice", name: "Dice.com", glyph: "⬢", tint: "#0EA5A4", priority: 3 },
];
const FREQUENCIES = ["hourly", "daily", "weekly"];
const TIMEZONES = [
  { value: "Asia/Kolkata", label: "IST (UTC+5:30)" },
  { value: "America/Los_Angeles", label: "PST (UTC−8)" },
  { value: "America/New_York", label: "EST (UTC−5)" },
  { value: "UTC", label: "UTC" },
];
const WINDOWS = [
  { value: 24, label: "Last 24 hours" },
  { value: 72, label: "Last 3 days" },
  { value: 168, label: "Last 7 days" },
];
const WORK_MODES = ["Any", "Remote", "Hybrid", "On-site"];

function Toggle({ on, onChange }) {
  return (
    <button
      type="button" role="switch" aria-checked={on}
      className={"rr-toggle" + (on ? " on" : "")}
      onClick={() => onChange(!on)}
    >
      <span className="rr-knob" />
    </button>
  );
}

export default function RuleEngineRedesign() {
  const [config, setConfig] = useState(null);      // full HarvestConfig (merge target)
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  // Editable subset (initialised from the loaded config)
  const [sources, setSources] = useState({ linkedin: true, naukri: false, dice: false });
  const [frequency, setFrequency] = useState("daily");
  const [runTime, setRunTime] = useState("16:02");
  const [timezone, setTimezone] = useState("Asia/Kolkata");
  const [keyword, setKeyword] = useState("");
  const [workMode, setWorkMode] = useState("Any");
  const [searchWindow, setSearchWindow] = useState(24);

  // Run Now overlay state
  const [running, setRunning] = useState(false);
  const [runMsg, setRunMsg] = useState("");
  const [runProgress, setRunProgress] = useState(0);
  const [runDone, setRunDone] = useState(false);
  const pollRef = useRef(null);

  // Tabs with sliding indicator
  const [tab, setTab] = useState("sources");
  const tabsRef = useRef(null);
  const [ind, setInd] = useState({ left: 0, width: 0 });
  useEffect(() => {
    const el = tabsRef.current?.querySelector(`[data-tab="${tab}"]`);
    if (el) setInd({ left: el.offsetLeft, width: el.offsetWidth });
  }, [tab, loading]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const cfg = await getHarvestConfig();
        if (cancelled) return;
        setConfig(cfg);
        setSources({
          linkedin: !!cfg.sources?.linkedin,
          naukri: !!cfg.sources?.naukri,
          dice: !!cfg.sources?.dice,
        });
        setFrequency(cfg.schedule?.frequency || "daily");
        setRunTime(cfg.schedule?.run_time || "09:00");
        setTimezone(cfg.schedule?.timezone || "Asia/Kolkata");
        setKeyword(cfg.filters?.keyword || "");
        setWorkMode(cfg.filters?.work_mode || "Any");
        setSearchWindow(cfg.filters?.search_window_hours || 24);
        setLoading(false);
      } catch {
        if (!cancelled) { setLoadError("Could not load the harvest configuration."); setLoading(false); }
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Resume the progress overlay if a harvest is already in flight (parity with
  // the classic page's /active-run resume-on-mount).
  useEffect(() => {
    (async () => {
      try {
        const res = await getActiveRun();
        if (res?.active && res?.job_id) startPolling(res.job_id);
      } catch { /* status is best-effort */ }
    })();
    return () => clearInterval(pollRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const activeCount = Object.values(sources).filter(Boolean).length;
  const tzLabel = TIMEZONES.find((t) => t.value === timezone)?.label || timezone;

  const markDirty = (setter) => (v) => { setter(v); setDirty(true); };

  async function handleSave() {
    if (!config || saving) return;
    setSaving(true);
    const merged = {
      ...config,
      sources: { ...config.sources, ...sources },
      filters: {
        ...config.filters,
        keyword: keyword.trim(),
        work_mode: workMode,
        search_window_hours: Number(searchWindow),
      },
      schedule: { ...config.schedule, frequency, run_time: runTime, timezone },
    };
    try {
      await saveHarvestConfig(merged);
      setConfig(merged);
      setDirty(false);
    } catch { /* keep dirty so the user can retry */ }
    setSaving(false);
  }

  function startPolling(jobId) {
    clearInterval(pollRef.current);
    setRunning(true); setRunDone(false); setRunProgress(5); setRunMsg("Starting harvest…");
    pollRef.current = setInterval(async () => {
      try {
        const st = await getHarvestStatus(jobId);
        setRunProgress(st.progress ?? 0);
        setRunMsg(st.message || st.status || "Running…");
        if (["completed", "failed", "cancelled"].includes(st.status)) {
          clearInterval(pollRef.current);
          setRunProgress(100);
          setRunDone(true);
        }
      } catch { /* transient poll errors are fine */ }
    }, 4000);
  }

  async function handleRun() {
    if (running) return;
    if (dirty) await handleSave(); // run with what's on screen
    try {
      const res = await runHarvestAgent();
      if (res?.job_id) startPolling(res.job_id);
    } catch {
      setRunMsg("Could not start the harvest — is one already running?");
      setRunning(true); setRunDone(true);
    }
  }

  const sourceStats = useMemo(() => ({ linkedin: "—", naukri: "—", dice: "—" }), []);

  if (loading) {
    return (
      <main className="ha-main rr-root"><style>{CSS}</style>
        <div className="rr-load"><Loader2 className="rr-spin" size={18} /> Loading configuration…</div>
      </main>
    );
  }

  return (
    <main className="ha-main rr-root">
      <style>{CSS}</style>

      {/* ── Header ── */}
      <div className="rr-head">
        <div>
          <h1>Rule Engine <span>· Configuration</span></h1>
          <div className="rr-status">
            <span className="rr-ready"><span className="rr-dot" /> Ready</span>
            <span className="rr-sep" />
            <span>Runs <b>{frequency}</b> at <b>{runTime}</b> · {tzLabel}</span>
          </div>
        </div>
        <div className="rr-actions">
          <button className="rr-btn rr-run" onClick={handleRun} disabled={running && !runDone}>
            <Play size={15} /> Run Now
          </button>
          <button className={"rr-btn rr-save" + (dirty ? "" : " is-saved")} onClick={handleSave} disabled={saving}>
            {saving ? <Loader2 className="rr-spin" size={15} /> : <Save size={15} />}
            {dirty ? "Save changes" : "Saved"}
          </button>
        </div>
      </div>

      {loadError && <div className="rr-err">{loadError}</div>}

      {/* ── Tabs ── */}
      <div className="rr-tabs" ref={tabsRef}>
        <button data-tab="sources" className={"rr-tab" + (tab === "sources" ? " active" : "")} onClick={() => setTab("sources")}>Sources & Schedule</button>
        <button data-tab="search" className={"rr-tab" + (tab === "search" ? " active" : "")} onClick={() => setTab("search")}>Search Rules</button>
        <span className="rr-ind" style={{ left: ind.left, width: ind.width }} />
      </div>

      {tab === "sources" && (
        <div className="rr-grid">
          {/* Sources card */}
          <section className="rr-card rr-rise">
            <div className="rr-card-h"><span>Job Sources <i className="rr-req">*</i></span><small>{activeCount} of {SOURCES.length} active</small></div>
            {SOURCES.map((s) => {
              const on = sources[s.key];
              return (
                <div key={s.key} className={"rr-src" + (on ? " on" : "")}>
                  <span className="rr-src-ic" style={{ background: s.tint }}>{s.glyph}</span>
                  <span className="rr-src-tx">
                    <b>{s.name}</b>
                    <small>
                      <span className="rr-prio">Priority {s.priority}</span>
                      <span className={"rr-chip " + (on ? "rr-chip-on" : "rr-chip-off")}>{on ? "Active" : "Paused"}</span>
                    </small>
                  </span>
                  <span className="rr-src-stat"><b>{sourceStats[s.key]}</b>last run</span>
                  <Toggle on={on} onChange={(v) => { setSources((p) => ({ ...p, [s.key]: v })); setDirty(true); }} />
                </div>
              );
            })}
            {activeCount === 0 && <div className="rr-warn">Select at least one source before running.</div>}
          </section>

          {/* Schedule card */}
          <section className="rr-card rr-rise" style={{ animationDelay: ".08s" }}>
            <div className="rr-card-h"><span>Run Schedule</span></div>
            <div className="rr-banner">
              <span className="rr-banner-ic"><Clock size={17} /></span>
              <span>Runs <b>{frequency} at {runTime}</b> ({tzLabel}) · looks back <b>{WINDOWS.find((w) => w.value === Number(searchWindow))?.label.toLowerCase() || `${searchWindow}h`}</b></span>
            </div>
            <div className="rr-fgrid">
              <label className="rr-fld rr-full">
                <span>Frequency</span>
                <span className="rr-seg">
                  {FREQUENCIES.map((f) => (
                    <button key={f} className={frequency === f ? "on" : ""} onClick={() => markDirty(setFrequency)(f)}>
                      {f[0].toUpperCase() + f.slice(1)}
                    </button>
                  ))}
                </span>
              </label>
              <label className="rr-fld"><span>Run time</span>
                <input className="rr-inp" type="time" value={runTime} onChange={(e) => markDirty(setRunTime)(e.target.value)} />
              </label>
              <label className="rr-fld"><span>Timezone</span>
                <select className="rr-inp" value={timezone} onChange={(e) => markDirty(setTimezone)(e.target.value)}>
                  {TIMEZONES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                </select>
              </label>
            </div>
          </section>
        </div>
      )}

      {tab === "search" && (
        <div className="rr-grid">
          <section className="rr-card rr-rise">
            <div className="rr-card-h"><span>Search Rules</span></div>
            <div className="rr-fgrid">
              <label className="rr-fld"><span>Keyword</span>
                <input className="rr-inp" placeholder="e.g. AI Engineer" value={keyword} onChange={(e) => markDirty(setKeyword)(e.target.value)} />
              </label>
              <label className="rr-fld"><span>Work mode</span>
                <select className="rr-inp" value={workMode} onChange={(e) => markDirty(setWorkMode)(e.target.value)}>
                  {WORK_MODES.map((m) => <option key={m}>{m}</option>)}
                </select>
              </label>
              <label className="rr-fld rr-full"><span>Search window (how far back to look)</span>
                <select className="rr-inp" value={searchWindow} onChange={(e) => markDirty(setSearchWindow)(e.target.value)}>
                  {WINDOWS.map((w) => <option key={w.value} value={w.value}>{w.label}</option>)}
                </select>
              </label>
            </div>
            <div className="rr-info">
              <Info size={14} />
              <span>Rule stored in <code>harvest_config.search_window_hours</code>. The agent skips any posting older than now − N hours.</span>
            </div>
          </section>
        </div>
      )}

      {/* ── Run overlay ── */}
      {running && (
        <div className="rr-overlay" onClick={(e) => { if (e.target === e.currentTarget && runDone) setRunning(false); }}>
          <div className="rr-runcard">
            <div className="rr-run-top">
              <div className="rr-run-t">
                {runDone
                  ? <><CheckCircle2 size={18} className="rr-done" /> Harvest {runProgress >= 100 ? "complete" : "finished"}</>
                  : <><span className="rr-spinner" /> Running harvest…</>}
              </div>
              <div className="rr-run-s">{runMsg}</div>
            </div>
            <div className="rr-run-body">
              <div className="rr-prog-lab"><span>Progress</span><b>{Math.min(100, Math.round(runProgress))}%</b></div>
              <div className="rr-bar"><i style={{ width: `${Math.min(100, runProgress)}%` }} /></div>
            </div>
            <div className="rr-run-foot">
              <button className="rr-btn" onClick={() => setRunning(false)} disabled={!runDone && runProgress < 100}>
                {runDone ? "Close" : "Running…"}
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

const CSS = `
.rr-root{flex:1;min-width:0;padding:26px 30px;background:#F8FAFC;color:#1E293B;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;min-height:100vh;}
.rr-load{display:flex;align-items:center;gap:9px;color:#64748B;font-size:14px;padding:40px 0;}
.rr-spin{animation:rrRot .8s linear infinite;}
.rr-err{background:#FEF2F2;border:1px solid #FCA5A5;color:#B91C1C;border-radius:10px;padding:10px 14px;font-size:13px;margin:14px 0;}

.rr-head{display:flex;align-items:flex-start;gap:18px;flex-wrap:wrap;}
.rr-head h1{margin:0;font-size:25px;font-weight:800;letter-spacing:-.02em;}
.rr-head h1 span{color:#64748B;font-weight:600;}
.rr-status{display:flex;align-items:center;gap:14px;margin-top:9px;flex-wrap:wrap;font-size:12.5px;color:#64748B;}
.rr-status b{color:#1E293B;}
.rr-ready{display:inline-flex;align-items:center;gap:6px;color:#047857;font-weight:700;background:#ECFDF5;border:1px solid #86EFAC;padding:3px 10px;border-radius:999px;}
.rr-dot{width:7px;height:7px;border-radius:50%;background:#16A34A;}
.rr-sep{width:4px;height:4px;border-radius:50%;background:#94A3B8;}
.rr-actions{margin-left:auto;display:flex;gap:10px;}
.rr-btn{display:inline-flex;align-items:center;gap:8px;border-radius:9px;padding:10px 17px;font-size:14px;font-weight:600;font-family:inherit;cursor:pointer;transition:.16s;border:1px solid #E2E8F0;background:#fff;color:#1E293B;}
.rr-btn:disabled{opacity:.6;cursor:default;}
.rr-run{border:0;background:#16A34A;color:#fff;box-shadow:0 2px 8px rgba(22,163,74,.35);}
.rr-run:hover:not(:disabled){background:#15803D;transform:translateY(-1px);}
.rr-save{border:0;background:#2563EB;color:#fff;box-shadow:0 2px 8px rgba(37,99,235,.30);}
.rr-save.is-saved{background:#fff;color:#2563EB;border:1px solid #2563EB;box-shadow:none;}

.rr-tabs{position:relative;display:flex;gap:26px;margin:22px 0 0;border-bottom:1px solid #E2E8F0;}
.rr-tab{position:relative;padding:12px 2px;font-size:14.5px;font-weight:600;color:#64748B;cursor:pointer;background:none;border:0;font-family:inherit;transition:color .18s;}
.rr-tab:hover{color:#1E293B;}
.rr-tab.active{color:#2563EB;}
.rr-ind{position:absolute;bottom:-1px;height:2.5px;border-radius:2px;background:#2563EB;transition:left .28s cubic-bezier(.4,0,.2,1), width .28s cubic-bezier(.4,0,.2,1);}

.rr-grid{display:grid;grid-template-columns:1.05fr 1fr;gap:20px;align-items:start;padding-top:22px;}
@media(max-width:900px){.rr-grid{grid-template-columns:1fr;}}
.rr-card{background:#fff;border:1px solid #E2E8F0;border-radius:14px;box-shadow:0 1px 2px rgba(15,23,42,.05);padding:20px;}
.rr-rise{opacity:0;transform:translateY(12px);animation:rrRise .5s cubic-bezier(.2,.7,.3,1) forwards;}
.rr-card-h{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;}
.rr-card-h span{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#64748B;}
.rr-card-h small{font-size:11.5px;color:#94A3B8;}
.rr-req{color:#EF4444;font-style:normal;}
.rr-warn{margin-top:6px;font-size:12.5px;color:#B45309;background:#FFFBEB;border:1px solid #FDE68A;border-radius:8px;padding:8px 12px;}

.rr-src{display:flex;align-items:center;gap:13px;padding:13px;border:1px solid #E2E8F0;border-radius:11px;margin-bottom:10px;transition:.18s;background:#fff;}
.rr-src:hover{border-color:#CBD5E1;box-shadow:0 1px 2px rgba(15,23,42,.05);}
.rr-src.on{border-color:#86EFAC;background:linear-gradient(90deg,#ECFDF5,#fff 60%);}
.rr-src-ic{width:38px;height:38px;border-radius:10px;display:grid;place-items:center;color:#fff;font-weight:800;font-size:15px;flex:none;}
.rr-src-tx{display:flex;flex-direction:column;gap:3px;flex:1;min-width:0;}
.rr-src-tx b{font-size:14.5px;}
.rr-src-tx small{display:flex;align-items:center;gap:8px;}
.rr-prio{font-size:10.5px;font-weight:700;color:#64748B;background:#F1F5F9;border-radius:999px;padding:1px 8px;}
.rr-chip{font-size:11px;font-weight:700;padding:1px 9px;border-radius:999px;}
.rr-chip-on{background:#ECFDF5;color:#047857;}
.rr-chip-off{background:#F1F5F9;color:#64748B;}
.rr-src-stat{text-align:right;font-size:11.5px;color:#64748B;display:flex;flex-direction:column;}
.rr-src-stat b{font-size:15px;color:#1E293B;font-variant-numeric:tabular-nums;}

.rr-toggle{position:relative;width:44px;height:25px;border-radius:999px;background:#CBD5E1;cursor:pointer;transition:background .2s;flex:none;border:0;padding:0;}
.rr-toggle.on{background:#16A34A;}
.rr-knob{position:absolute;top:2.5px;left:2.5px;width:20px;height:20px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.3);transition:transform .22s cubic-bezier(.34,1.56,.64,1);}
.rr-toggle.on .rr-knob{transform:translateX(19px);}

.rr-banner{display:flex;align-items:center;gap:12px;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:12px;padding:13px 15px;margin-bottom:18px;font-size:13.5px;color:#1E40AF;}
.rr-banner b{font-weight:800;}
.rr-banner-ic{width:34px;height:34px;border-radius:10px;background:#fff;border:1px solid #BFDBFE;display:grid;place-items:center;color:#2563EB;flex:none;}
.rr-fgrid{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
@media(max-width:520px){.rr-fgrid{grid-template-columns:1fr;}}
.rr-fld{display:flex;flex-direction:column;gap:6px;}
.rr-full{grid-column:1/-1;}
.rr-fld>span{font-size:11.5px;font-weight:700;color:#64748B;}
.rr-inp{height:40px;border:1px solid #CBD5E1;border-radius:9px;padding:0 12px;font-size:13.5px;font-family:inherit;background:#fff;color:#1E293B;}
.rr-inp:focus{outline:0;border-color:#2563EB;box-shadow:0 0 0 3px rgba(37,99,235,.12);}
.rr-seg{display:flex;background:#F1F5F9;border-radius:9px;padding:3px;}
.rr-seg button{flex:1;border:0;background:none;font-family:inherit;font-size:13px;font-weight:600;color:#64748B;padding:8px;border-radius:7px;cursor:pointer;transition:.15s;}
.rr-seg button.on{background:#fff;color:#2563EB;box-shadow:0 1px 2px rgba(15,23,42,.08);}
.rr-info{display:flex;gap:9px;align-items:flex-start;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:10px;padding:11px 13px;font-size:12.5px;color:#1E40AF;margin-top:16px;}
.rr-info svg{flex:none;margin-top:1px;}
.rr-info code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#fff;border:1px solid #BFDBFE;border-radius:5px;padding:1px 6px;font-size:11.5px;}

.rr-overlay{position:fixed;inset:0;background:rgba(15,23,42,.45);backdrop-filter:blur(3px);z-index:60;display:flex;align-items:center;justify-content:center;padding:20px;animation:rrFade .2s;}
.rr-runcard{width:min(520px,100%);background:#fff;border-radius:16px;box-shadow:0 24px 48px rgba(15,23,42,.25);overflow:hidden;animation:rrPop .3s cubic-bezier(.34,1.4,.5,1);}
.rr-run-top{background:linear-gradient(120deg,#F6F3FF,#fff);padding:20px 22px;border-bottom:1px solid #DDD6FE;}
.rr-run-t{font-weight:800;font-size:17px;display:flex;align-items:center;gap:10px;}
.rr-done{color:#16A34A;}
.rr-spinner{width:18px;height:18px;border-radius:50%;border:2.5px solid #DDD6FE;border-top-color:#6D28D9;animation:rrRot .8s linear infinite;display:inline-block;}
.rr-run-s{font-size:12.5px;color:#64748B;margin-top:4px;}
.rr-run-body{padding:20px 22px;}
.rr-prog-lab{display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px;}
.rr-prog-lab b{font-variant-numeric:tabular-nums;}
.rr-bar{height:8px;border-radius:999px;background:#F1F5F9;overflow:hidden;}
.rr-bar i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#6D28D9,#7C5CFC);transition:width .9s cubic-bezier(.3,.8,.3,1);}
.rr-run-foot{padding:16px 22px;border-top:1px solid #E2E8F0;display:flex;justify-content:flex-end;}

@keyframes rrRot{to{transform:rotate(360deg);}}
@keyframes rrRise{to{opacity:1;transform:none;}}
@keyframes rrFade{from{opacity:0;}}
@keyframes rrPop{from{transform:scale(.94);opacity:.6;}}
@media (prefers-reduced-motion: reduce){.rr-rise{animation:none;opacity:1;transform:none;}}
`;
