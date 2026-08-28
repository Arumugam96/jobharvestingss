import React, { useState } from "react";
import DOMPurify from "dompurify";
import EmailComposeModal from "./components/EmailComposeModal";
import LinkedInMessageModal from "./components/LinkedInMessageModal";
import {
  ArrowLeft,
  MapPin,
  Briefcase,
  Banknote,
  Calendar,
  ExternalLink,
  Building2,
  User,
  Eye,
  FileText,
  Link2,
  UserCircle,
  Mail,
  Filter,
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

function ContactAction({ glyph: Glyph, href, onClick, title, variant, newTab = true }) {
  // A click handler (opens the LLM composer) takes precedence over a plain href
  // (mailto / profile link fallback); with neither, the action is disabled.
  if (onClick) {
    return (
      <button
        type="button"
        className={`ha-cbtn ha-cbtn-on-${variant}`}
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

function JobDetailsView({ job = {}, onBack = () => {}, onEdit }) {
  // Same LLM outreach composers the jobs-table rows use. They need the DB-backed
  // job.id (carried through by mapJobToDetail) plus job.email / job.company.
  const [emailModalJob, setEmailModalJob] = useState(null);
  const [linkedinModalJob, setLinkedinModalJob] = useState(null);

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
                <span className="ha-val">{job.posterContact?.email || <span className="ha-muted">—</span>}</span>
              </div>
              <div className="ha-field">
                <span className="ha-key">Mobile</span>
                <span className="ha-val">{job.posterContact?.mobile || <span className="ha-muted">—</span>}</span>
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
                title="Email"
                newTab={false}
                onClick={job.id && job.email ? () => setEmailModalJob(job) : undefined}
                href={!job.id && job.email ? `mailto:${job.email}` : null}
              />
              <ContactAction
                glyph={LinkedInIcon}
                variant="li"
                title="LinkedIn"
                onClick={job.id && job.linkedin ? () => setLinkedinModalJob(job) : undefined}
                href={!job.id && job.linkedin ? job.linkedin : null}
              />
            </div>
          </div>
        </section>

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

      {emailModalJob && <EmailComposeModal job={emailModalJob} onClose={() => setEmailModalJob(null)} />}
      {linkedinModalJob && <LinkedInMessageModal job={linkedinModalJob} onClose={() => setLinkedinModalJob(null)} />}
    </div>
  );
}

const styles = `
  .ha-page { min-height: 100vh; width: 100%; background: #F8FAFC; color: #1E293B;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  .ha-container { max-width: 1280px; margin: 0 auto; padding: 22px 32px 48px; }

  .ha-topbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; }
  .ha-back { display: inline-flex; align-items: center; gap: 7px; cursor: pointer;
    background: #fff; border: 1px solid #E2E8F0; color: #334155;
    font-size: 13px; font-weight: 600; padding: 8px 14px; border-radius: 8px; }
  .ha-back:hover { background: #F1F5F9; border-color: #CBD5E1; }
  .ha-back:focus-visible { outline: 2px solid #2563EB; outline-offset: 1px; }
  .ha-edit-btn { display: inline-flex; align-items: center; gap: 7px; cursor: pointer;
    background: #2563EB; border: none; color: #fff;
    font-size: 13px; font-weight: 600; padding: 8px 16px; border-radius: 8px; }
  .ha-edit-btn:hover { background: #1E40AF; }
  .ha-viewbadge { display: inline-flex; align-items: center; gap: 5px;
    font-size: 12px; font-weight: 600; color: #64748B; background: #fff;
    border: 1px solid #E2E8F0; padding: 7px 12px; border-radius: 8px; }

  .ha-hero { background: #fff; border: 1px solid #E2E8F0; border-radius: 14px;
    padding: 24px 28px; margin-bottom: 18px; }
  .ha-hero-meta { display: flex; align-items: center; gap: 14px; margin-bottom: 12px; }
  .ha-source { font-size: 11px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
    color: #2563EB; background: #EFF4FF; border: 1px solid #DBE6FF;
    padding: 4px 10px; border-radius: 6px; }
  .ha-hero-company { display: inline-flex; align-items: center; gap: 6px;
    color: #475569; font-size: 14px; font-weight: 500; }
  .ha-title { font-size: 26px; font-weight: 700; line-height: 1.25; margin: 0 0 18px; }

  .ha-chips { display: flex; flex-wrap: wrap; gap: 9px; }
  .ha-chip { display: inline-flex; align-items: center; gap: 6px;
    font-size: 13px; color: #334155; background: #F1F5F9;
    border: 1px solid #E2E8F0; padding: 7px 13px; border-radius: 999px; }
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

  .ha-section { background: #fff; border: 1px solid #E2E8F0; border-radius: 14px; padding: 22px; }
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
  .ha-jd-html > *:first-child { margin-top: 0; }
  .ha-jd-html > *:last-child { margin-bottom: 0; }

  .ha-rows { display: flex; flex-direction: column; gap: 11px; }
  .ha-row { display: grid; grid-template-columns: 92px 1fr; gap: 12px; align-items: start; }
  .ha-key { font-size: 13px; color: #64748B; }
  .ha-val { font-size: 13.5px; color: #1E293B; word-break: break-word; }
  .ha-muted { color: #94A3B8; }

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
  .ha-cbtn-on-wa { background: #fff; border-color: #C8EFD7; color: #1A7F4B; cursor: pointer; }
  .ha-cbtn-on-wa:hover { background: #ECFBF2; }
  .ha-cbtn-on-mail { background: #fff; border-color: #93C5FD; color: #2563EB; cursor: pointer; }
  .ha-cbtn-on-mail:hover { background: #EFF4FF; }
  .ha-cbtn-on-li { background: #0A66C2; border-color: #0A66C2; color: #fff; cursor: pointer; }
  .ha-cbtn-on-li:hover { background: #084d92; }

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
`;

export default JobDetailsView;
