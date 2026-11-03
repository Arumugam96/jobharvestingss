import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Bell, Reply, X, Check, PhoneCall } from "lucide-react";
import {
  getOutreachNotifications, markReplyRead, markAllRepliesRead,
  getCallFollowUps, markFollowUpDone, markAllFollowUpsDone,
} from "../api";
import { fmtRel, avatarColor, initials } from "./outreachUi";

/* In-app notifications — inbound prospect replies + due follow-up calls.
 *
 * Mounted once in the shared AppLayout, so it rides along on every sidebar-chrome
 * page. Polls every POLL_MS (the product decision was polling, not SSE):
 *   • GET /outreach/notifications — UNREAD replies + a total count (only when the
 *     tenant has the outreach feature: `repliesEnabled`);
 *   • GET /phone-verifications/follow-ups — the current user's follow-up calls that
 *     are due (an Unanswered call logged with Follow-up on, 24h later).
 * Both are merged into one list (newest first) and rendered as:
 *   • a floating bell with a badge (unread replies + due follow-ups); clicking opens
 *     a dropdown. A reply row deep-links to its thread (/mail/:outreach_id) and marks
 *     itself read; a follow-up row marks itself done and opens the job. "Mark all
 *     read" clears both.
 *   • a toast for items that arrive AFTER the first poll (so a page load doesn't
 *     spam toasts for the existing backlog).
 *
 * Positioned bottom-right as a self-contained affordance: the app has no global top
 * bar, and a fixed top-right bell would collide with each page's own header actions
 * (Mail logs' Export/Refresh). The toast stacks just above the bell.
 *
 * Indigo (#4F46E5) for replies — the same Replied accent used across Mail logs and
 * the thread page; amber for follow-up calls so the two read apart at a glance. */

const POLL_MS = 30_000;
const REPLY = "#4F46E5";
const REPLY_SOFT = "#EEF0FF";
const FU = "#B45309";
const FU_SOFT = "#FFF7ED";
const TOAST_MS = 8_000;

// Build the thread deep-link for a notification (same resolution the Mail-logs row
// uses: the thread page reads ?job/?recruiter on a fresh tab). Falls back to the
// Mail logs list when the reply couldn't be matched to a send.
function threadHref(n) {
  if (!n.outreach_id) return "/outreach";
  const q = n.job_id ? `?job=${encodeURIComponent(n.job_id)}`
    : n.recruiter_id ? `?recruiter=${encodeURIComponent(n.recruiter_id)}` : "";
  return `/mail/${encodeURIComponent(n.outreach_id)}${q}`;
}

// One list shape for both sources. `key` is namespaced because reply ids are
// uuids and follow-up ids are ints — they share the seen-set and toast ids.
const asReply = (n) => ({ ...n, type: "reply", key: `reply:${n.id}`, sortAt: n.received_at });
const asFollowUp = (f) => ({ ...f, type: "call_followup", key: `fu:${f.id}`, sortAt: f.follow_up_due_at });
const ts = (iso) => { const t = new Date(iso || 0).getTime(); return Number.isNaN(t) ? 0 : t; };

export default function NotificationBell({ repliesEnabled = true }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [replies, setReplies] = useState([]);
  const [repliesUnread, setRepliesUnread] = useState(0);
  const [followUps, setFollowUps] = useState([]);
  const [followUpsDue, setFollowUpsDue] = useState(0);
  const [open, setOpen] = useState(false);
  const [toasts, setToasts] = useState([]); // [{...normalized item}]

  const seenRef = useRef(new Set());    // namespaced keys already seen
  const seededRef = useRef({ reply: false, fu: false }); // first successful poll per source
  const panelRef = useRef(null);
  const toastTimers = useRef({});

  // Toast only items that are genuinely NEW since we started watching — never on
  // a source's first successful poll (that backlog is for the bell, not toasts).
  const noteFresh = useCallback((source, list) => {
    if (!seededRef.current[source]) {
      seededRef.current[source] = true;
      list.forEach((n) => seenRef.current.add(n.key));
      return [];
    }
    const fresh = list.filter((n) => !seenRef.current.has(n.key));
    fresh.forEach((n) => seenRef.current.add(n.key));
    return fresh;
  }, []);

  const poll = useCallback(async () => {
    const [rep, fu] = await Promise.allSettled([
      repliesEnabled ? getOutreachNotifications({ limit: 20 }) : Promise.resolve(null),
      getCallFollowUps({ limit: 20 }),
    ]);
    // A failed source keeps its last good state; try again next tick.
    let fresh = [];
    if (rep.status === "fulfilled" && rep.value) {
      const next = (rep.value.items || []).map(asReply);
      setReplies(next);
      setRepliesUnread(rep.value.unread || 0);
      fresh = fresh.concat(noteFresh("reply", next));
    }
    if (fu.status === "fulfilled" && fu.value) {
      const next = (fu.value.items || []).map(asFollowUp);
      setFollowUps(next);
      setFollowUpsDue(fu.value.due || 0);
      fresh = fresh.concat(noteFresh("fu", next));
    }
    if (fresh.length) {
      fresh.sort((x, y) => ts(y.sortAt) - ts(x.sortAt));
      setToasts((cur) => [...fresh, ...cur].slice(0, 3));
    }
  }, [repliesEnabled, noteFresh]);

  useEffect(() => {
    poll();
    const t = setInterval(poll, POLL_MS);
    return () => clearInterval(t);
  }, [poll]);

  // Outreach feature off for this tenant → drop any replies already shown.
  useEffect(() => {
    if (repliesEnabled) return;
    setReplies([]);
    setRepliesUnread(0);
  }, [repliesEnabled]);

  const items = useMemo(
    () => [...replies, ...followUps].sort((x, y) => ts(y.sortAt) - ts(x.sortAt)),
    [replies, followUps]
  );
  const unread = repliesUnread + followUpsDue;

  // Auto-dismiss each toast after TOAST_MS.
  useEffect(() => {
    toasts.forEach((t) => {
      if (toastTimers.current[t.key]) return;
      toastTimers.current[t.key] = setTimeout(() => {
        setToasts((cur) => cur.filter((x) => x.key !== t.key));
        delete toastTimers.current[t.key];
      }, TOAST_MS);
    });
  }, [toasts]);

  // Close the dropdown on an outside click.
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => { if (panelRef.current && !panelRef.current.contains(e.target)) setOpen(false); };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [open]);

  const dismissToast = (key) => {
    setToasts((cur) => cur.filter((x) => x.key !== key));
    clearTimeout(toastTimers.current[key]);
    delete toastTimers.current[key];
  };

  // Open an item and clear it (optimistically): a reply opens its thread and is
  // marked read; a follow-up is marked done and opens the job.
  const openItem = useCallback(async (n) => {
    setOpen(false);
    dismissToast(n.key);
    if (n.type === "call_followup") {
      setFollowUps((cur) => cur.filter((x) => x.key !== n.key));
      setFollowUpsDue((d) => Math.max(0, d - 1));
      try { await markFollowUpDone(n.id); } catch { /* best-effort */ }
      // `from` = the page the bell was opened on, so the job detail's Back returns there.
      navigate(`/jobs/${encodeURIComponent(n.job_id)}`, { state: { from: location.pathname + location.search } });
      return;
    }
    setReplies((cur) => cur.filter((x) => x.key !== n.key));
    setRepliesUnread((u) => Math.max(0, u - 1));
    try { await markReplyRead(n.id, true); } catch { /* best-effort */ }
    navigate(threadHref(n));
  }, [navigate, location.pathname, location.search]);

  const clearAll = async () => {
    setReplies([]);
    setRepliesUnread(0);
    setFollowUps([]);
    setFollowUpsDue(0);
    await Promise.allSettled([
      repliesEnabled ? markAllRepliesRead() : Promise.resolve(),
      markAllFollowUpsDone(),
    ]);
  };

  const label = unread > 0 ? `${unread} notification${unread === 1 ? "" : "s"}` : "Notifications";

  return (
    <>
      <style>{CSS}</style>

      {/* Toasts — newest on top, stacked just above the bell. */}
      <div className="rb-toasts">
        {toasts.map((t) => {
          const fu = t.type === "call_followup";
          const preview = fu ? t.remarks : t.preview;
          return (
            <div key={t.key} className={"rb-toast" + (fu ? " fu" : "")} role="status">
              <span className="rb-toast-accent" />
              <button className="rb-toast-x" onClick={() => dismissToast(t.key)} aria-label="Dismiss"><X size={14} /></button>
              <div className="rb-toast-row">
                <span className="rb-toast-icon">
                  {fu ? <PhoneCall size={16} strokeWidth={2.3} /> : <Reply size={17} strokeWidth={2.3} />}
                </span>
                <div style={{ minWidth: 0 }}>
                  <div className="rb-toast-lab">{fu ? "Follow-up call due" : "New reply"}</div>
                  <div className="rb-toast-name">
                    {fu
                      ? `${t.contact_name || t.phone || "Contact"}${t.company ? ` · ${t.company}` : ""}`
                      : `${t.from_name || t.from_email || "A prospect"}${t.company ? ` · ${t.company}` : ""}`}
                  </div>
                </div>
              </div>
              {preview && <div className="rb-toast-prev">{preview}</div>}
              <div className="rb-toast-acts">
                <button className="rb-btn rb-btn-primary" onClick={() => openItem(t)}>{fu ? "Open job" : "View reply"}</button>
                <button className="rb-btn rb-btn-ghost" onClick={() => dismissToast(t.key)}>Dismiss</button>
              </div>
            </div>
          );
        })}
      </div>

      {/* Bell + dropdown. */}
      <div className="rb-wrap" ref={panelRef}>
        {open && (
          <div className="rb-panel" role="dialog" aria-label="Notifications">
            <div className="rb-panel-head">
              <b>Notifications</b>
              {items.length > 0 && (
                <button className="rb-markall" onClick={clearAll}><Check size={13} /> Mark all read</button>
              )}
            </div>
            <div className="rb-list">
              {items.length === 0 ? (
                <div className="rb-empty">You're all caught up.</div>
              ) : items.map((n) => {
                if (n.type === "call_followup") {
                  const sub = [n.company, n.job_title].filter(Boolean).join(" · ");
                  return (
                    <button key={n.key} className="rb-item rb-item-fu" onClick={() => openItem(n)}
                      title="Mark done and open the job">
                      <span className="rb-av rb-av-fu">
                        <PhoneCall size={16} />
                        <span className="rb-dot" />
                      </span>
                      <span className="rb-item-txt">
                        <span className="rb-item-top"><b>Follow-up call due:</b> {n.contact_name || n.phone}</span>
                        {sub && <span className="rb-item-sub">{sub}</span>}
                        {n.remarks && <span className="rb-item-prev">{n.remarks}</span>}
                        <span className="rb-item-meta">{n.contact_name && n.phone ? `${n.phone} · ` : ""}Due {fmtRel(n.follow_up_due_at)}</span>
                      </span>
                    </button>
                  );
                }
                const name = n.from_name || n.from_email || "Reply";
                return (
                  <button key={n.key} className="rb-item" onClick={() => openItem(n)}>
                    <span className="rb-av" style={{ background: avatarColor(name) }}>
                      {initials(n.from_name, n.from_email)}
                      <span className="rb-dot" />
                    </span>
                    <span className="rb-item-txt">
                      <span className="rb-item-top"><b>{name}</b> replied</span>
                      {n.preview && <span className="rb-item-prev">{n.preview}</span>}
                      <span className="rb-item-meta">{n.company ? `${n.company} · ` : ""}{fmtRel(n.received_at)}</span>
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        )}
        <button
          className={"rb-bell" + (unread > 0 ? " has-unread" : "")}
          onClick={() => setOpen((v) => !v)}
          aria-label={label}
          title={label}
        >
          <Bell size={20} />
          {unread > 0 && <span className="rb-count">{unread > 99 ? "99+" : unread}</span>}
        </button>
      </div>
    </>
  );
}

const CSS = `
.rb-wrap{ position:fixed; right:24px; bottom:24px; z-index:1100; }
.rb-bell{ position:relative; width:52px; height:52px; border-radius:50%; display:grid; place-items:center;
  background:#fff; color:#334155; border:1px solid #E2E8F0; cursor:pointer;
  box-shadow:0 10px 26px -10px rgba(15,23,42,.35); transition:transform .15s ease, box-shadow .15s ease, color .15s ease; }
.rb-bell:hover{ transform:translateY(-2px); box-shadow:0 16px 32px -12px rgba(15,23,42,.42); color:#1E293B; }
.rb-bell:focus-visible{ outline:2px solid ${REPLY}; outline-offset:2px; }
.rb-bell.has-unread{ color:${REPLY}; border-color:rgba(79,70,229,.45); box-shadow:0 10px 26px -10px rgba(79,70,229,.5), 0 0 0 4px rgba(79,70,229,.10); }
.rb-count{ position:absolute; top:-3px; right:-3px; min-width:21px; height:21px; padding:0 6px; border-radius:999px;
  background:${REPLY}; color:#fff; font-size:11.5px; font-weight:800; display:grid; place-items:center;
  border:2px solid #fff; font-variant-numeric:tabular-nums; }

.rb-panel{ position:absolute; right:0; bottom:64px; width:340px; max-width:calc(100vw - 48px);
  background:#fff; border:1px solid #E2E8F0; border-radius:14px; overflow:hidden;
  box-shadow:0 26px 60px -18px rgba(15,23,42,.4); font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; }
.rb-panel-head{ display:flex; align-items:center; justify-content:space-between; padding:13px 15px; border-bottom:1px solid #EDF1F6; }
.rb-panel-head b{ font-size:14px; color:#0F172A; }
.rb-markall{ display:inline-flex; align-items:center; gap:5px; border:0; background:transparent; cursor:pointer;
  color:${REPLY}; font-size:12px; font-weight:700; font-family:inherit; padding:2px 4px; }
.rb-markall:hover{ text-decoration:underline; }
.rb-list{ max-height:min(60vh,420px); overflow-y:auto; }
.rb-empty{ padding:28px 16px; text-align:center; color:#94A3B8; font-size:13px; }
.rb-item{ display:flex; gap:11px; width:100%; text-align:left; padding:12px 15px; border:0; border-bottom:1px solid #EDF1F6;
  background:linear-gradient(90deg, rgba(79,70,229,.05), transparent 55%); cursor:pointer; font-family:inherit; }
.rb-item:last-child{ border-bottom:0; }
.rb-item:hover{ background:#F6F7FE; }
.rb-av{ position:relative; width:36px; height:36px; border-radius:50%; flex:none; display:grid; place-items:center;
  color:#fff; font-weight:700; font-size:12px; }
.rb-dot{ position:absolute; top:-1px; right:-1px; width:11px; height:11px; border-radius:50%; background:${REPLY}; border:2px solid #fff; }
.rb-item-txt{ min-width:0; display:flex; flex-direction:column; gap:2px; }
.rb-item-top{ font-size:13px; color:#0F172A; }
.rb-item-prev{ font-size:12px; color:#64748B; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:240px; }
.rb-item-meta{ font-size:11px; color:#94A3B8; margin-top:2px; }
.rb-item-sub{ font-size:12px; color:#475569; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:240px; }
.rb-item-fu{ background:linear-gradient(90deg, rgba(180,83,9,.06), transparent 55%); }
.rb-item-fu:hover{ background:#FFFBF5; }
.rb-av-fu{ background:${FU_SOFT}; color:${FU}; border:1px solid #FED7AA; }
.rb-av-fu .rb-dot{ background:${FU}; }

.rb-toasts{ position:fixed; right:24px; bottom:88px; z-index:1100; display:flex; flex-direction:column; gap:12px;
  width:320px; max-width:calc(100vw - 48px); }
.rb-toast{ position:relative; background:#fff; border:1px solid rgba(79,70,229,.28); border-radius:13px; overflow:hidden;
  padding:14px 15px 14px 18px; box-shadow:0 22px 50px -16px rgba(15,23,42,.5), 0 0 0 4px rgba(79,70,229,.08);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; animation:rb-in .22s ease-out; }
@keyframes rb-in{ from{ opacity:0; transform:translateY(8px); } to{ opacity:1; transform:none; } }
.rb-toast-accent{ position:absolute; left:0; top:0; bottom:0; width:4px; background:${REPLY}; }
.rb-toast-x{ position:absolute; top:9px; right:9px; border:0; background:transparent; color:#94A3B8; cursor:pointer; padding:2px; line-height:0; }
.rb-toast-x:hover{ color:#475569; }
.rb-toast-row{ display:flex; align-items:center; gap:10px; }
.rb-toast-icon{ width:34px; height:34px; border-radius:10px; flex:none; display:grid; place-items:center; background:${REPLY_SOFT}; color:${REPLY}; }
.rb-toast-lab{ font-size:11px; font-weight:800; letter-spacing:.07em; text-transform:uppercase; color:${REPLY}; }
.rb-toast-name{ font-size:13.5px; font-weight:700; color:#0F172A; margin-top:1px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:230px; }
.rb-toast-prev{ font-size:12.5px; color:#64748B; line-height:1.45; margin-top:9px;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.rb-toast-acts{ display:flex; gap:8px; margin-top:12px; }
.rb-btn{ font-family:inherit; border-radius:9px; font-size:12.5px; font-weight:700; padding:7px 13px; cursor:pointer; border:1px solid transparent; }
.rb-btn-primary{ background:${REPLY}; color:#fff; }
.rb-btn-primary:hover{ background:#4338CA; }
.rb-btn-ghost{ background:#fff; color:#475569; border-color:#E2E8F0; }
.rb-btn-ghost:hover{ background:#F8FAFC; }

.rb-toast.fu{ border-color:rgba(180,83,9,.3); box-shadow:0 22px 50px -16px rgba(15,23,42,.5), 0 0 0 4px rgba(180,83,9,.08); }
.rb-toast.fu .rb-toast-accent{ background:${FU}; }
.rb-toast.fu .rb-toast-icon{ background:${FU_SOFT}; color:${FU}; }
.rb-toast.fu .rb-toast-lab{ color:${FU}; }
.rb-toast.fu .rb-btn-primary{ background:${FU}; }
.rb-toast.fu .rb-btn-primary:hover{ background:#92400E; }

@media (prefers-reduced-motion: reduce){ .rb-bell{ transition:none; } .rb-toast{ animation:none; } }
`;
