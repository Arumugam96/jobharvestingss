"""Prompts + templates for recruiter outreach generation
(app/services/outreach_service.py).

The outreach email is a short, personal note: a greeting, three brief lines
(reference the posting → a general, conversational mention of how Sightspectrum
can help with their contract IT hiring → a low-pressure CTA), then a deterministic
closing (sign-off and optional website link). The middle lines are LLM-generated
and kept deliberately general and varied — no fixed marketing offer or quantified
claims (candidate counts, turnaround windows, pricing) — so the mail reads as 1:1
correspondence rather than a promotional blast; when LLM generation fails,
render_email_fallback builds an equivalent plain template from the job context.

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
# Neutral, conversational subject fragments — no offers, counts, or time windows —
# so the subject reads like a personal note about the role, not a marketing blast
# (offer-y subjects with numbers/"48 hrs" are a strong Gmail-Promotions signal).
# Randomizing also keeps subjects from looking mass-generated.
SUBJECT_TAGLINES = (
    "quick note on your open role",
    "regarding your recent posting",
    "about your IT hiring",
    "supporting your open role",
    "a quick introduction",
    "regarding your job post",
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

# ── General value line + deterministic closing ───────────────────────────────
# A general, non-promotional value statement used as the deterministic FALLBACK
# body. The LLM is NOT handed this string — the email/follow-up system prompts ask
# the model to convey the same idea in its own words and vary it each time. Kept
# deliberately free of quantified offers (candidate counts, turnaround windows) and
# pricing/"no fee" language — those are the phrases that push mail to Promotions.
OFFER_LINE = (
    "We help teams hire contract IT talent across India and can put forward "
    "candidates suited to your role."
)
# Low-pressure CTA line — the fallback uses this verbatim; the LLM writes its own
# closing question from the intent described in the system prompt.
CTA_LINE = "Would it help if I shared a few relevant profiles, or would a quick call be easier?"
# Hardcoded sender title in the sign-off (spelling per business template).
SENDER_TITLE = "HR Recruiter, SightSpectrum"
# Company name used in the intro line and the email signature.
COMPANY_NAME = "SightSpectrum"
# Role-only fallback title (no company) for the intro + signature when a sender isn't
# listed in SENDER_IDENTITIES — derived from SENDER_TITLE so the two stay in sync.
DEFAULT_ROLE = SENDER_TITLE.split(",")[0].strip()
# NOTE: no opt-out text is part of the generated copy. Unsubscribe is Mailjet-managed
# now (Mailjet injects its own List-Unsubscribe + hosted opt-out), and email_service
# appends a plain "reply to unsubscribe" line at send time.
DECK_LINK_TEMPLATE = "More about us: {url}"

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
        <Full name or First>       (only when a name resolves)
        <Role Title>, SightSpectrum
        <sender_email>             (only when present)

    Name and title come from resolve_identity so the plain-text sign-off matches the HTML
    signature and the intro. The name line is dropped for role mailboxes with no derivable
    name; the email line is left as plain text here and becomes a bold clickable mailto link
    when the email is sent as HTML (where email_service replaces this whole block with a
    formatted signature card)."""
    identity = resolve_identity(sender_email)
    name = identity["full"] or identity["first"]
    email = (sender_email or "").strip()
    lines = ["Regards,"]
    if name:
        lines.append(name)
    lines.append(f"{identity['title']}, {COMPANY_NAME}")
    if email:
        lines.append(email)
    return "\n".join(lines)


def _insert_after_greeting(body: str, intro: str) -> str:
    """Place the deterministic intro line right after the greeting. `body` has already been
    through _greeting_on_own_line, so any greeting is isolated as the first paragraph; split
    on the first blank line and inject the intro between the greeting and the rest. When the
    body has no recognizable greeting, the intro simply leads."""
    if not intro:
        return body
    parts = body.split("\n\n", 1)
    if parts and _GREETING_RE.match(parts[0].strip()):
        greeting = parts[0]
        rest = parts[1] if len(parts) == 2 else ""
        return f"{greeting}\n\n{intro}\n\n{rest}" if rest else f"{greeting}\n\n{intro}"
    return f"{intro}\n\n{body}" if body else intro


def append_closing(
    pitch: str, sender_email: str, deck_url: str = "", contact_block: str = "", intro: str = "",
) -> str:
    """Append the deterministic closing to an LLM- or template-generated pitch, in
    this order (blocks separated by blank lines):

        Hi <name>,

        <intro>                          (when given — deterministic self-intro +
                                          posting reference, right after the greeting)

        <pitch body>

        <contact_block>                  (when given — the automated-send desk
                                          "reach out to us" name/phone block)

        More about us: <deck_url>        (when deck_url is set)

        Regards,                         (sign-off block — ALWAYS LAST)
        <Name>
        <Role Title>, SightSpectrum
        <sender_email>

    The sign-off is placed last so the signature closes the email; the phone/contact block
    sits above the website link (phone → website → sign-off). The sender email, website URL,
    and phone number are left as plain text here; email_service turns them into a bold
    clickable mailto link, a clickable link, and a bold clickable tel: link when the email is
    sent as HTML (and replaces the sign-off with a formatted signature card), and appends a
    plain unsubscribe line at send time."""
    body = _insert_after_greeting(_greeting_on_own_line(_strip_trailing_closing(pitch)), intro)
    url = (deck_url or "").strip()
    contact = (contact_block or "").strip()
    blocks = [body] if body else []
    if contact:
        blocks.append(contact)
    if url:
        blocks.append(DECK_LINK_TEMPLATE.format(url=url))
    blocks.append(_build_sign_off(sender_email))
    return "\n\n".join(b for b in blocks if b).strip()


EMAIL_SYSTEM_PROMPT = (
    "You are an IT staffing recruiter at Sightspectrum. You write very short, "
    "direct B2B outreach emails to recruiters, talent-acquisition contacts, and "
    "hiring managers who have posted a job, offering pre-screened contract IT "
    "profiles. Write like a real person sending a quick, confident note — never a "
    "hyped sales blast, never long. "

    "EMAIL STRUCTURE: "
    "Output a greeting line, then EXACTLY two short lines, each as its own "
    "paragraph (a blank line between each). Nothing else. Do NOT open with a line that "
    "references the job posting or introduces yourself — a fixed introduction line "
    "(the sender's name + a reference to the posting) is added automatically right after "
    "the greeting, so start straight into how you can help. "
    "(1) In one or two short sentences, naturally explain how Sightspectrum could help with the role. "
    "Convey only that Sightspectrum supports contract IT hiring and can put forward candidates "
    "suited to the role. Use your own wording and vary the sentence structure, tone, and "
    "phrasing between messages so the result feels individually written rather than templated. "
    "(2) A brief, low-pressure closing question inviting an easy next step — for "
    "instance offering to send the profiles or to set up a short call. Phrase it "
    "freshly in your own words each time, as one short sentence; do NOT reuse a "
    "canned line. "

    "TONE: "
    "Concise, professional, and human. Apply the requested tone and audience note "
    "from the user prompt only to lightly steer phrasing (warmth, word choice, and — "
    "for an existing client — a brief nod to the ongoing partnership). Avoid generic "
    "sales hype ('industry-leading', 'best-in-class', 'cutting-edge', etc.). "

    "WRITE FOR THE PRIMARY INBOX (avoid the Promotions tab): "
    "Sound like one person emailing another, not a campaign. Do NOT use bulk-marketing "
    "patterns — quantified or time-bound offers, deadlines, 'free'/'no fee'/'no cost'/"
    "'discount', superlatives, urgency, repeated calls to action, or promotional "
    "taglines. Keep it plain, specific, and low-key, and vary the wording every time. "

    "GREETING RULES: "
    "Address the recipient by first name when a recipient name is given (e.g. "
    "'Hi Jane,'); when no name is given, use a neutral greeting such as 'Hello,'. "
    "Put the greeting on its OWN line, followed by a blank line, then the two lines. "

    "PLACEHOLDER RULES: "
    "NEVER output a bracketed placeholder such as '[Recipient Name]', '[Name]', "
    "'[Company]', or '[Your Name]'. If a detail is unknown, omit it or rephrase "
    "neutrally. Do not invent company information, names, phone numbers, or email "
    "addresses. "

    "CONTACT AND SIGN-OFF RULES: "
    "Write ONLY the greeting and the two lines. Do NOT add a sign-off such as "
    "'Regards' or the sender's name, and do NOT add a phone number, email address, "
    "website, or opt-out line — the introduction, sign-off, website link, and opt-out "
    "notice are appended automatically by the application. "

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


# Per-sender display identity for the outreach intro + signature. The email local part
# only yields a first name (see sender_display_name), so anything that can't be derived
# from the address — the person's FULL name, their formal role title, and their direct
# phone — is listed here, keyed by the lowercased sender email. Senders not listed fall
# back to a first-name-only identity with the default role and no phone (resolve_identity),
# so an unlisted sender still gets a valid signature, just without a bespoke full name.
SENDER_IDENTITIES: dict[str, dict[str, str]] = {
    "sanjeetha@sightspectrum.com": {
        "first": "Sanjeeta",
        "full": "Sanjeeta Mohanty",
        "title": "Business Development Executive",
        "phone": "",  # TODO: add Sanjeeta's direct number; the Tel row is omitted while empty.
    },
}


def resolve_identity(sender_email: str) -> dict[str, str]:
    """Resolve the sender's display identity (``first``, ``full``, ``title``, ``phone``)
    used by the intro line, the plain-text sign-off, and the HTML signature — one source
    of truth so all three always agree.

    Looks up SENDER_IDENTITIES by lowercased email. For an unlisted sender, falls back to
    the first name derived from the email (sender_display_name), an empty full name
    (callers use ``first``), the role-only DEFAULT_ROLE, and no phone. ``title`` is always
    role-only (no company); callers append COMPANY_NAME where a company is wanted."""
    email = (sender_email or "").strip().lower()
    entry = SENDER_IDENTITIES.get(email) or {}
    first = entry.get("first") or sender_display_name(sender_email)
    return {
        "first": first,
        "full": entry.get("full", ""),
        "title": entry.get("title") or DEFAULT_ROLE,
        "phone": entry.get("phone", ""),
    }


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

def _article(word: str) -> str:
    """Indefinite article for `word` — "an" before a vowel sound, else "a". Good enough
    for role titles ("a Business Development Executive", "an HR Recruiter")."""
    return "an" if word[:1].lower() in "aeiou" else "a"


def build_intro(sender_email: str, job: dict) -> str:
    """Deterministic self-introduction + posting reference, placed right after the greeting:

        I'm <First>, a <Role Title> with SightSpectrum. I saw the listing for <Job Title> at <Company>.

    The name/title come from resolve_identity(sender_email) so the intro matches the
    sign-off/signature; <Job Title> is the EXACT stored title so the sent HTML links the
    first occurrence of it to the posting (see email_service._outreach_body_to_html), and
    <Company> falls back to a neutral stand-in. Returns "" when there's no job title
    (nothing to reference). When no first name resolves (a role mailbox), drops the personal
    "I'm <name>" opener for a neutral variant."""
    title = (job.get("job_title") or "").strip()
    if not title:
        return ""
    company = (job.get("company") or "").strip() or _GENERIC_COMPANY
    identity = resolve_identity(sender_email)
    first = identity["first"]
    role = identity["title"]
    listing = f"I saw the listing for {title} at {company}."
    if first:
        return f"I'm {first}, {_article(role)} {role} with {COMPANY_NAME}. {listing}"
    return f"I'm reaching out from {COMPANY_NAME} about the listing for {title} at {company}."


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
    # With a title: "<Role> — <tagline>". Without one: the tagline alone, capitalized.
    return f"{title} — {tagline}" if title else tagline[:1].upper() + tagline[1:]


def render_email_fallback(
    client_type: str, job: dict, sender_email: str = "", deck_url: str = "", contact_block: str = ""
) -> tuple[str, str]:
    """Deterministic fallback used when LLM generation fails — builds the same
    fixed template (greeting + deterministic intro + fixed offer + CTA) directly
    from the job context, with the same closing appended as the LLM path. The
    posting reference now lives in the intro (build_intro), so the fallback body is
    just the offer + CTA. For an existing ("active") client the offer line carries a
    brief partnership nod; the fixed claims are unchanged. `client_type` other than
    "active" is treated as a new/unknown prospect. `contact_block` (automated sends)
    is placed above the sign-off, mirroring the LLM path."""
    poster = (job.get("job_poster_name") or "").strip()

    greeting = f"Hi {poster.split()[0]}," if poster else "Hello,"
    offer = (
        f"As your empaneled Sightspectrum partner, {OFFER_LINE[0].lower()}{OFFER_LINE[1:]}"
        if client_type == "active"
        else OFFER_LINE
    )
    body = "\n\n".join([greeting, offer, CTA_LINE])
    intro = build_intro(sender_email, job)
    return build_email_subject(job), append_closing(body, sender_email, deck_url, contact_block, intro=intro)


# ── Follow-up email ──────────────────────────────────────────────────────────
# A follow-up reuses the same job context and deterministic closing (append_closing)
# as an initial email, but the middle lines are a gentle nudge referencing the
# earlier note — NEVER a fresh introduction. The prior send's subject/body are
# supplied so the model can acknowledge it without repeating it.

FOLLOWUP_SYSTEM_PROMPT = (
    "You are an IT staffing recruiter at Sightspectrum writing a SHORT follow-up "
    "email to a recruiter you already emailed earlier about their job posting. "
    "This is a second-touch nudge, NOT a first introduction — the recipient has "
    "already received your initial note (its subject and body are provided). Write "
    "like a real person sending a brief, friendly check-in. "

    "STRUCTURE: "
    "Output a greeting line, then EXACTLY three short lines, each its own paragraph "
    "(a blank line between each). Nothing else. "
    "(1) A one-line gentle follow-up that references the earlier message about the "
    "role — e.g. 'Following up on my note about <JOB TITLE> at <COMPANY>.' Use the "
    "job title EXACTLY as given in the context (verbatim — do not paraphrase, "
    "shorten, reorder, or change capitalization). Do NOT re-introduce Sightspectrum "
    "as if for the first time. "
    "(2) A brief, general reminder of how you can help — in your OWN words, varied "
    "every time: that Sightspectrum supports their contract IT hiring across India "
    "and can put forward suitable candidates. Keep it to one natural, conversational "
    "sentence. Do NOT frame it as a marketing offer: no candidate counts or turnaround "
    "windows (e.g. '3-4 profiles', '48 hours'), no pricing or 'free'/'no fee' phrasing, "
    "and do NOT add other services, skills, technologies, statistics, or guarantees. "
    "(3) A brief, low-pressure closing question inviting an easy next step — for "
    "instance offering to send the profiles or to set up a short call. Phrase it "
    "freshly in your own words each time, as one short sentence; do NOT reuse a "
    "canned line. "

    "TONE: "
    "Concise, professional, human, and unpushy — acknowledge they may be busy. Apply "
    "the requested tone only to lightly steer phrasing. Never guilt-trip or over-"
    "apologize. Avoid sales hype ('industry-leading', 'best-in-class', etc.). "

    "WRITE FOR THE PRIMARY INBOX (avoid the Promotions tab): "
    "Sound like one person following up with another, not a campaign. Do NOT use bulk-"
    "marketing patterns — quantified or time-bound offers, deadlines, 'free'/'no fee', "
    "superlatives, urgency, repeated calls to action, or promotional taglines. Keep it "
    "plain and low-key, and vary the wording every time. "

    "GREETING RULES: "
    "Address the recipient by first name when a recipient name is given (e.g. "
    "'Hi Jane,'); when no name is given, use a neutral greeting such as 'Hello,'. "
    "Put the greeting on its OWN line, followed by a blank line, then the three lines. "

    "PLACEHOLDER RULES: "
    "NEVER output a bracketed placeholder such as '[Recipient Name]', '[Name]', "
    "'[Company]', or '[Your Name]'. If a detail is unknown, omit it or rephrase "
    "neutrally. Do not invent company information, names, phone numbers, or emails. "

    "CONTACT AND SIGN-OFF RULES: "
    "Write ONLY the greeting and the three lines. Do NOT add a sign-off, the sender's "
    "name, a phone number, email address, website, or opt-out line — these are "
    "appended automatically by the application. "

    "OUTPUT RULES: "
    "Return ONLY valid JSON in exactly this form: "
    "{\"subject\": \"...\", \"body\": \"...\"}. "
    "Do not include commentary, explanations, markdown, HTML, or code fences."
)


def build_followup_email_prompt(
    client_type: str,
    tone: str,
    job: dict,
    prior_subject: str = "",
    prior_body: str = "",
    prior_sent_at: str = "",
    sender_email: str = "",
) -> str:
    """User prompt for follow-up email generation. Supplies the same job context
    as an initial email PLUS the prior outreach (subject/body/when) so the model
    writes a genuine second-touch nudge instead of another first introduction.
    The follow-up structure, offer claims, and output rules live in
    FOLLOWUP_SYSTEM_PROMPT; the sign-off is appended by append_closing."""
    tone_instr = TONE_INSTRUCTIONS.get(tone, TONE_INSTRUCTIONS["Formal"])
    audience = _AUDIENCE.get(client_type, _AUDIENCE["new"])
    prior_block_lines = ["Earlier outreach already sent to this recipient:"]
    if prior_sent_at:
        prior_block_lines.append(f"- Sent: {prior_sent_at}")
    if prior_subject:
        prior_block_lines.append(f"- Subject: {prior_subject}")
    if prior_body:
        snippet = prior_body.strip()
        if len(snippet) > 900:  # keep the prompt bounded — the opening carries the intent
            snippet = snippet[:900] + " …"
        prior_block_lines.append(f"- Body:\n{snippet}")
    prior_block = "\n".join(prior_block_lines)
    return (
        f"Audience — {audience}\n\n"
        f"Tone: {tone} — {tone_instr}\n\n"
        f"Recipient / role context:\n{_job_context(job, include_description=False)}\n\n"
        f"{prior_block}\n\n"
        f"Write a FOLLOW-UP outreach email BODY only (a gentle second-touch nudge "
        f"referencing the earlier note — do NOT reintroduce Sightspectrum from "
        f"scratch), following the structure and rules in the system instructions. "
        f'Return ONLY JSON: {{"subject": "...", "body": "..."}}'
    )


def build_followup_subject(prior_subject: str, job: dict) -> str:
    """Deterministic follow-up subject: reply-prefix the prior subject so it reads
    as the same thread ('Re: <prior>'), avoiding a doubled 'Re:'. Falls back to a
    fresh outreach subject when no prior subject is known."""
    prior = (prior_subject or "").strip()
    if not prior:
        return build_email_subject(job)
    return prior if prior.lower().startswith("re:") else f"Re: {prior}"


def render_followup_fallback(
    job: dict, prior_subject: str = "", sender_email: str = "", deck_url: str = "", contact_block: str = ""
) -> tuple[str, str]:
    """Deterministic follow-up used when LLM generation fails — a brief nudge built
    from the job context, with the same closing appended as the LLM path.
    `contact_block` (automated sends) is placed above the sign-off."""
    company = (job.get("company") or "").strip() or _GENERIC_COMPANY
    title = (job.get("job_title") or "").strip()
    poster = (job.get("job_poster_name") or "").strip()

    greeting = f"Hi {poster.split()[0]}," if poster else "Hello,"
    reference = (
        f"Just following up on my earlier note about {title} at {company}."
        if title
        else f"Just following up on my earlier note about your hiring at {company}."
    )
    body = "\n\n".join([greeting, reference, OFFER_LINE, CTA_LINE])
    return build_followup_subject(prior_subject, job), append_closing(body, sender_email, deck_url, contact_block)


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
