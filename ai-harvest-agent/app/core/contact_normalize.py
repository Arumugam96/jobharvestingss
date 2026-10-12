"""Defensive post-extraction cleaning for recruiter email / phone values, plus
the shared free-text / HTML email extractor.

Extraction never invents an address: every value returned is a verbatim
substring of the source (at most trimmed of text glued onto its end).
"""
from __future__ import annotations

import html as _html
import re
from urllib.parse import unquote

from app.core.iana_tlds import IANA_TLDS


# A phone in E.164 is at most 15 digits; anything below ~8 is a fragment.
_PHONE_MIN_DIGITS = 8
_PHONE_MAX_DIGITS = 15

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Kept for stripping decorative separators from a phone before digit-counting.
_PHONE_STRIP_RE = re.compile(r"[\s()\-. ]")



def normalize_phone(raw: str | None) -> str | None:
    """Return a cleaned phone string, or ``None`` if it is missing/masked/invalid.

    - Rejects values containing masking noise (``*`` / ``\\``) — e.g. ``+\\87*******``.
    - Strips spaces, parens, dashes and dots; preserves a single leading ``+``.
    - After cleaning, everything left must be digits; requires 8–15 digits.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    has_plus = value.startswith("+")
    stripped = _PHONE_STRIP_RE.sub("", value)
    if has_plus:
        stripped = "+" + stripped.lstrip("+")

    digits = stripped[1:] if stripped.startswith("+") else stripped
    if not digits.isdigit():
        return None
    if not (_PHONE_MIN_DIGITS <= len(digits) <= _PHONE_MAX_DIGITS):
        return None
    return ("+" + digits) if has_plus else digits


def normalize_email(raw: str | None) -> str | None:
    """Return a cleaned email string, or ``None`` if missing/masked/malformed.

    - Rejects masked values (``*`` / ``\\``, e.g. ``m***********p.com``).
    - Requires a basic ``local@domain.tld`` shape.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    value = value.strip("<>").strip()
    if not _EMAIL_RE.match(value):
        return None
    # Deliberately NOT lower-casing or trimming glued text here:
    # suppression_service keys suppressions and HMACs unsubscribe tokens on
    # normalize_email(), so changing its output would orphan existing rows /
    # tokens. Glued-text trimming happens at extraction time instead
    # (clean_email_candidate / extract_emails below).
    return value


# ── Email extraction from free text / HTML ─────────────────────────────────────

# A loose candidate: local@domain.tld where the TLD run is greedy, so it may
# swallow letters glued on after the address ("x@infosys.comCTCIndustry") —
# clean_email_candidate() trims those back off.
EMAIL_CANDIDATE_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Lower-case TLD run followed by an upper-case letter ("comCTC") or an all-caps
# TLD followed by a Capitalized word ("COMContact") — text glued onto the address.
_CASE_GLUE_LOWER_RE = re.compile(r"^([a-z]{2,})(?=[A-Z])")
_CASE_GLUE_UPPER_RE = re.compile(r"^([A-Z]{2,})(?=[A-Z][a-z])")
_CAPITALIZED_WORD_RE = re.compile(r"^[A-Z][a-z]{3,}$")
_MAX_TLD_LEN = 24
_MAILTO_RE = re.compile(r"""href\s*=\s*["']?\s*mailto:([^"'\s>?]+)""", re.I)
_TEL_RE = re.compile(r"""href\s*=\s*["']?\s*tel:([^"'>]+)""", re.I)
_TAG_RE = re.compile(r"<[^>]*>")


def _resolve_tld(labels: list[str]) -> list[str] | None:
    """Return ``labels`` with the last one cut back to a real TLD, or None.

    Order of evidence (strongest first):
      a. case glue inside the last label: "comCTCIndustryTesting" -> "com";
      b. a Capitalized word after a dot on an all-lowercase domain whose
         previous label is itself a TLD: "infosys.com.Contact" -> "infosys.com";
      c. the label is a real IANA TLD -> kept as-is ("co", "community");
      d. letters glued onto a real TLD: "comctc" -> longest real-TLD prefix "com";
      e. no real TLD at all -> drop the label and retry with the previous one.
    """
    labels = list(labels)
    while len(labels) >= 2:
        tld = labels[-1]
        head_is_lower = not any(ch.isupper() for ch in "".join(labels[:-1]))
        m = _CASE_GLUE_LOWER_RE.match(tld) or _CASE_GLUE_UPPER_RE.match(tld)
        if m:
            tld = m.group(1)
        elif (head_is_lower and len(labels) >= 3 and _CAPITALIZED_WORD_RE.match(tld)
              and labels[-2].lower() in IANA_TLDS):
            labels.pop()
            continue
        low = tld.lower()
        if low in IANA_TLDS:
            labels[-1] = tld
            return labels
        for n in range(min(len(low) - 1, _MAX_TLD_LEN), 1, -1):
            if low[:n] in IANA_TLDS:
                labels[-1] = tld[:n]
                return labels
        labels.pop()
    return None


def clean_email_candidate(raw: str | None) -> str | None:
    """Trim a regex email candidate back to the real address, or None.

    Removes text glued onto the end of the domain (see _resolve_tld) and
    validates the basic shape (local part ≤ 64 chars, no empty/oversized or
    hyphen-edged labels, a real IANA TLD). The result is always a verbatim
    prefix of ``raw`` — nothing is ever added or predicted.
    """
    if not raw:
        return None
    value = str(raw).strip().strip("<>").strip()
    if value.lower().startswith("mailto:"):
        value = value[7:]
    m = EMAIL_CANDIDATE_RE.fullmatch(value)
    if not m:
        return None
    local, _, domain = value.partition("@")
    if not local or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    labels = domain.split(".")
    if any(not label for label in labels):
        return None
    resolved = _resolve_tld(labels)
    if resolved is None:
        return None
    if any(len(label) > 63 or label.startswith("-") or label.endswith("-") for label in resolved):
        return None
    return f"{local}@{'.'.join(resolved)}"


def html_contact_text(html: str | None) -> str:
    """Text of ``html`` for contact extraction only: EVERY tag (inline ones
    included) becomes a line break, so text from adjacent elements can never
    glue onto an address ("x@acme.com</a><b>CTC</b>" -> "x@acme.com\\nCTC").
    Entities are unescaped. Not for display."""
    if not html:
        return ""
    return _html.unescape(_TAG_RE.sub("\n", str(html)))


def mailto_addresses(html: str | None) -> list[str]:
    """Raw ``mailto:`` targets of every link in ``html`` (URL-decoded, query dropped)."""
    if not html:
        return []
    return [unquote(_html.unescape(m.group(1))).strip() for m in _MAILTO_RE.finditer(str(html))]


def tel_numbers(html: str | None) -> list[str]:
    """Raw ``tel:`` targets of every link in ``html``."""
    if not html:
        return []
    return [unquote(_html.unescape(m.group(1))).strip() for m in _TEL_RE.finditer(str(html))]


def find_emails(text: str | None) -> list[str]:
    """Every distinct address in free text, in order, each trimmed by
    clean_email_candidate (case-insensitive de-dupe, first spelling kept)."""
    out: list[str] = []
    seen: set[str] = set()
    for m in EMAIL_CANDIDATE_RE.finditer(text or ""):
        email = clean_email_candidate(m.group(0))
        if email and email.lower() not in seen:
            seen.add(email.lower())
            out.append(email)
    return out


def extract_emails(text: str | None = "", html: str | None = "") -> list[str]:
    """Every distinct address in a description, most trustworthy source first:
    ``mailto:`` link targets, then the tag-boundary text of ``html`` (see
    html_contact_text), then the already-formatted ``text``. Callers apply their
    own junk filters."""
    out: list[str] = []
    seen: set[str] = set()
    candidates = [clean_email_candidate(m) for m in mailto_addresses(html)]
    candidates += find_emails(html_contact_text(html))
    candidates += find_emails(text)
    for email in candidates:
        if email and email.lower() not in seen:
            seen.add(email.lower())
            out.append(email)
    return out
