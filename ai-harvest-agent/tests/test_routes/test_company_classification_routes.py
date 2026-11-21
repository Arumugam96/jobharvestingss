"""Admin API for company overrides / classification keywords, and PATCH /jobs/{id}
``apply_to_company`` saving a manual override."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.models.auth import AuthenticatedUser
from app.models.company_override import ClassificationKeywordORM, CompanyOverrideORM
from app.models.harvest_run import HarvestRunORM, ScrapedJobORM

ADMIN = AuthenticatedUser(id="u-admin", email="admin@sightspectrum.com", is_active=True,
                          is_verified=True, role="admin")
LEAD = AuthenticatedUser(id="u-lead", email="lead@sightspectrum.com", is_active=True,
                         is_verified=True, role="business_lead")
PLAIN_USER = AuthenticatedUser(id="u-user", email="user@sightspectrum.com", is_active=True,
                               is_verified=True, role="user")
RUN_PREFIX = "ccroutes-"


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(db_session):
    """Routes commit into the shared in-memory DB — remove this module's rows."""
    yield
    await db_session.rollback()
    runs = select(HarvestRunORM.id).where(HarvestRunORM.run_id.like(f"{RUN_PREFIX}%"))
    await db_session.execute(delete(ScrapedJobORM).where(ScrapedJobORM.run_id.in_(runs)))
    await db_session.execute(delete(HarvestRunORM).where(HarvestRunORM.run_id.like(f"{RUN_PREFIX}%")))
    await db_session.execute(delete(CompanyOverrideORM))
    await db_session.execute(delete(ClassificationKeywordORM))
    await db_session.commit()


@pytest_asyncio.fixture
async def as_user(db_session):
    """(client, set_user): an API client authenticated as whoever set_user() picked."""
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


def _uniq(name: str) -> str:
    return f"{name} {uuid.uuid4().hex[:8]}"


# ── Auth ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("user", [LEAD, PLAIN_USER], ids=["lead", "user"])
async def test_non_admins_get_403(as_user, user) -> None:
    client, set_user = as_user
    set_user(user)
    calls = [
        client.get("/company-overrides"),
        client.put("/company-overrides/acme", json={"hiring_entity": "GCC"}),
        client.delete("/company-overrides/acme"),
        client.get("/classification-keywords"),
        client.post("/classification-keywords", json={"list": "staffing", "term": "x"}),
        client.delete("/classification-keywords/1"),
    ]
    for coro in calls:
        resp = await coro
        assert resp.status_code == 403, resp.text


# ── Overrides CRUD ───────────────────────────────────────────────────────────

async def test_admin_override_crud(as_user) -> None:
    client, _ = as_user
    name = _uniq("Acme Consulting Pvt Ltd")
    resp = await client.put(f"/company-overrides/{name}", json={
        "hiring_entity": "Direct Client", "company_size": "51-200", "domain": "Finance"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    key = body["company_key"]
    assert key == name.lower().replace(" pvt ltd", "")  # normalized
    assert body["company_name"] == name
    assert (body["hiring_entity"], body["company_size"], body["domain"], body["source"]) == (
        "Direct Client", "51-200 employees", "Finance", "manual")
    assert body["updated_by_email"] == ADMIN.email

    # Edit by key (full replace: omitted fields clear).
    resp = await client.put(f"/company-overrides/{key}", json={"hiring_entity": "GCC"})
    assert resp.status_code == 200
    assert (resp.json()["hiring_entity"], resp.json()["company_size"], resp.json()["domain"]) == (
        "GCC", None, None)
    assert resp.json()["company_name"] == name  # kept

    listed = (await client.get("/company-overrides", params={"q": "acme consulting"})).json()
    assert listed["total"] == 1 and listed["items"][0]["company_key"] == key
    assert (await client.get("/company-overrides", params={"source": "llm"})).json()["total"] == 0

    assert (await client.delete(f"/company-overrides/{key}")).status_code == 200
    assert (await client.delete(f"/company-overrides/{key}")).status_code == 404
    assert (await client.get("/company-overrides")).json()["total"] == 0


async def test_override_validation(as_user) -> None:
    client, _ = as_user
    assert (await client.put("/company-overrides/acme", json={"hiring_entity": "Vendor"})).status_code == 422
    assert (await client.put("/company-overrides/acme", json={"company_size": "huge"})).status_code == 422
    assert (await client.put("/company-overrides/acme", json={"domain": "x" * 51})).status_code == 422
    assert (await client.get("/company-overrides", params={"source": "robot"})).status_code == 422


async def test_admin_put_replaces_llm_row(as_user, db_session) -> None:
    from app.services.business_filter_service import CompanyOverride
    from app.services.company_classification_service import save_llm_overrides

    await save_llm_overrides(db_session, [CompanyOverride("zeta works", "Zeta Works",
                                                          hiring_entity="Staffing Firm", source="llm")])
    await db_session.commit()
    client, _ = as_user
    listed = (await client.get("/company-overrides", params={"source": "llm"})).json()
    assert listed["items"][0]["source"] == "llm"
    resp = await client.put("/company-overrides/zeta works", json={"hiring_entity": "Direct Client"})
    assert resp.json()["source"] == "manual" and resp.json()["company_name"] == "Zeta Works"


# ── Keywords ─────────────────────────────────────────────────────────────────

async def test_admin_keyword_crud(as_user) -> None:
    client, _ = as_user
    resp = await client.post("/classification-keywords", json={"list": "staffing", "term": "  Body   SHOP "})
    assert resp.status_code == 201, resp.text
    kw = resp.json()
    assert (kw["list"], kw["term"], kw["created"]) == ("staffing", "body shop", True)

    dup = await client.post("/classification-keywords", json={"list": "staffing", "term": "body shop"})
    assert dup.json()["created"] is False and dup.json()["id"] == kw["id"]

    lists = (await client.get("/classification-keywords")).json()["lists"]
    assert set(lists) == {"staffing", "direct", "gcc"}
    assert [t["term"] for t in lists["staffing"]] == ["body shop"]

    assert (await client.post("/classification-keywords", json={"list": "other", "term": "x"})).status_code == 422
    assert (await client.post("/classification-keywords", json={"list": "gcc", "term": "--"})).status_code == 422

    assert (await client.delete(f"/classification-keywords/{kw['id']}")).status_code == 200
    assert (await client.delete(f"/classification-keywords/{kw['id']}")).status_code == 404


# ── PATCH /jobs/{id} apply_to_company ───────────────────────────────────────

async def _job(db, company: str) -> ScrapedJobORM:
    run = HarvestRunORM(run_id=f"{RUN_PREFIX}{uuid.uuid4().hex[:12]}")
    db.add(run)
    await db.flush()
    job = ScrapedJobORM(run_id=run.id, source="linkedin", job_title="Data Engineer", company=company,
                        location="Chennai", job_url=f"https://example.com/{uuid.uuid4().hex}")
    db.add(job)
    await db.flush()
    await db.commit()
    return job


async def test_job_edit_apply_to_company_creates_manual_override(as_user, db_session) -> None:
    client, set_user = as_user
    set_user(LEAD)  # business leads may do this through the job edit path
    company = _uniq("Kite Consulting")
    job = await _job(db_session, company)
    resp = await client.patch(f"/jobs/{job.id}", json={
        "hiring_entity": "Direct Client", "company_size": "11-50 employees",
        "job_poster_name": "Asha", "apply_to_company": True})
    assert resp.status_code == 200, resp.text
    ov = resp.json()["company_override"]
    assert ov["source"] == "manual" and ov["updated_by_email"] == LEAD.email
    assert (ov["hiring_entity"], ov["company_size"], ov["domain"]) == (
        "Direct Client", "11-50 employees", None)

    row = (await db_session.execute(select(CompanyOverrideORM).where(
        CompanyOverrideORM.company_key == ov["company_key"]))).scalars().one()
    assert row.company_name == company

    # A later edit of another field merges into the same row.
    resp = await client.patch(f"/jobs/{job.id}", json={"domain": "Finance", "apply_to_company": True})
    ov = resp.json()["company_override"]
    assert (ov["hiring_entity"], ov["company_size"], ov["domain"]) == (
        "Direct Client", "11-50 employees", "Finance")


async def test_job_edit_without_apply_to_company_saves_no_override(as_user, db_session) -> None:
    client, _ = as_user
    job = await _job(db_session, _uniq("Lumen Consulting"))
    resp = await client.patch(f"/jobs/{job.id}", json={"hiring_entity": "GCC"})
    assert resp.status_code == 200
    assert resp.json()["company_override"] is None
    assert (await client.get("/company-overrides")).json()["total"] == 0
