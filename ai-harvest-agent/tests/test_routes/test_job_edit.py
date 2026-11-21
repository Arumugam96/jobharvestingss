"""Tests for PATCH /jobs/{id} (manual job edit) and the job view's recruiter_location."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.models.auth import AuthenticatedUser
from app.models.harvest_run import HarvestRunORM, ScrapedJobORM
from app.models.recruiter import RecruiterORM

ADMIN = AuthenticatedUser(id="u-admin", email="admin@sightspectrum.com", is_active=True,
                          is_verified=True, role="admin")
LEAD = AuthenticatedUser(id="u-lead", email="lead@sightspectrum.com", is_active=True,
                         is_verified=True, role="business_lead")
OTHER_LEAD_EMAIL = "someone.else@sightspectrum.com"
PLAIN_USER = AuthenticatedUser(id="u-user", email="user@sightspectrum.com", is_active=True,
                               is_verified=True, role="user")


RUN_PREFIX = "jobedit-"


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(db_session):
    """The route commits, and the in-memory DB is shared by the whole session —
    remove this module's rows afterwards so tests that count jobs stay exact."""
    yield
    await db_session.rollback()
    runs = select(HarvestRunORM.id).where(HarvestRunORM.run_id.like(f"{RUN_PREFIX}%"))
    await db_session.execute(delete(ScrapedJobORM).where(ScrapedJobORM.run_id.in_(runs)))
    await db_session.execute(delete(HarvestRunORM).where(HarvestRunORM.run_id.like(f"{RUN_PREFIX}%")))
    await db_session.execute(delete(RecruiterORM).where(RecruiterORM.dedup_key.like(f"{RUN_PREFIX}%")))
    await db_session.commit()


@pytest_asyncio.fixture
async def as_user(db_session):
    """Yields (client, set_user): an API client whose get_current_user is whoever
    set_user() last picked (admin by default)."""
    from app.core.dependencies import get_current_user, get_db_session
    from app.main import create_app

    app = create_app()
    current = {"user": ADMIN}
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: current["user"]

    def set_user(user: AuthenticatedUser) -> None:
        current["user"] = user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c, set_user


async def _run(db) -> HarvestRunORM:
    run = HarvestRunORM(run_id=f"{RUN_PREFIX}{uuid.uuid4().hex[:12]}")
    db.add(run)
    await db.flush()
    return run


async def _job(db, run, company: str, **kw) -> ScrapedJobORM:
    job = ScrapedJobORM(
        run_id=run.id, source="linkedin", job_title="Data Engineer", company=company,
        location="Chennai, Tamil Nadu, India", job_url=f"https://example.com/{uuid.uuid4().hex}",
        **kw,
    )
    db.add(job)
    await db.flush()
    return job


def _uniq(name: str) -> str:
    """A company name unique to this test (the in-memory DB is shared across tests)."""
    return f"{name} {uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_admin_edits_job_assigned_to_someone_else(as_user, db_session) -> None:
    client, _ = as_user
    run = await _run(db_session)
    job = await _job(db_session, run, _uniq("Globex"), assigned_to_email=OTHER_LEAD_EMAIL)
    await db_session.commit()

    body = {
        "hiring_entity": "GCC",
        "company": "  Globex Renamed  ",
        "company_size": "51-200 employees",
        "domain": "Data Engineering",
        "job_poster_name": "Priya Raman",
        "job_poster_designation": "Talent Partner",
    }
    resp = await client.patch(f"/jobs/{job.id}", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["updated_other_jobs"] == 0
    view = data["job"]
    assert view["id"] == job.id
    assert view["hiring_entity"] == "GCC"
    assert view["is_gcc"] is True
    assert view["company"] == "Globex Renamed"
    assert view["company_size"] == "51-200 employees"
    assert view["company_size_tier"] == "Small"
    assert view["domain"] == "Data Engineering"
    assert view["job_poster_name"] == "Priya Raman"
    assert view["job_poster_designation"] == "Talent Partner"

    await db_session.refresh(job)
    assert job.company == "Globex Renamed" and job.hiring_entity == "GCC"


@pytest.mark.asyncio
async def test_only_sent_fields_change(as_user, db_session) -> None:
    client, _ = as_user
    run = await _run(db_session)
    job = await _job(db_session, run, _uniq("Initech"), hiring_entity="Direct Client",
                     domain="IT", job_poster_name="Old Name")
    await db_session.commit()

    resp = await client.patch(f"/jobs/{job.id}", json={"company_size": "1,001-5,000"})
    assert resp.status_code == 200, resp.text
    view = resp.json()["job"]
    assert view["company_size"] == "1,001-5,000 employees"  # bare band → stored form
    assert view["hiring_entity"] == "Direct Client"
    assert view["domain"] == "IT"
    assert view["job_poster_name"] == "Old Name"


@pytest.mark.asyncio
async def test_business_lead_edits_unassigned_and_own_job(as_user, db_session) -> None:
    client, set_user = as_user
    set_user(LEAD)
    run = await _run(db_session)
    free = await _job(db_session, run, _uniq("Umbrella"))
    mine = await _job(db_session, run, _uniq("Umbrella"), assigned_to_email="LEAD@sightspectrum.com")
    await db_session.commit()

    for job in (free, mine):
        resp = await client.patch(f"/jobs/{job.id}", json={"hiring_entity": "Staffing Firm"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["job"]["hiring_entity"] == "Staffing Firm"


@pytest.mark.asyncio
async def test_business_lead_cannot_edit_someone_elses_job(as_user, db_session) -> None:
    client, set_user = as_user
    set_user(LEAD)
    run = await _run(db_session)
    job = await _job(db_session, run, _uniq("Hooli"), assigned_to_email=OTHER_LEAD_EMAIL,
                     hiring_entity="Direct Client")
    await db_session.commit()

    resp = await client.patch(f"/jobs/{job.id}", json={"hiring_entity": "GCC"})
    assert resp.status_code == 403
    assert "assigned to" in resp.json()["detail"]
    await db_session.refresh(job)
    assert job.hiring_entity == "Direct Client"


@pytest.mark.asyncio
async def test_plain_user_cannot_edit(as_user, db_session) -> None:
    client, set_user = as_user
    set_user(PLAIN_USER)
    run = await _run(db_session)
    job = await _job(db_session, run, _uniq("Vandelay"))
    await db_session.commit()

    resp = await client.patch(f"/jobs/{job.id}", json={"domain": "SAP"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_missing_job_404(as_user) -> None:
    client, _ = as_user
    resp = await client.patch(f"/jobs/{uuid.uuid4()}", json={"domain": "SAP"})
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"hiring_entity": "Any"},
    {"hiring_entity": "Recruiter"},
    {"company_size": "lots of people"},
    {"company": "   "},
    {"domain": "x" * 51},
])
async def test_invalid_values_422(as_user, db_session, body) -> None:
    client, _ = as_user
    run = await _run(db_session)
    job = await _job(db_session, run, _uniq("Stark"))
    await db_session.commit()

    resp = await client.patch(f"/jobs/{job.id}", json=body)
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_apply_to_company_updates_same_company_only(as_user, db_session) -> None:
    client, _ = as_user
    run = await _run(db_session)
    tag = uuid.uuid4().hex[:8]
    target = await _job(db_session, run, f"Acme{tag} Corp", job_poster_name="Target POC")
    same_a = await _job(db_session, run, f"ACME{tag} Inc.", job_poster_name="Keep A",
                        hiring_entity="Staffing Firm", company_size="", domain="IT")
    same_b = await _job(db_session, run, f"acme{tag}", job_poster_name="Keep B")
    # Shares the SQL pre-filter substring but is a different company key.
    different = await _job(db_session, run, f"Acme{tag}ville", hiring_entity="Ambiguous",
                           company_size="2-10 employees", domain="Cloud")
    await db_session.commit()

    resp = await client.patch(f"/jobs/{target.id}", json={
        "hiring_entity": "Direct Client",
        "company_size": "10,001+ employees",
        "domain": "SAP",
        "job_poster_name": "New POC",
        "job_poster_designation": "Recruiter",
        "apply_to_company": True,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_other_jobs"] == 2

    rows = {
        r.id: r for r in (await db_session.execute(
            select(ScrapedJobORM).where(ScrapedJobORM.id.in_(
                [target.id, same_a.id, same_b.id, different.id])
            ).execution_options(populate_existing=True)
        )).scalars()
    }
    for jid, poc in ((same_a.id, "Keep A"), (same_b.id, "Keep B")):
        row = rows[jid]
        assert (row.hiring_entity, row.company_size, row.domain) == (
            "Direct Client", "10,001+ employees", "SAP")
        assert row.is_gcc is False
        # Name / POC fields are never propagated.
        assert row.job_poster_name == poc
        assert row.job_poster_designation is None
    assert rows[same_a.id].company == f"ACME{tag} Inc."
    other = rows[different.id]
    assert (other.hiring_entity, other.company_size, other.domain) == (
        "Ambiguous", "2-10 employees", "Cloud")
    assert rows[target.id].job_poster_name == "New POC"


@pytest.mark.asyncio
async def test_apply_to_company_only_sent_company_fields(as_user, db_session) -> None:
    client, _ = as_user
    run = await _run(db_session)
    tag = uuid.uuid4().hex[:8]
    target = await _job(db_session, run, f"Wayne{tag}")
    sibling = await _job(db_session, run, f"Wayne{tag} Ltd", hiring_entity="GCC", is_gcc=True,
                         company_size="51-200 employees", domain="IT")
    await db_session.commit()

    resp = await client.patch(f"/jobs/{target.id}", json={"domain": "Cloud", "apply_to_company": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_other_jobs"] == 1
    await db_session.refresh(sibling)
    assert (sibling.hiring_entity, sibling.is_gcc, sibling.company_size, sibling.domain) == (
        "GCC", True, "51-200 employees", "Cloud")


@pytest.mark.asyncio
async def test_recruiter_location_in_view(as_user, db_session) -> None:
    from app.services.harvest_run_service import recruiter_location, scraped_job_view

    client, _ = as_user
    run = await _run(db_session)
    rec = RecruiterORM(dedup_key=f"{RUN_PREFIX}{uuid.uuid4().hex}", name_company_key="x",
                       person_name="Priya", city="Chennai", state="Tamil Nadu",
                       country="India", location="Somewhere else")
    db_session.add(rec)
    await db_session.flush()
    job = await _job(db_session, run, _uniq("Cyberdyne"), recruiter_id=rec.id)
    bare = await _job(db_session, run, _uniq("Cyberdyne"))
    await db_session.commit()

    resp = await client.patch(f"/jobs/{job.id}", json={"domain": "IT"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["job"]["recruiter_location"] == "Chennai, Tamil Nadu, India"

    resp = await client.patch(f"/jobs/{bare.id}", json={"domain": "IT"})
    assert resp.json()["job"]["recruiter_location"] is None

    # Fallbacks: duplicate parts collapse; no structured parts → scraped location.
    assert recruiter_location(RecruiterORM(city="Singapore", state="", country="singapore",
                                           location="")) == "Singapore"
    assert recruiter_location(RecruiterORM(city="", state="", country="",
                                           location=" Bengaluru, Karnataka ")) == "Bengaluru, Karnataka"
    assert recruiter_location(RecruiterORM(city="", state="", country="", location="")) is None
    assert "recruiter_location" in scraped_job_view(bare)
