"""Defensive post-extraction cleaning for recruiter email / phone values.

The LLM extraction prompts already ask for clean, verbatim contact details, but
provider misses still slip through — e.g. a masked phone came back as
``+\\87*******``. These helpers are a second, deterministic layer applied right
after extraction (where values were previously only ``.strip()``-ed) so noise
never reaches the DB. Both return ``None`` when the value is missing, masked,
malformed, or too short to be a real contact.
"""
from __future__ import annotations

import re

# Placeholder strings some models emit instead of a real null.
_NULL_TOKENS = {"", "null", "none", "n/a", "na", "not found", "not available", "-", "—"}

# Characters that betray a masked / redacted value (asterisks) or extraction
# noise (backslashes). Any of these ⇒ reject outright.
_MASK_CHARS = ("*", "\\")

# A phone in E.164 is at most 15 digits; anything below ~8 is a fragment.
_PHONE_MIN_DIGITS = 8
_PHONE_MAX_DIGITS = 15

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Kept for stripping decorative separators from a phone before digit-counting.
_PHONE_STRIP_RE = re.compile(r"[\s()\-. ]")


def _is_null_token(value: str) -> bool:
    return value.strip().lower() in _NULL_TOKENS


def normalize_phone(raw: str | None) -> str | None:
    """Return a cleaned phone string, or ``None`` if it is missing/masked/invalid.

    - Rejects values containing masking noise (``*`` / ``\\``) — e.g. ``+\\87*******``.
    - Strips spaces, parens, dashes and dots; preserves a single leading ``+``.
    - After cleaning, everything left must be digits; requires 8–15 digits.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if _is_null_token(value):
        return None
    if any(ch in value for ch in _MASK_CHARS):
        return None

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
    if _is_null_token(value):
        return None
    if any(ch in value for ch in _MASK_CHARS):
        return None
    value = value.strip("<>").strip()
    if not _EMAIL_RE.match(value):
        return None
    return value
