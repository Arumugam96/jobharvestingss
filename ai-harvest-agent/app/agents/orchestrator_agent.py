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
from contextlib import AsyncExitStack
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
from app.core.job_url import normalize_job_url
from app.models.harvest_models import HarvestConfig
from app.models.response_models import HarvestJob
from app.models.unified_job import UnifiedJob
from app.services import run_guard
from app.services.business_filter_service import BusinessFilterService
from app.services.source_status import (
    REASON_ERROR,
    REASON_LLM_UNAVAILABLE,
    REASON_LOGIN_REQUIRED,
    SourceStatusTracker,
)

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
    """Remove duplicates across all sources by normalized job_url only.

    The posting URL is the identity: the same company + title at different
    locations (or reposted) are distinct postings with distinct URLs, so
    company+title is deliberately NOT a key. Jobs with no URL are kept."""
    seen_urls: set[str] = set()
    deduped:   list[UnifiedJob] = []
    for job in jobs:
        url_key = normalize_job_url(job.job_url)
        if url_key and url_key in seen_urls:
            continue
        if url_key:
            seen_urls.add(url_key)
        deduped.append(job)
    return deduped


# Fixed execution priority — lower index = runs first
_SOURCE_PRIORITY = ["naukri", "linkedin", "dice"]

# Config/status key → the UnifiedJob.source / jobs_by_source display name.
_SOURCE_DISPLAY = {"naukri": "Naukri", "linkedin": "LinkedIn", "dice": "Dice"}


# ── Converters: source-specific dataclass → UnifiedJob ────────────────────────

def _merge_token_usage(a: dict, b: dict) -> dict:
    """Sum two LLMService.get_usage_summary() dicts provider-by-provider."""
    out: dict = {}
    for provider in set(a) | set(b):
        pa, pb = a.get(provider) or {}, b.get(provider) or {}
        out[provider] = {k: int(pa.get(k, 0) or 0) + int(pb.get(k, 0) or 0) for k in set(pa) | set(pb)}
    return out


def _naukri_to_unified(j: "NaukriScrapedJob") -> UnifiedJob:  # type: ignore[name-defined]
    # Naukri's own consultant flag is the strongest Staffing-vs-Direct signal
    # there is (see BusinessFilterService._infer_hiring_entity): True => posted by
    # a recruitment consultant, False => posted by the company itself, None =>
    # Naukri didn't say (no hint).
    consultant = getattr(j, "consultant", None)
    hiring_hint = "staffing" if consultant is True else ("direct" if consultant is False else "")
    return UnifiedJob(
        job_title               = j.job_title,
        company                 = j.company,
        location                = j.location,
        salary                  = j.salary,
        experience              = j.experience,
        posted_date             = j.posted_date,
        job_url                 = j.job_url,
        job_description         = j.job_description,
        job_description_html    = getattr(j, "job_description_html", ""),
        skills                  = j.skills,
        work_mode               = j.work_mode,
        source                  = "Naukri",
        scraped_at              = datetime.now(timezone.utc),
        company_url             = getattr(j, "company_url", ""),
        company_size            = getattr(j, "company_size", ""),
        employment_type         = getattr(j, "employment_type", ""),
        job_type                = getattr(j, "employment_type", ""),
        domain_hint             = getattr(j, "industry_hint", ""),
        hiring_hint             = hiring_hint,
        job_poster_name         = getattr(j, "recruiter_name", None),
        job_poster_designation  = getattr(j, "job_poster_designation", None),
        linkedin_profile_url    = getattr(j, "linkedin_profile_url", None),
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
        job_description_html    = getattr(j, "job_description_html", ""),
        skills                  = j.skills,
        work_mode               = j.work_mode,
        source                  = "LinkedIn",
        scraped_at              = datetime.now(timezone.utc),
        company_url             = getattr(j, "company_url", ""),
        company_size            = getattr(j, "company_size", ""),
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
        scraped_at              = datetime.now(timezone.utc),
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
    # Jobs that degraded to selector-only because every LLM provider was down
    # (see LinkedInAgent.get_reenrich_queue()) — [] on a healthy run. The caller
    # persists these to reenrichment_tasks for a later run to re-extract.
    reenrich_queue:   list[dict]                   = field(default_factory=list)
    # Final per-source status snapshot (app/services/source_status.py) — one
    # {state, count, message, reason, error, updated_at} entry per ENABLED source.
    source_status:    dict[str, dict]              = field(default_factory=dict)
    # Provider message when LinkedIn stopped on an extraction-LLM outage while the
    # other enabled source(s) completed — the run still finishes normally (its
    # LinkedIn partial jobs / LLM log / re-enrich queue are in the fields above).
    # "" when there was no isolated outage.
    llm_outage:       str                          = ""

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

    def __init__(self, config: HarvestConfig, already_harvested: set[str] | None = None) -> None:
        self._config = config
        # Normalized job_urls to skip at the source (LinkedIn + Naukri) — loaded from
        # the DB by the route for cross-run dedup, so a posting harvested within
        # the lookback window isn't re-scraped/re-emailed. Empty ⇒ no source skip.
        self._already_harvested: set[str] = already_harvested or set()
        # Set by _collect_all when LinkedIn stopped on an extraction-LLM outage
        # while another enabled source completed (the run carries on).
        self._llm_outage: LLMUnavailableError | None = None

    # ── Full pipeline (POST /run-harvest-agent) ───────────────────────────────

    async def run_all(
        self,
        wait_for_login: bool = False,
        on_status: Callable[[str], Awaitable[None]] | None = None,
        on_persist: Callable[[list[UnifiedJob]], Awaitable[None]] | None = None,
        on_source_status: Callable[[dict[str, dict]], Awaitable[None]] | None = None,
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
        on_persist      Optional async callback invoked with batches of
                        UnifiedJob as they are scraped (per LinkedIn page /
                        per source), so the caller can persist them to the DB
                        incrementally — crash safety + live progress. These
                        batches are RAW (pre-classification/pre-dedup); the
                        caller reconciles with the final result.all_jobs at end.
        on_source_status Optional async callback handed a per-source status
                        snapshot ({source: {state, count, message, reason,
                        error, updated_at}}) whenever a source changes state,
                        scrapes a batch, or (throttled) reports progress — the
                        caller persists it for GET /harvest-status `sources`.

        An extraction-LLM outage stops ONLY LinkedIn (the one LLM consumer):
        Naukri/Dice run to completion and the pipeline below runs as usual, with
        LinkedIn's pre-outage partial jobs included and result.llm_outage set.
        Only when no other enabled source completed does the outage still abort
        the run (LLMUnavailableError carrying .partial_result) — today's
        all-sources-failed behaviour.
        """
        config      = self._config
        started_at  = datetime.now(timezone.utc)
        run_id      = started_at.strftime("%Y%m%d_%H%M%S")
        result      = OrchestratorResult(started_at=started_at)
        # Created here (inside the loop run_all executes on — a proactor worker
        # thread on Windows) so its lock/timer bind to the right event loop.
        tracker     = SourceStatusTracker(
            [s for s in _SOURCE_PRIORITY if getattr(config.sources, s, False)],
            on_change=on_source_status,
        )

        # ── Step 1: collect raw jobs from all enabled sources ─────────────────
        try:
            raw_by_source, result.token_usage, result.llm_calls, result.reenrich_queue = await self._collect_all(
                config, wait_for_login=wait_for_login, on_status=on_status, on_persist=on_persist,
                tracker=tracker,
            )
        except LLMUnavailableError as exc:
            # LinkedIn's extraction LLM went down AND no other enabled source
            # completed, so there is nothing to carry the run. Keep whatever was
            # collected (unprocessed — we skip dedup/classify/verify since the
            # run is failing anyway) so the caller can persist the partial
            # harvest, then re-raise to mark the run failed.
            from app.services.llm_service import empty_usage_summary

            result.token_usage    = getattr(exc, "partial_token_usage", None) or empty_usage_summary()
            result.llm_calls      = getattr(exc, "partial_llm_calls", None) or []
            result.reenrich_queue = getattr(exc, "partial_reenrich_queue", None) or []
            partial_raw           = getattr(exc, "partial_raw", None) or {}
            partial_jobs: list[UnifiedJob] = []
            for source, jobs in partial_raw.items():
                result.sources_executed.append(source)
                partial_jobs.extend(jobs)
            result.jobs_by_source = dict(partial_raw)
            result.all_jobs       = partial_jobs
            result.source_status  = tracker.snapshot()
            result.completed_at   = datetime.now(timezone.utc)
            exc.partial_result    = result
            logger.error(
                "orchestrator_run_aborted_llm_unavailable",
                error=str(exc), partial_jobs=len(partial_jobs),
            )
            raise

        if self._llm_outage is not None:
            # Isolated outage: LinkedIn stopped, the other source(s) completed.
            result.llm_outage = getattr(self._llm_outage, "message", "") or str(self._llm_outage)

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

        # Align each source's live (raw, pre-dedup) count with its final kept
        # total so `sources[*].count` matches the legacy naukri/linkedin/dice
        # fields the caller writes from jobs_by_source. States are untouched.
        for key in tracker.sources:
            tracker.set_count(key, len(deduped_by_source.get(_SOURCE_DISPLAY.get(key, key), [])))
        result.source_status = tracker.snapshot()

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
        on_persist: Callable[[list[UnifiedJob]], Awaitable[None]] | None = None,
        tracker: SourceStatusTracker | None = None,
    ) -> tuple[dict[str, list[UnifiedJob]], dict, list[dict], list[dict]]:
        """
        Run all enabled source agents IN PARALLEL using a single shared browser
        context (one page per source).  Using a single PersistentBrowserManager
        avoids Chrome profile-lock conflicts that arise when launching multiple
        browser instances against the same profile directory.

        Returns ({source_name: [UnifiedJob, …]}, token_usage, llm_calls,
        reenrich_queue) — all but the first are the LinkedIn LLM fallback's
        cumulative Claude/Ollama usage, per-call audit log, and degraded-job
        re-enrichment queue (Naukri and Dice never hit the LLM), empty if
        LinkedIn wasn't enabled or never triggered the fallback.

        Every source runs to its own end: each runner catches its own failures
        and reports them on `tracker` (per-source state/count/message). An
        extraction-LLM outage in LinkedIn stops ONLY LinkedIn — its pre-outage
        jobs are kept as its result and self._llm_outage is set — unless no other
        enabled source completed, in which case the LLMUnavailableError is raised
        (carrying the partial harvest) exactly as before.
        """
        from app.scrapers.browser_manager import PersistentBrowserManager
        from app.scrapers.dice_scraper import DiceScrapedJob, DiceScraper
        from app.services.llm_service import empty_usage_summary
        from app.services.session_manager import account_profile_dir

        self._llm_outage = None
        results:  dict[str, list[UnifiedJob]] = {}
        # Populated by _harvest_linkedin so token usage survives even if that
        # coroutine raises — the LinkedInAgent instance is created before any
        # of its login/scrape work that could fail.
        linkedin_agent_holder: dict[str, LinkedInAgent] = {}
        # Same for the NaukriAgent — its (grounded) LLM enrichment calls are
        # merged into the run's token usage + llm_calls audit below.
        naukri_agent_holder: dict[str, Any] = {}
        # The LLMUnavailableError LinkedIn stopped on (if any) — held here, not
        # raised, so the other sources keep running.
        linkedin_outage: dict[str, LLMUnavailableError] = {}
        enabled = [s for s in _SOURCE_PRIORITY if getattr(config.sources, s, False)]
        if tracker is None:
            tracker = SourceStatusTracker(enabled)

        if not enabled:
            logger.warning("orchestrator_no_sources_enabled")
            return results, empty_usage_summary(), [], []

        logger.info("orchestrator_parallel_start", sources=enabled)

        # ── Per-source progress wrappers ───────────────────────────────────────
        # The source is known at the call site, so each source's messages and
        # batch counts land in ITS tracker entry (no cross-source overwrite);
        # the run-level on_status still receives every message (latest overall).
        def _status_for(src: str) -> Callable[[str], Awaitable[None]]:
            async def _cb(msg: str) -> None:
                await tracker.message(src, msg)
                if on_status:
                    await on_status(msg)
            return _cb

        # Structured live progress (Rule Engine "Live Harvest" card): each agent's
        # on_progress payload (phase / found / listed / skipped / target / done /
        # in_batch / …) lands in ITS tracker entry. The run's filter window rides
        # along so the card can show "Past Nh". Never raises into the scraper.
        window_hours = getattr(config.filters, "search_window_hours", None)

        def _progress_for(src: str) -> Callable[[dict], Awaitable[None]]:
            async def _cb(payload: dict) -> None:
                try:
                    data = dict(payload or {})
                    if data.get("window_hours") is None and window_hours is not None:
                        data["window_hours"] = window_hours
                    await tracker.progress(src, data)
                except Exception as exc:
                    logger.debug("orchestrator_progress_failed", source=src, error=str(exc))
            return _cb

        async def _start(src: str) -> None:
            await tracker.start(src)
            if window_hours is not None:
                try:
                    await tracker.set_listing(src, window_hours=window_hours)
                except Exception as exc:
                    logger.debug("orchestrator_progress_failed", source=src, error=str(exc))

        async def _finish(src: str, n: int) -> None:
            msg = f"Stopped by user — {n} jobs scraped" if run_guard.is_stop_requested() else None
            await tracker.done(src, n, message=msg)

        async def _skip_if_stopped(src: str) -> bool:
            # A stop requested before this source even began (e.g. during the
            # browser launch) — don't start it at all.
            if run_guard.is_stop_requested():
                await tracker.skip(src, "Stopped by user")
                return True
            return False

        # Browser layout. DEFAULT (NAUKRI_BROWSER_MODE=shared): ONE browser — the
        # selected LinkedIn account's persistent Chrome profile — with one TAB per
        # enabled source, all scraping concurrently (both tabs visible in the one
        # window of the Live Browser / noVNC view). Naukri is guest-only, so its
        # tab only needs the context's full-Chromium channel (Akamai denies
        # chromium-headless-shell; PersistentBrowserManager now passes
        # channel="chromium"). Opt-out, NAUKRI_BROWSER_MODE=isolated: Naukri runs
        # in its own fresh cookie-less browser (see naukri_guest_browser), and the
        # persistent profile isn't launched at all when Naukri is the only source
        # — for a Naukri-only worker / a host without the LinkedIn profile.
        from app.config import get_settings
        naukri_isolated = (
            "naukri" in enabled and get_settings().naukri_browser_mode == "isolated"
        )
        shared_sources = [s for s in enabled if not (s == "naukri" and naukri_isolated)]

        # Launch the shared browser under the SELECTED LinkedIn account's Chrome
        # profile so account "2" reuses its own saved session (account "1"/default
        # → the original profile, unchanged). LinkedInAgent auto-logs-in via env
        # credentials if that profile isn't authenticated yet. NOTE: this shared
        # context is also used by Naukri/Dice when enabled in the same run — for a
        # non-default account they'd use the account-suffixed profile too; the
        # account selector is a LinkedIn concern, so run other sources under
        # account "1" if they need their own saved sessions.
        try:
            async with AsyncExitStack() as browser_stack:
                # Open one independent tab per shared source — they share cookies but
                # each has its own navigation state. An isolated Naukri gets no tab
                # here (pages["naukri"] stays None -> it opens its own guest browser).
                pages: dict[str, Any] = {s: None for s in enabled}
                if shared_sources:
                    linkedin_profile = account_profile_dir(
                        config.browser.chrome_profile, config.sources.linkedin_account,
                    )
                    logger.info(
                        "orchestrator_profile_selected",
                        linkedin_account=config.sources.linkedin_account, profile_dir=linkedin_profile,
                    )
                    pbm = await browser_stack.enter_async_context(PersistentBrowserManager(
                        profile_dir = linkedin_profile,
                        headless    = config.browser.headless,
                        slow_mo     = config.browser.slow_mo_ms,
                    ))
                    for source in shared_sources:
                        pages[source] = await pbm.acquire_page(label=source)
                    logger.info(
                        "orchestrator_browser_layout",
                        mode="single_browser_tabs", tabs=shared_sources,
                        own_browser=["naukri"] if naukri_isolated else [],
                        headless=config.browser.headless,
                    )
                else:
                    pbm = None

                # ── Per-source harvest coroutines ──────────────────────────────
                # Each runner owns its failure handling: it never raises (bar
                # cancellation), it returns ("Source", jobs) and records its
                # outcome (done / failed + reason) on the tracker.

                async def _harvest_naukri(page: Any) -> tuple[str, list[UnifiedJob]]:
                    from app.agents.naukri_agent import (
                        NaukriAgent,
                        NaukriScrapedJob,
                        naukri_guest_browser,
                    )
                    if await _skip_if_stopped("naukri"):
                        return "Naukri", []
                    logger.info("orchestrator_dispatching", source="naukri",
                                browser_mode="isolated" if page is None else "shared")
                    await _start("naukri")
                    try:
                        agent = NaukriAgent(already_harvested=self._already_harvested)
                        naukri_agent_holder["agent"] = agent

                        # Incremental persist, exactly like LinkedIn: the agent hands
                        # us every N freshly-scraped jobs; count them for the live
                        # per-source status, then convert + forward them. No
                        # end-of-run on_persist for Naukri (that would double-insert).
                        async def _naukri_on_batch(scraped_batch: list[NaukriScrapedJob]) -> None:
                            if not scraped_batch:
                                return
                            await tracker.add_jobs("naukri", len(scraped_batch))
                            if on_persist:
                                await on_persist([_naukri_to_unified(j) for j in scraped_batch])

                        async def _run_on(p: Any) -> list[NaukriScrapedJob]:
                            return await agent._run(
                                p, config.filters, on_status=_status_for("naukri"),
                                on_batch=_naukri_on_batch,
                                on_progress=_progress_for("naukri"),
                            )

                        if page is None:
                            async with naukri_guest_browser(
                                headless = config.browser.headless,
                                slow_mo  = config.browser.slow_mo_ms,
                            ) as guest:
                                scraped = await _run_on(await guest.new_page())
                        else:
                            scraped = await _run_on(page)
                        unified = [_naukri_to_unified(j) for j in scraped]
                        logger.info("orchestrator_source_done", source="naukri", count=len(unified))
                        logger.info("batch_saved", source="Naukri", count=len(unified))
                        await _finish("naukri", len(unified))
                        return "Naukri", unified
                    except Exception as exc:
                        logger.exception(
                            "orchestrator_naukri_error", error=str(exc),
                            note="Naukri failed; LinkedIn and Dice results are retained",
                        )
                        await tracker.fail("naukri", REASON_ERROR, error=str(exc) or type(exc).__name__)
                        return "Naukri", []

                async def _harvest_linkedin(page: Any) -> tuple[str, list[UnifiedJob]]:
                    if await _skip_if_stopped("linkedin"):
                        return "LinkedIn", []
                    logger.info("orchestrator_dispatching", source="linkedin")
                    logger.info("linkedin_agent_started")
                    await _start("linkedin")
                    # Every job handed to on_batch so far — the pre-outage partial
                    # harvest if the extraction LLM dies mid-scrape. Raw objects
                    # (converted only if needed) so later in-place enrichment sticks.
                    scraped_so_far: list[LinkedInScrapedJob] = []
                    try:
                        agent   = LinkedInAgent(already_harvested=self._already_harvested)
                        linkedin_agent_holder["agent"] = agent

                        # Per-page incremental persist: the agent hands us each page's
                        # freshly-scraped LinkedInScrapedJobs; count them for the live
                        # per-source status, convert to UnifiedJob here (where
                        # _linkedin_to_unified lives) and forward to the caller's
                        # persist callback. LinkedIn is the long pole, so this is
                        # where mid-scrape crash protection + live progress matter
                        # most. Best-effort in the callback itself.
                        async def _linkedin_on_batch(scraped_batch: list[LinkedInScrapedJob]) -> None:
                            if not scraped_batch:
                                return
                            scraped_so_far.extend(scraped_batch)
                            await tracker.add_jobs("linkedin", len(scraped_batch))
                            if on_persist:
                                await on_persist([_linkedin_to_unified(j) for j in scraped_batch])

                        scraped: list[LinkedInScrapedJob] = await agent._run(
                            page, config.filters, wait_for_login=wait_for_login,
                            on_status=_status_for("linkedin"),
                            on_batch=_linkedin_on_batch,
                            on_progress=_progress_for("linkedin"),
                        )
                        # ── Checkpoint 2: jobs received by orchestrator ────────
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

                        await _finish("linkedin", len(unified))
                        return "LinkedIn", unified
                    except LinkedInLoginError as exc:
                        logger.warning(
                            "orchestrator_linkedin_login_failed", error=str(exc),
                            note="LinkedIn skipped; other sources are retained",
                        )
                        await tracker.fail("linkedin", REASON_LOGIN_REQUIRED, error=str(exc))
                        return "LinkedIn", []
                    except LLMUnavailableError as exc:
                        # The extraction LLM is down. Stop ONLY LinkedIn (the one
                        # LLM consumer): keep the jobs it scraped before the outage
                        # and let Naukri/Dice run to completion. Token usage, the
                        # LLM call log and the re-enrich queue are read off the
                        # agent after the gather, exactly as on a healthy run.
                        linkedin_outage["exc"] = exc
                        partial = [_linkedin_to_unified(j) for j in scraped_so_far]
                        logger.error(
                            "orchestrator_linkedin_llm_unavailable",
                            error=str(exc), partial_jobs=len(partial),
                            note="LinkedIn stopped; other sources keep running",
                        )
                        await tracker.fail(
                            "linkedin", REASON_LLM_UNAVAILABLE,
                            error=getattr(exc, "message", "") or str(exc),
                            count=len(partial),
                            message=f"Stopped — LLM unavailable ({len(partial)} jobs kept)",
                        )
                        return "LinkedIn", partial
                    except Exception as exc:
                        logger.exception(
                            "orchestrator_linkedin_error", error=str(exc),
                            note="LinkedIn failed; other sources are retained",
                        )
                        await tracker.fail("linkedin", REASON_ERROR, error=str(exc) or type(exc).__name__)
                        return "LinkedIn", []

                async def _harvest_dice(page: Any) -> tuple[str, list[UnifiedJob]]:
                    if await _skip_if_stopped("dice"):
                        return "Dice", []
                    logger.info("orchestrator_dispatching", source="dice")
                    logger.info("dice_agent_started")
                    await _start("dice")
                    try:
                        scraper = DiceScraper(page, config.filters)
                        scraped: list[DiceScrapedJob] = await scraper.run()
                        unified = [_dice_to_unified(j) for j in scraped]
                        logger.info("orchestrator_source_done", source="dice", count=len(unified))
                        logger.info("batch_saved", source="Dice", count=len(unified))
                        # Incremental persist (crash safety): Dice returns its whole
                        # list at once, so persist it here as one batch.
                        if on_persist and unified:
                            await on_persist(unified)
                        await _finish("dice", len(unified))
                        return "Dice", unified
                    except Exception as exc:
                        logger.exception(
                            "orchestrator_dice_error", error=str(exc),
                            note="Dice failed; other sources are retained",
                        )
                        await tracker.fail("dice", REASON_ERROR, error=str(exc) or type(exc).__name__)
                        return "Dice", []

                _RUNNERS = {
                    "naukri":   _harvest_naukri,
                    "linkedin": _harvest_linkedin,
                    "dice":     _harvest_dice,
                }

                async def _run_in_tab(src: str) -> tuple[str, list[UnifiedJob]]:
                    # Run one source on its own tab, then close JUST that tab
                    # (finished, failed or user-stopped) so the remaining
                    # source(s) keep the window. release_page never closes the
                    # last open tab (a headed Chromium would quit) and never
                    # raises; an isolated Naukri has no tab here (None).
                    try:
                        return await _RUNNERS[src](pages[src])
                    finally:
                        if pbm is not None and pages[src] is not None:
                            await pbm.release_page(pages[src], label=src)

                # ── Launch all enabled sources concurrently ────────────────────
                # Wait for EVERY source (no FIRST_EXCEPTION short-circuit): each
                # runner catches its own failures — including LinkedIn's LLM
                # outage — so one source failing never cancels the others. If
                # _collect_all itself is cancelled, gather cancels every child and
                # waits for them before re-raising, so no task outlives the
                # browser context closed below.
                tasks = [
                    asyncio.ensure_future(_run_in_tab(src))
                    for src in enabled
                ]
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)

                for src, outcome in zip(enabled, outcomes):
                    if isinstance(outcome, BaseException):
                        # Defensive — runners don't raise; never lose the others.
                        logger.error(
                            "orchestrator_source_unexpected_exception",
                            source=src, error=str(outcome), error_type=type(outcome).__name__,
                        )
                        await tracker.fail(src, REASON_ERROR, error=str(outcome) or type(outcome).__name__)
                        continue
                    source_name, unified_jobs = outcome
                    results[source_name] = unified_jobs
        finally:
            # Cancel the throttled-message timer and flush the last snapshot.
            await tracker.aclose()

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
        if "agent" in naukri_agent_holder:
            try:
                naukri_agent = naukri_agent_holder["agent"]
                token_usage = _merge_token_usage(token_usage, naukri_agent.get_token_usage())
                llm_calls = list(llm_calls) + list(naukri_agent.get_llm_call_log())
            except Exception as exc:  # audit merge must never fail the run
                logger.warning("orchestrator_naukri_llm_usage_merge_failed", error=str(exc))
        reenrich_queue = (
            linkedin_agent_holder["agent"].get_reenrich_queue()
            if "agent" in linkedin_agent_holder
            else []
        )
        logger.info("orchestrator_token_usage", **token_usage["total"])

        fatal = linkedin_outage.get("exc")
        if fatal is not None:
            completed = [s for s in enabled if s != "linkedin" and tracker.state(s) == "done"]
            if not completed:
                # No other enabled source completed (LinkedIn-only run, or every
                # other source failed too) — keep today's failure behaviour: carry
                # the partial harvest + usage/audit out on the exception so the
                # caller (run_all → _run_harvest_background) can persist what was
                # scraped before the outage and record the failing LLM call, then
                # mark the run failed with the provider message.
                logger.error(
                    "orchestrator_llm_unavailable_abort",
                    error=str(fatal),
                    partial_sources={k: len(v) for k, v in results.items()},
                    note="LLM provider down and no other source completed — run fails",
                )
                fatal.partial_raw = results
                fatal.partial_token_usage = token_usage
                fatal.partial_llm_calls = llm_calls
                fatal.partial_reenrich_queue = reenrich_queue
                raise fatal
            # Isolated outage — the run carries on with the completed sources.
            self._llm_outage = fatal
            logger.warning(
                "orchestrator_llm_unavailable_linkedin_only",
                error=str(fatal),
                completed_sources=completed,
                partial_sources={k: len(v) for k, v in results.items()},
                note="LLM provider down — LinkedIn stopped, other sources completed",
            )

        return results, token_usage, llm_calls, reenrich_queue
