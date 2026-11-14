"""Shared Apollo fallback — the single place the three contact-discovery agents
(linkedin_agent, recruiter_contact_agent, prospect_intelligence_agent) reach for
Apollo *after* their own LLM/regex extraction has come up empty.

Keeping the gate/cooldown decision here (rather than in each agent) means the
credit-conservation rules live in exactly one spot:

  * no-op when Apollo isn't configured or there's no LinkedIn URL;
  * email only — phones are never requested here (they're requested after a sent
    outreach email: app/services/outreach_phone_service.py);
  * skip if Apollo was EVER tried for this profile (apollo_enriched_at set) — no
    re-sweeps. (Company / company-location lookups still use apollo_recheck_days.)

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
from app.core.company_size import band_from_employee_count
from app.core.hr_titles import HR_TITLES, hr_title_tier
from app.services.apollo_client import ApolloAPIError, ApolloClient, ApolloPersonResult

logger = structlog.get_logger(__name__)


@dataclass
class ApolloFallbackResult:
    email: str = ""
    phone: str = ""
    # Extra details Apollo returns alongside the match (no extra credit) —
    # merged onto the recruiter row by each caller's save_enrichment(...).
    secondary_email: str = ""
    company_linkedin_url: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    # ── Company/organization details from the same match (no extra credit) ──
    # `state`/`country` above are the PERSON's location; these company_* fields
    # are the ORGANIZATION's. company_size_band is Apollo's estimated_num_employees
    # mapped to the canonical "<band> employees" string (app/core/company_size.py),
    # so it reads identically to a LinkedIn-scraped size band.
    company_size_band: str = ""
    company_industry: str = ""
    company_domain: str = ""
    company_state: str = ""
    company_country: str = ""
    matched: bool = False
    attempted: bool = False          # True once an Apollo call was actually issued
    source: str = ""                 # "apollo" when Apollo supplied the email
    # Apollo person id — stored on recruiters.apollo_person_id so a later phone request
    # (outreach_phone_service) can match by id and a delivery can find the recruiter.
    person_id: str = ""

    @property
    def enrichment_source(self) -> str:
        """Alias of ``source``: "apollo" when Apollo supplied the email, else ""."""
        return self.source


_SKIP = ApolloFallbackResult()  # attempted=False, nothing found


def _aware_utc(dt: datetime | None) -> datetime | None:
    """Treat a naive datetime as UTC so it can be compared with ``now(timezone.utc)``."""
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
    """Try Apollo for a still-missing email. Returns an ApolloFallbackResult; callers
    merge .email into their own result and pass .attempted / .enrichment_source /
    .person_id through to save_enrichment(...).

    EMAIL-ONLY: harvest enrichment never asks Apollo for a phone (8 credits each).
    Phones are requested only after an initial outreach email was actually sent to an
    eligible recruiter — app/services/outreach_phone_service.py (settings.apollo_reveal_phone
    gates that step). ``already_phone`` is accepted for call compatibility only."""
    if not settings.apollo_api_key or not linkedin_url:
        return _SKIP

    if already_email:
        return _SKIP  # nothing left worth a credit

    # One lookup per profile, ever. Apollo bills a matched person even when it has no
    # email, and a miss almost never turns into a hit later (a 30-day re-sweep on
    # 2026-11-13 re-paid ~480 old misses for a 16% hit rate), so a profile Apollo has
    # already been asked about is never re-sent automatically. On-demand lookups go
    # through Recruiter Finder, which doesn't use this helper.
    last = _aware_utc(apollo_enriched_at)
    if last is not None:
        logger.info(
            "apollo_skip_already_tried", linkedin_url=linkedin_url,
            age_days=(datetime.now(timezone.utc) - last).days,
        )
        return _SKIP

    # Explicit marker that the Apollo fallback tier was reached for this profile
    # (i.e. the LLM/regex extraction came up empty and the gate/cooldown passed).
    logger.info(
        "apollo_fallback_triggered",
        linkedin_url=linkedin_url,
        person=person_name,
        company=company_name,
        want_email=True,
        want_phone=False,
    )

    client = client or ApolloClient(settings)
    try:
        person = await client.enrich_person_by_linkedin(
            linkedin_url,
            reveal_email=True,
            reveal_phone=False,
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

    email = person.email or ""
    logger.info(
        "apollo_fallback_result",
        linkedin_url=linkedin_url,
        matched=person.matched,
        email_found=bool(email),
        person_id=person.id or "",
    )
    org = person.organization
    return ApolloFallbackResult(
        email=email,
        # These ride along with the match (no extra reveal/credit), so pass them
        # through whenever Apollo matched — independent of the email reveal.
        secondary_email=person.secondary_email or "",
        company_linkedin_url=(org.linkedin_url if org else "") or "",
        address=person.address or "",
        city=person.city or "",
        state=person.state or "",
        country=person.country or "",
        # Organization details already present on the match — previously dropped.
        # estimated_num_employees → canonical band so it reads like a scraped size.
        company_size_band=band_from_employee_count(org.size) if org else "",
        company_industry=(org.industry if org else "") or "",
        company_domain=(org.domain if org else "") or "",
        company_state=(org.state if org else "") or "",
        company_country=(org.country if org else "") or "",
        matched=person.matched,
        attempted=True,
        source="apollo" if email else "",
        person_id=str(person.id or "") if person.matched else "",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Company + location HR-contact fallback
# ══════════════════════════════════════════════════════════════════════════════
# Used when a harvested job has NO resolvable recruiter email: find the best
# location-specific HR/recruiting contact for the job's (company, location) via
# Apollo — company → organization → HR people filtered by person_locations → reveal
# the first available email. ALWAYS location-constrained; there is NO company-wide
# fallback. Credit discipline (recheck cooldown + the global daily cap) lives here,
# same as apollo_contact_fallback above.


@dataclass
class CompanyContactResult:
    name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    company_domain: str = ""
    matched: bool = False            # True when Apollo resolved the organization
    attempted: bool = False          # True once a real Apollo search was issued (hit or miss)
    cap_reached: bool = False        # True when the daily budget blocked the search (retry later)


_COMPANY_CONTACT_SKIP = CompanyContactResult()  # attempted=False, nothing searched


async def apollo_company_contact_fallback(
    *,
    settings: Settings,
    company_name: str,
    location_terms: str,
    domain: str = "",
    attempted_at: datetime | None = None,
    reveal_cap: int | None = None,
    titles: list[str] | None = None,
    client: ApolloClient | None = None,
) -> CompanyContactResult:
    """Find the best location-specific HR/recruiting contact for a (company, location).

    ``location_terms`` is REQUIRED (the Apollo person_locations filter) — an empty value
    returns a skip rather than running an unconstrained, company-wide search. Honors the
    ``apollo_recheck_days`` cooldown vs ``attempted_at`` and the global daily credit cap.
    When the cap blocks the search, returns ``cap_reached=True, attempted=False`` so the
    caller caches NOTHING and retries the next day (a cap denial is not a negative). A
    clean search that finds no email returns ``attempted=True`` (a cacheable negative).
    Never raises."""
    if not settings.apollo_api_key or not company_name.strip() or not location_terms.strip():
        return _COMPANY_CONTACT_SKIP

    titles = titles or HR_TITLES
    reveal_cap = settings.company_contact_reveal_cap if reveal_cap is None else reveal_cap

    # Recheck cooldown — don't re-search a (company, location) tried recently. The
    # caller normally gates on this too (via the cache row), but enforce it here so the
    # helper is safe to call directly.
    last = _aware_utc(attempted_at)
    if last is not None and (datetime.now(timezone.utc) - last) < timedelta(days=settings.apollo_recheck_days):
        logger.info("apollo_company_contact_skip_cooldown", company=company_name, age_days=(datetime.now(timezone.utc) - last).days)
        return _COMPANY_CONTACT_SKIP

    from app.services import apollo_budget

    async def _budget_ok() -> bool:
        """True if the enrichment budget has headroom today (None = unlimited / DB error)."""
        # Front-run the ApolloClient's own gate so we can distinguish "cap reached"
        # (defer, no negative) from "searched, found nothing" (cacheable negative).
        # Harvest-time fallback → also bound by the enrichment sub-cap.
        remaining = await apollo_budget.remaining_today(purpose="enrichment")
        return remaining is None or remaining > 0

    if not await _budget_ok():
        return CompanyContactResult(cap_reached=True)

    logger.info(
        "apollo_company_contact_triggered",
        company=company_name, location=location_terms, domain=domain,
    )

    client = client or ApolloClient(settings)
    dom = (domain or "").strip()

    # ── Resolve the Apollo organization (need its id for the people search) ──
    org = None
    try:
        if dom:
            org = await client.enrich_organization(dom)
        if org is None or not org.id:
            if not await _budget_ok():
                return CompanyContactResult(cap_reached=True)
            org = await client.search_organization(company_name, domain=dom)
    except ApolloAPIError as exc:
        logger.warning("apollo_company_contact_org_failed", company=company_name, error=str(exc))
        return CompanyContactResult(attempted=True)

    if org is None or not org.id:
        # Could be a genuine no-match, or the daily cap blocking the resolve — only
        # record a negative when budget is actually available.
        if not await _budget_ok():
            return CompanyContactResult(cap_reached=True)
        return CompanyContactResult(attempted=True, company_domain=(dom or ""))

    resolved_domain = (org.domain or dom or "")

    # ── Search the org's HR people, LOCATION-FILTERED ──
    if not await _budget_ok():
        return CompanyContactResult(cap_reached=True)
    try:
        people = await client.search_people(
            [org.id], titles, person_locations=[location_terms],
        )
    except ApolloAPIError as exc:
        logger.warning("apollo_company_contact_people_failed", company=company_name, error=str(exc))
        return CompanyContactResult(attempted=True, company_domain=resolved_domain)

    # ── Reveal emails until the first hit, bounded by reveal_cap ──
    revealed = 0
    for person in people:
        if revealed >= reveal_cap:
            break
        pid = getattr(person, "id", None)
        if not pid:
            continue
        if not await _budget_ok():
            # Out of budget mid-reveal — defer this (company, location), cache nothing.
            return CompanyContactResult(cap_reached=True)
        revealed += 1
        try:
            match = await client.match_person_by_id(pid, reveal_email=True)
        except ApolloAPIError as exc:
            logger.debug("apollo_company_contact_reveal_failed", person_id=pid, error=str(exc))
            continue
        if match.email:
            logger.info(
                "apollo_company_contact_found",
                company=company_name, location=location_terms, email_found=True,
            )
            return CompanyContactResult(
                name=(match.name or getattr(person, "name", "") or ""),
                title=(match.title or getattr(person, "title", "") or ""),
                email=match.email,
                phone=match.phone or "",
                company_domain=resolved_domain,
                matched=True,
                attempted=True,
            )

    # Searched (org matched) but no revealable email in that location — cacheable negative.
    logger.info(
        "apollo_company_contact_no_email",
        company=company_name, location=location_terms, people=len(people), revealed=revealed,
    )
    return CompanyContactResult(attempted=True, company_domain=resolved_domain, matched=True)


# ── Company + location HR shortlist (free) ───────────────────────────────────────

@dataclass
class HrShortlistResult:
    people: list[ApolloPersonResult]   # ranked best-first, at most `size`; emails LOCKED
    company_domain: str = ""
    attempted: bool = False            # True once a real search was issued (hit or miss)
    cap_reached: bool = False          # daily cap blocked the org resolve — retry later


async def apollo_hr_shortlist(
    *,
    settings: Settings,
    company_name: str,
    location_terms: str,
    domain: str = "",
    size: int = 10,
    verified_only: bool = True,
    client: ApolloClient | None = None,
) -> HrShortlistResult:
    """The top ``size`` HR / talent-acquisition people at a (company, location) —
    ids, names and titles only. The people search is free; the org resolve costs a
    credit but is cached per company (resolve_apollo_org), so a company is paid for
    once across all its locations. Emails are revealed later, one person at a
    time, by the rotation (company_location_contact_service). Ranked by
    hr_title_tier (recruiters first), keeping Apollo's order within a tier.
    ``verified_only`` asks Apollo for people with a verified email only.
    ``location_terms`` is REQUIRED — never a company-wide search. Never raises."""
    if not settings.apollo_api_key or not company_name.strip() or not location_terms.strip():
        return HrShortlistResult(people=[])

    from app.services.company_enrichment_service import resolve_apollo_org

    client = client or ApolloClient(settings)
    dom = (domain or "").strip()
    try:
        res = await resolve_apollo_org(
            settings, company_name=company_name, domain=dom, guess_domain=True, client=client,
        )
    except ApolloAPIError as exc:
        logger.warning("apollo_hr_shortlist_org_failed", company=company_name, error=str(exc))
        return HrShortlistResult(people=[], attempted=True)
    if res.cap_reached:
        return HrShortlistResult(people=[], company_domain=dom, cap_reached=True)
    org = res.org
    if org is None or not org.id:
        return HrShortlistResult(people=[], company_domain=dom, attempted=True)

    resolved_domain = org.domain or dom or ""
    try:
        people = await client.search_people(
            [org.id], HR_TITLES, person_locations=[location_terms], per_page=25,
            email_statuses=["verified"] if verified_only else None,
        )
    except ApolloAPIError as exc:
        logger.warning("apollo_hr_shortlist_people_failed", company=company_name, error=str(exc))
        return HrShortlistResult(people=[], company_domain=resolved_domain, attempted=True)

    seen: set[str] = set()
    unique = []
    for p in people:
        if p.id and p.id not in seen:
            seen.add(p.id)
            unique.append(p)
    ranked = sorted(unique, key=lambda p: hr_title_tier(p.title))[: max(1, size)]
    logger.info(
        "apollo_hr_shortlist", company=company_name, location=location_terms,
        found=len(people), kept=len(ranked),
    )
    return HrShortlistResult(people=ranked, company_domain=resolved_domain, attempted=True)
