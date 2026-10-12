"""Grounding checks for contact values proposed by an LLM.

Data-integrity rule (CLAUDE.md): an email / phone / name is only stored when it
is written in a real public source — never predicted. Every value an LLM
returns is therefore verified against the source text(s) it was given, and
anything that cannot be found there is rejected. All checks are pure and
deterministic; each returns the value to store (taken from the SOURCE spelling
where possible) or None.
"""
from __future__ import annotations

import re
from typing import Iterable

from app.core.contact_normalize import EMAIL_CANDIDATE_RE, clean_email_candidate
from app.core.iana_tlds import IANA_TLDS

_LOCAL_CHARS = "A-Za-z0-9._%+-"
# Obfuscated separators accepted when de-obfuscating "name [at] acme [dot] com".
_AT_SEP = r"(?:\s*[\[({<]\s*at\s*[\])}>]\s*|\s+at\s+|\s*@\s*)"
_DOT_SEP = r"(?:\s*[\[({<]\s*dot\s*[\])}>]\s*|\s+dot\s+|\s*\.\s*)"
# The de-obfuscated domain must END there — not continue with another label
# ("acme(dot)co(dot)in" must not ground "x@acme.co"). A sentence-ending
# ". Next" is fine: domain dots are never followed by whitespace.
_DEOB_END = (
    r"(?![A-Za-z0-9])(?!\.[A-Za-z0-9])"
    r"(?!\s*[\[({<]\s*dot\s*[\])}>])(?!\s+dot\s+[A-Za-z0-9])"
)
# Phone-like digit runs in a source (digits with common separators).
_PHONE_RUN_RE = re.compile(r"\+?\d[\d \t\-().]{6,24}\d")
_PHONE_MIN_DIGITS = 8
_WS_RE = re.compile(r"\s+")


def _sources(texts: Iterable[str | None]) -> list[str]:
    return [t for t in texts if t]


def ground_email(candidate: str | None, sources: Iterable[str | None]) -> tuple[str | None, str]:
    """Verify an LLM-proposed address against ``sources``.

    Returns ``(address, how)`` — ``how`` is "verbatim", "glued_prefix" or
    "deobfuscated" on success, else ``(None, reason)``. Accepted when:
      * it equals (case-insensitively) an address-shaped run in a source, or
        that run after clean_email_candidate() trimming; or
      * it is a prefix of such a run whose remainder starts with a letter
        (text glued onto the address) and it ends on a real TLD; or
      * de-obfuscation: its local part and every domain label occur, in that
        order, joined only by at/dot separators ("[at]", "(dot)", " at ", …).
    The returned address uses the source's own spelling.
    """
    cand = (candidate or "").strip().strip("<>").strip()
    if cand.lower().startswith("mailto:"):
        cand = cand[7:]
    if not EMAIL_CANDIDATE_RE.fullmatch(cand):
        return None, "malformed"
    cand_l = cand.lower()
    srcs = _sources(sources)

    for src in srcs:
        for m in EMAIL_CANDIDATE_RE.finditer(src):
            raw = m.group(0)
            raw_l = raw.lower()
            if raw_l == cand_l:
                return raw, "verbatim"
            trimmed = clean_email_candidate(raw)
            if trimmed and trimmed.lower() == cand_l:
                return trimmed, "verbatim"
            if raw_l.startswith(cand_l):
                # Only at a visible glue boundary: lower-case TLD then an
                # upper-case letter ("…comCTC"). A cut between two lower-case
                # letters ("…co|mctc") is a truncated domain, not glue.
                rest = raw[len(cand):]
                tld = cand.rsplit(".", 1)[-1]
                if rest[:1].isupper() and tld[-1:].islower() and tld.lower() in IANA_TLDS:
                    return raw[: len(cand)], "glued_prefix"

    local, _, domain = cand.partition("@")
    labels = domain.split(".")
    pattern = (
        rf"(?<![{_LOCAL_CHARS}])({re.escape(local)}){_AT_SEP}"
        + _DOT_SEP.join(f"({re.escape(label)})" for label in labels)
        + _DEOB_END
    )
    deob = re.compile(pattern, re.I)
    for src in srcs:
        m = deob.search(src)
        if m:
            parts = m.groups()
            return f"{parts[0]}@{'.'.join(parts[1:])}", "deobfuscated"
    return None, "not_in_source"


def ground_phone(candidate: str | None, sources: Iterable[str | None]) -> tuple[str | None, str]:
    """Verify an LLM-proposed phone against the phone-like digit runs of the
    sources. Its digits must equal a contiguous run of digit groups ("+91 98765
    43210" -> "919876543210" or "9876543210"), optionally minus a ≤3-digit
    country/trunk prefix of the first group ("+919876543210" -> "9876543210").
    A country code the source never wrote is therefore rejected, and so is an
    arbitrary slice out of the middle of a longer number. Returns the
    candidate's digits (``+`` kept only when the source wrote it)."""
    digits = re.sub(r"\D", "", candidate or "")
    if len(digits) < _PHONE_MIN_DIGITS:
        return None, "too_short"
    for src in _sources(sources):
        for m in _PHONE_RUN_RE.finditer(src):
            run = m.group(0)
            groups = re.findall(r"\d+", run)
            for i in range(len(groups)):
                acc = ""
                for j in range(i, len(groups)):
                    acc += groups[j]
                    if len(acc) < len(digits):
                        continue
                    if acc == digits:
                        plus = "+" if i == 0 and run.startswith("+") else ""
                        return plus + digits, "verbatim"
                    if acc.endswith(digits) and len(acc) - len(digits) <= 3 \
                            and len(acc) - len(digits) < len(groups[i]):
                        return digits, "verbatim_without_prefix"
                    break
    return None, "not_in_source"


def ground_text(candidate: str | None, sources: Iterable[str | None],
                *, max_len: int = 120) -> tuple[str | None, str]:
    """Verify a short free-text value (person name, designation, company):
    whitespace-normalized, case-insensitive, whole-word substring of a source.
    Returns the source's own spelling."""
    value = _WS_RE.sub(" ", (candidate or "").strip()).strip(" ,;:-")
    if len(value) < 2:
        return None, "empty"
    if len(value) > max_len:
        return None, "too_long"
    if "@" in value:
        return None, "looks_like_email"
    words = [re.escape(w) for w in value.split(" ")]
    pattern = re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.I)
    for src in _sources(sources):
        m = pattern.search(src)
        if m:
            return _WS_RE.sub(" ", m.group(0)), "verbatim"
    return None, "not_in_source"
