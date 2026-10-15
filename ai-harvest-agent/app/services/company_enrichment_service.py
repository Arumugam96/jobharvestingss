"""Company-level enrichment pass (size + HQ location), shared by every harvest agent.

Browser-free: one Apollo organization resolve per unique company (``resolve_apollo_org``
— also the shared, cached org-id lookup behind every Apollo people search), cached
on CompanyORM (app/services/company_service.py) so bulk_insert_scraped_jobs applies
it to ALL of that company's jobs. Extracted from LinkedInAgent._enrich_companies /
_run_company_waterfall so the Naukri agent can run the exact same pass.

Spend control: every Apollo call goes through ApolloClient, whose chokepoint
enforces the DB-backed daily credit cap (apollo_budget). Never call Apollo HTTP
directly from here.

An optional ``fallback`` hook lets a browser-bound caller add a second step when
Apollo leaves size or country empty (LinkedIn's company-card scrape + LLM, which is
currently disabled — LinkedIn passes no hook, i.e. the historical "apollo_only").
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import structlog

from app.core.company_domain import infer_company_domain, is_placeholder_company
from app.core.dependencies import get_session_factory
from app.services import run_guard
from app.services.apollo_client import ApolloClient, ApolloOrgResult

logger = structlog.get_logger(__name__)

# Max unique companies to enrich per run (bounds time + Apollo volume; the daily
# credit cap in apollo_budget is the hard account-wide backstop on top of this).
COMPANY_ENRICH_CAP = 40

# Hosts that are job-board / social profile pages, never a company's own website —
# a company_url on one of these must not be turned into an Apollo lookup domain.
_NON_COMPANY_HOSTS = (
    "linkedin.com", "naukri.com", "ambitionbox.com", "dice.com", "indeed.com",
    "glassdoor.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "google.com",
)

FallbackHook = Callable[[str, str], Awaitable[dict]]


def domain_from_website(url: str | None) -> str:
    """Bare domain (``acme.com``) of a company's OWN website URL, or "" when the
    URL is blank, unparsable, or points at a job board / social profile (e.g. a
    LinkedIn company page) rather than the company's site."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlparse(raw).hostname or "").lower().strip(".")
    except ValueError:
        return ""
    if not host or "." not in host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    if any(host == h or host.endswith("." + h) for h in _NON_COMPANY_HOSTS):
        return ""
    return host


# ── Apollo organization resolve (cached per company) ────────────────────────────

@dataclass
class OrgResolution:
    org: ApolloOrgResult | None = None  # None: unknown to Apollo, or deferred by the cap
    cap_reached: bool = False           # daily cap blocked the lookup — nothing cached, retry later
    attempted: bool = False             # a paid Apollo lookup was issued by this call
    size_band: str = ""                 # "<band> employees" (canonical; also served from cache)


def _org_from_row(row: Any) -> ApolloOrgResult:
    """A cached CompanyORM row as the ApolloOrgResult callers expect (no employee
    count — the row keeps only the size band)."""
    return ApolloOrgResult(
        id=row.apollo_org_id, name=row.company_name or None, domain=row.domain or None,
        industry=row.company_industry or None, state=row.company_state or None,
        country=row.company_country or None,
    )


async def resolve_apollo_org(
    settings: Any,
    *,
    company_name: str,
    domain: str = "",
    guess_domain: bool = False,
    client: ApolloClient | None = None,
) -> OrgResolution:
    """The Apollo organization for a company — needed before any people search.

    Resolving costs a credit (organizations/enrich by domain, then
    mixed_companies/search by name when that finds no id), so the result is cached on
    CompanyORM by company key and reused by every location, run and feature: a hit
    forever, a miss for apollo_recheck_days. Both lookups reserve against the daily
    cap inside ApolloClient; a cap denial returns ``cap_reached`` and caches nothing.

    ``domain`` is the caller's known domain. When it disagrees with the cached row's
    domain the cache is bypassed (two companies whose names normalize alike) and the
    fresh result is not written over the other company's id. ``guess_domain`` lets
    the enrich lookup use a domain inferred from the name when none is known — never
    used for that conflict check. Raises ApolloAPIError like the client, so callers
    keep their existing error handling."""
    from app.services import apollo_budget
    from app.services.company_service import (
        apollo_org_resolve_due,
        get_companies_by_keys,
        normalize_company_key,
        record_apollo_org,
    )

    key = normalize_company_key(company_name)
    dom = (domain or "").strip().lower()

    row = None
    if key:
        try:
            async with get_session_factory(settings)() as db:
                row = (await get_companies_by_keys(db, {key})).get(key)
        except Exception as exc:
            logger.warning("apollo_org_cache_read_failed", company=company_name, error=str(exc))
    domain_conflict = bool(row is not None and dom and row.domain and row.domain.lower() != dom)
    if row is not None and not domain_conflict and not apollo_org_resolve_due(row, settings.apollo_recheck_days):
        logger.info("apollo_org_cache_hit", company=company_name, found=bool(row.apollo_org_id))
        if not row.apollo_org_id:
            return OrgResolution()
        return OrgResolution(org=_org_from_row(row), size_band=row.company_size or "")

    async def _budget_left() -> bool:
        remaining = await apollo_budget.remaining_today()
        return remaining is None or remaining > 0

    if not await _budget_left():
        return OrgResolution(cap_reached=True)

    client = client or ApolloClient(settings)
    lookup_dom = dom or (infer_company_domain(company_name)[0] if guess_domain else "")
    org = await client.enrich_organization(lookup_dom) if lookup_dom else None
    if org is None or not org.id:
        org = await client.search_organization(company_name, domain=lookup_dom)
    if org is None or not org.id:
        org = None
        # A None can also be the cap denying the lookup — only a real miss is cached.
        if not await _budget_left():
            return OrgResolution(cap_reached=True, attempted=True)

    from app.core.company_size import band_from_employee_count
    size_band = band_from_employee_count(org.size) if org else ""
    if key and not (domain_conflict and row.apollo_org_id):
        try:
            async with get_session_factory(settings)() as db:
                await record_apollo_org(
                    db, company_key=key, company_name=company_name,
                    org_id=(org.id if org else ""),
                    domain=((org.domain or "") if org else ""),
                    company_size=size_band,
                    company_country=((org.country or "") if org else ""),
                    company_state=((org.state or "") if org else ""),
                    company_industry=((org.industry or "") if org else ""),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("apollo_org_cache_write_failed", company=company_name, error=str(exc))
    logger.info("apollo_org_resolved", company=company_name, found=bool(org))
    return OrgResolution(org=org, attempted=True, size_band=size_band)


async def run_company_waterfall(
    settings: Any,
    *,
    company_name: str,
    company_url: str = "",
    seed_size: str = "",
    fallback: FallbackHook | None = None,
) -> dict:
    """Fill one company's size + HQ location.

      1. Apollo organizations/enrich by domain — the company's own website domain
         when known (e.g. Naukri's companyDetail.websiteUrl), else inferred from the
         name. Preferred when it matches (overwrites).
      2. Only if size or country is STILL missing and a `fallback` hook was given →
         ``fallback(company_name, company_url)``; its non-empty values fill gaps.

    `seed_size` is a size already scraped from a job, kept so a company that only
    needs its HQ location doesn't lose it. Returns {company_size, company_country,
    company_state, company_industry, domain, apollo_attempted}."""
    out = {"company_size": seed_size or "", "company_country": "", "company_state": "",
           "company_industry": "", "domain": "", "apollo_attempted": False}

    site_domain = domain_from_website(company_url)
    out["domain"] = site_domain or infer_company_domain(company_name)[0]
    if settings.apollo_enrich_company and settings.apollo_api_key and out["domain"]:
        try:
            # Shared, cached org resolve — a company already resolved by any feature
            # costs nothing here.
            res = await resolve_apollo_org(
                settings, company_name=company_name, domain=site_domain, guess_domain=True,
            )
            out["apollo_attempted"] = not res.cap_reached
            org = res.org
            if org:
                for f, v in (("company_country", org.country or ""),
                             ("company_state", org.state or ""),
                             ("company_industry", org.industry or ""),
                             ("company_size", res.size_band)):
                    if v:
                        out[f] = v  # Apollo preferred → overwrite
                if org.domain:
                    out["domain"] = org.domain
        except Exception as exc:
            logger.debug("company_apollo_failed", domain=out["domain"], error=str(exc))

    if fallback is not None and company_url and (not out["company_size"] or not out["company_country"]):
        try:
            extra = await fallback(company_name, company_url) or {}
            for f in ("company_size", "company_country", "company_state", "company_industry"):
                if not out[f] and extra.get(f):
                    out[f] = extra[f]
        except Exception as exc:
            logger.debug("company_fallback_failed", company=company_name, error=str(exc))

    return out


async def enrich_companies(
    jobs: list,
    settings: Any = None,
    *,
    fallback: FallbackHook | None = None,
    cap: int = COMPANY_ENRICH_CAP,
) -> dict:
    """Company-enrichment pass over a run's jobs (any source's scraped dataclass or
    UnifiedJob — read via getattr: company / company_url / company_size).

    Runs once per unique company (placeholder names like "Unknown Company" are
    skipped), honours the recheck cooldown (settings.apollo_recheck_days — a company
    already holding size + country is skipped), the per-run `cap`, and a cooperative
    stop. Writes ONLY the CompanyORM cache. Best-effort; returns a counts summary."""
    from app.services.company_service import (
        company_needs_enrichment,
        get_companies_by_keys,
        normalize_company_key,
        upsert_company_enrichment,
    )

    if settings is None:
        from app.config import get_settings
        settings = get_settings()

    # One entry per unique company: display name + a company_url + any size
    # already scraped from a job (seeded so the cache keeps it).
    entries: dict[str, dict] = {}
    for j in jobs:
        name = (getattr(j, "company", "") or "").strip()
        if not name or is_placeholder_company(name):
            continue
        key = normalize_company_key(name)
        if not key:
            continue
        slot = entries.setdefault(key, {"company_name": name, "company_url": "", "company_size": ""})
        url = (getattr(j, "company_url", "") or "").strip()
        if not slot["company_url"] and url:
            slot["company_url"] = url
        size = (getattr(j, "company_size", "") or "").strip()
        if not slot["company_size"] and size:
            slot["company_size"] = size

    keys = list(entries)
    if not keys:
        return {"companies": 0, "enriched": 0}

    session_factory = get_session_factory(settings)
    async with session_factory() as db:
        cached = await get_companies_by_keys(db, set(keys))
    keys = [k for k in keys
            if company_needs_enrichment(cached.get(k), settings.apollo_recheck_days)]
    if not keys:
        return {"companies": 0, "enriched": 0}

    enriched = 0
    for key in keys:
        if enriched >= cap:
            logger.info("company_enrich_cap_reached", cap=cap)
            break
        if run_guard.is_stop_requested():
            logger.info("company_enrichment_stopped_by_user", enriched=enriched)
            break
        info = entries[key]
        try:
            data = await run_company_waterfall(
                settings,
                company_name=info["company_name"],
                company_url=info["company_url"],
                seed_size=info["company_size"],
                fallback=fallback,
            )
        except Exception as exc:
            logger.debug("company_waterfall_failed", company=info["company_name"], error=str(exc))
            data = {}
        enriched += 1
        try:
            async with session_factory() as db:
                await upsert_company_enrichment(
                    db,
                    company_key=key,
                    company_name=info["company_name"],
                    domain=data.get("domain", ""),
                    company_size=data.get("company_size", ""),
                    company_country=data.get("company_country", ""),
                    company_state=data.get("company_state", ""),
                    company_industry=data.get("company_industry", ""),
                    apollo_attempted=data.get("apollo_attempted", False),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("company_cache_write_failed", company=info["company_name"], error=str(exc))

    logger.info("company_enrichment_pass_complete", companies=len(keys), enriched=enriched)
    return {"companies": len(keys), "enriched": enriched}
