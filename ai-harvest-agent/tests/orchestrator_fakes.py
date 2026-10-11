"""Shared fakes for orchestrator / harvest-route tests (not a test module).

Replaces the browser + per-source agents with in-process fakes so
OrchestratorAgent.run_all() and _run_harvest_background_impl() run for real
(LeadEnrichment, BusinessFilter classify/apply, dedup, persistence calls) with
no Playwright, network or LLM. ``install_source_fakes`` wires them in.

LinkedIn modes:  "ok" | "outage" (2 jobs via on_batch, then LLMUnavailableError)
Naukri modes:    "ok" | "gated" (waits until LinkedIn has failed, then finishes)
                 | "error" (RuntimeError) | "off"
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from app.agents.linkedin_agent import LinkedInScrapedJob
from app.agents.naukri_agent import NaukriScrapedJob
from app.core.exceptions import LLMUnavailableError
from app.services.llm_service import empty_usage_summary

LLM_DOWN_MSG = "Local LLM 'test-model' is unavailable — contact the admin team."

# Structured on_progress payloads the fake agents send (Live Harvest card). The
# two sources use deliberately different numbers so cross-source bleed shows up.
LI_LISTING = {"phase": "details", "reported_total": 200, "listed": 25, "skipped": 3,
              "target": 197, "cap": 500, "page": 1, "batch_size": 2, "done": 0, "in_batch": 0}
NK_LISTING = {"phase": "details", "reported_total": 1284, "listed": 20, "skipped": 1,
              "target": 300, "cap": 300, "page": 1, "total_pages": 65, "batch_size": 2,
              "done": 0, "in_batch": 0}


def li_job(i: int) -> LinkedInScrapedJob:
    return LinkedInScrapedJob(
        job_title=f"Python Engineer {i}", company=f"LinkedIn Co {i}", location="Remote",
        salary="", experience="", posted_date="",
        job_url=f"https://www.linkedin.com/jobs/view/{4100000000 + i}",
        job_description="Build Python services.",
    )


def nk_job(i: int) -> NaukriScrapedJob:
    return NaukriScrapedJob(
        job_title=f"Data Engineer {i}", company=f"Naukri Co {i}", location="Pune",
        salary="", experience="", posted_date="",
        job_url=f"https://www.naukri.com/job-listings-data-engineer-{i}",
        job_description="Build data pipelines.",
    )


class FakePage:
    pass


class FakeBrowserManager:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "FakeBrowserManager":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def new_page(self) -> FakePage:
        return FakePage()

    # PersistentBrowserManager's multi-tab API (orchestrator: one tab per source).
    async def acquire_page(self, label: str = "") -> FakePage:
        return FakePage()

    async def release_page(self, page: Any, label: str = "") -> None:
        return None


@asynccontextmanager
async def fake_guest_browser(*args: Any, **kwargs: Any):
    yield FakeBrowserManager()


def isolate_run_side_files(monkeypatch, tmp_path) -> None:
    """run_all() has two on-disk side effects outside the DB: debug JSON under
    data/debug/linkedin (CWD-relative) and BusinessFilterService's append-only
    ambiguous_companies.json master list (ABSOLUTE path). Point both at tmp so
    the fake companies never leak into the real data/ dir."""
    import app.services.business_filter_service as bfs_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bfs_mod, "_AMBIGUOUS_FILE", tmp_path / "ambiguous_companies.json")


def install_source_fakes(monkeypatch, *, linkedin: str = "outage", naukri: str = "gated") -> dict:
    """Patch the browser + LinkedIn/Naukri agents. Returns a shared `state` dict
    the fakes write to (event order, whether Naukri outlived LinkedIn, …)."""
    import app.agents.naukri_agent as naukri_mod
    import app.agents.orchestrator_agent as orch_mod
    import app.scrapers.browser_manager as bm_mod
    from app.services import run_guard

    run_guard.clear_stop()
    state: dict[str, Any] = {
        "events": [],
        "linkedin_failed": asyncio.Event(),
        "naukri_finished_after_linkedin_failed": False,
        "naukri_cancelled": False,
    }
    token_usage = empty_usage_summary()
    token_usage["ollama"]["calls"] = 1
    token_usage["total"]["calls"] = 1
    llm_calls = [{"provider": "ollama", "model": "test-model", "success": False,
                  "error": LLM_DOWN_MSG, "job_url": li_job(3).job_url}]
    reenrich_queue = [{"job_url": li_job(2).job_url, "source": "LinkedIn",
                       "content": "c", "schema_description": "s", "system": "y"}]
    state.update(token_usage=token_usage, llm_calls=llm_calls, reenrich_queue=reenrich_queue)

    class FakeLinkedInAgent:
        def __init__(self, already_harvested: set[str] | None = None) -> None:
            pass

        async def _run(self, page, f, wait_for_login=False, on_status=None, on_batch=None,
                       on_progress=None):
            await on_status("LinkedIn: searching…")
            if on_progress:
                await on_progress(LI_LISTING)
                await on_progress({**LI_LISTING, "done": 1, "in_batch": 1})
            await on_batch([li_job(1), li_job(2)])
            if linkedin == "outage":
                state["events"].append("linkedin_failed")
                state["linkedin_failed"].set()
                raise LLMUnavailableError(LLM_DOWN_MSG)
            await on_batch([li_job(3)])
            return [li_job(1), li_job(2), li_job(3)]

        def get_token_usage(self) -> dict:
            return token_usage

        def get_llm_call_log(self) -> list[dict]:
            return list(llm_calls)

        def get_reenrich_queue(self) -> list[dict]:
            return list(reenrich_queue)

    class FakeNaukriAgent:
        def __init__(self, already_harvested: set[str] | None = None) -> None:
            pass

        async def _run(self, page, f, on_status=None, on_batch=None, on_progress=None):
            try:
                await on_status("Naukri: searching…")
                if on_progress:
                    await on_progress(NK_LISTING)
                    await on_progress({**NK_LISTING, "done": 2, "in_batch": 2})
                if naukri == "error":
                    raise RuntimeError("Naukri search filters not applied")
                await on_batch([nk_job(1), nk_job(2)])
                if naukri == "gated":
                    # Still mid-scrape when LinkedIn's LLM dies — must NOT be cancelled.
                    await asyncio.wait_for(state["linkedin_failed"].wait(), timeout=5)
                    await asyncio.sleep(0.01)
                await on_batch([nk_job(3)])
                # Stand-in for Naukri's post-harvest passes (company enrichment,
                # company-location contacts) that run inside NaukriAgent._run.
                await on_status("Naukri: enriching companies for 3 jobs")
                state["events"].append("naukri_post_passes")
                state["naukri_finished_after_linkedin_failed"] = state["linkedin_failed"].is_set()
                return [nk_job(1), nk_job(2), nk_job(3)]
            except asyncio.CancelledError:
                state["naukri_cancelled"] = True
                raise

    monkeypatch.setattr(bm_mod, "PersistentBrowserManager", FakeBrowserManager)
    monkeypatch.setattr(orch_mod, "LinkedInAgent", FakeLinkedInAgent)
    monkeypatch.setattr(naukri_mod, "NaukriAgent", FakeNaukriAgent)
    monkeypatch.setattr(naukri_mod, "naukri_guest_browser", fake_guest_browser)
    return state


class RecordingRunService:
    """Stand-in for HarvestRunService: records every call made through
    db_write/db_read (patched to call the lambda with db=None)."""

    calls: list[tuple[str, dict]] = []

    def __init__(self, db: Any) -> None:
        pass

    def _rec(self, name: str, **kw: Any) -> None:
        type(self).calls.append((name, kw))

    async def update_run(self, run_pk: str, **fields: Any) -> None:
        self._rec("update_run", **fields)

    async def bulk_insert_scraped_jobs(self, run_pk: str, jobs: list[dict]) -> None:
        self._rec("bulk_insert_scraped_jobs", jobs=jobs)

    async def replace_run_jobs(self, run_pk: str, jobs: list[dict]) -> None:
        self._rec("replace_run_jobs", jobs=jobs)

    async def bulk_insert_llm_calls(self, run_pk: str, calls: list[dict]) -> None:
        self._rec("bulk_insert_llm_calls", calls=calls)

    async def enqueue_reenrichment(self, run_pk: str, items: list[dict]) -> int:
        self._rec("enqueue_reenrichment", items=items)
        return len(items)

    async def mark_extraction_pending(self, run_pk: str, job_urls: list[str]) -> None:
        self._rec("mark_extraction_pending", job_urls=job_urls)

    async def recent_harvested_job_urls(self, within_days: int) -> set[str]:
        return set()

    async def list_jobs_for_run(self, run_pk: str) -> list:
        return []

    async def list_pending_report_runs(self) -> list:
        return []

    async def jobs_saved_today(self) -> int:
        return 0


def install_route_fakes(monkeypatch) -> dict:
    """Patch run_harvest_agent's DB / email / history / sweep / outreach
    collaborators so _run_harvest_background_impl runs hermetically.
    Returns {"service": RecordingRunService, "outreach": [...], "history": [...]}"""
    import app.routes.run_harvest_agent as route
    import app.services.auto_outreach_service as outreach_mod
    from app.services.job_tracker import JobTracker

    RecordingRunService.calls = []
    outreach_calls: list[dict] = []
    history: list[dict] = []

    async def _call(fn):
        return await fn(None)

    async def _no_sweep() -> dict:
        return {}

    async def _no_report(*args: Any, **kwargs: Any) -> None:
        return None

    async def _outreach(rows, **kwargs: Any) -> None:
        outreach_calls.append({"rows": rows, **kwargs})

    class _History:
        def append(self, entry: dict) -> None:
            history.append(entry)

    monkeypatch.setattr(route, "HarvestRunService", RecordingRunService)
    monkeypatch.setattr(route, "db_write", _call)
    monkeypatch.setattr(route, "db_read", _call)
    monkeypatch.setattr(route, "run_reenrichment_sweep", _no_sweep)
    monkeypatch.setattr(route, "send_harvest_report", _no_report)
    monkeypatch.setattr(route, "EmailSender", lambda settings: None)
    monkeypatch.setattr(route, "_history_svc", _History())
    monkeypatch.setattr(outreach_mod, "run_auto_outreach_after_harvest", _outreach)
    monkeypatch.setattr(JobTracker, "_persist", classmethod(lambda cls: None))
    return {"service": RecordingRunService, "outreach": outreach_calls, "history": history}
