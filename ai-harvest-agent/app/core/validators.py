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
    # fullmatch anchors both ends so "sightspectrum.com.evil.com" or
    # "sightspectrum.co.in" can never match — the domain must be exact.
    return re.compile(rf"^[^@\s]+@{re.escape(domain)}$", re.IGNORECASE)


def validate_company_email(email: str) -> str:
    """Return the (lightly normalized) email if it belongs to the allowed
    company domain, otherwise raise ``ValueError``."""
    settings = get_settings()
    email = email.strip()
    pattern = _domain_pattern(settings.allowed_email_domain.lower())
    if not pattern.fullmatch(email):
        raise ValueError(
            f"Email must be a valid @{settings.allowed_email_domain} address"
        )
    return email.lower()
