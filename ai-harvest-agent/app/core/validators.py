"""Centralized company-email validation.

The allowed domain is read from ``Settings.allowed_email_domain`` so it is
configurable via the ``ALLOWED_EMAIL_DOMAIN`` env var, but a single regex
here is the one source of truth for what counts as a valid company email —
every schema/route/service must go through :func:`validate_company_email`
instead of re-implementing the check.
"""
from __future__ import annotations

import re
from functools import lru_cache

from app.config import get_settings


@lru_cache
def _domain_pattern(domain: str) -> re.Pattern[str]:
    # Accept the company's second-level label ("sightspectrum") under ANY
    # single-label TLD — sightspectrum.com / .in / .org / .io …. The base label
    # is derived from settings.allowed_email_domain. Restricting the TLD to a
    # single dotless label ([a-z]{2,}) still blocks look-alikes whose
    # registrable domain isn't sightspectrum.*: multi-label suffixes like
    # "sightspectrum.co.in" and "sightspectrum.com.evil.com", and the
    # subdomain-of-evil case "sightspectrum.evil.com", can never match.
    base = re.escape(domain.split(".", 1)[0])
    return re.compile(rf"^[^@\s]+@{base}\.[a-z]{{2,}}$", re.IGNORECASE)


def validate_company_email(email: str) -> str:
    """Return the (lightly normalized) email if it belongs to the company —
    the ``sightspectrum`` second-level domain under any TLD — otherwise raise
    ``ValueError``."""
    settings = get_settings()
    email = email.strip()
    pattern = _domain_pattern(settings.allowed_email_domain.lower())
    if not pattern.fullmatch(email):
        base = settings.allowed_email_domain.split(".", 1)[0]
        raise ValueError(
            f"Email must be a valid @{base} company address "
            f"(e.g. name@{settings.allowed_email_domain})"
        )
    return email.lower()
