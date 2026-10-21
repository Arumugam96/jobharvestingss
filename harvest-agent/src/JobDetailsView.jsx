import React, { useState, useEffect, useCallback, useRef } from "react";
import DOMPurify from "dompurify";
import EmailComposeModal from "./components/EmailComposeModal";
import LinkedInMessageModal from "./components/LinkedInMessageModal";
import { getOutreachStatus, getJobOutreachDetail, openLoginRequired, ApiError } from "./api";
import LiveBrowserView from "./components/LiveBrowserView";
import { GLASS, GLASS_BTN, PAGE_BG, GLASS_FALLBACK } from "./theme";
import { copyText } from "./lib/clipboard";
import {
  ArrowLeft,
  MapPin,
  Briefcase,
  Banknote,
  Calendar,
  ExternalLink,
  Building2,
  User,
  Users,
  Eye,
  FileText,
  Link2,
  UserCircle,
  Mail,
  Filter,
  UserSearch,
  ChevronDown,
  BadgeCheck,
  Check,
  Copy,
  Gauge,
  Lock,
  Monitor,
  Search,
} from "lucide-react";

const WhatsAppIcon = ({ size = 16 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M12.04 2C6.58 2 2.13 6.45 2.13 11.91c0 1.75.46 3.45 1.32 4.95L2 22l5.25-1.38a9.9 9.9 0 0 0 4.79 1.22h.01c5.46 0 9.91-4.45 9.91-9.91 0-2.65-1.03-5.14-2.9-7.01A9.82 9.82 0 0 0 12.04 2Zm0 18.13h-.01a8.2 8.2 0 0 1-4.18-1.15l-.3-.18-3.11.82.83-3.04-.2-.31a8.18 8.18 0 0 1-1.26-4.36c0-4.54 3.7-8.24 8.24-8.24a8.2 8.2 0 0 1 8.23 8.25c0 4.54-3.7 8.24-8.24 8.24Zm4.52-6.16c-.25-.12-1.47-.72-1.69-.81-.23-.08-.39-.12-.56.13-.16.25-.64.81-.79.97-.14.17-.29.19-.54.06-.25-.12-1.05-.39-2-1.23-.74-.66-1.23-1.47-1.38-1.72-.14-.25-.01-.38.11-.51.11-.11.25-.29.37-.43.13-.15.17-.25.25-.41.08-.17.04-.31-.02-.43-.06-.12-.56-1.34-.76-1.84-.2-.48-.4-.42-.56-.43h-.48c-.17 0-.43.06-.66.31-.22.25-.86.85-.86 2.07 0 1.22.89 2.4 1.01 2.56.12.17 1.75 2.67 4.23 3.74.59.26 1.05.41 1.41.52.59.19 1.13.16 1.56.1.48-.07 1.47-.6 1.68-1.18.21-.58.21-1.07.14-1.18-.06-.1-.22-.16-.47-.28Z" />
  </svg>
);

const LinkedInIcon = ({ size = 16 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M20.45 20.45h-3.56v-5.57c0-1.33-.02-3.04-1.85-3.04-1.85 0-2.14 1.45-2.14 2.94v5.67H9.35V9h3.41v1.56h.05c.48-.9 1.64-1.85 3.37-1.85 3.6 0 4.27 2.37 4.27 5.45v6.29ZM5.34 7.43a2.06 2.06 0 1 1 0-4.13 2.06 2.06 0 0 1 0 4.13ZM7.12 20.45H3.55V9h3.57v11.45ZM22.22 0H1.77C.79 0 0 .77 0 1.73v20.54C0 23.22.79 24 1.77 24h20.45c.98 0 1.78-.78 1.78-1.73V1.73C24 .77 23.2 0 22.22 0Z" />
  </svg>
);

// Renders a contact value that may originate from two sources — the scraped job
// and the enriched recruiter record. Both present (and distinct) → two labeled
// lines (Job / Recruiter); single source → unlabeled; neither → em-dash.
// `fallback` covers rows that carry only the merged value (JSON read path).
// `recruiterSource` "apollo" (Apollo revealed the recruiter value) labels it "Apollo"
// instead of "Recruiter" — also when it's the only value shown.
function DualContactVal({ scraped, recruiter, fallback, recruiterSource }) {
  const s = (scraped || "").trim();
  const r = (recruiter || "").trim();
  const muted = <span className="ha-muted">—</span>;
  const apollo = (recruiterSource || "").toLowerCase() === "apollo";

  if (!s && !r) {
    const f = (fallback || "").trim();
    return f ? <span>{f}</span> : muted;
  }
  if (s && r && s !== r) {
    return (
      <span style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <span><span className="ha-src-tag">Job</span> {s}</span>
        <span><span className="ha-src-tag">{apollo ? "Apollo" : "Recruiter"}</span> {r}</span>
      </span>
    );
  }
  if (!s && apollo) {
    return <span><span className="ha-src-tag">Apollo</span> {r}</span>;
  }
  return <span>{s || r}</span>;
}

// Origin of the contact that triggered a Resdex search (resdex_searches.contact_source).
const CONTACT_SOURCE_LABEL = {
  job_post: "Job post",
  recruiter_profile: "Recruiter profile",
  apollo: "Apollo",
  apollo_hr: "Apollo HR",
};

// Company-size tier → badge colours (bg tint + text), matching the jobs table.
const SIZE_TIER_STYLE = {
  Small:      { bg: "#ECFDF5", fg: "#047857" }, // emerald
  Medium:     { bg: "#EFF6FF", fg: "#1D4ED8" }, // blue
  Large:      { bg: "#FFFBEB", fg: "#B45309" }, // amber
  Enterprise: { bg: "#F5F3FF", fg: "#6D28D9" }, // violet
};

// Company size as a coloured tier badge with the employee count beneath it.
function CompanySizeBadge({ band, tier }) {
  const count = (band || "").replace(/\s*employees?\s*$/i, "").trim();
  if (!tier && !count) return <span className="ha-muted">—</span>;
  const s = SIZE_TIER_STYLE[tier] || { bg: "#F1F5F9", fg: "#475569" };
  return (
    <span className="ha-sizewrap">
      <span className="ha-sizebadge" style={{ background: s.bg, color: s.fg }}>{tier || "Unknown"}</span>
      {count && <span className="ha-sizecount">{count} employees</span>}
    </span>
  );
}

function ContactAction({ glyph: Glyph, href, onClick, title, variant, newTab = true, sent = false }) {
  // A click handler (opens the LLM composer) takes precedence over a plain href
  // (mailto / profile link fallback); with neither, the action is disabled.
  if (onClick) {
    return (
      <button
        type="button"
        className={`ha-cbtn ${sent ? "ha-cbtn-sent" : `ha-cbtn-on-${variant}`}`}
        style={{ padding: 0, boxSizing: "border-box", cursor: "pointer", font: "inherit" }}
        title={title}
        onClick={onClick}
      >
        <Glyph size={18} />
      </button>
    );
  }
  if (href) {
    return (
      <a
        className={`ha-cbtn ha-cbtn-on-${variant}`}
        href={href}
        title={title}
        {...(newTab ? { target: "_blank", rel: "noreferrer" } : {})}
      >
        <Glyph size={18} />
      </a>
    );
  }
  return (
    <span className="ha-cbtn ha-cbtn-off" title={`${title} not available`} aria-disabled="true">
      <Glyph size={18} />
    </span>
  );
}

// ── Naukri Resdex candidates + outreach email (GET /jobs/{id}/outreach-detail) ──

const fmtWhen = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};

const fmtDay = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
};

const RESDEX_SEARCH_URL = "https://resdex.naukri.com/v3?activeTab=advSrch";
const RESDEX_MAX_TIERS = 3; // relaxation ladder (app/services/resdex_query.py)

// Status callouts shown in place of the candidate grid when nothing was kept.
const RESDEX_STATUS = {
  no_results: {
    tone: "neutral", Icon: Search, title: "No suitable candidates", badge: "No match",
    text: "Resdex returned no profiles that clear the skill bar, even after relaxing the search. Try the search below by hand with a broader experience range or fewer must-have skills.",
  },
  session_expired: {
    tone: "bad", Icon: Lock, title: "Naukri recruiter login needed", badge: "Login needed", login: true,
    text: "The recruiter session had expired, so Resdex wasn't searched for this job. Log in once and the next run will search again — the query below is ready if you want to search now.",
  },
  session_busy: {
    tone: "neutral", Icon: Lock, title: "Recruiter login window was open", badge: "Skipped",
    text: "The search was skipped while the Naukri login window held the recruiter session.",
  },
  cap_reached: {
    tone: "warn", Icon: Gauge, title: "Daily Resdex limit reached", badge: "Limit reached",
    text: "Today's Resdex allowance was used up, so this job was skipped. It will be searched on the next run after the limit resets (5:30 AM IST). You can still run the query below by hand.",
  },
  error: { tone: "neutral", Icon: Search, title: "Resdex search failed", badge: "Error", text: "" },
  skipped_no_jd: {
    tone: "neutral", Icon: FileText, title: "Skipped — no job description", badge: "Skipped",
    text: "There was no description or skill list to build a Resdex search from.",
  },
};

const asList = (v) => (Array.isArray(v) ? v : v == null || v === "" ? [] : [v]);
const lc = (s) => String(s || "").toLowerCase();

// Filters are built by the match service; render the known keys readably and any
// other key generically so nothing it records is hidden ("applied" is runner
// bookkeeping, not a filter).
function filterRows(filters) {
  const f = { ...(filters || {}) };
  delete f.applied;
  const rows = [];
  const take = (...keys) => {
    for (const k of keys) {
      if (f[k] !== undefined && f[k] !== null && f[k] !== "") {
        const v = f[k];
        keys.forEach((kk) => delete f[kk]);
        return v;
      }
    }
    keys.forEach((kk) => delete f[kk]);
    return undefined;
  };
  const min = take("min_exp", "exp_min", "experience_min");
  const max = take("max_exp", "exp_max", "experience_max");
  if (min !== undefined || max !== undefined) {
    rows.push(["Experience", `${min ?? "any"}–${max ?? "any"} yrs`]);
  }
  const locs = take("locations", "current_locations", "location");
  if (asList(locs).length) rows.push(["Locations", asList(locs).join(", ")]);
  const pref = take("preferred_locations");
  if (asList(pref).length) rows.push(["Preferred", asList(pref).join(", ")]);
  const notice = take("notice_period_max_days", "notice_max_days", "notice_period");
  if (notice !== undefined) rows.push(["Notice", typeof notice === "number" ? `≤ ${notice} days` : String(notice)]);
  const active = take("active_within_months", "active_in_months", "active_months");
  if (active !== undefined) rows.push(["Active within", typeof active === "number" ? `${active} months` : String(active)]);
  Object.entries(f).forEach(([k, v]) => {
    if (v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length)) return;
    rows.push([k.replace(/_/g, " "), Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v)]);
  });
  return rows;
}

// Boolean query with AND / OR / NOT highlighted (no innerHTML).
function QueryText({ query }) {
  return String(query).split(/\b(AND|OR|NOT)\b/).map((part, i) =>
    i % 2 ? <span key={i} className="ha-rx-op">{part}</span> : part);
}

function CopyQueryButton({ text, targetRef }) {
  const [msg, setMsg] = useState("");
  const onCopy = async () => {
    if (await copyText(text)) {
      setMsg("Copied");
      setTimeout(() => setMsg(""), 1800);
      return;
    }
    // Last resort (both clipboard paths refused): select the query for a manual Ctrl+C.
    const el = targetRef.current;
    if (el) {
      const r = document.createRange();
      r.selectNodeContents(el);
      const s = window.getSelection();
      s.removeAllRanges();
      s.addRange(r);
    }
    setMsg("Selected — press Ctrl+C to copy");
  };
  return (
    <>
      <button type="button" className="ha-rx-btn ha-rx-btn-primary" onClick={onCopy}>
        <Copy size={13} /> Copy query
      </button>
      <span className="ha-rx-copied" role="status" aria-live="polite">{msg}</span>
    </>
  );
}

const normMust = (req) => asList(req?.mandatory_skills)
  .map((m) => (typeof m === "string" ? { name: m } : m))
  .filter((m) => m && m.name);

// Resdex names often arrive ALL CAPS / all lower — title-case those (mirrors the
// backend email's _display_name); mixed case is kept as typed.
function displayName(raw) {
  const s = String(raw || "").replace(/\s+/g, " ").trim();
  if (!s || (s !== s.toUpperCase() && s !== s.toLowerCase())) return s;
  return s.toLowerCase().split(/([\s.'-])/)
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w)).join("");
}

function CandidateCard({ c, must }) {
  const matched = new Set(asList(c.matched_skills).map(lc));
  const realName = displayName(c.candidate_name);
  const name = realName || `Candidate ${c.rank}`;
  const initials = realName.split(/[\s.]+/).filter(Boolean)
    .slice(0, 2).map((w) => w[0].toUpperCase()).join("");
  const skills = asList(c.skills);
  const ordered = [...skills].sort((a, b) => matched.has(lc(b)) - matched.has(lc(a)));
  const shown = ordered.slice(0, 12);
  const missing = must.map((m) => m.name).filter((m) => !matched.has(lc(m)));
  const pct = typeof c.skill_coverage === "number" ? Math.round(c.skill_coverage * 100) : null;
  const prev = asList(c.previous_roles)[0];
  const exp = c.experience_text || (c.experience_years != null ? `${c.experience_years} yrs` : "");
  const facts = [
    ["Experience", exp], ["Notice", c.notice_period], ["Location", c.location],
    ["Prefers", asList(c.preferred_locations).join(", ")], ["CTC", c.ctc], ["Last active", c.last_active],
  ].filter(([, v]) => v);
  return (
    <article className="ha-rx-card" aria-label={name}>
      <div className="ha-rx-head">
        <div className="ha-avatar ha-avatar-amber ha-rx-avatar">
          {initials || <User size={17} />}
          <span className="ha-rx-rank">#{c.rank}</span>
        </div>
        <div style={{ minWidth: 0 }}>
          <div className="ha-rx-name">{name}</div>
          <div className="ha-rx-role">
            {c.current_designation || c.headline || "—"}
            {c.current_company ? <><span className="ha-rx-at"> @ </span>{c.current_company}</> : null}
          </div>
        </div>
        {/* Top-right, like the emailed card: the "Naukri Verified" badge every card
            carries (backend naukri_verified_badge = RESDEX_SHOW_NAUKRI_VERIFIED). */}
        {(c.naukri_verified_badge || c.emailed) && (
          <div className="ha-rx-tags">
            {c.naukri_verified_badge && (
              <span className="ha-rx-verified"><Check size={11} strokeWidth={3} /> Naukri Verified</span>
            )}
            {c.emailed && <span className="ha-rx-emailed" title="Included in the outreach email"><BadgeCheck size={12} /> In email</span>}
          </div>
        )}
      </div>

      {(must.length > 0 || pct !== null) && (
        <div className="ha-rx-cov">
          <div className="ha-rx-cov-top">
            <span>{must.length ? <><strong>{must.length - missing.length} of {must.length}</strong> must-have skills</> : "Skill coverage"}</span>
            {pct !== null && <strong>{pct}%</strong>}
          </div>
          {must.length > 0 && (
            <div className="ha-rx-cov-bar" style={{ gridTemplateColumns: `repeat(${must.length}, 1fr)` }} aria-hidden="true">
              {must.map((m) => <span key={m.name} className={matched.has(lc(m.name)) ? "on" : ""} title={m.name} />)}
            </div>
          )}
        </div>
      )}

      <dl className="ha-rx-facts">
        {facts.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
        {c.education && <div className="wide"><dt>Education</dt><dd>{c.education}</dd></div>}
        {prev && (prev.designation || prev.organization) && (
          <div className="wide"><dt>Previously</dt><dd>
            {prev.designation || "—"}
            {prev.organization && <span className="ha-rx-sub"> @ {prev.organization}</span>}
            {(prev.start || prev.end) && <span className="ha-rx-sub"> · {[prev.start, prev.end].filter(Boolean).join("–")}</span>}
          </dd></div>
        )}
      </dl>

      {(shown.length > 0 || missing.length > 0) && (
        <div className="ha-rx-skills">
          {shown.map((s, i) => (matched.has(lc(s))
            ? <span key={`${s}-${i}`} className="ha-rx-skill ha-rx-skill-on"><Check size={11} strokeWidth={3} />{s}</span>
            : <span key={`${s}-${i}`} className="ha-rx-skill">{s}</span>))}
          {skills.length > shown.length && <span className="ha-rx-more">+{skills.length - shown.length} more</span>}
          {missing.map((m) => <span key={`miss-${m}`} className="ha-rx-skill ha-rx-skill-miss" title="Must-have skill not on this profile">{m}</span>)}
        </div>
      )}

      <div className="ha-rx-foot">
        {/* Resdex /v3/preview links are bound to the harvester's search session and
            fail elsewhere, so the backend sends the reusable search-results link
            (resdex_url) plus this candidate's position in it instead. */}
        {c.profile_url ? (
          <a className="ha-rx-btn ha-rx-btn-amber" href={c.profile_url} target="_blank" rel="noreferrer">
            Open in Naukri <ExternalLink size={13} />
          </a>
        ) : c.resdex_url ? (
          <a className="ha-rx-btn ha-rx-btn-amber" href={c.resdex_url} target="_blank" rel="noreferrer"
            title={`Opens the Resdex search this candidate came from${c.resdex_position ? ` — they were result #${c.resdex_position}` : ""}. Needs the same Naukri recruiter login.`}>
            Find in Resdex{c.resdex_position ? ` (#${c.resdex_position})` : ""} <ExternalLink size={13} />
          </a>
        ) : <span className="ha-muted">No profile link</span>}
        <span className="ha-rx-hint"><Lock size={12} /> Recruiter login</span>
      </div>
    </article>
  );
}

function ResdexSearchPanel({ resdex }) {
  const queryRef = useRef(null);
  const req = resdex.requirements || {};
  const must = normMust(req);
  const optional = asList(req.optional_skills).map((o) => (typeof o === "string" ? o : o?.name)).filter(Boolean);
  const titles = asList(req.job_titles);
  const fRows = filterRows(resdex.filters);
  const relaxedLoc = (resdex.tier || 0) >= RESDEX_MAX_TIERS && asList(req.locations).length > 0
    && !fRows.some(([k]) => k === "Locations");
  if (!resdex.boolean_query && !must.length && !fRows.length) return null;

  const tierParts = [];
  if (resdex.tier) tierParts.push(<strong key="t">Tier {resdex.tier} of {RESDEX_MAX_TIERS}</strong>);
  if (resdex.result_count) tierParts.push(`${Number(resdex.result_count).toLocaleString("en-IN")} matches in Resdex`);
  if (resdex.searches_used) tierParts.push(`${resdex.searches_used} search${resdex.searches_used === 1 ? "" : "es"} used`);

  return (
    <div className="ha-rx-search">
      <div className="ha-rx-search-head">
        <h3><Search size={13} /> Search used</h3>
        {tierParts.length > 0 && (
          <span className="ha-rx-tierline">
            {tierParts.map((p, i) => <React.Fragment key={i}>{i ? " · " : ""}{p}</React.Fragment>)}
          </span>
        )}
        <a className="ha-rx-btn" href={resdex.resdex_url || RESDEX_SEARCH_URL} target="_blank" rel="noreferrer">
          {resdex.resdex_url ? "Open these results in Resdex" : "Open Resdex search"} <ExternalLink size={13} />
        </a>
      </div>
      <div className="ha-rx-search-body">
        <div className="ha-rx-col">
          {resdex.boolean_query && (
            <div>
              <div className="ha-rx-mini">Boolean query</div>
              <pre className="ha-rx-query" ref={queryRef} tabIndex={0}><QueryText query={resdex.boolean_query} /></pre>
              <div className="ha-rx-query-acts">
                <CopyQueryButton text={resdex.boolean_query} targetRef={queryRef} />
                <span className="ha-rx-hint">In Resdex, paste it into Keywords with <strong>Boolean</strong> turned on.</span>
              </div>
            </div>
          )}
          {(fRows.length > 0 || relaxedLoc) && (
            <div>
              <div className="ha-rx-mini">Filters applied</div>
              <div className="ha-rx-fchips">
                {fRows.map(([k, v]) => (
                  <span key={k} className="ha-rx-fchip"><span className="k" style={{ textTransform: "capitalize" }}>{k}</span> {v}</span>
                ))}
                {relaxedLoc && (
                  <span className="ha-rx-fchip relaxed"><span className="k">Locations</span> Any (relaxed at tier {RESDEX_MAX_TIERS})</span>
                )}
              </div>
            </div>
          )}
        </div>
        {(must.length > 0 || optional.length > 0 || titles.length > 0) && (
          <div className="ha-rx-col">
            {must.length > 0 && (
              <div>
                <div className="ha-rx-mini">
                  Must-have skills
                  <span className="ha-rx-extracted">
                    · {req.extracted_by === "fallback" ? "from the job's skill list (LLM unavailable)" : "extracted from the JD"}
                  </span>
                </div>
                <div className="ha-rx-reqs">
                  {must.map((m) => {
                    const variants = asList(m.variants).filter((v) => lc(v) !== lc(m.name));
                    const high = m.priority === 1 || m.priority === "high";
                    return (
                      <div className="ha-rx-req" key={m.name}>
                        <span>
                          <span className="nm">{m.name}</span>
                          {variants.length > 0 && <span className="var"> · {variants.join(", ")}</span>}
                        </span>
                        {m.priority != null && (
                          <span className={`ha-rx-prio${high ? " high" : ""}`}>
                            {typeof m.priority === "number" ? `P${m.priority}` : m.priority}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
            {optional.length > 0 && (
              <div>
                <div className="ha-rx-mini">Nice to have</div>
                <div className="ha-rx-skills">{optional.map((s) => <span key={s} className="ha-rx-skill">{s}</span>)}</div>
              </div>
            )}
            {titles.length > 0 && (
              <div>
                <div className="ha-rx-mini">Job titles searched</div>
                <div className="ha-rx-plain">{titles.join(" · ")}</div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// Collapsed by default (accordion): the header carries the one-line summary.
function ResdexCandidatesSection({ resdex, onLogin }) {
  const [open, setOpen] = useState(false);
  const cands = resdex.candidates || [];
  const must = normMust(resdex.requirements);
  const st = RESDEX_STATUS[resdex.status];
  const badge = cands.length ? `${cands.length} matched` : (st?.badge || resdex.status || "—");
  const emailedN = cands.filter((c) => c.emailed).length;
  const sourceLabel = CONTACT_SOURCE_LABEL[resdex.contact_source] || "";

  return (
    <section className="ha-section ha-poc ha-enriched ha-rx">
      <button type="button" className="ha-rx-acc" onClick={() => setOpen((v) => !v)}
        aria-expanded={open} aria-controls="ha-rx-body">
        <span className="ha-rx-acc-title"><UserSearch size={14} /> Naukri Resdex candidates</span>
        <span className="ha-en-badge">{badge}</span>
        {sourceLabel && (
          <span className="ha-src-tag" title="Contact the search was run for">{sourceLabel}</span>
        )}
        {emailedN > 0 && <span className="ha-rx-acc-meta">{emailedN} in email</span>}
        {resdex.searched_at && <span className="ha-rx-when">Searched {fmtWhen(resdex.searched_at)}</span>}
        <ChevronDown size={16} className="ha-rx-chev" style={{ transform: open ? "rotate(180deg)" : "none" }} />
      </button>

      {open && (
        <div id="ha-rx-body" className="ha-rx-body">
          {cands.length ? (
            <>
              <div className="ha-rx-summary">
                <span>Ranked by must-have skill coverage</span>
                {emailedN > 0 && <span><strong>{emailedN} of {cands.length}</strong> included in the outreach email</span>}
              </div>
              <div className="ha-rx-grid">
                {cands.map((c) => <CandidateCard key={c.id} c={c} must={must} />)}
              </div>
            </>
          ) : (
            <div className={`ha-rx-state ha-rx-state-${st?.tone || "neutral"}`}>
              <span className="ha-rx-state-ico">{st ? <st.Icon size={18} /> : <Search size={18} />}</span>
              <h3>{st?.title || `Status: ${resdex.status}`}</h3>
              {(st?.text || resdex.error) && (
                <p>{st?.text}{resdex.status === "error" && resdex.error ? ` ${resdex.error}` : ""}</p>
              )}
              {st?.login && (
                <div className="ha-rx-state-acts">
                  <button type="button" className="ha-rx-btn ha-rx-btn-primary" onClick={onLogin}>
                    <Monitor size={13} /> Open Live Browser to log in
                  </button>
                </div>
              )}
            </div>
          )}

          <ResdexSearchPanel resdex={resdex} />

          <div className="ha-en-foot">
            <Lock size={13} /> Opening a profile in Naukri needs the recruiter login and may use a profile-view credit.
          </div>
        </div>
      )}
    </section>
  );
}

function EmailStep({ tone, children }) {
  return <span className={`ha-em-step ha-em-${tone}`}>{children}</span>;
}

// Hero box: latest outreach email + "View email" (opens the existing /mail/:id
// thread page in a new tab — the body is never rendered on the job page).
function OutreachEmailStrip({ emails, onCompose }) {
  const list = emails || [];
  if (!list.length) {
    return (
      <aside className="ha-em-strip" aria-label="Outreach email">
        <div className="ha-em-strip-top"><Mail size={13} /> Outreach email</div>
        <div className="ha-em-steps"><EmailStep tone="off">Not sent yet</EmailStep></div>
        {onCompose && (
          <div className="ha-em-row">
            <span className="ha-em-to">No email has been sent for this job yet.</span>
            <button type="button" className="ha-rx-btn" onClick={onCompose}><Mail size={13} /> Compose email</button>
          </div>
        )}
      </aside>
    );
  }
  const e = list[0]; // newest first
  const failed = e.status === "failed";
  const q = e.job_id ? `?job=${encodeURIComponent(e.job_id)}`
    : e.recruiter_id ? `?recruiter=${encodeURIComponent(e.recruiter_id)}` : "";
  const openMail = () => window.open(`/mail/${encodeURIComponent(e.id)}${q}`, "_blank", "noopener");
  const kinds = list.some((m) => m.outreach_kind === "followup") ? "initial + follow-up" : "initial";
  return (
    <aside className="ha-em-strip" aria-label="Outreach email">
      <div className="ha-em-strip-top">
        <Mail size={13} /> Outreach email
        <span className="ha-em-count">{list.length} message{list.length === 1 ? "" : "s"} · {kinds}</span>
      </div>
      <div className="ha-em-subject" title={e.subject || ""}>{e.subject || <span className="ha-muted">(no subject)</span>}</div>
      <div className="ha-em-steps">
        {failed
          ? <EmailStep tone="bad">Failed {fmtDay(e.sent_at || e.created_at)}</EmailStep>
          : <EmailStep tone="ok"><Check size={11} strokeWidth={3} /> Sent {fmtDay(e.sent_at || e.created_at)}</EmailStep>}
        {e.delivered_at && <><span className="ha-em-sep">›</span><EmailStep tone="ok">Delivered</EmailStep></>}
        {e.opened_at && <><span className="ha-em-sep">›</span><EmailStep tone="info">Opened {fmtDay(e.opened_at)}</EmailStep></>}
        {e.replied_at && <><span className="ha-em-sep">›</span><EmailStep tone="reply">Replied {fmtDay(e.replied_at)}</EmailStep></>}
        {e.bounced_at && <><span className="ha-em-sep">›</span><EmailStep tone="bad">Bounced</EmailStep></>}
      </div>
      <div className="ha-em-row">
        <span className="ha-em-to" title={e.to_email || ""}>To {e.to_email || "—"}</span>
        <button type="button" className="ha-rx-btn ha-rx-btn-primary" onClick={openMail}>
          View email <ExternalLink size={13} />
        </button>
      </div>
    </aside>
  );
}

function JobDetailsView({ job = {}, onBack = () => {}, onEdit }) {
  // Same LLM outreach composers the jobs-table rows use. They need the DB-backed
  // job.id (carried through by mapJobToDetail) plus job.email / job.company.
  // emailModal carries follow-up context: { job, followup, parentOutreachId }.
  const [emailModal, setEmailModal] = useState(null);
  const [linkedinModalJob, setLinkedinModalJob] = useState(null);
  // DB-backed sent state for this job ({ email?, linkedin? }); drives the green
  // "sent" icons and whether the Email icon opens an initial or a follow-up.
  const [outreach, setOutreach] = useState({});
  // Resdex candidates + sent emails for this job (GET /jobs/{id}/outreach-detail).
  // Non-blocking: the page renders without it; a failure shows one muted line.
  const [detail, setDetail] = useState(null);
  const [detailError, setDetailError] = useState(false);
  // Naukri recruiter login from a 'login needed' Resdex state — same global
  // action as the app-wide banner (POST /login-required/open + Live Browser).
  const [liveLogin, setLiveLogin] = useState(null);   // title | null
  const [loginNote, setLoginNote] = useState("");
  const openRecruiterLogin = async () => {
    setLoginNote("");
    try {
      const res = await openLoginRequired({ source: "naukri_recruiter" });
      if (res && res.open_live_browser) setLiveLogin("Naukri recruiter login — live browser");
      else setLoginNote((res && res.message) || "");
    } catch (err) {
      setLoginNote(err instanceof ApiError ? err.message : "Could not reach the harvest backend.");
    }
  };

  const refreshOutreach = useCallback(async () => {
    if (!job.id) return;
    // Refetched together with the sent-state map, so a send/log from the
    // composers (onSent / onLogged) also refreshes the Outreach email panel.
    getJobOutreachDetail(job.id)
      .then((d) => { setDetail(d || null); setDetailError(false); })
      .catch(() => setDetailError(true));
    try {
      const map = await getOutreachStatus([job.id]);
      setOutreach((map && map[job.id]) || {});
    } catch {
      // best-effort — icons fall back to the default (not-sent) state
    }
  }, [job.id]);
  useEffect(() => { refreshOutreach(); }, [refreshOutreach]);

  const emailSent = !!outreach.email;
  const liSent = !!outreach.linkedin;

  // Inferred (company + job-location) HR contact — its own section below Point of
  // Contact, shown only when one was discovered for this job. Kept separate from
  // the scraped/recruiter poster so provenance stays unambiguous.
  const cc = job.companyContact || {};
  const hasCompanyContact = !!(cc.email || cc.phone || cc.name);
  const ccSourceLabel = cc.source
    ? `Inferred · ${cc.source.charAt(0).toUpperCase()}${cc.source.slice(1)}`
    : "Inferred";

  const linkOrDash = (url, label) =>
    url ? (
      <a className="ha-link" href={url} target="_blank" rel="noreferrer">
        {label || url} <ExternalLink size={13} />
      </a>
    ) : (
      <span className="ha-muted">—</span>
    );

  return (
    <div className="ha-page" role="region" aria-label="Job details">
      <style>{styles}</style>

      <div className="ha-container">
        <div className="ha-topbar">
          <button className="ha-back" onClick={onBack}>
            <ArrowLeft size={16} /> Back
          </button>
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            {onEdit && (
              <button className="ha-edit-btn" onClick={onEdit}>
                Edit
              </button>
            )}
            <span className="ha-viewbadge">
              <Eye size={13} /> View only
            </span>
          </div>
        </div>

        <header className="ha-hero">
          <div style={{ minWidth: 0 }}>
          <div className="ha-hero-meta">
            <span className="ha-source">{job.source}</span>
            <span className="ha-hero-company">
              <Building2 size={15} /> {job.company}
            </span>
          </div>
          <h1 className="ha-title">{job.jobTitle}</h1>
          <div className="ha-chips">
            <span className="ha-chip"><MapPin size={14} /> {job.location || "—"}</span>
            <span className="ha-chip"><Briefcase size={14} /> {job.jobType || "—"}</span>
            <span className="ha-chip ha-chip-accent"><Banknote size={14} /> {job.salary || "—"}</span>
            <span className="ha-chip"><Calendar size={14} /> {job.postedDate || "—"}</span>
          </div>
          </div>
          {detail && (
            <OutreachEmailStrip
              emails={detail.emails}
              onCompose={job.id && job.email ? () => setEmailModal({ job, followup: false }) : null}
            />
          )}
        </header>

        <section className="ha-section ha-poc">
          <div className="ha-label"><UserCircle size={14} /> Point of Contact</div>
          <div className="ha-poc-body">
            <div className="ha-poster">
              <div className="ha-avatar"><User size={20} /></div>
              <div className="ha-poster-info">
                <div className="ha-poster-name">{job.posterName || "—"}</div>
                <div className="ha-poster-title">{job.posterTitle || "—"}</div>
              </div>
            </div>

            <div className="ha-poc-fields">
              <div className="ha-field">
                <span className="ha-key">Email</span>
                <span className="ha-val">
                  <DualContactVal
                    scraped={job.posterContact?.emailScraped}
                    recruiter={job.posterContact?.emailRecruiter}
                    recruiterSource={job.posterContact?.emailRecruiterSource}
                    fallback={job.posterContact?.email}
                  />
                </span>
              </div>
              <div className="ha-field">
                <span className="ha-key">Mobile</span>
                <span className="ha-val">
                  <DualContactVal
                    scraped={job.posterContact?.mobileScraped}
                    recruiter={job.posterContact?.mobileRecruiter}
                    fallback={job.posterContact?.mobile}
                  />
                </span>
              </div>
              <div className="ha-field">
                <span className="ha-key">LinkedIn</span>
                <span className="ha-val">{linkOrDash(job.posterLinkedIn, "View profile")}</span>
              </div>
            </div>

            <div className="ha-actions">
              <ContactAction
                glyph={WhatsAppIcon}
                variant="wa"
                title="WhatsApp"
                href={job.posterContact?.mobile ? `https://wa.me/${job.posterContact.mobile.replace(/\D/g, "")}` : null}
              />
              <ContactAction
                glyph={Mail}
                variant="mail"
                title={emailSent ? "Email sent — click to follow up" : "Email"}
                sent={emailSent}
                newTab={false}
                onClick={
                  job.id && emailSent
                    ? () => setEmailModal({ job, followup: true, parentOutreachId: outreach.email.outreach_id })
                    : job.id && job.email
                      ? () => setEmailModal({ job, followup: false })
                      : undefined
                }
                href={!job.id && job.email ? `mailto:${job.email}` : null}
              />
              <ContactAction
                glyph={LinkedInIcon}
                variant="li"
                title={liSent ? "LinkedIn message sent" : "LinkedIn"}
                sent={liSent}
                onClick={job.id && job.linkedin ? () => setLinkedinModalJob(job) : undefined}
                href={!job.id && job.linkedin ? job.linkedin : null}
              />
            </div>
          </div>
        </section>

        {hasCompanyContact && (
          <section className="ha-section ha-poc ha-enriched">
            <div className="ha-label">
              <Building2 size={14} /> Apollo Resolved
              <span className="ha-en-badge">{ccSourceLabel}</span>
            </div>
            <div className="ha-poc-body">
              <div className="ha-poster">
                <div className="ha-avatar ha-avatar-amber"><User size={20} /></div>
                <div className="ha-poster-info">
                  <div className="ha-poster-name">{cc.name || "HR Contact"}</div>
                  <div className="ha-poster-title">{cc.title || "—"}</div>
                </div>
              </div>

              <div className="ha-poc-fields">
                <div className="ha-field">
                  <span className="ha-key">Email</span>
                  <span className="ha-val">{cc.email || <span className="ha-muted">—</span>}</span>
                </div>
                <div className="ha-field">
                  <span className="ha-key">Phone</span>
                  <span className="ha-val">{cc.phone || <span className="ha-muted">—</span>}</span>
                </div>
                <div className="ha-field">
                  <span className="ha-key">Location</span>
                  <span className="ha-val">{cc.location || <span className="ha-muted">—</span>}</span>
                </div>
              </div>

              <div className="ha-actions">
                <ContactAction
                  glyph={WhatsAppIcon}
                  variant="wa"
                  title="WhatsApp"
                  href={cc.phone ? `https://wa.me/${cc.phone.replace(/\D/g, "")}` : null}
                />
                <ContactAction
                  glyph={Mail}
                  variant="mail"
                  title={emailSent ? "Email sent — click to follow up" : "Email HR contact"}
                  sent={emailSent}
                  newTab={false}
                  onClick={
                    job.id && emailSent
                      ? () => setEmailModal({ job: { ...job, email: cc.email }, followup: true, parentOutreachId: outreach.email.outreach_id })
                      : job.id && cc.email
                        ? () => setEmailModal({ job: { ...job, email: cc.email }, followup: false })
                        : undefined
                  }
                  href={!job.id && cc.email ? `mailto:${cc.email}` : null}
                />
              </div>
            </div>
            <div className="ha-en-foot">
              Not the job poster — a likely HR contact at this company's location. Verify before relying on it.
            </div>
          </section>
        )}

        {detail?.resdex && (
          <ResdexCandidatesSection
            resdex={detail.resdex}
            onLogin={openRecruiterLogin}
          />
        )}
        {loginNote && <div className="ha-rx-err">{loginNote}</div>}
        {detailError && !detail && (
          <div className="ha-rx-err">Couldn't load Naukri Resdex candidates / outreach email details.</div>
        )}

        <div className="ha-grid">
          <main className="ha-main">
            <section className="ha-section">
              <div className="ha-label"><FileText size={14} /> Job Description</div>
              {job.jdHtml ? (
                // Rich LinkedIn description HTML. Sanitized server-side; DOMPurify
                // here is defense-in-depth before dangerouslySetInnerHTML.
                <div
                  className="ha-jd ha-jd-html"
                  dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(job.jdHtml) }}
                />
              ) : (
                <p className="ha-jd">{job.jd || "No description provided."}</p>
              )}
            </section>
          </main>

          <aside className="ha-rail">
            <section className="ha-section ha-company">
              <div className="ha-label"><Building2 size={14} /> Company</div>
              <div className="ha-company-grid">
                <div className="ha-company-item">
                  <span className="ha-company-key"><Users size={13} /> Size</span>
                  <CompanySizeBadge band={job.companySize} tier={job.companySizeTier} />
                </div>
                <div className="ha-company-item">
                  <span className="ha-company-key"><MapPin size={13} /> Headquarters</span>
                  <span className="ha-company-val">
                    {[job.companyState, job.companyCountry].filter(Boolean).join(", ") ||
                      <span className="ha-muted">—</span>}
                  </span>
                </div>
              </div>
            </section>
            <section className="ha-section">
              <div className="ha-label"><Filter size={14} /> Classification</div>
              <div className="ha-rows">
                {/* Filter status hidden for now.
                <div className="ha-row">
                  <span className="ha-key">Status</span>
                  <span className="ha-val" style={{ color: job.passedFilter === false ? "#B45309" : "#047857", fontWeight: 600 }}>
                    {job.passedFilter === false ? "Flagged" : "Qualified"}
                  </span>
                </div>
                {job.passedFilter === false && (
                  <div className="ha-row">
                    <span className="ha-key">Reason</span>
                    <span className="ha-val">{job.filterReason || <span className="ha-muted">—</span>}</span>
                  </div>
                )}
                */}
                <div className="ha-row">
                  <span className="ha-key">Hiring</span>
                  <span className="ha-val">{job.hiringEntity || <span className="ha-muted">—</span>}</span>
                </div>
                <div className="ha-row">
                  <span className="ha-key">Domain</span>
                  <span className="ha-val">{job.domain || <span className="ha-muted">—</span>}</span>
                </div>
              </div>
            </section>
            <section className="ha-section">
              <div className="ha-label"><Link2 size={14} /> Links</div>
              <div className="ha-rows">
                <div className="ha-row">
                  <span className="ha-key">Apply link</span>
                  <span className="ha-val">{linkOrDash(job.applyLink, "Open application")}</span>
                </div>
                <div className="ha-row">
                  <span className="ha-key">Company URL</span>
                  <span className="ha-val">{linkOrDash(job.companyUrl, job.companyUrl)}</span>
                </div>
              </div>
            </section>
          </aside>
        </div>
      </div>

      {emailModal && (
        <EmailComposeModal
          job={emailModal.job}
          followup={emailModal.followup}
          parentOutreachId={emailModal.parentOutreachId}
          onClose={() => setEmailModal(null)}
          onSent={refreshOutreach}
        />
      )}
      {liveLogin && (
        <LiveBrowserView title={liveLogin} onClose={() => { setLiveLogin(null); refreshOutreach(); }} />
      )}
      {linkedinModalJob && (
        <LinkedInMessageModal
          job={linkedinModalJob}
          onClose={() => setLinkedinModalJob(null)}
          onLogged={refreshOutreach}
        />
      )}
    </div>
  );
}

const styles = `
  .ha-page { min-height: 100vh; width: 100%; ${PAGE_BG} color: #1E293B;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  .ha-container { max-width: 1280px; margin: 0 auto; padding: 22px 32px 48px; }

  .ha-topbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; }
  .ha-back { display: inline-flex; align-items: center; gap: 7px; cursor: pointer;
    ${GLASS_BTN} color: #334155;
    font-size: 13px; font-weight: 600; padding: 8px 14px; border-radius: 8px; }
  .ha-back:hover { background: rgba(241,245,249,.92); border-color: #CBD5E1; }
  .ha-back:focus-visible { outline: 2px solid #2563EB; outline-offset: 1px; }
  .ha-edit-btn { display: inline-flex; align-items: center; gap: 7px; cursor: pointer;
    background: #2563EB; border: none; color: #fff;
    font-size: 13px; font-weight: 600; padding: 8px 16px; border-radius: 8px; }
  .ha-edit-btn:hover { background: #1E40AF; }
  .ha-viewbadge { display: inline-flex; align-items: center; gap: 5px;
    font-size: 12px; font-weight: 600; color: #64748B; ${GLASS_BTN}
    padding: 7px 12px; border-radius: 8px; }

  .ha-hero { ${GLASS} border-radius: 14px;
    padding: 15px 28px; margin-bottom: 15px; }
  .ha-hero-meta { display: flex; align-items: center; gap: 14px; margin-bottom: 12px; }
  .ha-source { font-size: 11px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
    color: #2563EB; background: rgba(239,244,255,.8); border: 1px solid #DBE6FF;
    padding: 4px 10px; border-radius: 6px; }
  .ha-hero-company { display: inline-flex; align-items: center; gap: 6px;
    color: #475569; font-size: 14px; font-weight: 500; }
  .ha-title { font-size: 26px; font-weight: 700; line-height: 1.25; margin: 0 0 18px; }

  .ha-chips { display: flex; flex-wrap: wrap; gap: 9px; }
  .ha-chip { display: inline-flex; align-items: center; gap: 6px;
    font-size: 13px; color: #334155; background: rgba(248,250,252,.7);
    border: 1px solid rgba(203,213,225,.6); padding: 7px 13px; border-radius: 999px; }
  .ha-chip svg { color: #64748B; }
  .ha-chip-accent { background: #FFF7EC; border-color: #FCE3BC; color: #92580B; }
  .ha-chip-accent svg { color: #F59E0B; }

  .ha-grid { display: grid; grid-template-columns: minmax(0, 2.2fr) minmax(320px, 1fr); gap: 18px; align-items: start; margin-top: 18px; }
  .ha-rail { display: flex; flex-direction: column; gap: 18px; }

  .ha-poc-body { display: flex; align-items: center; gap: 28px; flex-wrap: wrap; }
  .ha-poc-fields { display: flex; gap: 28px; flex-wrap: wrap; flex: 1; min-width: 220px; }
  .ha-field { display: flex; flex-direction: column; gap: 3px; }
  .ha-poc .ha-poster { margin-bottom: 0; padding-bottom: 0; border-bottom: none;
    padding-right: 28px; border-right: 1px solid #F1F5F9; }
  .ha-poc .ha-actions { margin-top: 0; width: 320px; flex-shrink: 0; }

  /* Company/location enriched contact — its own section, amber-accented so an
     inferred HR contact never reads as the scraped job poster. */
  .ha-enriched { position: relative; border-color: #FCE3BC; margin-top: 18px; }
  .ha-enriched::before { content: ""; position: absolute; left: 0; top: 16px; bottom: 16px;
    width: 3px; border-radius: 0 3px 3px 0; background: #F59E0B; }
  .ha-enriched .ha-label { color: #92580B; }
  .ha-enriched .ha-label svg { color: #F59E0B; }
  .ha-en-badge { font-size: 10px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase;
    background: #FFF7EC; color: #92580B; border: 1px solid #FCE3BC; padding: 2px 8px; border-radius: 999px; }
  .ha-avatar-amber { background: #FEF3E2; color: #B45309; }
  .ha-en-foot { margin-top: 14px; display: flex; align-items: center; gap: 7px;
    font-size: 12px; color: #92580B; background: #FFF7EC; border: 1px solid #FCE3BC;
    border-radius: 9px; padding: 9px 13px; }

  /* Naukri Resdex candidates — amber like the inferred contact (internal-only
     data), collapsed by default; the accordion header carries the summary. */
  .ha-section.ha-rx { padding: 0; }
  .ha-rx-acc { display: flex; align-items: center; gap: 8px 10px; flex-wrap: wrap; width: 100%;
    background: none; border: none; cursor: pointer; text-align: left; font: inherit; padding: 18px 22px; border-radius: 14px; }
  .ha-rx-acc:focus-visible { outline: 2px solid #2563EB; outline-offset: -2px; }
  .ha-rx-acc-title { display: inline-flex; align-items: center; gap: 7px; font-size: 11px; font-weight: 700;
    letter-spacing: .05em; text-transform: uppercase; color: #92580B; }
  .ha-rx-acc-title svg { color: #F59E0B; }
  .ha-rx-acc-meta { font-size: 11px; font-weight: 700; color: #047857; background: #ECFDF5;
    border: 1px solid #A7F3D0; padding: 2px 8px; border-radius: 999px; }
  .ha-rx-when { font-size: 11px; font-weight: 500; color: #94A3B8; margin-left: auto; }
  .ha-rx-chev { color: #92580B; flex-shrink: 0; transition: transform .2s; }
  .ha-rx-acc:hover .ha-rx-chev { color: #F59E0B; }
  .ha-rx-body { padding: 0 22px 22px; }
  .ha-rx-summary { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12.5px; color: #64748B; margin: 0 0 14px; }
  .ha-rx-summary strong { color: #334155; font-weight: 600; }

  .ha-rx-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
  .ha-rx-card { display: flex; flex-direction: column; gap: 12px; min-width: 0;
    background: rgba(255,255,255,.78); border: 1px solid #F1E3CC; border-radius: 12px; padding: 14px; }
  .ha-rx-head { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 10px; align-items: start; }
  .ha-rx-avatar { width: 38px; height: 38px; font-size: 13px; font-weight: 700; position: relative; }
  .ha-rx-rank { position: absolute; top: -7px; left: -7px; font-size: 10px; font-weight: 800; background: #fff;
    color: #92580B; border: 1px solid #FCE3BC; border-radius: 999px; padding: 0 5px; line-height: 16px; }
  .ha-rx-name { font-size: 14.5px; font-weight: 600; color: #1E293B; }
  .ha-rx-role { font-size: 12.5px; color: #334155; word-break: break-word; }
  .ha-rx-at, .ha-rx-sub { color: #64748B; }
  .ha-rx-emailed { display: inline-flex; align-items: center; gap: 4px; font-size: 10.5px; font-weight: 700; white-space: nowrap;
    color: #047857; background: #ECFDF5; border: 1px solid #A7F3D0; padding: 2px 8px; border-radius: 999px; }
  .ha-rx-tags { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }
  .ha-rx-verified { display: inline-flex; align-items: center; gap: 3px; font-size: 10.5px; font-weight: 700;
    white-space: nowrap; color: #16794A; background: #E9F7EF; border: 1px solid #BFE5CF; padding: 1px 8px; border-radius: 999px; }
  .ha-rx-cov { display: flex; flex-direction: column; gap: 5px; }
  .ha-rx-cov-top { display: flex; justify-content: space-between; font-size: 11.5px; color: #64748B; }
  .ha-rx-cov-top strong { color: #334155; font-weight: 700; }
  .ha-rx-cov-bar { display: grid; gap: 3px; }
  .ha-rx-cov-bar span { height: 6px; border-radius: 3px; background: #E2E8F0; }
  .ha-rx-cov-bar span.on { background: #F59E0B; }
  .ha-rx-facts { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; margin: 0; }
  .ha-rx-facts div { min-width: 0; }
  .ha-rx-facts dt { font-size: 10.5px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: #94A3B8; }
  .ha-rx-facts dd { margin: 1px 0 0; font-size: 12.5px; color: #1E293B; word-break: break-word; }
  .ha-rx-facts .wide { grid-column: 1 / -1; }
  .ha-rx-skills { display: flex; flex-wrap: wrap; gap: 5px; }
  .ha-rx-skill { display: inline-flex; align-items: center; gap: 3px; font-size: 11.5px; color: #475569;
    background: #F8FAFC; border: 1px solid #E2E8F0; padding: 2px 8px; border-radius: 999px; }
  .ha-rx-skill-on { background: #FFF7EC; border-color: #FCD9A0; color: #92580B; font-weight: 600; }
  .ha-rx-skill-miss { border-style: dashed; color: #94A3B8; background: transparent; text-decoration: line-through; }
  .ha-rx-more { font-size: 11.5px; color: #94A3B8; padding: 2px 4px; }
  .ha-rx-foot { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap;
    margin-top: auto; padding-top: 10px; border-top: 1px solid #F1F5F9; }
  .ha-rx-hint { font-size: 11.5px; color: #94A3B8; display: inline-flex; align-items: center; gap: 4px; }
  .ha-rx-btn { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; text-decoration: none; white-space: nowrap;
    font: inherit; font-size: 12px; font-weight: 600; padding: 6px 11px; border-radius: 8px;
    background: rgba(255,255,255,.85); border: 1px solid rgba(148,163,184,.45); color: #334155; }
  .ha-rx-btn:hover { border-color: #94A3B8; }
  .ha-rx-btn:focus-visible { outline: 2px solid #2563EB; outline-offset: 2px; }
  .ha-rx-btn-primary { background: #2563EB; border-color: #2563EB; color: #fff; }
  .ha-rx-btn-primary:hover { background: #1E40AF; border-color: #1E40AF; }
  .ha-rx-btn-amber { background: #FFF7EC; border-color: #FCE3BC; color: #92580B; }
  .ha-rx-btn-amber:hover { border-color: #F59E0B; }

  .ha-rx-state { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 4px 14px; align-items: start;
    padding: 16px; border-radius: 12px; border: 1px solid #E2E8F0; background: rgba(255,255,255,.72); }
  .ha-rx-state-ico { grid-row: span 3; width: 38px; height: 38px; border-radius: 10px; display: flex;
    align-items: center; justify-content: center; background: #fff; color: #64748B; border: 1px solid #E2E8F0; }
  .ha-rx-state h3 { margin: 0; font-size: 14.5px; font-weight: 700; color: #1E293B; }
  .ha-rx-state p { margin: 0; font-size: 13px; color: #334155; max-width: 72ch; }
  .ha-rx-state-acts { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
  .ha-rx-state-bad { border-color: #FECACA; background: #FEF2F2; }
  .ha-rx-state-bad .ha-rx-state-ico { color: #B91C1C; border-color: #FECACA; }
  .ha-rx-state-warn { border-color: #FCE3BC; background: #FFF7EC; }
  .ha-rx-state-warn .ha-rx-state-ico { color: #92580B; border-color: #FCE3BC; }

  .ha-rx-search { margin-top: 18px; border: 1px solid #E2E8F0; border-radius: 12px; background: rgba(255,255,255,.72); }
  .ha-rx-search-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 12px 16px;
    border-bottom: 1px solid #F1F5F9; }
  .ha-rx-search-head h3 { margin: 0; font-size: 12px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase;
    color: #334155; display: inline-flex; align-items: center; gap: 6px; }
  .ha-rx-tierline { font-size: 12px; color: #64748B; }
  .ha-rx-tierline strong { color: #334155; }
  .ha-rx-search-head .ha-rx-btn { margin-left: auto; }
  .ha-rx-search-body { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr); }
  .ha-rx-col { padding: 14px 16px 16px; min-width: 0; display: flex; flex-direction: column; gap: 14px; }
  .ha-rx-col + .ha-rx-col { border-left: 1px solid #F1F5F9; }
  .ha-rx-mini { font-size: 10.5px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; color: #94A3B8; margin-bottom: 6px; }
  .ha-rx-extracted { font-weight: 500; text-transform: none; letter-spacing: 0; margin-left: 4px; }
  .ha-rx-query { margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; line-height: 1.6;
    color: #1E293B; background: #F1F5F9; border: 1px solid #E2E8F0; border-radius: 8px; padding: 10px 12px;
    white-space: pre-wrap; word-break: break-word; max-height: 160px; overflow: auto; }
  .ha-rx-op { color: #2563EB; font-weight: 700; }
  .ha-rx-query-acts { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 8px; }
  .ha-rx-copied { font-size: 12px; color: #047857; font-weight: 600; }
  .ha-rx-fchips { display: flex; flex-wrap: wrap; gap: 6px; }
  .ha-rx-fchip { font-size: 12px; color: #334155; background: #fff; border: 1px solid #E2E8F0; padding: 4px 10px; border-radius: 999px; }
  .ha-rx-fchip .k { color: #64748B; }
  .ha-rx-fchip.relaxed { border-style: dashed; color: #94A3B8; }
  .ha-rx-reqs { display: flex; flex-direction: column; gap: 6px; }
  .ha-rx-req { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; align-items: baseline; font-size: 12.5px; }
  .ha-rx-req .nm { color: #1E293B; font-weight: 600; }
  .ha-rx-req .var { color: #64748B; }
  .ha-rx-prio { font-size: 10px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; padding: 1px 7px;
    border-radius: 999px; border: 1px solid #E2E8F0; color: #64748B; }
  .ha-rx-prio.high { color: #92580B; border-color: #FCE3BC; background: #FFF7EC; }
  .ha-rx-plain { font-size: 12.5px; color: #334155; }
  .ha-rx .ha-en-foot { margin-top: 14px; }
  .ha-rx-err { margin-top: 12px; font-size: 12px; color: #94A3B8; }

  /* Outreach email box in the hero (top-right) — opens the /mail/:id page. */
  .ha-hero { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px 28px; align-items: start; }
  .ha-em-strip { width: 330px; max-width: 100%; border: 1px solid #E2E8F0; background: rgba(255,255,255,.78);
    border-radius: 12px; padding: 12px 14px; display: flex; flex-direction: column; gap: 9px; }
  .ha-em-strip-top { display: flex; align-items: center; gap: 7px; font-size: 10.5px; font-weight: 700; letter-spacing: .05em;
    text-transform: uppercase; color: #1E40AF; }
  .ha-em-strip-top svg { color: #2563EB; }
  .ha-em-count { margin-left: auto; font-weight: 500; text-transform: none; letter-spacing: 0; color: #94A3B8; font-size: 11px; }
  .ha-em-subject { font-size: 13px; font-weight: 600; color: #1E293B; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ha-em-steps { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; font-size: 11.5px; font-weight: 600; }
  .ha-em-step { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 999px; border: 1px solid transparent; }
  .ha-em-sep { color: #94A3B8; }
  .ha-em-ok { background: #ECFDF5; color: #047857; border-color: #A7F3D0; }
  .ha-em-info { background: #EFF6FF; color: #1D4ED8; border-color: #BFDBFE; }
  .ha-em-reply { background: #F5F3FF; color: #6D28D9; border-color: #DDD6FE; }
  .ha-em-bad { background: #FEF2F2; color: #B91C1C; border-color: #FECACA; }
  .ha-em-off { color: #94A3B8; border: 1px dashed #E2E8F0; }
  .ha-em-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
  .ha-em-to { font-size: 12px; color: #64748B; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  @media (max-width: 1080px) { .ha-rx-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @media (max-width: 900px) {
    .ha-hero { grid-template-columns: 1fr; }
    .ha-em-strip { width: 100%; }
    .ha-rx-search-body { grid-template-columns: 1fr; }
    .ha-rx-col + .ha-rx-col { border-left: none; border-top: 1px solid #F1F5F9; }
  }
  @media (max-width: 680px) {
    .ha-rx-grid { grid-template-columns: 1fr; }
    .ha-rx-acc { padding: 16px; }
    .ha-rx-body { padding: 0 16px 16px; }
    .ha-rx-search-head .ha-rx-btn { margin-left: 0; }
  }

  .ha-section { ${GLASS} border-radius: 14px; padding: 22px; }
  .ha-label { display: flex; align-items: center; gap: 7px;
    font-size: 11px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase;
    color: #1E40AF; margin-bottom: 14px; }
  .ha-label svg { color: #2563EB; }

  .ha-jd { font-size: 14.5px; line-height: 1.7; color: #334155; margin: 0; white-space: pre-wrap; }
  /* Rich (HTML) description branch — real block elements, so no pre-wrap. */
  .ha-jd-html { white-space: normal; }
  .ha-jd-html h1, .ha-jd-html h2, .ha-jd-html h3, .ha-jd-html h4 {
    font-size: 15px; font-weight: 700; color: #1E293B; margin: 16px 0 6px; }
  .ha-jd-html p { margin: 0 0 10px; }
  .ha-jd-html ul, .ha-jd-html ol { margin: 0 0 10px; padding-left: 22px; }
  .ha-jd-html li { margin: 2px 0; }
  .ha-jd-html strong, .ha-jd-html b { font-weight: 700; color: #1E293B; }
  .ha-jd-html a { color: #2563EB; text-decoration: none; }
  .ha-jd-html a:hover { text-decoration: underline; }
  /* The LLM-produced description HTML may now mirror LinkedIn's own layout
     (any structural tags survive the backend sanitizer) — keep the extras tidy. */
  .ha-jd-html div, .ha-jd-html section, .ha-jd-html article { margin: 0 0 10px; }
  .ha-jd-html table { border-collapse: collapse; margin: 0 0 10px; max-width: 100%; }
  .ha-jd-html th, .ha-jd-html td { border: 1px solid #E2E8F0; padding: 6px 10px; font-size: 13.5px; text-align: left; }
  .ha-jd-html th { background: #F8FAFC; font-weight: 600; }
  .ha-jd-html hr { border: 0; border-top: 1px solid #E2E8F0; margin: 14px 0; }
  .ha-jd-html blockquote { border-left: 3px solid #E2E8F0; margin: 0 0 10px; padding: 2px 0 2px 12px; color: #475569; }
  .ha-jd-html pre, .ha-jd-html code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; background: #F1F5F9; border-radius: 6px; }
  .ha-jd-html pre { padding: 10px 12px; overflow-x: auto; }
  .ha-jd-html code { padding: 1px 5px; }
  .ha-jd-html > *:first-child { margin-top: 0; }
  .ha-jd-html > *:last-child { margin-bottom: 0; }

  .ha-rows { display: flex; flex-direction: column; gap: 11px; }
  .ha-row { display: grid; grid-template-columns: 92px 1fr; gap: 12px; align-items: start; }
  .ha-key { font-size: 13px; color: #64748B; }
  .ha-val { font-size: 13.5px; color: #1E293B; word-break: break-word; }
  .ha-muted { color: #94A3B8; }
  .ha-src-tag { font-size: 10px; font-weight: 600; color: #64748B;
    text-transform: uppercase; letter-spacing: .03em; margin-right: 4px; }

  /* Company card — size badge + HQ location, laid out as two tidy cells. */
  .ha-company-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  .ha-company-item { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
  .ha-company-key { display: inline-flex; align-items: center; gap: 5px;
    font-size: 11px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; color: #64748B; }
  .ha-company-key svg { color: #94A3B8; }
  .ha-company-val { font-size: 14px; font-weight: 500; color: #1E293B; word-break: break-word; }
  .ha-sizewrap { display: flex; flex-direction: column; gap: 4px; align-items: flex-start; }
  .ha-sizebadge { font-size: 11px; font-weight: 700; letter-spacing: .02em;
    padding: 3px 11px; border-radius: 999px; white-space: nowrap; }
  .ha-sizecount { font-size: 12px; color: #94A3B8; }
  @media (max-width: 520px) { .ha-company-grid { grid-template-columns: 1fr; } }

  .ha-link { color: #2563EB; text-decoration: none; font-weight: 500;
    display: inline-flex; align-items: center; gap: 4px; }
  .ha-link:hover { text-decoration: underline; color: #1E40AF; }
  .ha-link:focus-visible { outline: 2px solid #2563EB; outline-offset: 2px; border-radius: 3px; }

  .ha-poster { display: flex; align-items: center; gap: 12px; margin-bottom: 16px;
    padding-bottom: 16px; border-bottom: 1px solid #F1F5F9; }
  .ha-avatar { width: 42px; height: 42px; border-radius: 10px; background: #EFF4FF;
    color: #2563EB; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
  .ha-poster-name { font-size: 14.5px; font-weight: 600; }
  .ha-poster-title { font-size: 12.5px; color: #64748B; }

  .ha-actions { display: flex; gap: 10px; margin-top: 16px; }
  .ha-cbtn { width: 44px; height: 44px; display: inline-flex; align-items: center; justify-content: center;
    border-radius: 10px; border: 1px solid transparent; text-decoration: none; transition: .15s; }
  .ha-cbtn:focus-visible { outline: 2px solid #2563EB; outline-offset: 2px; }
  .ha-cbtn-off { background: #F1F5F9; border-color: #E7ECF2; color: #B9C2CC; cursor: not-allowed; }
  .ha-cbtn-on-wa { background: rgba(255,255,255,.85); border-color: #C8EFD7; color: #1A7F4B; cursor: pointer; }
  .ha-cbtn-on-wa:hover { background: #ECFBF2; }
  .ha-cbtn-on-mail { background: rgba(255,255,255,.85); border-color: #93C5FD; color: #2563EB; cursor: pointer; }
  .ha-cbtn-on-mail:hover { background: #EFF4FF; }
  .ha-cbtn-on-li { background: #0A66C2; border-color: #0A66C2; color: #fff; cursor: pointer; }
  .ha-cbtn-on-li:hover { background: #084d92; }
  /* Outreach already sent — filled green so a contacted recruiter is obvious. */
  .ha-cbtn-sent { background: #ECFDF5; border-color: #86EFAC; color: #047857; cursor: pointer; }
  .ha-cbtn-sent:hover { background: #047857; color: #fff; }

  @media (max-width: 900px) {
    .ha-grid { grid-template-columns: 1fr; }
    .ha-container { padding: 18px 18px 36px; }
    .ha-title { font-size: 22px; }
    .ha-poc-body { flex-direction: column; align-items: stretch; gap: 16px; }
    .ha-poc .ha-poster { padding-right: 0; border-right: none;
      padding-bottom: 16px; border-bottom: 1px solid #F1F5F9; }
    .ha-poc .ha-actions { width: 100%; }
  }
  @media (max-width: 520px) {
    .ha-row { grid-template-columns: 1fr; gap: 2px; }
    .ha-actions { flex-direction: column; }
  }
  ${GLASS_FALLBACK(".ha-hero,.ha-section")}
`;

export default JobDetailsView;
