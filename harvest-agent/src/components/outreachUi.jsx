import React from "react";
import {
  Send, CheckCircle2, MailOpen, MousePointerClick, Ban, CornerUpLeft, ShieldAlert, BellOff,
} from "lucide-react";

/* Shared outreach formatting + badges, used by both the Mail logs list
 * (OutreachHistoryPage) and the standalone thread page (OutreachThreadPage) so a
 * sent mail's status, tone, delivery timeline and avatar look identical in both.
 * Self-contained inline styles — no dependency on any page's CSS. */

// ── Formatting helpers ──────────────────────────────────────────────────────

export function fmtAbs(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

export function fmtTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

// Compact "3h ago" / "2d ago" relative label for the Sent column.
export function fmtRel(iso) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return fmtAbs(iso);
}

const AVATAR_COLORS = ["#6366F1", "#0EA5E9", "#8B5CF6", "#10B981", "#F43F5E", "#F59E0B", "#14B8A6", "#EC4899"];
export function initials(name, email) {
  const base = (name || email || "?").trim();
  const parts = base.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return base.slice(0, 2).toUpperCase();
}
export function avatarColor(seed) {
  const s = (seed || "?");
  let n = 0;
  for (let i = 0; i < s.length; i += 1) n = (n + s.charCodeAt(i)) % AVATAR_COLORS.length;
  return AVATAR_COLORS[n];
}

// Delivery engagement — derived from the send status + latest Mailjet event.
export function engagement(it) {
  if (it.status === "failed") return { label: "Failed", color: "#B91C1C", dot: "#B91C1C" };
  switch (it.delivery_status) {
    case "clicked":
      return { label: "Clicked", color: "#0D9488", dot: "#0D9488" };
    case "opened":
      return { label: "Opened", color: "#0E7C5A", dot: "#0E7C5A" };
    case "delivered":
      return { label: "Delivered", color: "#1E40AF", dot: "#2563EB" };
    case "bounced":
      return { label: "Bounced", color: "#B91C1C", dot: "#B91C1C" };
    case "blocked":
      return { label: "Blocked", color: "#B91C1C", dot: "#B91C1C" };
    case "spam":
      return { label: "Spam", color: "#B91C1C", dot: "#B91C1C" };
    case "unsubscribed":
      return { label: "Unsubscribed", color: "#6D28D9", dot: "#6D28D9" };
    default:
      return { label: "Sent", color: "#64748B", dot: "#94A3B8" };
  }
}

// Delivery-state label → dot/text color, shared by the detail timeline steps.
export const EVENT_COLOR = {
  Delivered: "#1E40AF", Opened: "#0E7C5A", Clicked: "#0D9488",
  Bounced: "#B91C1C", Blocked: "#B91C1C", Spam: "#B91C1C", Unsubscribed: "#6D28D9",
};

// The ordered delivery lifecycle for the detail timeline, built from the row's scalar
// timestamps + event trail (not just events[]). This surfaces states the raw trail
// misses: "Delivered" is seeded from the send's 200 response (delivered_at, no webhook),
// and a click implies an open (opened_at), so both show even when Mailjet only fired a
// click. De-duped by label in Sent → Delivered → Opened → Clicked order, with any
// terminal negative (Bounced/Blocked/Spam) or Unsubscribed appended.
export function deliveryTimeline(it) {
  const steps = [];
  const add = (label, at) => { if (at && !steps.some((s) => s.label === label)) steps.push({ label, at }); };
  add("Sent", it.created_at);            // always present
  add("Delivered", it.delivered_at);     // from the 200 (or a 'sent' webhook)
  add("Opened", it.opened_at);           // open, or click implies open
  const click = (it.events || []).find((e) => (e.event || "").toLowerCase() === "click");
  if (click) add("Clicked", click.at);
  if (["bounced", "blocked", "spam"].includes(it.delivery_status)) add(engagement(it).label, it.bounced_at);
  if (it.delivery_status === "unsubscribed") {
    const un = (it.events || []).find((e) => (e.event || "").toLowerCase() === "unsub");
    add("Unsubscribed", (un && un.at) || it.created_at);
  }
  return steps;
}

export const TONE_STYLE = {
  Formal: { color: "#3730A3", bg: "#EEF0FF" },
  Friendly: { color: "#0E7C5A", bg: "#E7F7F0" },
  Direct: { color: "#9A3412", bg: "#FFF1E8" },
  "Follow-up": { color: "#92580B", bg: "#FFF6E9" },
};
export function toneMeta(it) {
  if (it.tone && TONE_STYLE[it.tone]) return { label: it.tone, ...TONE_STYLE[it.tone] };
  if (it.tone) return { label: it.tone, color: "#475569", bg: "#F1F5F9" };
  if (it.outreach_kind === "followup") return { label: "Follow-up", ...TONE_STYLE["Follow-up"] };
  return null;
}

export const CLIENT_LABEL = { active: "Active client", new: "New client", unknown: "" };

// The positive delivery lifecycle, in order — the set of stages a mail progresses through.
export const ENGAGEMENT_STAGES = ["Delivered", "Opened", "Clicked"];

// Per-status icon + soft-pill palette for the engagement badges.
export const STATUS_META = {
  Delivered:    { Icon: CheckCircle2,      color: "#1E7F4F", bg: "#E7F7EE" },
  Opened:       { Icon: MailOpen,          color: "#0E7C5A", bg: "#E7F7F0" },
  Clicked:      { Icon: MousePointerClick, color: "#0D7D74", bg: "#E4F5F3" },
  Sent:         { Icon: Send,              color: "#64748B", bg: "#F1F5F9" },
  Failed:       { Icon: Ban,               color: "#DC2626", bg: "#FDECEC" },
  Bounced:      { Icon: CornerUpLeft,      color: "#DC2626", bg: "#FDECEC" },
  Blocked:      { Icon: Ban,               color: "#DC2626", bg: "#FDECEC" },
  Spam:         { Icon: ShieldAlert,       color: "#DC2626", bg: "#FDECEC" },
  Unsubscribed: { Icon: BellOff,           color: "#6D28D9", bg: "#F1EBFD" },
};

// The delivery states a mail has reached, as labels — one icon badge is rendered per
// entry. A failed send or terminal negative (bounced/blocked/spam/unsubscribed) is a
// single badge; a positive send shows the cumulative lifecycle up to the furthest stage
// (e.g. Delivered → Opened → Clicked).
export function engagementLabels(it) {
  if (it.status === "failed") return ["Failed"];
  const ds = it.delivery_status;
  if (ds === "bounced") return ["Bounced"];
  if (ds === "blocked") return ["Blocked"];
  if (ds === "spam") return ["Spam"];
  if (ds === "unsubscribed") return ["Unsubscribed"];
  const furthest = ds === "clicked" ? "Clicked" : ds === "opened" ? "Opened" : ds === "delivered" ? "Delivered" : null;
  const idx = ENGAGEMENT_STAGES.indexOf(furthest);
  return idx === -1 ? ["Sent"] : ENGAGEMENT_STAGES.slice(0, idx + 1);
}

// ── Presentational bits ─────────────────────────────────────────────────────

export function StatusBadge({ label }) {
  const meta = STATUS_META[label] || STATUS_META.Sent;
  const Icon = meta.Icon;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 5, background: meta.bg, color: meta.color, borderRadius: 999, padding: "3px 10px 3px 8px", fontSize: 11.5, fontWeight: 700, whiteSpace: "nowrap" }}>
      <Icon size={13} strokeWidth={2.5} /> {label}
    </span>
  );
}

// Engagement cell: the reached delivery states as horizontal icon badges.
export function EngagementCell({ it }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
      {engagementLabels(it).map((label) => <StatusBadge key={label} label={label} />)}
    </span>
  );
}

export function ToneChip({ it }) {
  const t = toneMeta(it);
  if (!t) return <span style={{ color: "#CBD5E1" }}>—</span>;
  return <span style={{ fontSize: 11.5, fontWeight: 700, color: t.color, background: t.bg, borderRadius: 999, padding: "3px 10px", whiteSpace: "nowrap" }}>{t.label}</span>;
}

export function TimelineStep({ on, label, time, color = "#0E7C5A" }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, color: on ? "#334155" : "#94A3B8" }}>
      <span style={{ width: 7, height: 7, borderRadius: "50%", background: on ? color : "#CBD5E1" }} />
      {label} {on && <b style={{ color: "#0F172A", fontWeight: 700 }}>{time}</b>}
    </span>
  );
}
