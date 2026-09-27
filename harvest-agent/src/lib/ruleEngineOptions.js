/*
 * Shared Rule Engine option lists + helpers, used by BOTH rule engine pages
 * (classic RuleEngineConfig for internal/client_in, RuleEngineRedesign for
 * client_us) so the two can't drift apart.
 *
 * Values mirror the backend enums in app/models/harvest_models.py — a select
 * that sends anything outside these Literals gets a 422 from PUT
 * /harvest-config (this is exactly how the redesign's old "On-site" bug
 * happened; the backend value is "Onsite").
 */

export const JOB_TYPES = ["Any", "Contract", "Permanent", "Part-time", "Freelance", "Full-time"];
export const WORK_MODES = ["Any", "Remote", "Hybrid", "Onsite"];

// Only values the classifier can actually assign (the domain_keywords.json
// buckets + the coarse IT/Non-IT split). "Engineering"/"Finance"/"Operations"
// exist in the backend Literal but the classifier never labels a job with
// them, so selecting one would extract zero jobs.
export const DOMAINS = ["Any", "Data Engineering", "Data Science", "AI/ML", "SAP", "Cloud", "Digital", "UX/UI", "ERP", "Cyber Security", "Infrastructure", "IT", "Non-IT"];

export const HIRING_ENTITIES = ["Any", "Direct Client", "GCC", "Ambiguous", "Staffing Firm"];

export const GCC_MODES = [
  { value: "include_gcc", label: "Include GCC" },
  { value: "gcc_only", label: "GCC only" },
  { value: "exclude_gcc", label: "Exclude GCC" },
];

export const SEARCH_WINDOWS = [
  { value: 24, label: "Last 24 hours" },
  { value: 48, label: "Last 48 hours" },
  { value: 72, label: "Last 72 hours" },
  { value: 168, label: "Last 7 days" },
  { value: 720, label: "Last 30 days" },
];

// Two supported LinkedIn accounts. Each has its own saved session + Chrome
// profile on the server (see app/services/session_manager.py account keying).
export const LINKEDIN_ACCOUNTS = [
  { id: "1", label: "Account 1" },
  { id: "2", label: "Account 2" },
];

export const TIMEZONES = [
  { value: "Asia/Kolkata", label: "IST (UTC+5:30)" },
  { value: "UTC", label: "GMT (UTC+0)" },
  { value: "America/New_York", label: "EST (UTC−5)" },
  { value: "America/Chicago", label: "CST (UTC−6)" },
  { value: "America/Denver", label: "MST (UTC−7)" },
  { value: "America/Los_Angeles", label: "PST (UTC−8)" },
  { value: "Asia/Singapore", label: "SGT (UTC+8)" },
];

export const CURRENCIES = ["USD", "INR", "EUR", "GBP"];

// Mirrors app/models/harvest_models.py::_NON_GCC_ENTITIES — a specific hiring
// entity already decides GCC handling, so gcc_mode collapses to the default.
export const NON_GCC_ENTITIES = ["Direct Client", "Staffing Firm", "Ambiguous"];

// Load-time + click-time reconciliation (matches the backend's _reconcile_gcc
// validator): any specific entity neutralizes a saved gcc_mode.
export const normalizeGccMode = (hiringEntity, gccMode) =>
  hiringEntity !== "Any" ? "include_gcc" : (gccMode || "include_gcc");

export const gccLocked = (hiringEntity) => hiringEntity !== "Any";

// A saved config may hold a value outside the curated list (hand-edited JSON,
// older builds). Append it so a <select> never silently coerces it on save.
// Works for plain string lists and {value,label} lists alike.
export const withSavedOption = (options, saved) => {
  if (saved == null || saved === "") return options;
  const has = options.some((o) => (o && typeof o === "object" ? o.value : o) === saved);
  if (has) return options;
  return [...options, typeof options[0] === "object" ? { value: saved, label: String(saved) } : saved];
};

export const fmtRunDate = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};

export const nowLabel = () => {
  const d = new Date();
  let h = d.getHours();
  const m = String(d.getMinutes()).padStart(2, "0");
  const ap = h >= 12 ? "PM" : "AM";
  h = h % 12 || 12;
  return `Today ${String(h).padStart(2, "0")}:${m} ${ap}`;
};
