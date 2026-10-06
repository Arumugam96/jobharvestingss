"""Tests that the company+location HR contact flows end-to-end WITHOUT touching the
original poster: bulk_insert attaches it from the cache, scraped_job_view resolves the
outreach recipient, _dedupe_targets collapses a shared company contact to one send, and
build_greeting addresses whoever we actually email.
"""
from __future__ import annotations

import pytest

from app.core.location import normalize_location_key
from app.prompts.outreach_prompts import build_greeting
from app.services.auto_outreach_service import _dedupe_targets
from app.services.company_location_contact_service import upsert_location_contact
from app.services.company_service import normalize_company_key
from app.services.harvest_run_service import HarvestRunService, scraped_job_view

_LOC = "Bengaluru, Karnataka, India"
_CK = normalize_company_key("Acme")
_LK = normalize_location_key(_LOC)


async def _seed_cache(db) -> None:
    await upsert_location_contact(
        db, company_key=_CK, location_key=_LK,
        company_name="Acme", location=_LOC, domain="acme.com",
        hr_contact_name="HR Person", hr_contact_title="Talent Acquisition",
        hr_contact_email="hr@acme.com", hr_contact_phone="", attempted=True,
    )
    await db.flush()


def _job(**over) -> dict:
    base = dict(
        source="LinkedIn", job_title="Backend Engineer", company="Acme", location=_LOC,
        job_url="https://linkedin.com/jobs/view/1",
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_bulk_insert_attaches_contact_for_posterless_email_less_job(db_session) -> None:
    svc = HarvestRunService(db_session)
    await _seed_cache(db_session)
    run_pk = await svc.create_run(run_id="20260101_000001", source="LinkedIn")

    await svc.bulk_insert_scraped_jobs(run_pk, [_job(job_url="https://linkedin.com/jobs/view/10")])

    rows = await svc.list_jobs_for_run(run_pk)
    assert len(rows) == 1
    job = rows[0]
    assert job.company_contact_email == "hr@acme.com"
    assert job.company_contact_name == "HR Person"
    assert job.company_contact_source == "apollo"
    # Poster fields untouched (there was no poster).
    assert job.job_poster_name is None
    assert (job.email_id or "") == ""


@pytest.mark.asyncio
async def test_bulk_insert_preserves_named_poster_when_attaching_contact(db_session) -> None:
    svc = HarvestRunService(db_session)
    await _seed_cache(db_session)
    run_pk = await svc.create_run(run_id="20260101_000002", source="LinkedIn")

    # Poster present but NO email found for them -> company contact is attached,
    # the real poster's identity is preserved.
    await svc.bulk_insert_scraped_jobs(run_pk, [
        _job(job_url="https://linkedin.com/jobs/view/11", job_poster_name="Real Poster",
             linkedin_profile_url="https://linkedin.com/in/realposter")
    ])

    job = (await svc.list_jobs_for_run(run_pk))[0]
    assert job.job_poster_name == "Real Poster"                 # preserved
    assert job.linkedin_profile_url == "https://linkedin.com/in/realposter"
    assert job.company_contact_email == "hr@acme.com"           # attached separately


@pytest.mark.asyncio
async def test_bulk_insert_skips_contact_when_job_has_real_email(db_session) -> None:
    svc = HarvestRunService(db_session)
    await _seed_cache(db_session)
    run_pk = await svc.create_run(run_id="20260101_000003", source="LinkedIn")

    await svc.bulk_insert_scraped_jobs(run_pk, [
        _job(job_url="https://linkedin.com/jobs/view/12", job_poster_name="Real Poster",
             email_id="real@acme.com")
    ])

    job = (await svc.list_jobs_for_run(run_pk))[0]
    assert (job.company_contact_email or "") == ""             # not attached
    assert job.email_id == "real@acme.com"                      # real poster email wins


@pytest.mark.asyncio
async def test_scraped_job_view_resolves_company_contact_as_recipient(db_session) -> None:
    svc = HarvestRunService(db_session)
    await _seed_cache(db_session)
    run_pk = await svc.create_run(run_id="20260101_000004", source="LinkedIn")
    await svc.bulk_insert_scraped_jobs(run_pk, [_job(job_url="https://linkedin.com/jobs/view/13")])
    job = (await svc.list_jobs_for_run(run_pk))[0]

    view = scraped_job_view(job)
    # email_id stays scraped-or-recruiter only (None here); outreach_* adds the fallback.
    assert view["email_id"] is None
    assert view["outreach_to_email"] == "hr@acme.com"
    assert view["outreach_to_name"] == "HR Person"
    assert view["company_contact_email"] == "hr@acme.com"
    # Greeting addresses the actual recipient, not a missing poster.
    assert build_greeting(view) == "Hi HR,"


@pytest.mark.asyncio
async def test_dedupe_collapses_shared_company_contact_to_one_send(db_session) -> None:
    svc = HarvestRunService(db_session)
    await _seed_cache(db_session)
    run_pk = await svc.create_run(run_id="20260101_000005", source="LinkedIn")
    # Two different postings, same company+location, both email-less -> one company contact.
    await svc.bulk_insert_scraped_jobs(run_pk, [
        _job(job_url="https://linkedin.com/jobs/view/21"),
        _job(job_url="https://linkedin.com/jobs/view/22"),
    ])
    rows = await svc.list_jobs_for_run(run_pk)

    targets, no_email, below_size = _dedupe_targets(rows)
    assert no_email == 0
    assert len(targets) == 1                       # collapsed to a single send
    t = targets[0]
    assert t["email"] == "hr@acme.com"
    assert t["contact_kind"] == "company"
    assert t["recruiter_id"] is None               # the company contact isn't the poster


def test_build_greeting_prefers_outreach_name() -> None:
    assert build_greeting({"outreach_to_name": "HR Person"}) == "Hi HR,"
    assert build_greeting({"job_poster_name": "Real Poster"}) == "Hi Real,"
    assert build_greeting({}) == "Hello,"
