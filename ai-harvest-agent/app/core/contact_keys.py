"""Identity keys for counting *unique* contacts.

Several jobs often share one contact (a recruiter with ten postings, one poster on
three jobs), so "jobs with a phone" overstates how many people can be reached.
These keys decide when two stored values are the same contact. Every unique-contact
figure in the UI — the Harvested Jobs footer (GET /jobs/facets), the run-detail
header (mirrored in harvest-agent/src/lib/jobsData.js uniqueContactStats) and the
Phone logs "Total contacts" card — counts distinct keys, so they all agree.

A value whose key is "" (blank, or a phone with no digits such as "n/a") is not a
contact at all and never counts.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

_NON_DIGIT_RE = re.compile(r"\D")


def phone_key(raw: str | None) -> str:
    """Trailing 10 digits of a number (all its digits when shorter), so
    "+91 98400 12345" and "9840012345" are one contact. "" when it has no digits."""
    digits = _NON_DIGIT_RE.sub("", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


def email_key(raw: str | None) -> str:
    """Trimmed, lower-cased address ("" when blank)."""
    return (raw or "").strip().lower()


def linkedin_key(raw: str | None) -> str:
    """Profile URL without scheme, "www.", query, fragment or trailing slash, lower-cased."""
    url = (raw or "").strip().lower().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    url = re.sub(r"^https?://", "", url)
    return url[4:] if url.startswith("www.") else url


def poc_key(name: str | None, company: str | None) -> str:
    """One POC = one (name, company) pair, so two "Priya"s at different companies
    stay two people. "" when there is no name."""
    n = " ".join((name or "").split()).lower()
    return f"{n}|{' '.join((company or '').split()).lower()}" if n else ""


def unique_contact_counts(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Distinct contacts across ``rows``. Each row is one job's contact values:
    ``{"emails": [...], "phones": [...], "linkedin": str, "poc_name": str,
    "company": str}`` — every email/phone the job shows (poster, recruiter and
    company HR contact), not only the primary one.

    Returns ``{"unique_emails", "unique_phones", "unique_linkedin", "unique_pocs"}``."""
    emails: set[str] = set()
    phones: set[str] = set()
    linkedins: set[str] = set()
    pocs: set[str] = set()
    for row in rows:
        emails.update(k for k in map(email_key, row.get("emails") or ()) if k)
        phones.update(k for k in map(phone_key, row.get("phones") or ()) if k)
        if k := linkedin_key(row.get("linkedin")):
            linkedins.add(k)
        if k := poc_key(row.get("poc_name"), row.get("company")):
            pocs.add(k)
    return {"unique_emails": len(emails), "unique_phones": len(phones),
            "unique_linkedin": len(linkedins), "unique_pocs": len(pocs)}
