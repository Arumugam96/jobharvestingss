"""
Tests for the re-enrichment persistence path (HarvestRunService): jobs that
degraded to selector-only (all LLM providers down) are queued, flagged pending,
then either backfilled on a successful retry (apply) or given up on (fail).
"""
from __future__ import annotations

import pytest

from app.services.harvest_run_service import HarvestRunService

JOB_URL = "https://linkedin.com/jobs/view/555"


def _degraded_job(**overrides) -> dict:
    base = dict(
        source="LinkedIn",
        job_title="Data Engineer",
        company="Acme",
        location="Remote",
        job_url=JOB_URL,
        job_description="",  # selector-only: no description captured
    )
    base.update(overrides)
    return base


def _queue_item(**overrides) -> dict:
    base = dict(
        job_url=JOB_URL,
        source="LinkedIn",
        content="---JOB DETAIL PAGE---\nAbout the job ...",
        schema_description='{"description": str}',
        system="You extract structured job data.",
    )
    base.update(overrides)
    return base


async def _seed_pending(svc: HarvestRunService) -> str:
    """Create a run with one degraded job + its queued re-enrichment task."""
    run_pk = await svc.create_run(run_id="20260904_100000", source="LinkedIn")
    await svc.bulk_insert_scraped_jobs(run_pk, [_degraded_job()])
    await svc.enqueue_reenrichment(run_pk, [_queue_item()])
    await svc.mark_extraction_pending(run_pk, [JOB_URL])
    return run_pk


@pytest.mark.asyncio
async def test_enqueue_and_mark_pending(db_session) -> None:
    svc = HarvestRunService(db_session)
    run_pk = await _seed_pending(svc)

    tasks = await svc.list_pending_reenrichment(10)
    assert len(tasks) == 1
    t = tasks[0]
    assert t["job_url"] == JOB_URL
    assert t["content"].startswith("---JOB DETAIL PAGE---")
    assert t["schema_description"] and t["system"]

    jobs = await svc.list_jobs_for_run(run_pk)
    assert jobs[0].extraction_status == "pending"


@pytest.mark.asyncio
async def test_enqueue_dedups_same_url(db_session) -> None:
    svc = HarvestRunService(db_session)
    run_pk = await svc.create_run(run_id="20260904_101000", source="LinkedIn")
    inserted = await svc.enqueue_reenrichment(run_pk, [_queue_item(), _queue_item()])
    assert inserted == 1
    assert len(await svc.list_pending_reenrichment(10)) == 1


@pytest.mark.asyncio
async def test_apply_reenrichment_backfills_and_marks_done(db_session) -> None:
    svc = HarvestRunService(db_session)
    run_pk = await _seed_pending(svc)
    task_id = (await svc.list_pending_reenrichment(10))[0]["id"]

    await svc.apply_reenrichment(task_id, run_pk, JOB_URL, {
        "description": "We build scalable data pipelines.",
        "description_html": "<p>We build scalable data pipelines.</p>",
        "skills": ["Python", "Spark"],
        "recruiter_name": "Jane Doe",
        "recruiter_email": "jane.doe@acme.com",
    })

    job = (await svc.list_jobs_for_run(run_pk))[0]
    assert job.job_description == "We build scalable data pipelines."
    assert job.job_description_html == "<p>We build scalable data pipelines.</p>"
    assert job.email_id == "jane.doe@acme.com"
    assert job.extraction_status == "ok"
    # Task no longer pending.
    assert await svc.list_pending_reenrichment(10) == []


@pytest.mark.asyncio
async def test_apply_does_not_overwrite_with_blanks(db_session) -> None:
    """A retry that returns empty fields must not wipe existing selector data."""
    svc = HarvestRunService(db_session)
    run_pk = await svc.create_run(run_id="20260904_102000", source="LinkedIn")
    await svc.bulk_insert_scraped_jobs(run_pk, [_degraded_job(job_description="kept")])
    await svc.enqueue_reenrichment(run_pk, [_queue_item()])
    task_id = (await svc.list_pending_reenrichment(10))[0]["id"]

    await svc.apply_reenrichment(task_id, run_pk, JOB_URL, {"description": ""})

    job = (await svc.list_jobs_for_run(run_pk))[0]
    assert job.job_description == "kept"        # blank did not overwrite
    assert job.extraction_status == "ok"        # still marked resolved


@pytest.mark.asyncio
async def test_fail_reenrichment_marks_failed(db_session) -> None:
    svc = HarvestRunService(db_session)
    run_pk = await _seed_pending(svc)
    task_id = (await svc.list_pending_reenrichment(10))[0]["id"]

    await svc.fail_reenrichment(task_id, run_pk, JOB_URL, "expired")

    assert await svc.list_pending_reenrichment(10) == []
    job = (await svc.list_jobs_for_run(run_pk))[0]
    assert job.extraction_status == "failed"


@pytest.mark.asyncio
async def test_bump_attempt_keeps_pending(db_session) -> None:
    svc = HarvestRunService(db_session)
    run_pk = await _seed_pending(svc)
    task_id = (await svc.list_pending_reenrichment(10))[0]["id"]

    await svc.bump_reenrichment_attempt(task_id)

    tasks = await svc.list_pending_reenrichment(10)
    assert len(tasks) == 1              # still pending for a later run
    assert tasks[0]["attempts"] == 1
