"""
Orchestrator Agent — unified entry point for every harvest execution.

Two public methods
──────────────────
run()       Legacy interface — used by /run-harvest and the scheduler.
            Returns (list[HarvestJob], source_label_str).

run_all()   Full pipeline — used by POST /run-harvest-agent.
            Executes sources in priority order, applies business filters,
            deduplicates cross-source, returns OrchestratorResult.

Source priority (fixed):
  1. Naukri
  2. LinkedIn
  3. Dice

Extensibility
─────────────
To add a new source (e.g. Indeed):
  • Create  app/agents/indeed_agent.py  with  class IndeedAgent
  • Add     "indeed": bool  to  SourcesConfig
  • Add an  "indeed"  block in  _collect_all()  below
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import structlog

from app.agents.linkedin_agent import (
    LINKEDIN_SESSION_FILE,
    LinkedInAgent,
    LinkedInLoginError,
    LinkedInScrapedJob,
)
from app.core.exceptions import LLMUnavailableError
from app.models.harvest_models import HarvestConfig
from app.models.response_models import HarvestJob
from app.models.unified_job import UnifiedJob
from app.services.business_filter_service import BusinessFilterService

logger = structlog.get_logger(__name__)


def _filter_by_date_window(jobs: list[UnifiedJob], window_hours: int) -> list[UnifiedJob]:
    """
    Secondary safety filter — drop jobs whose posted_date falls outside
    the configured search window.

    This is a backstop for the rare case where a job board ignores the
    URL-level time filter (jobAge / f_TPR / datePosted) and returns older
    listings. Jobs with a missing or unparseable posted_date are kept so
    we never silently discard valid records.
    """
    if not window_hours or window_hours <= 0:
        return jobs

    cutoff = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # Go back ceil(window_hours/24) whole calendar days from today's UTC midnight.
    # We deliberately allow one extra calendar day beyond the strict window: this
    # is a calendar-DATE comparison, and boards report coarse, day-granular dates
    # (LinkedIn's "1 day ago" resolves to *yesterday*), so a job well inside a
    # rolling 24h window routinely lands on yesterday's date. Without the extra
    # day, a 24h window meant "posted today (UTC)" and silently dropped genuinely
    # recent jobs that the board's own time filter had already included.
    import math
    extra_days = math.ceil(window_hours / 24)
    from datetime import timedelta
    cutoff = cutoff - timedelta(days=extra_days)

    kept = []
    dropped = 0
    for job in jobs:
        raw = (job.posted_date or "").strip()
        if not raw:
            kept.append(job)   # no date — keep, don't silently discard
            continue
        try:
            # Handles ISO date (2026-06-24) and ISO datetime strings
            pd = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if pd.tzinfo is None:
                pd = pd.replace(tzinfo=timezone.utc)
            if pd.date() >= cutoff.date():
                kept.append(job)
            else:
                dropped += 1
        except ValueError:
            kept.append(job)   # unparseable date — keep

    if dropped:
        logger.info(
            "date_window_filter",
            window_hours=window_hours,
            cutoff_date=cutoff.date().isoformat(),
            before=len(jobs),
            kept=len(kept),
            dropped=dropped,
        )
    return kept


def _deduplicate(jobs: list[UnifiedJob]) -> list[UnifiedJob]:
    """Remove duplicates across all sources by job_url, then company+title."""
    seen_urls: set[str] = set()
    seen_ct:   set[str] = set()
    deduped:   list[UnifiedJob] = []
    for job in jobs:
        url_key = job.job_url.split("?")[0].rstrip("/").lower() if job.job_url else ""
        ct_key  = (
            re.sub(r"\s+", " ", job.company.lower().strip())
            + "::"
            + re.sub(r"\s+", " ", job.job_title.lower().strip())
        )
        if (url_key and url_key in seen_urls) or ct_key in seen_ct:
            continue
        if url_key:
            seen_urls.add(url_key)
        seen_ct.add(ct_key)
        deduped.append(job)
    return deduped


def _cross_source_enrich(jobs: list[UnifiedJob]) -> list[UnifiedJob]:
    """
    Cross-source lead enrichment.

    Builds a lookup from Naukri jobs (which have the richest contact data) keyed
    by (recruiter_name_lower, company_lower).  LinkedIn and Dice jobs that share
    the same recruiter name + company but are missing email / phone / LinkedIn URL
    are enriched from the matching Naukri record.
    """
    # Build index from Naukri records that have at least a name
    naukri_index: dict[str, UnifiedJob] = {}
    for j in jobs:
        if j.source == "Naukri" and j.job_poster_name:
            key = (
                re.sub(r"\s+", "", j.job_poster_name.lower())
                + "::"
                + re.sub(r"\s+", "", (j.current_company or j.company or "").lower())
            )
            if key not in naukri_index:
                naukri_index[key] = j

    enriched_count = 0
    for job in jobs:
        if job.source == "Naukri":
            continue
        if not job.job_poster_name:
            continue
        key = (
            re.sub(r"\s+", "", job.job_poster_name.lower())
            + "::"
            + re.sub(r"\s+", "", (job.current_company or job.company or "").lower())
        )
        match = naukri_index.get(key)
        if not match:
            continue
        changed = False
        if not job.email_id and match.email_id:
            job.email_id = match.email_id
            changed = True
        if not job.contact_number and match.contact_number:
            job.contact_number = match.contact_number
            changed = True
        if not job.job_poster_designation and match.job_poster_designation:
            job.job_poster_designation = match.job_poster_designation
            changed = True
        if changed:
            enriched_count += 1

    logger.info("cross_source_enrichment_complete", enriched=enriched_count)
    return jobs


# Fixed execution priority — lower index = runs first
_SOURCE_PRIORITY = ["naukri", "linkedin", "dice"]


# ── Converters: source-specific dataclass → UnifiedJob ────────────────────────

def _naukri_to_unified(j: "NaukriScrapedJob") -> UnifiedJob:  # type: ignore[name-defined]
    return UnifiedJob(
        job_title               = j.job_title,
        company                 = j.company,
        location                = j.location,
        salary                  = j.salary,
        experience              = j.experience,
        posted_date             = j.posted_date,
        job_url                 = j.job_url,
        job_description         = j.job_description,
        skills                  = j.skills,
        work_mode               = j.work_mode,
        source                  = "Naukri",
        job_poster_name         = getattr(j, "recruiter_name", None),
        job_poster_designation  = getattr(j, "job_poster_designation", None),
        current_company         = getattr(j, "recruiter_company", None),
        email_id                = getattr(j, "email_id", None),
        contact_number          = getattr(j, "contact_number", None),
    )


def _linkedin_to_unified(j: LinkedInScrapedJob) -> UnifiedJob:
    return UnifiedJob(
        job_title               = j.job_title,
        company                 = j.company,
        location                = j.location,
        salary                  = j.salary,
        experience              = j.experience,
        posted_date             = j.posted_date,
        job_url                 = j.job_url,
        job_description         = j.job_description,
        skills                  = j.skills,
        work_mode               = j.work_mode,
        source                  = "LinkedIn",
        company_url             = getattr(j, "company_url", ""),
        employment_type         = getattr(j, "employment_type", ""),
        job_type                = getattr(j, "employment_type", ""),
        domain_hint             = getattr(j, "industry_hint", ""),
        job_poster_name         = getattr(j, "job_poster_name", None),
        job_poster_designation  = getattr(j, "job_poster_designation", None),
        linkedin_profile_url    = getattr(j, "linkedin_profile_url", None),
        current_company         = getattr(j, "job_poster_company", None),
        email_id                = getattr(j, "job_poster_email", None),
        contact_number          = getattr(j, "job_poster_phone", None),
    )


def _dice_to_unified(j: "DiceScrapedJob") -> UnifiedJob:  # type: ignore[name-defined]
    return UnifiedJob(
        job_title               = j.job_title,
        company                 = j.company,
        location                = j.location,
        salary                  = j.salary,
        experience              = j.experience,
        posted_date             = j.posted_date,
        job_url                 = j.job_url,
        job_description         = j.job_description,
        skills                  = j.skills,
        work_mode               = j.work_mode,
        source                  = "Dice",
        company_url             = getattr(j, "company_url", ""),
        employment_type         = getattr(j, "employment_type", ""),
        job_type                = getattr(j, "employment_type", ""),
        job_poster_name         = getattr(j, "recruiter_name", None),
        job_poster_designation  = getattr(j, "job_poster_designation", None),
        current_company         = getattr(j, "recruiter_company", None),
        email_id                = getattr(j, "email_id", None),
        contact_number          = getattr(j, "contact_number", None),
        linkedin_profile_url    = getattr(j, "linkedin_profile_url", None),
    )


# ── OrchestratorResult ─────────────────────────────────────────────────────────

@dataclass
class OrchestratorResult:
    """Rich result object returned by run_all()."""
    sources_executed: list[str]                   = field(default_factory=list)
    jobs_by_source:   dict[str, list[UnifiedJob]] = field(default_factory=dict)
    all_jobs:         list[UnifiedJob]            = field(default_factory=list)
    started_at:       datetime                    = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at:     datetime                    = field(default_factory=lambda: datetime.now(timezone.utc))
    combined_path:    str                         = ""
    excel_path:       str                         = ""
    # Cumulative Claude/Ollama token usage from the LinkedIn LLM fallback
    # (see LinkedInAgent.get_token_usage()) — {} until _collect_all() runs.
    token_usage:      dict                         = field(default_factory=dict)
    # Per-call LLM audit log from the LinkedIn LLM fallback (see
    # LinkedInAgent.get_llm_call_log()) — [] if LinkedIn wasn't run or never
    # triggered the fallback. Naukri/Dice never call an LLM.
    llm_calls:        list[dict]                   = field(default_factory=list)

    @property
    def total_jobs(self) -> int:
        return len(self.all_jobs)

    @property
    def verified_jobs(self) -> int:
        return sum(1 for j in self.all_jobs if j.verification_status == "verified")

    @property
    def direct_clients(self) -> int:
        return sum(1 for j in self.all_jobs if j.hiring_entity == "Direct Client")

    @property
    def gcc(self) -> int:
        return sum(1 for j in self.all_jobs if j.hiring_entity == "GCC")

    @property
    def staffing_firms(self) -> int:
        return sum(1 for j in self.all_jobs if j.hiring_entity == "Staffing Firm")

    @property
    def ambiguous(self) -> int:
        return sum(1 for j in self.all_jobs if j.hiring_entity == "Ambiguous")


# ══════════════════════════════════════════════════════════════════════════════
# OrchestratorAgent
# ══════════════════════════════════════════════════════════════════════════════

class OrchestratorAgent:
    """
    Routes a HarvestConfig to source agents and returns aggregated results.

    Usage (new pipeline)::

        config = ConfigService().load()
        orch   = OrchestratorAgent(config)
        result = await orch.run_all()   # OrchestratorResult

    Usage (legacy — backward compat)::

        config     = ConfigService().load()
        orch       = OrchestratorAgent(config)
        jobs, src  = await orch.run()   # list[HarvestJob], str
    """

    def __init__(self, config: HarvestConfig) -> None:
        self._config = config

    # ── Full pipeline (POST /run-harvest-agent) ───────────────────────────────

    async def run_all(
        self,
        wait_for_login: bool = False,
        on_status: Callable[[str], Awaitable[None]] | None = None,
    ) -> OrchestratorResult:
        """
        Execute all enabled sources in priority order, apply business filters,
        deduplicate cross-source, save combined results, and return OrchestratorResult.

        wait_for_login  Passed through to LinkedInAgent — when True, a run that
                        finds no authenticated LinkedIn session pauses and waits
                        for a human to log in via the live browser view instead
                        of failing immediately. Only appropriate for manually-
                        triggered runs (POST /run-harvest-agent); leave False for
                        unattended/scheduled runs where no one can log in.
        on_status       Optional async callback for human-readable progress
                        messages (e.g. "waiting for login…"), forwarded to
                        JobTracker by the caller.
        """
        config      = self._config
        started_at  = datetime.now(timezone.utc)
        run_id      = started_at.strftime("%Y%m%d_%H%M%S")
        result      = OrchestratorResult(started_at=started_at)

        # ── Step 1: collect raw jobs from all enabled sources ─────────────────
        try:
            raw_by_source, result.token_usage, result.llm_calls = await self._collect_all(
                config, wait_for_login=wait_for_login, on_status=on_status
            )
        except LLMUnavailableError as exc:
            # The extraction LLM went down mid-scrape. Keep whatever was
            # collected (unprocessed — we skip dedup/classify/verify since the
            # run is failing anyway) so the caller can persist the partial
            # harvest, then re-raise to mark the run failed.
            from app.services.llm_service import empty_usage_summary

            result.token_usage = getattr(exc, "partial_token_usage", None) or empty_usage_summary()
            result.llm_calls   = getattr(exc, "partial_llm_calls", None) or []
            partial_raw        = getattr(exc, "partial_raw", None) or {}
            partial_jobs: list[UnifiedJob] = []
            for source, jobs in partial_raw.items():
                result.sources_executed.append(source)
                partial_jobs.extend(jobs)
            result.jobs_by_source = dict(partial_raw)
            result.all_jobs       = partial_jobs
            result.completed_at   = datetime.now(timezone.utc)
            exc.partial_result    = result
            logger.error(
                "orchestrator_run_aborted_llm_unavailable",
                error=str(exc), partial_jobs=len(partial_jobs),
            )
            raise

        # ── Step 2: convert to UnifiedJob ─────────────────────────────────────
        all_unified: list[UnifiedJob] = []
        for source, jobs in raw_by_source.items():
            result.sources_executed.append(source)
            result.jobs_by_source[source] = jobs
            all_unified.extend(jobs)

        logger.info("orchestrator_raw_total", total=len(all_unified))

        # ── Step 2a: log batch_saved as we process the unified list ──────────
        _BATCH_SIZE = 100
        for _bi in range(0, len(all_unified), _BATCH_SIZE):
            _batch_n = _bi // _BATCH_SIZE + 1
            logger.info(
                "batch_saved",
                batch        = _batch_n,
                count        = len(all_unified[_bi : _bi + _BATCH_SIZE]),
                total_so_far = min(_bi + _BATCH_SIZE, len(all_unified)),
                stage        = "unified",
            )

        # ── Step 2b: cross-source lead enrichment ────────────────────────────
        from app.services.lead_enrichment_service import LeadEnrichmentService
        all_unified = LeadEnrichmentService().enrich(all_unified)

        # ── Step 3: classify + apply business filters ─────────────────────────
        svc         = BusinessFilterService()
        all_unified = svc.classify_all(all_unified, config.filters)
        all_unified = svc.apply_all(all_unified, config.filters)

        logger.info(
            "classification_completed",
            total         = len(all_unified),
            direct_client = sum(1 for j in all_unified if j.hiring_entity == "Direct Client"),
            gcc           = sum(1 for j in all_unified if j.hiring_entity == "GCC"),
            staffing_firm = sum(1 for j in all_unified if j.hiring_entity == "Staffing Firm"),
            ambiguous     = sum(1 for j in all_unified if j.hiring_entity == "Ambiguous"),
        )

        # ── Step 4: company verification (optional) ───────────────────────────
        if config.filters.verification.enabled:
            from app.agents.verification_agent import VerificationAgent
            verifier    = VerificationAgent(config.filters.verification, headless=True)
            all_unified = await verifier.verify_batch(all_unified)

        # ── Step 5: cross-source deduplication ────────────────────────────────
        _li_before_dedup = sum(1 for j in all_unified if j.source == "LinkedIn")
        logger.info("linkedin_jobs_before_dedup", count=_li_before_dedup)

        before_dedup = len(all_unified)
        all_unified  = _deduplicate(all_unified)
        removed      = before_dedup - len(all_unified)

        _li_after_dedup = sum(1 for j in all_unified if j.source == "LinkedIn")
        logger.info("linkedin_jobs_after_dedup", count=_li_after_dedup,
                    removed_by_dedup=_li_before_dedup - _li_after_dedup)
        logger.info(
            "deduplication_completed",
            before          = before_dedup,
            after           = len(all_unified),
            removed         = removed,
            linkedin_before = _li_before_dedup,
            linkedin_after  = _li_after_dedup,
        )

        # Save debug: linkedin after dedup
        try:
            import json as _json_dd
            _dbg_dd = Path("data/debug/linkedin")
            _dbg_dd.mkdir(parents=True, exist_ok=True)
            (_dbg_dd / "linkedin_after_dedup.json").write_text(
                _json_dd.dumps(
                    {"stage": "after_dedup", "count": _li_after_dedup,
                     "jobs": [{"title": j.job_title, "company": j.company, "url": j.job_url}
                               for j in all_unified if j.source == "LinkedIn"]},
                    indent=2, ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

        # ── Step 5b: enforce search_window_hours on posted_date ──────────────
        # The URL-level filter (jobAge/f_TPR/datePosted) is the primary gate;
        # this is a secondary code-level check to drop any jobs the board
        # returned outside the configured window despite the filter.
        # all_unified = _filter_by_date_window(
        #     all_unified, config.filters.search_window_hours
        # )

        # ── Step 6: rebuild jobs_by_source from deduped set ───────────────────
        deduped_by_source: dict[str, list[UnifiedJob]] = {}
        for job in all_unified:
            deduped_by_source.setdefault(job.source, []).append(job)
        result.jobs_by_source = deduped_by_source

        # ── Step 7: finalize result (DB is the only store) ────────────────────
        # Combined-JSON and Excel result files are no longer written at
        # harvest time — the caller mirrors all_jobs to ScrapedJobORM and
        # reports are generated from those rows (merged with RecruiterORM
        # contact info) on demand: GET /download/{json,excel} and the
        # report email (see app/services/report_service.py).
        result.all_jobs = all_unified

        _li_final = len(deduped_by_source.get("LinkedIn", []))
        logger.info("linkedin_jobs_final", count=_li_final)

        result.completed_at  = datetime.now(timezone.utc)

        elapsed = (result.completed_at - started_at).total_seconds()

        # Save linkedin_summary.json — single file showing all pipeline stages
        try:
            import json as _json_sum
            _dbg_sum = Path("data/debug/linkedin")
            _dbg_sum.mkdir(parents=True, exist_ok=True)
            (_dbg_sum / "linkedin_summary.json").write_text(
                _json_sum.dumps(
                    {
                        "run_id":                         run_id,
                        "linkedin_jobs_extracted":        _li_before_dedup,
                        "linkedin_jobs_received_by_orch": _li_before_dedup,
                        "linkedin_jobs_before_dedup":     _li_before_dedup,
                        "linkedin_jobs_after_dedup":      _li_after_dedup,
                        "linkedin_jobs_final":            _li_final,
                        "root_cause_if_zero":
                            "Check uvicorn_err.txt for UnicodeEncodeError or LinkedInLoginError "
                            "before the linkedin_jobs_received_by_orchestrator log line."
                            if _li_before_dedup == 0 else "OK",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

        lead_count = sum(
            1 for j in all_unified
            if getattr(j, "job_poster_name", None)
            or getattr(j, "email_id", None)
            or getattr(j, "contact_number", None)
        )

        logger.info(
            "harvest_completed",
            run_id         = run_id,
            sources        = result.sources_executed,
            linkedin_jobs  = len(deduped_by_source.get("LinkedIn", [])),
            naukri_jobs    = len(deduped_by_source.get("Naukri", [])),
            dice_jobs      = len(deduped_by_source.get("Dice", [])),
            combined_jobs  = result.total_jobs,
            lead_records   = lead_count,
            verified       = result.verified_jobs,
            elapsed        = round(elapsed, 1),
        )
        return result

    # ── Legacy interface (backward compat) ────────────────────────────────────

    async def run(self) -> tuple[list[HarvestJob], str]:
        """
        Legacy method used by /run-harvest and the APScheduler job.
        Returns (list[HarvestJob], source_label).
        """
        config      = self._config
        all_jobs:   list[HarvestJob] = []
        src_labels: list[str]        = []

        if config.sources.linkedin:
            logger.info("orchestrator_dispatching", source="linkedin")
            session_path   = str(LINKEDIN_SESSION_FILE) if LINKEDIN_SESSION_FILE.exists() else None
            agent          = LinkedInAgent()
            linkedin_scraped = await agent.harvest(
                filters  = config.filters,
                headless = config.browser.headless,
                slow_mo  = config.browser.slow_mo_ms,
            )
            for j in linkedin_scraped:
                all_jobs.append(HarvestJob(
                    title     = j.job_title,
                    company   = j.company,
                    location  = j.location,
                    posted    = j.posted_date,
                    job_url   = j.job_url,
                    work_mode = j.work_mode,
                    source    = "LinkedIn",
                ))
            src_labels.append("LinkedIn")
            logger.info("orchestrator_source_done", source="linkedin", count=len(linkedin_scraped))

        if config.sources.naukri:
            from app.agents.naukri_agent import NaukriAgent
            logger.info("orchestrator_dispatching", source="naukri")
            naukri_agent   = NaukriAgent()
            naukri_scraped = await naukri_agent.harvest(
                filters  = config.filters,
                headless = config.browser.headless,
                slow_mo  = config.browser.slow_mo_ms,
            )
            for j in naukri_scraped:
                all_jobs.append(HarvestJob(
                    title     = j.job_title,
                    company   = j.company,
                    location  = j.location,
                    posted    = j.posted_date,
                    job_url   = j.job_url,
                    work_mode = j.work_mode,
                    source    = "Naukri",
                ))
            src_labels.append("Naukri")
            logger.info("orchestrator_source_done", source="naukri", count=len(naukri_scraped))

        source_label = ", ".join(src_labels) if src_labels else "none"
        logger.info("orchestrator_complete", total=len(all_jobs), sources=source_label)
        return all_jobs, source_label

    # ── Internal: collect raw UnifiedJobs from all enabled sources ─────────────

    async def _collect_all(
        self,
        config: HarvestConfig,
        wait_for_login: bool = False,
        on_status: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[dict[str, list[UnifiedJob]], dict, list[dict]]:
        """
        Run all enabled source agents IN PARALLEL using a single shared browser
        context (one page per source).  Using a single PersistentBrowserManager
        avoids Chrome profile-lock conflicts that arise when launching multiple
        browser instances against the same profile directory.

        Returns ({source_name: [UnifiedJob, …]}, token_usage, llm_calls) —
        token_usage/llm_calls are the LinkedIn LLM fallback's cumulative
        Claude/Ollama usage and per-call audit log (Naukri and Dice never hit
        the LLM), empty if LinkedIn wasn't enabled or never triggered the
        fallback.
        """
        from app.scrapers.browser_manager import PersistentBrowserManager
        from app.scrapers.dice_scraper import DiceScrapedJob, DiceScraper
        from app.services.llm_service import empty_usage_summary

        results:  dict[str, list[UnifiedJob]] = {}
        # Populated by _harvest_linkedin so token usage survives even if that
        # coroutine raises — the LinkedInAgent instance is created before any
        # of its login/scrape work that could fail.
        linkedin_agent_holder: dict[str, LinkedInAgent] = {}
        enabled = [s for s in _SOURCE_PRIORITY if getattr(config.sources, s, False)]

        if not enabled:
            logger.warning("orchestrator_no_sources_enabled")
            return results, empty_usage_summary(), []

        logger.info("orchestrator_parallel_start", sources=enabled)

        async with PersistentBrowserManager(
            profile_dir = config.browser.chrome_profile,
            headless    = config.browser.headless,
            slow_mo     = config.browser.slow_mo_ms,
        ) as pbm:
            # Open one independent tab per source — they share cookies but
            # each has its own navigation state.
            pages: dict[str, Any] = {}
            for source in enabled:
                pages[source] = await pbm.new_page()

            # ── Per-source harvest coroutines ──────────────────────────────────

            async def _harvest_naukri(page: Any) -> tuple[str, list[UnifiedJob]]:
                from app.agents.naukri_agent import NaukriAgent, NaukriScrapedJob
                logger.info("orchestrator_dispatching", source="naukri")
                try:
                    agent   = NaukriAgent()
                    scraped: list[NaukriScrapedJob] = await agent._run(page, config.filters)
                    unified = [_naukri_to_unified(j) for j in scraped]
                    logger.info("orchestrator_source_done", source="naukri", count=len(unified))
                    logger.info("batch_saved", source="Naukri", count=len(unified))
                    return "Naukri", unified
                except Exception as exc:
                    logger.exception(
                        "orchestrator_naukri_error", error=str(exc),
                        note="Naukri failed; LinkedIn and Dice results are retained",
                    )
                    return "Naukri", []

            async def _harvest_linkedin(page: Any) -> tuple[str, list[UnifiedJob]]:
                logger.info("orchestrator_dispatching", source="linkedin")
                logger.info("linkedin_agent_started")
                try:
                    agent   = LinkedInAgent()
                    linkedin_agent_holder["agent"] = agent
                    scraped: list[LinkedInScrapedJob] = await agent._run(
                        page, config.filters, wait_for_login=wait_for_login, on_status=on_status,
                    )
                    # ── Checkpoint 2: jobs received by orchestrator ────────────
                    logger.info("linkedin_jobs_received_by_orchestrator", count=len(scraped))
                    unified = [_linkedin_to_unified(j) for j in scraped]
                    logger.info("orchestrator_source_done", source="linkedin", count=len(unified))
                    logger.info("batch_saved", source="LinkedIn", count=len(unified))

                    # Save raw debug snapshot for orchestrator stage
                    try:
                        _dbg = Path("data/debug/linkedin")
                        _dbg.mkdir(parents=True, exist_ok=True)
                        import json as _json_o
                        (_dbg / "linkedin_raw_jobs.json").write_text(
                            _json_o.dumps(
                                {"stage": "orchestrator_received", "count": len(unified),
                                 "jobs": [{"title": j.job_title, "company": j.company,
                                           "url": j.job_url} for j in unified]},
                                indent=2, ensure_ascii=False,
                            ),
                            encoding="utf-8",
                        )
                    except Exception:
                        pass

                    return "LinkedIn", unified
                except LinkedInLoginError as exc:
                    logger.warning(
                        "orchestrator_linkedin_login_failed", error=str(exc),
                        note="LinkedIn skipped; other sources are retained",
                    )
                    return "LinkedIn", []
                except LLMUnavailableError:
                    # Extraction LLM is down — do NOT downgrade to an empty
                    # source; let it propagate so _collect_all halts the whole
                    # run (all sources), per product requirement.
                    raise
                except Exception as exc:
                    logger.exception(
                        "orchestrator_linkedin_error", error=str(exc),
                        note="LinkedIn failed; other sources are retained",
                    )
                    return "LinkedIn", []

            async def _harvest_dice(page: Any) -> tuple[str, list[UnifiedJob]]:
                logger.info("orchestrator_dispatching", source="dice")
                logger.info("dice_agent_started")
                try:
                    scraper = DiceScraper(page, config.filters)
                    scraped: list[DiceScrapedJob] = await scraper.run()
                    unified = [_dice_to_unified(j) for j in scraped]
                    logger.info("orchestrator_source_done", source="dice", count=len(unified))
                    logger.info("batch_saved", source="Dice", count=len(unified))
                    return "Dice", unified
                except Exception as exc:
                    logger.exception(
                        "orchestrator_dice_error", error=str(exc),
                        note="Dice failed; other sources are retained",
                    )
                    return "Dice", []

            _RUNNERS = {
                "naukri":   _harvest_naukri,
                "linkedin": _harvest_linkedin,
                "dice":     _harvest_dice,
            }

            # ── Launch all enabled sources concurrently ────────────────────────
            # FIRST_EXCEPTION (not gather-return_exceptions) so an
            # LLMUnavailableError escaping any source stops the wait immediately;
            # benign per-source failures are already caught inside each runner
            # (returned as ("Source", [])), so the only thing that can raise here
            # is a fatal LLM outage — which must halt the *entire* scrape.
            tasks = [
                asyncio.ensure_future(_RUNNERS[src](pages[src]))
                for src in enabled
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

            fatal: LLMUnavailableError | None = None
            for task in done:
                exc = task.exception()
                if exc is None:
                    item = task.result()
                    if item:
                        source_name, unified_jobs = item
                        results[source_name] = unified_jobs
                elif isinstance(exc, LLMUnavailableError):
                    fatal = exc
                else:
                    logger.exception("orchestrator_source_unexpected_exception", error=str(exc))

            if fatal is not None:
                # Stop all still-running sources — the LLM they'd fall back on
                # is down, so there's nothing to wait for.
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                logger.error(
                    "orchestrator_llm_unavailable_abort",
                    error=str(fatal),
                    partial_sources={k: len(v) for k, v in results.items()},
                    note="LLM provider down — all scraping stopped",
                )

        # ── Log batch_saved events every 100 jobs across combined list ─────────
        all_so_far = [j for jobs in results.values() for j in jobs]
        _BATCH = 100
        for i in range(0, len(all_so_far), _BATCH):
            batch_num = i // _BATCH + 1
            logger.info(
                "batch_saved",
                batch        = batch_num,
                count        = len(all_so_far[i : i + _BATCH]),
                total_so_far = min(i + _BATCH, len(all_so_far)),
            )

        token_usage = (
            linkedin_agent_holder["agent"].get_token_usage()
            if "agent" in linkedin_agent_holder
            else empty_usage_summary()
        )
        llm_calls = (
            linkedin_agent_holder["agent"].get_llm_call_log()
            if "agent" in linkedin_agent_holder
            else []
        )
        logger.info("orchestrator_token_usage", **token_usage["total"])

        if fatal is not None:
            # Carry the partial harvest + usage/audit out on the exception so the
            # caller (run_all → _run_harvest_background) can persist what was
            # scraped before the outage and record the failing LLM call, then
            # mark the run failed with the provider message.
            fatal.partial_raw = results
            fatal.partial_token_usage = token_usage
            fatal.partial_llm_calls = llm_calls
            raise fatal

        return results, token_usage, llm_calls
