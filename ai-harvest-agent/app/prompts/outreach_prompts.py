"""Prompts + templates for recruiter outreach generation
(app/services/outreach_service.py).

Two audiences, each with a canonical reference message supplied by the business:
  * active client  — a company already on ss_active_clients.json
  * new client     — everyone else

The reference messages serve double duty:
  1. LLM context — the model rewrites the reference in the requested tone,
     personalized to the job/company, and returns JSON {"subject","body"}.
  2. Dynamic fallback — if LLM generation fails, the reference is rendered
     directly with the company name interpolated (render_email_fallback).

Everything is plain text (the email is sent text-only with the corporate-
overview pptx attached), so bodies contain no markdown/HTML.
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

Looking forward to your response."""

# For "unknown" companies (nothing to personalize) — use the new-client message
# with a generic stand-in so no "{company}" leaks into the text.
_GENERIC_COMPANY = "your organization"

_FALLBACK_SUBJECTS = {
    "active": "Sightspectrum — Vendor Partnership Support",
    "new": "Sightspectrum — IT Staffing Partnership",
    "unknown": "Sightspectrum — IT Staffing Partnership",
}

# ── Deterministic closing (reach-out CTA + sign-off) ─────────────────────────
# The LLM writes only the pitch; we append these ourselves so the contact detail
# is always correct and the signature is consistent. Rendered as a bold, clickable
# mailto link in the outgoing HTML email (see email_service._outreach_body_to_html).

SIGN_OFF = "Best Regards,\nSightspectrum Team"
REACHOUT_TEMPLATE = "Please feel free to reach me directly at {email}."
# Appended only when a hosted deck URL is configured (OUTREACH_DECK_URL). Replaces
# the old file attachment — rendered as a clickable link in the HTML email.
DECK_LINK_TEMPLATE = "You can view our company overview here: {url}"

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
        return f"Best Regards,\n{name}\nSightspectrum"
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
    if url:
        parts.append(DECK_LINK_TEMPLATE.format(url=url))
    parts.append(_build_sign_off(sender_email))
    return "\n\n".join(p for p in parts if p).strip()


EMAIL_SYSTEM_PROMPT = (
    "You are a business-development specialist at Sightspectrum, an IT staffing "
    "firm. You write short, professional B2B outreach emails to recruiters / "
    "talent-acquisition contacts who have posted a job, offering Sightspectrum's "
    "staffing support. Write like a real person making a warm, understated, "
    "consultative introduction — never a hyped sales blast. "
    "Structure the body as a greeting line followed by three short paragraphs: "
    "(1) a one-sentence introduction in which the SENDER introduces themselves by "
    "name and notes they came across the recipient's posting for the role (e.g. "
    "\"I'm Ravi from Sightspectrum, and I came across your posting for a Senior "
    "Data Engineer role.\"); use the sender name given in the context, or if none "
    "is given introduce simply as writing from Sightspectrum. "
    "(2) two sentences that reference the role's focus and position Sightspectrum "
    "as a staffing partner: name ONLY the one or two capabilities/skills that "
    "directly match this role (e.g. Data Engineering and Cloud for a data role) "
    "and mention that you can support contract and/or permanent hiring — do NOT "
    "list every service Sightspectrum provides. "
    "(3) a brief, low-pressure closing line inviting a conversation about the "
    "recipient's current or upcoming hiring needs. "
    "Keep the whole body to roughly 70-100 words; no filler, no hype, and do NOT "
    "restate or summarize the full job description (you MAY reference the role's "
    "focus or a key skill to show relevance). Plain text only (no markdown, no "
    "HTML); do not invent facts, statistics, names, phone numbers, or email "
    "addresses; base the message on the provided reference text, preserving its "
    "intent and offer; personalize naturally to the recipient's company and the "
    "role they posted. Address the recipient by their first name when a recipient "
    'name is given (e.g. "Dear Jane," for a formal tone or "Hi Jane," for a '
    'warmer one); when no recipient name is given, use a neutral professional '
    'greeting such as "Hello,". Put the greeting on its OWN line, then a blank '
    'line, then the three paragraphs (e.g. "Hi Jane,\\n\\nI\'m Ravi from '
    'Sightspectrum, …"). '
    'NEVER output a bracketed placeholder such as "[Recipient Name]", "[Name]", '
    '"[Company]", or "[Your Name]" — if a detail is unknown, omit it or rephrase '
    "neutrally. Write ONLY the greeting and the three body paragraphs: do NOT add "
    "a closing sign-off (no 'Best regards', no sender name, no 'Sightspectrum "
    "Team') and do NOT add a reach-out/contact line — the sign-off and contact "
    "line are appended automatically. "
    'Return ONLY valid JSON of the form {"subject": "...", "body": "..."} with '
    "no commentary and no code fences."
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
    """User prompt for email generation. `client_type` picks the reference,
    `tone` picks the style, `job` supplies personalization context, and
    `sender_email` lets the opening introduce the sender by name."""
    company = (job.get("company") or "").strip() or _GENERIC_COMPANY
    reference = (
        ACTIVE_CLIENT_REFERENCE if client_type == "active" else NEW_CLIENT_REFERENCE
    ).format(company=company)
    tone_instr = TONE_INSTRUCTIONS.get(tone, TONE_INSTRUCTIONS["Formal"])
    audience = {
        "active": "This recipient's company is an existing Sightspectrum client.",
        "new": "This recipient's company is a new prospect (not yet a client).",
        "unknown": "The recipient's company could not be identified — keep it generic.",
    }.get(client_type, "")
    sender_name = sender_display_name(sender_email)
    sender_ctx = (
        f'Sender name: {sender_name} — open by introducing the sender in the '
        f'first sentence (e.g. "This is {sender_name} from Sightspectrum …").\n\n'
        if sender_name
        else "Sender name: unknown — introduce simply as writing from "
        "Sightspectrum, with no personal name and no placeholder.\n\n"
    )
    return (
        f"{audience}\n\n"
        f"Tone: {tone} — {tone_instr}\n\n"
        f"{sender_ctx}"
        f"Reference message to adapt (keep its intent and offer):\n"
        f"\"\"\"\n{reference}\n\"\"\"\n\n"
        f"Recipient / role context:\n{_job_context(job)}\n\n"
        f"Write the outreach email BODY only — no sign-off and no reach-out/contact "
        f"line (those are appended automatically). Structure it as a first-name "
        f"greeting line followed by three short paragraphs: (1) a one-sentence "
        f"intro where the sender introduces themselves by name and notes they came "
        f"across the recipient's posting for the role; (2) two sentences that "
        f"reference the role's focus and position Sightspectrum, naming ONLY the "
        f"one or two capabilities that match this job and whether contract and/or "
        f"permanent hiring is supported — not the full service list; (3) a brief, "
        f"low-pressure line inviting a conversation about current or upcoming "
        f"hiring needs. Keep it to ~70-100 words, warm and professional, and do "
        f"not restate the full job description. "
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
