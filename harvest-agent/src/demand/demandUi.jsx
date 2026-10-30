import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckCircle2, RotateCcw, Search, FileSearch, Gauge, Lock, XCircle, Square, Clock, Loader2, Check,
  AlertTriangle, X, Hand,
} from "lucide-react";
import { startNaukriRecruiterLogin } from "../api";
import { C, STATUS_TONES, GLASS_MODAL } from "../theme";
import { demandError, getDemandUsage } from "./demandApi";

/* Shared pieces of the Demand Search pages (src/demand/*): status metadata, pills,
 * the stepper, the session/quota pills, the "A harvest is running" modal, the
 * role="switch" toggle, the recruiter-session hook and the page CSS (dm- prefix,
 * layered on the app's ha-* classes from ThemeStyles). Nothing here is used by
 * the harvest pages. */

// Copy shared by the tooltips, the pre-flight card and the batch panel.
export const SESSION_TIP =
  "Green only when the last recorded recruiter check says logged_in. Connect (Naukri Launcher login), " +
  "verify and harvest pre-checks write that record. Checked again when the batch starts.";
export const QUOTA_TIP =
  "One account-wide Resdex quota per UTC day (resets 5:30 AM IST), shared with harvest outreach.";
export const RESDEX_SEARCH_URL = "https://resdex.naukri.com/v3?activeTab=advSrch";

// Row statuses (GET /demand/batches/{run_id} rows[].status).
export const ROW_META = {
  matched:         { label: "Matched", Icon: CheckCircle2, tone: "good", chip: "done" },
  reused:          { label: "Reused", Icon: RotateCcw, tone: "reuse", chip: "reuse" },
  no_results:      { label: "No match", Icon: Search, tone: "neutral", chip: "nf" },
  // Recruiter not logged in: requirements + T1–T3 queries built, Resdex not searched.
  queries_only:    { label: "Queries only · not searched", Icon: FileSearch, tone: "info", chip: "query" },
  cap_reached:     { label: "Daily cap reached", Icon: Gauge, tone: "amber", chip: "cap" },
  session_expired: { label: "Login needed", Icon: Lock, tone: "bad", chip: "fail" },
  error:           { label: "Error", Icon: XCircle, tone: "bad", chip: "fail" },
  stopped:         { label: "Stopped", Icon: Square, tone: "stopped", chip: "stop" },
  queued:          { label: "Queued", Icon: Clock, tone: "neutral", chip: "queue" },
  in_progress:     { label: "In progress", Icon: Loader2, tone: "running", chip: "proc" },
};
export const ATTENTION = ["cap_reached", "session_expired", "error"];
export const ACTIVE = ["queued", "in_progress"];
export const isActiveRow = (r) => ACTIVE.includes(r?.status);

const SUB_STEP = { requirements: "Extracting requirements", searching: "Searching Resdex" };
export const subStepLabel = (r) => SUB_STEP[r?.sub_step] || "In progress";

const TONES = {
  good:    { background: "#ECFDF5", color: "#047857" },
  reuse:   { background: "#EFF6FF", color: "#1D4ED8" },
  neutral: { background: "#F1F5F9", color: "#475569" },
  info:    { background: "#F0F9FF", color: "#0369A1" },
  amber:   { background: "#FFF7EC", color: "#92580B" },
  bad:     { background: "#FEF2F2", color: "#B91C1C" },
  stopped: { background: "#FFF7ED", color: "#9A3412" },
  running: { background: "#FFFBEB", color: "#B45309" },
};

/** Resdex status pill for one row; in-progress rows show their sub-step. */
export function RowStatusPill({ row }) {
  const meta = ROW_META[row?.status] || ROW_META.error;
  const Icon = meta.Icon;
  const label = row?.status === "in_progress" ? subStepLabel(row) : meta.label;
  return (
    <span className="ha-pill dm-pill" style={TONES[meta.tone]}>
      <Icon size={12} className={row?.status === "in_progress" ? "ha-spin" : ""} /> {label}
    </span>
  );
}

const BATCH_LABEL = { success: "completed", stopped: "stopped", failed: "failed", running: "running" };

/** Batch status pill — the app's STATUS_TONES, "success" read as "completed". */
export function BatchStatusPill({ status }) {
  const tone = STATUS_TONES[status] || STATUS_TONES.no_results;
  const Icon = tone.Icon;
  return (
    <span className="ha-pill dm-pill" style={{ background: tone.background, color: tone.color }}>
      <Icon size={12} className={status === "running" ? "ha-spin" : ""} /> {BATCH_LABEL[status] || status}
    </span>
  );
}

const STEPS = ["Upload", "Validate & map", "Pre-flight", "Search", "Results"];

export function Stepper({ active }) {
  return (
    <div className="dm-stepper" aria-label="Progress">
      {STEPS.map((s, i) => {
        const n = i + 1;
        const cls = n < active ? "done" : n === active ? "active" : "";
        return (
          <div key={s} className="dm-step-wrap">
            {i > 0 && <div className={"dm-step-line" + (n <= active ? " done" : "")} />}
            <div className={"dm-step " + cls} aria-current={n === active ? "step" : undefined}>
              <span className="dm-dot">{n < active ? <Check size={14} /> : n}</span>
              <span className="dm-step-t">{s}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Formatting ───────────────────────────────────────────────────────────────

export function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function fmtDay(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function fmtAgo(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const min = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (min < 1) return "just now";
  if (min < 60) return `${min} min ago`;
  const h = Math.round(min / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

/** "45 min – 2 h 10 min" for n rows at 1–3 min each ("" for none). */
export function fmtDuration(n) {
  if (!n) return "";
  const part = (m) => (m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ""}`);
  return `${part(n)} – ${part(n * 3)}`;
}

export const pct = (v) => (typeof v === "number" ? Math.round(v * 100) : null);

// ── Recruiter session + quota ─────────────────────────────────────────────────

const LOGIN_MAX_MS = 11 * 60 * 1000;

/** GET /demand/resdex-usage, refreshed on demand, plus Connect: the existing Naukri
 * recruiter login flow (POST /naukri-recruiter/login) watched until the recorded
 * session says logged_in. `live` opens the Live Browser view for the login. */
export function useDemandUsage() {
  const [usage, setUsage] = useState(null);
  const [error, setError] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [live, setLive] = useState(false);
  const [note, setNote] = useState("");
  const alive = useRef(true);
  const timer = useRef(null);
  const deadline = useRef(0);

  const refresh = useCallback(async () => {
    try {
      const res = await getDemandUsage();
      if (!alive.current) return null;
      setUsage(res);
      setError("");
      return res;
    } catch (err) {
      if (alive.current) setError(demandError(err).message);
      return null;
    }
  }, []);

  const stopWatch = () => { if (timer.current) { clearTimeout(timer.current); timer.current = null; } };

  const seenOpen = useRef(false);

  // Follow the login window until the recorded session says logged_in, the window
  // closes without a login, or LOGIN_MAX_MS passes.
  const watch = useCallback(() => {
    stopWatch();
    timer.current = setTimeout(async () => {
      const res = await refresh();
      if (!alive.current) return;
      const s = res?.session;
      if (s?.login_window_open) seenOpen.current = true;
      if (s?.logged_in && !s.login_window_open) { setConnecting(false); setNote(""); return; }
      if (Date.now() > deadline.current || (s && seenOpen.current && !s.login_window_open)) {
        setConnecting(false);
        setNote(s?.logged_in ? "" : "The login window closed without a recruiter login. Connect again to retry.");
        return;
      }
      watch();
    }, 3000);
  }, [refresh]);

  useEffect(() => {
    alive.current = true;
    refresh();
    return () => { alive.current = false; stopWatch(); };
  }, [refresh]);

  const connect = useCallback(async () => {
    setConnecting(true);
    setNote("Opening the Naukri recruiter login window…");
    try {
      const res = await startNaukriRecruiterLogin(600);
      setNote(res.status === "busy" ? "A login window is already open — finish logging in in the Live Browser." : "Log in once in the Live Browser.");
      setLive(true);
      seenOpen.current = false;
      deadline.current = Date.now() + LOGIN_MAX_MS;
      watch();
    } catch (err) {
      setConnecting(false);
      setNote(demandError(err).message);
    }
  }, [watch]);

  return { usage, error, refresh, connect, connecting, live, setLive, note };
}

/** "Connected" only when the recorded check says logged_in. */
export function sessionState(usage) {
  const s = usage?.session;
  if (!s) return { key: "none", label: "Not connected", pill: TONES.neutral, dot: "off" };
  if (s.logged_in && !s.login_window_open) return { key: "ok", label: "Connected", pill: TONES.good, dot: "good" };
  if (s.login_window_open) return { key: "busy", label: "Login window open", pill: TONES.amber, dot: "amb" };
  if (s.has_record) return { key: "exp", label: "Expired", pill: TONES.bad, dot: "bad" };
  return { key: "none", label: "Not connected", pill: TONES.neutral, dot: "off" };
}

export function quotaText(usage) {
  const u = usage?.usage || {};
  if (u.searches == null) return "Resdex today: —";
  return `Resdex today: ${u.searches}/${u.search_cap || "∞"} searches`;
}

/** The "Naukri recruiter: Connected" + "Resdex today" pills with their sub-line. */
export function SessionQuotaPills({ usage }) {
  const st = sessionState(usage);
  const checked = usage?.session?.checked_at;
  return (
    <div className="dm-pillstack">
      <div className="dm-pillrow">
        <span className="ha-pill dm-pill" style={st.pill} title={SESSION_TIP}>
          <span className={"dm-dot-s " + st.dot} /> Naukri recruiter: {st.label}
        </span>
        <span className="ha-pill dm-pill" style={TONES.neutral} title={QUOTA_TIP}>
          <Gauge size={12} /> {quotaText(usage)}
        </span>
      </div>
      <div className="dm-pill-sub">
        {checked ? `Last checked ${fmtAgo(checked)}. ` : "No recruiter check on record yet. "}
        Checked again when the batch starts. The quota is shared with harvest outreach.
      </div>
    </div>
  );
}

// ── Modal + switch ────────────────────────────────────────────────────────────

/** "A harvest is running" — shown when Start (or Re-run) meets the single-flight
 * guard. Nothing is started or queued. Escape / backdrop / OK close it. */
export function HarvestRunningModal({ info, onClose }) {
  const okRef = useRef(null);
  useEffect(() => {
    const prev = document.activeElement;
    okRef.current?.focus();
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
      if (e.key === "Tab") { e.preventDefault(); okRef.current?.focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); prev?.focus?.(); };
  }, [onClose]);
  const h = info || {};
  const source = h.source === "Demand" ? "Another Demand Search batch" : `${h.source || "A"} harvest`;
  const started = h.started_at ? ` started ${new Date(h.started_at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}` : "";
  const progress = typeof h.progress === "number" && h.progress > 0 ? `, about ${h.progress}% done` : "";
  return (
    <div className="dm-modal-wrap">
      <div className="dm-modal-bd" onClick={onClose} />
      <div className="dm-modal" role="dialog" aria-modal="true" aria-labelledby="dm-modal-h" aria-describedby="dm-modal-p">
        <div className="dm-modal-h">
          <span className="dm-modal-ico"><AlertTriangle size={17} /></span>
          <h3 id="dm-modal-h">A harvest is running</h3>
          <button type="button" className="dm-x" aria-label="Close" onClick={onClose}><X size={16} /></button>
        </div>
        <p id="dm-modal-p">
          {source}{started}{progress}. Demand Search uses the same recruiter browser and Resdex quota, so it
          can't run at the same time. Start it again after the {h.source === "Demand" ? "batch" : "harvest"} finishes.
        </p>
        <div className="dm-modal-acts">
          <button type="button" ref={okRef} className="ha-btn ha-btn-primary" onClick={onClose}>OK</button>
        </div>
      </div>
    </div>
  );
}

export function Switch({ checked, onChange, children }) {
  return (
    <button type="button" role="switch" aria-checked={checked} className="dm-switch" onClick={() => onChange(!checked)}>
      <span className="dm-sw-track" aria-hidden="true"><span className="dm-sw-thumb" /></span>
      <span>{children}</span>
    </button>
  );
}

export function Note({ tone = "info", icon: Icon, children }) {
  return (
    <div className={`dm-note dm-note-${tone}`}>
      {Icon && <Icon size={16} />}
      <div>{children}</div>
    </div>
  );
}

export const HumanIcon = Hand;

/* Page CSS — dm-* only; cards, buttons, tables, inputs and pills come from the
 * app's ThemeStyles (ha-*). */
export function DemandStyles() {
  return <style>{styles}</style>;
}

const styles = `
  .dm-pill{display:inline-flex;align-items:center;gap:5px;white-space:nowrap;}
  .dm-pillstack{display:flex;flex-direction:column;align-items:flex-end;gap:4px;max-width:520px;}
  .dm-pillrow{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end;}
  .dm-pill-sub{font-size:11.5px;color:${C.textSoft};text-align:right;}
  .dm-dot-s{width:8px;height:8px;border-radius:50%;display:inline-block;background:#94A3B8;}
  .dm-dot-s.good{background:#059669;}.dm-dot-s.bad{background:#DC2626;}.dm-dot-s.amb{background:#D97706;}
  .dm-card{padding:16px 18px;}
  .dm-sec-title{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:12px;}
  .dm-sec-title h3{margin:0;font-size:14px;font-weight:700;color:${C.text};display:inline-flex;align-items:center;gap:8px;}
  .dm-sec-title h3 svg{color:${C.primary};}
  .dm-muted{color:#94A3B8;}
  .dm-soft{color:${C.textSoft};}
  .dm-num{font-variant-numeric:tabular-nums;}

  .dm-stepper{display:flex;align-items:center;flex-wrap:wrap;gap:6px 0;margin:2px 0 16px;}
  .dm-step-wrap{display:flex;align-items:center;}
  .dm-step{display:inline-flex;align-items:center;gap:8px;font-size:12.5px;color:${C.textSoft};font-weight:600;}
  .dm-dot{width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-size:12px;font-weight:700;background:#F1F5F9;color:#64748B;border:1px solid #E2E8F0;}
  .dm-step.active{color:${C.primary};}.dm-step.active .dm-dot{background:${C.primary};color:#fff;border-color:${C.primary};}
  .dm-step.done{color:#047857;}.dm-step.done .dm-dot{background:#ECFDF5;color:#047857;border-color:#A7F3D0;}
  .dm-step-line{width:34px;height:2px;background:#E2E8F0;margin:0 10px;border-radius:2px;}
  .dm-step-line.done{background:#A7F3D0;}

  .dm-up-grid{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:16px;}
  .dm-drop{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:6px;text-align:center;min-height:190px;
    border:2px dashed #DBE7FF;background:${C.pale};border-radius:12px;padding:24px 20px;cursor:pointer;transition:background .15s,border-color .15s;font:inherit;color:${C.text};}
  .dm-drop:hover,.dm-drop.is-drag{border-color:${C.primary};background:#DBE7FF;}
  .dm-drop:focus-visible{outline:2px solid ${C.primary};outline-offset:2px;}
  .dm-drop h4{margin:4px 0 0;font-size:15px;}
  .dm-drop p{margin:0;font-size:13px;color:${C.textSoft};}
  .dm-drop .u{color:${C.primary};font-weight:600;text-decoration:underline;}
  .dm-drop-ic{width:46px;height:46px;border-radius:12px;display:grid;place-items:center;background:#fff;color:${C.primary};border:1px solid #DBE7FF;}
  .dm-req{display:flex;flex-direction:column;gap:12px;font-size:13px;}
  .dm-req-row{display:grid;grid-template-columns:72px minmax(0,1fr);gap:10px;align-items:start;}
  .dm-req-row>span{font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:${C.textSoft};padding-top:4px;}
  .dm-chips{display:flex;flex-wrap:wrap;gap:6px;}
  .dm-colchip{font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid #E2E8F0;background:#fff;color:#334155;}
  .dm-colchip.req{border-color:#BFDBFE;background:#EFF6FF;color:#1E40AF;font-weight:600;}
  .dm-colchip .syn{font-weight:400;color:${C.textSoft};}
  .dm-limits{display:flex;flex-wrap:wrap;gap:6px 14px;color:#334155;}

  .dm-note{display:flex;gap:10px;align-items:flex-start;font-size:13px;border-radius:10px;padding:10px 12px;border:1px solid;}
  .dm-note svg{flex-shrink:0;margin-top:1px;}
  .dm-note-info{background:#EFF6FF;border-color:#DBE7FF;color:#1E3A8A;}
  .dm-note-warn{background:#FFFBEB;border-color:#FDE68A;color:#92400E;}
  .dm-note-bad{background:#FEF2F2;border-color:#FECACA;color:#991B1B;}

  .dm-tbl-wrap{border:1px solid #EEF2F7;border-radius:10px;overflow:hidden;}
  .dm-tbl-scroll{overflow-x:auto;}
  .dm-tbl-scroll .ha-table{min-width:860px;}
  .dm-sub{font-size:11.5px;color:#94A3B8;margin-top:2px;}
  .dm-ttl-off{display:flex;flex-direction:column;color:#334155;font-weight:600;}
  .dm-ttl-off small{font-weight:400;color:#94A3B8;font-size:11.5px;}
  .dm-empty{text-align:center;padding:36px 16px;color:#94A3B8;}

  /* Validation */
  .dm-summary{display:flex;flex-direction:column;gap:16px;}
  .dm-filechip{display:flex;align-items:center;gap:12px;border:1px solid #E2E8F0;background:rgba(255,255,255,.8);border-radius:10px;padding:10px 12px;}
  .dm-fic{width:38px;height:38px;border-radius:9px;display:grid;place-items:center;background:#ECFDF5;color:#047857;flex-shrink:0;}
  .dm-fic.bad{background:#FEF2F2;color:#B91C1C;}
  .dm-fname{font-weight:700;font-size:14px;word-break:break-all;}
  .dm-fmeta{font-size:12px;color:${C.textSoft};}
  .dm-filechip .ha-btn{margin-left:auto;}
  .dm-valrow{display:flex;flex-wrap:wrap;gap:8px;}
  .dm-valchip{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:600;padding:4px 11px;border-radius:999px;border:1px solid;}
  .dm-valchip.total{background:#F1F5F9;border-color:#E2E8F0;color:#334155;}
  .dm-valchip.ready{background:#ECFDF5;border-color:#A7F3D0;color:#047857;}
  .dm-valchip.miss{background:#FEF2F2;border-color:#FECACA;color:#B91C1C;}
  .dm-valchip.reuse{background:#EFF6FF;border-color:#BFDBFE;color:#1D4ED8;}
  .dm-blk-lbl{font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:${C.textSoft};margin-bottom:8px;}
  .dm-blk-lbl .lc{text-transform:none;letter-spacing:0;font-weight:500;margin-left:4px;}
  .dm-maprow{display:flex;flex-wrap:wrap;gap:6px;}
  .dm-mapchip{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;padding:4px 10px;border-radius:8px;border:1px solid #E2E8F0;background:#fff;color:#334155;}
  .dm-mapchip.req{border-color:#A7F3D0;background:#ECFDF5;color:#047857;font-weight:600;}
  .dm-mapchip.ok{border-color:#BFDBFE;background:#EFF6FF;color:#1E40AF;}
  .dm-mapchip.off{color:#94A3B8;border-style:dashed;}
  .dm-mapchip .from{font-weight:400;color:${C.textSoft};}
  .dm-issues{display:flex;flex-direction:column;gap:6px;}
  .dm-issue{display:grid;grid-template-columns:70px minmax(0,1fr);gap:10px;font-size:13px;padding:8px 10px;border-radius:8px;background:#FEF2F2;border:1px solid #FECACA;}
  .dm-issue .rn{font-weight:700;color:#B91C1C;}
  .dm-issue .fix{display:block;color:${C.textSoft};font-size:12px;margin-top:2px;}
  .dm-mini{width:100%;border-collapse:collapse;font-size:12.5px;table-layout:fixed;min-width:900px;}
  .dm-mini th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:${C.textSoft};padding:8px 10px;background:linear-gradient(180deg,#F8FAFF,#EEF3FB);}
  .dm-mini td{padding:8px 10px;border-top:1px solid #EEF2F7;vertical-align:top;}
  .dm-mini tr.is-bad td{background:#FFF7F7;}
  .dm-clip{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .dm-chip{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;font-weight:600;padding:2px 9px;border-radius:999px;border:1px solid;white-space:nowrap;}
  .dm-chip .d{width:6px;height:6px;border-radius:50%;background:currentColor;}
  .dm-chip.done{background:#ECFDF5;border-color:#A7F3D0;color:#047857;}
  .dm-chip.reuse{background:#EFF6FF;border-color:#BFDBFE;color:#1D4ED8;}
  .dm-chip.fail{background:#FEF2F2;border-color:#FECACA;color:#B91C1C;}
  .dm-chip.nf{background:#F1F5F9;border-color:#E2E8F0;color:#475569;}
  .dm-chip.cap{background:#FFF7EC;border-color:#FCE3BC;color:#92580B;}
  .dm-chip.stop{background:#FFF7ED;border-color:#FED7AA;color:#9A3412;}
  .dm-chip.query{background:#F0F9FF;border-color:#BAE6FD;color:#0369A1;}
  .dm-chip.queue{background:#fff;border-color:#E2E8F0;color:#64748B;}
  .dm-chip.proc{background:#FFFBEB;border-color:#FDE68A;color:#B45309;}
  .dm-foot{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end;padding-top:6px;}
  .dm-foot .left{margin-right:auto;font-size:12.5px;color:${C.textSoft};}
  .dm-state{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 14px;align-items:start;padding:16px;border-radius:12px;border:1px solid #E2E8F0;background:rgba(255,255,255,.72);}
  .dm-state-ico{grid-row:span 4;width:38px;height:38px;border-radius:10px;display:grid;place-items:center;background:#fff;color:#64748B;border:1px solid #E2E8F0;}
  .dm-state h4{margin:0;font-size:14.5px;font-weight:700;color:${C.text};}
  .dm-state p{margin:0;font-size:13px;color:#334155;max-width:76ch;}
  .dm-state.bad{border-color:#FECACA;background:#FEF2F2;}.dm-state.bad .dm-state-ico{color:#B91C1C;border-color:#FECACA;}
  .dm-state.warn{border-color:#FCE3BC;background:#FFF7EC;}.dm-state.warn .dm-state-ico{color:#92580B;border-color:#FCE3BC;}
  .dm-state-acts{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:8px;font-size:12px;color:${C.textSoft};}

  /* Pre-flight */
  .dm-pf-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;}
  .dm-pf{border:1px solid #E2E8F0;border-radius:12px;padding:14px;background:rgba(255,255,255,.8);display:flex;flex-direction:column;gap:8px;font-size:13px;}
  .dm-pf.bad{border-color:#FECACA;background:#FEF2F2;}
  .dm-pf p{margin:0;color:#334155;}
  .dm-pf-h{display:flex;align-items:center;gap:6px;font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:${C.textSoft};}
  .dm-pf-big{font-size:18px;font-weight:800;color:${C.text};display:flex;align-items:center;gap:8px;}
  .dm-pf-big.good{color:#047857;}.dm-pf-big.bad{color:#B91C1C;}.dm-pf-big.off{color:#475569;}
  .dm-pf-why{font-size:11.5px;color:#94A3B8;}
  .dm-meter-top{display:flex;justify-content:space-between;font-size:12.5px;color:#334155;}
  .dm-track{height:7px;border-radius:99px;background:#E2E8F0;overflow:hidden;margin-top:4px;}
  .dm-track i{display:block;height:100%;background:linear-gradient(90deg,#2563EB,#7C3AED);}
  .dm-track i.amber{background:#F59E0B;}
  .dm-field{display:flex;flex-direction:column;gap:4px;max-width:460px;}
  .dm-field label{font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:${C.textSoft};}

  /* Batch page */
  .dm-back{display:inline-flex;align-items:center;gap:7px;cursor:pointer;font:inherit;font-size:13px;font-weight:600;padding:8px 14px;border-radius:8px;
    background:linear-gradient(160deg,rgba(255,255,255,.9),rgba(255,255,255,.65));border:1px solid rgba(148,163,184,.45);color:#334155;}
  .dm-back:hover{background:rgba(241,245,249,.92);}
  .dm-runid{font-size:20px;font-weight:800;letter-spacing:-.01em;color:${C.text};word-break:break-all;}
  .dm-klbl{font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:${C.textSoft};}
  .dm-meta-row{display:flex;gap:24px;flex-wrap:wrap;margin-top:14px;}
  .dm-meta-row .v{font-size:13.5px;color:${C.text};margin-top:2px;}
  .dm-runstats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;width:100%;}
  .dm-switch-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:14px 0 10px;}
  .dm-switch{display:inline-flex;align-items:center;gap:10px;border:0;background:none;cursor:pointer;font:inherit;font-size:13.5px;font-weight:600;color:${C.text};padding:4px 0;}
  .dm-switch:focus-visible{outline:2px solid ${C.primary};outline-offset:3px;border-radius:6px;}
  .dm-sw-track{width:38px;height:22px;border-radius:99px;background:#CBD5E1;position:relative;transition:background .15s;flex-shrink:0;}
  .dm-sw-thumb{position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(15,23,42,.3);transition:left .15s;}
  .dm-switch[aria-checked="true"] .dm-sw-track{background:${C.primary};}
  .dm-switch[aria-checked="true"] .dm-sw-thumb{left:19px;}
  .dm-split{display:grid;grid-template-columns:360px minmax(0,1fr);gap:12px;align-items:start;}
  @media(max-width:1000px){.dm-split{grid-template-columns:1fr;}}
  .dm-proc{padding:14px;display:flex;flex-direction:column;gap:12px;font-size:13px;min-width:0;}
  .dm-proc-top{display:flex;align-items:center;justify-content:space-between;gap:8px;}
  .dm-live{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px;background:#FFFBEB;color:#B45309;border:1px solid #FDE68A;}
  .dm-live .d{width:7px;height:7px;border-radius:50%;background:currentColor;animation:dm-pulse 1.4s ease-in-out infinite;}
  .dm-live.ended{background:#F1F5F9;color:#475569;border-color:#E2E8F0;}.dm-live.ended .d{animation:none;}
  .dm-live.paused{background:#FEF2F2;color:#B91C1C;border-color:#FECACA;}
  @keyframes dm-pulse{50%{opacity:.35;}}
  .dm-cur{display:flex;gap:8px;align-items:flex-start;color:#334155;}
  .dm-cur svg{flex-shrink:0;margin-top:2px;color:${C.primary};}
  .dm-substeps{display:flex;flex-wrap:wrap;gap:4px;align-items:center;font-size:11.5px;}
  .dm-sst{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:999px;border:1px solid #E2E8F0;color:#94A3B8;background:#fff;}
  .dm-sst.ok{color:#047857;border-color:#A7F3D0;background:#ECFDF5;}
  .dm-sst.now{color:#B45309;border-color:#FDE68A;background:#FFFBEB;}
  .dm-bigbar{height:9px;border-radius:99px;background:#E2E8F0;overflow:hidden;}
  .dm-bigbar i{display:block;height:100%;background:linear-gradient(90deg,#2563EB,#7C3AED);transition:width .3s;}
  .dm-bigbar-foot{display:flex;justify-content:space-between;font-size:12px;color:${C.textSoft};margin-top:6px;}
  .dm-ccount{display:grid;grid-template-columns:1fr 1fr;gap:6px;}
  .dm-ct{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:#334155;}
  .dm-ct .d{width:8px;height:8px;border-radius:50%;background:#94A3B8;}
  .dm-ct.query .d{background:#0284C7;}
  .dm-ct.done .d{background:#059669;}.dm-ct.reuse .d{background:#2563EB;}.dm-ct.cap .d{background:#D97706;}.dm-ct.fail .d{background:#DC2626;}
  .dm-quota{border:1px solid #E2E8F0;border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;gap:5px;background:rgba(255,255,255,.7);}
  .dm-rowlist{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px;max-height:320px;overflow-y:auto;}
  .dm-rowlist li{display:grid;grid-template-columns:22px minmax(0,1fr) auto;gap:8px;align-items:center;font-size:12.5px;}
  .dm-rowlist .n{color:#94A3B8;font-variant-numeric:tabular-nums;}
  .dm-rowlist .tt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .dm-legend summary{cursor:pointer;font-size:12.5px;font-weight:600;color:${C.primary};}
  .dm-legend-body{display:flex;flex-direction:column;gap:6px;margin-top:8px;font-size:12px;color:${C.textSoft};}
  .dm-legend-body div{display:flex;flex-wrap:wrap;gap:6px;align-items:center;}
  .dm-confirm{border:1px solid #FECACA;background:#FEF2F2;border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;gap:8px;font-size:12.5px;}
  .dm-confirm.pale{border-color:#DBE7FF;background:#EFF6FF;}
  .dm-confirm p{margin:0;color:#334155;}
  .dm-confirm .acts{display:flex;gap:8px;flex-wrap:wrap;}
  .ha-btn-sm{padding:6px 12px;font-size:13px;}
  .ha-btn-danger{border:0;font-weight:600;background:#DC2626;color:#fff;}
  .ha-btn-danger:hover{background:#B91C1C;}
  .ha-btn:disabled{opacity:.55;cursor:not-allowed;}
  .dm-tbl-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:10px;}
  .dm-tbl-head .t{font-size:14px;font-weight:700;color:${C.text};}
  .dm-tbl-head .stats{display:flex;gap:12px;font-size:12.5px;color:${C.textSoft};}
  .dm-pager{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;font-size:12px;color:${C.textSoft};padding:10px 4px 0;}
  .dm-cov{display:flex;align-items:center;gap:8px;}
  .dm-cov .dm-track{width:70px;margin:0;}
  .dm-cov b{font-size:12.5px;}

  /* Modal */
  .dm-modal-wrap{position:fixed;inset:0;z-index:200;display:grid;place-items:center;padding:16px;}
  .dm-modal-bd{position:absolute;inset:0;background:rgba(15,23,42,.45);}
  .dm-modal{position:relative;${GLASS_MODAL}border-radius:14px;padding:18px 20px;max-width:460px;width:100%;}
  .dm-modal-h{display:flex;align-items:center;gap:10px;}
  .dm-modal-h h3{margin:0;font-size:16px;flex:1;}
  .dm-modal-ico{width:32px;height:32px;border-radius:9px;display:grid;place-items:center;background:#FEF3C7;color:#B45309;}
  .dm-modal p{font-size:13.5px;color:#334155;line-height:1.55;margin:12px 0 16px;}
  .dm-modal-acts{display:flex;justify-content:flex-end;}
  .dm-x{border:0;background:none;cursor:pointer;color:#64748B;padding:4px;border-radius:6px;display:grid;place-items:center;}
  .dm-x:hover{background:#F1F5F9;}

  @media(max-width:900px){.dm-up-grid,.dm-pf-grid{grid-template-columns:1fr;}.dm-pillstack{align-items:flex-start;}.dm-pillrow{justify-content:flex-start;}.dm-pill-sub{text-align:left;}}
  @media(max-width:520px){.dm-runstats{grid-template-columns:repeat(2,minmax(0,1fr));}.dm-step-line{width:14px;margin:0 6px;}.dm-step-t{display:none;}}
  @media (prefers-reduced-motion: reduce){.dm-live .d{animation:none;}.dm-sw-track,.dm-sw-thumb,.dm-bigbar i{transition:none;}}
`;
