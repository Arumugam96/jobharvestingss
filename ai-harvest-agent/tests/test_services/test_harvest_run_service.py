"""
Tests for HarvestRunService — persistence of harvest runs, scraped jobs, and
LLM call audit logs (HarvestRunORM / ScrapedJobORM / LlmCallORM).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.harvest_run_service import HarvestRunService, filters_view, run_to_result_summary


def _unified_job_dict(**overrides) -> dict:
    base = dict(
        source="LinkedIn",
        job_title="Backend Engineer",
        company="Acme Corp",
        location="Remote",
        salary="Not Disclosed",
        experience="3-5 years",
        posted_date="2026-08-01",
        job_url="https://linkedin.com/jobs/view/123",
        job_description="Build things.",
        skills=["Python", "SQL"],
        work_mode="remote",
        job_type="Contract",
        domain="IT",
        hiring_entity="Direct Client",
        is_gcc=False,
        verification_status="verified",
        job_poster_name="Jane Doe",
        job_poster_designation="Recruiter",
        linkedin_profile_url="https://linkedin.com/in/janedoe",
        current_company="Acme Corp",
        email_id="jane@acme.com",
        contact_number="+1-555-0100",
    )
    base.update(overrides)
    return base


def _llm_call_dict(**overrides) -> dict:
    base = dict(
        job_url="https://linkedin.com/jobs/view/123",
        provider="claude",
        model="claude-sonnet-4-6",
        prompt="Extract...",
        response='{"description": "..."}',
        prompt_chars=10,
        response_chars=20,
        input_tokens=100,
        output_tokens=50,
        latency_ms=250,
        success=True,
        error_message=None,
        retry_count=0,
    )
    base.update(overrides)
    return base


# ══════════════════════════════════════════════════════════════════════════════
# create_run / update_run
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_run_defaults_sources_from_source(db_session) -> None:
    svc = HarvestRunService(db_session)

    run_pk = await svc.create_run(run_id="20260807_120000", source="LinkedIn")

    run = await svc.get_by_run_id("20260807_120000", source="LinkedIn")
    assert run is not None
    assert run.id == run_pk
    assert run.source == "LinkedIn"
    assert run.sources == ["LinkedIn"]
    assert run.status == "running"
    assert run.job_id is None


@pytest.mark.asyncio
async def test_create_run_orchestrator_has_null_source(db_session) -> None:
    svc = HarvestRunService(db_session)

    await svc.create_run(
        run_id="20260807_120001",
        job_id="job-abc",
        source=None,
        sources=["naukri", "linkedin"],
        filters_snapshot={"keyword": "python"},
        started_at=datetime.now(timezone.utc),
    )

    run = await svc.get_by_job_id("job-abc")
    assert run is not None
    assert run.source is None
    assert run.sources == ["naukri", "linkedin"]
    assert run.filters_snapshot == {"keyword": "python"}


@pytest.mark.asyncio
async def test_update_run_applies_fields(db_session) -> None:
    svc = HarvestRunService(db_session)
    run_pk = await svc.create_run(run_id="20260807_120002", source="Dice")

    await svc.update_run(
        run_pk,
        status="success",
        progress=100,
        combined_count=5,
        token_usage={"total": {"calls": 1}},
    )

    run = await svc.get_by_run_id("20260807_120002", source="Dice")
    assert run.status == "success"
    assert run.progress == 100
    assert run.combined_count == 5
    assert run.token_usage == {"total": {"calls": 1}}


@pytest.mark.asyncio
async def test_update_run_with_no_fields_is_a_noop(db_session) -> None:
    svc = HarvestRunService(db_session)
    run_pk = await svc.create_run(run_id="20260807_120003", source="Naukri")

    await svc.update_run(run_pk)  # should not raise

    run = await svc.get_by_run_id("20260807_120003", source="Naukri")
    assert run.status == "running"


# ══════════════════════════════════════════════════════════════════════════════
# bulk_insert_scraped_jobs
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bulk_insert_scraped_jobs_round_trips_fields(db_session) -> None:
    svc = HarvestRunService(db_session)
    run_pk = await svc.create_run(run_id="20260807_120004", source="LinkedIn")

    await svc.bulk_insert_scraped_jobs(run_pk, [_unified_job_dict()])

    run = await svc.get_by_run_id("20260807_120004", source="LinkedIn")
    assert len(run.jobs) == 1
    job = run.jobs[0]
    assert job.job_title == "Backend Engineer"
    assert job.skills == ["Python", "SQL"]
    assert job.is_gcc is False
    assert job.email_id == "jane@acme.com"
    assert job.run_id == run_pk


@pytest.mark.asyncio
async def test_bulk_insert_scraped_jobs_tolerates_missing_keys(db_session) -> None:
    """Naukri/Dice scraped dicts don't carry job_type/domain/hiring_entity/etc.
    (those are only added by BusinessFilterService in the orchestrator path) —
    missing keys must fall back to column defaults, not raise."""
    svc = HarvestRunService(db_session)
    run_pk = await svc.create_run(run_id="20260807_120005", source="Naukri")

    minimal = {
        "source": "Naukri",
        "job_title": "Data Analyst",
        "company": "Beta Inc",
        "location": "Bengaluru",
        "job_url": "https://naukri.com/job/456",
    }
    await svc.bulk_insert_scraped_jobs(run_pk, [minimal])

    run = await svc.get_by_run_id("20260807_120005", source="Naukri")
    job = run.jobs[0]
    assert job.job_title == "Data Analyst"
    assert job.skills == []
    assert job.domain == "Any"
    assert job.hiring_entity == "Any"
    assert job.verification_status == "pending"
    assert job.job_poster_name is None


@pytest.mark.asyncio
async def test_bulk_insert_scraped_jobs_empty_list_is_a_noop(db_session) -> None:
    svc = HarvestRunService(db_session)
    run_pk = await svc.create_run(run_id="20260807_120006", source="Dice")

    await svc.bulk_insert_scraped_jobs(run_pk, [])

    run = await svc.get_by_run_id("20260807_120006", source="Dice")
    assert run.jobs == []


# ══════════════════════════════════════════════════════════════════════════════
# bulk_insert_llm_calls
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bulk_insert_llm_calls_round_trips_fields(db_session) -> None:
    from sqlalchemy import select
    from app.models.harvest_run import LlmCallORM

    svc = HarvestRunService(db_session)
    run_pk = await svc.create_run(run_id="20260807_120007", source="LinkedIn")

    await svc.bulk_insert_llm_calls(run_pk, [_llm_call_dict(), _llm_call_dict(success=False, error_message="boom", response=None)])

    result = await db_session.execute(select(LlmCallORM).where(LlmCallORM.run_id == run_pk))
    calls = list(result.scalars())
    assert len(calls) == 2
    successes = [c for c in calls if c.success]
    failures = [c for c in calls if not c.success]
    assert len(successes) == 1
    assert successes[0].input_tokens == 100
    assert len(failures) == 1
    assert failures[0].error_message == "boom"
    assert failures[0].response is None


# ══════════════════════════════════════════════════════════════════════════════
# Read methods: get_by_job_id / get_by_run_id / list_runs
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_by_run_id_distinguishes_orchestrator_from_single_source(db_session) -> None:
    """Same run_id string could in principle collide between an orchestrator
    run and a single-source run — source is part of the lookup key."""
    svc = HarvestRunService(db_session)
    shared_run_id = "20260807_999999"

    await svc.create_run(run_id=shared_run_id, source=None, sources=["naukri"])
    await svc.create_run(run_id=shared_run_id, source="LinkedIn")

    orchestrator_run = await svc.get_by_run_id(shared_run_id, source=None)
    linkedin_run = await svc.get_by_run_id(shared_run_id, source="LinkedIn")

    assert orchestrator_run.source is None
    assert linkedin_run.source == "LinkedIn"
    assert orchestrator_run.id != linkedin_run.id


@pytest.mark.asyncio
async def test_list_runs_filters_by_source_and_orders_newest_first(db_session) -> None:
    svc = HarvestRunService(db_session)
    await svc.create_run(run_id="20260807_130001", source="Naukri")
    await svc.create_run(run_id="20260807_130002", source="Naukri")
    await svc.create_run(run_id="20260807_130003", source="LinkedIn")

    naukri_runs = await svc.list_runs(source="Naukri")
    linkedin_runs = await svc.list_runs(source="LinkedIn")

    assert {r.run_id for r in naukri_runs} == {"20260807_130001", "20260807_130002"}
    assert {r.run_id for r in linkedin_runs} == {"20260807_130003"}


@pytest.mark.asyncio
async def test_get_by_job_id_returns_none_when_missing(db_session) -> None:
    svc = HarvestRunService(db_session)
    assert await svc.get_by_job_id("does-not-exist") is None


# ══════════════════════════════════════════════════════════════════════════════
# list_scraped_jobs — run_id scoping (backs GET /jobs?run_id=…)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_scraped_jobs_run_id_scopes_to_that_run(db_session) -> None:
    """GET /jobs?run_id=<display id> must return only that run's jobs. The
    caller passes the human-facing display run_id; list_scraped_jobs resolves
    it to the HarvestRunORM PK(s) via subquery."""
    svc = HarvestRunService(db_session)

    run_a = await svc.create_run(run_id="20260807_200001", source="LinkedIn")
    run_b = await svc.create_run(run_id="20260807_200002", source="LinkedIn")
    await svc.bulk_insert_scraped_jobs(run_a, [
        _unified_job_dict(job_title="A-one", job_url="https://linkedin.com/jobs/view/a1"),
        _unified_job_dict(job_title="A-two", job_url="https://linkedin.com/jobs/view/a2"),
    ])
    await svc.bulk_insert_scraped_jobs(run_b, [
        _unified_job_dict(job_title="B-one", job_url="https://linkedin.com/jobs/view/b1"),
    ])

    scoped, total = await svc.list_scraped_jobs(run_id="20260807_200001")
    assert total == 2
    assert {j.job_title for j in scoped} == {"A-one", "A-two"}

    all_jobs, all_total = await svc.list_scraped_jobs()
    assert all_total == 3


@pytest.mark.asyncio
async def test_list_scraped_jobs_unknown_run_id_returns_empty(db_session) -> None:
    svc = HarvestRunService(db_session)
    run = await svc.create_run(run_id="20260807_200003", source="LinkedIn")
    await svc.bulk_insert_scraped_jobs(run, [_unified_job_dict()])

    scoped, total = await svc.list_scraped_jobs(run_id="does_not_exist")
    assert scoped == []
    assert total == 0


# ══════════════════════════════════════════════════════════════════════════════
# Shared view helpers
# ══════════════════════════════════════════════════════════════════════════════

def test_filters_view_extracts_known_subset_with_defaults() -> None:
    view = filters_view({"keyword": "python", "location": "Remote", "unrelated": "dropped"})
    assert view["keyword"] == "python"
    assert view["location"] == "Remote"
    assert "unrelated" not in view
    assert view["max_jobs"] == 0
    assert view["include_undisclosed_salary"] is False


def test_filters_view_handles_none_snapshot() -> None:
    view = filters_view(None)
    assert view["keyword"] == ""
    assert view["salary_min"] is None


@pytest.mark.asyncio
async def test_run_to_result_summary_shape(db_session) -> None:
    svc = HarvestRunService(db_session)
    run_pk = await svc.create_run(
        run_id="20260807_140000", source="Dice", started_at=datetime.now(timezone.utc),
    )
    await svc.update_run(run_pk, status="success", combined_count=3, json_path="/data/results/dice/x.json")

    run = await svc.get_by_run_id("20260807_140000", source="Dice")
    summary = run_to_result_summary(run)

    assert summary == {
        "run_id": "20260807_140000",
        "executed_at": run.started_at.isoformat(),
        "status": "success",
        "total_found": 3,
        "source": "Dice",
        "file_path": "/data/results/dice/x.json",
    }
