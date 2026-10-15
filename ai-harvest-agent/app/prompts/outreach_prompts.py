"""Prompts + templates for recruiter outreach generation
(app/services/outreach_service.py).

The initial outreach email is a short BDE-style note:

    Hi <First>,

    I'm <Name>, a <Role> at SightSpectrum. I noticed your <Platform> posting for
    <Job Title> at <Company> and wanted to connect.

    <contract IT staffing value + audience ask (vendor onboarding / priority roles)>

    <short, low-pressure question>

then a deterministic closing (contact block, website link, sign-off). The greeting,
the "I'm <Name>, a <Role> at SightSpectrum." intro and the closing are added by code
(append_closing); the LLM writes only the body — the job-reference sentence that
continues the intro's paragraph, the value/ask paragraph and the closing question —
kept general and varied, with no quantified claims (candidate counts, turnaround
windows, pricing), so the mail reads as 1:1 correspondence rather than a promotional
blast. When LLM generation fails, render_email_fallback builds the same shape from
the job context.

Audiences steer the phrasing and the ask:
  * active client  — a company already on ss_active_clients.json
  * new client     — everyone else ("unknown" when the company can't be identified)
The initial-email prompt sends the per-audience ask (_EMAIL_ASK); follow-ups send
the broader positioning note (_AUDIENCE).

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

# Per-audience ask for the INITIAL email only (build_email_prompt) — what the value
# paragraph should ask for. Follow-ups keep the broader _AUDIENCE note above.
_EMAIL_ASK = {
    "active": (
        "This company is an existing SightSpectrum client. Acknowledge the existing "
        "partnership briefly and invite them to share their priority/upcoming "
        "requirements. Do NOT ask about vendor onboarding."
    ),
    "new": (
        "This company is a new prospect. Ask about their vendor onboarding process and "
        "potential collaboration."
    ),
    "unknown": (
        "The company could not be identified. Ask about their vendor onboarding process "
        "and potential collaboration."
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
# Value/ask + CTA lines for the INITIAL-email fallback (render_email_fallback) only —
# OFFER_LINE / CTA_LINE above stay as the follow-up fallback's lines.
INTRO_OFFER_NEW = (
    "We provide skilled contract IT professionals to support your hiring needs, and I'd "
    "be happy to learn about your vendor onboarding process and explore potential "
    "collaboration."
)
INTRO_OFFER_ACTIVE = (
    "As your existing staffing partner, we'd be glad to support your current requirements "
    "with skilled contract IT professionals; please feel free to share your priority roles."
)
INTRO_CTA = "Would you be available for a brief call?"
# Hardcoded sender title in the sign-off.
SENDER_TITLE = "Business Development Executive, SightSpectrum"
# Company name used in the intro line and the email signature.
COMPANY_NAME = "SightSpectrum"
# Role-only fallback title (no company) for the intro + signature when a sender isn't
# listed in SENDER_IDENTITIES — derived from SENDER_TITLE so the two stay in sync.
DEFAULT_ROLE = SENDER_TITLE.split(",")[0].strip()

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


# A leading sentence that is ONLY a self-introduction, which the model may write despite
# instructions: "I'm Hari from SightSpectrum." / "I'm Hari, a BDE at SightSpectrum." /
# "My name is Hari Prasath." Deliberately tight — the sentence must end right after
# "SightSpectrum" (or the bare name) and must not mention "your …", so a sentence that
# also carries the posting reference (and the job title email_service links) never matches,
# nor does "SightSpectrum Pvt. Ltd." / "sightspectrum.com" (no mid-abbreviation cuts).
_SELF_INTRO_RE = re.compile(
    r"^(?:"
    r"(?:I['’]?m|I am|This is)\s+(?:(?!\byour\b)[^.!?\n]){0,80}?\b(?:from|at|with)\s+SightSpectrum"
    r"|My name is\s+[A-Za-z][\w'’-]*(?:\s+[A-Za-z][\w'’-]*)?"
    r")\s*[.!](?:\s+|$)",
    re.IGNORECASE,
)


def _strip_self_intro(paragraph: str) -> str:
    """Drop a leading model-written self-introduction sentence from `paragraph`, so the
    deterministic intro merged in front of it isn't duplicated ("I'm Hari, … I'm Hari
    from SightSpectrum."). Only a sentence that is purely a self-intro is removed (see
    _SELF_INTRO_RE); anything else is returned unchanged — a duplicated intro is a safer
    failure than losing the job-title sentence."""
    text = (paragraph or "").strip()
    m = _SELF_INTRO_RE.match(text)
    return text[m.end():].lstrip() if m else text


def _merge_intro(intro: str, text: str) -> str:
    """Merge the intro into the first paragraph of `text` (after dropping any model
    self-intro there): "I'm <First>, a <Role> at SightSpectrum. I noticed your …".
    Later paragraphs are kept as-is; with no remaining text the intro stands alone."""
    rest = (text or "").strip()
    while rest:
        first, _, remaining = rest.partition("\n\n")
        first = _strip_self_intro(first)
        remaining = remaining.strip()
        if first:
            merged = f"{intro} {first}"
            return f"{merged}\n\n{remaining}" if remaining else merged
        rest = remaining  # the paragraph was only a self-intro — merge with the next
    return intro


def _insert_after_greeting(body: str, intro: str) -> str:
    """Place the deterministic intro right after the greeting, merged into the first
    paragraph of the pitch (the posting-reference sentence) so the two read as one
    opening paragraph. `body` has already been through _greeting_on_own_line, so any
    greeting is isolated as the first paragraph; split on the first blank line and merge
    the intro into the rest. When the body has no recognizable greeting, the merged intro
    simply leads. Only initial emails pass an intro — follow-ups are unaffected."""
    if not intro:
        return body
    parts = body.split("\n\n", 1)
    if parts and _GREETING_RE.match(parts[0].strip()):
        greeting = parts[0]
        rest = parts[1] if len(parts) == 2 else ""
        return f"{greeting}\n\n{_merge_intro(intro, rest)}"
    return _merge_intro(intro, body)


def _ensure_greeting(body: str, greeting: str) -> str:
    """Force `greeting` to be the single opening line of `body`, replacing any greeting the
    model emitted. `body` has already been through _greeting_on_own_line, so a model greeting
    is isolated as the first paragraph — drop it and prepend the deterministic one, so the
    email always opens with a recipient greeting rather than the sender's self-introduction.
    No-op when `greeting` is empty."""
    if not greeting:
        return body
    text = (body or "").lstrip()
    parts = text.split("\n\n", 1)
    if parts and _GREETING_RE.match(parts[0].strip()):
        rest = parts[1].lstrip() if len(parts) == 2 else ""
    else:
        rest = text
    return f"{greeting}\n\n{rest}" if rest else greeting


def append_closing(
    pitch: str, sender_email: str, deck_url: str = "", contact_block: str = "", intro: str = "",
    greeting: str = "", candidate_block: str = "",
) -> str:
    """Append the deterministic closing to an LLM- or template-generated pitch, in
    this order (blocks separated by blank lines):

        Hi <name>,

        <intro> <first pitch paragraph>  (when given — the deterministic self-intro is
                                          merged into the pitch's opening paragraph,
                                          i.e. the posting reference)

        <rest of the pitch body>

        <candidate_block>                (when given — the anonymised Resdex matching-
                                          profiles card, resdex_match_service.
                                          format_candidate_block)

        <contact_block>                  (when given — the automated-send desk
                                          "reach out to us" name/phone block)

        More about us: <deck_url>        (when deck_url is set)

        Regards,                         (sign-off block — ALWAYS LAST)
        <Name>
        <Role Title>, SightSpectrum
        <sender_email>
    """
    body = _ensure_greeting(
        _insert_after_greeting(_greeting_on_own_line(_strip_trailing_closing(pitch)), intro),
        greeting,
    )
    url = (deck_url or "").strip()
    contact = (contact_block or "").strip()
    candidates = (candidate_block or "").strip()
    blocks = [body] if body else []
    if candidates:
        blocks.append(candidates)
    if contact:
        blocks.append(contact)
    if url:
        blocks.append(DECK_LINK_TEMPLATE.format(url=url))
    blocks.append(_build_sign_off(sender_email))
    return "\n\n".join(b for b in blocks if b).strip()


EMAIL_SYSTEM_PROMPT = """
You write short, natural outreach emails for SightSpectrum, an IT staffing company.

CONTEXT:
You're writing to a recruiter, talent acquisition professional, or hiring manager who posted an IT job. The purpose is to start a conversation about contract IT staffing.

WHAT THE APPLICATION ADDS (do NOT write these):
The application automatically adds the greeting ("Hi <Name>,") and the sender's introduction ("I'm <Name>, a <Role> at SightSpectrum.") before your text, and the contact details, website link and sign-off after it. Your body continues the same paragraph straight after that introduction.

WHAT TO WRITE — the BODY only, as 3 short paragraphs separated by a blank line, about 45–85 words in total:

P1. One sentence referencing the opening. Use the EXACT job title provided in the context (preserve its wording and capitalization; don't shorten, rephrase, or reorder it), the company name, and the platform exactly as given in the context's "Job posting source" (e.g. your Naukri posting, your LinkedIn posting, your Dice posting). Never assume LinkedIn; if the source is unknown, say "your job posting" and don't name any platform. If the company is unknown, leave it out. Vary the verb (noticed / came across / saw) and how the sentence ends (e.g. "and wanted to connect", "and wanted to introduce our IT staffing services").
P2. One or two sentences saying SightSpectrum provides skilled contract IT professionals to support their hiring needs, followed by the ask given in the "Ask —" line of the request.
P3. One short, low-pressure question, e.g. whether they'd be available for a brief call or a quick discussion next week. Don't start it with "Thanks" or "Thank you".

STYLE REFERENCES (style reference — do not copy verbatim; vary wording). Each shows a BODY only, for a sample job "Senior Data Engineer" at "Acme Corp" — always use the real job title, company and platform from the context:

Reference 1 (new prospect, Naukri):
I noticed your Naukri posting for Senior Data Engineer at Acme Corp and wanted to connect.

We provide skilled contract IT professionals to support your hiring needs. I'd be happy to learn about your vendor onboarding process and explore potential collaboration.

Would you be available for a brief call?

Reference 2 (new prospect, LinkedIn):
I came across your LinkedIn posting for Senior Data Engineer at Acme Corp and wanted to introduce our IT staffing services.

SightSpectrum provides qualified contract IT professionals to help organizations meet their technical hiring needs. I'd welcome the opportunity to understand your vendor onboarding process and explore how we could support your current and future staffing requirements.

Would you be available for a brief discussion next week?

Reference 3 (existing client, Dice):
I saw your Dice posting for Senior Data Engineer at Acme Corp and wanted to connect.

As your existing staffing partner, we'd be glad to support this role with skilled contract IT professionals. Please feel free to share your priority requirements with us.

Would a quick call this week work for you?

HUMAN WRITING:

* Write like a person sending a quick, genuine work email — simple, relaxed, and professional. Everyday English and natural contractions are fine.
* Vary the wording and sentence structure between emails; don't reuse the references word for word.
* Don't repeat the job title unnecessarily.
* Avoid corporate language, generic pleasantries, exaggerated enthusiasm, and sales pitches.
* Avoid phrases like "I hope this email finds you well", "Moreover", "Furthermore", and "In today's fast-paced market".
* Don't invent personal connections, previous conversations, candidate details, or company facts.
* Don't claim candidates are pre-screened, available, or qualified for this specific role unless the supplied context confirms it.

DON'T:
* Don't write a greeting or salutation.
* Don't introduce yourself or name the sender — no "I'm … from SightSpectrum", no "My name is …".
* Don't add a sign-off, signature, phone number, URL, or email address.
* Never output bracketed placeholders such as [Name], [Job Title], or [Company Name].
* Don't mention candidate counts, fees, discounts, guarantees, deadlines, or urgency.

SUBJECT:
Write a short, relevant subject line connected to the actual job. Keep it natural and specific, without clickbait or promotional wording.

OUTPUT:
Return ONLY a valid JSON object with exactly these keys:
{"subject": "...", "body": "..."}

Both values must be strings. Escape quotation marks, backslashes, and newlines correctly. Do not include markdown, HTML, code fences, or explanations.
"""


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
    "sanjeeta@sightspectrum.com": {
        "first": "Sanjeeta",
        "full": "Sanjeeta Mohanty",
        "title": "Business Development Executive",
        "phone": "", 
    },
    "ananyamehta@sightspectrum.com": {
        "first": "Ananya",
        "full": "Ananya Mehta",
        "title": "Business Development Executive",
        "phone": "", 
    }
}


def resolve_identity(sender_email: str) -> dict[str, str]:
    """Resolve the sender's display identity (``first``, ``full``, 
    ``title``, ``phone``)
    """
    email = (sender_email or "").strip().lower()
    entry = SENDER_IDENTITIES.get(email) or {}
    first = entry.get("first") or sender_display_name(sender_email)
    return {
        "first": first,
        "full": entry.get("full", ""),
        "title": entry.get("title") or DEFAULT_ROLE,
        "phone": entry.get("phone", ""),
    }


# ── Job platform (where the posting was found) ──────────────────────────────
# The outreach email references "your <platform> posting", so the platform must come
# from the job's own `source` — a Naukri job must never be called a LinkedIn post.
# scraped_jobs.source values: "LinkedIn" | "Naukri" | "Dice" | "LinkedIn Feed" (older
# rows / agents may use lowercase). LinkedIn Feed leads are LinkedIn posts.

PLATFORM_NAMES = ("LinkedIn", "Naukri", "Dice")

# Only used when `source` is missing/unrecognized: the posting URL's host is hard
# evidence of the platform (not a guess). Exact host or a subdomain of it.
_PLATFORM_HOSTS = (("linkedin.com", "LinkedIn"), ("naukri.com", "Naukri"), ("dice.com", "Dice"))


def _platform_from_name(value: str) -> str:
    """Canonical platform for a source string / matched mention, or "" if none."""
    v = re.sub(r"[\s_\-.]+", "", (value or "").lower())
    if v.startswith("linkedin"):  # "linkedin", "linkedinfeed", "linkedin.com"
        return "LinkedIn"
    if v.startswith("naukri"):
        return "Naukri"
    if v.startswith("dice"):
        return "Dice"
    return ""


def job_platform(job: dict) -> str:
    """The job board the posting came from — "LinkedIn", "Naukri", "Dice", or "" when
    it can't be determined. Uses the job's `source`; only when that is missing or
    unrecognized does it fall back to the posting URL's host. Never guesses."""
    platform = _platform_from_name(job.get("source") or "")
    if platform:
        return platform
    url = (job.get("job_url") or "").strip().lower()
    m = re.match(r"^[a-z][a-z0-9+.\-]*://([^/?#:]+)", url)
    host = m.group(1) if m else ""
    for domain, name in _PLATFORM_HOSTS:
        if host == domain or host.endswith("." + domain):
            return name
    return ""


def posting_phrase(job: dict) -> str:
    """"your Naukri posting" / "your LinkedIn posting" / "your Dice posting", or the
    neutral "your job posting" when the platform is unknown."""
    platform = job_platform(job)
    return f"your {platform} posting" if platform else "your job posting"


# Post-check for LLM output: the model sometimes defaults to "LinkedIn" whatever the
# source. Only platform mentions in a POSTING context are touched (e.g. "your LinkedIn
# post", "saw your opening on LinkedIn") — an unrelated mention like "connect on LinkedIn"
# is left alone, as are mentions inside the job title / company name / URLs / emails.
_PLAT_TOKEN = r"(?:linked\s?in|naukri|dice)(?:\.com)?"  # compiled with re.I
_POSTING_NOUN = (
    r"(?:job\s+)?(?:post|posting|listing|ad|advert|advertisement|opening|vacancy|"
    r"requirement|job|role)s?"
)
# "LinkedIn posting", "Naukri job post", "Dice listing" …
_PLAT_BEFORE_NOUN_RE = re.compile(
    rf"\b(?P<plat>{_PLAT_TOKEN})\b(?P<gap>\s+)(?={_POSTING_NOUN}\b)", re.I
)
# "saw your opening for X on LinkedIn", "came across your post on Naukri" …
_PLAT_AFTER_VERB_RE = re.compile(
    r"\b(?:post(?:ed|ing|s)?|listing|listed|advertis\w*|saw|seen|noticed|came\s+across|"
    r"spotted|found|opening|role|requirement)\b[^.!?\n]{0,80}?"
    rf"(?P<pre>\s(?:on|via|through|from)\s+)(?P<plat>{_PLAT_TOKEN})\b",
    re.I,
)
_URL_OR_EMAIL_RE = re.compile(r"https?://\S+|[\w.+-]+@[\w-]+\.[\w.-]+")


def _protected_spans(text: str, job: dict) -> list[tuple[int, int]]:
    """Spans that must never be rewritten: the job title, the company name (either may
    legitimately contain a platform word), and any URL / email address."""
    spans = [m.span() for m in _URL_OR_EMAIL_RE.finditer(text)]
    for value in ((job.get("job_title") or ""), (job.get("company") or "")):
        tokens = value.split()
        if not tokens:
            continue
        pat = re.compile(r"\s+".join(re.escape(t) for t in tokens), re.I)
        spans.extend(m.span() for m in pat.finditer(text))
    return spans


def correct_platform_mentions(text: str, job: dict) -> tuple[str, int]:
    """Make LLM-written outreach text name the job's real platform. Returns
    (corrected_text, number_of_corrections).

    * known platform → a wrong platform in a posting context is replaced by the right
      one ("your LinkedIn post" → "your Naukri post" for a Naukri job);
    * unknown platform → the platform mention is dropped ("your LinkedIn posting" →
      "your posting", "your post on LinkedIn for X" → "your post for X"), since we must
      not claim a platform we can't verify.
    Mentions already naming the right platform, and anything outside a posting context,
    are left unchanged."""
    if not text:
        return text, 0
    platform = job_platform(job)
    protected = _protected_spans(text, job)

    def _is_protected(start: int, end: int) -> bool:
        return any(start < pe and ps < end for ps, pe in protected)

    edits: dict[int, tuple[int, int, str]] = {}  # keyed by plat start → (start, end, repl)
    for m in _PLAT_BEFORE_NOUN_RE.finditer(text):
        ps, pe = m.span("plat")
        if _platform_from_name(m.group("plat")) == platform or _is_protected(ps, pe):
            continue
        if platform:
            edits[ps] = (ps, pe, platform)
        else:  # drop "LinkedIn " (word + following whitespace)
            edits[ps] = (ps, m.end("gap"), "")
    for m in _PLAT_AFTER_VERB_RE.finditer(text):
        ps, pe = m.span("plat")
        if ps in edits or _platform_from_name(m.group("plat")) == platform or _is_protected(ps, pe):
            continue
        if platform:
            edits[ps] = (ps, pe, platform)
        else:  # drop " on LinkedIn"
            edits[ps] = (m.start("pre"), pe, "")

    if not edits:
        return text, 0
    out = text
    for start, end, repl in sorted(edits.values(), reverse=True):
        before, after = out[:start], out[end:]
        if not repl and after and (not before.strip() or before.rstrip()[-1] in ".!?\n"):
            after = after[:1].upper() + after[1:]  # deletion left a sentence start
        out = before + repl + after
    return out, len(edits)


# ── Job-context block shared by both builders ────────────────────────────────

def _platform_context_line(job: dict) -> str:
    """Prompt line telling the model which platform the posting is on (or that it is
    unknown, in which case no platform may be named)."""
    platform = job_platform(job)
    title = (job.get("job_title") or "").strip()
    if not platform:
        return (
            "Job posting source: unknown — refer to it neutrally as \"your job posting\" "
            "and do NOT name any job platform (LinkedIn, Naukri, Dice, etc.)."
        )
    others = " or ".join(p for p in PLATFORM_NAMES if p != platform)
    example = f"your {platform} posting for {title}" if title else f"your {platform} posting"
    return (
        f"Job posting source: {platform} — this role was posted on {platform}. When you "
        f"reference the posting, call it their {platform} posting (e.g. \"{example}\"). "
        f"Do NOT say it was posted on {others}."
    )


def _job_context(job: dict, include_description: bool = True, greet: bool = True) -> str:
    """Recipient/role context block for the user prompts. `greet=False` (the initial
    email, whose greeting is added deterministically by append_closing) tells the model
    NOT to write a greeting; the follow-up and LinkedIn prompts keep the default."""
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
    lines.append(_platform_context_line(job))
    if not greet:
        lines.append(
            f"Recipient name: {poster or 'unknown'} — the greeting is added automatically; "
            "do not write one, do NOT invent a name and do NOT leave a bracketed placeholder"
        )
    elif poster:
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


def build_greeting(job: dict) -> str:
    """Deterministic recipient greeting placed at the very top of the email: 'Hi <First>,'
    when a recipient name is known, else a neutral 'Hello,'. Used by both the LLM path
    (prepended via append_closing) and the deterministic fallbacks, so every outreach email
    opens with a greeting to the RECIPIENT rather than the sender's self-introduction.

    Prefers `outreach_to_name` (the resolved recipient — the poster for a scraped/recruiter
    email, else the company+location HR contact we're actually emailing) so the greeting
    always matches the recipient; falls back to `job_poster_name` for callers/views that
    don't carry the resolved name."""
    name = (job.get("outreach_to_name") or job.get("job_poster_name") or "").strip()
    return f"Hi {name.split()[0]}," if name else "Hello,"


def build_intro(sender_email: str, job: dict) -> str:
    """Deterministic self-introduction, placed right after the greeting:

        I'm <First>, a <Role Title> at SightSpectrum.

    The name/title come from resolve_identity(sender_email) so the intro matches the
    sign-off/signature. The posting reference is NOT hard-coded here — the LLM writes it
    as the first body sentence (using the exact job title, which
    email_service._outreach_body_to_html then links to the posting), and append_closing
    merges this intro in front of it as one opening paragraph. When no first name
    resolves (a role mailbox), drops the personal "I'm <name>" opener for a neutral variant.
    `job` is accepted for call-site compatibility but is no longer used."""
    identity = resolve_identity(sender_email)
    first = identity["first"]
    role = identity["title"]
    if first:
        return f"I'm {first}, {_article(role)} {role} at {COMPANY_NAME}."
    return f"I'm reaching out from {COMPANY_NAME}."


# Extra prompt line, only when a Resdex candidate card follows the body (the card itself
# is deterministic — the model must not describe or invent the profiles).
CANDIDATES_CONTEXT_LINE = (
    "Matching candidate profiles are included below the message — mention briefly that "
    "you've included a few relevant profiles; do not describe them."
)


def build_email_prompt(
    client_type: str, tone: str, job: dict, sender_email: str = "",
    candidates_included: bool = False,
) -> str:
    """User prompt for email generation. It supplies only the per-request
    variables — `client_type` picks the audience ask (_EMAIL_ASK: vendor onboarding for
    new/unknown prospects, priority requirements for active clients), `tone` the style
    (lightly steers phrasing), and `job` the personalization context (with greet=False —
    the greeting is deterministic). `sender_email` is unused here (the greeting, intro
    and sign-off are added by append_closing); it's kept for signature compatibility
    with the caller. The body structure and output rules live in EMAIL_SYSTEM_PROMPT.
    `candidates_included` adds CANDIDATES_CONTEXT_LINE after the role context (a Resdex
    candidate card follows the body); False leaves the prompt text unchanged."""
    tone_instr = TONE_INSTRUCTIONS.get(tone, TONE_INSTRUCTIONS["Formal"])
    audience = _AUDIENCE.get(client_type, _AUDIENCE["new"])
    ask = _EMAIL_ASK.get(client_type, _EMAIL_ASK["new"])
    candidates = f"{CANDIDATES_CONTEXT_LINE}\n\n" if candidates_included else ""
    return (
        f"Audience — {audience}\n\n"
        f"Ask — {ask}\n\n"
        f"Tone: {tone} — {tone_instr}\n\n"
        f"Recipient / role context:\n{_job_context(job, include_description=False, greet=False)}\n\n"
        f"{candidates}"
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
    client_type: str, job: dict, sender_email: str = "", deck_url: str = "",
    contact_block: str = "", candidate_block: str = "",
) -> tuple[str, str]:
    """Deterministic fallback used when LLM generation fails — builds the same shape the
    LLM path assembles (greeting + self-intro merged with the posting reference + offer/ask
    + CTA) directly from the job context, with the same closing appended. The posting
    reference is written here as the first body line; append_closing merges the intro in
    front of it. An existing ("active") client gets the partnership/priority-roles line
    (INTRO_OFFER_ACTIVE); any other `client_type` is treated as a new/unknown prospect and
    gets the vendor-onboarding line (INTRO_OFFER_NEW). `contact_block` (automated sends)
    is placed above the sign-off, mirroring the LLM path; `candidate_block` (Resdex
    matching profiles) sits between the body and the contact block."""
    company = (job.get("company") or "").strip() or _GENERIC_COMPANY
    title = (job.get("job_title") or "").strip()

    greeting = build_greeting(job)
    # "your Naukri posting" / "your LinkedIn posting" / "your Dice posting", or the neutral
    # "your job posting" when the source is unknown — never a guessed platform. The title
    # stays verbatim so email_service._outreach_body_to_html links it to the posting.
    posting = posting_phrase(job)
    reference = (
        f"I noticed {posting} for {title} at {company} and wanted to connect."
        if title
        else f"I noticed {posting} at {company} and wanted to connect."
    )
    offer = INTRO_OFFER_ACTIVE if client_type == "active" else INTRO_OFFER_NEW
    body = "\n\n".join([reference, offer, INTRO_CTA])
    intro = build_intro(sender_email, job)
    return build_email_subject(job), append_closing(
        body, sender_email, deck_url, contact_block, intro=intro, greeting=greeting,
        candidate_block=candidate_block,
    )


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
    "Do NOT write a greeting — a recipient greeting is added automatically before your "
    "text. Output EXACTLY three short lines, each its own paragraph (a blank line between "
    "each). Nothing else. "
    "(1) A one-line gentle follow-up that references the earlier message about the "
    "role — e.g. 'Following up on my note about <JOB TITLE> at <COMPANY>.' Use the "
    "job title EXACTLY as given in the context (verbatim — do not paraphrase, "
    "shorten, reorder, or change capitalization). If you mention where the job was "
    "posted, use ONLY the platform given as the context's 'Job posting source' (e.g. "
    "their Naukri posting) — never assume LinkedIn; if the source is unknown, name no "
    "platform. Do NOT re-introduce Sightspectrum as if for the first time. "
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
    "A recipient greeting is added automatically — do NOT write your own greeting or "
    "salutation. "

    "PLACEHOLDER RULES: "
    "NEVER output a bracketed placeholder such as '[Recipient Name]', '[Name]', "
    "'[Company]', or '[Your Name]'. If a detail is unknown, omit it or rephrase "
    "neutrally. Do not invent company information, names, phone numbers, or emails. "

    "CONTACT AND SIGN-OFF RULES: "
    "Write ONLY the three lines. Do NOT add a greeting, a sign-off, the sender's "
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
    # if prior_body:
    #     snippet = prior_body.strip()
    #     if len(snippet) > 900:  # keep the prompt bounded — the opening carries the intent
    #         snippet = snippet[:900] + " …"
    #     prior_block_lines.append(f"- Body:\n{snippet}")
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
    from the job context, with the same closing appended as the LLM path. The greeting is
    prepended deterministically by append_closing; a follow-up has no self-intro.
    `contact_block` (automated sends) is placed above the sign-off."""
    company = (job.get("company") or "").strip() or _GENERIC_COMPANY
    title = (job.get("job_title") or "").strip()

    greeting = build_greeting(job)
    posting = posting_phrase(job)  # platform from the job's source, else neutral
    reference = (
        f"Just following up on my earlier note about {posting} for {title} at {company}."
        if title
        else f"Just following up on my earlier note about {posting} at {company}."
    )
    body = "\n\n".join([reference, OFFER_LINE, CTA_LINE])
    return build_followup_subject(prior_subject, job), append_closing(
        body, sender_email, deck_url, contact_block, greeting=greeting,
    )


# ── LinkedIn ─────────────────────────────────────────────────────────────────

def build_linkedin_prompt(job: dict) -> str:
    return (
        "Write a short LinkedIn outreach message to this recruiter offering "
        "Sightspectrum's IT staffing support for the role they posted.\n\n"
        f"{_job_context(job)}\n\n"
        "Return ONLY the message text."
    )


def render_linkedin_fallback(company: str, platform: str = "") -> str:
    """`platform` is job_platform(job) — names where the job was posted (a LinkedIn
    message about a Naukri job says "your Naukri job post"); "" keeps it neutral."""
    name = (company or "").strip() or _GENERIC_COMPANY
    post = f"{platform} job post" if platform else "job post"
    return (
        f"Hi, I came across your {post} at {name}. Sightspectrum is an IT "
        "staffing firm supporting Contract, C2H, and Permanent hiring across Data "
        "Analytics, Cloud, and Digital technologies. We'd love to support your "
        "open requirements with quality-screened profiles. Could we connect to "
        "discuss your vendor onboarding process? — Sightspectrum Team"
    )