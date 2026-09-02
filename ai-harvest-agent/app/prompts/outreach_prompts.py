"""Prompts + templates for recruiter outreach generation
(app/services/outreach_service.py).

Two audiences, each with a canonical reference message supplied by the business:
  * active client  — a company already on ss_active_clients.json
  * new client     — everyone else

The reference messages are the dynamic fallback: if LLM generation fails, the
reference is rendered directly with the company name interpolated
(render_email_fallback). They are NOT fed to the LLM — the system prompt already
dictates the email's structure, length, and capability selection, and the full
reference (two long service lists) would fight those rules. The user prompt sends
only a concise per-audience positioning note (_AUDIENCE) describing how the
message should differ for an active client vs. a new prospect.

Everything is plain text (the email is sent text-only with the corporate-
overview pptx attached in the form Google drive link), bodies contain no markdown/HTML.
"""
from __future__ import annotations

import re

# ── Tone steering ────────────────────────────────────────────────────────────

TONES = ("Formal", "Friendly", "Direct")

TONE_INSTRUCTIONS = {
    "Formal": (
        "Professional and courteous. Full sentences, respectful salutation and "
        "sign-off, no slang or contractions."
    ),
    "Friendly": (
        "Warm and personable while still professional. Approachable phrasing and "
        "light contractions are fine; keep it genuine, not casual to a fault."
    ),
    "Direct": (
        "Crisp and to the point. Short sentences, lead with the ask, minimal "
        "preamble. Still polite, just economical."
    ),
}

# ── Reference messages (business-supplied; {company} is interpolated) ─────────

ACTIVE_CLIENT_REFERENCE = """\
Greetings from Sightspectrum!

I came across your LinkedIn post — you are looking for various professionals with different technical skills.

Sightspectrum is an empaneled vendor partner with {company}, and we have completed a few onboards recently across other verticals.

I request that you share your priority requirements with us, and we will support you with good profiles. Kindly share your contact details so we can discuss this further."""

NEW_CLIENT_REFERENCE = """\
I came across your LinkedIn post — you are looking for vendor support for your hiring needs, and Sightspectrum can support your requirements.

Sightspectrum Technology specializes in IT staffing services, supporting clients across Contract, C2H, and Permanent hiring models, with a strong focus on Data Analytics, Cloud, and Digital technologies.

We understand that {company} manages staffing programs for leading global clients, and we would like to explore the opportunity to partner with you as a vendor/sub-vendor for your ongoing and upcoming requirements.

Our key strengths include:
- Strong talent pool in Data Engineering, AI/ML, Cloud, and Full-stack technologies
- Quick turnaround time with quality-screened profiles
- Experience supporting enterprise clients and GCC environments
- Dedicated recruitment team aligned to client-specific needs

Our key staffing services include:
- Panel Support: L1 technical evaluation assistance based on client specifications.
- Implant Model: Free-of-cost onsite support for clients onboarding 10+ candidates per month.
- Train & Deploy: Cross-training experienced professionals on niche technologies to meet evolving project demands.
- Payroll Processing Services.

We would appreciate the opportunity to connect and understand your vendor onboarding process. Please let us know a convenient time to discuss this further.
"""

# ── Per-audience positioning (LLM prompt) ─────────────────────────────────────
# What the system prompt can't know per request: how to position Sightspectrum
# for an existing client vs. a new prospect. The full reference messages above
# are NOT sent to the LLM (they'd fight the system prompt's "pick 1-2
# capabilities, ~70-100 words" rules); these one-liners carry only the
# positioning difference, which is all the model needs on top of the catalog.
_AUDIENCE = {
    "active": (
        "This recipient's company is an existing Sightspectrum client — "
        "Sightspectrum is already an empaneled vendor partner with them and has "
        "completed onboards recently. Acknowledge that partnership and invite them "
        "to share their priority requirements."
    ),
    "new": (
        "This recipient's company is a new prospect (not yet a client). Introduce "
        "Sightspectrum as a potential staffing partner and express interest in "
        "their vendor onboarding process for current and upcoming requirements."
    ),
    "unknown": (
        "The recipient's company could not be identified — keep it generic. "
        "Introduce Sightspectrum as a staffing partner and invite a conversation "
        "about hiring needs."
    ),
}

# For "unknown" companies (nothing to personalize) — use the new-client message
# with a generic stand-in so no "{company}" leaks into the text.
_GENERIC_COMPANY = "your organization"

_FALLBACK_SUBJECTS = {
    "active": "Sightspectrum — Vendor Partnership Support",
    "new": "Sightspectrum — IT Staffing Partnership",
    "unknown": "Sightspectrum — IT Staffing Partnership",
}

# ── Deterministic closing (reach-out CTA + sign-off) ────────────────────────
SIGN_OFF = "Best Regards,\nSightspectrum Team"
REACHOUT_TEMPLATE = "Please feel free to reach me directly at {email}."
# Static contact line appended to every outreach email, right below the reach-out
# line (business-supplied; same on the LLM and fallback paths).
CONTACT_BLOCK = "Contact : Vishnu Hassan/96290 82587"
DECK_LINK_TEMPLATE = "Visit our WebSite : {url}"

_SIGNOFF_MARKERS = (
    "best regards", "warm regards", "kind regards", "best wishes", "best,",
    "regards", "sincerely", "cheers", "thanks", "thank you", "sightspectrum team",
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _strip_trailing_closing(pitch: str) -> str:
    """Remove any trailing sign-off / contact lines the model may have added
    despite instructions, so the deterministic closing we append isn't
    duplicated. Only strips a trailing block (sign-off phrases, lines containing
    an email address, or blanks) — body content before it is left untouched."""
    lines = (pitch or "").rstrip().splitlines()
    i = len(lines) - 1
    while i >= 0:
        s = lines[i].strip().lower()
        if s == "" or any(s.startswith(m) for m in _SIGNOFF_MARKERS) or _EMAIL_RE.search(lines[i]):
            i -= 1
            continue
        break
    return "\n".join(lines[: i + 1]).rstrip()


# Salutations the model may open with — used to force the greeting onto its own
# line. Matches a leading "Hi/Hello/Hey/Dear/Greetings … ," (up to the comma).
_GREETING_RE = re.compile(r"^\s*((?:hi|hello|hey|dear|greetings)\b[^\n,]*,)[ \t]*", re.IGNORECASE)


def _greeting_on_own_line(body: str) -> str:
    """Put the opening greeting on its own line, with a blank line before the body:

        Hi Nivetha,

        Sightspectrum specializes …

    The model often runs the greeting straight into the first sentence
    ("Hi Nivetha, Sightspectrum …"); this splits it deterministically so every
    email reads the same way regardless of how the model formatted it. Left
    untouched when there's no recognizable greeting or it's already on its own line."""
    text = (body or "").lstrip()
    m = _GREETING_RE.match(text)
    if not m:
        return body
    greeting = m.group(1).strip()
    rest = text[m.end():].lstrip()
    return f"{greeting}\n\n{rest}" if rest else greeting


def _build_sign_off(sender_email: str) -> str:
    """Sign-off personalized to the sender when their name can be recovered from
    the email ("Best Regards,\\n<Name>\\nSightspectrum"), mirroring the opening
    introduction; otherwise the neutral team sign-off."""
    name = sender_display_name(sender_email)
    if name:
        return f"\nBest Regards,\n{name}\nSightspectrum"
    return SIGN_OFF


def append_closing(pitch: str, sender_email: str, deck_url: str = "") -> str:
    """Append the reach-out line (to sender_email), then the deck link (when
    deck_url is set), then the Sightspectrum sign-off to an LLM- or
    template-generated pitch — the deck link sits just before the sign-off. The
    sign-off is signed with the sender's name when it can be derived from
    sender_email (see _build_sign_off). The email/URL are left as plain text
    here; they become a bold clickable mailto link and a clickable deck link when
    the email is sent as HTML."""
    body = _greeting_on_own_line(_strip_trailing_closing(pitch))
    email = (sender_email or "").strip()
    url = (deck_url or "").strip()
    parts = [body] if body else []
    if email:
        parts.append(REACHOUT_TEMPLATE.format(email=email))
    parts.append(CONTACT_BLOCK)  # static contact line, directly under the reach-out line
    if url:
        parts.append(DECK_LINK_TEMPLATE.format(url=url))
    parts.append(_build_sign_off(sender_email))
    return "\n".join(p for p in parts if p).strip()


EMAIL_SYSTEM_PROMPT = (
    "You are a business-development specialist at Sightspectrum, an IT staffing "
    "and technology services company. You write short, professional B2B outreach "
    "emails to recruiters, talent-acquisition contacts, hiring managers, and other "
    "decision-makers who have posted a job, offering Sightspectrum's staffing support. "
    "Write like a real person making a warm, understated, consultative introduction — "
    "never a hyped sales blast. "

    "SIGHTSPECTRUM SERVICE AND CAPABILITY CATALOG: "
    "Use this catalog only to identify capabilities that are genuinely relevant to "
    "the job posting. Do NOT mention the entire catalog in an email. Select ONLY "
    "one or two capabilities that have a clear connection to the role. "

    "Core Staffing & Talent Services: "
    "IT staffing, contract staffing, permanent staffing, contract-to-hire staffing, "
    "technical talent sourcing, specialized technology talent, Hire-Train-Deploy. "

    "Data & Analytics: "
    "Data Engineering, data integration, data pipelines, ETL/ELT, data warehousing, "
    "data modeling, data streaming, data governance, data quality, business "
    "intelligence, data visualization, advanced analytics, analytics consulting. "

    "Cloud & Infrastructure: "
    "Cloud consulting, cloud adoption, cloud migration, cloud engineering, "
    "cloud infrastructure, cloud architecture, application migration, "
    "cloud security, cloud optimization and monitoring, AWS, Azure, GCP. "

    "AI & Emerging Technology: "
    "Artificial Intelligence, Generative AI, Machine Learning, AI integration, "
    "AI/ML engineering, intelligent automation, advanced analytics. "

    "Software & Application Development: "
    "Web application development, mobile application development, frontend "
    "development, backend development, full-stack development, software product "
    "development, enterprise application integration, application modernization, "
    "application maintenance and support. "

    "Consulting & Professional Services: "
    "Technology consulting, IT consulting, digital transformation, technology "
    "strategy, product professional services, application consulting, "
    "application integration, product development and product maintenance. "

    "Training & Workforce Development: "
    "Hire-Train-Deploy, custom technical training, cross-skilling, upskilling, "
    "on-demand skill training, certification preparation, IT leadership development. "

    "IMPORTANT SERVICE-SELECTION RULES: "
    "First identify the primary technical focus of the job posting. Then select "
    "the one or two Sightspectrum capabilities that most directly align with it. "
    "For example, a Data Engineer role should map to Data Engineering and possibly "
    "Cloud; an AI Engineer role should map to AI/ML and Generative AI; a Cloud "
    "Architect role should map to Cloud Engineering and Cloud Architecture; a "
    "Cybersecurity role should map to Cybersecurity and Cloud Security; a Software "
    "Engineer role should map to Software/Application Development. "
    "For an IT Manager or infrastructure leadership role, prioritize IT Staffing, "
    "Cloud/Infrastructure, Cybersecurity, or technical talent depending on the "
    "actual requirements in the posting. "
    "Never force a capability into the email simply because it exists in the "
    "catalog. If the job does not clearly match a specialized capability, use the "
    "broader staffing/talent capability. "
    "You MAY name the specific skills or technologies required by the job posting as "
    "the expertise of the contract/permanent candidates Sightspectrum can provide — "
    "Sightspectrum sources talent across technologies. Do NOT invent skills, "
    "technologies, certifications, or services that are not present in the job posting "
    "or the catalog. "

    "EMAIL STRUCTURE: "
    "Structure the body as a greeting line followed by exactly three short paragraphs: "
    "(1) one sentence introducing the sender by name, noting that they came across "
    "the recipient's posting on the source platform named in the context (e.g. "
    "LinkedIn, Naukri, or Dice), and referring to the role by its EXACT job title "
    "written verbatim as given in the context. Use the sender name given in the "
    "context; if none is given, introduce simply as writing from Sightspectrum. If no "
    "source is given, say you came across the posting without naming a platform. "
    "(2) Briefly and naturally explain how Sightspectrum can help with the hiring need." 
    "Mention that we can provide relevant qualified talent, including contract and/or "
    "permanent candidates where appropriate, and reference a few of the most relevant skills"
    " or technologies from the job posting to show alignment. Keep the focus on helping the "
    "customer find suitable talent efficiently and move the hiring process forward"
    "(3) one brief, low-pressure closing sentence inviting a conversation about the "
    "recipient's current or upcoming hiring needs. "

    "PERSONALIZATION RULES: "
    "Personalize naturally to the recipient's company and the specific role they posted. "
    "Reference the role title and, where useful, one important skill, technology, or "
    "responsibility from the job posting to establish relevance. Do not restate, "
    "summarize, or describe the job description. "
    "Do not invent company information, technologies, requirements, hiring volumes, "
    "client relationships, statistics, certifications, names, phone numbers, or email "
    "addresses. Base the message only on the provided reference text and the "
    "Sightspectrum capability catalog. "

    "TONE AND LENGTH: "
    "Keep the whole body roughly 70-100 words. Be concise, professional, confident, "
    "and consultative. Avoid generic sales language such as 'industry-leading', "
    "'best-in-class', 'unparalleled', 'revolutionary', 'deep pool of talent', "
    "'cutting-edge', or similar hype. Do not sound like a mass-generated sales email. "
    "Do not over-explain Sightspectrum. The purpose is to establish relevance and "
    "start a conversation, not to sell every service. "

    "GREETING RULES: "
    "Address the recipient by their first name when a recipient name is given "
    "(e.g. 'Dear Jane,' for a formal tone or 'Hi Jane,' for a warmer tone). "
    "When no recipient name is given, use a neutral professional greeting such as "
    "'Hello,'. Put the greeting on its OWN line, followed by a blank line, then the "
    "three paragraphs. "

    "PLACEHOLDER RULES: "
    "NEVER output a bracketed placeholder such as '[Recipient Name]', '[Name]', "
    "'[Company]', or '[Your Name]'. If a detail is unknown, omit it or rephrase "
    "neutrally. "

    "CONTACT AND SIGN-OFF RULES: "
    "Write ONLY the greeting and the three body paragraphs. Do NOT add a closing "
    "sign-off such as 'Best regards', 'Regards', or the sender's name. Do NOT add "
    "a phone number, email address, website, or separate contact line. The sign-off "
    "and contact information are appended automatically by the application. "

    "OUTPUT RULES: "
    "Return ONLY valid JSON in exactly this form: "
    "{\"subject\": \"...\", \"body\": \"...\"}. "
    "Do not include commentary, explanations, markdown, HTML, or code fences."
    )

LINKEDIN_SYSTEM_PROMPT = (
    "You are a business-development specialist at Sightspectrum, an IT staffing "
    "firm. You write a short LinkedIn outreach message (a connection/InMail note) "
    "to a recruiter who posted a job, offering Sightspectrum's staffing support. "
    "Rules: keep it under 100 words, ideally under 500 characters; plain text "
    "only; friendly and professional; do not invent facts or contact details; "
    "personalize to the company and role. Greet the recipient by their first "
    'name when a recipient name is given (e.g. "Hi Jane,"); otherwise use a '
    'neutral greeting such as "Hi there,". NEVER output a bracketed placeholder '
    'such as "[Recipient Name]", "[Name]", or "[Company]" — if a detail is '
    "unknown, omit it or rephrase neutrally. Return ONLY the message text — no "
    "subject line, no JSON, no code fences, no commentary."
)


# ── Sender identity ──────────────────────────────────────────────────────────

# Role/shared-mailbox local parts that are not personal names — a first token
# matching one of these yields no personal introduction (better a plain
# "from Sightspectrum" than "This is Bd …").
_NON_NAME_TOKENS = frozenset({
    "bd", "hr", "info", "sales", "admin", "contact", "support", "team", "noreply",
    "careers", "jobs", "recruiting", "recruitment", "talent", "hello", "office",
    "mail", "email", "no", "reply", "help", "service", "services", "hi",
})


def sender_display_name(sender_email: str) -> str:
    """Best-effort human first name for the sender, derived from their email
    local part, so the email can open with a personal introduction ("This is
    Ravi …"). "ravi.kumar@sightspectrum.com" → "Ravi"; drops a trailing company
    token ("hari.sightspectrum@gmail.com" → "Hari"). Returns "" when nothing
    name-like can be recovered (a role mailbox like "bd-team@…", or too short),
    leaving the model to open without a personal name."""
    local = (sender_email or "").split("@", 1)[0].strip()
    if not local:
        return ""
    # Split on the usual separators; the first token is the given name.
    first = re.split(r"[.\-_+]", local)[0].strip().lower()
    if not first or not first.isalpha() or len(first) < 2 or first in _NON_NAME_TOKENS:
        return ""
    return first.capitalize()


# ── Job-context block shared by both builders ────────────────────────────────

def _job_context(job: dict) -> str:
    company = (job.get("company") or "").strip() or "the company"
    poster = (job.get("job_poster_name") or "").strip()
    jd = (job.get("job_description") or "").strip()
    if len(jd) > 1500:  # keep the prompt bounded; the opening is the useful part
        jd = jd[:1500] + " …"
    lines = [
        f"Company: {company}",
    ]
    if poster:
        first_name = poster.split()[0]
        lines.append(
            f"Recipient name: {poster} — open the message by greeting them by "
            f'first name ("{first_name}")'
        )
    else:
        lines.append(
            "Recipient name: unknown — use a neutral greeting (e.g. \"Hello,\"); "
            "do NOT invent a name and do NOT leave a bracketed placeholder"
        )
    if jd:
        lines.append(f"Job description (for context, do not quote verbatim):\n{jd}")
    return "\n".join(lines)


# ── Email ────────────────────────────────────────────────────────────────────

def build_email_prompt(client_type: str, tone: str, job: dict, sender_email: str = "") -> str:
    """User prompt for email generation. It supplies only the per-request
    variables — `client_type` picks the audience positioning, `tone` the style,
    `job` the personalization context, and `sender_email` lets the opening
    introduce the sender by name. The email's structure, length, capability
    selection, greeting, sign-off, and output rules live in EMAIL_SYSTEM_PROMPT
    and are deliberately not restated here."""
    tone_instr = TONE_INSTRUCTIONS.get(tone, TONE_INSTRUCTIONS["Formal"])
    audience = _AUDIENCE.get(client_type, _AUDIENCE["new"])
    sender_name = sender_display_name(sender_email)
    sender_ctx = (
        f'Sender name: {sender_name} — open by introducing the sender in the '
        f'first sentence (e.g. "This is {sender_name} from Sightspectrum …").\n\n'
        if sender_name
        else "Sender name: unknown — introduce simply as writing from "
        "Sightspectrum, with no personal name and no placeholder.\n\n"
    )
    source = (job.get("source") or "").strip()
    source_ctx = (
        f"Job posting source: {source} — in the opening sentence, mention that you "
        f"came across the posting on {source}.\n\n"
        if source
        else "Job posting source: unknown — say you came across the posting without "
        "naming a platform.\n\n"
    )
    return (
        f"Audience — {audience}\n\n"
        f"Tone: {tone} — {tone_instr}\n\n"
        f"{sender_ctx}"
        f"{source_ctx}"
        f"Recipient / role context:\n{_job_context(job)}\n\n"
        f"Write the outreach email BODY only, following the structure and rules in "
        f"the system instructions. "
        f'Return ONLY JSON: {{"subject": "...", "body": "..."}}'
    )


def render_email_fallback(
    client_type: str, company: str, sender_email: str = "", deck_url: str = ""
) -> tuple[str, str]:
    """Static fallback used when LLM generation fails — the reference message
    with the company interpolated, plus a default subject. `client_type` chooses
    the template; "active" requires a real company (classify_client only returns
    "active" when one matched). The deck link + reach-out line + sign-off are
    appended the same way as the LLM path so fallback emails carry the same closing."""
    name = (company or "").strip()
    if client_type == "active":
        body = ACTIVE_CLIENT_REFERENCE.format(company=name or _GENERIC_COMPANY)
        subject = f"{_FALLBACK_SUBJECTS['active']}" + (f" — {name}" if name else "")
    else:
        body = NEW_CLIENT_REFERENCE.format(company=name or _GENERIC_COMPANY)
        subject = f"{_FALLBACK_SUBJECTS.get(client_type, _FALLBACK_SUBJECTS['new'])}" + (
            f" — {name}" if name else ""
        )
    return subject, append_closing(body, sender_email, deck_url)


# ── LinkedIn ─────────────────────────────────────────────────────────────────

def build_linkedin_prompt(job: dict) -> str:
    return (
        "Write a short LinkedIn outreach message to this recruiter offering "
        "Sightspectrum's IT staffing support for the role they posted.\n\n"
        f"{_job_context(job)}\n\n"
        "Return ONLY the message text."
    )


def render_linkedin_fallback(company: str) -> str:
    name = (company or "").strip() or _GENERIC_COMPANY
    return (
        f"Hi, I came across your job post at {name}. Sightspectrum is an IT "
        "staffing firm supporting Contract, C2H, and Permanent hiring across Data "
        "Analytics, Cloud, and Digital technologies. We'd love to support your "
        "open requirements with quality-screened profiles. Could we connect to "
        "discuss your vendor onboarding process? — Sightspectrum Team"
    )
