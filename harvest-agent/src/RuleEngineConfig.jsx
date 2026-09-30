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
import LiveBrowserView from "./components/LiveBrowserView";
import StopHarvestButton from "./components/StopHarvestModal";
import { GLASS, GLASS_FALLBACK, GLASS_INPUT, GLASS_INPUT_FOCUS } from "./theme";
import {
  JOB_TYPES, WORK_MODES, HIRING_ENTITIES, GCC_MODES, SEARCH_WINDOWS,
  LINKEDIN_ACCOUNTS, TIMEZONES, CURRENCIES, fmtRunDate, nowLabel,
  IT_JOB_CATEGORIES, NON_IT_JOB_CATEGORIES, COMPANY_SIZE_RANGES,
} from "./lib/ruleEngineOptions";
import SearchableMultiSelect from "./components/SearchableMultiSelect";
import useCountUp from "./useCountUp";
import { makeStallWatch, STALL_WARN_MSG } from "./stallWatch";

/* Glyph badge for a source/account tile — identical to the US redesign's
   rr-src-ic (colored rounded square with a short glyph), replacing the old
   per-source SVG logos so both clients render the same. */
function SourceBadge({ glyph, tint }) {
  return <span className="rec-src-ic" style={{ background: tint }} aria-hidden="true">{glyph}</span>;
}

/* Option lists + label helpers now live in lib/ruleEngineOptions, shared with
   RuleEngineRedesign so the two rule engine pages can't drift. Frequency is a
   local segmented control mirroring the US redesign (hourly/daily/weekly). */
const FREQUENCIES = ["hourly", "daily", "weekly"];

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

// Enter-to-add removable-chip text input, used by the Job Domain card's
// "Others" custom job title/search-term field.
function TagInput({ tags, onChange, placeholder }) {
  const [text, setText] = useState("");
  const commit = () => {
    const v = text.trim();
    if (v && !tags.includes(v)) onChange([...tags, v]);
    setText("");
  };
  return (
    <div className="rec-tags">
      {tags.map((t) => (
        <span className="rec-tag" key={t}>
          {t}
          <button type="button" aria-label={`Remove ${t}`} onClick={() => onChange(tags.filter((x) => x !== t))}>×</button>
        </span>
      ))}
      <input className="rec-tag-input" type="text" value={text} placeholder={tags.length ? "" : placeholder}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") { e.preventDefault(); commit(); }
          else if (e.key === "Backspace" && !text && tags.length) onChange(tags.slice(0, -1));
        }}
        onBlur={commit} />
    </div>
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

function Card({ title, desc, required, invalid, error, span = false, children }) {
  return (
    <div className={"rec-card" + (span ? " rec-span" : "") + (invalid ? " is-invalid" : "")}>
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

const DEFAULT_CONFIG = {
  sources: { linkedin: true, naukri: false, dice: false, linkedin_account: "1" },
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

  // `sources` holds ONLY the three source toggles — the selected LinkedIn
  // account lives in its own state (linkedinAccount) so its truthy string can't
  // leak into the `errors.jobSource` "at least one source enabled" check.
  const [sources, setSources] = useState({ linkedin: true, naukri: false, dice: false });
  const [linkedinAccount, setLinkedinAccount] = useState(DEFAULT_CONFIG.sources.linkedin_account);
  const [keyword, setKeyword] = useState(DEFAULT_CONFIG.filters.keyword);
  const [location, setLocation] = useState(DEFAULT_CONFIG.filters.location);
  const [jobType, setJobType] = useState(DEFAULT_CONFIG.filters.job_type);
  const [workMode, setWorkMode] = useState(DEFAULT_CONFIG.filters.work_mode);
  const [frequency, setFrequency] = useState(DEFAULT_CONFIG.schedule.frequency);
  const [runTime, setRunTime] = useState(DEFAULT_CONFIG.schedule.run_time);
  const [timezone, setTimezone] = useState(DEFAULT_CONFIG.schedule.timezone);
  const [scheduleEnabled, setScheduleEnabled] = useState(DEFAULT_CONFIG.schedule.enabled);
  const [searchWindow, setSearchWindow] = useState(DEFAULT_CONFIG.filters.search_window_hours);
  // Job Domain redesign — primary selector + its two independent category
  // lists (switching back and forth keeps each one's in-progress edits) +
  // custom titles for "Others". Company Size is local-only (never persisted;
  // see the RuleEngineConfig redesign plan) so it isn't loaded/saved at all.
  const [jobDomain, setJobDomain] = useState("IT"); // "IT" | "Non-IT" | "Others"
  const [itCategories, setItCategories] = useState(IT_JOB_CATEGORIES);
  const [nonItCategories, setNonItCategories] = useState(NON_IT_JOB_CATEGORIES);
  const [customJobTitles, setCustomJobTitles] = useState([]);
  const [companySizes, setCompanySizes] = useState(["ALL"]);
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
  // DEV ONLY: flip to true to force the progress cards visible so you can style
  // them without an active run. Set back to false to restore normal behavior.
  const PREVIEW_PROG = false;
  const [runMessage, setRunMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [dirty, setDirty] = useState(false);
  const [attempted, setAttempted] = useState(false);

  const pollTimer = useRef(null);
  const stallWatch = useRef(makeStallWatch());
  const [stallWarn, setStallWarn] = useState(""); // amber "no progress" notice while a run is stalled
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
    // Spread only the source toggles — never `linkedin_account` (see the
    // `sources` state comment) — then load the selected account separately.
    setSources({
      linkedin: !!config.sources.linkedin,
      naukri:   !!config.sources.naukri,
      dice:     !!config.sources.dice,
    });
    setLinkedinAccount(config.sources?.linkedin_account || "1");
    setKeyword(config.filters.keyword || "");
    setLocation(config.filters.location || "");
    setJobType(config.filters.job_type || "Any");
    setWorkMode(config.filters.work_mode || "Any");
    const loadedDomain = config.filters.domain;
    const loadedJobDomain = loadedDomain === "IT" ? "IT" : loadedDomain === "Non-IT" ? "Non-IT" : "Others";
    setJobDomain(loadedJobDomain);
    const loadedCategories = config.filters.job_categories || [];
    setItCategories(loadedJobDomain === "IT" && loadedCategories.length ? loadedCategories : IT_JOB_CATEGORIES);
    setNonItCategories(loadedJobDomain === "Non-IT" && loadedCategories.length ? loadedCategories : NON_IT_JOB_CATEGORIES);
    setCustomJobTitles(config.filters.custom_job_titles || []);
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
    setScheduleEnabled(!!config.schedule.enabled);
    setDirty(false);
  }

  const errors = {
    jobSource: !(sources.linkedin || sources.naukri || sources.dice),
    jobType: !jobType,
    jobDomainCategory:
      jobDomain === "IT" ? itCategories.length === 0
      : jobDomain === "Non-IT" ? nonItCategories.length === 0
      : customJobTitles.length === 0,
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
      sources: { ...sources, linkedin_account: linkedinAccount },
      filters: {
        ...loadedConfig.filters,
        keyword: keyword.trim(),
        location: location.trim(),
        job_type: jobType,
        work_mode: workMode,
        search_window_hours: Number(searchWindow),
        domain: jobDomain === "IT" ? "IT" : jobDomain === "Non-IT" ? "Non-IT" : "Any",
        job_categories: jobDomain === "IT" ? itCategories : jobDomain === "Non-IT" ? nonItCategories : [],
        custom_job_titles: jobDomain === "Others" ? customJobTitles : [],
        hiring_entity: hiringEntity,
        gcc_mode: gccMode,
        salary_min: salaryMin === "" ? null : Number(salaryMin),
        salary_max: salaryMax === "" ? null : Number(salaryMax),
        salary_currency: currency,
        include_undisclosed_salary: includeUndisclosed,
      },
      schedule: { ...loadedConfig.schedule, frequency, run_time: runTime, timezone, enabled: scheduleEnabled },
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
          const live = status.jobs_saved_today ?? status.combined ?? 0;
          // `jobs_saved_today` is a live DB count of scraped_jobs rows persisted
          // today (across all runs), so it reflects real saved rows and climbs as
          // each batch inserts. Fall back to `combined` (this run's count) if the
          // backend didn't send it.
          setHarvestedLive(live);
          // Daily cap (MAX_JOBS_PER_DAY) drives the Max / Remaining cards.
          setMaxPerDay(status.max_jobs_per_day ?? 0);
          // Watchdog: if message + saved-count haven't changed for 2 min the run
          // is stalled (e.g. LLM down, waiting out its timeout) — warn without
          // failing, since the backend stays the source of truth for terminal state.
          const { stalled } = stallWatch.current.note(`${status.message || ""}|${live}`);
          setStallWarn(stalled ? STALL_WARN_MSG : "");
          // Poll every 6s while running so the live count visibly ticks without
          // hammering the server.
          pollTimer.current = setTimeout(tick, 6000);
          return;
        }
        setStallWarn("");
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
            setStallWarn("");
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
        setStallWarn("");
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
          stallWatch.current.reset();
          setStallWarn("");
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
      // 409 = the backend rejected because a run is already in flight — keep the
      // controls frozen rather than clearing them; any other error unfreezes.
      setHarvestRunning(err instanceof ApiError && err.status === 409);
    }
  };

  // Per-account setup status, keyed by account id ("1" | "2").
  const [linkedinSetup, setLinkedinSetup] = useState({
    "1": { loading: false, message: "" },
    "2": { loading: false, message: "" },
  });
  const [naukriSetup, setNaukriSetup] = useState({ loading: false, message: "" });
  const [liveViewSource, setLiveViewSource] = useState(null); // "linkedin" | "naukri" | null

  const setAcctStatus = (accountId, patch) =>
    setLinkedinSetup((s) => ({ ...s, [accountId]: { ...s[accountId], ...patch } }));

  const handleLinkedinSetup = async (accountId = "1") => {
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

  // Glyph + tint per source — mirrors the US redesign's SOURCES exactly.
  // Priorities stay India-first (Naukri #1); only the visual treatment matches.
  const sourceList = [
    { key: "naukri",   name: "Naukri.com",   priority: 1, glyph: "N",  tint: "#6D28D9" },
    { key: "linkedin", name: "LinkedIn Jobs", priority: 2, glyph: "in", tint: "#2563EB" },
    { key: "dice",     name: "Dice.com",      priority: 3, glyph: "⬢",  tint: "#0EA5A4" },
  ];
  const activeCount = sourceList.filter((s) => sources[s.key]).length;

  // The backend pauses a running harvest and waits for manual LinkedIn login
  // (see LinkedInAgent._wait_for_manual_login) — surface that clearly so the
  // user knows to click "Watch Live Browser" instead of just waiting.
  const needsLogin = runState === "running" && /log ?in/i.test(runMessage || "");

  // Labels for the compact schedule summary banner (mirrors the US redesign).
  const tzLabel = TIMEZONES.find((t) => t.value === timezone)?.label || timezone;
  const windowLabel = SEARCH_WINDOWS.find((w) => w.value === Number(searchWindow))?.label || `${searchWindow}h`;

  return (
    // Rendered inside the shared AppLayout (ha-root → Sidebar → Outlet), so this
    // screen no longer draws its own root wrapper or Sidebar — just its main
    // column (rec-main is flex:1, so it fills the space beside the shared nav).
    <>
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
              {runState === "running" && stallWarn && (
                <>
                  <span className="rec-dot">·</span>
                  <span className="rec-status rec-status--warn" style={{ color: "#B45309" }}>
                    <AlertTriangle size={14} /> {stallWarn}
                  </span>
                </>
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

        {(runState === "running" || PREVIEW_PROG) && (
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
                {/* Job Sources — styled to match the US redesign exactly:
                    "N of N active" count, gradient active tile, glyph badge,
                    priority chip + Active/Paused pill. */}
                <div className={"rec-panel" + (showErr("jobSource") ? " is-invalid" : "")}>
                  <div className="rec-panel-head">
                    <span>Job Sources <span className="rec-req">*</span></span>
                    <small>{activeCount} of {sourceList.length} active</small>
                  </div>
                  <div className="rec-sources">
                    {sourceList.map(({ key, name, priority, glyph, tint }) => {
                      const on = sources[key];
                      return (
                        <div className={"rec-source" + (on ? " is-on" : "")} key={key}>
                          <SourceBadge glyph={glyph} tint={tint} />
                          <div className="rec-source-text">
                            <div className="rec-source-name">{name}</div>
                            <div className="rec-source-sub">
                              <span className="rec-prio">Priority {priority}</span>
                              <span className={"rec-statuschip " + (on ? "is-on" : "is-off")}>{on ? "Active" : "Paused"}</span>
                            </div>
                          </div>
                          <Toggle on={on} onChange={(v) => setSource(key, v)} label={name} />
                        </div>
                      );
                    })}
                  </div>
                  {showErr("jobSource") && <div className="rec-error">Enable at least one job source.</div>}
                </div>

                {/* Run Schedule — compact summary banner + segmented frequency +
                    automatic-runs toggle, matching the US redesign. */}
                <div className="rec-panel">
                  <div className="rec-panel-head">Run Schedule</div>
                  <div className="rec-banner">
                    <span className="rec-banner-ic"><Clock size={17} /></span>
                    <span>Runs <b>{frequency} at {runTime || "—"}</b> ({tzLabel}) · looks back <b>{windowLabel.toLowerCase()}</b></span>
                  </div>
                  <div className="rec-field rec-field--full" style={{ marginTop: 0 }}>
                    <label>Frequency</label>
                    <div className="rec-seg" role="group" aria-label="Frequency">
                      {FREQUENCIES.map((f) => (
                        <button key={f} type="button" className={frequency === f ? "is-on" : ""}
                          aria-pressed={frequency === f}
                          onClick={() => { setFrequency(f); markDirty(); }}>
                          {f[0].toUpperCase() + f.slice(1)}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div className="rec-field-row" style={{ marginTop: 14 }}>
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
                  <div className="rec-tglrow">
                    <span className="rec-tglrow-tx">
                      <b>Automatic scheduled runs</b>
                      <small>Run the harvest automatically on the schedule above.</small>
                    </span>
                    <Toggle on={scheduleEnabled} onChange={(v) => { setScheduleEnabled(v); markDirty(); }} label="Automatic scheduled runs" />
                  </div>
                  {/* <div className="rec-field-row" style={{ marginTop: 14 }}>
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
                  </div> */}
                </div>
              </div>

              <div className="rec-panel" style={{ marginTop: 18 }}>
                <div className="rec-panel-head">Connect Accounts</div>
                <p className="rec-card-desc" style={{ marginBottom: 16 }}>
                  Log in once per account in the browser window that opens — the session is saved on the server
                  and reused on every future harvest run. LinkedIn supports two accounts, each with its own saved
                  session; the <strong>Active LinkedIn account</strong> below is the one every harvest run uses.
                </p>

                {/* Active LinkedIn account selector — persisted in harvest_config (sources.linkedin_account) */}
                <div className="rec-field" style={{ maxWidth: 320, marginBottom: 16 }}>
                  <label>Active LinkedIn account (used for harvest runs)</label>
                  <Select
                    value={LINKEDIN_ACCOUNTS.find((a) => a.id === linkedinAccount)?.label || "Account 1"}
                    onChange={(label) => { setLinkedinAccount(LINKEDIN_ACCOUNTS.find((a) => a.label === label).id); markDirty(); }}
                    ariaLabel="Active LinkedIn account"
                    options={LINKEDIN_ACCOUNTS.map((a) => a.label)}
                  />
                </div>

                {/* Two LinkedIn account cards — each connects/saves its own session */}
                <div className="rec-field-row">
                  {LINKEDIN_ACCOUNTS.map((a) => {
                    const st = linkedinSetup[a.id] || {};
                    const isActive = linkedinAccount === a.id;
                    return (
                      <div key={a.id} className="rec-source"
                        style={{ flex: 1, border: isActive ? "1px solid #0A66C2" : "1px solid var(--line)",
                                 background: isActive ? "rgba(10,102,194,0.06)" : undefined,
                                 borderRadius: 10, padding: "12px 14px" }}>
                        <SourceBadge glyph="in" tint="#2563EB" />
                        <div className="rec-source-text">
                          <div className="rec-source-name">
                            LinkedIn · {a.label}{isActive && <span className="rec-on"> · active</span>}
                          </div>
                          <div className="rec-source-sub">{st.message || "Not connected in this session"}</div>
                        </div>
                        <button type="button" className="rec-btn rec-btn--save"
                          onClick={() => handleLinkedinSetup(a.id)} disabled={st.loading}>
                          {st.loading ? <Loader2 size={16} className="rec-spin" /> : <LogIn size={16} />}
                          {st.loading ? "Waiting…" : "Connect"}
                        </button>
                      </div>
                    );
                  })}
                </div>

                {/* Naukri (single account) */}
                <div className="rec-field-row" style={{ marginTop: 12 }}>
                  <div className="rec-source" style={{ flex: 1, border: "1px solid var(--line)", borderRadius: 10, padding: "12px 14px" }}>
                    <SourceBadge glyph="N" tint="#6D28D9" />
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

              {/* Even rows: Job type ↔ Hiring entity share a row, the tall
                  Domain chip set spans full width, GCC ↔ Location pair up, and
                  Salary spans full width (matches the approved mockup). */}
              <div className="rec-grid rec-grid--2">
                <Card title="Job type" required invalid={showErr("jobType")} desc="Pushed into each source's search (e.g. LinkedIn f_JT). Non-matching jobs are flagged after scraping, not dropped.">
                  <div className="rec-chips">
                    {JOB_TYPES.map((t) => (
                      <Chip key={t} active={jobType === t} onClick={() => { setJobType(t); markDirty(); }}>{t}</Chip>
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

                <Card title="Job Domain" required invalid={showErr("jobDomainCategory")}
                  desc="Select the job domain and specific job categories to include in harvesting."
                  error={
                    jobDomain === "IT" ? "Select at least one IT job category."
                    : jobDomain === "Non-IT" ? "Select at least one Non-IT job category."
                    : "Enter at least one job title or search term."
                  }>
                  <div className="rec-chips" style={{ marginBottom: 14 }}>
                    {["IT", "Non-IT", "Others"].map((t) => (
                      <Chip key={t} active={jobDomain === t} onClick={() => { setJobDomain(t); markDirty(); }}>{t}</Chip>
                    ))}
                  </div>

                  {jobDomain === "IT" && (
                    <div className="rec-field">
                      <label>IT Job Categories</label>
                      <SearchableMultiSelect
                        options={IT_JOB_CATEGORIES}
                        selected={itCategories}
                        onChange={(next) => { setItCategories(next); markDirty(); }}
                        allLabel="All IT Jobs"
                        selectAllLabel="Select All IT Jobs"
                        searchPlaceholder="Search IT job categories..."
                        ariaLabel="IT Job Categories"
                      />
                    </div>
                  )}
                  {jobDomain === "Non-IT" && (
                    <div className="rec-field">
                      <label>Non-IT Job Categories</label>
                      <SearchableMultiSelect
                        options={NON_IT_JOB_CATEGORIES}
                        selected={nonItCategories}
                        onChange={(next) => { setNonItCategories(next); markDirty(); }}
                        allLabel="All Non-IT Jobs"
                        selectAllLabel="Select All Non-IT Jobs"
                        searchPlaceholder="Search Non-IT job categories..."
                        ariaLabel="Non-IT Job Categories"
                      />
                    </div>
                  )}
                  {jobDomain === "Others" && (
                    <div className="rec-field">
                      <label>Job Title / Search Term</label>
                      <TagInput tags={customJobTitles}
                        onChange={(next) => { setCustomJobTitles(next); markDirty(); }}
                        placeholder="e.g. Marine Engineer, Geologist, Fashion Designer" />
                      <p className="rec-card-desc" style={{ marginTop: 8, marginBottom: 0 }}>
                        Enter the specific job title or search term you want to harvest.
                      </p>
                    </div>
                  )}

                  <div className="rec-note rec-note--info" style={{ marginTop: 16 }}>
                    <Info size={16} />
                    <span>Categories/titles are saved for reference; only the IT / Non-IT / Any selection above narrows the live search today.</span>
                  </div>
                </Card>

                <Card title="Company Size" desc="Filter jobs based on the hiring company&rsquo;s approximate employee count.">
                  <div className="rec-chips">
                    <Chip active={companySizes.includes("ALL")} onClick={() => setCompanySizes(["ALL"])}>All Sizes</Chip>
                    {COMPANY_SIZE_RANGES.map((r) => (
                      <Chip key={r.value} active={companySizes.includes(r.value)}
                        onClick={() => {
                          setCompanySizes((prev) => {
                            const withoutAll = prev.filter((s) => s !== "ALL");
                            const next = withoutAll.includes(r.value)
                              ? withoutAll.filter((s) => s !== r.value)
                              : [...withoutAll, r.value];
                            return next.length === 0 ? ["ALL"] : next;
                          });
                        }}>{r.label}</Chip>
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

                <Card span title="Salary / Budget" desc="Checked after scraping against the posting's disclosed salary. Out-of-range jobs are flagged, not dropped.">
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
                      <Select value={currency} onChange={(v) => { setCurrency(v); markDirty(); }} ariaLabel="Currency" options={CURRENCIES} />
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
    </>
  );
}

const styles = `
  /* Design tokens live on .rec-main (the component's rendered root) rather than a
     .rec-root wrapper — this screen renders inside the shared AppLayout and no
     longer draws its own root element, so scoping the vars here keeps every
     var(--…) resolving for the whole subtree (buttons/borders included). */
  .rec-main * { box-sizing:border-box; }

  /* Sidebar styles now live in Sidebar.jsx (the shared component). */

  /* Main */
  .rec-main {
    --primary:#2563EB; --secondary:#1E40AF; --accent:#F59E0B;
    --bg:#F8FAFC; --text:#1E293B; --muted:#64748B; --line:#E2E8F0;
    --green:#16A34A; --green-bg:#ECFDF5; --green-bd:#86EFAC;
    --sidebar:#0F172A;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    color:var(--text); background:transparent; /* ha-root's PAGE_BG gradient shows through */
    font-size:14px; line-height:1.45; -webkit-font-smoothing:antialiased;
    flex:1; min-width:0; display:flex; flex-direction:column; overflow:hidden;
  }
  .rec-header { display:flex; justify-content:space-between; align-items:flex-start; gap:18px; padding:15px 20px 16px;
    background:linear-gradient(160deg, rgba(255,255,255,.88), rgba(255,255,255,.58) 72%);
    -webkit-backdrop-filter:blur(12px) saturate(150%); backdrop-filter:blur(12px) saturate(150%);
    border-bottom:1px solid rgba(255,255,255,.75); box-shadow:inset 0 1px 0 rgba(255,255,255,.7); }
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
  .rec-btn--watch { background:linear-gradient(160deg, rgba(255,255,255,.9), rgba(255,255,255,.65)); color:var(--primary); border:1px solid var(--primary); box-shadow:inset 0 1px 0 rgba(255,255,255,.75); }
  .rec-btn--watch:hover { background:rgba(239,246,255,.92); }
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
  /* Full-width white band flush with the header/tabs gutters (28px) so the
     stats read as part of the page, not a boxed cluster on a grey strip. */
  .rec-prog { margin:0; padding:18px 28px; background:rgba(255,255,255,.55); display:grid; gap:16px; grid-template-columns:repeat(3,minmax(0,1fr)); }
  @media (max-width:640px) { .rec-prog { grid-template-columns:1fr; } }
  .rec-prog-card {
    position:relative; overflow:hidden; text-align:center;
    background:linear-gradient(180deg,#F6F3FF 42%,#fff 72%),#fff;
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
  .rec-tabs { display:flex; gap:26px; padding:0 28px;
    background:linear-gradient(160deg, rgba(255,255,255,.7), rgba(255,255,255,.45));
    -webkit-backdrop-filter:blur(12px) saturate(150%); backdrop-filter:blur(12px) saturate(150%);
    border-bottom:1px solid rgba(255,255,255,.7); }
  .rec-tab { background:none; border:none; cursor:pointer; padding:13px 2px 12px; font-size:14px; font-weight:600; color:var(--muted); border-bottom:2.5px solid transparent; margin-bottom:-1px; transition:color .15s; }
  .rec-tab:hover { color:var(--text); }
  .rec-tab.is-active { color:var(--primary); border-bottom-color:var(--primary); }

  /* Content */
  .rec-content { padding:24px 28px 40px; overflow-y:auto; flex:1; }
  .rec-grid { display:grid; gap:14px; }
  .rec-grid--2 { grid-template-columns:1fr 1fr; }
  .rec-span { grid-column:1/-1; }
  /* Compact, tight-fitting cards — matches the tightened US redesign spacing. */
  .rec-panel, .rec-card { ${GLASS} border-radius:14px; padding:16px 18px; }
  .rec-panel.is-invalid, .rec-card.is-invalid { border-color:#FCA5A5; box-shadow:0 0 0 3px rgba(220,38,38,.08); }
  /* Card header mirrors the US rr-card-h: uppercase label left, optional count
     right, no divider rule. */
  .rec-panel-head { display:flex; align-items:center; justify-content:space-between; gap:10px; font-size:12px; letter-spacing:.1em; font-weight:700; text-transform:uppercase; color:var(--muted); margin-bottom:14px; }
  .rec-panel-head small { font-size:11.5px; letter-spacing:0; text-transform:none; font-weight:600; color:#94A3B8; }

  /* Sources — matched to the US redesign exactly: glyph badge, gradient active
     tile, priority chip + Active/Paused pill. */
  .rec-sources { display:flex; flex-direction:column; }
  .rec-source { display:flex; align-items:center; gap:13px; padding:10px 12px; border:1px solid rgba(203,213,225,.7); border-radius:11px; background:rgba(255,255,255,.65); margin-bottom:8px; transition:border-color .18s, box-shadow .18s, background .18s; }
  .rec-source:last-child { margin-bottom:0; }
  /* Side-by-side cards use the row's gap for spacing; drop the stacking margin so
     align-items:stretch gives both cards equal height (the non-last card would
     otherwise stretch 8px shorter). */
  .rec-field-row .rec-source { margin-bottom:0; }
  .rec-source:hover { border-color:#CBD5E1; box-shadow:0 1px 2px rgba(15,23,42,.05); }
  .rec-source.is-on { border-color:#86EFAC; background:linear-gradient(90deg,rgba(236,253,245,.9),rgba(255,255,255,.5) 60%); }
  .rec-src-ic { width:38px; height:38px; border-radius:10px; display:grid; place-items:center; color:#fff; font-weight:800; font-size:15px; flex:none; }
  .rec-source-text { flex:1; min-width:0; }
  .rec-source-name { font-weight:600; font-size:14.5px; }
  .rec-source-sub { display:flex; align-items:center; gap:8px; margin-top:3px; }
  .rec-prio { font-size:10.5px; font-weight:700; color:var(--muted); background:rgba(241,245,249,.9); border-radius:999px; padding:1px 8px; }
  .rec-statuschip { font-size:11px; font-weight:700; padding:1px 9px; border-radius:999px; }
  .rec-statuschip.is-on { background:#ECFDF5; color:#047857; }
  .rec-statuschip.is-off { background:#F1F5F9; color:#64748B; }
  .rec-on { color:var(--green); font-weight:600; }
  .rec-off { color:#94A3B8; }

  /* Toggle — same dimensions as the US rr-toggle. */
  .rec-toggle { width:44px; height:25px; border-radius:999px; border:none; background:#CBD5E1; position:relative; cursor:pointer; flex:0 0 auto; transition:background .2s; padding:0; }
  .rec-toggle.is-on { background:var(--green); }
  .rec-toggle-knob { position:absolute; top:2.5px; left:2.5px; width:20px; height:20px; border-radius:50%; background:#fff; box-shadow:0 1px 3px rgba(0,0,0,.3); transition:transform .22s cubic-bezier(.34,1.56,.64,1); }
  .rec-toggle.is-on .rec-toggle-knob { transform:translateX(19px); }

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
  .rec-input { width:100%; height:40px; padding:0 12px; ${GLASS_INPUT} border-radius:9px; font-size:14px; color:var(--text); outline:none; transition:border-color .15s,box-shadow .15s,background .15s; }
  .rec-input:focus { border-color:var(--primary); box-shadow:0 0 0 3px rgba(37,99,235,.12); ${GLASS_INPUT_FOCUS} }

  /* Select */
  .rec-select { position:relative; }
  .rec-select select { width:100%; height:40px; padding:0 36px 0 12px; ${GLASS_INPUT} border-radius:9px; font-size:14px; color:var(--text); appearance:none; -webkit-appearance:none; cursor:pointer; outline:none; transition:border-color .15s,box-shadow .15s,background .15s; }
  .rec-select select:focus { border-color:var(--primary); box-shadow:0 0 0 3px rgba(37,99,235,.12); ${GLASS_INPUT_FOCUS} }
  .rec-select-caret { position:absolute; right:11px; top:50%; transform:translateY(-50%); color:#94A3B8; pointer-events:none; }

  /* Time */
  .rec-time { position:relative; }
  .rec-time input { width:100%; height:40px; padding:0 36px 0 12px; ${GLASS_INPUT} border-radius:9px; font-size:14px; color:var(--text); outline:none; transition:border-color .15s,box-shadow .15s,background .15s; }
  .rec-time input:focus { border-color:var(--primary); box-shadow:0 0 0 3px rgba(37,99,235,.12); ${GLASS_INPUT_FOCUS} }
  .rec-time-icon { position:absolute; right:11px; top:50%; transform:translateY(-50%); color:#94A3B8; pointer-events:none; }

  /* Compact schedule summary banner (mirrors the US redesign's rr-banner) */
  .rec-banner { display:flex; align-items:center; gap:12px; background:rgba(239,246,255,.85); border:1px solid #BFDBFE; border-radius:12px; padding:13px 15px; margin-bottom:18px; font-size:13.5px; color:#1E40AF; }
  .rec-banner b { font-weight:800; text-transform:capitalize; }
  .rec-banner-ic { width:34px; height:34px; border-radius:10px; background:#fff; border:1px solid #BFDBFE; display:grid; place-items:center; color:var(--primary); flex:none; }

  /* Segmented frequency control */
  .rec-seg { display:flex; background:rgba(241,245,249,.9); border-radius:9px; padding:3px; }
  .rec-seg button { flex:1; border:none; background:none; font-family:inherit; font-size:13px; font-weight:600; color:var(--muted); padding:8px; border-radius:7px; cursor:pointer; transition:.15s; }
  .rec-seg button.is-on { background:#fff; color:var(--primary); box-shadow:0 1px 2px rgba(15,23,42,.08); }

  /* Automatic-runs toggle row */
  .rec-tglrow { display:flex; align-items:center; gap:12px; padding:12px 0 2px; border-top:1px solid rgba(226,232,240,.8); margin-top:16px; }
  .rec-tglrow-tx { flex:1; min-width:0; }
  .rec-tglrow-tx b { display:block; font-size:13.5px; }
  .rec-tglrow-tx small { color:var(--muted); font-size:12px; }

  /* Cards */
  .rec-card-title { font-size:15.5px; font-weight:700; margin-bottom:4px; }
  .rec-card-desc { font-size:12.8px; color:var(--muted); margin:0 0 14px; }
  .rec-req { color:#DC2626; margin-left:3px; font-weight:700; }
  .rec-error { margin-top:12px; font-size:12.5px; font-weight:600; color:#DC2626; }
  .rec-validation { display:inline-flex; align-items:center; gap:5px; align-self:center; font-size:12.5px; font-weight:600; color:#DC2626; margin-right:4px; }

  /* Chips */
  .rec-chips { display:flex; flex-wrap:wrap; gap:9px; }
  .rec-chip { border:1px solid rgba(148,163,184,.45); background:rgba(255,255,255,.75); color:#475569; padding:8px 15px; border-radius:999px; font-size:13px; font-weight:600; cursor:pointer; transition:all .14s; }
  .rec-chip:hover { border-color:#CBD5E1; background:rgba(248,250,252,.95); }
  .rec-chip.is-active.rec-chip--green { background:var(--green-bg); border-color:var(--green-bd); color:#047857; }
  .rec-chip.is-active.rec-chip--amber { background:#FFFBEB; border-color:#FCD34D; color:#B45309; }

  /* Inline toggle */
  .rec-inline-toggle { display:flex; align-items:center; gap:11px; margin-top:18px; font-size:13.5px; color:#475569; }

  /* Tag input — Others custom job title chips, mirrors .rec-chip's pill look */
  .rec-tags { display:flex; flex-wrap:wrap; align-items:center; gap:8px; min-height:40px; padding:6px 10px; ${GLASS_INPUT} border-radius:9px; }
  .rec-tags:focus-within { border-color:var(--primary); box-shadow:0 0 0 3px rgba(37,99,235,.12); ${GLASS_INPUT_FOCUS} }
  .rec-tag { display:inline-flex; align-items:center; gap:6px; background:var(--green-bg); border:1px solid var(--green-bd); color:#047857; padding:4px 6px 4px 12px; border-radius:999px; font-size:12.8px; font-weight:600; }
  .rec-tag button { border:none; background:none; color:#047857; cursor:pointer; font-size:15px; line-height:1; padding:0 4px; }
  .rec-tag-input { flex:1; min-width:120px; border:none; outline:none; background:none; font-size:14px; color:var(--text); }

  /* Section label */
  .rec-section-label { font-size:11px; letter-spacing:1.3px; font-weight:700; text-transform:uppercase; color:var(--muted); margin:30px 0 16px; }

  /* Placeholder */
  .rec-placeholder { text-align:center; padding:70px 24px; color:var(--muted); border:1px dashed var(--line); border-radius:14px; background:rgba(255,255,255,.6); }
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
  ${GLASS_FALLBACK(".rec-panel,.rec-card")}
`;
