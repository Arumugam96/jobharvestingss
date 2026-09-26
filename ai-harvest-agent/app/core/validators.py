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

from app.models.tenant import EMAIL_DOMAIN_TENANTS


@lru_cache
def _allowed_pattern(labels: tuple[str, ...]) -> re.Pattern[str]:
    # Accept any allowed workspace second-level label (internal + each onboarded
    # client, see EMAIL_DOMAIN_TENANTS) under ANY single-label TLD — .com / .in /
    # .io …. Restricting the TLD to a single dotless label ([a-z]{2,}) still
    # blocks look-alikes whose registrable domain isn't one of ours: multi-label
    # suffixes ("...co.in", "...com.evil.com") and subdomain-of-evil
    # ("...evil.com") can never match.
    alt = "|".join(re.escape(label) for label in labels)
    return re.compile(rf"^[^@\s]+@(?:{alt})\.[a-z]{{2,}}$", re.IGNORECASE)


def validate_company_email(email: str) -> str:
    """Return the (lightly normalized) email if it belongs to an allowed
    workspace domain (internal or an onboarded client), else raise
    ``ValueError``. The set of allowed domains is EMAIL_DOMAIN_TENANTS."""
    email = email.strip()
    labels = tuple(sorted(EMAIL_DOMAIN_TENANTS.keys()))
    if not _allowed_pattern(labels).fullmatch(email):
        raise ValueError(
            "Email must be a valid company or client workspace address"
        )
    return email.lower()
