"""
Tests for phone_verification_service — logging calls (every outcome saves with no
required fields), the do-not-call side effects, and contact-level inheritance: a
call on one job stands for the same contact's (phone key + company) other jobs,
derived at read time (inherited_for_jobs, GET /jobs' ``called`` / ``group``
filters and the facets' call cards) — never copied.

The in-memory engine is shared by the whole session and log_call commits, so every
test uses its own company name and phone number.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.auth import AuthenticatedUser
from app.models.harvest_run import HarvestRunORM, ScrapedJobORM
from app.models.phone_call_log import PhoneCallLogORM
from app.models.phone_contact_flag import PhoneContactFlagORM
from app.models.suppression import EmailSuppressionORM
from app.services import phone_verification_service as pvs
from app.services.harvest_run_service import HarvestRunService

LEAD = AuthenticatedUser(id="u-lead", email="lead.one@example.com", is_active=True,
                         is_verified=True, role="business_lead")
OTHER = AuthenticatedUser(id="u-other", email="lead.two@example.com", is_active=True,
                          is_verified=True, role="business_lead")


def _uniq() -> str:
    return uuid.uuid4().hex[:10]


def _phone() -> str:
    """A fresh 10-digit number (unique per test) with a +91 prefix."""
    return "+91 9" + str(uuid.uuid4().int)[:9]


async def _run(db) -> str:
    run = HarvestRunORM(id=str(uuid.uuid4()), run_id=f"r_{_uniq()}", status="completed")
    db.add(run)
    await db.flush()
    return run.id


async def _job(db, run_id: str, *, company: str, phone: str = "", title: str = "Engineer",
               email: str = "", age_min: int = 0, assigned_to: str | None = None,
               company_phone: str = "") -> ScrapedJobORM:
    job = ScrapedJobORM(
        id=str(uuid.uuid4()), tenant_id="internal", run_id=run_id, source="LinkedIn",
        job_title=title, company=company, location="Chennai", job_url=f"https://x/{_uniq()}",
        contact_number=phone or None, email_id=email or None,
        company_contact_phone=company_phone or None,
        assigned_to_email=assigned_to,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_min),
    )
    db.add(job)
    await db.commit()
    return job


async def _log(db, job, user=LEAD, *, phone=None, status="answered", outcome="connect_back",
               details=None, **kw):
    return await pvs.log_call(db, job, user, phone=phone or job.contact_number or "",
                              status=status, outcome=outcome, details=details or {}, **kw)


# ── Saving: no required fields ───────────────────────────────────────────────

@pytest.mark.parametrize("status,outcome", [
    (status, outcome) for status, outs in pvs.CALL_OUTCOMES.items() for outcome in sorted(outs)
])
async def test_every_outcome_saves_with_empty_details_remarks_and_proof(db_session, status, outcome):
    run = await _run(db_session)
    job = await _job(db_session, run, company=f"Co {_uniq()}", phone=_phone())
    row = await _log(db_session, job, status=status, outcome=outcome, details={}, remarks="",
                     proof=[])
    assert row.id and row.call_status == status and row.call_outcome == outcome
    assert json.loads(row.outcome_details) == {}


async def test_apply_to_contact_is_kept_for_any_outcome(db_session):
    run = await _run(db_session)
    job = await _job(db_session, run, company=f"Co {_uniq()}", phone=_phone())
    row = await _log(db_session, job, status="unanswered", outcome="busy",
                     details={"applyToContact": False, "junk": 1})
    assert json.loads(row.outcome_details) == {"applyToContact": False}


# ── do_not_call ───────────────────────────────────────────────────────────────

async def test_do_not_call_without_confirm_flags_number_but_keeps_email(db_session):
    run = await _run(db_session)
    email = f"poc.{_uniq()}@acme.test"
    job = await _job(db_session, run, company=f"Co {_uniq()}", phone=_phone(), email=email)
    await _log(db_session, job, outcome="do_not_call", details={}, phone_source="job")
    flag = (await db_session.execute(select(PhoneContactFlagORM).where(
        PhoneContactFlagORM.phone_key == pvs.normalize_phone(job.contact_number)))).scalar_one()
    assert flag.dnc_at is not None
    assert (await db_session.execute(select(EmailSuppressionORM).where(
        EmailSuppressionORM.email == email))).first() is None


async def test_do_not_call_with_confirm_suppresses_email(db_session):
    run = await _run(db_session)
    email = f"poc.{_uniq()}@acme.test"
    job = await _job(db_session, run, company=f"Co {_uniq()}", phone=_phone(), email=email)
    await _log(db_session, job, outcome="do_not_call", details={"dncConfirm": True},
               phone_source="job")
    assert (await db_session.execute(select(EmailSuppressionORM).where(
        EmailSuppressionORM.email == email))).first() is not None


# ── Inheritance ───────────────────────────────────────────────────────────────

async def test_call_on_job_a_is_inherited_by_job_b_same_phone_and_company(db_session):
    run = await _run(db_session)
    company, phone = f"Acme {_uniq()}", _phone()
    a = await _job(db_session, run, company=company, phone=phone, title="Job A")
    # Same contact, formatted differently and the company with other case/spaces.
    b = await _job(db_session, run, company=f"  {company.upper()} ", phone=phone[-10:])
    await _log(db_session, a)
    got = await pvs.inherited_for_jobs(db_session, [b.id])
    assert got[b.id]["inherited"] is True
    assert got[b.id]["source_job_id"] == a.id
    assert got[b.id]["source_job_title"] == "Job A"
    assert got[b.id]["outcome"] == "connect_back"
    # A job never inherits its own call.
    assert a.id not in await pvs.inherited_for_jobs(db_session, [a.id])


async def test_unanswered_and_not_interested_inherit(db_session):
    run = await _run(db_session)
    company, phone = f"Acme {_uniq()}", _phone()
    a = await _job(db_session, run, company=company, phone=phone)
    b = await _job(db_session, run, company=company, phone=phone)
    await _log(db_session, a, status="unanswered", outcome="no_answer")
    assert (await pvs.inherited_for_jobs(db_session, [b.id]))[b.id]["outcome"] == "no_answer"
    await _log(db_session, a, outcome="not_interested", details={"reason": "x", "revisit": "1 mo"})
    assert (await pvs.inherited_for_jobs(db_session, [b.id]))[b.id]["outcome"] == "not_interested"


async def test_recruiter_or_company_phone_is_the_job_key_when_no_own_number(db_session):
    run = await _run(db_session)
    company, phone = f"Acme {_uniq()}", _phone()
    a = await _job(db_session, run, company=company, phone=phone)
    b = await _job(db_session, run, company=company, company_phone=phone)
    await _log(db_session, a)
    assert b.id in await pvs.inherited_for_jobs(db_session, [b.id])


async def test_no_inheritance_across_companies(db_session):
    run = await _run(db_session)
    phone = _phone()
    a = await _job(db_session, run, company=f"Acme {_uniq()}", phone=phone)
    b = await _job(db_session, run, company=f"Other {_uniq()}", phone=phone)
    await _log(db_session, a)
    assert await pvs.inherited_for_jobs(db_session, [b.id]) == {}


async def test_position_closed_is_not_inherited(db_session):
    run = await _run(db_session)
    company, phone = f"Acme {_uniq()}", _phone()
    a = await _job(db_session, run, company=company, phone=phone)
    b = await _job(db_session, run, company=company, phone=phone)
    await _log(db_session, a, outcome="position_closed")
    assert await pvs.inherited_for_jobs(db_session, [b.id]) == {}


async def test_opted_out_call_is_not_inherited_and_claims_nothing(db_session):
    run = await _run(db_session)
    company, phone = f"Acme {_uniq()}", _phone()
    a = await _job(db_session, run, company=company, phone=phone)
    b = await _job(db_session, run, company=company, phone=phone)
    row = await _log(db_session, a, details={"window": "Tomorrow", "applyToContact": False})
    assert row.claimed_siblings == 0
    assert await pvs.inherited_for_jobs(db_session, [b.id]) == {}
    await db_session.refresh(b)
    assert b.assigned_to_email is None


async def test_clear_calls_removes_inheritance(db_session):
    run = await _run(db_session)
    company, phone = f"Acme {_uniq()}", _phone()
    a = await _job(db_session, run, company=company, phone=phone)
    b = await _job(db_session, run, company=company, phone=phone)
    await _log(db_session, a)
    assert b.id in await pvs.inherited_for_jobs(db_session, [b.id])
    await pvs.clear_calls(db_session, a.id)
    assert await pvs.inherited_for_jobs(db_session, [b.id]) == {}


async def test_inheritance_copies_no_rows(db_session):
    run = await _run(db_session)
    company, phone = f"Acme {_uniq()}", _phone()
    a = await _job(db_session, run, company=company, phone=phone)
    b = await _job(db_session, run, company=company, phone=phone)
    await _log(db_session, a)
    rows = (await db_session.execute(select(PhoneCallLogORM.job_id).where(
        PhoneCallLogORM.job_id.in_([a.id, b.id])))).scalars().all()
    assert rows == [a.id]


# ── GET /jobs `called` filter, facets and grouping ───────────────────────────

async def test_called_filter_and_stats(db_session):
    run = await _run(db_session)
    company, phone = f"Acme {_uniq()}", _phone()
    a = await _job(db_session, run, company=company, phone=phone)
    b = await _job(db_session, run, company=company, phone=phone)
    c = await _job(db_session, run, company=company, phone=_phone())  # other contact
    d = await _job(db_session, run, company=company)  # no phone at all
    await _log(db_session, a)
    svc = HarvestRunService(db_session)

    async def ids(called):
        jobs, total = await svc.list_scraped_jobs(company_exact=company, called=called)
        assert total == len(jobs)
        return {j.id for j in jobs}

    assert await ids("yes") == {a.id}
    assert await ids("contact") == {b.id}
    assert await ids("no") == {c.id, d.id}
    stats = (await svc.job_facets(company_exact=company))["stats"]
    assert (stats["total"], stats["called"], stats["contactcalled"], stats["notcalled"]) == (4, 1, 1, 2)
    # The bucket's own count is filter-consistent too.
    stats = (await svc.job_facets(company_exact=company, called="contact"))["stats"]
    assert stats["total"] == 1


async def test_group_contact_keeps_newest_job_with_sibling_count(db_session):
    run = await _run(db_session)
    company, phone = f"Acme {_uniq()}", _phone()
    old = await _job(db_session, run, company=company, phone=phone, age_min=30)
    mid = await _job(db_session, run, company=company, phone=phone, age_min=20)
    new = await _job(db_session, run, company=company, phone=phone, age_min=10)
    solo1 = await _job(db_session, run, company=company)  # no phone: never grouped
    solo2 = await _job(db_session, run, company=company)
    svc = HarvestRunService(db_session)
    jobs, total = await svc.list_scraped_jobs(company_exact=company, called="no", group="contact")
    by_id = {j.id: j.sibling_count for j in jobs}
    assert total == 3 == len(jobs)
    assert by_id == {new.id: 2, solo1.id: 0, solo2.id: 0}
    assert old.id not in by_id and mid.id not in by_id
    # Pagination totals follow the grouped rows.
    page1, total1 = await svc.list_scraped_jobs(company_exact=company, called="no",
                                                group="contact", page=1, page_size=2)
    assert total1 == 3 and len(page1) == 2
    # Ungrouped, every job is listed.
    _jobs, total = await svc.list_scraped_jobs(company_exact=company, called="no")
    assert total == 5


# ── Siblings and claiming ─────────────────────────────────────────────────────

async def test_siblings_lists_other_jobs_of_the_contact(db_session):
    run = await _run(db_session)
    company, phone = f"Acme {_uniq()}", _phone()
    a = await _job(db_session, run, company=company, phone=phone)
    b = await _job(db_session, run, company=company, phone=phone, assigned_to=OTHER.email)
    c = await _job(db_session, run, company=company, phone=phone)
    await _job(db_session, run, company=f"Other {_uniq()}", phone=phone)
    await _log(db_session, c, OTHER)
    got = await pvs.contact_siblings(db_session, a, phone)
    assert got["count"] == 2 and got["uncalled"] == 1
    items = {i["id"]: i for i in got["items"]}
    assert set(items) == {b.id, c.id}
    assert items[b.id]["assigned_to"]["email"] == OTHER.email
    assert items[c.id]["called"] is True and items[b.id]["called"] is False


async def test_log_call_claims_only_unassigned_siblings(db_session):
    run = await _run(db_session)
    company, phone = f"Acme {_uniq()}", _phone()
    a = await _job(db_session, run, company=company, phone=phone)
    free = await _job(db_session, run, company=company, phone=phone)
    held = await _job(db_session, run, company=company, phone=phone, assigned_to=OTHER.email)
    elsewhere = await _job(db_session, run, company=f"Other {_uniq()}", phone=phone)
    row = await _log(db_session, a)
    assert row.claimed_siblings == 1
    for j in (a, free, held, elsewhere):
        await db_session.refresh(j)
    assert a.assigned_to_email == LEAD.email
    assert free.assigned_to_email == LEAD.email
    assert held.assigned_to_email == OTHER.email
    assert elsewhere.assigned_to_email is None


async def test_position_closed_claims_no_siblings(db_session):
    run = await _run(db_session)
    company, phone = f"Acme {_uniq()}", _phone()
    a = await _job(db_session, run, company=company, phone=phone)
    b = await _job(db_session, run, company=company, phone=phone)
    row = await _log(db_session, a, outcome="position_closed")
    assert row.claimed_siblings == 0
    await db_session.refresh(b)
    assert b.assigned_to_email is None
