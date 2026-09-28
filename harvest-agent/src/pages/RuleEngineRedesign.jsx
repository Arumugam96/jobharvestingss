import React, { useEffect, useRef, useState } from "react";
import {
  Play, Save, Loader2, Clock, Info, CheckCircle2, AlertTriangle, Eye,
  Target, Database, Hourglass, Check,
} from "lucide-react";
import {
  getHarvestConfig, saveHarvestConfig, runHarvestAgent, getHarvestStatus, getActiveRun,
  getRunHistory, getRunHistoryEntry, setupLinkedinSession, setupNaukriSession, ApiError,
} from "../api";
import LiveBrowserView from "../components/LiveBrowserView";
import StopHarvestButton from "../components/StopHarvestModal";
import useCountUp from "../useCountUp";
import { makeStallWatch, STALL_WARN_MSG } from "../stallWatch";
import { useHarvestData } from "../HarvestDataContext";
import { GLASS, GLASS_FALLBACK, GLASS_INPUT, GLASS_INPUT_FOCUS, GLASS_MODAL } from "../theme";
import {
  JOB_TYPES, WORK_MODES, DOMAINS, HIRING_ENTITIES, GCC_MODES, SEARCH_WINDOWS,
  LINKEDIN_ACCOUNTS, TIMEZONES, CURRENCIES, normalizeGccMode, gccLocked,
  withSavedOption, fmtRunDate, nowLabel,
} from "../lib/ruleEngineOptions";

/*
 * Redesigned Rule Engine — feature-flagged to tenants whose slip sets
 * features.ruleEngineRedesign (client_us today); internal/client_in keep the
 * classic RuleEngineConfig. Same backend contract: loads the full
 * HarvestConfig via GET /harvest-config, edits the fields it owns, and PUTs
 * the merged object back so untouched fields (browser, notifications,
 * filters.verification, filters.max_jobs) survive.
 *
 * Full parity with the classic page (design per the approved mockup,
 * https://claude.ai/code/artifact/4a1c1b8e-1fe2-4969-869e-04202c77437f):
 * live header status + last saved/run, live progress cards with count-up,
 * stall watchdog, Stop Harvest, Watch Live Browser, source validation that
 * blocks Save/Run, all filters (job type, domain, hiring entity, GCC,
 * location, salary), LinkedIn/Naukri account connect, schedule.enabled
 * toggle, and the shared option lists from lib/ruleEngineOptions (which also
 * fixes the old "On-site" → 422 save bug; the backend value is "Onsite").
 * Laid out like the classic page: EVERYTHING (sources, schedule, accounts,
 * all filter cards) lives on the "Sources & Schedule" view, with the same
 * "Filters"/"Verification" placeholder tabs.
 */

const SOURCES = [
  { key: "linkedin", name: "LinkedIn Jobs", glyph: "in", tint: "#2563EB", priority: 1 },
  { key: "naukri", name: "Naukri.com", glyph: "N", tint: "#6D28D9", priority: 2 },
  { key: "dice", name: "Dice.com", glyph: "⬢", tint: "#0EA5A4", priority: 3 },
];
const FREQUENCIES = ["hourly", "daily", "weekly"];

function Toggle({ on, onChange, label }) {
  return (
    <button
      type="button" role="switch" aria-checked={on} aria-label={label}
      className={"rr-toggle" + (on ? " on" : "")}
      onClick={() => onChange(!on)}
    >
      <span className="rr-knob" />
    </button>
  );
}

function Chip({ active, onClick, children, disabled, title }) {
  return (
    <button type="button" className={"rr-fchip" + (active ? " on" : "")}
      aria-pressed={active} onClick={onClick} disabled={disabled} title={title}>
      {children}
    </button>
  );
}

export default function RuleEngineRedesign() {
  const { refreshAll, harvestRunning, setHarvestRunning } = useHarvestData();

  const [config, setConfig] = useState(null);      // full HarvestConfig (merge target)
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [attempted, setAttempted] = useState(false);

  // Editable subset (initialised from the loaded config). `sources` holds ONLY
  // the three toggles — the selected LinkedIn account lives apart so its truthy
  // string can't satisfy the "at least one source" check.
  const [sources, setSources] = useState({ linkedin: true, naukri: false, dice: false });
  const [linkedinAccount, setLinkedinAccount] = useState("1");
  const [frequency, setFrequency] = useState("daily");
  const [runTime, setRunTime] = useState("09:00");
  const [timezone, setTimezone] = useState("America/New_York");
  const [scheduleEnabled, setScheduleEnabled] = useState(false);
  const [keyword, setKeyword] = useState("");
  const [location, setLocation] = useState("");
  const [jobType, setJobType] = useState("Any");
  const [workMode, setWorkMode] = useState("Any");
  const [searchWindow, setSearchWindow] = useState(24);
  const [domain, setDomain] = useState("Any");
  const [hiringEntity, setHiringEntity] = useState("Any");
  const [gccMode, setGccMode] = useState("include_gcc");
  const [salaryMin, setSalaryMin] = useState("");
  const [salaryMax, setSalaryMax] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [includeUndisclosed, setIncludeUndisclosed] = useState(true);

  // Run/status state (mirrors the classic page)
  const [lastSaved, setLastSaved] = useState("—");
  const [lastRun, setLastRun] = useState("—");
  const [harvested, setHarvested] = useState(0);
  const [harvestedLive, setHarvestedLive] = useState(0);
  const [maxPerDay, setMaxPerDay] = useState(0); // MAX_JOBS_PER_DAY (.env); 0 = unlimited
  const [runState, setRunState] = useState("idle"); // idle | running | success | failed
  const [runMessage, setRunMessage] = useState("");
  const [runProgress, setRunProgress] = useState(0);
  const [overlayOpen, setOverlayOpen] = useState(false); // dismissible mid-run
  const [stallWarn, setStallWarn] = useState("");
  const pollTimer = useRef(null);
  const stallWatch = useRef(makeStallWatch());
  useEffect(() => () => clearTimeout(pollTimer.current), []);

  // Connect-accounts state, keyed by LinkedIn account id ("1" | "2").
  const [linkedinSetup, setLinkedinSetup] = useState({
    "1": { loading: false, message: "" },
    "2": { loading: false, message: "" },
  });
  const [naukriSetup, setNaukriSetup] = useState({ loading: false, message: "" });
  const [liveViewSource, setLiveViewSource] = useState(null); // "linkedin" | "naukri" | "harvest" | null

  // Animated count-up for the live progress cards.
  const unlimited = !(maxPerDay > 0);
  const liveCount = useCountUp(harvestedLive);
  const maxCount = useCountUp(maxPerDay);
  const remainingCount = useCountUp(unlimited ? 0 : Math.max(0, maxPerDay - harvestedLive));

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
        const [cfg, history] = await Promise.all([
          getHarvestConfig(),
          getRunHistory().catch(() => ({ runs: [] })),
        ]);
        if (cancelled) return;
        applyConfig(cfg);
        const latest = history.runs && history.runs[0];
        if (latest) {
          setLastRun(fmtRunDate(latest.completed_at || latest.started_at));
          setHarvested(latest.jobs_found ?? 0);
        }
        setLoading(false);
      } catch (err) {
        if (!cancelled) {
          setLoadError(
            err instanceof ApiError
              ? `Could not load configuration: ${err.message}`
              : "Could not load the harvest configuration."
          );
          setLoading(false);
        }
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function applyConfig(cfg) {
    setConfig(cfg);
    setSources({
      linkedin: !!cfg.sources?.linkedin,
      naukri: !!cfg.sources?.naukri,
      dice: !!cfg.sources?.dice,
    });
    setLinkedinAccount(cfg.sources?.linkedin_account || "1");
    setFrequency(cfg.schedule?.frequency || "daily");
    setRunTime(cfg.schedule?.run_time || "09:00");
    // US-friendly fallback only when the saved config carries no timezone.
    setTimezone(cfg.schedule?.timezone || "America/New_York");
    setScheduleEnabled(!!cfg.schedule?.enabled);
    setKeyword(cfg.filters?.keyword || "");
    setLocation(cfg.filters?.location || "");
    setJobType(cfg.filters?.job_type || "Any");
    setWorkMode(cfg.filters?.work_mode || "Any");
    setSearchWindow(cfg.filters?.search_window_hours || 24);
    setDomain(cfg.filters?.domain || "Any");
    const loadedEntity = cfg.filters?.hiring_entity || "Any";
    setHiringEntity(loadedEntity);
    // A specific hiring entity determines GCC-ness — neutralize a stale
    // gcc_mode on load (the backend's _reconcile_gcc normalizes it too).
    setGccMode(normalizeGccMode(loadedEntity, cfg.filters?.gcc_mode));
    setSalaryMin(cfg.filters?.salary_min == null ? "" : String(cfg.filters.salary_min));
    setSalaryMax(cfg.filters?.salary_max == null ? "" : String(cfg.filters.salary_max));
    setCurrency(cfg.filters?.salary_currency || "USD");
    setIncludeUndisclosed(cfg.filters?.include_undisclosed_salary ?? true);
    setDirty(false);
  }

  const activeCount = Object.values(sources).filter(Boolean).length;
  const errors = { jobSource: activeCount === 0 };
  const hasErrors = Object.values(errors).some(Boolean);
  const showErr = (k) => attempted && errors[k];
  const markDirty = (setter) => (v) => { setter(v); setDirty(true); };

  const tzOptions = withSavedOption(TIMEZONES, timezone);
  const currencyOptions = withSavedOption(CURRENCIES, currency);
  const tzLabel = tzOptions.find((t) => t.value === timezone)?.label || timezone;
  const windowLabel = SEARCH_WINDOWS.find((w) => w.value === Number(searchWindow))?.label || `${searchWindow}h`;

  function buildPayload() {
    return {
      ...config, // preserves browser, notifications, and anything else unowned
      sources: { ...config.sources, ...sources, linkedin_account: linkedinAccount },
      filters: {
        ...config.filters, // preserves max_jobs + verification
        keyword: keyword.trim(),
        location: location.trim(),
        job_type: jobType,
        work_mode: workMode,
        search_window_hours: Number(searchWindow),
        domain,
        hiring_entity: hiringEntity,
        gcc_mode: gccMode,
        salary_min: salaryMin === "" ? null : Number(salaryMin),
        salary_max: salaryMax === "" ? null : Number(salaryMax),
        salary_currency: currency,
        include_undisclosed_salary: includeUndisclosed,
      },
      schedule: { ...config.schedule, frequency, run_time: runTime, timezone, enabled: scheduleEnabled },
    };
  }

  async function handleSave() {
    if (!config || saving) return;
    if (hasErrors) { setAttempted(true); setTab("sources"); return; }
    setSaving(true);
    setSaveError("");
    try {
      // Adopt the PUT response as the new merge target — the backend may
      // normalize fields (e.g. gcc_mode via _reconcile_gcc).
      const saved = await saveHarvestConfig(buildPayload());
      setConfig(saved);
      setLastSaved(nowLabel());
      setDirty(false);
      setAttempted(false);
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : "Could not save configuration — check the backend connection.");
    } finally {
      setSaving(false);
    }
  }

  // GET /harvest-status/{job_id} carries live progress — a self-rescheduling
  // tick (6s while running, 10s retry), NOT an interval, so a slow response
  // can't stack requests. Falls back to run-history if the tracker entry is
  // gone (server restarted mid-run).
  function pollHarvestStatus(jobId, runId) {
    const tick = async () => {
      try {
        const status = await getHarvestStatus(jobId);
        if (status.status === "running") {
          setRunMessage(status.message || "Running…");
          setRunProgress(status.progress ?? 0);
          const live = status.jobs_saved_today ?? status.combined ?? 0;
          setHarvestedLive(live);
          setMaxPerDay(status.max_jobs_per_day ?? 0);
          const { stalled } = stallWatch.current.note(`${status.message || ""}|${live}`);
          setStallWarn(stalled ? STALL_WARN_MSG : "");
          pollTimer.current = setTimeout(tick, 6000);
          return;
        }
        setStallWarn("");
        setRunProgress(100);
        if (status.status === "failed") {
          setRunState("failed");
          setRunMessage(status.error || status.message || `Harvest run ${runId} failed — check server logs.`);
          setHarvestRunning(false);
          return;
        }
        // success | no_results | stopped
        setRunState("success");
        setHarvested(status.combined ?? 0);
        setLastRun(fmtRunDate(status.completed_at));
        setHarvestRunning(false);
        refreshAll();
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          try {
            const entry = await getRunHistoryEntry(runId);
            setStallWarn("");
            setRunProgress(100);
            if (entry.status === "failed") {
              setRunState("failed");
              setRunMessage(entry.error || `Harvest run ${runId} failed — check server logs.`);
            } else {
              setRunState("success");
              setHarvested(entry.jobs_found ?? 0);
              setLastRun(fmtRunDate(entry.completed_at));
              refreshAll();
            }
          } catch {
            setRunMessage("Harvest running — no live status available yet; retrying…");
            pollTimer.current = setTimeout(tick, 10000);
            return;
          }
          setHarvestRunning(false);
          return;
        }
        setStallWarn("");
        setRunState("failed");
        setRunMessage(err instanceof ApiError ? err.message : "Lost connection while checking harvest status.");
        setHarvestRunning(false);
      }
    };
    tick();
  }

  // Adopt an already-in-flight run (scheduler / another tab) on mount.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await getActiveRun();
        if (!cancelled && res?.active && res.job_id) {
          setRunState("running");
          setRunMessage("Harvesting…");
          setHarvestedLive(0);
          setHarvestRunning(true);
          stallWatch.current.reset();
          setStallWarn("");
          pollHarvestStatus(res.job_id, res.run_id);
        }
      } catch { /* backend unreachable — HealthBadge surfaces the outage */ }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleRun() {
    if (runState === "running") { setOverlayOpen(true); return; }
    if (harvestRunning) {
      setRunState("failed");
      setRunMessage("Another harvest is already running (Source Runs page or a previous session) — wait for it to finish. Running two at once collides on the shared browser profile and both fail.");
      return;
    }
    if (hasErrors) { setAttempted(true); setTab("sources"); return; }
    if (dirty) {
      await handleSave();
      if (hasErrors) return;
    }
    setRunState("running");
    setHarvestRunning(true);
    setRunMessage("Starting harvest…");
    setRunProgress(0);
    setHarvestedLive(0);
    setOverlayOpen(true);
    stallWatch.current.reset();
    setStallWarn("");
    try {
      const res = await runHarvestAgent();
      if (res.status === "failed") {
        setRunState("failed");
        setRunMessage(res.reason || res.message || "Harvest could not be started.");
        setHarvestRunning(false);
        return;
      }
      pollHarvestStatus(res.job_id, res.run_id);
    } catch (err) {
      setRunState("failed");
      setRunMessage(err instanceof ApiError ? err.message : "Could not reach the harvest backend.");
      // 409 = a run is already in flight — keep the controls frozen.
      setHarvestRunning(err instanceof ApiError && err.status === 409);
    }
  }

  const setAcctStatus = (accountId, patch) =>
    setLinkedinSetup((s) => ({ ...s, [accountId]: { ...s[accountId], ...patch } }));

  const handleLinkedinSetup = async (accountId) => {
    setAcctStatus(accountId, { loading: true, message: "Log in below — this is the live browser. Waiting up to 10 minutes…" });
    setLiveViewSource("linkedin");
    try {
      const res = await setupLinkedinSession(accountId);
      setAcctStatus(accountId, { loading: false, message: res.status === "ready" ? (res.message || "LinkedIn session saved.") : (res.reason || res.message || "Could not confirm login.") });
    } catch (err) {
      setAcctStatus(accountId, { loading: false, message: err instanceof ApiError ? err.message : "Could not reach the harvest backend." });
    } finally {
      setLiveViewSource(null);
    }
  };

  const handleNaukriSetup = async () => {
    setNaukriSetup({ loading: true, message: "Log in below — this is the live browser. Waiting up to 10 minutes…" });
    setLiveViewSource("naukri");
    try {
      const res = await setupNaukriSession();
      setNaukriSetup({ loading: false, message: res.status === "ready" ? (res.message || "Naukri session saved.") : (res.reason || res.message || "Could not confirm login.") });
    } catch (err) {
      setNaukriSetup({ loading: false, message: err instanceof ApiError ? err.message : "Could not reach the harvest backend." });
    } finally {
      setLiveViewSource(null);
    }
  };

  // The backend pauses a running harvest waiting for a manual LinkedIn login —
  // pulse the Watch button so the user knows to open the live browser.
  const needsLogin = runState === "running" && /log ?in/i.test(runMessage || "");
  const gccIsLocked = gccLocked(hiringEntity);

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

      {liveViewSource && (
        <LiveBrowserView
          title={
            liveViewSource === "linkedin" ? "LinkedIn login — live browser" :
            liveViewSource === "naukri" ? "Naukri login — live browser" :
            "Harvest in progress — live browser"
          }
          onClose={() => setLiveViewSource(null)}
        />
      )}

      {/* ── Header ── */}
      <div className="rr-head">
        <div>
          <h1>Rule Engine <span>· Configuration</span></h1>
          <div className="rr-status">
            <span>Last saved: <b>{lastSaved}</b></span>
            <span className="rr-sep" />
            <span>Last run: <b>{lastRun}</b></span>
            <span className="rr-sep" />
            {runState === "running" ? (
              <button className="rr-stchip run" onClick={() => setOverlayOpen(true)} title="Show run progress">
                <Loader2 size={13} className="rr-spin" /> {runMessage || "Running…"}
                {harvestedLive > 0 && <> · <b>{harvestedLive}</b> saved</>}
              </button>
            ) : runState === "failed" ? (
              <span className="rr-stchip err"><AlertTriangle size={13} /> {runMessage || "Harvest failed"}</span>
            ) : (
              <span className="rr-stchip ok"><Check size={13} /> {runState === "success" ? `Success (${harvested} harvested)` : "Ready"}</span>
            )}
            {runState === "running" && stallWarn && (
              <span className="rr-stchip warn"><AlertTriangle size={13} /> {stallWarn}</span>
            )}
          </div>
        </div>
        <div className="rr-actions">
          {attempted && hasErrors && (
            <span className="rr-validation"><AlertTriangle size={14} /> Enable at least one job source</span>
          )}
          {saveError && <span className="rr-validation"><AlertTriangle size={14} /> {saveError}</span>}
          {(runState === "running" || harvestRunning) && (
            <button
              className={"rr-btn rr-watch" + (needsLogin ? " rr-watch-attn" : "")}
              onClick={() => setLiveViewSource("harvest")}
              title={needsLogin ? "LinkedIn needs you to log in — click to open the live browser" : undefined}
            >
              <Eye size={15} /> {needsLogin ? "Log in now — Watch Live Browser" : "Watch Live Browser"}
            </button>
          )}
          <StopHarvestButton
            harvestRunning={harvestRunning}
            className="rr-btn rr-stop"
            onStopped={() => setRunMessage("Stop requested — the run will halt shortly and save its jobs. The report email is deferred to the next successful run.")}
          />
          <button
            className="rr-btn rr-run"
            onClick={handleRun}
            disabled={loading || (harvestRunning && runState !== "running")}
            title={harvestRunning && runState !== "running" ? "A harvest is already running — controls locked until it finishes" : undefined}
          >
            {(runState === "running" || harvestRunning) ? <Loader2 className="rr-spin" size={15} /> : <Play size={15} />}
            {runState === "running" ? "Running" : harvestRunning ? "Running…" : "Run Now"}
          </button>
          <button className={"rr-btn rr-save" + (dirty ? "" : " is-saved")} onClick={handleSave} disabled={saving}>
            {saving ? <Loader2 className="rr-spin" size={15} /> : <Save size={15} />}
            {saving ? "Saving…" : dirty ? "Save changes" : "Saved"}
          </button>
        </div>
      </div>

      {loadError && <div className="rr-err">{loadError}</div>}

      {/* ── Live progress cards (Max / Saved / Remaining) ── */}
      {runState === "running" && (
        <div className="rr-prog">
          <div className="rr-card rr-prog-card">
            <div className="rr-prog-chip"><Target size={18} /></div>
            <div className="rr-prog-num">{unlimited ? "∞" : maxCount}</div>
            <div className="rr-prog-label">Max Jobs / Day</div>
            <div className="rr-prog-hint">daily harvest ceiling</div>
            <div className="rr-prog-bar" />
          </div>
          <div className="rr-card rr-prog-card">
            <span className="rr-prog-tag"><i /> Live</span>
            <div className="rr-prog-chip"><Database size={18} /></div>
            <div className="rr-prog-num">{liveCount}</div>
            <div className="rr-prog-label">Jobs Saved</div>
            <div className="rr-prog-hint">persisted to database</div>
            <div className="rr-prog-bar" />
          </div>
          <div className="rr-card rr-prog-card">
            <div className="rr-prog-chip"><Hourglass size={18} /></div>
            <div className="rr-prog-num">{unlimited ? "∞" : remainingCount}</div>
            <div className="rr-prog-label">Remaining Jobs</div>
            <div className="rr-prog-hint">Max − Saved</div>
            <div className="rr-prog-bar" />
          </div>
        </div>
      )}

      {/* ── Tabs ── */}
      <div className="rr-tabs" ref={tabsRef}>
        <button data-tab="sources" className={"rr-tab" + (tab === "sources" ? " active" : "")} onClick={() => setTab("sources")}>Sources & Schedule</button>
        <button data-tab="filters" className={"rr-tab" + (tab === "filters" ? " active" : "")} onClick={() => setTab("filters")}>Filters</button>
        <button data-tab="verification" className={"rr-tab" + (tab === "verification" ? " active" : "")} onClick={() => setTab("verification")}>Verification</button>
        <span className="rr-ind" style={{ left: ind.left, width: ind.width }} />
      </div>

      {tab === "sources" && (
        <div className="rr-grid">
          {/* Sources card */}
          <section className={"rr-card rr-rise" + (showErr("jobSource") ? " is-invalid" : "")}>
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
                  <Toggle on={on} label={s.name} onChange={(v) => { setSources((p) => ({ ...p, [s.key]: v })); setDirty(true); }} />
                </div>
              );
            })}
            {activeCount === 0 && (
              <div className="rr-warn">
                {attempted ? "Enable at least one job source — Save and Run Now are blocked until you do." : "Select at least one source before running."}
              </div>
            )}
          </section>

          {/* Schedule card */}
          <section className="rr-card rr-rise" style={{ animationDelay: ".08s" }}>
            <div className="rr-card-h"><span>Run Schedule</span></div>
            <div className="rr-banner">
              <span className="rr-banner-ic"><Clock size={17} /></span>
              <span>Runs <b>{frequency} at {runTime}</b> ({tzLabel}) · looks back <b>{windowLabel.toLowerCase()}</b></span>
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
                  {tzOptions.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                </select>
              </label>
            </div>
            <div className="rr-tglrow">
              <span className="rr-tglrow-tx">
                <b>Automatic scheduled runs</b>
                <small>Run the harvest automatically on the schedule above.</small>
              </span>
              <Toggle on={scheduleEnabled} label="Automatic scheduled runs" onChange={markDirty(setScheduleEnabled)} />
            </div>
          </section>
          {/* Connect accounts — same view as everything else, like the classic page */}
          <section className="rr-card rr-rise">
            <div className="rr-card-h"><span>Active LinkedIn Account</span></div>
            <p className="rr-desc">The harvester signs in with this account's saved session.</p>
            <select className="rr-inp" value={linkedinAccount} onChange={(e) => markDirty(setLinkedinAccount)(e.target.value)}>
              {LINKEDIN_ACCOUNTS.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
            </select>
            <div className="rr-info" style={{ marginTop: 14 }}>
              <Info size={14} />
              <span>Each account keeps its own saved session and Chrome profile on the server. Saved to <code>sources.linkedin_account</code>.</span>
            </div>
          </section>

          <section className="rr-card rr-rise" style={{ animationDelay: ".08s" }}>
            <div className="rr-card-h"><span>Connect Accounts</span></div>
            {LINKEDIN_ACCOUNTS.map((a) => {
              const st = linkedinSetup[a.id];
              return (
                <div className="rr-acct" key={a.id}>
                  <span className="rr-src-ic" style={{ background: "#2563EB" }}>in</span>
                  <span className="rr-acct-tx">
                    <b>LinkedIn — {a.label}</b>
                    <small>{st.message || "Open a live browser to log in; the session is saved for future runs."}</small>
                  </span>
                  <button className="rr-acct-btn" disabled={st.loading} onClick={() => handleLinkedinSetup(a.id)}>
                    {st.loading ? <Loader2 className="rr-spin" size={13} /> : null}
                    {st.loading ? "Waiting for login…" : "Connect via Live Browser"}
                  </button>
                </div>
              );
            })}
            <div className="rr-acct">
              <span className="rr-src-ic" style={{ background: "#6D28D9" }}>N</span>
              <span className="rr-acct-tx">
                <b>Naukri.com</b>
                <small>{naukriSetup.message || "Open a live browser to log in; the session is saved for future runs."}</small>
              </span>
              <button className="rr-acct-btn" disabled={naukriSetup.loading} onClick={handleNaukriSetup}>
                {naukriSetup.loading ? <Loader2 className="rr-spin" size={13} /> : null}
                {naukriSetup.loading ? "Waiting for login…" : "Connect via Live Browser"}
              </button>
            </div>
          </section>

          <section className="rr-card rr-rise rr-span">
            <div className="rr-card-h"><span>Search</span></div>
            <div className="rr-fgrid rr-fgrid--3">
              <label className="rr-fld"><span>Keyword</span>
                <input className="rr-inp" placeholder="e.g. AI Engineer" value={keyword} onChange={(e) => markDirty(setKeyword)(e.target.value)} />
              </label>
              <label className="rr-fld"><span>Work mode</span>
                <select className="rr-inp" value={workMode} onChange={(e) => markDirty(setWorkMode)(e.target.value)}>
                  {WORK_MODES.map((m) => <option key={m}>{m}</option>)}
                </select>
              </label>
              <label className="rr-fld"><span>Search window (how far back to look)</span>
                <select className="rr-inp" value={searchWindow} onChange={(e) => markDirty(setSearchWindow)(e.target.value)}>
                  {SEARCH_WINDOWS.map((w) => <option key={w.value} value={w.value}>{w.label}</option>)}
                </select>
              </label>
            </div>
            <div className="rr-info">
              <Info size={14} />
              <span>Rule stored in <code>harvest_config.search_window_hours</code>. The agent skips any posting older than now − N hours.</span>
            </div>
          </section>

          <section className="rr-card rr-rise">
            <div className="rr-card-h"><span>Job Type</span></div>
            <p className="rr-desc">Engagement type the posting offers.</p>
            <div className="rr-fchips">
              {JOB_TYPES.map((t) => (
                <Chip key={t} active={jobType === t} onClick={() => markDirty(setJobType)(t)}>{t}</Chip>
              ))}
            </div>
          </section>

          <section className="rr-card rr-rise" style={{ animationDelay: ".08s" }}>
            <div className="rr-card-h"><span>Hiring Entity</span></div>
            <p className="rr-desc">Who is doing the hiring — a specific entity locks the GCC flag below.</p>
            <div className="rr-fchips">
              {HIRING_ENTITIES.map((h) => (
                <Chip key={h} active={hiringEntity === h}
                  onClick={() => { setHiringEntity(h); setGccMode(normalizeGccMode(h, gccMode)); setDirty(true); }}>
                  {h}
                </Chip>
              ))}
            </div>
          </section>

          <section className="rr-card rr-rise rr-span">
            <div className="rr-card-h"><span>Domain</span></div>
            <p className="rr-desc">Keep only jobs the classifier files under this domain.</p>
            <div className="rr-fchips">
              {DOMAINS.map((d) => (
                <Chip key={d} active={domain === d} onClick={() => markDirty(setDomain)(d)}>{d}</Chip>
              ))}
            </div>
          </section>

          <section className="rr-card rr-rise">
            <div className="rr-card-h"><span>GCC Flag</span></div>
            <p className="rr-desc">
              {gccIsLocked
                ? `Determined by Hiring entity — "${hiringEntity}" already decides GCC handling.`
                : "Include, exclude, or only keep Global Capability Center postings."}
            </p>
            <div className="rr-fchips">
              {GCC_MODES.map((m) => (
                <Chip key={m.value} active={gccMode === m.value} disabled={gccIsLocked}
                  title={gccIsLocked ? "Set Hiring entity to \"Any\" to choose a GCC mode" : undefined}
                  onClick={() => markDirty(setGccMode)(m.value)}>
                  {m.label}
                </Chip>
              ))}
            </div>
          </section>

          <section className="rr-card rr-rise" style={{ animationDelay: ".08s" }}>
            <div className="rr-card-h"><span>Location</span></div>
            <p className="rr-desc">City, state, or "Remote". Leave blank for anywhere.</p>
            <input className="rr-inp" placeholder="e.g. New York, NY or Remote" value={location} onChange={(e) => markDirty(setLocation)(e.target.value)} />
          </section>

          <section className="rr-card rr-rise rr-span">
            <div className="rr-card-h"><span>Salary / Budget</span></div>
            <div className="rr-fgrid rr-fgrid--4">
              <label className="rr-fld"><span>Min (annual)</span>
                <input className="rr-inp" type="number" min="0" placeholder="e.g. 90000" value={salaryMin} onChange={(e) => markDirty(setSalaryMin)(e.target.value)} />
              </label>
              <label className="rr-fld"><span>Max (annual)</span>
                <input className="rr-inp" type="number" min="0" placeholder="No ceiling" value={salaryMax} onChange={(e) => markDirty(setSalaryMax)(e.target.value)} />
              </label>
              <label className="rr-fld"><span>Currency</span>
                <select className="rr-inp" value={currency} onChange={(e) => markDirty(setCurrency)(e.target.value)}>
                  {currencyOptions.map((c) => <option key={c}>{c}</option>)}
                </select>
              </label>
              <div className="rr-fld"><span>Undisclosed salaries</span>
                <div className="rr-tglrow rr-tglrow--bare">
                  <span className="rr-tglrow-tx"><small>Keep jobs that don't state a salary.</small></span>
                  <Toggle on={includeUndisclosed} label="Include undisclosed salaries" onChange={markDirty(setIncludeUndisclosed)} />
                </div>
              </div>
            </div>
          </section>
        </div>
      )}

      {/* Placeholder tabs — mirror the classic page's layout. */}
      {tab === "filters" && (
        <div className="rr-place rr-rise">
          <h3>Post-collection filters</h3>
          <p>Dedupe, keyword blocklists and scoring rules will land here. Today's harvest filters live under Sources &amp; Schedule.</p>
        </div>
      )}

      {tab === "verification" && (
        <div className="rr-place rr-rise">
          <h3>Verification</h3>
          <p>Contact-validation steps run automatically after each harvest. Their configuration will land here.</p>
        </div>
      )}

      {/* ── Run overlay — dismissible mid-run; header/progress stay live ── */}
      {overlayOpen && (
        <div className="rr-overlay" onClick={(e) => { if (e.target === e.currentTarget) setOverlayOpen(false); }}>
          <div className="rr-runcard">
            <div className="rr-run-top">
              <div className="rr-run-t">
                {runState === "running"
                  ? <><span className="rr-spinner" /> Running harvest…</>
                  : runState === "failed"
                    ? <><AlertTriangle size={18} className="rr-fail" /> Harvest failed</>
                    : <><CheckCircle2 size={18} className="rr-done" /> Harvest complete</>}
              </div>
              <div className="rr-run-s">{runMessage}</div>
            </div>
            <div className="rr-run-body">
              <div className="rr-prog-lab"><span>Progress</span><b>{Math.min(100, Math.round(runProgress))}%</b></div>
              <div className="rr-bar"><i style={{ width: `${Math.min(100, runProgress)}%` }} /></div>
              <div className="rr-run-live">
                Jobs saved so far: <b>{harvestedLive}</b>{!unlimited && <> of <b>{maxPerDay}</b> daily cap</>}
              </div>
            </div>
            <div className="rr-run-foot">
              {runState === "running" && (
                <button className="rr-btn rr-watch" onClick={() => setLiveViewSource("harvest")}>
                  <Eye size={15} /> Watch Live Browser
                </button>
              )}
              <StopHarvestButton
                harvestRunning={harvestRunning}
                className="rr-btn rr-stop"
                onStopped={() => setRunMessage("Stop requested — the run will halt shortly and save its jobs.")}
              />
              <button className="rr-btn" onClick={() => setOverlayOpen(false)}
                title={runState === "running" ? "The run keeps going — live status stays in the header" : undefined}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

const CSS = `
.rr-root{flex:1;min-width:0;padding:26px 30px;background:transparent;color:#1E293B;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;min-height:100vh;}
.rr-load{display:flex;align-items:center;gap:9px;color:#64748B;font-size:14px;padding:40px 0;}
.rr-spin{animation:rrRot .8s linear infinite;}
.rr-err{background:#FEF2F2;border:1px solid #FCA5A5;color:#B91C1C;border-radius:10px;padding:10px 14px;font-size:13px;margin:14px 0;}

.rr-head{display:flex;align-items:flex-start;gap:18px;flex-wrap:wrap;}
.rr-head h1{margin:0;font-size:25px;font-weight:800;letter-spacing:-.02em;}
.rr-head h1 span{color:#64748B;font-weight:600;}
.rr-status{display:flex;align-items:center;gap:12px;margin-top:9px;flex-wrap:wrap;font-size:12.5px;color:#64748B;}
.rr-status b{color:#1E293B;}
.rr-sep{width:4px;height:4px;border-radius:50%;background:#94A3B8;flex:none;}
.rr-stchip{display:inline-flex;align-items:center;gap:6px;font-weight:700;padding:3px 10px;border-radius:999px;border:1px solid transparent;font-size:12.5px;font-family:inherit;}
.rr-stchip.ok{color:#047857;background:#ECFDF5;border-color:#86EFAC;}
.rr-stchip.run{color:#92400E;background:#FFFBEB;border-color:#FDE68A;cursor:pointer;max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.rr-stchip.err{color:#B91C1C;background:#FEF2F2;border-color:#FCA5A5;max-width:460px;}
.rr-stchip.warn{color:#B45309;background:#FFFBEB;border-color:#FDE68A;}
.rr-validation{display:inline-flex;align-items:center;gap:5px;align-self:center;font-size:12.5px;font-weight:600;color:#DC2626;margin-right:4px;max-width:340px;}
.rr-actions{margin-left:auto;display:flex;gap:10px;flex-wrap:wrap;align-items:center;}
.rr-btn{display:inline-flex;align-items:center;gap:8px;border-radius:9px;padding:10px 17px;font-size:14px;font-weight:600;font-family:inherit;cursor:pointer;transition:.16s;border:1px solid #E2E8F0;background:rgba(255,255,255,.85);color:#1E293B;}
.rr-btn:disabled{opacity:.6;cursor:default;}
.rr-run{border:0;background:#16A34A;color:#fff;box-shadow:0 2px 8px rgba(22,163,74,.35);}
.rr-run:hover:not(:disabled){background:#15803D;transform:translateY(-1px);}
.rr-save{border:0;background:#2563EB;color:#fff;box-shadow:0 2px 8px rgba(37,99,235,.30);}
.rr-save.is-saved{background:linear-gradient(160deg, rgba(255,255,255,.9), rgba(255,255,255,.65));color:#2563EB;border:1px solid #2563EB;box-shadow:inset 0 1px 0 rgba(255,255,255,.75);}
.rr-stop{border:0;background:#DC2626;color:#fff;box-shadow:0 2px 8px rgba(220,38,38,.3);}
.rr-watch{background:linear-gradient(160deg, rgba(255,255,255,.9), rgba(255,255,255,.65));color:#2563EB;border:1px solid #2563EB;box-shadow:inset 0 1px 0 rgba(255,255,255,.75);}
.rr-watch:hover{background:rgba(239,246,255,.92);}
.rr-watch-attn{background:#F59E0B;color:#1E293B;border:1px solid #F59E0B;animation:rrPulse 1.4s ease-in-out infinite;}
.rr-watch-attn:hover{background:#D97706;}
@keyframes rrPulse{0%,100%{box-shadow:0 0 0 0 rgba(245,158,11,.55);}50%{box-shadow:0 0 0 6px rgba(245,158,11,0);}}

.rr-tabs{position:relative;display:flex;gap:26px;margin:22px 0 0;border-bottom:1px solid rgba(148,163,184,.35);}
.rr-tab{position:relative;padding:12px 2px;font-size:14.5px;font-weight:600;color:#64748B;cursor:pointer;background:none;border:0;font-family:inherit;transition:color .18s;}
.rr-tab:hover{color:#1E293B;}
.rr-tab.active{color:#2563EB;}
.rr-ind{position:absolute;bottom:-1px;height:2.5px;border-radius:2px;background:#2563EB;transition:left .28s cubic-bezier(.4,0,.2,1), width .28s cubic-bezier(.4,0,.2,1);}

.rr-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding-top:16px;}
.rr-span{grid-column:1/-1;}
@media(max-width:960px){.rr-grid{grid-template-columns:1fr;}}
.rr-place{text-align:center;padding:70px 24px;margin-top:22px;color:#64748B;border:1px dashed rgba(148,163,184,.5);border-radius:14px;background:rgba(255,255,255,.6);}
.rr-place h3{margin:0 0 8px;color:#1E293B;font-size:17px;}
.rr-place p{max-width:460px;margin:0 auto;font-size:13.5px;line-height:1.6;}
.rr-card{${GLASS}border-radius:14px;padding:16px 18px;}
.rr-card.is-invalid{border-color:#FCA5A5;box-shadow:0 0 0 3px rgba(220,38,38,.08);}
.rr-rise{opacity:0;transform:translateY(12px);animation:rrRise .5s cubic-bezier(.2,.7,.3,1) forwards;}
.rr-card-h{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;}
.rr-card-h span{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#64748B;}
.rr-card-h small{font-size:11.5px;color:#94A3B8;}
.rr-req{color:#EF4444;font-style:normal;}
.rr-desc{margin:-6px 0 11px;font-size:12.5px;color:#64748B;}
.rr-warn{margin-top:6px;font-size:12.5px;color:#B45309;background:#FFFBEB;border:1px solid #FDE68A;border-radius:8px;padding:8px 12px;}

.rr-src{display:flex;align-items:center;gap:13px;padding:10px 12px;border:1px solid rgba(203,213,225,.7);border-radius:11px;margin-bottom:8px;transition:.18s;background:rgba(255,255,255,.65);}
.rr-src:hover{border-color:#CBD5E1;box-shadow:0 1px 2px rgba(15,23,42,.05);}
.rr-src.on{border-color:#86EFAC;background:linear-gradient(90deg,rgba(236,253,245,.9),rgba(255,255,255,.5) 60%);}
.rr-src-ic{width:38px;height:38px;border-radius:10px;display:grid;place-items:center;color:#fff;font-weight:800;font-size:15px;flex:none;}
.rr-src-tx{display:flex;flex-direction:column;gap:3px;flex:1;min-width:0;}
.rr-src-tx b{font-size:14.5px;}
.rr-src-tx small{display:flex;align-items:center;gap:8px;}
.rr-prio{font-size:10.5px;font-weight:700;color:#64748B;background:rgba(241,245,249,.9);border-radius:999px;padding:1px 8px;}
.rr-chip{font-size:11px;font-weight:700;padding:1px 9px;border-radius:999px;}
.rr-chip-on{background:#ECFDF5;color:#047857;}
.rr-chip-off{background:#F1F5F9;color:#64748B;}

.rr-toggle{position:relative;width:44px;height:25px;border-radius:999px;background:#CBD5E1;cursor:pointer;transition:background .2s;flex:none;border:0;padding:0;}
.rr-toggle.on{background:#16A34A;}
.rr-knob{position:absolute;top:2.5px;left:2.5px;width:20px;height:20px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.3);transition:transform .22s cubic-bezier(.34,1.56,.64,1);}
.rr-toggle.on .rr-knob{transform:translateX(19px);}

.rr-banner{display:flex;align-items:center;gap:12px;background:rgba(239,246,255,.85);border:1px solid #BFDBFE;border-radius:12px;padding:11px 13px;margin-bottom:14px;font-size:13.5px;color:#1E40AF;}
.rr-banner b{font-weight:800;}
.rr-banner-ic{width:34px;height:34px;border-radius:10px;background:#fff;border:1px solid #BFDBFE;display:grid;place-items:center;color:#2563EB;flex:none;}
.rr-fgrid{display:grid;grid-template-columns:1fr 1fr;gap:11px;}
.rr-fgrid--3{grid-template-columns:repeat(3,1fr);}
.rr-fgrid--4{grid-template-columns:repeat(4,1fr);}
@media(max-width:860px){.rr-fgrid--3{grid-template-columns:1fr;}.rr-fgrid--4{grid-template-columns:1fr 1fr;}}
@media(max-width:520px){.rr-fgrid,.rr-fgrid--4{grid-template-columns:1fr;}}
.rr-fld{display:flex;flex-direction:column;gap:6px;}
.rr-full{grid-column:1/-1;}
.rr-fld>span{font-size:11.5px;font-weight:700;color:#64748B;}
.rr-inp{height:40px;${GLASS_INPUT}border-radius:9px;padding:0 12px;font-size:13.5px;font-family:inherit;color:#1E293B;width:100%;box-sizing:border-box;transition:border-color .15s,box-shadow .15s,background .15s;}
.rr-inp:focus{outline:0;border-color:#2563EB;box-shadow:0 0 0 3px rgba(37,99,235,.12);${GLASS_INPUT_FOCUS}}
.rr-seg{display:flex;background:rgba(241,245,249,.9);border-radius:9px;padding:3px;}
.rr-seg button{flex:1;border:0;background:none;font-family:inherit;font-size:13px;font-weight:600;color:#64748B;padding:8px;border-radius:7px;cursor:pointer;transition:.15s;}
.rr-seg button.on{background:#fff;color:#2563EB;box-shadow:0 1px 2px rgba(15,23,42,.08);}
.rr-tglrow{display:flex;align-items:center;gap:12px;padding:10px 0 2px;border-top:1px solid rgba(226,232,240,.8);margin-top:12px;}
.rr-tglrow--bare{border:0;margin:0;padding:8px 0 0;}
.rr-tglrow-tx{flex:1;min-width:0;}
.rr-tglrow-tx b{display:block;font-size:13.5px;}
.rr-tglrow-tx small{color:#64748B;font-size:12px;}
.rr-info{display:flex;gap:9px;align-items:flex-start;background:rgba(239,246,255,.85);border:1px solid #BFDBFE;border-radius:10px;padding:9px 12px;font-size:12.5px;color:#1E40AF;margin-top:12px;}
.rr-info svg{flex:none;margin-top:1px;}
.rr-info code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:#fff;border:1px solid #BFDBFE;border-radius:5px;padding:1px 6px;font-size:11.5px;}

/* Filter chips (distinct from the small rr-chip Active/Paused pills) */
.rr-fchips{display:flex;flex-wrap:wrap;gap:8px;}
.rr-fchip{border:1px solid #CBD5E1;background:rgba(255,255,255,.75);color:#64748B;font-size:12.5px;font-weight:600;padding:6px 13px;border-radius:999px;cursor:pointer;transition:.15s;font-family:inherit;}
.rr-fchip:hover:not(:disabled){border-color:#94A3B8;color:#1E293B;}
.rr-fchip.on{background:#ECFDF5;border-color:#86EFAC;color:#047857;}
.rr-fchip:disabled{opacity:.45;cursor:not-allowed;}

/* Connect-accounts rows */
.rr-acct{display:flex;align-items:center;gap:13px;padding:10px 12px;border:1px solid rgba(203,213,225,.7);border-radius:11px;margin-bottom:8px;background:rgba(255,255,255,.65);}
.rr-acct-tx{flex:1;min-width:0;}
.rr-acct-tx b{display:block;font-size:14px;}
.rr-acct-tx small{color:#64748B;font-size:12px;display:block;margin-top:2px;}
.rr-acct-btn{display:inline-flex;align-items:center;gap:7px;border:1px solid #2563EB;background:linear-gradient(160deg, rgba(255,255,255,.9), rgba(255,255,255,.65));box-shadow:inset 0 1px 0 rgba(255,255,255,.75);color:#2563EB;font-size:12.5px;font-weight:600;padding:7px 13px;border-radius:8px;cursor:pointer;transition:.15s;white-space:nowrap;font-family:inherit;}
.rr-acct-btn:hover:not(:disabled){background:rgba(239,246,255,.92);}
.rr-acct-btn:disabled{opacity:.7;cursor:default;}

/* Live progress cards */
.rr-prog{display:grid;gap:16px;grid-template-columns:repeat(3,minmax(0,1fr));margin-top:22px;}
@media(max-width:640px){.rr-prog{grid-template-columns:1fr;}}
.rr-prog-card{position:relative;overflow:hidden;text-align:center;padding:16px 12px 20px;}
.rr-prog-card::before{content:"";position:absolute;inset:0;pointer-events:none;background:radial-gradient(120% 90% at 50% -10%, rgba(124,92,252,.16), rgba(124,92,252,0) 60%);}
.rr-prog-card>*{position:relative;}
.rr-prog-tag{position:absolute;top:8px;right:8px;display:inline-flex;align-items:center;gap:4px;font-size:8.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:#6D28D9;background:#EDE7FF;padding:2px 6px;border-radius:99px;border:1px solid #DED9F2;}
.rr-prog-tag i{width:5px;height:5px;border-radius:99px;background:#7C5CFC;animation:rrPulseDot 1.8s ease-out infinite;}
@keyframes rrPulseDot{0%{box-shadow:0 0 0 0 rgba(124,92,252,.5);}70%{box-shadow:0 0 0 6px rgba(124,92,252,0);}100%{box-shadow:0 0 0 0 rgba(124,92,252,0);}}
.rr-prog-chip{width:38px;height:38px;margin:0 auto 9px;display:grid;place-items:center;border-radius:11px;color:#6D28D9;background:linear-gradient(160deg,rgba(124,92,252,.18),rgba(124,92,252,.08));border:1px solid #DED9F2;}
.rr-prog-num{font-size:30px;font-weight:800;line-height:1;letter-spacing:-.02em;font-variant-numeric:tabular-nums;}
.rr-prog-label{margin-top:6px;font-size:10.5px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:#6B6785;}
.rr-prog-hint{margin-top:2px;font-size:10px;color:#9A96B5;}
.rr-prog-bar{position:absolute;left:12px;right:12px;bottom:9px;height:4px;border-radius:99px;background:linear-gradient(90deg,#7C5CFC,#6D28D9);opacity:.9;}

/* Run overlay */
.rr-overlay{position:fixed;inset:0;background:rgba(15,23,42,.45);-webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px);z-index:60;display:flex;align-items:center;justify-content:center;padding:20px;animation:rrFade .2s;}
.rr-runcard{width:min(540px,100%);${GLASS_MODAL}border-radius:16px;overflow:hidden;animation:rrPop .3s cubic-bezier(.34,1.4,.5,1);}
.rr-run-top{background:linear-gradient(120deg,#F6F3FF,#fff);padding:20px 22px;border-bottom:1px solid #DDD6FE;}
.rr-run-t{font-weight:800;font-size:17px;display:flex;align-items:center;gap:10px;}
.rr-done{color:#16A34A;}
.rr-fail{color:#DC2626;}
.rr-spinner{width:18px;height:18px;border-radius:50%;border:2.5px solid #DDD6FE;border-top-color:#6D28D9;animation:rrRot .8s linear infinite;display:inline-block;}
.rr-run-s{font-size:12.5px;color:#64748B;margin-top:4px;}
.rr-run-body{padding:20px 22px;}
.rr-prog-lab{display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px;}
.rr-prog-lab b{font-variant-numeric:tabular-nums;}
.rr-bar{height:8px;border-radius:999px;background:#F1F5F9;overflow:hidden;}
.rr-bar i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,#6D28D9,#7C5CFC);transition:width .9s cubic-bezier(.3,.8,.3,1);}
.rr-run-live{margin-top:12px;font-size:12.5px;color:#64748B;}
.rr-run-live b{color:#1E293B;font-variant-numeric:tabular-nums;}
.rr-run-foot{padding:16px 22px;border-top:1px solid #E2E8F0;display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap;}

@keyframes rrRot{to{transform:rotate(360deg);}}
@keyframes rrRise{to{opacity:1;transform:none;}}
@keyframes rrFade{from{opacity:0;}}
@keyframes rrPop{from{transform:scale(.94);opacity:.6;}}
@media (prefers-reduced-motion: reduce){.rr-rise{animation:none;opacity:1;transform:none;}}
${GLASS_FALLBACK(".rr-card")}
`;
