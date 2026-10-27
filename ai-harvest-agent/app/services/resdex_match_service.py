"""Resdex candidate matching — the pre-pass of automatic outreach.

run_auto_outreach_after_harvest calls prepare_resdex_candidates() once, after
_dedupe_targets and before the send fan-out, so only jobs that will actually be
emailed are searched:

  1. select   recruiter/job-posting contacts (Apollo recruiter emails included) and,
              with RESDEX_INCLUDE_APOLLO_CONTACTS, the Apollo company HR contact;
              source in RESDEX_SOURCES, with a JD; drop targets outreach would skip anyway
              (no Resdex quota spent on emails that won't go out)
  2. reuse    a matched search for the same tenant + job_url within
              RESDEX_REUSE_DAYS instead of searching again
  3. extract  JD → requirements (LLM, grounded; no-LLM fallback) → relaxation
              ladder of Boolean queries + filters (app/services/resdex_query.py)
  4. search   one batched browser session (app/agents/resdex_candidate_agent)
  5. profile  intercepted JSON fields as-is, or LLM extraction from the card text
              grounded in that text
  6. pick     Resdex order, mandatory-skill coverage ≥ RESDEX_MIN_SKILL_COVERAGE,
              ≤ RESDEX_CANDIDATES_PER_JOB, then the DB-enforced daily candidate cap
  7. store    resdex_searches + resdex_candidates (tenant-stamped)

Never raises — a Resdex problem (session expired, cap, LLM down, parse failure)
only means the email goes out exactly as it would without Resdex.
format_candidate_block() renders the plain-text candidate block for the email and
render_candidate_cards_html() the matching HTML cards (name, role, experience,
location, notice, skills — never contacts, URL, employer or CTC).
"""
from __future__ import annotations

import asyncio
import html as html_lib
import inspect
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import structlog
from sqlalchemy import select, update

from app.config import get_settings
from app.core.job_url import normalize_job_url
from app.core.tenant_context import get_current_tenant_id
from app.models.harvest_run import LlmCallType
from app.models.resdex import (
    ResdexCandidateEmailORM,
    ResdexCandidateORM,
    ResdexSearchORM,
    ResdexSearchStatus,
)
from app.prompts import resdex_prompts as rp
from app.services.harvest_run_service import db_read, db_write
from app.services.jd_llm_enrichment import llm_configured
from app.services.llm_service import LLMService
from app.services.resdex_budget import try_reserve_candidates
from app.services.resdex_query import build_tiers, skill_coverage, term_key
from app.services.resdex_types import (
    CANDIDATE_FIELDS,
    ResdexBatchResult,
    ResdexCard,
    ResdexJobQuery,
    clean_profile_url,
)

logger = structlog.get_logger(__name__)

BatchRunner = Callable[..., Awaitable[dict[str, ResdexBatchResult]]]
SkipCheck = Callable[[dict], Awaitable[bool]]
# login_waiter(since) -> True once the recruiter session is logged in again (after
# `since`); the default raises the global "login needed" request and waits.
LoginWaiter = Callable[[datetime], Awaitable[bool]]

# Batch outcomes that mean "a person must log in" — retried once after a login.
_LOGIN_STATUSES = (ResdexSearchStatus.SESSION_EXPIRED, ResdexSearchStatus.SESSION_BUSY)
RESDEX_LOGIN_REASON = "Resdex candidate search needs the Naukri recruiter login"

# Concurrent LLM calls (requirements / profile extraction) across jobs.
_LLM_CONCURRENCY = 2
# Upper bound on result cards examined per job (each text card may cost one LLM call).
_MAX_CARDS_EXAMINED = 20
# The batch runner asks for this many results before relaxing to the next tier.
_MIN_RESULTS = 2
# Column widths of ResdexCandidateORM string fields (values are clipped, never padded).
_FIELD_LIMITS = {
    "candidate_name": 255, "current_designation": 255, "current_company": 255,
    "experience_text": 60, "location": 255, "notice_period": 60, "ctc": 60, "last_active": 60,
}
_LIST_FIELDS = ("previous_roles", "preferred_locations", "skills")


# ── LLM plumbing ──────────────────────────────────────────────────────────────

def resdex_llm_settings(settings: Any) -> Any:
    """``settings`` with the extraction model swapped for RESDEX_LLM_MODEL when set
    (same pattern as jd_llm_enrichment.naukri_llm_settings); unset → unchanged, so
    Resdex inherits EXTRACTION_LLM_MODEL."""
    model = (getattr(settings, "resdex_llm_model", "") or "").strip()
    if not model:
        return settings
    return settings.model_copy(update={"extraction_llm_model": model})


def _default_llm(settings: Any) -> Any | None:
    llm_settings = resdex_llm_settings(settings)
    try:
        if not llm_configured(llm_settings):
            return None
        return LLMService(llm_settings)
    except Exception as exc:
        logger.warning("resdex_llm_unavailable", error=str(exc))
        return None


async def _extract_requirements(llm: Any, job: dict, job_url: str, timeout_s: float) -> dict:
    """Grounded requirements for one job: the LLM extraction when it yields at
    least one grounded mandatory skill, else the no-LLM fallback."""
    if llm is not None:
        try:
            raw = await asyncio.wait_for(
                llm.extract_json(
                    content=rp.build_requirements_content(job),
                    schema_description=rp.RESDEX_REQUIREMENTS_SCHEMA,
                    system=rp.RESDEX_REQUIREMENTS_SYSTEM,
                    job_url=job_url or None,
                    call_type=LlmCallType.RESDEX_REQUIREMENTS,
                ),
                timeout=timeout_s,
            )
            req = rp.ground_requirements(raw, job)
            if req["mandatory_skills"]:
                return {**req, "extracted_by": "llm"}
            logger.info("resdex_requirements_llm_empty", job_url=job_url)
        except Exception as exc:
            logger.warning("resdex_requirements_llm_failed", job_url=job_url, error=str(exc))
    req = rp.ground_requirements(rp.fallback_requirements(job), job)
    return {**req, "extracted_by": "fallback"}


async def _card_fields(llm: Any, card: ResdexCard, job_url: str, timeout_s: float
                       ) -> tuple[dict, str] | None:
    """(fields, extraction_method) for one result card, or None when nothing can be
    extracted (text card with no LLM / LLM failure)."""
    if card.method == "json":
        fields = {k: (card.fields or {}).get(k) for k in CANDIDATE_FIELDS}
        method = "json"
    else:
        if llm is None or not (card.text or "").strip():
            return None
        try:
            raw = await asyncio.wait_for(
                llm.extract_json(
                    content=rp.build_profile_content(card.text),
                    schema_description=rp.RESDEX_PROFILE_EXTRACT_SCHEMA,
                    system=rp.RESDEX_PROFILE_EXTRACT_SYSTEM,
                    job_url=job_url or None,
                    call_type=LlmCallType.RESDEX_PROFILE_EXTRACT,
                ),
                timeout=timeout_s,
            )
        except Exception as exc:
            logger.warning("resdex_profile_llm_failed", job_url=job_url, error=str(exc))
            return None
        fields = rp.ground_profile(raw, card.text)
        method = "llm"
    # Never store a session-bound Resdex /v3/preview link (opens as "not valid"
    # outside the search's browser session) or a non-http(s) value.
    fields["profile_url"] = clean_profile_url(card.profile_url or fields.get("profile_url"))
    return _clean_fields(fields), method


def _dedupe_key(fields: dict, card: ResdexCard) -> str:
    """Identity of a result card within one search: Resdex's jsUserId when known,
    else the profile link ("" = no key, never deduplicated)."""
    uid = (card.raw or {}).get("jsUserId") if isinstance(card.raw, dict) else None
    if uid not in (None, ""):
        return f"uid:{uid}"
    url = fields.get("profile_url") or ""
    return f"url:{url}" if url else ""


def _clean_fields(fields: dict) -> dict:
    """Coerce to the ORM column types: strings clipped, lists of strings, float years."""
    out: dict[str, Any] = {}
    for k in CANDIDATE_FIELDS:
        v = fields.get(k)
        if k in _LIST_FIELDS:
            items = v if isinstance(v, list) else []
            out[k] = [str(x).strip() for x in items if str(x or "").strip()]
        elif k == "experience_years":
            try:
                out[k] = float(v) if v is not None and not isinstance(v, bool) else None
            except (TypeError, ValueError):
                out[k] = None
        else:
            scalar = isinstance(v, (str, int, float)) and not isinstance(v, bool)
            s = str(v).strip() if scalar else ""
            limit = _FIELD_LIMITS.get(k)
            out[k] = s[:limit] if limit else s
    if out["experience_years"] is None and out["experience_text"]:
        out["experience_years"] = rp.parse_experience_years(out["experience_text"])
    return out


# ── Batch runner (browser) ────────────────────────────────────────────────────

async def _default_batch_runner(
    jobs: list[ResdexJobQuery], *, headless: bool = False, min_results: int = _MIN_RESULTS,
) -> dict[str, ResdexBatchResult]:
    """The real Resdex runner, imported lazily (Playwright stack), run once inside a
    ProactorEventLoop thread when uvicorn --reload left a Windows selector loop
    (same pattern as naukri_recruiter_routes._run)."""
    from app.agents.resdex_candidate_agent import run_resdex_batch
    from app.core.proactor import needs_proactor, run_in_proactor
    from app.services.resdex_budget import try_reserve_search

    if needs_proactor():
        # The proactor thread has its own event loop; the cached async DB engine
        # belongs to this one, so budget reservations are marshalled back here
        # (the budget fails closed — a cross-loop error would block every search).
        main_loop = asyncio.get_running_loop()

        async def reserve_on_main_loop() -> bool:
            fut = asyncio.run_coroutine_threadsafe(try_reserve_search(), main_loop)
            return await asyncio.wrap_future(fut)

        return await _with_human_needed(run_resdex_batch, lambda extra: run_in_proactor(
            lambda: run_resdex_batch(
                jobs, headless=headless, min_results=min_results,
                reserve_search=reserve_on_main_loop, **extra,
            )
        ))
    return await _with_human_needed(run_resdex_batch, lambda extra: run_resdex_batch(
        jobs, headless=headless, min_results=min_results, **extra,
    ))


def _accepts_kwarg(fn: Callable, name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


async def _on_human_needed(message: str) -> None:
    """run_resdex_batch's on_human_needed: a CAPTCHA / challenge is held open in the
    Live Browser — raise the global "needs you in the Live Browser" request. May run
    on the proactor worker thread (the registry is thread-safe). Never raises."""
    try:
        from app.services import login_required as lr

        lr.raise_login_required(
            lr.SOURCE_NAUKRI_RECRUITER, kind=lr.KIND_HUMAN_NEEDED,
            reason=message or "Resdex needs you to solve a challenge in the Live Browser",
            expires_s=float(get_settings().resdex_human_wait_s or 600),
        )
    except Exception as exc:
        logger.debug("resdex_human_needed_raise_failed", error=str(exc))


async def _with_human_needed(
    run_resdex_batch: Callable, call: Callable[[dict], Awaitable[Any]],
) -> Any:
    """Run the batch, passing on_human_needed only when run_resdex_batch accepts it
    (works before and after that parameter exists); the human_needed request is
    dropped once the batch returns."""
    if not _accepts_kwarg(run_resdex_batch, "on_human_needed"):
        return await call({})
    try:
        return await call({"on_human_needed": _on_human_needed})
    finally:
        try:
            from app.services import login_required as lr

            lr.clear(lr.SOURCE_NAUKRI_RECRUITER, kind=lr.KIND_HUMAN_NEEDED)
        except Exception:
            pass


async def _default_login_waiter(since: datetime) -> bool:
    """Raise the global "Naukri recruiter login needed" request and wait up to
    LOGIN_WAIT_S for a person to log in via the Live Browser. Only the persisted
    recruiter status is polled (no browser), and the batch runner has already
    closed its browser, so the login window can take the recruiter profile."""
    from app.services import login_required as lr
    from app.services.run_guard import is_stop_requested

    lr.raise_login_required(lr.SOURCE_NAUKRI_RECRUITER, reason=RESDEX_LOGIN_REASON)
    if int(getattr(get_settings(), "login_wait_s", 0) or 0) <= 0:
        return False
    return await lr.wait_for_login(
        lr.SOURCE_NAUKRI_RECRUITER, since=since, should_stop=is_stop_requested,
    )


# ── Persistence ───────────────────────────────────────────────────────────────

def _candidate_dict(orm: ResdexCandidateORM) -> dict:
    d = {k: getattr(orm, k) for k in CANDIDATE_FIELDS}
    d.update(
        id=orm.id, search_id=orm.search_id, job_url=orm.job_url, rank=orm.rank,
        matched_skills=list(orm.matched_skills or []), skill_coverage=orm.skill_coverage,
        extraction_method=orm.extraction_method, outreach_id=orm.outreach_id,
    )
    return d


async def _persist(search: ResdexSearchORM, cands: list[ResdexCandidateORM]) -> bool:
    async def _write(db):
        db.add(search)
        await db.flush()                     # parent row before FK children
        for c in cands:
            db.add(c)
        return True

    return bool(await db_write(_write))


async def _record(
    *, tenant_id: str, run_id: str, job_url: str, target: dict, status: str,
    requirements: dict | None = None, result: ResdexBatchResult | None = None,
    error: str | None = None, picks: list[tuple[dict, str, float, list[str]]] | None = None,
) -> list[dict]:
    """Store one search (+ its picked candidates); returns the candidate dicts, or []
    when nothing was picked or the write failed (an unstored candidate is never
    emailed — the detail page and the outreach stamp both need the row)."""
    search_id = str(uuid.uuid4())
    search = ResdexSearchORM(
        id=search_id, tenant_id=tenant_id, run_id=run_id, job_url=job_url,
        job_title=(target.get("job_title") or "")[:500],
        company=(target.get("company") or "")[:500],
        requirements=requirements, boolean_query=(result.query if result else "") or "",
        filters=(result.filters if result else None) or None,
        tier=result.tier if result else 0, searches_used=result.searches_used if result else 0,
        status=status, result_count=result.result_count if result else 0,
        error=error if error is not None else (result.error if result else None),
        # Origin of the contact that triggered the search (detail-page badge).
        contact_source=(target.get("contact_origin") or "")[:30],
    )
    rows: list[ResdexCandidateORM] = []
    for rank, (fields, method, coverage, matched) in enumerate(picks or [], start=1):
        card_raw = fields.pop("_raw", None)
        rows.append(ResdexCandidateORM(
            id=str(uuid.uuid4()), tenant_id=tenant_id, search_id=search_id, job_url=job_url,
            rank=rank, matched_skills=matched, skill_coverage=round(coverage, 4),
            extraction_method=method, raw=card_raw,
            **{k: fields.get(k) for k in CANDIDATE_FIELDS},
        ))
    dicts = [_candidate_dict(r) for r in rows]   # read before the session owns them
    if not await _persist(search, rows):
        logger.warning("resdex_persist_failed", job_url=job_url, status=status)
        return []
    return dicts


async def _recent_matches(tenant_id: str, job_urls: list[str], reuse_days: int
                          ) -> dict[str, list[dict]]:
    """{normalized job_url: candidate dicts} of the newest matched search per job
    within ``reuse_days`` for this tenant."""
    if reuse_days <= 0 or not job_urls:
        return {}
    since = datetime.now(timezone.utc) - timedelta(days=reuse_days)

    async def _q(db):
        stmt = (
            select(ResdexSearchORM)
            .where(
                ResdexSearchORM.tenant_id == tenant_id,
                ResdexSearchORM.job_url.in_(job_urls),
                ResdexSearchORM.status == ResdexSearchStatus.MATCHED,
                ResdexSearchORM.created_at >= since,
            )
            .order_by(ResdexSearchORM.created_at.desc())
        )
        out: dict[str, list[dict]] = {}
        for s in (await db.execute(stmt)).scalars().all():
            if s.job_url not in out and s.candidates:
                out[s.job_url] = [_candidate_dict(c) for c in s.candidates]
        return out

    return await db_read(_q) or {}


# ── Main entry ────────────────────────────────────────────────────────────────

async def prepare_resdex_candidates(
    targets: list[dict], *, run_id: str, skip_check: SkipCheck | None = None,
    batch_runner: BatchRunner | None = None, llm: Any = None,
    login_waiter: LoginWaiter | None = None,
) -> dict[str, list[dict]]:
    """{target job_url: [candidate dicts incl. "id" and "search_id"]} for the
    outreach targets that got Resdex candidates. Never raises — {} on any failure.

    ``skip_check(target) -> True`` drops a target outreach would skip anyway;
    ``batch_runner`` / ``llm`` are injectable for tests (default: the real Resdex
    browser runner and an LLMService on RESDEX_LLM_MODEL).

    When the batch reports session_expired / session_busy, ``login_waiter`` is
    awaited (default — only with the real runner — raises the global "Naukri
    recruiter login needed" request and waits up to LOGIN_WAIT_S). If the person
    logged in, those jobs are searched again ONCE; otherwise their session_expired
    records are kept and the emails go out without candidates, as before."""
    try:
        return await _prepare(
            targets, run_id=run_id, skip_check=skip_check, batch_runner=batch_runner, llm=llm,
            login_waiter=login_waiter,
        )
    except Exception as exc:
        logger.warning("resdex_prepare_failed", run_id=run_id, error=str(exc))
        return {}


async def _select(targets: list[dict], settings: Any, skip_check: SkipCheck | None
                  ) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """({normalized job_url: [targets]} worth a Resdex search, {reason: count} of
    the targets dropped) — filters 1 of the pipeline; the no-JD check is separate
    so it can be recorded."""
    sources = {s.lower() for s in settings.resdex_source_list}
    kinds = {"recruiter"}
    # The company kind is the Apollo company+location HR contact; either flag
    # (RESDEX_INCLUDE_COMPANY_CONTACTS is the legacy alias) enables it.
    if settings.resdex_include_company_contacts or getattr(
        settings, "resdex_include_apollo_contacts", False
    ):
        kinds.add("company")
    if getattr(settings, "resdex_include_jobs_without_email", False):
        kinds.add("none")
    selected: dict[str, list[dict]] = {}
    dropped: dict[str, int] = {}

    def _drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    for t in targets or []:
        view = t.get("view") or {}
        if t.get("contact_kind") not in kinds:
            _drop(f"contact_{t.get('contact_kind') or 'unknown'}_not_enabled")
            continue
        if (view.get("source") or "").strip().lower() not in sources:
            _drop("source_not_enabled")
            continue
        norm = normalize_job_url(t.get("job_url") or view.get("job_url"))
        if not norm:
            _drop("no_job_url")
            continue
        if skip_check is not None:
            try:
                if await skip_check(t):
                    _drop("outreach_would_skip")
                    continue
            except Exception as exc:          # unsure → don't spend quota on it
                logger.warning("resdex_skip_check_failed", job_url=norm, error=str(exc))
                _drop("skip_check_failed")
                continue
        group = selected.setdefault(norm, [])
        # The first target labels the stored search — prefer a non-Apollo contact.
        if group and str(group[0].get("contact_origin") or "").startswith("apollo") \
                and not str(t.get("contact_origin") or "").startswith("apollo"):
            group.insert(0, t)
        else:
            group.append(t)
    return selected, dropped


async def _prepare(
    targets: list[dict], *, run_id: str, skip_check: SkipCheck | None,
    batch_runner: BatchRunner | None, llm: Any, login_waiter: LoginWaiter | None = None,
) -> dict[str, list[dict]]:
    settings = get_settings()
    if not settings.resdex_enabled:
        return {}
    tenant_id = get_current_tenant_id()
    selected, dropped = await _select(targets, settings, skip_check)
    # Always logged — a pass that searches nothing must say why (e.g. every job
    # had only an Apollo HR contact while RESDEX_INCLUDE_APOLLO_CONTACTS /
    # RESDEX_INCLUDE_COMPANY_CONTACTS are both off).
    logger.info("resdex_selection", run_id=run_id, targets=len(targets or []),
                selected=len(selected), dropped=dropped)
    if not selected:
        return {}

    out: dict[str, list[dict]] = {}

    def _assign(norm: str, cands: list[dict]) -> None:
        if cands:
            for t in selected[norm]:
                out[t["job_url"]] = cands

    # No JD and no scraped skills → nothing to build requirements from; recorded
    # for the detail page. (A JD can be empty when the LLM was down at scrape time;
    # the title + scraped skills still make a usable search.)
    searchable: dict[str, dict] = {}
    for norm, ts in selected.items():
        view = ts[0].get("view") or {}
        if not (view.get("job_description") or "").strip() and not view.get("skills"):
            await _record(tenant_id=tenant_id, run_id=run_id, job_url=norm, target=ts[0],
                          status=ResdexSearchStatus.SKIPPED_NO_JD)
            continue
        searchable[norm] = view

    reused = await _recent_matches(tenant_id, list(searchable), int(settings.resdex_reuse_days))
    for norm, cands in reused.items():
        _assign(norm, cands)
        searchable.pop(norm, None)
    if reused:
        logger.info("resdex_reused_searches", run_id=run_id, jobs=len(reused))
    if not searchable:
        return out

    if llm is None:
        llm = _default_llm(settings)
    timeout_s = float(settings.resdex_llm_timeout_s)
    sem = asyncio.Semaphore(_LLM_CONCURRENCY)

    async def _req(norm: str, view: dict) -> tuple[str, dict]:
        async with sem:
            return norm, await _extract_requirements(llm, view, norm, timeout_s)

    requirements = dict(await asyncio.gather(*(_req(n, v) for n, v in searchable.items())))

    jobs: list[ResdexJobQuery] = []
    for norm, req in requirements.items():
        tiers = build_tiers(req, int(settings.resdex_max_searches_per_job),
                            int(settings.resdex_active_within_months),
                            fixed_tier=int(getattr(settings, "resdex_search_tier", 0) or 0))
        if tiers:
            jobs.append(ResdexJobQuery(job_url=norm, tiers=tiers))
        else:
            await _record(tenant_id=tenant_id, run_id=run_id, job_url=norm,
                          target=selected[norm][0], status=ResdexSearchStatus.ERROR,
                          requirements=req, error="no searchable requirements")
    if not jobs:
        return out

    runner = batch_runner or _default_batch_runner
    waiter = login_waiter if login_waiter is not None else (
        _default_login_waiter if batch_runner is None else None
    )
    batch_started = datetime.now(timezone.utc)
    try:
        results = dict(await runner(jobs, headless=False, min_results=_MIN_RESULTS) or {})
        if waiter is not None:
            await _retry_after_login(runner, waiter, jobs, results, batch_started, run_id)
    except Exception as exc:
        logger.warning("resdex_batch_failed", run_id=run_id, jobs=len(jobs), error=str(exc))
        results = {}
        for job in jobs:
            await _record(tenant_id=tenant_id, run_id=run_id, job_url=job.job_url,
                          target=selected[job.job_url][0], status=ResdexSearchStatus.ERROR,
                          requirements=requirements[job.job_url], error=str(exc)[:1000])
        return out

    # Profile extraction (LLM-bound) runs concurrently across jobs; the budget
    # reservation + DB write below then go job by job, in batch order.
    async def _picks(job: ResdexJobQuery):
        result = results.get(job.job_url)
        if not isinstance(result, ResdexBatchResult):
            return None
        return await _pick(result, requirements[job.job_url], llm, job.job_url, settings, sem,
                           timeout_s)

    all_picks = await asyncio.gather(*(_picks(j) for j in jobs))

    for job, picks in zip(jobs, all_picks):
        norm = job.job_url
        req = requirements[norm]
        result = results.get(norm)
        if picks is None or not isinstance(result, ResdexBatchResult):
            await _record(tenant_id=tenant_id, run_id=run_id, job_url=norm,
                          target=selected[norm][0], status=ResdexSearchStatus.ERROR,
                          requirements=req, error="no result from the Resdex runner")
            continue
        status = ResdexSearchStatus.MATCHED if picks else (
            result.status if result.status and result.status != ResdexSearchStatus.MATCHED
            else ResdexSearchStatus.NO_RESULTS
        )
        error = None
        if picks:
            granted = await try_reserve_candidates(len(picks))
            if granted < len(picks):
                logger.info(
                    "resdex_candidate_cap", job_url=norm, wanted=len(picks), granted=granted,
                )
            picks = picks[:granted]
            if not picks:
                status, error = ResdexSearchStatus.CAP_REACHED, "daily candidate cap reached"
        _assign(norm, await _record(
            tenant_id=tenant_id, run_id=run_id, job_url=norm, target=selected[norm][0],
            status=status, requirements=req, result=result, error=error, picks=picks,
        ))

    logger.info(
        "resdex_prepare_complete", run_id=run_id, selected=len(selected),
        searched=len(jobs), reused=len(reused), jobs_with_candidates=len(
            {normalize_job_url(u) for u in out}
        ),
    )
    return out


async def _retry_after_login(
    runner: BatchRunner, waiter: LoginWaiter, jobs: list[ResdexJobQuery],
    results: dict[str, ResdexBatchResult], since: datetime, run_id: str,
) -> None:
    """Jobs the batch couldn't search because the recruiter session was gone (or the
    login window held the profile): wait for a human login, then search them again
    ONCE, replacing their results in place. A failed wait / retry keeps the original
    session_expired / session_busy results. Never raises."""
    blocked = [
        j for j in jobs
        if isinstance(results.get(j.job_url), ResdexBatchResult)
        and results[j.job_url].status in _LOGIN_STATUSES
    ]
    if not blocked:
        return
    logger.info("resdex_waiting_for_login", run_id=run_id, jobs=len(blocked))
    try:
        logged_in = bool(await waiter(since))
    except Exception as exc:
        logger.warning("resdex_login_wait_failed", run_id=run_id, error=str(exc))
        return
    if not logged_in:
        logger.info("resdex_login_not_completed", run_id=run_id, jobs=len(blocked))
        return
    try:
        retry = await runner(blocked, headless=False, min_results=_MIN_RESULTS) or {}
    except Exception as exc:
        logger.warning("resdex_retry_batch_failed", run_id=run_id, error=str(exc))
        return
    replaced = 0
    for j in blocked:
        r = retry.get(j.job_url)
        if isinstance(r, ResdexBatchResult):
            results[j.job_url] = r
            replaced += 1
    logger.info("resdex_retried_after_login", run_id=run_id, jobs=len(blocked), replaced=replaced)


async def _pick(
    result: ResdexBatchResult, req: dict, llm: Any, job_url: str, settings: Any,
    sem: asyncio.Semaphore, timeout_s: float,
) -> list[tuple[dict, str, float, list[str]]]:
    """Walk the cards in Resdex order, extracting lazily (a text card costs an LLM
    call), and keep those whose mandatory-skill coverage clears the threshold,
    up to RESDEX_CANDIDATES_PER_JOB."""
    want = max(0, int(settings.resdex_candidates_per_job))
    threshold = float(settings.resdex_min_skill_coverage)
    picks: list[tuple[dict, str, float, list[str]]] = []
    seen: set[str] = set()
    for card in (result.cards or [])[:_MAX_CARDS_EXAMINED]:
        if len(picks) >= want:
            break
        if card.method == "json":
            got = await _card_fields(llm, card, job_url, timeout_s)
        else:
            async with sem:
                got = await _card_fields(llm, card, job_url, timeout_s)
        if not got:
            continue
        fields, method = got
        key = _dedupe_key(fields, card)
        if key and key in seen:
            continue
        coverage, matched = skill_coverage(fields, req)
        if coverage < threshold:
            continue
        if key:
            seen.add(key)
        fields["_raw"] = card.raw if card.raw is not None else (
            {"text": card.text} if card.text else None
        )
        picks.append((fields, method, coverage, matched))
    return picks


# ── Email block ───────────────────────────────────────────────────────────────

_ROLE_MAX = 60
_SKILLS_MAX = 5
_CONTACTISH_RE = re.compile(
    r"https?://\S+|www\.\S+|[\w.+-]+@[\w-]+\.[\w.-]+|\+?\d[\d\s\-()]{7,}\d", re.I,
)


def _scrub(text: str, cand: dict) -> str:
    """Remove anything identifying from a free-text role line: URLs, emails, phone
    numbers, the candidate's name and their employers (current + previous)."""
    s = _CONTACTISH_RE.sub(" ", text or "")
    names = [cand.get("candidate_name") or "", cand.get("current_company") or ""]
    for role in cand.get("previous_roles") or []:
        if isinstance(role, str) and re.search(r"\s(?:at|@)\s", role):
            names.append(re.split(r"\s(?:at|@)\s", role)[-1])
    for n in names:
        n = n.strip()
        if len(n) >= 2:
            s = re.sub(
                r"\s*(?:\bat\b|@|,|-|\||\()?\s*(?<!\w)" + re.escape(n) + r"(?!\w)\)?",
                " ", s, flags=re.I,
            )
    s = " ".join(s.split()).strip(" ,-|@·")
    s = re.sub(r"\s+(?:at|@)$", "", s, flags=re.I).strip()
    return s


def _truncate(text: str, limit: int = _ROLE_MAX) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,-|·")
    return (cut or text[:limit]) + "…"


def _years(cand: dict) -> str:
    y = cand.get("experience_years")
    if isinstance(y, (int, float)) and not isinstance(y, bool) and y >= 0:
        y = round(float(y), 1)
        n = int(y) if y.is_integer() else y
        return f"{n} yr" if n == 1 else f"{n} yrs"
    return (cand.get("experience_text") or "").strip()


_NAME_MAX = 60
_NOTICE_PREFIX_RE = re.compile(r"^notice(?:\s+period)?\s*[:\-]\s*", re.I)


def _notice(cand: dict) -> str:
    """The notice-period value without a leading "Notice:"/"Notice period:" label
    (the block supplies its own label): "Notice: 30 days" → "30 days"."""
    n = " ".join((cand.get("notice_period") or "").split())
    return _NOTICE_PREFIX_RE.sub("", n) or n


def _display_name(cand: dict) -> str:
    """The candidate's name, whitespace-collapsed; an ALL-CAPS / all-lower Resdex
    name is title-cased. "" when missing or when it looks like a contact detail."""
    name = " ".join((cand.get("candidate_name") or "").split())
    if not name or _CONTACTISH_RE.search(name):
        return ""
    if name.isupper() or name.islower():
        name = name.title()
    return _truncate(name, _NAME_MAX)


def _initials(name: str) -> str:
    words = [w for w in name.split() if w[:1].isalpha()]
    if not words:
        return ""
    return (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()


def _split_skills(cand: dict) -> tuple[list[str], list[str]]:
    """(matched, other) display skills: the JD-matched ones first, then the rest of
    the profile's skills, de-duplicated across both and capped at _SKILLS_MAX total."""
    matched: list[str] = []
    other: list[str] = []
    keys: set[str] = set()
    for bucket, source in ((matched, cand.get("matched_skills")), (other, cand.get("skills"))):
        for s in source or []:
            if len(matched) + len(other) >= _SKILLS_MAX:
                return matched, other
            s = " ".join(str(s or "").split())
            k = term_key(s)
            if s and k and k not in keys and not _CONTACTISH_RE.search(s):
                keys.add(k)
                bucket.append(s)
    return matched, other


def _candidate_view(cand: dict) -> dict:
    """The display fields shared by the plain-text block and the HTML cards, so the
    two parts of the email always show the same thing. Never carries contacts, the
    profile URL, the employer or CTC; missing parts are ""/[]."""
    role = _scrub(cand.get("current_designation") or "", cand) or _scrub(
        cand.get("headline") or "", cand
    )
    matched, other = _split_skills(cand)
    return {
        "name": _display_name(cand),
        "role": _truncate(role),
        "facts": [(label, value) for label, value in (
            ("Experience", _years(cand)),
            ("Location", _scrub(cand.get("location") or "", cand)),
            ("Notice period", _notice(cand)),
        ) if value],
        "matched": matched,
        "other": other,
    }


def show_naukri_verified() -> bool:
    """RESDEX_SHOW_NAUKRI_VERIFIED: the "✓ Naukri Verified" badge on every candidate
    card (email text + HTML parts); False → no badge anywhere. Default on."""
    return bool(getattr(get_settings(), "resdex_show_naukri_verified", True))


_BLOCK_TITLE = "MATCHING PROFILES FROM OUR NETWORK"
_BLOCK_CTA = "Happy to share full profiles on request. Just reply to this email."
_BLOCK_MATCH_NOTE = "Matched skills are the ones your job description asks for."
_VERIFIED_TEXT = "✓ Naukri Verified"
_LABEL_WIDTH = len("Matched skills")


def _shortlist_line(job_title: str) -> str:
    title = " ".join((job_title or "").split())
    return f"Shortlisted for your {title} requirement" if title else ""


def format_candidate_block(cands: list[dict], job_title: str = "") -> str:
    """The plain-text candidate block for the outreach email (the text part, and the
    stored body); render_candidate_cards_html draws the same data as cards in the HTML
    part. Labels sit on the left, values on the right:

        MATCHING PROFILES FROM OUR NETWORK
        Shortlisted for your Java Developer requirement
        ====================================================

        1. Ravi Kumar  ✓ Naukri Verified
           Senior Java Developer
           ----------------------------------------------
           Experience     : 7 yrs
           Location       : Bengaluru
           Notice period  : 30 days
           Matched skills : Java, Spring Boot
           Other skills   : Microservices, AWS

        ====================================================
        Happy to share full profiles on request. Just reply to this email.
        Matched skills are the ones your job description asks for.

    Every heading carries "✓ Naukri Verified" (RESDEX_SHOW_NAUKRI_VERIFIED=false
    drops it). Includes the candidate's name but never contacts, profile URL,
    employer or CTC; missing parts are simply left out. "" for none."""
    if not cands:
        return ""
    rule = "=" * 52
    badge = f"  {_VERIFIED_TEXT}" if show_naukri_verified() else ""
    lines = [_BLOCK_TITLE]
    if _shortlist_line(job_title):
        lines.append(_shortlist_line(job_title))
    lines += [rule, ""]
    any_matched = False
    for i, cand in enumerate(cands, start=1):
        v = _candidate_view(cand)
        heading = v["name"] or v["role"] or f"Candidate {i}"
        lines.append(f"{i}. {heading}{badge}")
        if v["name"] and v["role"]:
            lines.append(f"   {v['role']}")
        rows = list(v["facts"])
        if v["matched"]:
            rows.append(("Matched skills", ", ".join(v["matched"])))
            any_matched = True
        if v["other"]:
            rows.append(("Other skills", ", ".join(v["other"])))
        if rows:
            lines.append("   " + "-" * 46)
            lines += [f"   {label.ljust(_LABEL_WIDTH)} : {value}" for label, value in rows]
        lines.append("")
    lines += [rule, _BLOCK_CTA]
    if any_matched:
        lines.append(_BLOCK_MATCH_NOTE)
    return "\n".join(lines)


# Card palette — the signature card's navy/violet (email_service.render_signature_html).
_C_NAVY = "#201a4a"
_C_VIOLET = "#7b53c9"
_C_MUTED = "#8a879f"
_C_TEXT = "#3f3d55"
_C_BORDER = "#e0d9f0"
_C_DIVIDER = "#eee9f7"
_C_TINT = "#faf8fe"
_C_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_C_LABEL = (
    f"font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:{_C_MUTED};"
    "font-weight:600;line-height:1.3;"
)
_C_VALUE = f"font-size:13px;color:{_C_NAVY};font-weight:600;line-height:1.4;"


def _chip(skill: str, matched: bool) -> str:
    look = (
        "background:#ece6f8;color:#4b2f8f;border:1px solid #d6c9f0;font-weight:600;"
        if matched else "background:#f4f4f7;color:#5c5a70;border:1px solid #e4e3ea;"
    )
    return (
        '<span style="display:inline-block;margin:0 4px 4px 0;padding:2px 9px;'
        f'border-radius:999px;font-size:12px;line-height:18px;{look}">'
        f"{html_lib.escape(skill)}</span>"
    )


def _verified_badge_html() -> str:
    """The "✓ Naukri Verified" pill (top-right of each card): text only (no image/SVG/
    web font), inline styles. Outlook's Word engine ignores padding/radius on inline
    spans, so there it degrades to green text on a light-green tint."""
    return (
        '<span style="display:inline-block;padding:1px 8px;border-radius:999px;'
        "background:#e9f7ef;border:1px solid #bfe5cf;color:#16794a;"
        f"font-family:{_C_FONT};font-size:11px;line-height:16px;font-weight:700;"
        'white-space:nowrap;">&#10003;&nbsp;Naukri Verified</span>'
    )


def _card_html(v: dict, index: int) -> str:
    esc = html_lib.escape
    title = v["name"] or v["role"] or f"Candidate {index}"
    subtitle = v["role"] if v["name"] else ""
    badge = _initials(v["name"]) or f"C{index}"
    header = (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;"><tr>'
        '<td valign="middle" width="44" style="width:44px;">'
        f'<div style="width:44px;height:44px;line-height:44px;border-radius:50%;'
        f"background:{_C_NAVY};color:#ffffff;text-align:center;font-family:{_C_FONT};"
        f'font-size:15px;font-weight:700;">{esc(badge)}</div></td>'
        f'<td valign="middle" style="padding-left:12px;font-family:{_C_FONT};">'
        f'<div style="font-size:15px;font-weight:700;color:{_C_NAVY};line-height:1.3;">'
        f"{esc(title)}</div>"
        + (
            f'<div style="font-size:13px;font-weight:600;color:{_C_VIOLET};line-height:1.4;">'
            f"{esc(subtitle)}</div>" if subtitle else ""
        )
        + "</td></tr></table>"
    )
    if show_naukri_verified():
        # 2-cell header: avatar + name/role on the left, the badge top-right.
        header = (
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'border="0" style="border-collapse:collapse;"><tr>'
            f'<td valign="top">{header}</td>'
            '<td align="right" valign="top" style="padding-left:8px;white-space:nowrap;">'
            f"{_verified_badge_html()}</td></tr></table>"
        )

    facts = "".join(
        f'<div style="{_C_LABEL}">{esc(label)}</div>'
        f'<div style="{_C_VALUE}{"" if n == len(v["facts"]) - 1 else "margin-bottom:9px;"}">'
        f"{esc(value)}</div>"
        for n, (label, value) in enumerate(v["facts"])
    )
    skills = "".join(
        f'<div style="{_C_LABEL}margin:0 0 5px;">{label}</div>'
        f'<div style="margin:0 0 6px;">{"".join(_chip(s, on) for s in items)}</div>'
        for label, items, on in (
            ("Matched skills", v["matched"], True), ("Other skills", v["other"], False),
        ) if items
    )
    # Details row: facts down a tinted left column, skills on the right. Either side
    # alone takes the full width; with neither, the card is just the header.
    cells = ""
    if facts and skills:
        cells = (
            f'<td valign="top" width="130" style="width:130px;padding:12px 14px;'
            f"background:{_C_TINT};border-right:1px solid {_C_DIVIDER};"
            f'border-bottom-left-radius:10px;font-family:{_C_FONT};">{facts}</td>'
            f'<td valign="top" style="padding:12px 14px 6px;font-family:{_C_FONT};">{skills}</td>'
        )
    elif facts or skills:
        cells = (
            f'<td valign="top" style="padding:12px 14px 6px;font-family:{_C_FONT};">'
            f"{facts or skills}</td>"
        )
    details = (
        '<tr><td style="padding:0;"><table role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="0" border="0" style="border-collapse:collapse;"><tr>{cells}</tr>'
        "</table></td></tr>" if cells else ""
    )
    header_border = f"border-bottom:1px solid {_C_DIVIDER};" if cells else ""
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f"style=\"border-collapse:separate;margin:0 0 12px;background:#ffffff;"
        f'border:1px solid {_C_BORDER};border-radius:10px;">'
        f'<tr><td style="padding:13px 14px 12px;{header_border}">{header}</td></tr>'
        f"{details}</table>"
    )


def render_candidate_cards_html(cands: list[dict], job_title: str = "") -> str:
    """The HTML-part counterpart of format_candidate_block: one profile card per
    candidate (initials badge, name, role; Experience / Location / Notice period down
    the left; matched + other skills as pills on the right). Table-based with inline
    styles only so Gmail/Outlook/Apple Mail render it alike, and stacked in one column
    so it works on phones. email_service draws it in place of the text block. "" for
    none."""
    if not cands:
        return ""
    views = [_candidate_view(c) for c in cands]
    shortlist = _shortlist_line(job_title)
    note = (
        f'<div style="font-size:11.5px;color:{_C_MUTED};margin-top:3px;">'
        f"{html_lib.escape(_BLOCK_MATCH_NOTE)}</div>"
        if any(v["matched"] for v in views) else ""
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="max-width:580px;border-collapse:collapse;margin:4px 0 0;">'
        f'<tr><td style="padding:0 0 8px;font-family:{_C_FONT};">'
        f'<div style="font-size:11px;letter-spacing:.14em;text-transform:uppercase;'
        f'font-weight:700;color:{_C_VIOLET};">{html_lib.escape(_BLOCK_TITLE.title())}</div>'
        + (
            f'<div style="font-size:12.5px;color:{_C_MUTED};margin-top:2px;">'
            f"{html_lib.escape(shortlist)}</div>" if shortlist else ""
        )
        + "</td></tr>"
        f'<tr><td>{"".join(_card_html(v, i) for i, v in enumerate(views, start=1))}</td></tr>'
        f'<tr><td style="padding:2px 0 0;font-family:{_C_FONT};font-size:13px;color:{_C_TEXT};">'
        f"{html_lib.escape(_BLOCK_CTA)}{note}</td></tr>"
        "</table>"
    )


async def mark_candidates_sent(candidate_ids: list[str], outreach_id: str) -> None:
    """Record that this outreach email included these candidates. Never raises.

    A matched search is reused for later contacts of the same posting (e.g. the next
    Apollo HR contact), so a candidate can go out in several emails: each pair gets a
    resdex_candidate_emails row, and resdex_candidates.outreach_id keeps the FIRST
    email (only stamped while empty) so an earlier email never loses its candidates."""
    ids = list(dict.fromkeys(c for c in candidate_ids or [] if c))
    if not ids or not outreach_id:
        return

    async def _stamp(db):
        await db.execute(
            update(ResdexCandidateORM)
            .where(ResdexCandidateORM.id.in_(ids), ResdexCandidateORM.outreach_id.is_(None))
            .values(outreach_id=outreach_id)
        )
        rows = (await db.execute(
            select(ResdexCandidateORM.id, ResdexCandidateORM.tenant_id)
            .where(ResdexCandidateORM.id.in_(ids))
        )).all()
        linked = set((await db.execute(
            select(ResdexCandidateEmailORM.candidate_id).where(
                ResdexCandidateEmailORM.outreach_id == outreach_id,
                ResdexCandidateEmailORM.candidate_id.in_(ids),
            )
        )).scalars())
        for cid, tid in rows:
            if cid not in linked:
                db.add(ResdexCandidateEmailORM(
                    id=str(uuid.uuid4()), tenant_id=tid, candidate_id=cid, outreach_id=outreach_id,
                ))
        return True

    try:
        await db_write(_stamp)
    except Exception as exc:  # db_write already swallows; belt-and-braces
        logger.warning("resdex_mark_sent_failed", outreach_id=outreach_id, error=str(exc))
