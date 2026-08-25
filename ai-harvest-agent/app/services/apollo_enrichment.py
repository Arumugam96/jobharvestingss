"""Shared Apollo fallback — the single place the three contact-discovery agents
(linkedin_agent, recruiter_contact_agent, prospect_intelligence_agent) reach for
Apollo *after* their own LLM/regex extraction has come up empty.

Keeping the gate/cooldown decision here (rather than in each agent) means the
credit-conservation rules live in exactly one spot:

  * no-op when Apollo isn't configured or there's no LinkedIn URL;
  * only reveal a channel we don't already have (email always eligible; phone
    only when settings.apollo_reveal_phone is on);
  * skip if this profile was tried within settings.apollo_recheck_days.

The helper only *decides + calls* Apollo and returns what it found. Persistence
stays in each agent's existing save_enrichment(...) call, which now takes
enrichment_source/apollo_attempted so the recruiter row records provenance and
the cooldown timestamp.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog

from app.config import Settings
from app.services.apollo_client import ApolloAPIError, ApolloClient

logger = structlog.get_logger(__name__)


@dataclass
class ApolloFallbackResult:
    email: str = ""
    phone: str = ""
    matched: bool = False
    attempted: bool = False          # True once an Apollo call was actually issued
    source: str = ""                 # "apollo" when Apollo supplied the email

    @property
    def enrichment_source(self) -> str:
        return self.source


_SKIP = ApolloFallbackResult()  # attempted=False, nothing found


def _aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def apollo_contact_fallback(
    *,
    settings: Settings,
    linkedin_url: str,
    person_name: str = "",
    company_name: str = "",
    company_domain: str = "",
    already_email: bool = False,
    already_phone: bool = False,
    apollo_enriched_at: datetime | None = None,
    client: ApolloClient | None = None,
) -> ApolloFallbackResult:
    """Try Apollo for a still-missing email (and optionally phone). Returns an
    ApolloFallbackResult; callers merge .email/.phone into their own result and
    pass .attempted / .enrichment_source through to save_enrichment(...)."""
    if not settings.apollo_api_key or not linkedin_url:
        return _SKIP

    want_email = not already_email
    want_phone = settings.apollo_reveal_phone and not already_phone
    if not want_email and not want_phone:
        return _SKIP  # nothing left worth a credit

    # Recheck cooldown — don't re-bill/re-hit a profile tried recently.
    last = _aware_utc(apollo_enriched_at)
    if last is not None:
        age = datetime.now(timezone.utc) - last
        if age < timedelta(days=settings.apollo_recheck_days):
            logger.info("apollo_skip_cooldown", linkedin_url=linkedin_url, age_days=age.days)
            return _SKIP

    # Explicit marker that the Apollo fallback tier was reached for this profile
    # (i.e. the LLM/regex extraction came up empty and the gate/cooldown passed).
    logger.info(
        "apollo_fallback_triggered",
        linkedin_url=linkedin_url,
        person=person_name,
        company=company_name,
        want_email=want_email,
        want_phone=want_phone,
    )

    client = client or ApolloClient(settings)
    try:
        person = await client.enrich_person_by_linkedin(
            linkedin_url,
            reveal_email=want_email,
            reveal_phone=want_phone,
            name=person_name,
            company=company_name,
            domain=company_domain,
        )
    except ApolloAPIError as exc:
        # A completed-but-failed attempt still counts as "attempted" so the
        # cooldown backs us off a persistently-failing profile (transient errors
        # were already retried inside the client).
        logger.warning("apollo_fallback_failed", linkedin_url=linkedin_url, error=str(exc))
        return ApolloFallbackResult(attempted=True)

    email = person.email or "" if want_email else ""
    phone = person.phone or "" if want_phone else ""
    logger.info(
        "apollo_fallback_result",
        linkedin_url=linkedin_url,
        matched=person.matched,
        email_found=bool(email),
        phone_found=bool(phone),
    )
    return ApolloFallbackResult(
        email=email,
        phone=phone,
        matched=person.matched,
        attempted=True,
        source="apollo" if email else "",
    )
