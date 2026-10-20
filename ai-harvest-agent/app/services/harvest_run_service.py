"""Persistence for harvest workflow runs, scraped jobs, and LLM call audit logs.

Covers the LinkedIn/Naukri/Dice job-board pipeline — both the multi-source
orchestrator flow (POST /run-harvest-agent) and the three single-source flows
(POST /run-{linkedin,naukri,dice}-agent). Writes here are additive alongside
the existing JSON/Excel file storage (JobTracker, RunHistoryService, the
per-source *StorageService classes keep running unchanged); the DB becomes
what the frontend-facing GET endpoints read from.
"""
from __future__ import annotations

import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, TypeVar

import structlog
from sqlalchemy import Text, and_, case, delete, exists, func, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import aliased, noload
from sqlalchemy.sql.functions import FunctionElement

from app.config import get_settings
from app.core.company_size import (
    BAND_TO_TIER,
    TIER_TO_BANDS,
    band_to_tier,
    normalize_company_size,
    parse_company_size_band,
)
from app.core.contact_normalize import normalize_email, normalize_phone
from app.core.dependencies import get_session_factory
from app.core.job_url import normalize_job_url
from app.core.location import normalize_location_key, parse_location
from app.core.tenant_context import apply_tenant, bind_session_tenant, get_current_tenant_id
from app.core.text_formatting import html_description_to_text, normalize_job_title
from app.models.harvest_run import (
    CompanyLocationContactORM,
    HarvestRunORM,
    LlmCallORM,
    LlmCallType,
    ReenrichmentTaskORM,
    ScrapedJobORM,
)
from app.models.recruiter import RecruiterORM
from app.models.resdex import ResdexSearchORM, ResdexSearchStatus
from app.services.company_service import get_companies_by_keys, normalize_company_key
from app.services.recruiter_service import upsert_recruiter

logger = structlog.get_logger(__name__)

_T = TypeVar("_T")

# Shared with app/routes/frontend_routes.py's GET /jobs — one canonical set so
# the DB-side ORDER BY and the JSON-fallback path's in-memory sort never drift.
JOB_SORT_FIELDS = {
    "posted_date", "company", "job_title", "source", "hiring_entity", "location",
    "job_poster_name",
}

# Contact-availability tokens accepted by GET /jobs' `contact` filter — mirrors
# the UI's jobMatchesContact() (harvest-agent/src/components/JobsTable.jsx).
# Positive tokens match rows that HAVE the channel, no_* tokens rows missing it,
# "none" rows with no contact at all. Selected tokens are OR-combined.
# "email"/"mobile" count the poster/recruiter value OR the Company-tagged
# company(+location) HR contact; "email_poster" / "email_company" split the two.
# "resdex" matches jobs whose latest Resdex search kept candidates (DB path only).
CONTACT_FILTER_TOKENS = {
    "email", "email_poster", "email_company", "mobile", "linkedin",
    "no_email", "no_mobile", "no_linkedin", "none", "resdex",
}


# Characters _txt_expr() trims. Plain SQL trim() strips only ASCII spaces, but
# the UI (JS .trim()) and _txt() also treat tabs/newlines/NBSP/zero-width
# space/BOM as blank. Two-arg trim(string, chars) is valid on SQLite and Postgres.
_TRIM_CHARS = " \t\n\r\x0b\x0c\u00a0\u200b\ufeff"


def _txt_expr(col):
    """NULL-safe trimmed text — NULL/""/whitespace all compare equal to ""."""
    return func.trim(func.coalesce(col, ""), _TRIM_CHARS)


def _contact_presence_exprs() -> dict[str, Any]:
    """SQL presence tests per contact channel, matching exactly what the UI's
    jobContact() (harvest-agent/src/lib/jobsData.js) shows in the jobs table:

    - email_poster  — the job's own scraped email OR its linked recruiter's
                      enriched email (the merge scraped_job_view() applies), so
                      the statement must outerjoin RecruiterORM.
    - email_company — the Company-tagged company(+location) HR contact, counted
                      only when it isn't a duplicate of a poster address (the UI
                      dedupes identical values). bulk_insert_scraped_jobs only
                      attaches it to email-less jobs, so in practice it never
                      coexists with a poster email.
    - email         — either of the above.
    - mobile        — scraped OR recruiter OR company-contact phone.
    - linkedin      — the scraped poster profile URL only.

    All values compare trimmed and NULL-safe, like the UI's checks."""
    cc_email = _txt_expr(ScrapedJobORM.company_contact_email)
    email_poster = or_(
        _txt_expr(ScrapedJobORM.email_id) != "",
        _txt_expr(RecruiterORM.official_email_id) != "",
    )
    email_company = and_(
        cc_email != "",
        cc_email != _txt_expr(ScrapedJobORM.email_id),
        cc_email != _txt_expr(RecruiterORM.official_email_id),
    )
    phone = or_(
        _txt_expr(ScrapedJobORM.contact_number) != "",
        _txt_expr(RecruiterORM.contact_number) != "",
        _txt_expr(ScrapedJobORM.company_contact_phone) != "",
    )
    return {
        "email":         or_(email_poster, cc_email != ""),
        "email_poster":  email_poster,
        "email_company": email_company,
        "mobile":        phone,
        "linkedin":      _txt_expr(ScrapedJobORM.linkedin_profile_url) != "",
    }


class _normalized_job_url(FunctionElement):  # type: ignore[type-arg]
    """SQL mirror of app.core.job_url.normalize_job_url — cut at '#' then '?', trim,
    strip trailing '/', lowercase — so a raw scraped_jobs.job_url compares equal to
    the normalized key resdex_searches stores."""
    type = Text()
    inherit_cache = True


@compiles(_normalized_job_url)
def _compile_normalized_job_url(element, compiler, **kw) -> str:  # SQLite (tests)
    url = compiler.process(list(element.clauses)[0], **kw)
    no_frag = f"substr({url}, 1, instr({url} || '#', '#') - 1)"
    no_query = f"substr({no_frag}, 1, instr({no_frag} || '?', '?') - 1)"
    return f"lower(rtrim(trim({no_query}), '/'))"


@compiles(_normalized_job_url, "postgresql")
def _compile_normalized_job_url_pg(element, compiler, **kw) -> str:
    url = compiler.process(list(element.clauses)[0], **kw)
    return f"lower(rtrim(btrim(split_part(split_part({url}, '#', 1), '?', 1)), '/'))"


def _resdex_match_expr():
    """Jobs whose LATEST Resdex search (same tenant + normalized job_url) is
    "matched" — i.e. ≥1 candidate kept. Latest-only so the filter agrees with the
    job detail page (GET /jobs/{id}/outreach-detail shows the newest search)."""
    latest = aliased(ResdexSearchORM)
    newer = aliased(ResdexSearchORM)
    return exists().where(
        latest.tenant_id == ScrapedJobORM.tenant_id,
        latest.job_url == _normalized_job_url(ScrapedJobORM.job_url),
        latest.status == ResdexSearchStatus.MATCHED,
        ~exists().where(
            newer.tenant_id == latest.tenant_id,
            newer.job_url == latest.job_url,
            newer.created_at > latest.created_at,
        ),
    )


def _contact_token_exprs() -> dict[str, Any]:
    """CONTACT_FILTER_TOKENS → SQL predicate (statement must outerjoin RecruiterORM)."""
    p = _contact_presence_exprs()
    return {
        **p,
        "no_email":    not_(p["email"]),
        "no_mobile":   not_(p["mobile"]),
        "no_linkedin": not_(p["linkedin"]),
        "none":        and_(not_(p["email"]), not_(p["mobile"]), not_(p["linkedin"])),
        "resdex":      _resdex_match_expr(),
    }


def poc_name_expr():
    """The POC the UI shows for a job (jobContact().pocName): the poster's name,
    else the Company-tagged contact's name; trimmed, "" when neither. Backs the
    POC facet, the `poc` filter, POC sorting and the POC count."""
    return func.coalesce(
        func.nullif(_txt_expr(ScrapedJobORM.job_poster_name), ""),
        _txt_expr(ScrapedJobORM.company_contact_name),
    )


# str.strip() covers \s and NBSP but not zero-width space / BOM (see _TRIM_CHARS).
_EDGE_BLANK_RE = re.compile(r"^[\s\u200b\ufeff]+|[\s\u200b\ufeff]+$")


def _txt(value: Any) -> str:
    return "" if value is None else _EDGE_BLANK_RE.sub("", str(value))


def job_dict_contact(job: dict[str, Any]) -> dict[str, Any]:
    """Python mirror of _contact_presence_exprs()/poc_name_expr() (and the UI's
    jobContact()) over a scraped_job_view()-shaped dict — used by GET /jobs and
    GET /jobs/facets on the JSON read path. Older combined files carry only the
    merged email_id/contact_number, which count as the poster's values.

    Returns {"poc_name", "email", "email_poster", "email_company", "mobile",
    "linkedin"} — the booleans keyed like CONTACT_FILTER_TOKENS' positive tokens."""
    poster_emails = {
        e for e in (_txt(job.get(k)) for k in ("email_scraped", "email_recruiter", "email_id")) if e
    }
    company_email = _txt(job.get("company_contact_email"))
    has_phone = any(
        _txt(job.get(k))
        for k in ("phone_scraped", "phone_recruiter", "contact_number", "company_contact_phone")
    )
    return {
        "poc_name":      _txt(job.get("job_poster_name")) or _txt(job.get("company_contact_name")),
        "email":         bool(poster_emails) or bool(company_email),
        "email_poster":  bool(poster_emails),
        "email_company": bool(company_email) and company_email not in poster_emails,
        "mobile":        has_phone,
        "linkedin":      bool(_txt(job.get("linkedin_profile_url"))),
    }


def data_source_mode() -> str:
    """Current value of DATA_SOURCE from .env — "auto" (default) | "database"
    | "json". See Settings.data_source in app/config.py for what each means."""
    return get_settings().data_source


async def resolve_read(
    mode: str,
    db_fn: Callable[[], Awaitable[_T]],
    json_fn: Callable[[], Any],
) -> tuple[Any, str]:
    """Pick DB vs. JSON for one GET-endpoint read, per the DATA_SOURCE setting.

    "json"     -> never call db_fn; always read the JSON/file store.
    "database" -> only call db_fn; caller gets whatever it returns (including
                  an empty/None "not found") without a JSON fallback.
    "auto"     -> call db_fn first; fall back to json_fn if it returned
                  nothing (None, [], falsy) — today's pre-toggle behavior.

    Returns (result, source_used) so the caller knows which shape it's
    holding — a DB row/ORM list needs its own mapper, a JSON result is
    already in the response's final shape.
    """
    if mode == "json":
        return json_fn(), "json"
    result = await db_fn()
    if result or mode == "database":
        return result, "database"
    return json_fn(), "json"


async def db_write(coro_fn: Callable[[AsyncSession], Awaitable[_T]]) -> _T | None:
    """Best-effort DB mirror for the harvest workflow.

    A DB outage must never break the file-based flow that already works
    today, so any exception here is logged and swallowed. Acquires its own
    short-lived session via get_session_factory() so it works both inside a
    request (alongside the usual Depends(get_db_session)) and from a detached
    asyncio.create_task, which has no FastAPI dependency injection available.
    """
    try:
        session_factory = get_session_factory(get_settings())
        async with session_factory() as db:
            await bind_session_tenant(db)  # RLS scope for this write session (Postgres)
            try:
                result = await coro_fn(db)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise
    except Exception as exc:
        logger.warning("harvest_db_mirror_failed", error=str(exc))
        return None


async def db_read(coro_fn: Callable[[AsyncSession], Awaitable[_T]]) -> _T | None:
    """Best-effort DB read for the frontend-facing GET endpoints. Returns None
    on any DB error (unreachable DB, etc.) rather than raising, so callers can
    fall back to the pre-existing file-based read for that endpoint."""
    try:
        session_factory = get_session_factory(get_settings())
        async with session_factory() as db:
            await bind_session_tenant(db)  # RLS scope for this read session (Postgres)
            return await coro_fn(db)
    except Exception as exc:
        logger.warning("harvest_db_read_failed", error=str(exc))
        return None


class HarvestRunService:
    """CRUD for HarvestRunORM / ScrapedJobORM / LlmCallORM."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── Write ────────────────────────────────────────────────────────────────

    async def create_run(
        self,
        run_id: str,
        job_id: str | None = None,
        source: str | None = None,
        sources: list[str] | None = None,
        filters_snapshot: dict[str, Any] | None = None,
        started_at: datetime | None = None,
    ) -> str:
        """Create a new run row. source=None for a multi-source orchestrator
        run; a source name ("LinkedIn"/"Naukri"/"Dice") for a standalone
        single-source run. Returns the new row's surrogate id."""
        run = HarvestRunORM(
            id=str(uuid.uuid4()),
            job_id=job_id,
            run_id=run_id,
            source=source,
            sources=sources if sources is not None else ([source] if source else []),
            filters_snapshot=filters_snapshot,
            started_at=started_at,
            tenant_id=get_current_tenant_id(),  # own this run to the current tenant
        )
        self._db.add(run)
        await self._db.flush()
        return run.id

    async def update_run(self, run_pk: str, **fields: Any) -> None:
        if not fields:
            return
        await self._db.execute(
            update(HarvestRunORM).where(HarvestRunORM.id == run_pk).values(**fields)
        )

    async def bulk_insert_scraped_jobs(self, run_pk: str, jobs: list[dict[str, Any]]) -> None:
        """jobs is a list of plain dicts keyed by ScrapedJobORM's column names
        (e.g. UnifiedJob.to_dict()'s output, or a per-source mapper's dict for
        the standalone single-source routes — see linkedin_routes.py's
        _to_scraped_job_dict() for the field-name translation each source
        needs, since LinkedIn/Naukri/Dice's own scraped dataclasses don't
        share field names for recruiter/poster info)."""
        if not jobs:
            return
        tid = get_current_tenant_id()  # own every inserted job to the current tenant
        # Company-level enrichment cache (app/services/company_service.py) — a
        # company's Apollo size/HQ-location, shared across ALL its jobs (recruiter
        # or not). Fetched once for the batch's companies.
        company_cache = await get_companies_by_keys(
            self._db, {normalize_company_key(j.get("company", "")) for j in jobs}
        )
        # Company+location HR-contact cache (app/services/company_location_contact_service.py)
        # — a location-specific contact discovered via Apollo for an email-less job's
        # (company, job-location). Attached below ONLY to jobs with no scraped/recruiter
        # email, so the real poster always wins. Fetched once for the batch's pairs.
        _loc_pairs = {
            (normalize_company_key(j.get("company", "")), normalize_location_key(j.get("location", "")))
            for j in jobs
        }
        _loc_pairs = {p for p in _loc_pairs if p[0] and p[1]}
        loc_contact_cache: dict[tuple[str, str], CompanyLocationContactORM] = {}
        if _loc_pairs:
            _lc_res = await self._db.execute(
                select(CompanyLocationContactORM).where(
                    CompanyLocationContactORM.company_key.in_({c for c, _ in _loc_pairs})
                )
            )
            for _lc in _lc_res.scalars():
                _lk = (_lc.company_key, _lc.location_key)
                if _lk in _loc_pairs:
                    loc_contact_cache[_lk] = _lc
        rows: list[ScrapedJobORM] = []
        for j in jobs:
            recruiter_id = None
            recruiter = None
            poster_name = (j.get("job_poster_name") or "").strip()
            if poster_name:
                recruiter = await upsert_recruiter(
                    self._db,
                    person_name=poster_name,
                    company_name=j.get("current_company") or j.get("company") or "",
                    designation=j.get("job_poster_designation") or "",
                    linkedin_profile_url=j.get("linkedin_profile_url"),
                    harvest_source=j.get("source", ""),
                )
                recruiter_id = recruiter.id if recruiter else None
            # Company size/HQ location backfill from the recruiter's Apollo org
            # match (populated earlier this run by _enrich_recruiters). Used only
            # when the scraped job dict carries no value of its own — LinkedIn's
            # own scraped company_size still wins. company_size default on the
            # recruiter is "NOT_FOUND", treated as empty here.
            rec_company_size = rec_company_country = rec_company_state = ""
            if recruiter is not None:
                _rcs = (recruiter.company_size or "").strip()
                if _rcs and _rcs != "NOT_FOUND":
                    rec_company_size = _rcs
                rec_company_country = (recruiter.company_country or "").strip()
                rec_company_state = (recruiter.company_state or "").strip()
            # Company enrichment cache — covers jobs with no recruiter to borrow
            # Apollo org data from. Lowest precedence (job dict > recruiter > cache).
            _comp = company_cache.get(normalize_company_key(j.get("company", "")))
            cache_company_size = (_comp.company_size if _comp else "") or ""
            cache_company_country = (_comp.company_country if _comp else "") or ""
            cache_company_state = (_comp.company_state if _comp else "") or ""
            # Derive the job-location country/state deterministically from the
            # free-text location (app/core/location.py) so the UI's Country
            # filter/facet works without an LLM/Apollo call. Prefer any value the
            # caller already provided (e.g. a future source that supplies it).
            loc_city, loc_state, loc_country = parse_location(j.get("location", ""))
            # Company+location HR contact (Apollo fallback) — attach ONLY when the job
            # has NO scraped email and NO linked-recruiter email, so the real poster's
            # contact always wins and job_poster_*/email_id below are never overwritten.
            cc_name = cc_title = cc_email = cc_phone = cc_location = cc_source = ""
            _job_email = (j.get("email_id") or "").strip()
            _rec_email = (recruiter.official_email_id or "").strip() if recruiter is not None else ""
            if not _job_email and not _rec_email:
                _lc_row = loc_contact_cache.get(
                    (normalize_company_key(j.get("company", "")), normalize_location_key(j.get("location", "")))
                )
                if _lc_row and (_lc_row.hr_contact_email or "").strip():
                    cc_name = _lc_row.hr_contact_name or ""
                    cc_title = _lc_row.hr_contact_title or ""
                    cc_email = _lc_row.hr_contact_email or ""
                    cc_phone = _lc_row.hr_contact_phone or ""
                    cc_location = _lc_row.location or ""
                    cc_source = "apollo"
            rows.append(
                ScrapedJobORM(
                    id=str(uuid.uuid4()),
                    run_id=run_pk,
                    tenant_id=tid,
                    source=j.get("source", ""),
                    # Strip LinkedIn's "<Title> <Title> with verification" a11y artifact
                    # so new harvests store a clean title everywhere it's read.
                    job_title=normalize_job_title(j.get("job_title", "")),
                    company=j.get("company", ""),
                    location=j.get("location", ""),
                    country=j.get("country") or loc_country,
                    state=j.get("state") or loc_state,
                    salary=j.get("salary", ""),
                    experience=j.get("experience", ""),
                    posted_date=j.get("posted_date", ""),
                    job_url=j.get("job_url", ""),
                    job_description=j.get("job_description", ""),
                    job_description_html=j.get("job_description_html", ""),
                    skills=j.get("skills") or [],
                    work_mode=j.get("work_mode", "not_specified"),
                    company_url=j.get("company_url", ""),
                    # Real scrape time stamped by the orchestrator converters;
                    # fall back to now() so the column is always app-populated
                    # rather than reflecting the batch-insert (created_at).
                    scraped_at=j.get("scraped_at") or datetime.now(timezone.utc),
                    company_size=j.get("company_size") or rec_company_size or cache_company_size,
                    company_country=j.get("company_country") or rec_company_country or cache_company_country,
                    company_state=j.get("company_state") or rec_company_state or cache_company_state,
                    employment_type=j.get("employment_type", ""),
                    job_type=j.get("job_type", ""),
                    domain=j.get("domain", "Any"),
                    hiring_entity=j.get("hiring_entity", "Any"),
                    is_gcc=j.get("is_gcc", False),
                    verification_status=j.get("verification_status", "pending"),
                    passed_filter=j.get("passed_filter", True),
                    filter_reason=j.get("filter_reason", ""),
                    job_poster_name=j.get("job_poster_name"),
                    job_poster_designation=j.get("job_poster_designation"),
                    linkedin_profile_url=j.get("linkedin_profile_url"),
                    current_company=j.get("current_company"),
                    email_id=j.get("email_id"),
                    contact_number=j.get("contact_number"),
                    # Company+location Apollo HR contact — stored SEPARATELY from the
                    # poster fields above (never overwritten); outreach uses
                    # company_contact_email as the last-resort recipient.
                    company_contact_name=cc_name,
                    company_contact_title=cc_title,
                    company_contact_email=cc_email,
                    company_contact_phone=cc_phone,
                    company_contact_location=cc_location,
                    company_contact_source=cc_source,
                    recruiter_id=recruiter_id,
                    lead_confidence=j.get("lead_confidence"),
                )
            )
        # Within-run company propagation (zero-cost): a company's size / HQ
        # location often lands on just one of its jobs (the one LinkedIn showed a
        # size band for, or the one whose recruiter Apollo matched). Share the
        # first non-empty value across every job of the same company so jobs
        # WITHOUT a recruiter still show it. Keyed by normalized company name.
        best: dict[str, dict[str, str]] = {}
        for r in rows:
            key = normalize_company_key(r.company or "")
            if not key:
                continue
            slot = best.setdefault(key, {"company_size": "", "company_country": "", "company_state": ""})
            for f in slot:
                if not slot[f] and getattr(r, f):
                    slot[f] = getattr(r, f)
        for r in rows:
            slot = best.get(normalize_company_key(r.company or ""))
            if not slot:
                continue
            for f in ("company_size", "company_country", "company_state"):
                if not getattr(r, f) and slot[f]:
                    setattr(r, f, slot[f])
        self._db.add_all(rows)
        await self._db.flush()

    async def delete_jobs_for_run(self, run_pk: str) -> None:
        """Delete all scraped-job rows for a run. Used by the end-of-run
        reconcile: incremental persistence writes raw/unclassified rows during
        the scrape (crash safety), then on completion those provisional rows are
        deleted and replaced with the final classified/deduped set. run_id is
        indexed, so this is cheap. Recruiters are NOT touched (they persist)."""
        await self._db.execute(delete(ScrapedJobORM).where(ScrapedJobORM.run_id == run_pk))

    async def replace_run_jobs(self, run_pk: str, jobs: list[dict[str, Any]]) -> None:
        """Atomically swap a run's scraped jobs for a new set (delete + insert in
        the same session/transaction), so the run's rows are never briefly empty
        for a concurrent reader. Used to reconcile provisional incremental rows
        with the final canonical list at end-of-run."""
        await self.delete_jobs_for_run(run_pk)
        await self.bulk_insert_scraped_jobs(run_pk, jobs)

    async def bulk_insert_llm_calls(self, run_pk: str, calls: list[dict[str, Any]]) -> None:
        if not calls:
            return
        self._db.add_all(
            [
                LlmCallORM(
                    id=str(uuid.uuid4()),
                    run_id=run_pk,
                    call_type=c.get("call_type", LlmCallType.JOB_HARVEST),
                    job_url=c.get("job_url"),
                    provider=c["provider"],
                    model=c["model"],
                    prompt=c["prompt"],
                    response=c.get("response"),
                    prompt_chars=c.get("prompt_chars", 0),
                    response_chars=c.get("response_chars", 0),
                    input_tokens=c.get("input_tokens"),
                    output_tokens=c.get("output_tokens"),
                    latency_ms=c.get("latency_ms"),
                    # Real wall-clock call time captured by LLMService at call time
                    # (see llm_service._complete_text_with_failover). Fall back to
                    # now() for any legacy entry that predates that field so the
                    # NOT-NULL column is always populated by the app, not the
                    # end-of-run INSERT's server_default.
                    called_at=c.get("called_at") or datetime.now(timezone.utc),
                    success=c.get("success", True),
                    error_message=c.get("error_message"),
                    retry_count=c.get("retry_count", 0),
                )
                for c in calls
            ]
        )
        await self._db.flush()

    # ── Read ─────────────────────────────────────────────────────────────────

    async def get_by_job_id(self, job_id: str) -> HarvestRunORM | None:
        """Look up an orchestrator run by its ephemeral poll id (GET /harvest-status/{job_id})."""
        result = await self._db.execute(select(HarvestRunORM).where(HarvestRunORM.job_id == job_id))
        return result.scalar_one_or_none()

    async def get_by_run_id(self, run_id: str, source: str | None = None) -> HarvestRunORM | None:
        """source=None matches orchestrator runs only; a source name matches
        that source's standalone runs only — mirrors list_runs()'s filter."""
        stmt = select(HarvestRunORM).where(HarvestRunORM.run_id == run_id)
        stmt = stmt.where(HarvestRunORM.source.is_(None) if source is None else HarvestRunORM.source == source)
        stmt = apply_tenant(stmt, HarvestRunORM.tenant_id)  # no cross-tenant lookup by run_id
        result = await self._db.execute(stmt.order_by(HarvestRunORM.created_at.desc()))
        return result.scalars().first()

    async def list_runs(self, source: str | None = None, limit: int = 50) -> list[HarvestRunORM]:
        stmt = select(HarvestRunORM)
        stmt = stmt.where(HarvestRunORM.source.is_(None) if source is None else HarvestRunORM.source == source)
        stmt = apply_tenant(stmt, HarvestRunORM.tenant_id)
        stmt = stmt.order_by(HarvestRunORM.created_at.desc()).limit(limit)
        result = await self._db.execute(stmt)
        return list(result.scalars())

    async def list_run_history(self, limit: int = 50) -> list[HarvestRunORM]:
        """Run-history feed for GET /run-history: the multi-source orchestrator
        runs (source IS NULL) PLUS the standalone LinkedIn Home Feed lead runs
        (source == "LinkedIn Feed"), newest first. Feed runs are surfaced here so
        an operator sees them alongside job-harvest runs; the caller tags each
        entry by source so the UI can render the two run types distinctly. The
        per-source LinkedIn/Naukri/Dice job runs stay excluded (they have their
        own results endpoints), matching list_runs(source=None)'s historical
        scope."""
        stmt = (
            select(HarvestRunORM)
            .where(
                or_(
                    HarvestRunORM.source.is_(None),
                    HarvestRunORM.source == "LinkedIn Feed",
                )
            )
            .order_by(HarvestRunORM.created_at.desc())
            .limit(limit)
        )
        stmt = apply_tenant(stmt, HarvestRunORM.tenant_id)
        result = await self._db.execute(stmt)
        return list(result.scalars())

    async def count_run_history(self) -> int:
        """Total run-history entries (same scope as list_run_history), UNCAPPED —
        so the UI badge reflects the real count rather than the list's 50-row cap."""
        stmt = select(func.count()).select_from(HarvestRunORM).where(
            or_(
                HarvestRunORM.source.is_(None),
                HarvestRunORM.source == "LinkedIn Feed",
            )
        )
        stmt = apply_tenant(stmt, HarvestRunORM.tenant_id)
        return (await self._db.execute(stmt)).scalar_one()

    async def fail_stale_running(self, message: str = "Interrupted by a server restart") -> int:
        """Mark every still-'running' run as 'failed'. A harvest executes in a
        detached asyncio task that does NOT survive a process restart, so any
        row left 'running' at startup is stale. Without this the single-flight
        guard's DB backstop (get_active_run) would see a dead run and reject
        every new start with 409 forever. Mirrors JobTracker.load_from_disk's
        in-memory reconciliation. Returns the number of rows updated."""
        result = await self._db.execute(
            update(HarvestRunORM)
            .where(HarvestRunORM.status == "running")
            .values(status="failed", message=message, error=message, progress=100)
        )
        return result.rowcount or 0

    async def get_active_run(self) -> HarvestRunORM | None:
        """The most-recent still-running harvest (any source, orchestrator or
        standalone), or None. Backs the single-flight guard: Playwright can't
        drive the same Chrome profile from two runs at once, so a second start
        while this returns non-None must be rejected. This is the cross-process
        backstop to the in-process asyncio.Lock in the start routes."""
        stmt = (
            select(HarvestRunORM)
            .options(noload(HarvestRunORM.jobs))  # guard only needs identity — don't load child jobs
            .where(HarvestRunORM.status == "running")
            .order_by(HarvestRunORM.started_at.desc())
            .limit(1)
        )
        result = await self._db.execute(stmt)
        return result.scalars().first()

    async def jobs_scraped_today(self) -> int:
        """Total jobs harvested across all runs since UTC midnight — backs the
        global daily cap (MAX_JOBS_PER_DAY). Sums `combined_count` (the deduped
        per-run total), which is written at run completion, so this reflects
        finished runs; the single-flight guard ensures the prior run has ended
        (and been counted) before a new one can start. `created_at` is used
        (never null); the boundary is naive UTC midnight to match SQLite's
        naive-UTC storage of the column."""
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        result = await self._db.execute(
            select(func.coalesce(func.sum(HarvestRunORM.combined_count), 0))
            .where(HarvestRunORM.created_at >= start)
        )
        return int(result.scalar_one() or 0)

    async def jobs_saved_today(self) -> int:
        """Live count of scraped_jobs rows actually persisted since UTC midnight,
        across every run today. Unlike jobs_scraped_today() (which sums
        combined_count, only finalized at run completion), this counts real rows
        as each incremental batch inserts — so the Rule Engine's "Jobs Saved"
        card climbs while a harvest is in flight. Same naive-UTC-midnight
        boundary as jobs_scraped_today()."""
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        result = await self._db.execute(
            select(func.count())
            .select_from(ScrapedJobORM)
            .where(ScrapedJobORM.created_at >= start)
        )
        return int(result.scalar_one() or 0)

    async def list_scraped_jobs(
        self,
        *,
        keyword:       str | None = None,
        company:       str | None = None,
        company_exact: str | None = None,
        job_title:     str | None = None,
        poc:           str | None = None,
        contact:       list[str] | None = None,
        size_tier:     str | None = None,
        country:       str | None = None,
        company_country: str | None = None,
        source:        str | None = None,
        hiring_entity: str | None = None,
        work_mode:     str | None = None,
        date_from:     str | None = None,
        date_to:       str | None = None,
        run_id:        str | None = None,
        sort_by:       str = "posted_date",
        sort_order:    str = "desc",
        page:          int = 1,
        page_size:     int = 100,
    ) -> tuple[list[ScrapedJobORM], int]:
        """ScrapedJobORM rows across *every* run (unlike get_by_run_id/
        list_runs, not scoped to one HarvestRunORM) — the DB-backed
        equivalent of frontend_routes.py's _apply_job_filters/_apply_sort/
        _paginate against the JSON file store. Filtering/sorting/pagination
        all happen in SQL so this scales as scraped_jobs accumulates across
        runs (the JSON file only ever held the latest run's jobs).

        posted_date is a free-text String column, not a real DateTime — the
        date_from/date_to bound is a string-prefix comparison, matching the
        JSON path's _date_gte/_date_lte, and only works if callers persist
        posted_date as a YYYY-MM-DD-prefixed string (true for LinkedIn's
        _format_posted(); not independently verified for Naukri/Dice)."""
        stmt = select(ScrapedJobORM)
        stmt = apply_tenant(stmt, ScrapedJobORM.tenant_id)  # scope to the caller's tenant
        token_exprs = _contact_token_exprs()
        selected = [token_exprs[t] for t in (contact or []) if t in token_exprs]
        if keyword or selected:
            # Both the keyword search and the contact predicates read the linked
            # recruiter's email/phone. Joined once; 1:1 on the recruiter PK —
            # never duplicates job rows.
            stmt = stmt.outerjoin(RecruiterORM, ScrapedJobORM.recruiter_id == RecruiterORM.id)
        if keyword:
            # Matches everything the UI's free-text search used to cover
            # client-side (title/company/source/POC/email/phone, incl. the
            # recruiter's enriched email/phone and the Company-tagged contact),
            # plus the description that this endpoint always searched.
            like = f"%{keyword}%"
            stmt = stmt.where(
                or_(
                    ScrapedJobORM.job_title.ilike(like),
                    ScrapedJobORM.job_description.ilike(like),
                    ScrapedJobORM.company.ilike(like),
                    ScrapedJobORM.job_poster_name.ilike(like),
                    ScrapedJobORM.source.ilike(like),
                    ScrapedJobORM.email_id.ilike(like),
                    ScrapedJobORM.contact_number.ilike(like),
                    RecruiterORM.official_email_id.ilike(like),
                    RecruiterORM.contact_number.ilike(like),
                    ScrapedJobORM.company_contact_name.ilike(like),
                    ScrapedJobORM.company_contact_email.ilike(like),
                    ScrapedJobORM.company_contact_phone.ilike(like),
                )
            )
        if company:
            stmt = stmt.where(ScrapedJobORM.company.ilike(f"%{company}%"))
        if company_exact:
            stmt = stmt.where(ScrapedJobORM.company == company_exact)
        if job_title:
            stmt = stmt.where(ScrapedJobORM.job_title == job_title)
        if poc:
            # The POC the table shows: poster name, else the company contact's.
            stmt = stmt.where(poc_name_expr() == poc.strip())
        if selected:
            stmt = stmt.where(or_(*selected))  # RecruiterORM joined above
        if size_tier:
            # company_size stores the canonical "<band> employees" string (or "");
            # the replace() folds legacy en-dash rows onto BAND_TO_TIER's hyphen keys.
            norm = func.replace(func.coalesce(ScrapedJobORM.company_size, ""), "–", "-")
            known = [f"{band} employees" for band in BAND_TO_TIER]
            if size_tier == "unknown":
                stmt = stmt.where(norm.notin_(known))
            elif size_tier in TIER_TO_BANDS:
                stmt = stmt.where(
                    norm.in_([f"{band} employees" for band in TIER_TO_BANDS[size_tier]])
                )
        if country:
            stmt = stmt.where(func.lower(ScrapedJobORM.country) == country.lower())
        if company_country:
            stmt = stmt.where(func.lower(ScrapedJobORM.company_country) == company_country.lower())
        if source:
            stmt = stmt.where(func.lower(ScrapedJobORM.source) == source.lower())
        if hiring_entity:
            stmt = stmt.where(func.lower(ScrapedJobORM.hiring_entity) == hiring_entity.lower())
        if work_mode:
            stmt = stmt.where(func.lower(ScrapedJobORM.work_mode) == work_mode.lower())
        if date_from:
            stmt = stmt.where(func.substr(ScrapedJobORM.posted_date, 1, 10) >= date_from[:10])
        if date_to:
            stmt = stmt.where(func.substr(ScrapedJobORM.posted_date, 1, 10) <= date_to[:10])
        if run_id:
            # ScrapedJobORM.run_id is the HarvestRunORM PK (uuid); the caller
            # passes the human-facing display run_id (e.g. "20260817_110703"),
            # so resolve it through a subquery. A display run_id can map to more
            # than one HarvestRunORM row (the orchestrator run plus any
            # single-source runs sharing the timestamp), hence IN (...).
            stmt = stmt.where(
                ScrapedJobORM.run_id.in_(
                    select(HarvestRunORM.id).where(HarvestRunORM.run_id == run_id)
                )
            )

        total = (
            await self._db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()

        if sort_by == "job_poster_name":
            # The UI's POC column (poster, else Company-tagged contact name).
            sort_column = poc_name_expr()
        else:
            sort_field = sort_by if sort_by in JOB_SORT_FIELDS else "posted_date"
            sort_column = getattr(ScrapedJobORM, sort_field)
        stmt = stmt.order_by(
            sort_column.desc() if sort_order.lower() == "desc" else sort_column.asc(),
            ScrapedJobORM.id,  # unique tiebreaker so OFFSET/LIMIT pages are stable
        )
        stmt = stmt.offset(max(0, page - 1) * page_size).limit(page_size)

        result = await self._db.execute(stmt)
        return list(result.scalars()), total

    async def job_facets(self) -> dict[str, Any]:
        """Distinct dropdown values + whole-dataset stats for the jobs page.

        GET /jobs is paginated server-side, so the UI can no longer derive its
        Company/Job/POC dropdown options or its header/footer stat counts from
        the loaded rows — this returns them across the entire scraped_jobs
        table in one call (GET /jobs/facets). Distincts are global, not scoped
        to the active filters."""
        async def _distinct(col) -> list[str]:
            result = await self._db.execute(apply_tenant(select(col).distinct(), ScrapedJobORM.tenant_id))
            return sorted(v for v in result.scalars() if v)

        companies  = await _distinct(ScrapedJobORM.company)
        job_titles = await _distinct(ScrapedJobORM.job_title)
        # The POC the table shows (poster, else the Company-tagged contact), so
        # every POC cell value is selectable in the POC dropdown.
        poc_names  = await _distinct(poc_name_expr())
        countries         = await _distinct(ScrapedJobORM.country)
        company_countries = await _distinct(ScrapedJobORM.company_country)

        # Same predicates as the `contact` filter (incl. Company-tagged contacts).
        presence = _contact_presence_exprs()

        def _count_if(expr):
            return func.sum(case((expr, 1), else_=0))

        stats_stmt = (
            select(
                func.count().label("total"),
                # nullif drops empty-string companies — COUNT ignores NULLs.
                func.count(func.distinct(func.nullif(ScrapedJobORM.company, ""))).label("companies"),
                _count_if(poc_name_expr() != "").label("pocs"),
                # IS NOT FALSE mirrors the UI's `passed_filter !== false`.
                _count_if(ScrapedJobORM.passed_filter.isnot(False)).label("qualified"),
                _count_if(ScrapedJobORM.passed_filter.is_(False)).label("flagged"),
                _count_if(presence["email"]).label("with_email"),
                _count_if(presence["email_poster"]).label("with_poster_email"),
                _count_if(presence["email_company"]).label("with_company_email"),
                _count_if(presence["mobile"]).label("with_phone"),
                _count_if(presence["linkedin"]).label("with_linkedin"),
            )
            .select_from(ScrapedJobORM)
            .outerjoin(RecruiterORM, ScrapedJobORM.recruiter_id == RecruiterORM.id)
        )
        stats_stmt = apply_tenant(stats_stmt, ScrapedJobORM.tenant_id)  # scope stats to the tenant
        row = (await self._db.execute(stats_stmt)).one()
        stats = {
            key: int(getattr(row, key) or 0)
            for key in (
                "total", "companies", "pocs", "qualified", "flagged",
                "with_email", "with_poster_email", "with_company_email",
                "with_phone", "with_linkedin",
            )
        }
        return {
            "companies":         companies,
            "job_titles":        job_titles,
            "poc_names":         poc_names,
            "countries":         countries,
            "company_countries": company_countries,
            "stats":             stats,
        }

    async def get_scraped_job_by_id(self, job_id: str) -> ScrapedJobORM | None:
        stmt = apply_tenant(select(ScrapedJobORM).where(ScrapedJobORM.id == job_id), ScrapedJobORM.tenant_id)
        result = await self._db.execute(stmt)  # tenant predicate closes the by-id IDOR
        return result.scalar_one_or_none()

    async def list_jobs_for_run(self, run_pk: str) -> list[ScrapedJobORM]:
        """All of one run's scraped jobs — used to build the post-harvest
        report (JSON/Excel) from the DB instead of harvest-time files."""
        result = await self._db.execute(
            select(ScrapedJobORM).where(ScrapedJobORM.run_id == run_pk)
        )
        return list(result.scalars())

    async def recent_harvested_job_urls(self, within_days: int) -> set[str]:
        """Normalized job_urls already harvested within the last `within_days`
        days (tenant-scoped), for SOURCE-LEVEL dedup — the LinkedIn agent skips
        re-scraping any posting whose URL is in this set, so it never re-enters
        the pipeline (no re-extract, no re-email). `within_days <= 0` = all-time.
        Keys are normalized with the same helper the agent uses, so they match."""
        stmt = apply_tenant(select(ScrapedJobORM.job_url), ScrapedJobORM.tenant_id)
        if within_days and within_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=within_days)
            stmt = stmt.where(ScrapedJobORM.created_at >= cutoff)
        rows = (await self._db.execute(stmt)).scalars().all()
        return {k for u in rows if (k := normalize_job_url(u))}

    async def list_all_jobs_for_report(self) -> list[ScrapedJobORM]:
        """Every scraped job on record, newest posting first — the dataset
        behind GET /download/{json,excel}, matching what GET /jobs lists."""
        stmt = apply_tenant(
            select(ScrapedJobORM).order_by(ScrapedJobORM.posted_date.desc()),
            ScrapedJobORM.tenant_id,  # exports must not leak other tenants' jobs
        )
        result = await self._db.execute(stmt)
        return list(result.scalars())

    async def list_pending_report_runs(self) -> list[HarvestRunORM]:
        """Runs that ended via a user stop and still owe a report email
        (report_pending=True), oldest first. The next successful run merges
        their jobs into its own report, then clears the flag."""
        stmt = (
            select(HarvestRunORM)
            .options(noload(HarvestRunORM.jobs))  # jobs fetched separately via list_jobs_for_run
            .where(HarvestRunORM.report_pending.is_(True))
            .order_by(HarvestRunORM.created_at.asc())
        )
        result = await self._db.execute(stmt)
        return list(result.scalars())

    async def clear_report_pending(self, run_pk: str) -> None:
        """Mark a stopped run's owed report as delivered."""
        await self._db.execute(
            update(HarvestRunORM)
            .where(HarvestRunORM.id == run_pk)
            .values(report_pending=False)
        )

    # ── Re-enrichment queue (degrade-on-outage → retry on a later run) ───────────

    async def enqueue_reenrichment(self, run_pk: str, items: list[dict[str, Any]]) -> int:
        """Persist jobs that degraded to selector-only (every LLM provider down) as
        pending reenrichment_tasks. items come from LinkedInAgent.get_reenrich_queue()
        (job_url, source, content, schema_description, system). Dedups against tasks
        already queued for this run. Returns the number inserted."""
        if not items:
            return 0
        existing = await self._db.execute(
            select(ReenrichmentTaskORM.job_url).where(ReenrichmentTaskORM.run_id == run_pk)
        )
        seen = {u for (u,) in existing.all()}
        rows: list[ReenrichmentTaskORM] = []
        for it in items:
            url = it.get("job_url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            rows.append(ReenrichmentTaskORM(
                id=str(uuid.uuid4()),
                run_id=run_pk,
                job_url=url,
                source=it.get("source", ""),
                content=it.get("content", ""),
                schema_description=it.get("schema_description", ""),
                system=it.get("system", ""),
            ))
        if rows:
            self._db.add_all(rows)
            await self._db.flush()
        return len(rows)

    async def mark_extraction_pending(self, run_pk: str, job_urls: list[str]) -> None:
        """Flag the scraped_jobs rows whose extraction was deferred to re-enrichment,
        so reports/UI can show them as pending rather than complete."""
        if not job_urls:
            return
        await self._db.execute(
            update(ScrapedJobORM)
            .where(ScrapedJobORM.run_id == run_pk, ScrapedJobORM.job_url.in_(job_urls))
            .values(extraction_status="pending")
        )

    async def list_pending_reenrichment(self, limit: int) -> list[dict[str, Any]]:
        """Pending re-enrichment tasks, oldest first, as plain dicts (so callers can
        run slow LLM calls without holding a DB session on detached ORM rows)."""
        stmt = (
            select(ReenrichmentTaskORM)
            .where(ReenrichmentTaskORM.status == "pending")
            .order_by(ReenrichmentTaskORM.first_seen_at.asc())
            .limit(limit)
        )
        result = await self._db.execute(stmt)
        return [
            {
                "id": t.id,
                "run_id": t.run_id,
                "job_url": t.job_url,
                "content": t.content,
                "schema_description": t.schema_description,
                "system": t.system,
                "first_seen_at": t.first_seen_at,
                "attempts": t.attempts,
            }
            for t in result.scalars()
        ]

    async def apply_reenrichment(self, task_id: str, run_pk: str, job_url: str, extracted: dict[str, Any]) -> None:
        """A re-enrichment retry succeeded — backfill the scraped_jobs row from the
        extracted dict (only overwriting fields the LLM actually returned) and mark
        the task done + the row extraction_status='ok'."""
        row = (await self._db.execute(
            select(ScrapedJobORM).where(
                ScrapedJobORM.run_id == run_pk, ScrapedJobORM.job_url == job_url
            )
        )).scalars().first()
        if row is not None:
            def _set(attr: str, val: Any) -> None:
                if val not in (None, "", []):
                    setattr(row, attr, val)
            _set("job_description", extracted.get("description"))
            _set("job_description_html", extracted.get("description_html"))
            _set("skills", extracted.get("skills"))
            _set("salary", extracted.get("salary"))
            _set("employment_type", extracted.get("employment_type"))
            _set("company_url", extracted.get("company_url"))
            _set("job_poster_name", extracted.get("recruiter_name"))
            _set("job_poster_designation", extracted.get("recruiter_title"))
            _set("linkedin_profile_url", extracted.get("recruiter_url"))
            _set("email_id", normalize_email(extracted.get("recruiter_email")))
            _set("contact_number", normalize_phone(extracted.get("recruiter_phone")))
            # Company-size band: the original harvest may have stored "" (LLM was
            # down at scrape time). Backfill it from the re-extraction — the LLM's
            # own value first, else a regex over the insights/about text — normalised
            # to the canonical "<band> employees" so the size filter matches. _set
            # skips empties, so a non-resolvable size never wipes an existing one.
            size_band = normalize_company_size(extracted.get("company_size") or "")
            if size_band:
                _set("company_size", f"{size_band} employees")
            else:
                insights = f"{extracted.get('job_insights') or ''}\n{extracted.get('about_company') or ''}"
                _set("company_size", parse_company_size_band(insights))
            row.extraction_status = "ok"
            # Best-effort recruiter identity now that a poster name may be available.
            poster = (extracted.get("recruiter_name") or "").strip()
            if poster:
                recruiter = await upsert_recruiter(
                    self._db,
                    person_name=poster,
                    company_name=extracted.get("recruiter_company") or extracted.get("company") or "",
                    designation=extracted.get("recruiter_title") or "",
                    linkedin_profile_url=extracted.get("recruiter_url"),
                    harvest_source="LinkedIn",
                )
                if recruiter:
                    row.recruiter_id = recruiter.id
        await self._db.execute(
            update(ReenrichmentTaskORM)
            .where(ReenrichmentTaskORM.id == task_id)
            .values(status="done", attempts=ReenrichmentTaskORM.attempts + 1,
                    last_attempt_at=datetime.now(timezone.utc))
        )

    async def fail_reenrichment(self, task_id: str, run_pk: str, job_url: str, reason: str) -> None:
        """Give up on a task (aged past the retention window) — mark it failed and
        the scraped_jobs row extraction_status='failed'."""
        await self._db.execute(
            update(ReenrichmentTaskORM)
            .where(ReenrichmentTaskORM.id == task_id)
            .values(status="failed", last_attempt_at=datetime.now(timezone.utc))
        )
        await self._db.execute(
            update(ScrapedJobORM)
            .where(ScrapedJobORM.run_id == run_pk, ScrapedJobORM.job_url == job_url)
            .values(extraction_status="failed")
        )
        logger.info("reenrichment_task_failed", task_id=task_id, reason=reason)

    async def bump_reenrichment_attempt(self, task_id: str) -> None:
        """Record a retry that didn't succeed (LLM still down / transient error),
        leaving the task pending for the next run."""
        await self._db.execute(
            update(ReenrichmentTaskORM)
            .where(ReenrichmentTaskORM.id == task_id)
            .values(attempts=ReenrichmentTaskORM.attempts + 1,
                    last_attempt_at=datetime.now(timezone.utc))
        )


# ── Ad-hoc LLM call audit (non-run-scoped) ──────────────────────────────────────

async def insert_llm_call(
    db: AsyncSession,
    *,
    call_type: str,
    provider: str,
    model: str,
    prompt: str = "",
    response: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    latency_ms: int | None = None,
    success: bool = True,
    error_message: str | None = None,
    job_url: str | None = None,
    run_id: str | None = None,
    called_at: datetime | None = None,
) -> None:
    """Record one LLM call in the shared `llm_calls` table. Unlike
    HarvestRunService.bulk_insert_llm_calls (harvest-run-scoped), this takes any
    session and defaults run_id=None, so the outreach flow (email/linkedin
    generation) can audit its calls without a harvest run. prompt/response char
    counts are derived so callers only pass the text.

    called_at is the real wall-clock time of the call; callers that captured it
    just before invoking the LLM should pass it. Defaults to now() — acceptable
    here because this row is inserted immediately after the call (no batch skew)."""
    db.add(
        LlmCallORM(
            id=str(uuid.uuid4()),
            run_id=run_id,
            call_type=call_type,
            job_url=job_url,
            provider=provider,
            model=model,
            prompt=prompt or "",
            response=response,
            prompt_chars=len(prompt or ""),
            response_chars=len(response or ""),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            called_at=called_at or datetime.now(timezone.utc),
            success=success,
            error_message=error_message,
        )
    )
    await db.flush()


# ── Shared read-side view helpers ───────────────────────────────────────────────
# Used by linkedin_routes.py / naukri_routes.py / dice_routes.py to reconstruct
# the exact same JSON shape their *StorageService JSON files already produce,
# so switching the read source from file to DB is invisible to the frontend.

def filters_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """The 14-key filter subset every source route's _build_payload() embeds
    in its saved JSON (identical shape across linkedin/naukri/dice_routes.py)."""
    s = snapshot or {}
    return {
        "keyword": s.get("keyword", ""),
        "location": s.get("location", ""),
        "job_type": s.get("job_type", ""),
        "work_mode": s.get("work_mode", ""),
        "search_window_hours": s.get("search_window_hours", 0),
        "max_jobs": s.get("max_jobs", 0),
        "domain": s.get("domain", ""),
        "hiring_entity": s.get("hiring_entity", ""),
        "gcc_mode": s.get("gcc_mode", ""),
        "salary_min": s.get("salary_min"),
        "salary_max": s.get("salary_max"),
        "salary_currency": s.get("salary_currency", ""),
        "include_undisclosed_salary": s.get("include_undisclosed_salary", False),
        "naukri_company_types": s.get("naukri_company_types", []),
    }


# Where a job's resolved outreach contact came from (contact_origin) — stored on
# resdex_searches.contact_source and shown as a badge on the job detail page.
CONTACT_ORIGIN_JOB_POST = "job_post"                  # scraped from the posting itself
CONTACT_ORIGIN_RECRUITER_PROFILE = "recruiter_profile"  # scraped recruiter contact info
CONTACT_ORIGIN_APOLLO = "apollo"                      # recruiter email revealed by Apollo
CONTACT_ORIGIN_APOLLO_HR = "apollo_hr"                # company+location Apollo HR contact


def contact_origin(job: Any) -> str:
    """Provenance of the contact outreach would email for this job — the same
    precedence as scraped_job_view's outreach_to_email (job email → linked recruiter
    email → company HR contact). Read only from stored provenance; "" when the job
    has no contact. Works on a ScrapedJobORM with its recruiter eager-loaded."""
    if _txt(getattr(job, "email_id", None)):
        return CONTACT_ORIGIN_JOB_POST
    recruiter = getattr(job, "recruiter", None)
    if recruiter is not None and _txt(getattr(recruiter, "official_email_id", None)):
        if _txt(getattr(recruiter, "enrichment_source", None)).lower() == "apollo":
            return CONTACT_ORIGIN_APOLLO
        return CONTACT_ORIGIN_RECRUITER_PROFILE
    if _txt(getattr(job, "company_contact_email", None)):
        # Company+location contacts are only ever discovered via Apollo.
        return CONTACT_ORIGIN_APOLLO_HR
    return ""


def scraped_job_view(job: ScrapedJobORM) -> dict[str, Any]:
    """Matches the shape of one entry in a combined-JSON file's "jobs" list
    (see frontend_routes.py's _load_all_jobs) — used by GET /jobs so the DB
    and JSON read paths are interchangeable to the frontend. `id` here is
    the row's real primary key; a JSON-sourced job instead gets a synthetic
    md5-of-url id from frontend_routes.py's _job_id() — the two id schemes
    only ever coexist across responses, never mixed within one.

    email_id/contact_number merge in the linked RecruiterORM's enriched
    contact info (official_email_id/contact_number) when the job row itself
    scraped none — one view feeds the UI, the report files, and the report
    email, so they all show the merged contact identically.

    email_scraped/email_recruiter (and the phone_* pair) additionally expose
    the two sources UNMERGED so the UI can show both, labeled by origin, when
    both are present. The merged email_id/contact_number above are kept as-is
    for the report files/email that rely on a single value."""
    recruiter = job.recruiter
    email_scraped = job.email_id or None
    email_recruiter = (recruiter.official_email_id if recruiter else "") or None
    phone_scraped = job.contact_number or None
    phone_recruiter = (recruiter.contact_number if recruiter else "") or None
    email = email_scraped or email_recruiter
    phone = phone_scraped or phone_recruiter
    # Company+location Apollo HR contact (attached at insert time only when the job
    # had no scraped/recruiter email). Kept separate from the poster above; it is the
    # LAST-resort outreach recipient. See bulk_insert_scraped_jobs / company_
    # location_contact_service. outreach_to_name tracks whoever we'd actually email so
    # the greeting matches the recipient (poster's name for a scraped/recruiter email,
    # the company contact's name otherwise).
    company_contact_email = (getattr(job, "company_contact_email", "") or "") or None
    company_contact_name = (getattr(job, "company_contact_name", "") or "") or None
    outreach_to_email = email or company_contact_email
    if email:
        outreach_to_name = job.job_poster_name or (recruiter.person_name if recruiter else None)
    else:
        outreach_to_name = company_contact_name
    # Plain-text description: use the stored text, else derive it from the stored
    # HTML at read time. LinkedIn jobs whose description was captured as HTML skip
    # the LLM's verbatim plain-text copy, so job_description is empty for them —
    # deriving here keeps the tag-free JSON/Excel downloads and the outreach
    # prompt populated without a separate stored/LLM-generated text field.
    job_description = job.job_description or html_description_to_text(job.job_description_html)
    # A NaN/inf float would 500 the whole response at render time (Starlette
    # encodes JSON with allow_nan=False) — coerce to None so one bad row can't
    # take down the entire jobs list.
    lead_confidence = job.lead_confidence
    if lead_confidence is not None and not math.isfinite(lead_confidence):
        lead_confidence = None
    return {
        "id":                     job.id,
        # Normalize at read time too, so already-harvested rows with the doubled
        # "<Title> with verification" artifact render clean in outreach + the UI.
        "job_title":              normalize_job_title(job.job_title),
        "company":                job.company,
        "location":               job.location,
        # Job-location country/state parsed from `location` (display-time Country
        # filter/facet); `location` above is still the verbatim string the UI shows.
        "country":                job.country,
        "state":                  job.state,
        "salary":                 job.salary,
        "experience":             job.experience,
        "posted_date":            job.posted_date,
        "job_url":                job.job_url,
        "job_description":        job_description,
        "job_description_html":   job.job_description_html,
        "skills":                 job.skills,
        "work_mode":              job.work_mode,
        "company_url":            job.company_url,
        # Company-size band captured per job + its friendly tier (Small/Medium/
        # Large/Enterprise or "" ⇒ Unknown), so the UI can offer a display-only
        # company-size filter. See app/core/company_size.py.
        "company_size":           job.company_size,
        "company_size_tier":      band_to_tier(job.company_size),
        # Company HQ location from Apollo org enrichment — a separate "Company
        # country" filter/facet, distinct from the job-location country above.
        "company_country":        job.company_country,
        "company_state":          job.company_state,
        "employment_type":        job.employment_type,
        "job_type":               job.job_type,
        "domain":                 job.domain,
        "hiring_entity":          job.hiring_entity,
        "is_gcc":                 job.is_gcc,
        "verification_status":    job.verification_status,
        "passed_filter":          job.passed_filter,
        "filter_reason":          job.filter_reason,
        "source":                 job.source,
        "job_poster_name":        job.job_poster_name,
        "job_poster_designation": job.job_poster_designation,
        "linkedin_profile_url":   job.linkedin_profile_url,
        "current_company":        job.current_company,
        "email_id":               email,
        "contact_number":         phone,
        "email_scraped":          email_scraped,
        "email_recruiter":        email_recruiter,
        "phone_scraped":          phone_scraped,
        "phone_recruiter":        phone_recruiter,
        # Company+location Apollo HR contact (separate from the poster above) and the
        # resolved outreach recipient. email_id above stays scraped-or-recruiter only
        # (UI/reports unchanged); auto-outreach uses outreach_to_email/outreach_to_name,
        # which additionally fall back to this company contact.
        "company_contact_name":     company_contact_name,
        "company_contact_title":    (getattr(job, "company_contact_title", "") or "") or None,
        "company_contact_email":    company_contact_email,
        "company_contact_phone":    (getattr(job, "company_contact_phone", "") or "") or None,
        "company_contact_location": (getattr(job, "company_contact_location", "") or "") or None,
        "company_contact_source":   (getattr(job, "company_contact_source", "") or "") or None,
        "outreach_to_email":        outreach_to_email,
        "outreach_to_name":         outreach_to_name,
        # Provenance: the linked recruiter email's enrichment source ("apollo" when
        # Apollo revealed it) and the resolved outreach contact's origin
        # (job_post | recruiter_profile | apollo | apollo_hr — see contact_origin).
        "email_recruiter_source":   ((getattr(recruiter, "enrichment_source", "") or "") or None)
                                    if email_recruiter else None,
        "outreach_to_source":       contact_origin(job) or None,
        # LinkedIn Home Feed leads carry an LLM lead-quality score; NULL for every
        # other source, so the UI shows a confidence badge only on feed leads.
        "lead_confidence":        lead_confidence,
    }


def run_to_result_summary(run: HarvestRunORM) -> dict[str, Any]:
    """Matches every *StorageService.list_results() summary row shape:
    {run_id, executed_at, status, total_found, source, file_path}."""
    return {
        "run_id": run.run_id,
        "executed_at": run.started_at.isoformat() if run.started_at else "",
        "status": run.status,
        "total_found": run.combined_count,
        "source": run.source or "",
        "file_path": run.json_path or "",
    }
