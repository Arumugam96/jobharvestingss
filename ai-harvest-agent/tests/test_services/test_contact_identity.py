"""
Tests for the widened "same contact" (phone_verification_service.ContactIdentity):
same company AND any shared identity — primary phone key, recruiter_id, poster email
key or LinkedIn key — across inheritance (inherited_for_jobs and the SQL twin behind
GET /jobs' ``called`` filter), the "Not called yet" grouping (one canonical key per
job), siblings / sibling claims, and the sticky contact owner in auto-assignment.

The in-memory engine is shared by the whole session and log_call commits, so every
test uses its own company, phone numbers and (for auto-assignment) its own tenant,
and the rows it created are deleted afterwards (_cleanup) — other modules count
every scraped job in the shared database.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, literal_column, select

from app.models.auth import AuthenticatedUser, UserORM
from app.models.harvest_run import HarvestRunORM, ScrapedJobORM
from app.models.job_assignment_rotation import JobAssignmentRotationORM
from app.models.phone_call_log import PhoneCallLogORM
from app.models.recruiter import RecruiterORM
from app.services import auto_assignment_service as aas
from app.services import phone_verification_service as pvs
from app.services.harvest_run_service import HarvestRunService

LEAD = AuthenticatedUser(id="u-ci-lead", email="ci.lead@example.com", is_active=True,
                         is_verified=True, role="business_lead")
OTHER = AuthenticatedUser(id="u-ci-other", email="ci.other@example.com", is_active=True,
                          is_verified=True, role="business_lead")


# Rows created by the running test, by model — deleted by _cleanup.
_created: dict[object, list[str]] = {}  # model (or "tenant") → ids


def _track(model: object, row_id: str) -> str:
    _created.setdefault(model, []).append(row_id)
    return row_id


@pytest.fixture(autouse=True)
async def _cleanup(db_session):
    yield
    await db_session.rollback()
    jobs = _created.get(ScrapedJobORM, [])
    if jobs:
        await db_session.execute(delete(PhoneCallLogORM).where(PhoneCallLogORM.job_id.in_(jobs)))
    tenants = _created.pop("tenant", [])
    if tenants:
        await db_session.execute(delete(JobAssignmentRotationORM).where(
            JobAssignmentRotationORM.tenant_id.in_(tenants)))
    for model in (ScrapedJobORM, RecruiterORM, UserORM, HarvestRunORM):
        ids = _created.pop(model, [])
        if ids:
            await db_session.execute(delete(model).where(model.id.in_(ids)))
    await db_session.commit()


def _uniq() -> str:
    return uuid.uuid4().hex[:10]


def _phone() -> str:
    return "+91 9" + str(uuid.uuid4().int)[:9]


async def _run(db) -> str:
    run = HarvestRunORM(id=_track(HarvestRunORM, str(uuid.uuid4())), run_id=f"r_{_uniq()}",
                        status="completed")
    db.add(run)
    await db.flush()
    return run.id


async def _recruiter(db, *, tenant: str = "internal", phone: str = "", email: str = "") -> str:
    rec = RecruiterORM(id=_track(RecruiterORM, str(uuid.uuid4())), tenant_id=tenant, dedup_key=f"nc:{_uniq()}",
                       name_company_key=_uniq(), person_name="Priya", contact_number=phone,
                       official_email_id=email)
    db.add(rec)
    await db.commit()
    return rec.id


async def _job(db, run_id: str, *, company: str, phone: str = "", email: str = "",
               linkedin: str = "", recruiter_id: str | None = None, tenant: str = "internal",
               age_min: int = 0, assigned_to: str | None = None) -> ScrapedJobORM:
    job = ScrapedJobORM(
        id=_track(ScrapedJobORM, str(uuid.uuid4())), tenant_id=tenant, run_id=run_id,
        source="LinkedIn",
        job_title="Engineer", company=company, location="Chennai",
        job_url=f"https://x/{_uniq()}", contact_number=phone or None, email_id=email or None,
        linkedin_profile_url=linkedin or None, recruiter_id=recruiter_id,
        assigned_to_email=assigned_to,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_min),
    )
    db.add(job)
    await db.commit()
    return job


async def _log(db, job, user=LEAD, *, phone=None, outcome="connect_back", details=None):
    return await pvs.log_call(db, job, user, phone=phone or job.contact_number or "",
                              status="answered", outcome=outcome, details=details or {})


async def _contact_bucket(db, company: str) -> set[str]:
    """GET /jobs called="contact" — the SQL twin (contact_call_exists)."""
    jobs, _total = await HarvestRunService(db).list_scraped_jobs(company_exact=company,
                                                                 called="contact")
    return {j.id for j in jobs}


# ── Key helpers ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "https://www.linkedin.com/in/Priya-X/?trk=feed#top",
    "http://linkedin.com/in/priya-x/",
    "  www.LinkedIn.com/in/priya-x  ",
    "https://in.linkedin.com/in/priya-x",
    "N/A",
    "",
])
async def test_linkedin_key_sql_matches_python_twin(db_session, raw):
    # A SQL literal (not a bind): the SQLite compile repeats its argument.
    fake_job = SimpleNamespace(linkedin_profile_url=literal_column(repr(raw)))
    got = await db_session.execute(select(pvs.job_linkedin_key_expr(fake_job)))
    assert got.scalar_one() == pvs.job_linkedin_key(raw)


def test_email_and_linkedin_keys():
    assert pvs.job_email_key(" Poc@Acme.TEST ", "x@y.z") == "poc@acme.test"
    assert pvs.job_email_key("Not found", "Rec@Acme.test") == "rec@acme.test"
    assert pvs.job_email_key("", "") == ""
    assert pvs.job_linkedin_key("https://www.linkedin.com/in/A-b/?x=1") == "linkedin.com/in/a-b"
    assert pvs.job_linkedin_key("not available") == ""


# ── Inheritance via each identity ─────────────────────────────────────────────

async def test_inherits_via_recruiter_with_a_different_number(db_session):
    run = await _run(db_session)
    company = f"Acme {_uniq()}"
    rid = await _recruiter(db_session)
    a = await _job(db_session, run, company=company, phone=_phone(), recruiter_id=rid)
    b = await _job(db_session, run, company=company, phone=_phone(), recruiter_id=rid)  # new number
    await _log(db_session, a)
    got = await pvs.inherited_for_jobs(db_session, [b.id])
    assert got[b.id]["source_job_id"] == a.id and got[b.id]["inherited"] is True
    assert await _contact_bucket(db_session, company) == {b.id}


async def test_inherits_via_poster_email_including_recruiter_email(db_session):
    run = await _run(db_session)
    company, email = f"Acme {_uniq()}", f"Poc.{_uniq()}@Acme.test"
    a = await _job(db_session, run, company=company, phone=_phone(), email=email)
    # No own email: the linked recruiter's official email is the poster email.
    rid = await _recruiter(db_session, email=f"  {email.lower()} ")
    b = await _job(db_session, run, company=company, phone=_phone(), recruiter_id=rid)
    c = await _job(db_session, run, company=company, email="N/A")  # placeholder: no identity
    await _log(db_session, a)
    got = await pvs.inherited_for_jobs(db_session, [b.id, c.id])
    assert set(got) == {b.id}
    assert await _contact_bucket(db_session, company) == {b.id}


async def test_inherits_via_linkedin_profile(db_session):
    run = await _run(db_session)
    company, slug = f"Acme {_uniq()}", _uniq()
    a = await _job(db_session, run, company=company, phone=_phone(),
                   linkedin=f"https://www.linkedin.com/in/{slug}/?trk=x")
    b = await _job(db_session, run, company=company, linkedin=f"linkedin.com/in/{slug.upper()}")
    await _log(db_session, a)
    assert b.id in await pvs.inherited_for_jobs(db_session, [b.id])
    assert await _contact_bucket(db_session, company) == {b.id}


async def test_no_identity_inheritance_across_companies(db_session):
    run = await _run(db_session)
    rid, email, slug = await _recruiter(db_session), f"p.{_uniq()}@x.test", _uniq()
    a = await _job(db_session, run, company=f"Acme {_uniq()}", phone=_phone(), recruiter_id=rid,
                   email=email, linkedin=f"https://linkedin.com/in/{slug}")
    other = f"Other {_uniq()}"
    b = await _job(db_session, run, company=other, recruiter_id=rid, email=email,
                   linkedin=f"https://linkedin.com/in/{slug}")
    await _log(db_session, a)
    assert await pvs.inherited_for_jobs(db_session, [b.id]) == {}
    assert await _contact_bucket(db_session, other) == set()


async def test_opted_out_call_is_not_inherited_via_identity(db_session):
    run = await _run(db_session)
    company, rid = f"Acme {_uniq()}", await _recruiter(db_session)
    a = await _job(db_session, run, company=company, phone=_phone(), recruiter_id=rid)
    b = await _job(db_session, run, company=company, phone=_phone(), recruiter_id=rid)
    row = await _log(db_session, a, details={"applyToContact": False})
    assert row.claimed_siblings == 0
    assert await pvs.inherited_for_jobs(db_session, [b.id]) == {}
    assert await _contact_bucket(db_session, company) == set()
    await db_session.refresh(b)
    assert b.assigned_to_email is None


# ── "Not called yet" grouping: one canonical key per job ─────────────────────

async def test_group_contact_uses_one_canonical_key(db_session):
    run = await _run(db_session)
    company, rid = f"Acme {_uniq()}", await _recruiter(db_session)
    email, slug = f"p.{_uniq()}@x.test", _uniq()
    r_old = await _job(db_session, run, company=company, phone=_phone(), recruiter_id=rid, age_min=50)
    r_new = await _job(db_session, run, company=company, phone=_phone(), recruiter_id=rid, age_min=40)
    e_old = await _job(db_session, run, company=company, email=email, age_min=30)
    e_new = await _job(db_session, run, company=company, email=email.upper(), age_min=20)
    l_one = await _job(db_session, run, company=company, linkedin=f"https://linkedin.com/in/{slug}",
                       age_min=10)
    l_two = await _job(db_session, run, company=company, linkedin=f"www.linkedin.com/in/{slug}/")
    solo = await _job(db_session, run, company=company)  # no identity: never grouped
    jobs, total = await HarvestRunService(db_session).list_scraped_jobs(
        company_exact=company, called="no", group="contact")
    by_id = {j.id: j.sibling_count for j in jobs}
    assert total == 4
    assert by_id == {r_new.id: 1, e_new.id: 1, l_two.id: 1, solo.id: 0}
    assert not {r_old.id, e_old.id, l_one.id} & set(by_id)
    stats = (await HarvestRunService(db_session).job_facets(company_exact=company))["stats"]
    assert (stats["notcalled_jobs"], stats["notcalled"]) == (7, 4)


# ── Siblings and claims via identity ─────────────────────────────────────────

async def test_siblings_and_claims_via_identity(db_session):
    run = await _run(db_session)
    company, rid, email = f"Acme {_uniq()}", await _recruiter(db_session), f"p.{_uniq()}@x.test"
    a = await _job(db_session, run, company=company, phone=_phone(), recruiter_id=rid, email=email)
    by_rec = await _job(db_session, run, company=company, phone=_phone(), recruiter_id=rid)
    by_mail = await _job(db_session, run, company=company, email=email)
    held = await _job(db_session, run, company=company, recruiter_id=rid, assigned_to=OTHER.email)
    stranger = await _job(db_session, run, company=company, phone=_phone())
    elsewhere = await _job(db_session, run, company=f"Other {_uniq()}", recruiter_id=rid)
    got = await pvs.contact_siblings(db_session, a, a.contact_number)
    assert got["count"] == 3 and got["uncalled"] == 3
    assert {i["id"] for i in got["items"]} == {by_rec.id, by_mail.id, held.id}
    row = await _log(db_session, a)
    assert row.claimed_siblings == 2
    for j in (by_rec, by_mail, held, stranger, elsewhere):
        await db_session.refresh(j)
    assert by_rec.assigned_to_email == by_mail.assigned_to_email == LEAD.email
    assert held.assigned_to_email == OTHER.email
    assert stranger.assigned_to_email is None and elsewhere.assigned_to_email is None


# ── Auto-assignment: sticky contact owner ────────────────────────────────────

async def _users(db, tenant: str, *specs: tuple[str, bool]) -> list[AuthenticatedUser]:
    out = []
    _track("tenant", tenant)
    for email, active in specs:
        uid = _track(UserORM, str(uuid.uuid4()))
        db.add(UserORM(id=uid, email=email, tenant_id=tenant, is_active=active,
                       is_verified=True, role="business_lead"))
        out.append(AuthenticatedUser(id=uid, email=email, is_active=active, is_verified=True,
                                     role="business_lead", tenant_id=tenant))
    await db.commit()
    return out


async def _assigned(db, job) -> tuple[str | None, str]:
    await db.refresh(job)
    return job.assigned_to_email, job.assignment_source


async def test_new_job_goes_to_the_called_contacts_owner(db_session):
    tenant = f"t_{_uniq()}"
    owner, _other = await _users(db_session, tenant, (f"a.{_uniq()}@x.test", True),
                                 (f"b.{_uniq()}@x.test", True))
    run = await _run(db_session)
    company, rid = f"Acme {_uniq()}", await _recruiter(db_session, tenant=tenant)
    a = await _job(db_session, run, company=company, phone=_phone(), recruiter_id=rid, tenant=tenant)
    await _log(db_session, a, owner)
    # Harvested later: same recruiter, a new number.
    new = await _job(db_session, run, company=company, phone=_phone(), recruiter_id=rid, tenant=tenant)
    fresh = [await _job(db_session, run, company=company, phone=_phone(), tenant=tenant)
             for _ in range(2)]
    assert await aas.auto_assign_jobs(db_session, job_ids=[new.id, *(j.id for j in fresh)]) == 3
    await db_session.commit()
    assert await _assigned(db_session, new) == (owner.email, aas.SOURCE_CONTACT)
    # Uncalled contacts still go round-robin.
    for j in fresh:
        assert (await _assigned(db_session, j))[1] == aas.SOURCE_AUTO


async def test_inactive_owner_falls_back_to_round_robin(db_session):
    tenant = f"t_{_uniq()}"
    owner, active = await _users(db_session, tenant, (f"a.{_uniq()}@x.test", False),
                                 (f"b.{_uniq()}@x.test", True))
    run = await _run(db_session)
    company, email = f"Acme {_uniq()}", f"p.{_uniq()}@x.test"
    a = await _job(db_session, run, company=company, phone=_phone(), email=email, tenant=tenant)
    await _log(db_session, a, owner)
    new = await _job(db_session, run, company=company, phone=_phone(), email=email, tenant=tenant)
    assert await aas.auto_assign_jobs(db_session, job_ids=[new.id]) == 1
    await db_session.commit()
    assert await _assigned(db_session, new) == (active.email, aas.SOURCE_AUTO)


async def test_opted_out_call_gives_no_sticky_owner(db_session):
    tenant = f"t_{_uniq()}"
    owner, other = await _users(db_session, tenant, (f"a.{_uniq()}@x.test", True),
                                (f"b.{_uniq()}@x.test", True))
    run = await _run(db_session)
    company, rid = f"Acme {_uniq()}", await _recruiter(db_session, tenant=tenant)
    a = await _job(db_session, run, company=company, phone=_phone(), recruiter_id=rid, tenant=tenant)
    await _log(db_session, a, owner, details={"applyToContact": False})
    new = await _job(db_session, run, company=company, phone=_phone(), recruiter_id=rid, tenant=tenant)
    assert await pvs.contact_call_owners(db_session, tenant, [new.id]) == {}
    assert await aas.auto_assign_jobs(db_session, job_ids=[new.id]) == 1
    await db_session.commit()
    assert (await _assigned(db_session, new))[1] == aas.SOURCE_AUTO
