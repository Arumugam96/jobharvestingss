import React, { useEffect, useState } from "react";
import { Mail, ChevronDown } from "lucide-react";

/* Read-only view of a recruiter-outreach thread — the messages already sent for
 * one job/recruiter (initial email, follow-ups, logged LinkedIn notes), oldest
 * first. Presentational only: the caller fetches via getOutreachHistory and
 * passes the items. Shared by the Mail logs page (row detail modal) and the
 * follow-up composer (EmailComposeModal) so a "sent thread" looks identical in
 * both. Self-contained inline styles — no dependency on any page's CSS.
 *
 * Two layouts:
 *   • default (collapsible=false) — every message rendered as a static, fully
 *     expanded card (the Mail logs detail modal).
 *   • collapsible=true — each message is an accordion card; clicking a header
 *     expands that one and collapses the others ("opens from above"). The parent
 *     can force every card shut via `forceCollapsed` (the composer does this when
 *     the user clicks "Generate follow-up"). */

const LinkedInGlyph = ({ size = 12 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M20.45 20.45h-3.56v-5.57c0-1.33-.02-3.04-1.85-3.04-1.85 0-2.14 1.45-2.14 2.94v5.67H9.35V9h3.41v1.56h.05c.48-.9 1.64-1.85 3.37-1.85 3.6 0 4.27 2.37 4.27 5.45v6.29ZM5.34 7.43a2.06 2.06 0 1 1 0-4.13 2.06 2.06 0 0 1 0 4.13ZM7.12 20.45H3.55V9h3.57v11.45ZM22.22 0H1.77C.79 0 0 .77 0 1.73v20.54C0 23.22.79 24 1.77 24h20.45c.98 0 1.78-.78 1.78-1.73V1.73C24 .77 23.2 0 22.22 0Z" />
  </svg>
);

function fmtDate(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

function ChannelChip({ channel }) {
  const isLi = channel === "linkedin";
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 11, fontWeight: 600, color: isLi ? "#0A66C2" : "#1E40AF", background: isLi ? "#EAF2FB" : "#EFF6FF", borderRadius: 999, padding: "2px 8px" }}>
      {isLi ? <LinkedInGlyph size={11} /> : <Mail size={11} />} {isLi ? "LinkedIn" : "Email"}
    </span>
  );
}

function KindChip({ kind }) {
  const isFollow = kind === "followup";
  return (
    <span style={{ fontSize: 11, fontWeight: 600, color: isFollow ? "#92580B" : "#475569", background: isFollow ? "#FFF7EC" : "#F1F5F9", borderRadius: 999, padding: "2px 8px" }}>
      {isFollow ? "Follow-up" : "Initial"}
    </span>
  );
}

function FailedChip() {
  return (
    <span style={{ fontSize: 11, fontWeight: 700, color: "#B91C1C", background: "#FEF2F2", borderRadius: 999, padding: "2px 8px" }}>Failed</span>
  );
}

function MessageBody({ m }) {
  return (
    <>
      {m.to_email && (
        <div style={{ fontSize: 12, color: "#64748B", marginBottom: 4 }}>
          <span style={{ fontWeight: 600 }}>To:</span> {m.to_email}
        </div>
      )}
      {m.subject && (
        <div style={{ fontSize: 13.5, fontWeight: 600, color: "#1E293B", marginBottom: 4 }}>{m.subject}</div>
      )}
      <div style={{ fontSize: 13, color: "#334155", whiteSpace: "pre-wrap", lineHeight: 1.5, maxHeight: 220, overflowY: "auto" }}>
        {m.body || (m.channel === "linkedin" ? "(LinkedIn message)" : "—")}
      </div>
    </>
  );
}

export default function OutreachThread({
  messages = [],
  loading = false,
  emptyText = "No previously sent messages.",
  collapsible = false,
  forceCollapsed = false,
}) {
  // Which card is expanded (collapsible mode only). Defaults to the most recent
  // message so there's something to read; collapses to none when the parent
  // signals via forceCollapsed (the composer, on "Generate follow-up").
  const [openId, setOpenId] = useState(null);
  useEffect(() => {
    if (!collapsible) return;
    setOpenId(forceCollapsed || !messages.length ? null : messages[messages.length - 1].id);
  }, [collapsible, forceCollapsed, messages]);

  if (loading) {
    return <div style={{ color: "#64748B", fontSize: 13, padding: "10px 2px" }}>Loading sent history…</div>;
  }
  if (!messages.length) {
    return <div style={{ color: "#94A3B8", fontSize: 13, padding: "10px 2px" }}>{emptyText}</div>;
  }

  if (!collapsible) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {messages.map((m) => {
          const failed = m.status && m.status !== "sent";
          return (
            <div key={m.id} style={{ border: "1px solid #E2E8F0", borderRadius: 10, padding: "10px 12px", background: "#FFFFFF" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 6 }}>
                <KindChip kind={m.outreach_kind} />
                <ChannelChip channel={m.channel} />
                {failed && <FailedChip />}
                <span style={{ marginLeft: "auto", fontSize: 12, color: "#64748B" }}>{fmtDate(m.created_at)}</span>
              </div>
              <MessageBody m={m} />
            </div>
          );
        })}
      </div>
    );
  }

  // Collapsible accordion — one card open at a time, "opens from above".
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
      {messages.map((m) => {
        const failed = m.status && m.status !== "sent";
        const open = openId === m.id && !forceCollapsed;
        return (
          <div
            key={m.id}
            style={{
              border: `1px solid ${open ? "#2563EB" : "#E2E8F0"}`,
              borderRadius: 10,
              background: "#FFFFFF",
              overflow: "hidden",
              opacity: forceCollapsed ? 0.6 : 1,
              boxShadow: open ? "0 0 0 2px rgba(37,99,235,.12)" : "none",
              transition: "border-color .15s, box-shadow .15s, opacity .2s",
            }}
          >
            <button
              type="button"
              onClick={() => { if (!forceCollapsed) setOpenId(open ? null : m.id); }}
              aria-expanded={open}
              style={{
                width: "100%", display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap",
                padding: "10px 12px", background: "transparent", border: 0, textAlign: "left",
                fontFamily: "inherit", cursor: forceCollapsed ? "default" : "pointer",
              }}
            >
              <KindChip kind={m.outreach_kind} />
              <ChannelChip channel={m.channel} />
              {failed && <FailedChip />}
              <span style={{ flex: 1, minWidth: 120, fontSize: 13, fontWeight: 600, color: "#1E293B", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {m.subject || (m.channel === "linkedin" ? "(LinkedIn message)" : "—")}
              </span>
              <span style={{ fontSize: 11.5, color: "#94A3B8", whiteSpace: "nowrap" }}>{fmtDate(m.created_at)}</span>
              <ChevronDown size={16} style={{ color: "#64748B", flex: "none", transform: open ? "rotate(180deg)" : "none", transition: "transform .2s" }} />
            </button>
            <div style={{ maxHeight: open ? 420 : 0, overflow: "hidden", transition: "max-height .28s ease" }}>
              <div style={{ padding: "2px 14px 14px", borderTop: "1px solid #E2E8F0" }}>
                <div style={{ marginTop: 10 }}>
                  <MessageBody m={m} />
                </div>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
