import React, { useCallback, useEffect, useRef, useState } from "react";
import { KeyRound, MonitorPlay, X, Loader2, Hand } from "lucide-react";
import { getLoginRequired, openLoginRequired, dismissLoginRequired, ApiError } from "../api";
import { useTenant } from "../TenantContext";
import LiveBrowserView from "./LiveBrowserView";

/* App-wide "login needed" banner — the one surface for every login-based source
 * (Naukri recruiter / Resdex, LinkedIn accounts, …).
 *
 * Mounted once in HarvestAgent (outside the routes), so it shows on every page,
 * detail views included. Polls GET /login-required — a cheap read of one small
 * server-side file, never a browser — every POLL_MS while the tab is visible
 * (paused when hidden, refreshed on return). Sessions themselves are only checked
 * by the backend's existing flows; this component never triggers a check.
 *
 * Each request gets an "Open Live Browser to log in" button: POST
 * /login-required/open starts that source's existing login flow (Naukri recruiter
 * login window / LinkedIn setup session) or, when a run's own browser is already
 * on the login page, just opens the Live Browser view (mounted here, globally).
 * A request a run is waiting on shows a countdown; "human_needed" requests (a
 * CAPTCHA / challenge held open) read "needs you in the Live Browser".
 *
 * Top-centre so it never collides with the reply bell / toasts (bottom-right) or
 * the pages' own header actions (top-right). Amber = needs attention. */

const POLL_MS = 12_000;
const AMBER = "#B45309";
const AMBER_SOFT = "#FEF3C7";
const AMBER_LINE = "rgba(217,119,6,.35)";

const itemKey = (it) => `${it.source}|${it.account || ""}|${it.kind}`;

function fmtLeft(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function waitingWhat(it) {
  if (it.source === "linkedin") return "the harvest is";
  if (/resdex/i.test(it.reason || "")) return "Resdex search is";
  return "a run is";
}

export default function LoginRequiredBanner() {
  const { hasFeature } = useTenant();
  const enabled = hasFeature("rules");
  const [items, setItems] = useState([]);
  const [skew, setSkew] = useState(0);              // server now − client now (ms)
  const [tick, setTick] = useState(Date.now());
  const [busy, setBusy] = useState({});             // key → true while opening
  const [notes, setNotes] = useState({});           // key → message from /open
  const [live, setLive] = useState(null);           // {key, title} | null
  const timer = useRef(null);
  const alive = useRef(true);

  const poll = useCallback(async () => {
    try {
      const res = await getLoginRequired();
      if (!alive.current) return;
      setItems(Array.isArray(res.items) ? res.items : []);
      const serverNow = Date.parse(res.now || "");
      if (!Number.isNaN(serverNow)) setSkew(serverNow - Date.now());
    } catch {
      // transient — keep the last good state, try again next tick
    }
  }, []);

  // Poll only while the app is open AND the tab is visible.
  useEffect(() => {
    alive.current = true;
    if (!enabled) return undefined;
    const start = () => {
      clearInterval(timer.current);
      timer.current = null;
      if (document.hidden) return;
      poll();
      timer.current = setInterval(poll, POLL_MS);
    };
    start();
    document.addEventListener("visibilitychange", start);
    return () => {
      alive.current = false;
      clearInterval(timer.current);
      document.removeEventListener("visibilitychange", start);
    };
  }, [enabled, poll]);

  const now = tick + skew;
  const anyCountdown = items.some((it) => it.waiting_until && Date.parse(it.waiting_until) > now);

  // 1 s ticker, only while a countdown is on screen.
  useEffect(() => {
    if (!anyCountdown) return undefined;
    const t = setInterval(() => setTick(Date.now()), 1000);
    return () => clearInterval(t);
  }, [anyCountdown]);

  // The request the Live Browser was opened for is gone (logged in / solved) →
  // close the view.
  useEffect(() => {
    if (live && !items.some((it) => itemKey(it) === live.key)) setLive(null);
  }, [items, live]);

  const open = async (it) => {
    const key = itemKey(it);
    setBusy((b) => ({ ...b, [key]: true }));
    setNotes((n) => ({ ...n, [key]: "" }));
    try {
      const res = await openLoginRequired({ source: it.source, account: it.account, kind: it.kind });
      if (res && res.open_live_browser) {
        setLive({
          key,
          title: it.kind === "human_needed"
            ? `${it.label} — complete the step in the live browser`
            : `${it.label} login — live browser`,
        });
      }
      setNotes((n) => ({ ...n, [key]: (res && !res.open_live_browser && res.message) || "" }));
      setTimeout(poll, 1500);
    } catch (err) {
      setNotes((n) => ({
        ...n, [key]: err instanceof ApiError ? err.message : "Could not reach the harvest backend.",
      }));
    } finally {
      setBusy((b) => ({ ...b, [key]: false }));
    }
  };

  const dismiss = async (it) => {
    setItems((cur) => cur.filter((x) => itemKey(x) !== itemKey(it)));
    try { await dismissLoginRequired({ source: it.source, account: it.account, kind: it.kind }); }
    catch { /* best-effort — it reappears on the next poll if still pending */ }
  };

  if (!enabled || (!items.length && !live)) return null;

  return (
    <>
      <style>{CSS}</style>
      {live && <LiveBrowserView title={live.title} onClose={() => setLive(null)} />}
      {items.length > 0 && (
        <div className="lr-stack" role="region" aria-label="Logins needed">
          {items.map((it) => {
            const key = itemKey(it);
            const human = it.kind === "human_needed";
            const until = it.waiting_until ? Date.parse(it.waiting_until) : NaN;
            const left = !Number.isNaN(until) && until > now ? until - now : null;
            const title = human ? `${it.label} needs you in the Live Browser` : `${it.label} login needed`;
            return (
              <div key={key} className={"lr-card" + (left != null ? " is-waiting" : "")} role="alert">
                <span className="lr-accent" />
                <span className="lr-icon">{human ? <Hand size={17} /> : <KeyRound size={17} />}</span>
                <div className="lr-body">
                  <div className="lr-title">
                    {title}
                    {left != null && (
                      <span className="lr-wait"> — {waitingWhat(it)} waiting (<b>{fmtLeft(left)}</b> left)</span>
                    )}
                  </div>
                  {it.reason && <div className="lr-reason">{it.reason}</div>}
                  {notes[key] && <div className="lr-note">{notes[key]}</div>}
                </div>
                <button
                  type="button" className="lr-btn" onClick={() => open(it)} disabled={!!busy[key]}
                  title="Opens the server's browser here so you can log in (or solve the challenge)"
                >
                  {busy[key] ? <Loader2 size={14} className="lr-spin" /> : <MonitorPlay size={14} />}
                  {human ? "Open Live Browser" : "Open Live Browser to log in"}
                </button>
                <button type="button" className="lr-x" onClick={() => dismiss(it)} aria-label="Dismiss" title="Dismiss">
                  <X size={14} />
                </button>
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}

const CSS = `
.lr-stack{ position:fixed; top:14px; left:50%; transform:translateX(-50%); z-index:1050;
  display:flex; flex-direction:column; gap:8px; width:min(680px, calc(100vw - 32px));
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; pointer-events:none; }
.lr-card{ pointer-events:auto; position:relative; display:flex; align-items:center; gap:12px;
  background:#fff; border:1px solid ${AMBER_LINE}; border-radius:12px; overflow:hidden;
  padding:11px 12px 11px 18px; box-shadow:0 18px 44px -16px rgba(15,23,42,.45), 0 0 0 4px rgba(217,119,6,.08);
  animation:lr-in .22s ease-out; }
.lr-card.is-waiting{ box-shadow:0 18px 44px -16px rgba(15,23,42,.45), 0 0 0 4px rgba(217,119,6,.16); }
@keyframes lr-in{ from{ opacity:0; transform:translateY(-8px); } to{ opacity:1; transform:none; } }
.lr-accent{ position:absolute; left:0; top:0; bottom:0; width:4px; background:#D97706; }
.lr-icon{ width:34px; height:34px; border-radius:10px; flex:none; display:grid; place-items:center;
  background:${AMBER_SOFT}; color:${AMBER}; }
.lr-body{ flex:1; min-width:0; }
.lr-title{ font-size:13.5px; font-weight:700; color:#0F172A; line-height:1.35; }
.lr-wait{ font-weight:600; color:${AMBER}; }
.lr-wait b{ font-variant-numeric:tabular-nums; }
.lr-reason{ font-size:12px; color:#64748B; margin-top:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.lr-note{ font-size:12px; color:#B91C1C; margin-top:3px; }
.lr-btn{ flex:none; display:inline-flex; align-items:center; gap:6px; font-family:inherit; cursor:pointer;
  border-radius:9px; font-size:12.5px; font-weight:700; padding:8px 12px; border:1px solid transparent;
  background:#D97706; color:#fff; }
.lr-btn:hover{ background:${AMBER}; }
.lr-btn:disabled{ opacity:.7; cursor:default; }
.lr-x{ flex:none; border:0; background:transparent; color:#94A3B8; cursor:pointer; padding:4px; line-height:0; border-radius:6px; }
.lr-x:hover{ color:#475569; background:#F1F5F9; }
.lr-spin{ animation:lr-rot .9s linear infinite; }
@keyframes lr-rot{ to{ transform:rotate(360deg); } }
@media (max-width:640px){
  .lr-card{ flex-wrap:wrap; }
  .lr-btn{ margin-left:46px; }
  .lr-reason{ white-space:normal; }
}
@media (prefers-reduced-motion: reduce){ .lr-card{ animation:none; } .lr-spin{ animation:none; } }
`;
