import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft, EyeOff, Building2, MapPin, Briefcase, Calendar, FileSpreadsheet, UserSearch, RefreshCw,
  ChevronDown, Check, Copy, ExternalLink, Lock, Search, Gauge, XCircle, Square, Monitor, Sparkles,
  FileText, FileSearch, AlertTriangle, Loader2, User,
} from "lucide-react";
import LiveBrowserView from "../components/LiveBrowserView";
import { SourceChip } from "../components/ui";
import { copyText } from "../lib/clipboard";
import { demandError, getDemandJob, rerunDemandJob } from "./demandApi";
import {
  HarvestRunningModal, Note, RowStatusPill, ROW_META, isActiveRow, fmtDay, fmtWhen, useDemandUsage,
  RESDEX_SEARCH_URL,
} from "./demandUi";

/* A Demand job's own page (/demand?batch=…&job=…) — not the harvest job page:
 * no apply link / company / point of contact / classification / outreach. The
 * Resdex section is open by default (requirements, candidates, the T1–T3 queries
 * with copy + Open Resdex search); "Re-run Resdex search" runs a fresh search for
 * this job only (POST /demand/jobs/{id}/rerun-resdex) and the page polls until it
 * finishes. Data: GET /demand/jobs/{id}. */

const POLL_MS = 4000;

// Callouts shown in place of the candidates when nothing was kept (row status).
const STATE_TXT = {
  no_results: { tone: "", Icon: Search, h: "No suitable candidates", p: "Resdex returned no profiles covering at least 60% of the must-have skills, even at tier 3. Run one of the tiers below by hand, or edit the JD to drop a must-have skill and re-run." },
  cap_reached: { tone: "warn", Icon: Gauge, h: "Daily Resdex limit reached", p: "Today's shared Resdex allowance ran out before this job was searched. Re-run it after the limit resets at 5:30 AM IST, or run one of the queries below by hand." },
  session_expired: { tone: "bad", Icon: Lock, h: "Naukri recruiter login needed", p: "The recruiter session expired before this job was searched. Log in again, then re-run this job. The queries below are ready if you want to search by hand now.", login: true },
  error: { tone: "", Icon: XCircle, h: "Resdex search failed", p: "Re-run this job. If it fails again, open the Live Browser and check that Resdex loads." },
  queries_only: { tone: "", Icon: FileSearch, h: "Queries ready, not searched", p: "Connect the Naukri recruiter session and re-run to find candidates, or copy a query into Resdex by hand.", login: true },
  stopped: { tone: "", Icon: Square, h: "Not searched: the batch was stopped", p: "The batch was stopped before this job ran. Re-run it to search Resdex for this job only." },
};

const TIER_LABEL = {
  1: "Strict: every must-have skill and a job title",
  2: "Balanced: lowest-priority skill dropped, job title or a nice-to-have skill",
  3: "Broad: tier 2's query, experience ±2 yrs, any location",
};

const asList = (v) => (Array.isArray(v) ? v : v == null || v === "" ? [] : [v]);
const lc = (s) => String(s || "").toLowerCase();
const normMust = (req) => asList(req?.mandatory_skills).map((m) => (typeof m === "string" ? { name: m } : m)).filter((m) => m?.name);

function displayName(raw) {
  const s = String(raw || "").replace(/\s+/g, " ").trim();
  if (!s || (s !== s.toUpperCase() && s !== s.toLowerCase())) return s;
  return s.toLowerCase().replace(/(^|[\s.'-])(\w)/g, (m, a, b) => a + b.toUpperCase());
}

function filterChips(filters) {
  const f = filters || {};
  const out = [];
  if (f.min_exp != null || f.max_exp != null) out.push(["Experience", `${f.min_exp ?? "any"}–${f.max_exp ?? "any"} yrs`]);
  if (asList(f.locations).length) out.push(["Locations", asList(f.locations).join(", ")]);
  if (f.active_within_months) out.push(["Active within", `${f.active_within_months} months`]);
  return out;
}

function QueryText({ query }) {
  return String(query).split(/\b(AND|OR|NOT)\b/).map((part, i) => (i % 2 ? <span key={i} className="dmj-op">{part}</span> : part));
}

function CopyButton({ text, targetRef }) {
  const [msg, setMsg] = useState("");
  const onCopy = async () => {
    if (await copyText(text)) { setMsg("Copied"); setTimeout(() => setMsg(""), 1800); return; }
    const el = targetRef.current;
    if (el) { const r = document.createRange(); r.selectNodeContents(el); const s = window.getSelection(); s.removeAllRanges(); s.addRange(r); }
    setMsg("Selected. Press Ctrl+C to copy");
  };
  return (
    <>
      <button type="button" className="ha-btn ha-btn-primary ha-btn-sm" onClick={onCopy}><Copy size={13} /> Copy query</button>
      <span className="dmj-copied" role="status" aria-live="polite">{msg}</span>
    </>
  );
}

function TierRow({ t }) {
  const ref = useRef(null);
  const chips = filterChips(t.filters);
  return (
    <div className={"dmj-tier" + (t.searched ? " searched" : "")}>
      <div className="dmj-tier-head">
        <strong>Tier {t.tier}</strong>
        {TIER_LABEL[t.tier] && <span className="dm-soft">{TIER_LABEL[t.tier]}</span>}
        {t.searched && <span className="dmj-tier-used">Searched</span>}
      </div>
      <pre className="dmj-query" ref={ref} tabIndex={0}><QueryText query={t.query} /></pre>
      {chips.length > 0 && (
        <div className="dmj-fchips">{chips.map(([k, v]) => <span key={k} className="dmj-fchip"><span className="k">{k}</span> {v}</span>)}</div>
      )}
      <div className="dmj-q-acts"><CopyButton text={t.query} targetRef={ref} /></div>
    </div>
  );
}

function CandidateCard({ c, must }) {
  const matched = new Set(asList(c.matched_skills).map(lc));
  const name = displayName(c.candidate_name) || `Candidate ${c.rank}`;
  const initials = displayName(c.candidate_name).split(/[\s.]+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
  const skills = asList(c.skills);
  const shown = [...skills].sort((a, b) => matched.has(lc(b)) - matched.has(lc(a))).slice(0, 12);
  const missing = must.map((m) => m.name).filter((m) => !matched.has(lc(m)));
  const cov = typeof c.skill_coverage === "number" ? Math.round(c.skill_coverage * 100) : null;
  const exp = c.experience_text || (c.experience_years != null ? `${c.experience_years} yrs` : "");
  const facts = [
    ["Experience", exp], ["Notice", c.notice_period], ["Location", c.location],
    ["Prefers", asList(c.preferred_locations).join(", ")], ["CTC", c.ctc], ["Last active", c.last_active],
  ].filter(([, v]) => v);
  const link = c.profile_url || c.resdex_url;
  return (
    <article className="dmj-card" aria-label={name}>
      <div className="dmj-head">
        <div className="dmj-avatar">{initials || <User size={16} />}<span className="dmj-rank">#{c.rank}</span></div>
        <div style={{ minWidth: 0 }}>
          <div className="dmj-name">{name}</div>
          <div className="dmj-role">{c.current_designation || c.headline || "—"}{c.current_company && <span className="dm-soft"> @ {c.current_company}</span>}</div>
        </div>
        {c.naukri_verified_badge && <span className="dmj-verified"><Check size={11} strokeWidth={3} /> Naukri Verified</span>}
      </div>
      {(must.length > 0 || cov != null) && (
        <div>
          <div className="dmj-cov-top">
            <span>{must.length ? <><strong>{must.length - missing.length} of {must.length}</strong> must-have skills</> : "Skill coverage"}</span>
            {cov != null && <strong>{cov}%</strong>}
          </div>
          {must.length > 0 && (
            <div className="dmj-cov-bar" style={{ gridTemplateColumns: `repeat(${must.length}, 1fr)` }} aria-hidden="true">
              {must.map((m) => <span key={m.name} className={matched.has(lc(m.name)) ? "on" : ""} title={m.name} />)}
            </div>
          )}
        </div>
      )}
      <dl className="dmj-facts">{facts.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
      {(shown.length > 0 || missing.length > 0) && (
        <div className="dmj-skills">
          {shown.map((s, i) => (matched.has(lc(s))
            ? <span key={`${s}-${i}`} className="dmj-skill on"><Check size={11} />{s}</span>
            : <span key={`${s}-${i}`} className="dmj-skill">{s}</span>))}
          {missing.map((m) => <span key={`m-${m}`} className="dmj-skill miss" title="Must-have skill not on this profile">{m}</span>)}
        </div>
      )}
      <div className="dmj-foot">
        {link ? (
          <a className="dmj-btn amber" href={link} target="_blank" rel="noreferrer"
            title={c.profile_url ? "" : `Opens the Resdex search this candidate came from${c.resdex_position ? ` — result #${c.resdex_position}` : ""}.`}>
            {c.profile_url ? "Open in Naukri" : `Find in Resdex${c.resdex_position ? ` (#${c.resdex_position})` : ""}`} <ExternalLink size={13} />
          </a>
        ) : <span className="dm-muted">No profile link</span>}
        <span className="dm-soft" style={{ fontSize: 11.5, display: "inline-flex", alignItems: "center", gap: 4 }}><Lock size={12} /> Recruiter login</span>
      </div>
    </article>
  );
}

function ResdexSection({ data, onLogin }) {
  const [open, setOpen] = useState(true);
  const { resdex, row, job } = data;
  const status = row?.status || resdex?.status;
  const cands = resdex?.candidates || [];
  const req = resdex?.requirements || {};
  const must = normMust(req);
  const optional = asList(req.optional_skills).map((o) => (typeof o === "string" ? o : o?.name)).filter(Boolean);
  const titles = asList(req.job_titles);
  const ok = (status === "matched" || status === "reused") && cands.length > 0;
  const st = STATE_TXT[status];
  const badge = isActiveRow(row) ? "Searching" : ok ? `${cands.length} matched` : (ROW_META[status]?.label || "Not searched");
  const when = status === "reused" ? `Reused match from ${fmtDay(row?.reused_from || resdex?.searched_at)}`
    : ["cap_reached", "session_expired", "stopped", "queries_only"].includes(status) ? "Not searched yet"
      : resdex?.searched_at ? `Searched ${fmtWhen(resdex.searched_at)}` : "";
  const tierLine = [];
  if (resdex?.tier && resdex?.searches_used) tierLine.push(`Tier ${resdex.tier}`);
  if (resdex?.result_count) tierLine.push(`${Number(resdex.result_count).toLocaleString("en-IN")} profiles in Resdex`);
  if (resdex?.searches_used) tierLine.push(`${resdex.searches_used} search${resdex.searches_used === 1 ? "" : "es"} used`);

  return (
    <section className="ha-card dmj-rx" aria-label="Naukri Resdex candidates">
      <button type="button" className="dmj-acc" aria-expanded={open} aria-controls="dmj-rx-body" onClick={() => setOpen((v) => !v)}>
        <span className="dmj-acc-title"><UserSearch size={14} /> Naukri Resdex candidates</span>
        <span className="dmj-badge">{badge}</span>
        <span className="dmj-when">{when}</span>
        <ChevronDown size={16} style={{ transform: open ? "rotate(180deg)" : "none", transition: "transform .2s", color: "#92580B" }} />
      </button>
      {open && (
        <div id="dmj-rx-body" className="dmj-rx-body">
          {isActiveRow(row) && (
            <Note icon={Loader2}><b>Searching.</b> {row.sub_step === "searching" ? "Searching Resdex." : "Extracting requirements, then T1."} This page updates when it finishes (usually 1 to 3 minutes).</Note>
          )}
          {resdex && (must.length > 0 || titles.length > 0) && (
            <div className="dmj-req">
              <div className="wide">
                <div className="dmj-mini">
                  Must-have skills{" "}
                  <span className={"dmj-by " + (req.extracted_by === "fallback" ? "fb" : "llm")}>
                    <Sparkles size={11} /> {req.extracted_by === "fallback" ? "Fallback: Skills column (LLM unavailable)" : "Extracted by LLM · title, JD + optional columns"}
                  </span>
                </div>
                <div className="dmj-skills">
                  {must.map((m) => <span key={m.name} className="dmj-skill must">{m.name}{m.priority != null && <span className="prio">P{m.priority}</span>}</span>)}
                </div>
                {optional.length > 0 && (
                  <>
                    <div className="dmj-mini" style={{ marginTop: 10 }}>Nice to have</div>
                    <div className="dmj-skills">{optional.map((s) => <span key={s} className="dmj-skill">{s}</span>)}</div>
                  </>
                )}
              </div>
              <div>
                <div className="dmj-mini">Experience &amp; location</div>
                <div className="dmj-plain">
                  {req.min_exp != null || req.max_exp != null ? `${req.min_exp ?? "any"}–${req.max_exp ?? "any"} yrs` : (job.experience || "Not stated")}
                  <br />{asList(req.locations).join(", ") || job.location || "Any location"}
                </div>
                {titles.length > 0 && (
                  <>
                    <div className="dmj-mini" style={{ marginTop: 10 }}>Job titles searched</div>
                    <div className="dmj-plain">{titles.join(" · ")}</div>
                  </>
                )}
              </div>
              <div>
                <div className="dmj-mini">Search result</div>
                <div className="dmj-plain">{tierLine.length ? tierLine.join(" · ") : <span className="dm-muted">Not searched</span>}</div>
                {req.extracted_by === "fallback" && (
                  <div style={{ marginTop: 10 }}>
                    <Note tone="warn" icon={AlertTriangle}>
                      The LLM was unavailable, so the must-have skills came from the file's Skills column. Normally the optional columns
                      are sent to the LLM as extra context with the title and JD.
                    </Note>
                  </div>
                )}
              </div>
            </div>
          )}

          {ok ? (
            <>
              <div className="dmj-summary">
                <span>Ranked by must-have skill coverage. Only profiles at 60% or above are kept.</span>
                {status === "reused" && <span><strong>Reused</strong> from the search on {fmtDay(row?.reused_from || resdex?.searched_at)}</span>}
              </div>
              <div className="dmj-grid">{cands.map((c) => <CandidateCard key={c.id} c={c} must={must} />)}</div>
            </>
          ) : st && !isActiveRow(row) ? (
            <div className={`dm-state ${st.tone}`}>
              <span className="dm-state-ico"><st.Icon size={18} /></span>
              <h4>{st.h}</h4>
              <p>{st.p}{status === "error" && row?.error ? ` (${row.error})` : ""}</p>
              {st.login && (
                <div className="dm-state-acts">
                  <button type="button" className="ha-btn ha-btn-primary ha-btn-sm" onClick={onLogin}><Monitor size={13} /> Open Live Browser to log in</button>
                </div>
              )}
            </div>
          ) : null}

          {asList(resdex?.tiers).length > 0 && (
            <div className="dmj-search">
              <div className="dmj-search-head">
                <h4><Search size={13} /> Search tiers</h4>
                <span className="dm-soft" style={{ fontSize: 12 }}>Copy any query into Resdex Keywords with <strong>Boolean</strong> turned on</span>
                <a className="dmj-btn" href={resdex.resdex_url || RESDEX_SEARCH_URL} target="_blank" rel="noreferrer">
                  {resdex.resdex_url ? "Open these results in Resdex" : "Open Resdex search"} <ExternalLink size={13} />
                </a>
              </div>
              <div className="dmj-tiers">{resdex.tiers.map((t) => <TierRow key={t.tier} t={t} />)}</div>
            </div>
          )}
          <div className="dmj-en-foot"><Lock size={13} /> Opening a profile in Naukri needs the recruiter login and may use a profile-view credit.</div>
        </div>
      )}
    </section>
  );
}

export default function DemandJobDetail({ jobId, runId, onBack }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [rerun, setRerun] = useState("idle");   // idle | ask | starting
  const [rerunErr, setRerunErr] = useState(null);
  const [modal, setModal] = useState(null);
  const sess = useDemandUsage();
  const timer = useRef(null);
  const alive = useRef(true);

  const load = useCallback(async () => {
    try {
      const res = await getDemandJob(jobId);
      if (!alive.current) return null;
      setData(res);
      setError("");
      return res;
    } catch (err) {
      if (alive.current) setError(demandError(err).message);
      return null;
    }
  }, [jobId]);

  const poll = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      const res = await load();
      if (alive.current && res && isActiveRow(res.row)) poll();
      else if (alive.current) sess.refresh();
    }, POLL_MS);
  }, [load, sess]);

  useEffect(() => {
    alive.current = true;
    load().then((res) => { if (res && isActiveRow(res.row)) poll(); });
    return () => { alive.current = false; if (timer.current) clearTimeout(timer.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  const doRerun = async () => {
    setRerun("starting");
    setRerunErr(null);
    try {
      await rerunDemandJob(jobId);
      await load();
      poll();
    } catch (err) {
      const e = demandError(err);
      if (e.code === "harvest_running") setModal(e.detail.harvest || {});
      else setRerunErr(e);
    }
    setRerun("idle");
  };

  if (!data) {
    return (
      <div className="ha-detail-page">
        <button type="button" className="dm-back" onClick={onBack}><ArrowLeft size={16} /> Back to batch</button>
        <div style={{ marginTop: 16 }} className={error ? "ha-errbanner" : "dm-soft"}>{error || "Loading job…"}</div>
      </div>
    );
  }

  const { job, batch, row, context } = data;
  const active = isActiveRow(row);
  const u = sess.usage?.usage || {};
  const left = u.searches != null && u.search_cap ? `${Math.max(0, u.search_cap - u.searches)} searches and ${Math.max(0, (u.candidate_cap || 0) - (u.candidates || 0))} candidates left` : "";
  const ctxRows = [["Skills", context.skills], ["Experience", context.experience], ["Location", context.location], ["Client", context.client]];

  return (
    <div className="ha-detail-page">
      <style>{styles}</style>
      {modal && <HarvestRunningModal info={modal} onClose={() => setModal(null)} />}
      {sess.live && <LiveBrowserView title="Naukri recruiter login — live browser" onClose={() => { sess.setLive(false); sess.refresh(); }} />}

      <div className="dmj-topbar">
        <button type="button" className="dm-back" onClick={onBack}><ArrowLeft size={16} /> Back to batch</button>
        <span className="dmj-viewbadge"><EyeOff size={13} /> Demand job · view only</span>
      </div>
      {error && <div className="ha-errbanner" style={{ marginBottom: 12 }}>{error}</div>}

      <header className="ha-card dmj-hero">
        <div style={{ minWidth: 0 }}>
          <div className="dmj-hero-meta">
            <SourceChip source="Demand" />
            <span className="dmj-hero-co"><Building2 size={14} /> {context.client ? `Client: ${context.client}` : <span className="dm-muted">No client given</span>}</span>
            <span className="dm-soft" style={{ fontSize: 12 }}>Row {row?.row ?? "—"} · Demand Search batch {batch?.run_id || runId}</span>
          </div>
          <h2 className="dmj-title">{job.job_title}</h2>
          <div className="dmj-chips">
            {job.location && <span className="dmj-chip"><MapPin size={14} />{job.location}</span>}
            {job.experience && <span className="dmj-chip"><Briefcase size={14} />{job.experience}</span>}
            {job.posted_date && <span className="dmj-chip"><Calendar size={14} />Uploaded {fmtDay(job.posted_date)}</span>}
            {batch?.filename && <span className="dmj-chip"><FileSpreadsheet size={14} />{batch.filename}</span>}
            <RowStatusPill row={row} />
          </div>
        </div>
        <aside className="dmj-act" aria-label="Resdex actions">
          <div className="dmj-act-top"><UserSearch size={13} /> Resdex search</div>
          {rerun === "ask" ? (
            <div className="dm-confirm pale">
              <p>
                <b>Search Resdex again for this job?</b> This uses up to 3 searches and 3 candidates from today's shared quota
                {left ? ` (${left})` : ""}. The newest search is what this page shows.
              </p>
              <div className="acts">
                <button type="button" className="ha-btn ha-btn-primary ha-btn-sm" onClick={doRerun}>Re-run now</button>
                <button type="button" className="ha-btn ha-btn-secondary ha-btn-sm" onClick={() => setRerun("idle")}>Cancel</button>
              </div>
            </div>
          ) : active ? (
            <Note icon={Loader2}><b>Re-running.</b> {row.sub_step === "searching" ? "Searching Resdex." : "Extracting requirements, then T1."} This page updates when it finishes.</Note>
          ) : (
            <>
              <button type="button" className="ha-btn ha-btn-primary ha-btn-sm" onClick={() => { setRerun("ask"); sess.refresh(); }} disabled={rerun === "starting" || batch?.status === "running"}>
                {rerun === "starting" ? <Loader2 size={14} className="ha-spin" /> : <RefreshCw size={14} />} Re-run Resdex search
              </button>
              <p>
                {batch?.status === "running" ? "Available once the batch has finished."
                  : row?.status === "queries_only"
                    ? "Searches Resdex if the recruiter session is connected; otherwise the queries are rebuilt."
                    : `Fresh search for this job only. ${row?.status === "reused" ? `This job reused a match from ${fmtDay(row.reused_from)}.` : "Uses today's shared quota."}`}
              </p>
            </>
          )}
          {rerunErr && (
            <div className="ha-errbanner" style={{ fontSize: 12.5 }}>
              {rerunErr.message}
              {rerunErr.code?.startsWith("session") && (
                <div style={{ marginTop: 6 }}>
                  <button type="button" className="ha-btn ha-btn-primary ha-btn-sm" onClick={sess.connect} disabled={sess.connecting}><Monitor size={13} /> Connect</button>
                </div>
              )}
            </div>
          )}
        </aside>
      </header>

      <ResdexSection data={data} onLogin={sess.connect} />

      <div className="dmj-dgrid">
        <main className="ha-card dmj-section">
          <div className="dmj-label"><FileText size={14} /> Job Description</div>
          <p className="dmj-jd">{job.job_description}</p>
        </main>
        <aside className="ha-card dmj-section">
          <div className="dmj-label"><FileSpreadsheet size={14} /> Demand Search batch</div>
          <div className="dmj-rows">
            <div className="dmj-krow"><span className="k">Batch</span><span className="v">{batch?.batch_name || batch?.run_id}</span></div>
            <div className="dmj-krow"><span className="k">Batch ID</span><span className="v">{batch?.run_id}</span></div>
            <div className="dmj-krow"><span className="k">File row</span><span className="v">{row?.row ?? "—"}</span></div>
            <div className="dmj-krow"><span className="k">Uploaded by</span><span className="v">{batch?.uploaded_by || "—"}</span></div>
          </div>
          <div className="dmj-label" style={{ margin: "16px 0 10px" }}><Sparkles size={14} /> Extra context sent to LLM</div>
          <div className="dmj-rows">
            {ctxRows.map(([k, v]) => (
              <div key={k} className="dmj-krow"><span className="k">{k}</span><span className="v">{v || <span className="dm-muted">Not in file</span>}</span></div>
            ))}
          </div>
          <p className="dm-soft" style={{ fontSize: 12, margin: "10px 0 0" }}>Sent with the title and JD. The LLM uses them as hints and does not treat them as overrides.</p>
        </aside>
      </div>
    </div>
  );
}

const styles = `
  .dmj-topbar{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:14px;}
  .dmj-viewbadge{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;color:#64748B;padding:7px 12px;border-radius:8px;border:1px solid rgba(148,163,184,.45);background:rgba(255,255,255,.75);}
  .dmj-hero{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:18px 28px;padding:16px 22px;margin-bottom:14px;}
  .dmj-hero-meta{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px;}
  .dmj-hero-co{display:inline-flex;align-items:center;gap:6px;color:#475569;font-size:14px;font-weight:500;}
  .dmj-title{font-size:24px;font-weight:700;line-height:1.25;margin:0 0 14px;color:#1E293B;}
  .dmj-chips{display:flex;flex-wrap:wrap;gap:8px;align-items:center;}
  .dmj-chip{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:#334155;background:rgba(248,250,252,.7);border:1px solid rgba(203,213,225,.6);padding:6px 12px;border-radius:999px;}
  .dmj-chip svg{color:#64748B;}
  .dmj-act{border:1px solid #E2E8F0;background:rgba(255,255,255,.78);border-radius:12px;padding:12px 14px;display:flex;flex-direction:column;gap:9px;align-items:flex-start;}
  .dmj-act p{margin:0;font-size:12px;color:#64748B;}
  .dmj-act-top{display:flex;align-items:center;gap:7px;font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#1E40AF;}
  .dmj-rx{padding:0;margin-bottom:14px;}
  .dmj-acc{display:flex;align-items:center;gap:8px 10px;flex-wrap:wrap;width:100%;background:none;border:0;cursor:pointer;text-align:left;font:inherit;padding:16px 20px;border-radius:12px;}
  .dmj-acc:focus-visible{outline:2px solid #2563EB;outline-offset:-2px;}
  .dmj-acc-title{display:inline-flex;align-items:center;gap:7px;font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#92580B;}
  .dmj-acc-title svg{color:#F59E0B;}
  .dmj-badge{font-size:10px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;background:#FFF7EC;color:#92580B;border:1px solid #FCE3BC;padding:2px 8px;border-radius:999px;}
  .dmj-when{font-size:11px;color:#94A3B8;margin-left:auto;}
  .dmj-rx-body{padding:0 20px 20px;display:flex;flex-direction:column;gap:14px;}
  .dmj-req{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr) minmax(0,1fr);gap:14px;border:1px solid #E2E8F0;border-radius:12px;padding:14px;background:rgba(255,255,255,.72);}
  .dmj-mini{font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#94A3B8;margin-bottom:6px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;}
  .dmj-by{display:inline-flex;align-items:center;gap:4px;text-transform:none;letter-spacing:0;font-weight:600;padding:1px 8px;border-radius:999px;}
  .dmj-by.llm{background:#F5F3FF;color:#6D28D9;}.dmj-by.fb{background:#FFFBEB;color:#B45309;}
  .dmj-plain{font-size:12.5px;color:#334155;}
  .dmj-summary{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12.5px;color:#64748B;}
  .dmj-summary strong{color:#334155;}
  .dmj-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;}
  .dmj-card{display:flex;flex-direction:column;gap:12px;min-width:0;background:rgba(255,255,255,.78);border:1px solid #F1E3CC;border-radius:12px;padding:14px;}
  .dmj-head{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:10px;align-items:start;}
  .dmj-avatar{width:38px;height:38px;border-radius:10px;background:#FEF3E2;color:#B45309;display:grid;place-items:center;font-size:13px;font-weight:700;position:relative;}
  .dmj-rank{position:absolute;top:-7px;left:-7px;font-size:10px;font-weight:800;background:#fff;color:#92580B;border:1px solid #FCE3BC;border-radius:999px;padding:0 5px;line-height:16px;}
  .dmj-name{font-size:14.5px;font-weight:600;color:#1E293B;}
  .dmj-role{font-size:12.5px;color:#334155;word-break:break-word;}
  .dmj-verified{display:inline-flex;align-items:center;gap:3px;font-size:10.5px;font-weight:700;white-space:nowrap;color:#16794A;background:#E9F7EF;border:1px solid #BFE5CF;padding:1px 8px;border-radius:999px;}
  .dmj-cov-top{display:flex;justify-content:space-between;font-size:11.5px;color:#64748B;margin-bottom:5px;}
  .dmj-cov-top strong{color:#334155;}
  .dmj-cov-bar{display:grid;gap:3px;}
  .dmj-cov-bar span{height:6px;border-radius:3px;background:#E2E8F0;}
  .dmj-cov-bar span.on{background:#F59E0B;}
  .dmj-facts{display:grid;grid-template-columns:1fr 1fr;gap:8px 12px;margin:0;}
  .dmj-facts dt{font-size:10.5px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:#94A3B8;}
  .dmj-facts dd{margin:1px 0 0;font-size:12.5px;color:#1E293B;word-break:break-word;}
  .dmj-skills{display:flex;flex-wrap:wrap;gap:5px;}
  .dmj-skill{display:inline-flex;align-items:center;gap:4px;font-size:11.5px;color:#475569;background:#F8FAFC;border:1px solid #E2E8F0;padding:2px 8px;border-radius:999px;}
  .dmj-skill.on{background:#FFF7EC;border-color:#FCD9A0;color:#92580B;font-weight:600;}
  .dmj-skill.miss{border-style:dashed;color:#94A3B8;background:transparent;text-decoration:line-through;}
  .dmj-skill.must{background:#FFF7EC;border-color:#FCE3BC;color:#92580B;font-weight:600;}
  .dmj-skill .prio{font-size:9.5px;font-weight:800;background:#fff;border-radius:999px;padding:0 5px;border:1px solid #FCE3BC;}
  .dmj-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-top:auto;padding-top:10px;border-top:1px solid #F1F5F9;}
  .dmj-btn{display:inline-flex;align-items:center;gap:6px;text-decoration:none;white-space:nowrap;font-size:12px;font-weight:600;padding:6px 11px;border-radius:8px;background:rgba(255,255,255,.85);border:1px solid rgba(148,163,184,.45);color:#334155;}
  .dmj-btn:hover{border-color:#94A3B8;}
  .dmj-btn.amber{background:#FFF7EC;border-color:#FCE3BC;color:#92580B;}
  .dmj-search{border:1px solid #E2E8F0;border-radius:12px;background:rgba(255,255,255,.72);}
  .dmj-search-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 14px;border-bottom:1px solid #F1F5F9;}
  .dmj-search-head h4{margin:0;font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#334155;display:inline-flex;align-items:center;gap:6px;}
  .dmj-search-head .dmj-btn{margin-left:auto;}
  .dmj-tiers{display:flex;flex-direction:column;gap:10px;padding:14px;}
  .dmj-tier{border:1px solid #E2E8F0;border-radius:10px;padding:10px 12px;background:#fff;display:flex;flex-direction:column;gap:8px;min-width:0;}
  .dmj-tier.searched{border-color:#FCE3BC;background:#FFF7EC;}
  .dmj-tier-head{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;font-size:12.5px;color:#334155;}
  .dmj-tier-used{margin-left:auto;font-size:11px;font-weight:700;color:#92580B;background:#fff;border:1px solid #FCE3BC;padding:1px 8px;border-radius:999px;}
  .dmj-query{margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;line-height:1.6;color:#1E293B;background:#F1F5F9;border:1px solid #E2E8F0;border-radius:8px;padding:10px 12px;white-space:pre-wrap;word-break:break-word;max-height:160px;overflow:auto;}
  .dmj-op{color:#2563EB;font-weight:700;}
  .dmj-fchips{display:flex;flex-wrap:wrap;gap:6px;}
  .dmj-fchip{font-size:12px;color:#334155;background:#fff;border:1px solid #E2E8F0;padding:4px 10px;border-radius:999px;}
  .dmj-fchip .k{color:#64748B;}
  .dmj-q-acts{display:flex;align-items:center;gap:10px;flex-wrap:wrap;}
  .dmj-copied{font-size:12px;color:#047857;font-weight:600;}
  .dmj-en-foot{display:flex;align-items:center;gap:7px;font-size:12px;color:#92580B;background:#FFF7EC;border:1px solid #FCE3BC;border-radius:9px;padding:9px 13px;}
  .dmj-dgrid{display:grid;grid-template-columns:minmax(0,2.2fr) minmax(300px,1fr);gap:14px;align-items:start;}
  .dmj-section{padding:18px 20px;}
  .dmj-label{display:flex;align-items:center;gap:7px;font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:#1E40AF;margin-bottom:12px;}
  .dmj-label svg{color:#2563EB;}
  .dmj-jd{font-size:14.5px;line-height:1.7;color:#334155;margin:0;white-space:pre-wrap;word-break:break-word;}
  .dmj-rows{display:flex;flex-direction:column;gap:9px;}
  .dmj-krow{display:grid;grid-template-columns:96px minmax(0,1fr);gap:10px;font-size:13px;}
  .dmj-krow .k{color:#64748B;}.dmj-krow .v{color:#1E293B;word-break:break-word;}
  @media(max-width:1080px){.dmj-grid{grid-template-columns:repeat(2,minmax(0,1fr));}}
  @media(max-width:900px){.dmj-hero,.dmj-dgrid,.dmj-req{grid-template-columns:1fr;}.dmj-title{font-size:21px;}}
  @media(max-width:680px){.dmj-grid{grid-template-columns:1fr;}.dmj-search-head .dmj-btn{margin-left:0;}}
`;
