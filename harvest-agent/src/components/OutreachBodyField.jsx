import React, { useState } from "react";

/* Shared body field for the outreach modals (EmailComposeModal, LinkedInMessageModal).
 * Adds an Edit / Preview toggle over the plain textarea. In Preview it renders the
 * body the same way the recipient sees it: any http(s) URL (the company-overview
 * deck link) becomes a blue link, and the reach-out email address becomes a bold
 * blue mailto link — mirroring the backend's _outreach_body_to_html so the modal
 * and the delivered email agree on exactly what is a link. Editing is unchanged:
 * click Edit to get the original textarea back.
 *
 * Links are built as React nodes (never dangerouslySetInnerHTML): the <a> elements
 * are constructed here and every other run of text is rendered as a plain React
 * string, so there is no HTML-injection surface. */

const LINK_COLOR = "#5f7fd0"; // matches the sent-email link color (email_service.py)
const LINK_STYLE = { color: LINK_COLOR };
// The job-title link is bold, mirroring the backend's title anchor
// (email_service.py: font-weight:700). Same blue as every other link.
const JOB_TITLE_LINK_STYLE = { color: LINK_COLOR, fontWeight: 700 };

// Same two patterns the backend uses (app/services/email_service.py) so the
// preview and the sent HTML linkify identically. URL first in the combined
// tokenizer so a URL is never partially matched as an email.
const URL_TOKEN = "https?://[^\\s<>\"]+";
const EMAIL_TOKEN = "[\\w.+-]+@[\\w-]+\\.[\\w.-]+";
const TOKEN_RE = new RegExp(`(${URL_TOKEN})|(${EMAIL_TOKEN})`, "g");
const HAS_EMAIL_RE = new RegExp(EMAIL_TOKEN);

// Turn one line into React nodes, wrapping URLs / emails in blue anchors and
// leaving all other text as plain strings.
function renderLine(line, key) {
  const nodes = [];
  let last = 0;
  let i = 0;
  TOKEN_RE.lastIndex = 0;
  let m;
  while ((m = TOKEN_RE.exec(line)) !== null) {
    if (m.index > last) nodes.push(line.slice(last, m.index));
    const token = m[0];
    if (m[1]) {
      nodes.push(
        <a key={`${key}-${i}`} href={token} target="_blank" rel="noreferrer" style={LINK_STYLE}>{token}</a>
      );
    } else {
      nodes.push(
        <a key={`${key}-${i}`} href={`mailto:${token}`} style={LINK_STYLE}>{token}</a>
      );
    }
    last = m.index + token.length;
    i += 1;
  }
  if (last < line.length) nodes.push(line.slice(last));
  return nodes;
}

/** Render outreach body text as an array of React nodes with blue deck/mailto
 * links. The reach-out line (any line containing an email) is bolded, matching
 * the <strong> the backend wraps that line in. Newlines are preserved literally
 * and shown via `white-space: pre-wrap` on the container.
 *
 * When `jobTitle`/`jobUrl` are given, the first verbatim occurrence of the job
 * title (the opening's role mention) is wrapped in a bold blue link to the posting
 * that opens in a new tab — the raw URL stays hidden — mirroring the backend's
 * _outreach_body_to_html so the preview matches the delivered email exactly. */
export function linkifyOutreachBody(text, jobTitle = "", jobUrl = "") {
  const title = String(jobTitle || "").trim();
  const url = String(jobUrl || "").trim();
  const linkTitle = !!(title && url);
  let titleLinked = false;
  const lines = String(text || "").split("\n");
  const out = [];
  lines.forEach((line, idx) => {
    if (idx > 0) out.push("\n");
    // Case-sensitive, first-occurrence match — same as the backend's `title in line`.
    const at = linkTitle && !titleLinked ? line.indexOf(title) : -1;
    let lineNodes;
    if (at !== -1) {
      titleLinked = true;
      lineNodes = [
        ...renderLine(line.slice(0, at), `${idx}a`),
        <a key={`title-${idx}`} href={url} target="_blank" rel="noreferrer" style={JOB_TITLE_LINK_STYLE}>{title}</a>,
        ...renderLine(line.slice(at + title.length), `${idx}b`),
      ];
    } else {
      lineNodes = renderLine(line, idx);
    }
    out.push(
      HAS_EMAIL_RE.test(line)
        ? <strong key={`ln-${idx}`}>{lineNodes}</strong>
        : <React.Fragment key={`ln-${idx}`}>{lineNodes}</React.Fragment>
    );
  });
  return out;
}

export default function OutreachBodyField({
  value = "",
  onChange = () => {},
  disabled = false,
  placeholder = "",
  textareaClassName = "",
  minHeight = 240,
  jobTitle = "",
  jobUrl = "",
}) {
  const [mode, setMode] = useState("preview");

  const tab = (active) => ({
    border: "none",
    background: active ? "#EFF6FF" : "transparent",
    color: active ? "#1E40AF" : "#94A3B8",
    fontSize: 12,
    fontWeight: 600,
    fontFamily: "inherit",
    padding: "3px 9px",
    borderRadius: 6,
    cursor: "pointer",
  });

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 4, marginBottom: 6 }}>
        <button type="button" style={tab(mode === "preview")} onClick={() => setMode("preview")}>Preview</button>
        <button type="button" style={tab(mode === "edit")} onClick={() => setMode("edit")}>Edit</button>
      </div>

      {mode === "preview" ? (
        <div
          className={textareaClassName}
          style={{ minHeight, whiteSpace: "pre-wrap", overflowWrap: "anywhere", overflowY: "auto", cursor: "text" }}
        >
          {value
            ? linkifyOutreachBody(value, jobTitle, jobUrl)
            : <span style={{ color: "#94A3B8" }}>{placeholder}</span>}
        </div>
      ) : (
        <textarea
          className={textareaClassName}
          style={{ minHeight }}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          placeholder={placeholder}
        />
      )}
    </div>
  );
}
