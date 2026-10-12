"""LLM enrichment for scraped job descriptions, layered on top of (never
instead of) deterministic parsing.

1. Grounded contact extraction — the LLM reads the description and proposes
   recruiter name / designation / company / emails / phones. Every value is
   then verified against the source (app/core/contact_grounding.py) and run
   through the usual normalizers + junk filters; anything not literally in the
   source is dropped and logged. Only called when the description shows a
   contact signal (has_contact_signals) — a description without one cannot
   hold a contact, so skipping it loses nothing.
2. Description restructuring — ONLY for flat, run-on descriptions
   (is_flat_description) that deterministic formatting could not split. The
   result is accepted only if its word sequence equals the input's
   (words_preserved); otherwise the deterministic text stands.

Source-agnostic: the Naukri agent is the first caller (see
NaukriAgent._schedule_llm_enrichment); LinkedIn / Dice can reuse it.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import structlog

from app.core.contact_grounding import ground_email, ground_phone, ground_text
from app.core.contact_normalize import clean_email_candidate, normalize_email, normalize_phone
from app.core.text_formatting import format_job_description
from app.models.harvest_run import LlmCallType
from app.prompts.jd_enrichment_prompts import (
    JD_CONTACT_SCHEMA,
    JD_CONTACT_SYSTEM_PROMPT,
    JD_RESTRUCTURE_SCHEMA,
    JD_RESTRUCTURE_SYSTEM_PROMPT,
    RESTRUCTURE_MAX_CHARS,
    build_contact_content,
    build_restructure_content,
)

logger = structlog.get_logger(__name__)


# ── Gates (pure) ───────────────────────────────────────────────────────────────

_CONTACT_CUE_RE = re.compile(
    r"@|mailto:|tel:|[\[({<]\s*at\s*[\])}>]|"
    r"\b(?:contact|e-?mail|mail|reach|whats\s*app|call|mobile|phone|"
    r"(?:send|share|forward|drop|mail)\s+(?:us\s+)?(?:your\s+|the\s+|an?\s+)?(?:updated\s+)?"
    r"(?:resume|cv|profile|profiles))\b",
    re.I,
)
# A phone-like run of ≥10 digits (optionally separated by spaces / dashes / dots).
_PHONE_LIKE_RE = re.compile(r"(?:\+?\d[ \t\-().]?){10,}")

FLAT_MIN_CHARS = 500
FLAT_LONGEST_LINE_CHARS = 500


def has_contact_signals(*texts: str | None) -> bool:
    """True when any text shows a contact cue: an "@", "[at]"/"(at)", a
    mailto:/tel: link, a phone-like digit run, or a cue word (contact, email,
    mail, reach, WhatsApp, call, phone, "send/share your resume/CV")."""
    for t in texts:
        if t and (_CONTACT_CUE_RE.search(t) or _PHONE_LIKE_RE.search(t)):
            return True
    return False


def is_flat_description(text: str | None) -> bool:
    """True for a long description that deterministic formatting left as a
    run-on blob: ≥ FLAT_MIN_CHARS, no bullet lines, and one line of
    ≥ FLAT_LONGEST_LINE_CHARS holding at least half of the text."""
    t = (text or "").strip()
    if len(t) < FLAT_MIN_CHARS:
        return False
    lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
    if any(ln.startswith("• ") for ln in lines):
        return False
    longest = max(len(ln) for ln in lines)
    return longest >= FLAT_LONGEST_LINE_CHARS and longest * 2 >= len(t)


_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def word_sequence(text: str | None) -> list[str]:
    """Lower-cased words/numbers in order — punctuation, bullets and whitespace dropped."""
    return _WORD_RE.findall((text or "").lower())


def words_preserved(original: str | None, candidate: str | None) -> bool:
    return word_sequence(original) == word_sequence(candidate)


def provider_ready(provider: str, settings: Any) -> bool:
    """Whether an LLMService provider has the credentials / endpoint it needs.
    A local (Ollama) provider only needs a URL — reachability is learned on the
    first call (LLMService's circuit breaker)."""
    if provider == "claude":
        return bool(getattr(settings, "anthropic_api_key", ""))
    if provider == "openrouter":
        return bool(getattr(settings, "openrouter_api_key", ""))
    return bool(getattr(settings, "local_llm_url", ""))


def naukri_llm_settings(settings: Any) -> Any:
    """``settings`` with the extraction provider swapped for NAUKRI_LLM_MODEL /
    NAUKRI_LLM_FALLBACK_MODEL when set, so this pass can use a stronger model
    than the app-wide EXTRACTION_LLM_MODEL. Unset → ``settings`` unchanged."""
    model = (getattr(settings, "naukri_llm_model", "") or "").strip()
    if not model:
        return settings
    fallback = (getattr(settings, "naukri_llm_fallback_model", "") or "").strip()
    return settings.model_copy(
        update={"extraction_llm_model": model, "extraction_fallback_model": fallback},
    )


def llm_configured(settings: Any) -> bool:
    """True when at least one provider in the extraction chain
    (EXTRACTION_LLM_MODEL + EXTRACTION_FALLBACK_MODEL) is usable."""
    from app.services.llm_service import LLMService
    try:
        chain = LLMService(settings).provider_chain()
    except Exception:
        return False
    return any(provider_ready(provider, settings) for provider, _model in chain)


# ── Grounding of the LLM's contact proposal ─────────────────────────────────────

_PLACEHOLDER_NAMES = frozenset({
    "hr", "hr team", "hr department", "recruiter", "recruiters", "recruitment team",
    "talent acquisition", "talent acquisition team", "ta team", "hiring team",
    "hiring manager", "team", "admin", "careers",
})


@dataclass
class GroundedContacts:
    """LLM-proposed contacts that survived grounding + normalization."""
    name:        str | None = None
    designation: str | None = None
    company:     str | None = None
    emails:      list[tuple[str, str]] = field(default_factory=list)  # (address, "personal"|"generic")
    phones:      list[str] = field(default_factory=list)
    rejected:    list[dict[str, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.name or self.designation or self.company or self.emails or self.phones)


def ground_llm_contacts(
    raw: Any,
    sources: Iterable[str | None],
    *,
    email_filter: Callable[[str | None], str | None] = normalize_email,
    job_ref: str = "",
) -> GroundedContacts:
    """Verify every value of an LLM contact proposal against ``sources``.

    Emails: ground_email (verbatim / glued-prefix / strict de-obfuscation),
    then clean_email_candidate trimming and ``email_filter`` (normalizer + the
    caller's junk filter). Phones: ground_phone then normalize_phone. Name /
    designation / company: ground_text (whole-word substring), placeholders
    ("HR Team") refused. Rejections are logged with the reason."""
    srcs = [s for s in sources if s]
    out = GroundedContacts()

    def _reject(fld: str, value: Any, reason: str) -> None:
        out.rejected.append({"field": fld, "value": str(value)[:120], "reason": reason})
        logger.info("jd_llm_contact_rejected", job=job_ref, field=fld,
                    value=str(value)[:120], reason=reason)

    if not isinstance(raw, dict):
        return out

    for item in raw.get("emails") or []:
        if isinstance(item, dict):
            addr, kind = item.get("address") or item.get("email"), item.get("type")
        else:
            addr, kind = item, None
        if not addr:
            continue
        grounded, how = ground_email(str(addr), srcs)
        if not grounded:
            _reject("email", addr, how)
            continue
        final = email_filter(clean_email_candidate(grounded))
        if not final:
            _reject("email", addr, "filtered")
            continue
        if all(final.lower() != e.lower() for e, _ in out.emails):
            out.emails.append((final, "personal" if str(kind).lower() == "personal" else "generic"))

    for phone in raw.get("phones") or []:
        if not phone:
            continue
        grounded, how = ground_phone(str(phone), srcs)
        if not grounded:
            _reject("phone", phone, how)
            continue
        final = normalize_phone(grounded)
        if not final:
            _reject("phone", phone, "invalid")
            continue
        if final not in out.phones:
            out.phones.append(final)

    for key, attr in (("recruiter_name", "name"), ("recruiter_designation", "designation"),
                      ("recruiter_company", "company")):
        value = raw.get(key)
        if not value or not isinstance(value, str):
            continue
        if attr == "name" and (value.strip().lower() in _PLACEHOLDER_NAMES
                               or any(ch.isdigit() for ch in value)):
            _reject(key, value, "placeholder")
            continue
        grounded, how = ground_text(value, srcs)
        if grounded:
            setattr(out, attr, grounded)
        else:
            _reject(key, value, how)
    return out


def choose_email(deterministic: str | None, grounded: GroundedContacts) -> tuple[str | None, str]:
    """Reconcile the deterministic (regex) email with the grounded LLM emails.

    Returns ``(email, origin)`` with origin "deterministic" | "llm" | "none":
      1. the LLM proposes a SHORTER verbatim prefix of the regex match (regex
         swallowed glued text: "x@infosys.comCTC" vs "x@infosys.com") → LLM;
      2. otherwise a grounded PERSONAL LLM address beats a deterministic one
         that is not personal (the LLM can tell a recruiter's own mailbox from
         a careers@ inbox — the regex just takes the first address);
      3. otherwise the deterministic value stands; with none, the first
         grounded LLM address (personal before generic)."""
    llm_all = [e for e, _ in grounded.emails]
    llm_personal = [e for e, kind in grounded.emails if kind == "personal"]
    if deterministic:
        det_l = deterministic.lower()
        for e in llm_all:
            if det_l.startswith(e.lower()) and len(e) < len(deterministic):
                return e, "llm"
        det_is_personal = any(e.lower() == det_l for e in llm_personal)
        if llm_personal and not det_is_personal:
            return llm_personal[0], "llm"
        return deterministic, "deterministic"
    if llm_personal:
        return llm_personal[0], "llm"
    if llm_all:
        return llm_all[0], "llm"
    return None, "none"


# ── LLM calls ──────────────────────────────────────────────────────────────────

async def llm_extract_contacts(
    llm: Any,
    description_text: str,
    *,
    sources: Iterable[str | None],
    link_targets: list[str] | None = None,
    job_ref: str = "",
    timeout_s: float = 45.0,
    email_filter: Callable[[str | None], str | None] = normalize_email,
) -> GroundedContacts:
    """One bounded LLM call → grounded contacts. Raises on LLM error/timeout
    (callers keep their deterministic result)."""
    raw = await asyncio.wait_for(
        llm.extract_json(
            content=build_contact_content(description_text, link_targets),
            schema_description=JD_CONTACT_SCHEMA,
            system=JD_CONTACT_SYSTEM_PROMPT,
            job_url=job_ref or None,
            call_type=LlmCallType.CONTACT_HARVEST,
        ),
        timeout=timeout_s,
    )
    grounded = ground_llm_contacts(raw, sources, email_filter=email_filter, job_ref=job_ref)
    logger.info(
        "jd_llm_contacts_grounded", job=job_ref, emails=len(grounded.emails),
        phones=len(grounded.phones), name=bool(grounded.name), rejected=len(grounded.rejected),
    )
    return grounded


async def llm_restructure_description(
    llm: Any,
    text: str,
    *,
    job_ref: str = "",
    timeout_s: float = 45.0,
) -> str | None:
    """One bounded LLM call that only re-lines a flat description. Returns the
    formatted text, or None when the input is too long, the output changed any
    word (content-preservation check), or it added no structure. Raises on LLM
    error/timeout."""
    if not text or len(text) > RESTRUCTURE_MAX_CHARS:
        return None
    raw = await asyncio.wait_for(
        llm.extract_json(
            content=build_restructure_content(text),
            schema_description=JD_RESTRUCTURE_SCHEMA,
            system=JD_RESTRUCTURE_SYSTEM_PROMPT,
            job_url=job_ref or None,
            call_type=LlmCallType.JOB_HARVEST,
        ),
        timeout=timeout_s,
    )
    out = raw.get("description") if isinstance(raw, dict) else None
    if not isinstance(out, str) or not out.strip():
        logger.info("jd_llm_restructure_rejected", job=job_ref, reason="empty")
        return None
    formatted = format_job_description(out)
    if not words_preserved(text, out) or not words_preserved(text, formatted):
        a, b = word_sequence(text), word_sequence(formatted)
        first_diff = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        logger.info(
            "jd_llm_restructure_rejected", job=job_ref, reason="words_changed",
            input_words=len(a), output_words=len(b), first_diff_at=first_diff,
        )
        return None
    if formatted.count("\n") <= text.strip().count("\n"):
        logger.info("jd_llm_restructure_rejected", job=job_ref, reason="no_structure_added")
        return None
    return formatted
