"""Prompts + templates for recruiter outreach generation
(app/services/outreach_service.py).

The outreach email is a short, direct offer: a greeting, three brief lines
(reference the posting → a fixed "3-4 pre-screened contract profiles in 48 hrs,
no fee unless you hire" offer → a low-pressure CTA), then a deterministic closing
(sign-off, optional website link, and a one-line STOP notice). The three middle
lines are LLM-generated but must preserve the fixed offer claims; when LLM
generation fails, render_email_fallback builds the same fixed template
deterministically from the job context.

Two audiences steer only the phrasing (not the fixed claims):
  * active client  — a company already on ss_active_clients.json
  * new client     — everyone else
The user prompt sends a concise per-audience positioning note (_AUDIENCE)
describing how the message should differ for an active client vs. a new prospect.

Everything is plain text (bodies contain no markdown/HTML); email_service renders
the HTML part (job-title link, mailto reach-out, website link) at send time.
"""
from __future__ import annotations

import random
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

# ── Subject taglines (one picked at random per generation) ───────────────────
# All mean the same thing; randomizing keeps subjects from looking mass-generated.
SUBJECT_TAGLINES = (
    "contract profiles ready in 48 hrs",
    "3-4 pre-screened profiles in 48 hrs",
    "contract IT profiles within 48 hours",
    "pre-screened contract profiles, 48 hr turnaround",
    "ready-to-interview profiles in 48 hrs",
    "contract staffing — profiles in 48 hrs",
)

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

# For an "unknown" company (nothing to personalize) — a generic stand-in so no
# "{company}" placeholder leaks into the text.
_GENERIC_COMPANY = "your organization"

# ── Fixed offer + deterministic closing ─────────────────────────────────────
# The core pitch line's business claims. The LLM may lightly rephrase it but must
# keep these claims; the fallback uses this verbatim.
OFFER_LINE = (
    "We staff contract IT roles across India and can share 3-4 pre-screened "
    "profiles within 48 hours. No fee unless you hire."
)
# Low-pressure CTA line (LLM may lightly rephrase; fallback uses verbatim).
CTA_LINE = "Should I send the profiles, or would a 10-minute call be easier?"
# Hardcoded sender title in the sign-off (spelling per business template).
SENDER_TITLE = "HR Recruiter, SightSpectrum"
# One-line opt-out footer, always the last line of the email.
STOP_LINE = "Not hiring right now? Reply STOP and we won't email again."
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
    """Sign-off block:

        Regards,
        <Name>                    (only when derivable from sender_email)
        HR Recruiter, SightSpectrum
        <sender_email>            (only when present)

    The name line is dropped for role mailboxes (see sender_display_name); the
    email line is left as plain text here and becomes a bold clickable mailto link
    when the email is sent as HTML."""
    name = sender_display_name(sender_email)
    email = (sender_email or "").strip()
    lines = ["Regards,"]
    if name:
        lines.append(name)
    lines.append(SENDER_TITLE)
    if email:
        lines.append(email)
    return "\n".join(lines)


def append_closing(pitch: str, sender_email: str, deck_url: str = "") -> str:
    """Append the deterministic closing to an LLM- or template-generated pitch:
    a blank line, the sign-off block (name + HR Recruiter title + sender email),
    the website/deck link when `deck_url` is set, and finally the one-line STOP
    opt-out. Blocks are separated by blank lines. The sender email and website URL
    are left as plain text here; email_service turns them into a bold clickable
    mailto link and a clickable link when the email is sent as HTML."""
    body = _greeting_on_own_line(_strip_trailing_closing(pitch))
    url = (deck_url or "").strip()
    blocks = [body] if body else []
    blocks.append(_build_sign_off(sender_email))
    if url:
        blocks.append(DECK_LINK_TEMPLATE.format(url=url))
    blocks.append(STOP_LINE)
    return "\n\n".join(b for b in blocks if b).strip()


EMAIL_SYSTEM_PROMPT = (
    "You are an IT staffing recruiter at Sightspectrum. You write very short, "
    "direct B2B outreach emails to recruiters, talent-acquisition contacts, and "
    "hiring managers who have posted a job, offering pre-screened contract IT "
    "profiles. Write like a real person sending a quick, confident note — never a "
    "hyped sales blast, never long. "

    "EMAIL STRUCTURE: "
    "Output a greeting line, then EXACTLY three short lines, each as its own "
    "paragraph (a blank line between each). Nothing else. "
    "(1) A one-line reference to the posting in the form 'Saw your post for "
    "<JOB TITLE> at <COMPANY>.' — write the job title using its EXACT text, "
    "verbatim as given in the context (do not paraphrase, shorten, reorder, or "
    "change capitalization), and use the company name from the context. "
    "(2) The offer. Keep the meaning and specific claims of this line: "
    f"'{OFFER_LINE}' — you MAY lightly rephrase the wording, but you MUST preserve "
    "every claim (contract IT roles across India; 3-4 pre-screened profiles; within "
    "48 hours; no fee unless you hire) and keep it to roughly the same length. Do "
    "NOT add extra services, skills, technologies, statistics, or claims. "
    f"(3) A brief, low-pressure closing question, e.g. '{CTA_LINE}' — you may "
    "lightly rephrase it, keeping it to one short sentence. "

    "TONE: "
    "Concise, professional, and human. Apply the requested tone and audience note "
    "from the user prompt only to lightly steer phrasing (warmth, word choice, and — "
    "for an existing client — a brief nod to the ongoing partnership). The tone and "
    "audience must NEVER change or expand the fixed offer claims in line (2). Avoid "
    "generic sales hype ('industry-leading', 'best-in-class', 'cutting-edge', etc.). "

    "GREETING RULES: "
    "Address the recipient by first name when a recipient name is given (e.g. "
    "'Hi Jane,'); when no name is given, use a neutral greeting such as 'Hello,'. "
    "Put the greeting on its OWN line, followed by a blank line, then the three lines. "

    "PLACEHOLDER RULES: "
    "NEVER output a bracketed placeholder such as '[Recipient Name]', '[Name]', "
    "'[Company]', or '[Your Name]'. If a detail is unknown, omit it or rephrase "
    "neutrally. Do not invent company information, names, phone numbers, or email "
    "addresses. "

    "CONTACT AND SIGN-OFF RULES: "
    "Write ONLY the greeting and the three lines. Do NOT add a sign-off such as "
    "'Regards' or the sender's name, and do NOT add a phone number, email address, "
    "website, or opt-out line — the sign-off, website link, and opt-out notice are "
    "appended automatically by the application. "

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

def _job_context(job: dict, include_description: bool = True) -> str:
    company = (job.get("company") or "").strip() or "the company"
    title = (job.get("job_title") or "").strip()
    poster = (job.get("job_poster_name") or "").strip()
    # The outreach email's fixed template references only the company, job title,
    # and recipient — it never quotes the description — so email generation passes
    # include_description=False to save input tokens. The LinkedIn message builder
    # still personalizes on the role, so it keeps the description.
    jd = (job.get("job_description") or "").strip() if include_description else ""
    if len(jd) > 1500:  # keep the prompt bounded; the opening is the useful part
        jd = jd[:1500] + " …"
    lines = [
        f"Company: {company}",
    ]
    if title:
        # The role mention in the opening must be this EXACT string — the sent email
        # links the first verbatim occurrence of it to the posting URL (see
        # email_service._outreach_body_to_html). Paraphrasing/shortening it here is
        # why the job-title link sometimes goes missing, so pin it explicitly.
        lines.append(
            f"Job title (refer to the role using this EXACT text, verbatim — do not "
            f"paraphrase, shorten, reorder, or change capitalization): {title}"
        )
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
    variables — `client_type` picks the audience positioning and `tone` the
    style (both only lightly steer phrasing of the fixed three lines), and `job`
    the personalization context. `sender_email` is unused here (the sign-off is
    appended deterministically by append_closing); it's kept for signature
    compatibility with the caller. The email's structure, fixed offer, greeting,
    sign-off, and output rules live in EMAIL_SYSTEM_PROMPT."""
    tone_instr = TONE_INSTRUCTIONS.get(tone, TONE_INSTRUCTIONS["Formal"])
    audience = _AUDIENCE.get(client_type, _AUDIENCE["new"])
    return (
        f"Audience — {audience}\n\n"
        f"Tone: {tone} — {tone_instr}\n\n"
        f"Recipient / role context:\n{_job_context(job, include_description=False)}\n\n"
        f"Write the outreach email BODY only, following the structure and rules in "
        f"the system instructions. "
        f'Return ONLY JSON: {{"subject": "...", "body": "..."}}'
    )


def build_email_subject(job: dict) -> str:
    """Deterministic subject for the outreach email: the exact job title plus a
    randomly chosen tagline (all taglines mean the same thing, so subjects vary
    across sends). Falls back to a title-less form when no job title is known.
    Used for both the LLM and fallback paths so the subject format is guaranteed."""
    title = (job.get("job_title") or "").strip()
    tagline = random.choice(SUBJECT_TAGLINES)
    return f"{title} — {tagline}" if title else f"Contract IT {tagline}"


def render_email_fallback(
    client_type: str, job: dict, sender_email: str = "", deck_url: str = ""
) -> tuple[str, str]:
    """Deterministic fallback used when LLM generation fails — builds the same
    fixed template (greeting + posting reference + fixed offer + CTA) directly
    from the job context, with the same closing appended as the LLM path. For an
    existing ("active") client the offer line carries a brief partnership nod;
    the fixed claims are unchanged. `client_type` other than "active" is treated
    as a new/unknown prospect."""
    company = (job.get("company") or "").strip() or _GENERIC_COMPANY
    title = (job.get("job_title") or "").strip()
    poster = (job.get("job_poster_name") or "").strip()

    greeting = f"Hi {poster.split()[0]}," if poster else "Hello,"
    reference = (
        f"Saw your post for {title} at {company}."
        if title
        else f"Saw your hiring post at {company}."
    )
    offer = (
        f"As your empaneled Sightspectrum partner, {OFFER_LINE[0].lower()}{OFFER_LINE[1:]}"
        if client_type == "active"
        else OFFER_LINE
    )
    body = "\n\n".join([greeting, reference, offer, CTA_LINE])
    return build_email_subject(job), append_closing(body, sender_email, deck_url)


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
