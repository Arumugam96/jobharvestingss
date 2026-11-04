import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  X, Check, ShieldCheck, PhoneIncoming, PhoneMissed, PhoneCall, Bell, AlertTriangle, RotateCcw,
  CornerUpRight, Ban, Upload, ChevronRight, ChevronDown, Paperclip, RefreshCw, AlertCircle,
} from "lucide-react";
import { GLASS_MODAL } from "../theme";
import { getPhoneCallHistory } from "../api";

/* Manual call log for a Harvested Jobs mobile number. The shield button in front
 * of the Mobile cell opens PhoneVerifyModal, where the user logs one call attempt:
 * call status (Answered / Unanswered), the outcome, an outcome-specific panel,
 * comments, proof of the call and a reminder for the next call. Earlier attempts
 * are listed in the "Call history" sheet. PhoneVerifyBadge next to the number
 * shows the latest call; hovering it shows the outcome and comments.
 * Calls are saved server-side (POST /jobs/{id}/phone-verification) and shared
 * with the tenant team; the reminder lands in the caller's notification bell.
 * The table ignores a call if the row's number has since changed. Styling uses `pvm-`. */

const MAX_FILES = 3;
const MAX_INLINE_PROOF = 300 * 1024; // larger images keep only their name (server caps inline proof)

export const PHONE_STATUSES = {
  answered: { label: "Answered", color: "#047857", bg: "#F0FDF4", border: "#86EFAC", Icon: PhoneIncoming },
  unanswered: { label: "Unanswered", color: "#DC2626", bg: "#FEF2F2", border: "#FCA5A5", Icon: PhoneMissed },
};

/* Outcome taxonomy — single source of truth.
 * panel: contextual fields; rem: reminder on by default (when = suggested time);
 * proof: 'required' | 'optional' | 'from2' (required from the 2nd attempt). */
const OUTCOMES = {
  answered: [
    { v: "good_response", l: "Good response — interested", tone: "good", panel: "next", rem: true, proof: "optional", when: [2, "10:00"] },
    { v: "connect_back", l: "Need to connect back", tone: "info", panel: "callback", rem: true, proof: "optional", when: [1, "14:30"] },
    { v: "share_details", l: "Asked to share details over email", tone: "info", panel: "email", rem: true, proof: "optional", when: [2, "10:00"] },
    { v: "not_interested", l: "Not interested", tone: "bad", panel: "reason", rem: false, proof: "optional" },
    { v: "wrong_number", l: "Wrong number", tone: "bad", panel: "wrong", rem: false, proof: "required", short: "Proof the number is someone else’s" },
    { v: "not_right_poc", l: "Not the right POC — redirected", tone: "warn", panel: "redirect", rem: true, proof: "optional", when: [1, "11:00"] },
    { v: "position_closed", l: "Position already closed / filled", tone: "warn", panel: null, rem: false, proof: "optional" },
    { v: "language_barrier", l: "Language barrier", tone: "warn", panel: "lang", rem: true, proof: "optional", when: [1, "11:00"] },
    { v: "do_not_call", l: "Asked not to be contacted again", tone: "bad", panel: "dnc", rem: false, proof: "required", remLock: true, short: "Call log or recording of the request" },
  ],
  unanswered: [
    { v: "no_answer", l: "Not answering — rang out", tone: "warn", panel: "retry", rem: true, proof: "from2", preset: "In 2 hours" },
    { v: "busy", l: "Busy / call declined", tone: "warn", panel: "retry", rem: true, proof: "from2", preset: "In 30 min" },
    { v: "switched_off", l: "Switched off", tone: "warn", panel: "retry", rem: true, proof: "from2", preset: "Tomorrow 10 AM" },
    { v: "unreachable", l: "Out of coverage / not reachable", tone: "warn", panel: "retry", rem: true, proof: "from2", preset: "Tomorrow 10 AM" },
    { v: "invalid_number", l: "Invalid number / not in service", tone: "bad", panel: "invalid", rem: false, proof: "required", short: "“Not in service” message or call log" },
    { v: "disconnected", l: "Call disconnected mid-way", tone: "warn", panel: "retry", rem: true, proof: "from2", preset: "In 30 min" },
    { v: "voicemail", l: "Voicemail / IVR only", tone: "info", panel: "retry", rem: true, proof: "from2", preset: "Tomorrow 10 AM" },
  ],
};
const outcomeOf = (call) => (call?.status && call?.outcome ? OUTCOMES[call.status]?.find((o) => o.v === call.outcome) : null);

const REASONS = ["Not hiring contract roles right now", "Requirement already closed", "Rate / budget mismatch",
  "Works with an empanelled vendor only", "No vendor empanelment open", "Hiring handled by another team"];
const NEXT_STEPS = ["Share JD", "Schedule call", "Send profiles", "Rate card"];
const SEND_WHAT = ["Company profile", "Rate card", "Profiles", "Empanelment form"];
const WINDOWS = ["Morning (9–12)", "Afternoon (12–4)", "Evening (4–7)"];
const REVISIT = ["1 mo", "3 mo", "6 mo", "Never"];
const LANGUAGES = ["Hindi", "Tamil", "Telugu", "Kannada", "Malayalam", "Bengali"];
const PROOF_TYPES = ["Call log screenshot", "Dialler / CRM", "Truecaller", "WhatsApp", "Call recording", "Other"];

/* ---------- dates: <input type=date/time> use local "YYYY-MM-DD" / "HH:MM" ---------- */
const pad = (n) => String(n).padStart(2, "0");
const toParts = (d) => [`${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`, `${pad(d.getHours())}:${pad(d.getMinutes())}`];
const daysFromNow = (days, time) => {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return [toParts(d)[0], time];
};
const RETRY_PRESETS = {
  "In 30 min": () => toParts(new Date(Date.now() + 30 * 60e3)),
  "In 2 hours": () => toParts(new Date(Date.now() + 120 * 60e3)),
  "Tomorrow 10 AM": () => daysFromNow(1, "10:00"),
  "Next Monday": () => daysFromNow(((8 - new Date().getDay()) % 7) || 7, "10:00"),
};
const suggestedWhen = (c) => (c?.preset ? RETRY_PRESETS[c.preset]() : c?.when ? daysFromNow(...c.when) : daysFromNow(1, "10:00"));

const fmtTime = (iso) => {
  try { return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }); } catch { return ""; }
};
// Server call view → the call shape the badge, history and modal read.
export const callFromView = (v) => (v ? {
  id: v.id,
  phone: v.phone,
  status: v.status,
  outcome: v.outcome || "",
  details: v.details || {},
  proof: v.proof || [],
  remarks: v.remarks || "",
  reminderAt: v.follow_up_due_at || null,
  at: v.called_at,
  by: v.called_by?.name || "",
  attempts: v.attempts || 1,
} : null);

const styles = `
.pvm-btn { width: 24px; height: 24px; flex: none; display: inline-flex; align-items: center; justify-content: center; border-radius: 999px; border: 1px solid #CBD5E1; background: rgba(255,255,255,.85); color: #64748B; cursor: pointer; padding: 0; transition: background .12s, border-color .12s, color .12s; }
.pvm-btn:hover { background: #EFF4FF; border-color: #93C5FD; color: #2563EB; }
.pvm-btn.done { background: #ECFDF5; border-color: #86EFAC; color: #047857; }
.pvm-btn.done:hover { background: #D1FAE5; border-color: #34D399; color: #047857; }
.pvm-btn:focus-visible, .pvm-badge:focus-visible { outline: 2px solid #2563EB; outline-offset: 2px; }
.pvm-cell { display: flex; align-items: center; gap: 8px; min-width: 0; }
.pvm-cell > .pvm-val { min-width: 0; overflow: hidden; }
.pvm-badge { flex: none; display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; border-radius: 999px; border: 1px solid; padding: 0; cursor: pointer; }
.pvm-pop { position: fixed; z-index: 1000; width: 280px; ${GLASS_MODAL} border-radius: 10px; padding: 12px 14px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1E293B; white-space: normal; }
.pvm-pop-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.pvm-pop-status { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 600; }
.pvm-pop-outcome { font-size: 12.5px; color: #475569; margin-top: 3px; }
.pvm-pop-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: #94A3B8; margin-bottom: 4px; }
.pvm-pop-text { font-size: 13px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
.pvm-pop-empty { font-size: 13px; color: #94A3B8; font-style: italic; }
.pvm-pop-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.pvm-pop-tag { display: inline-flex; align-items: center; gap: 5px; font-size: 11.5px; font-weight: 600; color: #1D4ED8; background: #EFF4FF; border: 1px solid #DBE6FF; border-radius: 999px; padding: 2px 9px; }
.pvm-pop-tag.muted { color: #64748B; background: #F8FAFC; border-color: #E2E8F0; }
.pvm-pop-foot { display: flex; align-items: center; justify-content: space-between; margin-top: 10px; font-size: 11.5px; color: #94A3B8; }
.pvm-link { display: inline-flex; align-items: center; gap: 4px; border: none; background: none; color: #2563EB; font-size: 12px; font-weight: 600; cursor: pointer; padding: 0; font-family: inherit; }
.pvm-link:hover { text-decoration: underline; }
.pvm-close { width: 26px; height: 26px; flex: none; border: none; background: transparent; border-radius: 7px; color: #94A3B8; display: flex; align-items: center; justify-content: center; cursor: pointer; }
.pvm-close:hover { background: #F1F5F9; color: #64748B; }

.pvm-overlay { position: fixed; inset: 0; background: rgba(15,23,42,.45); -webkit-backdrop-filter: blur(4px); backdrop-filter: blur(4px); display: flex; align-items: center; justify-content: center; padding: 20px 16px; z-index: 1000; }
.pvm-card { width: 100%; max-width: 560px; max-height: calc(100vh - 40px); background: #fff; border-radius: 14px; box-shadow: 0 24px 60px rgba(15,23,42,.24); display: flex; flex-direction: column; position: relative; overflow: hidden; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Roboto, sans-serif; color: #1E293B; }
.pvm-head { display: flex; align-items: center; gap: 10px; padding: 13px 16px; border-bottom: 1px solid #EEF2F7; }
.pvm-head h2 { margin: 0; font-size: 15.5px; font-weight: 700; letter-spacing: -.01em; }
.pvm-head .pvm-org { font-size: 13px; color: #94A3B8; font-weight: 500; }
.pvm-head .pvm-close { margin-left: auto; }
.pvm-body { padding: 13px 16px 4px; overflow-y: auto; }
.pvm-foot { display: flex; align-items: center; gap: 9px; padding: 11px 16px; border-top: 1px solid #EEF2F7; background: #FCFDFE; }

.pvm-contact { display: flex; align-items: center; gap: 10px; padding-bottom: 12px; border-bottom: 1px solid #EEF2F7; margin-bottom: 13px; }
.pvm-num { font-size: 19px; font-weight: 700; letter-spacing: -.02em; line-height: 1.1; }
.pvm-nm { font-size: 12.5px; color: #64748B; margin-top: 1px; }

.pvm-lbl { font-size: 10px; font-weight: 700; letter-spacing: .085em; text-transform: uppercase; color: #94A3B8; margin: 0 0 6px; display: flex; align-items: center; gap: 7px; }
.pvm-req { color: #DC2626; }
.pvm-tag { font-size: 9px; font-weight: 800; letter-spacing: .05em; padding: 1.5px 5px; border-radius: 4px; border: 1px solid; }
.pvm-tag.need { background: #FEF2F2; border-color: #FECACA; color: #DC2626; }
.pvm-tag.opt { background: #F8FAFC; border-color: #E2E8F0; color: #94A3B8; }

.pvm-top { display: grid; grid-template-columns: 216px 1fr; gap: 12px; margin-bottom: 12px; }
.pvm-seg { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.pvm-seg button { display: flex; align-items: center; justify-content: center; gap: 5px; padding: 9px 4px; cursor: pointer; border: 1px solid #E2E8F0; border-radius: 9px; background: #fff; font-family: inherit; font-size: 12px; font-weight: 600; color: #64748B; transition: .13s; min-width: 0; white-space: nowrap; }
.pvm-seg button:hover { border-color: #CBD5E1; background: #FCFDFF; }
.pvm-dot { width: 12px; height: 12px; border-radius: 50%; border: 1.5px solid #CBD5E1; flex: none; transition: .13s; }
.pvm-seg button.on { border-color: #2563EB; background: #EFF6FF; color: #1E40AF; }
.pvm-seg button.on .pvm-dot { border-color: #2563EB; border-width: 4px; }
.pvm-seg button.neg.on { border-color: #F59E0B; background: #FFFBEB; color: #B45309; }
.pvm-seg button.neg.on .pvm-dot { border-color: #F59E0B; }

.pvm-sel { position: relative; }
.pvm-sel select { width: 100%; appearance: none; -webkit-appearance: none; padding: 9px 30px 9px 11px; font-size: 13px; font-family: inherit; color: #1E293B; border: 1px solid #E2E8F0; border-radius: 9px; background: #fff; cursor: pointer; }
.pvm-sel select:disabled { background: #F8FAFC; color: #94A3B8; cursor: not-allowed; }
.pvm-sel select.ph { color: #94A3B8; }
.pvm-chev { position: absolute; right: 10px; top: 50%; transform: translateY(-50%); pointer-events: none; color: #94A3B8; }
.pvm-card input[type=text], .pvm-card input[type=tel], .pvm-card input[type=email], .pvm-card input[type=date], .pvm-card input[type=time], .pvm-card textarea { width: 100%; padding: 8px 11px; font-size: 13px; font-family: inherit; color: #1E293B; border: 1px solid #E2E8F0; border-radius: 9px; background: #fff; box-sizing: border-box; }
.pvm-card select:focus, .pvm-card input:focus, .pvm-card textarea:focus { outline: none; border-color: #2563EB; box-shadow: 0 0 0 3px rgba(37,99,235,.12); }
.pvm-card input:disabled { background: #F8FAFC; color: #94A3B8; }
.pvm-card textarea { height: 62px; resize: none; line-height: 1.45; }
.pvm-card ::placeholder { color: #94A3B8; }
.pvm-flabel { font-size: 11px; font-weight: 600; color: #64748B; margin: 0 0 5px; }

.pvm-panel { padding: 11px 12px; border: 1px solid #EEF2F7; background: #FBFCFE; border-radius: 10px; margin-bottom: 12px; }
.pvm-note { display: flex; gap: 7px; font-size: 11.5px; line-height: 1.45; margin-bottom: 9px; }
.pvm-note:last-child { margin-bottom: 0; }
.pvm-note svg { flex: none; margin-top: 1px; }
.pvm-note.info { color: #1E40AF; } .pvm-note.warn { color: #B45309; } .pvm-note.bad { color: #DC2626; } .pvm-note.good { color: #059669; }
.pvm-prow { display: flex; gap: 8px; align-items: flex-end; }
.pvm-prow > * { flex: 1; min-width: 0; }
.pvm-prow > .grow2 { flex: 2; }
.pvm-chips { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; }
.pvm-chip { border: 1px solid #E2E8F0; background: #fff; color: #64748B; border-radius: 999px; padding: 4.5px 10px; font-size: 11.5px; font-weight: 600; cursor: pointer; font-family: inherit; transition: .12s; }
.pvm-chip:hover { border-color: #CBD5E1; background: #F8FAFC; }
.pvm-chip.on { background: #2563EB; border-color: #2563EB; color: #fff; }
.pvm-check { display: flex; align-items: center; gap: 8px; cursor: pointer; font-size: 12px; line-height: 1.4; background: none; border: 0; padding: 0; font-family: inherit; color: inherit; text-align: left; }
.pvm-box { width: 15px; height: 15px; border-radius: 4px; border: 1.5px solid #CBD5E1; background: #fff; flex: none; display: flex; align-items: center; justify-content: center; color: #fff; transition: .12s; }
.pvm-check.on .pvm-box { background: #2563EB; border-color: #2563EB; }
.pvm-check.on.danger .pvm-box { background: #DC2626; border-color: #DC2626; }

.pvm-mid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
.pvm-drop { width: 100%; border: 1.5px dashed #CBD5E1; border-radius: 10px; height: 62px; display: flex; align-items: center; justify-content: center; gap: 8px; cursor: pointer; background: #FCFDFE; transition: .13s; padding: 0 10px; text-align: left; font-family: inherit; color: inherit; }
.pvm-drop:hover, .pvm-drop.over { border-color: #2563EB; background: #EFF6FF; }
.pvm-drop.over { box-shadow: 0 0 0 3px rgba(37,99,235,.1); }
.pvm-drop.err { border-color: #FCA5A5; background: #FEF2F2; }
.pvm-drop > svg { color: #94A3B8; flex: none; }
.pvm-drop:hover > svg, .pvm-drop.over > svg { color: #2563EB; }
.pvm-drop-t { font-size: 11.5px; font-weight: 600; line-height: 1.35; }
.pvm-drop-t u { color: #2563EB; text-decoration: none; border-bottom: 1px solid currentColor; }
.pvm-drop-s { font-size: 10px; color: #94A3B8; margin-top: 1px; }
.pvm-files { display: flex; flex-direction: column; gap: 5px; min-height: 62px; max-height: 96px; overflow-y: auto; }
.pvm-fcard { display: flex; gap: 8px; align-items: center; border: 1px solid #E2E8F0; border-radius: 9px; padding: 5px 6px; background: #fff; }
.pvm-fthumb { width: 26px; height: 32px; object-fit: cover; border-radius: 5px; flex: none; background: #F1F5F9; display: flex; align-items: center; justify-content: center; color: #94A3B8; }
.pvm-fmeta { flex: 1; min-width: 0; }
.pvm-fname { font-size: 11.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pvm-fsel { margin-top: 2px; }
.pvm-fsel select { padding: 2px 20px 2px 5px; font-size: 10.5px; border-radius: 5px; border-color: #EEF2F7; }
.pvm-fsel .pvm-chev { right: 5px; }
.pvm-fx { border: 0; background: transparent; color: #94A3B8; cursor: pointer; padding: 3px; border-radius: 5px; display: flex; }
.pvm-fx:hover { background: #FEF2F2; color: #DC2626; }
.pvm-more { font-size: 10.5px; color: #2563EB; font-weight: 600; cursor: pointer; border: 0; background: none; font-family: inherit; padding: 2px; align-self: flex-start; }

.pvm-rem { display: flex; align-items: center; gap: 10px; border: 1px solid #E2E8F0; border-radius: 10px; padding: 9px 11px; margin-bottom: 10px; background: #fff; }
.pvm-rem.auto { border-color: #BFDBFE; background: #EFF6FF; }
.pvm-rem.off { background: #F8FAFC; }
.pvm-rem-ic { color: #2563EB; display: flex; } .pvm-rem.off .pvm-rem-ic { color: #94A3B8; }
.pvm-rem-t { font-size: 12.5px; font-weight: 600; }
.pvm-rem-s { font-size: 10.5px; color: #94A3B8; margin-top: 1px; }
.pvm-rem-when { display: flex; gap: 6px; margin-left: auto; align-items: center; }
.pvm-card .pvm-rem-when input[type=date] { width: 132px; padding: 6px 9px; font-size: 12px; border-radius: 8px; }
.pvm-card .pvm-rem-when input[type=time] { width: 108px; padding: 6px 9px; font-size: 12px; border-radius: 8px; }
.pvm-sw { width: 38px; height: 22px; border-radius: 999px; background: #CBD5E1; position: relative; cursor: pointer; flex: none; transition: .16s; border: 0; padding: 0; }
.pvm-sw::after { content: ""; position: absolute; top: 3px; left: 3px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: .16s; box-shadow: 0 1px 3px rgba(0,0,0,.2); }
.pvm-sw.on { background: #2563EB; }
.pvm-sw.on::after { transform: translateX(16px); }
.pvm-sw:disabled { opacity: .45; cursor: not-allowed; }
.pvm-sw:focus-visible, .pvm-chip:focus-visible, .pvm-seg button:focus-visible, .pvm-hist:focus-visible { outline: 2px solid #2563EB; outline-offset: 2px; }

.pvm-hist { width: 100%; display: flex; align-items: center; gap: 8px; padding: 8px 11px; margin-bottom: 12px; border: 1px solid #E2E8F0; border-radius: 10px; background: #FBFCFE; cursor: pointer; font-family: inherit; font-size: 12px; font-weight: 600; color: #1E293B; text-align: left; }
.pvm-hist:hover:not(:disabled) { background: #F4F8FD; border-color: #CBD5E1; }
.pvm-hist:disabled { cursor: default; color: #94A3B8; }
.pvm-hist .cnt { margin-left: auto; font-size: 10.5px; font-weight: 600; color: #94A3B8; }
.pvm-pips { display: flex; gap: 3px; }
.pvm-pip { width: 6px; height: 6px; border-radius: 50%; }
.pvm-tone-warn { background: #F59E0B; } .pvm-tone-bad { background: #DC2626; } .pvm-tone-good { background: #059669; } .pvm-tone-info { background: #2563EB; } .pvm-tone-none { background: #CBD5E1; }

.pvm-sheet { position: absolute; inset: 0; background: #fff; display: flex; flex-direction: column; z-index: 5; animation: pvm-rise .16s ease-out; }
@keyframes pvm-rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
.pvm-sheet-head { display: flex; align-items: center; gap: 9px; padding: 13px 16px; border-bottom: 1px solid #EEF2F7; }
.pvm-sheet-head h3 { margin: 0; font-size: 14px; font-weight: 700; }
.pvm-sheet-head .sub { font-size: 11.5px; color: #94A3B8; font-weight: 500; }
.pvm-sheet-head .pvm-close { margin-left: auto; }
.pvm-sheet-body { padding: 6px 16px 14px; overflow-y: auto; flex: 1; }
.pvm-hitem { display: flex; gap: 10px; padding: 11px 0; border-bottom: 1px dashed #EEF2F7; }
.pvm-hitem:last-child { border-bottom: 0; }
.pvm-rail { display: flex; flex-direction: column; align-items: center; flex: none; padding-top: 4px; }
.pvm-hdot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.pvm-rail .line { width: 1px; flex: 1; background: #E2E8F0; margin-top: 5px; }
.pvm-hmain { flex: 1; min-width: 0; }
.pvm-hrow { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.pvm-hrow b { font-size: 12.5px; }
.pvm-hbadge { font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 999px; border: 1px solid; }
.pvm-hbadge.warn { background: #FFFBEB; border-color: #FDE68A; color: #B45309; }
.pvm-hbadge.bad { background: #FEF2F2; border-color: #FECACA; color: #DC2626; }
.pvm-hbadge.good { background: #ECFDF5; border-color: #A7F3D0; color: #059669; }
.pvm-hbadge.info, .pvm-hbadge.none { background: #EFF6FF; border-color: #BFDBFE; color: #1E40AF; }
.pvm-hmeta { font-size: 10.5px; color: #94A3B8; margin-top: 3px; }
.pvm-hnote { font-size: 11.5px; color: #64748B; margin-top: 4px; line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
.pvm-hproofs { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.pvm-hproof { display: inline-flex; align-items: center; gap: 7px; padding: 3px 8px 3px 3px; border: 1px solid #E2E8F0; border-radius: 7px; background: #fff; font-family: inherit; cursor: default; }
.pvm-hproof.zoom { cursor: zoom-in; }
.pvm-hproof.zoom:hover { border-color: #CBD5E1; background: #F8FAFC; }
.pvm-hproof img, .pvm-hproof .ph { width: 20px; height: 25px; object-fit: cover; border-radius: 4px; display: flex; align-items: center; justify-content: center; background: #F1F5F9; color: #94A3B8; }
.pvm-hproof span { font-size: 10.5px; color: #64748B; font-weight: 600; }
.pvm-hproof em { font-style: normal; color: #94A3B8; font-weight: 500; }
.pvm-hnone { font-size: 10.5px; color: #94A3B8; margin-top: 6px; display: flex; align-items: center; gap: 5px; }

.pvm-fbtn { padding: 9px 17px; border-radius: 9px; font-size: 13px; font-weight: 600; font-family: inherit; cursor: pointer; border: 1px solid #E2E8F0; background: #fff; color: #64748B; display: inline-flex; align-items: center; gap: 6px; }
.pvm-fbtn:hover { background: #F8FAFC; }
.pvm-fbtn.primary { background: #2563EB; border-color: #2563EB; color: #fff; }
.pvm-fbtn.primary:hover { background: #1E40AF; }
.pvm-fbtn.primary:disabled { background: #93B4F5; border-color: #93B4F5; cursor: not-allowed; }
.pvm-hint { font-size: 11px; color: #94A3B8; margin-right: auto; }
.pvm-err { display: flex; align-items: center; gap: 7px; font-size: 12.5px; border-radius: 9px; padding: 8px 11px; margin-bottom: 10px; background: #FEF2F2; border: 1px solid #FCA5A5; color: #B91C1C; }
.pvm-spin { animation: pvm-rotate .8s linear infinite; }
@keyframes pvm-rotate { to { transform: rotate(360deg); } }
.pvm-fbtn:disabled { opacity: .6; cursor: not-allowed; }
.pvm-lb { position: fixed; inset: 0; background: rgba(15,23,42,.72); display: flex; align-items: center; justify-content: center; z-index: 1100; cursor: zoom-out; }
.pvm-lb img { max-width: 88vw; max-height: 88vh; border-radius: 12px; box-shadow: 0 24px 60px rgba(0,0,0,.4); }

@media (max-width: 640px) {
  .pvm-overlay { padding: 0; align-items: flex-end; }
  .pvm-card { max-width: 100%; max-height: 100vh; border-radius: 14px 14px 0 0; }
  .pvm-top, .pvm-mid { grid-template-columns: 1fr; }
  .pvm-prow { flex-wrap: wrap; }
  .pvm-prow > * { flex-basis: 100%; }
  .pvm-rem { flex-wrap: wrap; }
  .pvm-rem-when { margin-left: 0; width: 100%; }
  .pvm-hint { display: none; }
}
`;

// Rendered once by the table so the per-row button/badge/popover are styled.
export function PhoneVerifyStyles() {
  return <style>{styles}</style>;
}

// Turns green once at least one call has been logged for the number.
export function PhoneVerifyButton({ onClick, verified = false }) {
  return (
    <button type="button" className={"pvm-btn" + (verified ? " done" : "")}
      aria-label={verified ? "Log another call" : "Verify mobile number"} onClick={onClick}>
      <ShieldCheck size={14} />
    </button>
  );
}

// Outcome icon next to the number for the latest attempt; hovering (or
// clicking/focusing) it shows a popover with the outcome and comments. A short
// close delay lets the pointer cross the gap into the popover. The popover is
// portalled to <body>: the table card's backdrop-filter would otherwise make
// position:fixed resolve against the card and its overflow:hidden would clip it.
export function PhoneVerifyBadge({ result, onEdit }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState(null);
  const btnRef = useRef(null);
  const popRef = useRef(null);
  const hideTimer = useRef(null);
  const show = () => { clearTimeout(hideTimer.current); setOpen(true); };
  const hideSoon = () => { clearTimeout(hideTimer.current); hideTimer.current = setTimeout(() => setOpen(false), 150); };
  useEffect(() => () => clearTimeout(hideTimer.current), []);

  useLayoutEffect(() => {
    if (!open || !btnRef.current) return;
    const r = btnRef.current.getBoundingClientRect();
    const width = 280;
    const left = Math.max(8, Math.min(r.left + r.width / 2 - width / 2, window.innerWidth - width - 8));
    const popH = popRef.current?.offsetHeight || 160;
    const below = r.bottom + 8 + popH <= window.innerHeight;
    setPos({ left, top: below ? r.bottom + 8 : Math.max(8, r.top - 8 - popH) });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    const onDown = (e) => {
      if (popRef.current?.contains(e.target) || btnRef.current?.contains(e.target)) return;
      close();
    };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  const last = result;
  const st = last && PHONE_STATUSES[last.status];
  if (!st) return null;
  const { Icon } = st;
  const oc = outcomeOf(last);
  const count = last.attempts;
  return (
    <>
      <button ref={btnRef} type="button" className="pvm-badge" aria-expanded={open}
        aria-label={`${st.label}${oc ? ` — ${oc.l}` : ""} — view comments`}
        style={{ color: st.color, background: st.bg, borderColor: st.border }}
        onMouseEnter={show} onMouseLeave={hideSoon} onFocus={show}
        onClick={show}>
        <Icon size={13} />
      </button>
      {open && createPortal(
        <div ref={popRef} className="pvm-pop" role="dialog" aria-label="Call details"
          onMouseEnter={show} onMouseLeave={hideSoon}
          style={{ borderColor: st.border, borderTop: `3px solid ${st.color}`,
            ...(pos ? { left: pos.left, top: pos.top } : { visibility: "hidden", left: 0, top: 0 }) }}>
          <div className="pvm-pop-head">
            <div>
              <span className="pvm-pop-status" style={{ color: st.color }}><Icon size={14} /> {st.label}</span>
              {oc && <div className="pvm-pop-outcome">{oc.l}</div>}
            </div>
            <button className="pvm-close" style={{ width: 24, height: 24 }} onClick={() => setOpen(false)} aria-label="Close"><X size={14} /></button>
          </div>
          <div className="pvm-pop-label">Comments</div>
          {last.remarks
            ? <div className="pvm-pop-text">{last.remarks}</div>
            : <div className="pvm-pop-empty">No comments added.</div>}
          <div className="pvm-pop-tags">
            {last.reminderAt && <span className="pvm-pop-tag"><Bell size={11} /> {fmtTime(last.reminderAt)}</span>}
            {last.proof?.length > 0 && <span className="pvm-pop-tag muted"><Paperclip size={11} /> {last.proof.length} proof</span>}
            <span className="pvm-pop-tag muted">Attempt {count}</span>
          </div>
          <div className="pvm-pop-foot">
            <span>{last.at ? fmtTime(last.at) : ""}{last.by ? ` · ${last.by}` : ""}</span>
            {onEdit && <button className="pvm-link" onClick={() => { setOpen(false); onEdit(); }}><PhoneCall size={12} /> Log call</button>}
          </div>
        </div>,
        document.body
      )}
    </>
  );
}

/* ---------- small controls ---------- */
function Chips({ options, value, onChange, multi = false }) {
  const sel = multi ? value || [] : value;
  const isOn = (o) => (multi ? sel.includes(o) : sel === o);
  const toggle = (o) => {
    if (multi) onChange(isOn(o) ? sel.filter((x) => x !== o) : [...sel, o]);
    else onChange(isOn(o) ? "" : o);
  };
  return options.map((o) => (
    <button key={o} type="button" className={"pvm-chip" + (isOn(o) ? " on" : "")} aria-pressed={isOn(o)} onClick={() => toggle(o)}>{o}</button>
  ));
}

function CheckRow({ on, danger, onChange, children }) {
  return (
    <button type="button" role="checkbox" aria-checked={!!on} className={"pvm-check" + (on ? " on" : "") + (danger ? " danger" : "")}
      onClick={() => onChange(!on)}>
      <span className="pvm-box">{on && <Check size={10} strokeWidth={3} />}</span><span>{children}</span>
    </button>
  );
}

const Req = () => <span className="pvm-req">*</span>;

/* Outcome-specific fields — at most two rows each. `d` is the details object. */
function OutcomePanel({ c, d, set, ctx, onPreset }) {
  switch (c.panel) {
    case "callback":
      return (
        <div><p className="pvm-flabel">Preferred window</p>
          <div className="pvm-chips">
            <Chips options={WINDOWS} value={d.window} onChange={(v) => set({ window: v })} />
            <span style={{ fontSize: 11, color: "#94A3B8", marginLeft: 4 }}>Exact time set in the reminder below</span>
          </div>
        </div>
      );
    case "next":
      return (<>
        <div className="pvm-note good"><Check size={13} /><span>Positive response — this POC will be flagged <b>warm</b>.</span></div>
        <div className="pvm-prow">
          <div className="grow2"><p className="pvm-flabel">Next step <Req /></p>
            <div className="pvm-chips"><Chips multi options={NEXT_STEPS} value={d.nextSteps} onChange={(v) => set({ nextSteps: v })} /></div></div>
          <div><p className="pvm-flabel">Requirement discussed</p>
            <input type="text" placeholder="e.g. 4 × Sr DE, Blr" value={d.requirement || ""} onChange={(e) => set({ requirement: e.target.value })} /></div>
        </div>
      </>);
    case "email":
      return (
        <div className="pvm-prow">
          <div><p className="pvm-flabel">Send details to <Req /></p>
            <input type="email" placeholder={ctx.email || "name@company.com"} value={d.sendTo || ""} onChange={(e) => set({ sendTo: e.target.value })} /></div>
          <div className="grow2"><p className="pvm-flabel">What to send</p>
            <div className="pvm-chips"><Chips multi options={SEND_WHAT} value={d.sendWhat} onChange={(v) => set({ sendWhat: v })} /></div></div>
        </div>
      );
    case "reason":
      return (
        <div className="pvm-prow">
          <div className="grow2"><p className="pvm-flabel">Reason <Req /></p>
            <div className="pvm-sel">
              <select className={d.reason ? "" : "ph"} value={d.reason || ""} onChange={(e) => set({ reason: e.target.value })}>
                <option value="">Select a reason…</option>
                {REASONS.map((r) => <option key={r}>{r}</option>)}
              </select><ChevronDown size={13} className="pvm-chev" />
            </div></div>
          <div><p className="pvm-flabel">Revisit after</p>
            <div className="pvm-chips"><Chips options={REVISIT} value={d.revisit} onChange={(v) => set({ revisit: v })} /></div></div>
        </div>
      );
    case "wrong":
      return (<>
        <div className="pvm-note bad"><AlertTriangle size={13} /><span><b>{ctx.phone}</b> will be marked invalid and dropped from the dialler.</span></div>
        <div className="pvm-prow">
          <CheckRow danger on={d.markInvalid !== false} onChange={(v) => set({ markInvalid: v })}>
            Mark invalid{ctx.pocName ? ` for ${ctx.pocName}` : ""}
          </CheckRow>
          <div><input type="tel" placeholder="Correct number, if known" value={d.correctNumber || ""} onChange={(e) => set({ correctNumber: e.target.value })} /></div>
        </div>
      </>);
    case "redirect":
      return (<>
        <div className="pvm-note info"><CornerUpRight size={13} /><span>A new POC will be created{ctx.company ? <> against <b>{ctx.company}</b></> : ""}.</span></div>
        <div className="pvm-prow">
          <div><p className="pvm-flabel">Referred to <Req /></p>
            <input type="text" placeholder="Full name" value={d.referredName || ""} onChange={(e) => set({ referredName: e.target.value })} /></div>
          <div><p className="pvm-flabel">Their number</p>
            <input type="tel" placeholder="+91 " value={d.referredPhone || ""} onChange={(e) => set({ referredPhone: e.target.value })} /></div>
          <div><p className="pvm-flabel">Team</p>
            <input type="text" placeholder="e.g. Vendor Mgmt" value={d.referredTeam || ""} onChange={(e) => set({ referredTeam: e.target.value })} /></div>
        </div>
      </>);
    case "lang":
      return (
        <div className="pvm-prow">
          <div className="grow2"><p className="pvm-flabel">Preferred language</p>
            <div className="pvm-chips"><Chips options={LANGUAGES} value={d.language} onChange={(v) => set({ language: v })} /></div></div>
          <div style={{ paddingBottom: 4 }}><CheckRow on={d.reassign} onChange={(v) => set({ reassign: v })}>Reassign recruiter</CheckRow></div>
        </div>
      );
    case "dnc":
      return (<>
        <div className="pvm-note bad"><Ban size={13} /><span><b>Do-not-call.</b> Suppressed across calls, email and WhatsApp.</span></div>
        <CheckRow danger on={d.dncConfirm} onChange={(v) => set({ dncConfirm: v })}>
          I confirm the contact asked not to be approached again <Req />
        </CheckRow>
      </>);
    case "retry":
      return (<>
        <div className="pvm-note warn"><RotateCcw size={13} /><span>Attempt <b>{ctx.attempt}</b> — pick when to try again.</span></div>
        <div><p className="pvm-flabel">Retry at</p>
          <div className="pvm-chips">
            <Chips options={Object.keys(RETRY_PRESETS)} value={d.preset}
              onChange={(v) => { set({ preset: v }); if (v) onPreset(RETRY_PRESETS[v]()); }} />
          </div>
        </div>
      </>);
    case "invalid":
      return (<>
        <div className="pvm-note bad"><AlertTriangle size={13} /><span>The number will be marked <b>invalid</b> — no further attempts scheduled.</span></div>
        <div className="pvm-prow">
          <CheckRow danger on={d.markInvalid !== false} onChange={(v) => set({ markInvalid: v })}>Mark {ctx.phone} invalid</CheckRow>
          <CheckRow on={d.reenrich} onChange={(v) => set({ reenrich: v })}>Queue for number re-enrichment</CheckRow>
        </div>
      </>);
    default:
      return null;
  }
}

function ProofThumb({ file, className }) {
  return file.src
    ? <img className={className} src={file.src} alt="" />
    : <span className={className}><Paperclip size={12} /></span>;
}

// `calls` is oldest first.
function HistorySheet({ calls, subtitle, onClose, onZoom }) {
  const newestFirst = calls.map((a, i) => ({ ...a, n: i + 1 })).reverse();
  return (
    <div className="pvm-sheet" role="dialog" aria-label="Call history">
      <div className="pvm-sheet-head">
        <h3>Call history</h3>
        <span className="sub">{subtitle}</span>
        <button className="pvm-close" onClick={onClose} aria-label="Close call history"><X size={15} /></button>
      </div>
      <div className="pvm-sheet-body">
        {newestFirst.map((h, i) => {
          const oc = outcomeOf(h);
          const tone = oc?.tone || (h.status === "answered" ? "good" : "bad");
          return (
            <div key={h.id ?? h.n} className="pvm-hitem">
              <div className="pvm-rail"><span className={`pvm-hdot pvm-tone-${tone}`} />{i < newestFirst.length - 1 && <span className="line" />}</div>
              <div className="pvm-hmain">
                <div className="pvm-hrow"><b>Attempt {h.n}</b>
                  <span className={`pvm-hbadge ${tone}`}>{PHONE_STATUSES[h.status]?.label}{oc ? ` · ${oc.l}` : ""}</span></div>
                <div className="pvm-hmeta">{[h.at && fmtTime(h.at), h.by].filter(Boolean).join(" · ")}
                  {h.reminderAt ? ` · reminder ${fmtTime(h.reminderAt)}` : ""}</div>
                {h.remarks && <div className="pvm-hnote">{h.remarks}</div>}
                {h.proof?.length ? (
                  <div className="pvm-hproofs">
                    {h.proof.map((f, k) => (
                      <button key={k} type="button" className={"pvm-hproof" + (f.src ? " zoom" : "")} onClick={() => f.src && onZoom(f.src)}>
                        <ProofThumb file={f} className={f.src ? "" : "ph"} />
                        <span>{f.name} <em>· {f.type}{f.size ? ` · ${f.size}` : ""}</em></span>
                      </button>
                    ))}
                  </div>
                ) : <div className="pvm-hnone"><AlertTriangle size={12} /> No proof attached</div>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* Logs one call. Loads the job's call history from the server; `latest` (the
 * table's latest call, if any) gives the attempt number until it arrives.
 * onSave receives the call payload and may return a promise — the modal stays
 * open with the error shown if it rejects, and closes once it resolves. */
export default function PhoneVerifyModal({ jobId, phone, pocName, pocTitle, company, email, latest = null, onSave = () => {}, onClose = () => {} }) {
  const [hist, setHist] = useState({ loading: true, calls: [], error: "" });
  const history = hist.calls;
  const attemptNo = (hist.loading ? latest?.attempts || 0 : history.length) + 1;
  const [busy, setBusy] = useState(false);
  const [saveError, setSaveError] = useState("");

  useEffect(() => {
    let cancelled = false;
    getPhoneCallHistory(jobId)
      .then((res) => {
        if (cancelled) return;
        const calls = (res?.items || []).map(callFromView).reverse(); // API is newest first
        setHist({ loading: false, calls, error: "" });
      })
      .catch((err) => { if (!cancelled) setHist({ loading: false, calls: [], error: err?.message || "Couldn't load call history." }); });
    return () => { cancelled = true; };
  }, [jobId]);

  const [status, setStatusRaw] = useState(null);
  const [outcome, setOutcomeRaw] = useState(null);
  const [details, setDetails] = useState({});
  const [remarks, setRemarks] = useState("");
  const [files, setFiles] = useState([]);
  const [rem, setRemState] = useState({ on: false, date: "", time: "" });
  const [sheetOpen, setSheetOpen] = useState(false);
  const [zoom, setZoom] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInput = useRef(null);

  const c = status && outcome ? OUTCOMES[status].find((o) => o.v === outcome) : null;
  const proofNeeded = !!c && (c.proof === "required" || (c.proof === "from2" && attemptNo >= 2));
  const set = (patch) => setDetails((prev) => ({ ...prev, ...patch }));

  const resetOutcome = () => { setDetails({}); setFiles([]); setRemState({ on: false, date: "", time: "" }); };
  const setStatus = (s) => { setStatusRaw(s); setOutcomeRaw(null); resetOutcome(); };
  const setOutcome = (v) => {
    setOutcomeRaw(v || null);
    const oc = v ? OUTCOMES[status].find((o) => o.v === v) : null;
    setDetails(oc?.preset ? { preset: oc.preset } : {});
    setFiles([]);
    if (oc?.rem) { const [date, time] = suggestedWhen(oc); setRemState({ on: true, date, time }); }
    else setRemState({ on: false, date: "", time: "" });
  };
  // Switching the reminder off keeps the date/time, so nothing typed is lost.
  const toggleRem = () => {
    if (!c || c.remLock) return;
    setRemState((r) => {
      if (r.on) return { ...r, on: false };
      if (r.date) return { ...r, on: true };
      const [date, time] = daysFromNow(1, "10:00");
      return { on: true, date, time };
    });
  };
  const applyPreset = ([date, time]) => setRemState({ on: true, date, time });

  const addFiles = (list) => {
    const incoming = [...list].slice(0, MAX_FILES - files.length);
    incoming.forEach((f) => {
      const id = `${f.name}-${f.size}-${Math.random()}`;
      const item = { id, name: f.name, size: `${Math.max(1, Math.round(f.size / 1024))} KB`, type: PROOF_TYPES[0], src: "" };
      setFiles((prev) => (prev.length < MAX_FILES ? [...prev, item] : prev));
      if (f.type.startsWith("image/") && f.size <= MAX_INLINE_PROOF) {
        const r = new FileReader();
        r.onload = (e) => setFiles((prev) => prev.map((x) => (x.id === id ? { ...x, src: e.target.result } : x)));
        r.readAsDataURL(f);
      }
    });
    if (fileInput.current) fileInput.current.value = "";
  };

  // Paste a screenshot straight into the proof list once an outcome is picked.
  useEffect(() => {
    if (!c) return;
    const onPaste = (ev) => {
      const imgs = [...(ev.clipboardData?.items || [])].filter((i) => i.type.startsWith("image/")).map((i) => i.getAsFile());
      if (imgs.length) { ev.preventDefault(); addFiles(imgs); }
    };
    document.addEventListener("paste", onPaste);
    return () => document.removeEventListener("paste", onPaste);
  });

  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      if (zoom) setZoom(null);
      else if (sheetOpen) setSheetOpen(false);
      else if (!busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, zoom, sheetOpen, busy]);

  // Gate Save; the hint says what is still missing.
  let ok = false, why;
  if (!status) why = "Select a call status to continue";
  else if (!c) why = status === "answered" ? "Select a call outcome" : "Select why the call did not connect";
  else if (c.panel === "reason" && !details.reason) why = "Select a reason";
  else if (c.panel === "dnc" && !details.dncConfirm) why = "Confirm the do-not-call request";
  else if (c.panel === "next" && !details.nextSteps?.length) why = "Pick at least one next step";
  else if (c.panel === "email" && !details.sendTo?.trim()) why = "Add the email to send details to";
  else if (c.panel === "redirect" && !details.referredName?.trim()) why = "Add who you were referred to";
  else if (proofNeeded && !files.length) why = "Attach proof of the call to save";
  else { ok = true; why = `Saving as “${c.l}” · attempt ${attemptNo}`; }

  const save = async () => {
    if (!ok || busy) return;
    const reminderAt = rem.on && rem.date ? new Date(`${rem.date}T${rem.time || "10:00"}`) : null;
    setBusy(true);
    setSaveError("");
    try {
      await onSave({
        phone, status, outcome, details, remarks: remarks.trim(),
        proof: files.map(({ name, size, type, src }) => ({ name, size, type, src })),
        reminder_at: reminderAt && !isNaN(reminderAt) ? reminderAt.toISOString() : null,
      });
      onClose();
    } catch (err) {
      setSaveError(err?.message || "Couldn't save the call. Please try again.");
      setBusy(false);
    }
  };

  const remSub = !c ? "Pick an outcome to schedule the next call."
    : c.remLock ? "Not available — the contact is suppressed."
    : rem.on ? (c.rem ? "Suggested by this outcome · edit the date or time freely." : "The contact moves into the Follow-ups queue at this time.")
    : "Switch on to schedule the next call.";
  const withProof = history.filter((h) => h.proof?.length).length;
  const ctx = { phone, pocName, company, email, attempt: attemptNo };

  return (
    <div className="pvm-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <style>{styles}</style>
      <div className="pvm-card" role="dialog" aria-modal="true" aria-label="Verify mobile number">
        <div className="pvm-head">
          <h2>Verify mobile number {company && <span className="pvm-org">· {company}</span>}</h2>
          <button className="pvm-close" onClick={onClose} disabled={busy} aria-label="Close"><X size={16} /></button>
        </div>

        <div className="pvm-body">
          <div className="pvm-contact">
            <div>
              <div className="pvm-num">{phone}</div>
              {(pocName || pocTitle) && <div className="pvm-nm">{[pocName, pocTitle].filter(Boolean).join(" · ")}</div>}
            </div>
          </div>

          <div className="pvm-top">
            <div>
              <p className="pvm-lbl">Call status <Req /></p>
              <div className="pvm-seg" role="radiogroup" aria-label="Call status">
                <button type="button" role="radio" aria-checked={status === "answered"} className={status === "answered" ? "on" : ""}
                  onClick={() => setStatus("answered")}><span className="pvm-dot" /> Answered</button>
                <button type="button" role="radio" aria-checked={status === "unanswered"} className={"neg" + (status === "unanswered" ? " on" : "")}
                  onClick={() => setStatus("unanswered")}><span className="pvm-dot" /> Unanswered</button>
              </div>
            </div>
            <div>
              <p className="pvm-lbl">{status === "unanswered" ? "Reason not answered" : "Call outcome"} <Req /></p>
              <div className="pvm-sel">
                <select className={outcome ? "" : "ph"} disabled={!status} value={outcome || ""} onChange={(e) => setOutcome(e.target.value)}>
                  <option value="">{!status ? "Pick a call status first…" : status === "answered" ? "What came out of the call…" : "Why it did not connect…"}</option>
                  {status && OUTCOMES[status].map((o) => <option key={o.v} value={o.v}>{o.l}</option>)}
                </select>
                <ChevronDown size={13} className="pvm-chev" />
              </div>
            </div>
          </div>

          {c?.panel && (
            <div className="pvm-panel">
              <OutcomePanel c={c} d={details} set={set} ctx={ctx} onPreset={applyPreset} />
            </div>
          )}

          <div className="pvm-mid">
            <div>
              <p className="pvm-lbl">Comments <span className="pvm-tag opt">OPTIONAL</span></p>
              <textarea value={remarks} onChange={(e) => setRemarks(e.target.value)}
                placeholder={status === "unanswered" ? "e.g. Switched off, will retry tomorrow" : "e.g. Spoke to the recruiter, prefers calls after 2 PM"} />
            </div>
            <div>
              <p className="pvm-lbl">Proof of call <span className={"pvm-tag " + (proofNeeded ? "need" : "opt")}>{proofNeeded ? "REQUIRED" : "OPTIONAL"}</span></p>
              {files.length ? (
                <div className="pvm-files">
                  {files.map((f) => (
                    <div key={f.id} className="pvm-fcard">
                      <ProofThumb file={f} className="pvm-fthumb" />
                      <div className="pvm-fmeta">
                        <div className="pvm-fname" title={f.name}>{f.name}</div>
                        <div className="pvm-fsel pvm-sel">
                          <select value={f.type} aria-label="Proof type"
                            onChange={(e) => setFiles((prev) => prev.map((x) => (x.id === f.id ? { ...x, type: e.target.value } : x)))}>
                            {PROOF_TYPES.map((t) => <option key={t}>{t}</option>)}
                          </select><ChevronDown size={10} className="pvm-chev" />
                        </div>
                      </div>
                      <button type="button" className="pvm-fx" aria-label={`Remove ${f.name}`}
                        onClick={() => setFiles((prev) => prev.filter((x) => x.id !== f.id))}><X size={12} /></button>
                    </div>
                  ))}
                  {files.length < MAX_FILES && <button type="button" className="pvm-more" onClick={() => fileInput.current?.click()}>+ add another</button>}
                </div>
              ) : (
                <button type="button" className={"pvm-drop" + (dragOver ? " over" : "") + (proofNeeded ? " err" : "")}
                  onClick={() => fileInput.current?.click()}
                  onDragEnter={(e) => { e.preventDefault(); setDragOver(true); }}
                  onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
                  onDragLeave={(e) => { e.preventDefault(); setDragOver(false); }}
                  onDrop={(e) => { e.preventDefault(); setDragOver(false); addFiles(e.dataTransfer.files); }}>
                  <Upload size={16} />
                  <div>
                    <div className="pvm-drop-t">Drop, paste or <u>browse</u></div>
                    <div className="pvm-drop-s">
                      {proofNeeded ? (c.short || `Call-log screenshot — attempt ${attemptNo}`) : "Screenshot or recording · up to 3"}
                    </div>
                  </div>
                </button>
              )}
              <input ref={fileInput} type="file" hidden multiple accept="image/*,application/pdf,audio/*"
                onChange={(e) => addFiles(e.target.files)} />
            </div>
          </div>

          <div className={"pvm-rem" + (!rem.on ? " off" : c?.rem ? " auto" : "")}>
            <span className="pvm-rem-ic"><Bell size={15} /></span>
            <div>
              <div className="pvm-rem-t">Reminder</div>
              <div className="pvm-rem-s">{remSub}</div>
            </div>
            <div className="pvm-rem-when">
              <input type="date" aria-label="Reminder date" disabled={!rem.on} value={rem.date}
                onChange={(e) => setRemState((r) => ({ ...r, date: e.target.value }))} />
              <input type="time" aria-label="Reminder time" disabled={!rem.on} value={rem.time}
                onChange={(e) => setRemState((r) => ({ ...r, time: e.target.value }))} />
              <button type="button" role="switch" aria-checked={rem.on} aria-label="Reminder"
                className={"pvm-sw" + (rem.on ? " on" : "")} disabled={!c || c.remLock} onClick={toggleRem} />
            </div>
          </div>

          {saveError && <div className="pvm-err" role="alert"><AlertCircle size={14} style={{ flex: "none" }} /> {saveError}</div>}

          <button type="button" className="pvm-hist" disabled={!history.length} onClick={() => setSheetOpen(true)}>
            {history.length > 0 && (
              <span className="pvm-pips">
                {[...history].reverse().map((h, i) => (
                  <span key={i} className={`pvm-pip pvm-tone-${outcomeOf(h)?.tone || "none"}`} />
                ))}
              </span>
            )}
            Call history
            <span className="cnt">
              {hist.loading ? "Loading…"
                : hist.error ? hist.error
                : history.length
                  ? `${history.length} earlier call${history.length > 1 ? "s" : ""} · ${withProof} with proof`
                  : "No earlier calls"}
            </span>
            {history.length > 0 && <ChevronRight size={14} color="#94A3B8" />}
          </button>
        </div>

        <div className="pvm-foot">
          <span className="pvm-hint">{why}</span>
          <button className="pvm-fbtn" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="pvm-fbtn primary" onClick={save} disabled={!ok || busy}>
            {busy ? <RefreshCw size={14} className="pvm-spin" /> : <Check size={14} />} {busy ? "Saving…" : "Save"}
          </button>
        </div>

        {sheetOpen && (
          <HistorySheet calls={history} onClose={() => setSheetOpen(false)} onZoom={setZoom}
            subtitle={`${phone}${pocName ? ` · ${pocName}` : ""} · ${history.length} attempt${history.length > 1 ? "s" : ""}`} />
        )}
      </div>
      {zoom && <div className="pvm-lb" onClick={() => setZoom(null)}><img src={zoom} alt="Proof of call" /></div>}
    </div>
  );
}
