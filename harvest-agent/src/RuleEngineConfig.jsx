import React, { useEffect, useRef, useState } from "react";
import {
  SlidersHorizontal,
  Play, Save, Clock, Info, AlertTriangle, ChevronDown, Check, Loader2, LogIn, Eye,
  Target, Database, Hourglass,
} from "lucide-react";
import {
  getHarvestConfig, saveHarvestConfig, runHarvestAgent, getHarvestStatus,
  getRunHistory, getRunHistoryEntry, getActiveRun, setupLinkedinSession, setupNaukriSession, ApiError,
} from "./api";
import Sidebar from "./components/Sidebar";
import LiveBrowserView from "./components/LiveBrowserView";
import StopHarvestButton from "./components/StopHarvestModal";
import useCountUp from "./useCountUp";

const NaukriIcon = () => (
  <svg width="34" height="34" viewBox="0 0 34 34" aria-hidden="true">
    <rect width="34" height="34" rx="8" fill="#EF4444" />
    <path d="M11 22V12.5c0-.6.5-1 1.1-.9l4.4.9c.4.1.7.5.7.9V22" fill="none" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    <path d="M17.2 13.4l4.4-.9c.6-.1 1.1.3 1.1.9V22" fill="none" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    <circle cx="17" cy="22.5" r="1.1" fill="#fff" />
  </svg>
);

const LinkedInIcon = () => (
  <svg width="34" height="34" viewBox="0 0 34 34" aria-hidden="true">
    <rect width="34" height="34" rx="8" fill="#0A66C2" />
    <circle cx="11.6" cy="11.4" r="1.7" fill="#fff" />
    <rect x="10.1" y="14.6" width="3" height="9" rx="0.6" fill="#fff" />
    <path d="M16 14.6h2.9v1.3c.5-.9 1.5-1.6 3-1.6 2.4 0 3.6 1.5 3.6 4.2v5.1h-3v-4.6c0-1.2-.5-2-1.6-2-1 0-1.6.7-1.6 2v4.6h-3z" fill="#fff" />
  </svg>
);

const DiceIcon = () => (
  <svg width="34" height="34" viewBox="0 0 34 34" aria-hidden="true">
    <rect width="34" height="34" rx="8" fill="#0EA5A4" />
    <rect x="9" y="9" width="16" height="16" rx="4" fill="#fff" />
    <circle cx="13.2" cy="13.2" r="1.5" fill="#0EA5A4" />
    <circle cx="20.8" cy="13.2" r="1.5" fill="#0EA5A4" />
    <circle cx="17" cy="17" r="1.5" fill="#0EA5A4" />
    <circle cx="13.2" cy="20.8" r="1.5" fill="#0EA5A4" />
    <circle cx="20.8" cy="20.8" r="1.5" fill="#0EA5A4" />
  </svg>
);

/* ── Backend enum ↔ display-label maps (app/models/harvest_models.py) ───── */
const JOB_TYPES      = ["Any", "Contract", "Permanent", "Part-time", "Freelance", "Full-time"];
const WORK_MODES     = ["Any", "Remote", "Hybrid", "Onsite"];
// Only values the classifier can actually assign (the domain_keywords.json
// buckets + the coarse IT/Non-IT split). "Engineering"/"Finance"/"Operations"
// were removed — the backend never labels a job with them, so selecting one
// would extract zero jobs.
const DOMAINS        = ["Any", "Data Engineering", "Data Science", "AI/ML", "SAP", "Cloud", "Digital", "UX/UI", "ERP", "Cyber Security", "Infrastructure", "IT", "Non-IT"];
const HIRING_ENTITIES = ["Any", "Direct Client", "GCC", "Ambiguous", "Staffing Firm"];
const GCC_MODES = [
  { value: "include_gcc", label: "Include GCC" },
  { value: "gcc_only", label: "GCC only" },
  { value: "exclude_gcc", label: "Exclude GCC" },
];
const SEARCH_WINDOWS = [
  { value: 24, label: "Last 24 hours" },
  { value: 48, label: "Last 48 hours" },
  { value: 72, label: "Last 72 hours" },
  { value: 168, label: "Last 7 days" },
  { value: 720, label: "Last 30 days" },
];
const FREQUENCIES = [
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
];
const TIMEZONES = [
  { value: "Asia/Kolkata", label: "IST (UTC+5:30)" },
  { value: "UTC", label: "GMT (UTC+0)" },
  { value: "America/New_York", label: "EST (UTC−5)" },
  { value: "America/Los_Angeles", label: "PST (UTC−8)" },
  { value: "Asia/Singapore", label: "SGT (UTC+8)" },
];

function Toggle({ on, onChange, label }) {
  return (
    <button type="button" role="switch" aria-checked={on} aria-label={label}
      className={"rec-toggle" + (on ? " is-on" : "")} onClick={() => onChange(!on)}>
      <span className="rec-toggle-knob" />
    </button>
  );
}

function Chip({ active, onClick, children, variant = "green", disabled = false, title }) {
  return (
    <button type="button" className={"rec-chip" + (active ? " is-active rec-chip--" + variant : "")}
      aria-pressed={active} onClick={onClick} disabled={disabled} title={title}
      style={disabled ? { opacity: 0.4, cursor: "not-allowed" } : undefined}>
      {children}
    </button>
  );
}

function Select({ value, onChange, options, ariaLabel }) {
  return (
    <div className="rec-select">
      <select value={value} aria-label={ariaLabel} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
      <ChevronDown size={16} className="rec-select-caret" />
    </div>
  );
}

function Card({ title, desc, required, invalid, error, children }) {
  return (
    <div className={"rec-card" + (invalid ? " is-invalid" : "")}>
      <div className="rec-card-title">
        {title}
        {required && <span className="rec-req" aria-label="required">*</span>}
      </div>
      {desc && <p className="rec-card-desc">{desc}</p>}
      {children}
      {invalid && <div className="rec-error">{error || "Select at least one option."}</div>}
    </div>
  );
}

const fmtRunDate = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};

const nowLabel = () => {
  const d = new Date();
  let h = d.getHours();
  const m = String(d.getMinutes()).padStart(2, "0");
  const ap = h >= 12 ? "PM" : "AM";
  h = h % 12 || 12;
  return `Today ${String(h).padStart(2, "0")}:${m} ${ap}`;
};

const DEFAULT_CONFIG = {
  sources: { linkedin: true, naukri: false, dice: false },
  filters: {
    keyword: "", location: "", job_type: "Any", work_mode: "Any",
    search_window_hours: 24, max_jobs: 500,
    domain: "Any", hiring_entity: "Any", gcc_mode: "include_gcc",
    salary_min: null, salary_max: null, salary_currency: "INR",
    include_undisclosed_salary: true,
    verification: { enabled: false, method: "career_page", on_mismatch: "flag", on_not_found: "flag" },
  },
  schedule: { frequency: "daily", run_time: "09:00", timezone: "Asia/Kolkata", enabled: false },
  browser: { headless: false, slow_mo_ms: 0, chrome_profile: "data/chrome_profile" },
};

export default function RuleEngineConfig({
  onNavigate = () => {}, jobsCount = 0, runsCount = 0, onRunComplete = () => {},
  harvestRunning = false, setHarvestRunning = () => {},
}) {
  const [activeTab, setActiveTab] = useState("sources");

  const [loadedConfig, setLoadedConfig] = useState(DEFAULT_CONFIG);
  const [configLoading, setConfigLoading] = useState(true);
  const [loadError, setLoadError] = useState("");

  const [sources, setSources] = useState(DEFAULT_CONFIG.sources);
  const [keyword, setKeyword] = useState(DEFAULT_CONFIG.filters.keyword);
  const [location, setLocation] = useState(DEFAULT_CONFIG.filters.location);
  const [jobType, setJobType] = useState(DEFAULT_CONFIG.filters.job_type);
  const [workMode, setWorkMode] = useState(DEFAULT_CONFIG.filters.work_mode);
  const [frequency, setFrequency] = useState(DEFAULT_CONFIG.schedule.frequency);
  const [runTime, setRunTime] = useState(DEFAULT_CONFIG.schedule.run_time);
  const [timezone, setTimezone] = useState(DEFAULT_CONFIG.schedule.timezone);
  const [searchWindow, setSearchWindow] = useState(DEFAULT_CONFIG.filters.search_window_hours);
  const [domain, setDomain] = useState(DEFAULT_CONFIG.filters.domain);
  const [hiringEntity, setHiringEntity] = useState(DEFAULT_CONFIG.filters.hiring_entity);
  const [gccMode, setGccMode] = useState(DEFAULT_CONFIG.filters.gcc_mode);
  const [salaryMin, setSalaryMin] = useState("");
  const [salaryMax, setSalaryMax] = useState("");
  const [currency, setCurrency] = useState(DEFAULT_CONFIG.filters.salary_currency);
  const [includeUndisclosed, setIncludeUndisclosed] = useState(DEFAULT_CONFIG.filters.include_undisclosed_salary);

  const [lastSaved, setLastSaved] = useState("—");
  const [lastRun, setLastRun] = useState("—");
  const [harvested, setHarvested] = useState(0);
  const [harvestedLive, setHarvestedLive] = useState(0); // jobs saved so far, updated live during a run
  const [maxPerDay, setMaxPerDay] = useState(0);         // MAX_JOBS_PER_DAY from the backend (.env); 0 = unlimited
  const [runState, setRunState] = useState("idle"); // idle | running | success | failed
  const [runMessage, setRunMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [dirty, setDirty] = useState(false);
  const [attempted, setAttempted] = useState(false);

  const pollTimer = useRef(null);
  useEffect(() => () => clearTimeout(pollTimer.current), []);

  // Animated count-up for the live progress cards. `liveCount` is the real
  // jobs-saved-to-DB count (status.combined); Remaining = Max − Saved, clamped
  // at 0. When the daily cap is 0 (unlimited) Max/Remaining show ∞.
  const unlimited = !(maxPerDay > 0);
  const liveCount = useCountUp(harvestedLive);
  const maxCount = useCountUp(maxPerDay);
  const remainingCount = useCountUp(unlimited ? 0 : Math.max(0, maxPerDay - harvestedLive));

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [config, history] = await Promise.all([
          getHarvestConfig(),
          getRunHistory().catch(() => ({ runs: [] })),
        ]);
        if (cancelled) return;
        applyConfig(config);
        const latest = history.runs && history.runs[0];
        if (latest) {
          setLastRun(fmtRunDate(latest.completed_at || latest.started_at));
          setHarvested(latest.jobs_found ?? 0);
        }
      } catch (err) {
        if (!cancelled) {
          setLoadError(
            err instanceof ApiError
              ? `Could not load configuration: ${err.message}`
              : "Could not reach the harvest backend. Is it running on the configured API URL?"
          );
        }
      } finally {
        if (!cancelled) setConfigLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function applyConfig(config) {
    setLoadedConfig(config);
    setSources(config.sources);
    setKeyword(config.filters.keyword || "");
    setLocation(config.filters.location || "");
    setJobType(config.filters.job_type || "Any");
    setWorkMode(config.filters.work_mode || "Any");
    setDomain(config.filters.domain || "Any");
    const loadedEntity = config.filters.hiring_entity || "Any";
    setHiringEntity(loadedEntity);
    // A specific hiring entity determines GCC-ness — neutralize any stale/
    // conflicting gcc_mode on load so the UI never shows the impossible combo
    // (the backend normalizes it too; see FiltersConfig._reconcile_gcc).
    setGccMode(loadedEntity !== "Any" ? "include_gcc" : (config.filters.gcc_mode || "include_gcc"));
    setSearchWindow(config.filters.search_window_hours || 24);
    setSalaryMin(config.filters.salary_min == null ? "" : String(config.filters.salary_min));
    setSalaryMax(config.filters.salary_max == null ? "" : String(config.filters.salary_max));
    setCurrency(config.filters.salary_currency || "INR");
    setIncludeUndisclosed(config.filters.include_undisclosed_salary ?? true);
    setFrequency(config.schedule.frequency || "daily");
    setRunTime(config.schedule.run_time || "09:00");
    setTimezone(config.schedule.timezone || "Asia/Kolkata");
    setDirty(false);
  }

  const errors = {
    jobSource: !Object.values(sources).some(Boolean),
    jobType: !jobType,
    domain: !domain,
    hiring: !hiringEntity,
    gccFlag: !gccMode,
  };
  const hasErrors = Object.values(errors).some(Boolean);
  const showErr = (k) => attempted && errors[k];
  const markDirty = () => setDirty(true);
  const setSource = (key, val) => { setSources((s) => ({ ...s, [key]: val })); markDirty(); };

  function buildPayload() {
    return {
      ...loadedConfig,
      sources: { ...sources },
      filters: {
        ...loadedConfig.filters,
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
      schedule: { ...loadedConfig.schedule, frequency, run_time: runTime, timezone },
    };
  }

  const handleSave = async () => {
    if (hasErrors) { setAttempted(true); setActiveTab("sources"); return; }
    setSaving(true);
    setSaveError("");
    try {
      const saved = await saveHarvestConfig(buildPayload());
      setLoadedConfig(saved);
      setLastSaved(nowLabel());
      setDirty(false);
      setAttempted(false);
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : "Could not save configuration — check the backend connection.");
    } finally {
      setSaving(false);
    }
  };

  // GET /harvest-status/{job_id} carries live progress — including a
  // human-readable `message` that changes to a "waiting for login…" prompt
  // if LinkedIn isn't authenticated (see LinkedInAgent._wait_for_manual_login).
  // Falls back to run-history for the final result once status stops "running".
  function pollHarvestStatus(jobId, runId) {
    const tick = async () => {
      try {
        const status = await getHarvestStatus(jobId);
        if (status.status === "running") {
          setRunMessage(status.message || "Running…");
          // `jobs_saved_today` is a live DB count of scraped_jobs rows persisted
          // today (across all runs), so it reflects real saved rows and climbs as
          // each batch inserts. Fall back to `combined` (this run's count) if the
          // backend didn't send it.
          setHarvestedLive(status.jobs_saved_today ?? status.combined ?? 0);
          // Daily cap (MAX_JOBS_PER_DAY) drives the Max / Remaining cards.
          setMaxPerDay(status.max_jobs_per_day ?? 0);
          // Poll every 6s while running so the live count visibly ticks without
          // hammering the server.
          pollTimer.current = setTimeout(tick, 6000);
          return;
        }
        if (status.status === "failed") {
          setRunState("failed");
          setRunMessage(status.error || status.message || `Harvest run ${runId} failed — check server logs.`);
          setHarvestRunning(false);
          return;
        }
        // success | no_results
        setRunState("success");
        setHarvested(status.combined ?? harvested);
        setLastRun(fmtRunDate(status.completed_at));
        setHarvestRunning(false);
        onRunComplete();
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          // JobTracker entry not found (e.g. server restarted) — fall back
          // to run-history, which is the durable record.
          try {
            const entry = await getRunHistoryEntry(runId);
            if (entry.status === "failed") {
              setRunState("failed");
              setRunMessage(entry.error || `Harvest run ${runId} failed — check server logs.`);
            } else {
              setRunState("success");
              setHarvested(entry.jobs_found ?? harvested);
              setLastRun(fmtRunDate(entry.completed_at));
              onRunComplete();
            }
          } catch {
            setRunMessage("Harvest running — no live status available yet; retrying…");
            pollTimer.current = setTimeout(tick, 10000);
            return;
          }
          setHarvestRunning(false);
          return;
        }
        setRunState("failed");
        setRunMessage(err instanceof ApiError ? err.message : "Lost connection while checking harvest status.");
        setHarvestRunning(false);
      }
    };
    tick();
  }

  // Adopt an already-in-flight run this page didn't launch — e.g. one started by
  // the scheduler, or from another tab/session — so its live progress shows here
  // too. Runs once on mount; if /active-run reports an active job, start polling
  // its status just like a locally-started run.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await getActiveRun();
        if (!cancelled && res?.active && res.job_id) {
          setRunState("running");
          setRunMessage("Harvesting…");
          setHarvestedLive(0);
          pollHarvestStatus(res.job_id, res.run_id);
        }
      } catch {
        /* backend unreachable — HealthBadge surfaces the outage */
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleRun = async () => {
    if (runState === "running") return;
    if (harvestRunning) {
      setRunState("failed");
      setRunMessage("Another harvest is already running (Source Runs page or a previous session) — wait for it to finish. Running two at once collides on the shared browser profile and both fail.");
      return;
    }
    if (hasErrors) { setAttempted(true); setActiveTab("sources"); return; }

    // Run against whatever is currently saved on the server — save first if dirty.
    if (dirty) {
      await handleSave();
      if (hasErrors) return;
    }

    setRunState("running");
    setHarvestRunning(true);
    setRunMessage("Starting harvest…");
    setHarvestedLive(0);
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
      // 409 = the backend rejected because a run is already in flight — keep the
      // controls frozen rather than clearing them; any other error unfreezes.
      setHarvestRunning(err instanceof ApiError && err.status === 409);
    }
  };

  const [linkedinSetup, setLinkedinSetup] = useState({ loading: false, message: "" });
  const [naukriSetup, setNaukriSetup] = useState({ loading: false, message: "" });
  const [liveViewSource, setLiveViewSource] = useState(null); // "linkedin" | "naukri" | null

  const handleLinkedinSetup = async () => {
    setLinkedinSetup({ loading: true, message: "Log in below — this is the live browser. Waiting up to 10 minutes…" });
    setLiveViewSource("linkedin");
    try {
      const res = await setupLinkedinSession();
      setLinkedinSetup({ loading: false, message: res.status === "ready" ? (res.message || "LinkedIn session saved.") : (res.reason || res.message || "Could not confirm login.") });
    } catch (err) {
      setLinkedinSetup({ loading: false, message: err instanceof ApiError ? err.message : "Could not reach the harvest backend." });
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

  const sourceList = [
    { key: "naukri",   name: "Naukri.com",    priority: 1, Icon: NaukriIcon },
    { key: "linkedin", name: "LinkedIn Jobs",  priority: 2, Icon: LinkedInIcon },
    { key: "dice",     name: "Dice.com",       priority: 3, Icon: DiceIcon },
  ];

  // The backend pauses a running harvest and waits for manual LinkedIn login
  // (see LinkedInAgent._wait_for_manual_login) — surface that clearly so the
  // user knows to click "Watch Live Browser" instead of just waiting.
  const needsLogin = runState === "running" && /log ?in/i.test(runMessage || "");

  return (
    <div className="rec-root">
      <style>{styles}</style>

      {liveViewSource && (
        <LiveBrowserView
          title={
            liveViewSource === "linkedin" ? "LinkedIn login — live browser" :
            liveViewSource === "naukri"   ? "Naukri login — live browser" :
            "Harvest in progress — live browser"
          }
          onClose={() => setLiveViewSource(null)}
        />
      )}

      {/* Sidebar — shared component (Sidebar.jsx). This screen *is* the Rule
          Engine, so "rules" is always the active page. */}
      <Sidebar activePage="rules" onNavigate={onNavigate} jobsCount={jobsCount} runsCount={runsCount} />

      {/* Main */}
      <main className="rec-main">
        <header className="rec-header">
          <div className="rec-header-text">
            <h1>Rule Engine — Configuration</h1>
            <div className="rec-meta">
              <span>Last saved: {lastSaved}</span>
              <span className="rec-dot">·</span>
              <span>Last run: {lastRun}</span>
              <span className="rec-dot">·</span>
              {runState === "running" ? (
                <span className="rec-status rec-status--run">
                  <Loader2 size={14} className="rec-spin" /> {runMessage || "Running…"}
                  {harvestedLive > 0 && <> · <b>{harvestedLive}</b> saved</>}
                </span>
              ) : runState === "failed" ? (
                <span className="rec-status rec-status--err"><AlertTriangle size={14} /> {runMessage || "Harvest failed"}</span>
              ) : (
                <span className="rec-status rec-status--ok"><Check size={14} /> {runState === "success" ? `Success (${harvested} harvested)` : "Ready"}</span>
              )}
            </div>
          </div>
          <div className="rec-header-actions">
            {attempted && hasErrors && (
              <span className="rec-validation"><AlertTriangle size={14} /> Complete required fields</span>
            )}
            {saveError && <span className="rec-validation"><AlertTriangle size={14} /> {saveError}</span>}
            {(runState === "running" || harvestRunning) && (
              <button
                className={"rec-btn rec-btn--watch" + (needsLogin ? " rec-btn--watch-attention" : "")}
                onClick={() => setLiveViewSource("harvest")}
                title={needsLogin ? "LinkedIn needs you to log in — click to open the live browser" : undefined}
              >
                <Eye size={16} /> {needsLogin ? "Log in now — Watch Live Browser" : "Watch Live Browser"}
              </button>
            )}
            <StopHarvestButton harvestRunning={harvestRunning}
              onStopped={() => setRunMessage("Stop requested — the run will halt shortly and save its jobs. The report email is deferred to the next successful run.")} />
            <button
              className={"rec-btn rec-btn--run" + (harvestRunning && runState !== "running" ? " rec-btn--busy" : "")}
              onClick={handleRun}
              disabled={runState === "running" || configLoading || harvestRunning}
              title={harvestRunning && runState !== "running" ? "A harvest is already running — controls locked until it finishes" : undefined}>
              {(runState === "running" || harvestRunning) ? <Loader2 size={16} className="rec-spin" /> : <Play size={16} fill="currentColor" />}
              {runState === "running" ? "Running" : harvestRunning ? "Running…" : "Run Now"}
            </button>
            <button className="rec-btn rec-btn--save" onClick={handleSave} disabled={saving || configLoading}>
              {saving ? <Loader2 size={16} className="rec-spin" /> : <Save size={16} />}
              {saving ? "Saving…" : dirty ? "Save Config" : "Saved"}
            </button>
          </div>
        </header>

        {runState === "running" && (
          <div className="rec-prog">
            {/* Max Jobs / Day — daily cap from the backend .env (MAX_JOBS_PER_DAY) */}
            <div className="rec-prog-card">
              <div className="rec-prog-chip"><Target size={18} /></div>
              <div className="rec-prog-num">{unlimited ? "∞" : maxCount}</div>
              <div className="rec-prog-label">Max Jobs / Day</div>
              <div className="rec-prog-hint">daily harvest ceiling</div>
              <div className="rec-prog-bar" />
            </div>

            {/* Jobs Saved — live count of rows actually persisted to the DB */}
            <div className="rec-prog-card is-live">
              <span className="rec-prog-tag"><i /> Live</span>
              <div className="rec-prog-chip"><Database size={18} /></div>
              <div className="rec-prog-num">{liveCount}</div>
              <div className="rec-prog-label">Jobs Saved</div>
              <div className="rec-prog-hint">persisted to database</div>
              <div className="rec-prog-bar" />
            </div>

            {/* Remaining — Max − Saved, recomputed as Jobs Saved climbs */}
            <div className="rec-prog-card">
              <div className="rec-prog-chip"><Hourglass size={18} /></div>
              <div className="rec-prog-num">{unlimited ? "∞" : remainingCount}</div>
              <div className="rec-prog-label">Remaining Jobs</div>
              <div className="rec-prog-hint">Max − Saved</div>
              <div className="rec-prog-bar" />
            </div>
          </div>
        )}

        {loadError && (
          <div className="rec-note rec-note--error" style={{ margin: "16px 28px 0" }}>
            <AlertTriangle size={16} />
            <span>{loadError}</span>
          </div>
        )}

        {/* Tabs */}
        <div className="rec-tabs">
          {[["sources", "Sources & Schedule"], ["filters", "Filters"], ["verification", "Verification"]].map(([key, label]) => (
            <button key={key} className={"rec-tab" + (activeTab === key ? " is-active" : "")} onClick={() => setActiveTab(key)}>
              {label}
            </button>
          ))}
        </div>

        <div className="rec-content">
          {activeTab === "sources" && (
            <>
              <div className="rec-grid rec-grid--2">
                {/* Job Sources */}
                <div className={"rec-panel" + (showErr("jobSource") ? " is-invalid" : "")}>
                  <div className="rec-panel-head">
                    Job Sources <span className="rec-req">*</span>
                  </div>
                  <div className="rec-sources">
                    {sourceList.map(({ key, name, priority, Icon }) => {
                      const on = sources[key];
                      return (
                        <div className="rec-source" key={key}>
                          <Icon />
                          <div className="rec-source-text">
                            <div className="rec-source-name">{name}</div>
                            <div className="rec-source-sub">
                              Priority {priority} · <span className={on ? "rec-on" : "rec-off"}>{on ? "Active" : "Not active"}</span>
                            </div>
                          </div>
                          <Toggle on={on} onChange={(v) => setSource(key, v)} label={name} />
                        </div>
                      );
                    })}
                  </div>
                  {showErr("jobSource") && <div className="rec-error">Enable at least one job source.</div>}
                </div>

                {/* Run Schedule */}
                <div className="rec-panel">
                  <div className="rec-panel-head">Run Schedule</div>
                  <div className="rec-field-row">
                    <div className="rec-field">
                      <label>Frequency</label>
                      <Select value={FREQUENCIES.find((f) => f.value === frequency)?.label || "Daily"}
                        onChange={(label) => { setFrequency(FREQUENCIES.find((f) => f.label === label).value); markDirty(); }}
                        ariaLabel="Frequency" options={FREQUENCIES.map((f) => f.label)} />
                    </div>
                    <div className="rec-field">
                      <label>Run time</label>
                      <div className="rec-time">
                        <input type="time" value={runTime} onChange={(e) => { setRunTime(e.target.value); markDirty(); }} aria-label="Run time" />
                        <Clock size={15} className="rec-time-icon" />
                      </div>
                    </div>
                    <div className="rec-field">
                      <label>Timezone</label>
                      <Select value={TIMEZONES.find((t) => t.value === timezone)?.label || "IST (UTC+5:30)"}
                        onChange={(label) => { setTimezone(TIMEZONES.find((t) => t.label === label).value); markDirty(); }}
                        ariaLabel="Timezone" options={TIMEZONES.map((t) => t.label)} />
                    </div>
                  </div>
                  <div className="rec-field-row" style={{ marginTop: 14 }}>
                    <div className="rec-field">
                      <label>Keyword</label>
                      <input className="rec-input" type="text" placeholder="e.g. AI Engineer" value={keyword}
                        onChange={(e) => { setKeyword(e.target.value); markDirty(); }} />
                    </div>
                    <div className="rec-field">
                      <label>Work mode</label>
                      <Select value={workMode} onChange={(v) => { setWorkMode(v); markDirty(); }} ariaLabel="Work mode" options={WORK_MODES} />
                    </div>
                  </div>
                  <div className="rec-field rec-field--full">
                    <label>Search window (how far back to look)</label>
                    <Select value={SEARCH_WINDOWS.find((w) => w.value === searchWindow)?.label || "Last 24 hours"}
                      onChange={(label) => { setSearchWindow(SEARCH_WINDOWS.find((w) => w.label === label).value); markDirty(); }}
                      ariaLabel="Search window" options={SEARCH_WINDOWS.map((w) => w.label)} />
                  </div>
                  <div className="rec-note rec-note--info">
                    <Info size={16} />
                    <span>Rule stored in <code>harvest_config.search_window_hours</code>. Agent skips any posting older than now − N hours.</span>
                  </div>
                </div>
              </div>

              <div className="rec-panel" style={{ marginTop: 18 }}>
                <div className="rec-panel-head">Connect Accounts</div>
                <p className="rec-card-desc" style={{ marginBottom: 16 }}>
                  Log in once per source in the browser window that opens — the session is saved to the server's
                  Chrome profile and reused on every future harvest run.
                </p>
                <div className="rec-field-row">
                  <div className="rec-source" style={{ flex: 1, border: "1px solid var(--line)", borderRadius: 10, padding: "12px 14px" }}>
                    <LinkedInIcon />
                    <div className="rec-source-text">
                      <div className="rec-source-name">LinkedIn</div>
                      <div className="rec-source-sub">{linkedinSetup.message || "Not connected in this session"}</div>
                    </div>
                    <button type="button" className="rec-btn rec-btn--save" onClick={handleLinkedinSetup} disabled={linkedinSetup.loading}>
                      {linkedinSetup.loading ? <Loader2 size={16} className="rec-spin" /> : <LogIn size={16} />}
                      {linkedinSetup.loading ? "Waiting…" : "Connect"}
                    </button>
                  </div>
                  <div className="rec-source" style={{ flex: 1, border: "1px solid var(--line)", borderRadius: 10, padding: "12px 14px" }}>
                    <NaukriIcon />
                    <div className="rec-source-text">
                      <div className="rec-source-name">Naukri</div>
                      <div className="rec-source-sub">{naukriSetup.message || "Not connected in this session"}</div>
                    </div>
                    <button type="button" className="rec-btn rec-btn--save" onClick={handleNaukriSetup} disabled={naukriSetup.loading}>
                      {naukriSetup.loading ? <Loader2 size={16} className="rec-spin" /> : <LogIn size={16} />}
                      {naukriSetup.loading ? "Waiting…" : "Connect"}
                    </button>
                  </div>
                </div>
              </div>

              <div className="rec-section-label">Filters — search terms (job type, domain, location) narrow what each source fetches; every scraped job is kept and labelled, then flagged if it doesn&rsquo;t match (nothing is dropped)</div>

              <div className="rec-grid rec-grid--2">
                <Card title="Job type" required invalid={showErr("jobType")} desc="Pushed into each source's search (e.g. LinkedIn f_JT). Non-matching jobs are flagged after scraping, not dropped.">
                  <div className="rec-chips">
                    {JOB_TYPES.map((t) => (
                      <Chip key={t} active={jobType === t} onClick={() => { setJobType(t); markDirty(); }}>{t}</Chip>
                    ))}
                  </div>
                </Card>

                <Card title="Domain" required invalid={showErr("domain")} desc='A specific domain (or "IT") adds keywords to the search so fewer irrelevant jobs are fetched. Every job is also labelled by title/JD keywords and flagged if it doesn&rsquo;t match. "Non-IT"/"Any" don&rsquo;t narrow the search.'>
                  <div className="rec-chips">
                    {DOMAINS.map((t) => (
                      <Chip key={t} active={domain === t} onClick={() => { setDomain(t); markDirty(); }}>{t}</Chip>
                    ))}
                  </div>
                </Card>

                <Card title="Hiring entity" required invalid={showErr("hiring")} desc='Classified after scraping via company/JD keyword lists (GCC, staffing, direct-client). No LinkedIn equivalent, so non-matching jobs are flagged, not dropped.'>
                  <div className="rec-chips">
                    {HIRING_ENTITIES.map((t) => (
                      <Chip key={t} active={hiringEntity === t}
                        onClick={() => {
                          setHiringEntity(t);
                          // A specific hiring entity already determines GCC-ness,
                          // so neutralize GCC flag to avoid the impossible combo
                          // (e.g. gcc_only + Direct Client) — matches the backend
                          // auto-normalization in FiltersConfig._reconcile_gcc.
                          if (t !== "Any") setGccMode("include_gcc");
                          markDirty();
                        }}>{t}</Chip>
                    ))}
                  </div>
                </Card>

                <Card title="GCC flag" required invalid={showErr("gccFlag")}
                  desc={hiringEntity !== "Any"
                    ? `Determined by Hiring entity ("${hiringEntity}"). GCC flag applies only when Hiring entity is "Any".`
                    : "Global Capability Centre detection via keyword list in JD."}>
                  <div className="rec-chips">
                    {GCC_MODES.map((g) => (
                      <Chip key={g.value} active={gccMode === g.value}
                        disabled={hiringEntity !== "Any"}
                        title={hiringEntity !== "Any"
                          ? `Locked — Hiring entity "${hiringEntity}" already determines GCC status`
                          : undefined}
                        onClick={() => { setGccMode(g.value); markDirty(); }}>{g.label}</Chip>
                    ))}
                  </div>
                </Card>

                <Card title="Location" desc="Free-text location forwarded to every source agent's search query.">
                  <input className="rec-input" type="text" placeholder="e.g. Bangalore, India" value={location}
                    onChange={(e) => { setLocation(e.target.value); markDirty(); }} />
                </Card>

                <Card title="Salary / Budget" desc="Checked after scraping against the posting's disclosed salary. Out-of-range jobs are flagged, not dropped.">
                  <div className="rec-field-row">
                    <div className="rec-field">
                      <label>Min (₹ LPA)</label>
                      <input className="rec-input" type="number" value={salaryMin} onChange={(e) => { setSalaryMin(e.target.value); markDirty(); }} />
                    </div>
                    <div className="rec-field">
                      <label>Max (₹ LPA)</label>
                      <input className="rec-input" type="number" value={salaryMax} onChange={(e) => { setSalaryMax(e.target.value); markDirty(); }} />
                    </div>
                    <div className="rec-field">
                      <label>Currency</label>
                      <Select value={currency} onChange={(v) => { setCurrency(v); markDirty(); }} ariaLabel="Currency" options={["INR", "USD", "EUR", "GBP"]} />
                    </div>
                  </div>
                  <div className="rec-inline-toggle">
                    <Toggle on={includeUndisclosed} onChange={(v) => { setIncludeUndisclosed(v); markDirty(); }} label="Include undisclosed salary" />
                    <span>Include postings where salary is not disclosed</span>
                  </div>
                </Card>
              </div>
            </>
          )}

          {activeTab === "filters" && (
            <div className="rec-placeholder">
              <SlidersHorizontal size={28} />
              <h3>Post-collection filters</h3>
              <p>Refinement rules applied after harvesting — dedupe, keyword blocklists, and recruiter-contact requirements.</p>
            </div>
          )}

          {activeTab === "verification" && (
            <div className="rec-placeholder">
              <Check size={28} />
              <h3>Verification</h3>
              <p>Contact-validation steps — email format, mobile reachability, and POC confirmation before a posting enters Outreach.</p>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}

const styles = `
  .rec-root {
    --primary:#2563EB; --secondary:#1E40AF; --accent:#F59E0B;
    --bg:#F8FAFC; --text:#1E293B; --muted:#64748B; --line:#E2E8F0;
    --green:#16A34A; --green-bg:#ECFDF5; --green-bd:#86EFAC;
    --sidebar:#0F172A;
    display:flex; min-height:100vh;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    color:var(--text); background:var(--bg);
    font-size:14px; line-height:1.45; -webkit-font-smoothing:antialiased;
  }
  .rec-root * { box-sizing:border-box; }

  /* Sidebar styles now live in Sidebar.jsx (the shared component). */

  /* Main */
  .rec-main { flex:1; min-width:0; display:flex; flex-direction:column; overflow:hidden; }
  .rec-header { display:flex; justify-content:space-between; align-items:flex-start; gap:18px; padding:24px 28px 16px; background:#fff; border-bottom:1px solid var(--line); }
  .rec-header h1 { font-size:21px; font-weight:700; margin:0; letter-spacing:-0.3px; }
  .rec-meta { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-top:7px; font-size:12.5px; color:var(--muted); }
  .rec-dot { color:#CBD5E1; }
  .rec-status { display:inline-flex; align-items:center; gap:4px; font-weight:600; }
  .rec-status--ok { color:var(--green); }
  .rec-status--run { color:var(--accent); }
  .rec-status--err { color:#DC2626; }
  .rec-header-actions { display:flex; align-items:center; gap:10px; flex:0 0 auto; }
  .rec-btn { display:inline-flex; align-items:center; gap:7px; border:none; cursor:pointer; padding:9px 16px; border-radius:9px; font-size:13.5px; font-weight:600; transition:transform .08s,opacity .15s; white-space:nowrap; }
  .rec-btn:active { transform:translateY(1px); }
  .rec-btn:disabled { opacity:.75; cursor:default; }
  .rec-btn--run { background:var(--green); color:#fff; box-shadow:0 1px 2px rgba(22,163,74,.35); }
  .rec-btn--run:hover:not(:disabled) { background:#15803D; }
  /* A harvest is running elsewhere — grey the button + spinner to signal "locked". */
  .rec-btn--busy, .rec-btn--busy:hover { background:#94A3B8; box-shadow:none; }
  .rec-btn--save { background:var(--primary); color:#fff; box-shadow:0 1px 2px rgba(37,99,235,.35); }
  .rec-btn--save:hover { background:var(--secondary); }
  .rec-btn--watch { background:#fff; color:var(--primary); border:1px solid var(--primary); }
  .rec-btn--watch:hover { background:#EFF6FF; }
  .rec-btn--watch-attention {
    background:#F59E0B; color:#1E293B; border:1px solid #F59E0B;
    animation: rec-pulse 1.4s ease-in-out infinite;
  }
  .rec-btn--watch-attention:hover { background:#D97706; }
  @keyframes rec-pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(245,158,11,.55); }
    50%      { box-shadow: 0 0 0 6px rgba(245,158,11,0); }
  }

  /* ── Live harvest progress cards (Max / Saved / Remaining) ─────────────── */
  /* Compact + centered: capped width with auto side margins leaves breathing
     room at the left/right of the page. */
  .rec-prog { max-width:640px; margin:18px auto 4px; padding:0 24px; display:grid; gap:14px; grid-template-columns:repeat(3,minmax(0,1fr)); }
  @media (max-width:640px) { .rec-prog { grid-template-columns:1fr; max-width:320px; } }
  .rec-prog-card {
    position:relative; overflow:hidden; text-align:center;
    background:linear-gradient(160deg,#F6F3FF 0%,#fff 55%),#fff;
    border:1px solid #ECEAF6; border-radius:12px; padding:14px 12px 18px;
    box-shadow:0 1px 2px rgba(30,27,46,.05);
    transition:transform .22s cubic-bezier(.2,.7,.3,1), box-shadow .22s ease, border-color .22s ease;
  }
  .rec-prog-card::before {
    content:""; position:absolute; inset:0; pointer-events:none;
    background:radial-gradient(120% 90% at 50% -10%, rgba(124,92,252,.14), rgba(124,92,252,0) 60%);
    opacity:.9; transition:opacity .22s ease;
  }
  .rec-prog-card > * { position:relative; z-index:1; }
  .rec-prog-card:hover {
    transform:scale(1.035) translateY(-3px);
    box-shadow:0 18px 40px -18px rgba(109,40,217,.45), 0 8px 18px -12px rgba(30,27,46,.25);
    border-color:#DED9F2;
  }
  .rec-prog-card:hover::before { opacity:1; }
  .rec-prog-card.is-live { border-color:#DED9F2; }
  .rec-prog-tag {
    position:absolute; top:8px; right:8px; z-index:2; display:inline-flex; align-items:center; gap:4px;
    font-size:8.5px; font-weight:700; letter-spacing:.09em; text-transform:uppercase; color:#6D28D9;
    background:#EDE7FF; padding:2px 6px; border-radius:99px; border:1px solid #DED9F2;
  }
  .rec-prog-tag i { width:5px; height:5px; border-radius:99px; background:#7C5CFC; animation:rec-pulse-dot 1.8s ease-out infinite; }
  @keyframes rec-pulse-dot {
    0%   { box-shadow:0 0 0 0 rgba(124,92,252,.5); }
    70%  { box-shadow:0 0 0 6px rgba(124,92,252,0); }
    100% { box-shadow:0 0 0 0 rgba(124,92,252,0); }
  }
  .rec-prog-chip {
    width:38px; height:38px; margin:0 auto 9px; display:grid; place-items:center; border-radius:11px;
    color:#6D28D9; background:linear-gradient(160deg,rgba(124,92,252,.18),rgba(124,92,252,.08));
    border:1px solid #DED9F2; transition:transform .22s cubic-bezier(.2,.7,.3,1);
  }
  .rec-prog-card:hover .rec-prog-chip { transform:translateY(-1px) scale(1.06); }
  .rec-prog-num { font-size:30px; font-weight:800; line-height:1; letter-spacing:-.02em; color:#1E1B2E; font-variant-numeric:tabular-nums; }
  .rec-prog-label { margin-top:6px; font-size:10.5px; font-weight:600; letter-spacing:.05em; text-transform:uppercase; color:#6B6785; }
  .rec-prog-hint { margin-top:2px; font-size:10px; color:#9A96B5; }
  .rec-prog-bar { position:absolute; left:12px; right:12px; bottom:9px; height:4px; border-radius:99px; background:linear-gradient(90deg,#7C5CFC,#6D28D9); opacity:.9; }
  @media (prefers-reduced-motion: reduce) {
    .rec-prog-card, .rec-prog-chip { transition:none; }
    .rec-prog-tag i { animation:none; }
  }

  /* Tabs */
  .rec-tabs { display:flex; gap:26px; padding:0 28px; background:#fff; border-bottom:1px solid var(--line); }
  .rec-tab { background:none; border:none; cursor:pointer; padding:13px 2px 12px; font-size:14px; font-weight:600; color:var(--muted); border-bottom:2.5px solid transparent; margin-bottom:-1px; transition:color .15s; }
  .rec-tab:hover { color:var(--text); }
  .rec-tab.is-active { color:var(--primary); border-bottom-color:var(--primary); }

  /* Content */
  .rec-content { padding:24px 28px 40px; overflow-y:auto; flex:1; }
  .rec-grid { display:grid; gap:18px; }
  .rec-grid--2 { grid-template-columns:1fr 1fr; }
  .rec-panel, .rec-card { background:#fff; border:1px solid var(--line); border-radius:14px; padding:20px; }
  .rec-panel.is-invalid, .rec-card.is-invalid { border-color:#FCA5A5; box-shadow:0 0 0 3px rgba(220,38,38,.08); }
  .rec-panel-head { font-size:11px; letter-spacing:1.2px; font-weight:700; text-transform:uppercase; color:var(--muted); padding-bottom:14px; margin-bottom:14px; border-bottom:1px solid var(--line); }

  /* Sources */
  .rec-sources { display:flex; flex-direction:column; }
  .rec-source { display:flex; align-items:center; gap:13px; padding:12px 0; border-bottom:1px solid #F1F5F9; }
  .rec-source:last-child { border-bottom:none; }
  .rec-source-text { flex:1; min-width:0; }
  .rec-source-name { font-weight:600; font-size:14.5px; }
  .rec-source-sub { font-size:12.5px; color:var(--muted); margin-top:2px; }
  .rec-on { color:var(--green); font-weight:600; }
  .rec-off { color:#94A3B8; }

  /* Toggle */
  .rec-toggle { width:42px; height:24px; border-radius:999px; border:none; background:#CBD5E1; position:relative; cursor:pointer; flex:0 0 auto; transition:background .18s; padding:0; }
  .rec-toggle.is-on { background:var(--green); }
  .rec-toggle-knob { position:absolute; top:3px; left:3px; width:18px; height:18px; border-radius:50%; background:#fff; box-shadow:0 1px 2px rgba(0,0,0,.25); transition:left .18s; }
  .rec-toggle.is-on .rec-toggle-knob { left:21px; }

  /* Notes */
  .rec-note { display:flex; gap:9px; align-items:flex-start; padding:12px 14px; border-radius:10px; font-size:12.8px; line-height:1.5; margin-top:16px; }
  .rec-note svg { flex:0 0 auto; margin-top:1px; }
  .rec-note--info { background:#EFF6FF; color:#1E40AF; border:1px solid #BFDBFE; margin-top:18px; }
  .rec-note--error { background:#FEF2F2; color:#B91C1C; border:1px solid #FCA5A5; }
  .rec-note code { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px; background:#fff; border:1px solid #BFDBFE; border-radius:5px; padding:1px 6px; color:#1E3A8A; }

  /* Fields */
  .rec-field-row { display:flex; gap:14px; }
  .rec-field { flex:1; min-width:0; display:flex; flex-direction:column; }
  .rec-field--full { display:flex; flex-direction:column; margin-top:18px; }
  .rec-field label, .rec-field--full label { font-size:12.5px; font-weight:600; color:#475569; margin-bottom:6px; }
  .rec-input { width:100%; height:40px; padding:0 12px; border:1px solid var(--line); border-radius:9px; font-size:14px; color:var(--text); background:#fff; outline:none; transition:border-color .15s,box-shadow .15s; }
  .rec-input:focus { border-color:var(--primary); box-shadow:0 0 0 3px rgba(37,99,235,.12); }

  /* Select */
  .rec-select { position:relative; }
  .rec-select select { width:100%; height:40px; padding:0 36px 0 12px; border:1px solid var(--line); border-radius:9px; font-size:14px; color:var(--text); background:#fff; appearance:none; -webkit-appearance:none; cursor:pointer; outline:none; transition:border-color .15s,box-shadow .15s; }
  .rec-select select:focus { border-color:var(--primary); box-shadow:0 0 0 3px rgba(37,99,235,.12); }
  .rec-select-caret { position:absolute; right:11px; top:50%; transform:translateY(-50%); color:#94A3B8; pointer-events:none; }

  /* Time */
  .rec-time { position:relative; }
  .rec-time input { width:100%; height:40px; padding:0 36px 0 12px; border:1px solid var(--line); border-radius:9px; font-size:14px; color:var(--text); background:#fff; outline:none; transition:border-color .15s,box-shadow .15s; }
  .rec-time input:focus { border-color:var(--primary); box-shadow:0 0 0 3px rgba(37,99,235,.12); }
  .rec-time-icon { position:absolute; right:11px; top:50%; transform:translateY(-50%); color:#94A3B8; pointer-events:none; }

  /* Cards */
  .rec-card-title { font-size:15.5px; font-weight:700; margin-bottom:4px; }
  .rec-card-desc { font-size:12.8px; color:var(--muted); margin:0 0 14px; }
  .rec-req { color:#DC2626; margin-left:3px; font-weight:700; }
  .rec-error { margin-top:12px; font-size:12.5px; font-weight:600; color:#DC2626; }
  .rec-validation { display:inline-flex; align-items:center; gap:5px; align-self:center; font-size:12.5px; font-weight:600; color:#DC2626; margin-right:4px; }

  /* Chips */
  .rec-chips { display:flex; flex-wrap:wrap; gap:9px; }
  .rec-chip { border:1px solid var(--line); background:#fff; color:#475569; padding:8px 15px; border-radius:999px; font-size:13px; font-weight:600; cursor:pointer; transition:all .14s; }
  .rec-chip:hover { border-color:#CBD5E1; background:#F8FAFC; }
  .rec-chip.is-active.rec-chip--green { background:var(--green-bg); border-color:var(--green-bd); color:#047857; }
  .rec-chip.is-active.rec-chip--amber { background:#FFFBEB; border-color:#FCD34D; color:#B45309; }

  /* Inline toggle */
  .rec-inline-toggle { display:flex; align-items:center; gap:11px; margin-top:18px; font-size:13.5px; color:#475569; }

  /* Section label */
  .rec-section-label { font-size:11px; letter-spacing:1.3px; font-weight:700; text-transform:uppercase; color:var(--muted); margin:30px 0 16px; }

  /* Placeholder */
  .rec-placeholder { text-align:center; padding:70px 24px; color:var(--muted); border:1px dashed var(--line); border-radius:14px; background:#fff; }
  .rec-placeholder svg { color:#94A3B8; margin-bottom:12px; }
  .rec-placeholder h3 { margin:0 0 8px; color:var(--text); font-size:17px; }
  .rec-placeholder p { max-width:440px; margin:0 auto; font-size:13.5px; line-height:1.6; }

  /* Spinner */
  .rec-spin { animation:rec-rot 0.9s linear infinite; }
  @keyframes rec-rot { to { transform:rotate(360deg); } }

  @media (max-width:880px) {
    .rec-grid--2 { grid-template-columns:1fr; }
    .rec-header { flex-direction:column; }
  }
`;
