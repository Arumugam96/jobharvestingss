"""Tests for the company+location HR-contact discovery stack:
  * ApolloClient.search_organization / search_people / match_person_by_id payloads
  * apollo_enrichment.apollo_company_contact_fallback gating + reveal-until-first-email
  * company_location_contact_service cache CRUD + the discovery pass grouping/caching
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services.apollo_client import ApolloClient, ApolloOrgResult, ApolloPersonResult


# ── Fakes ─────────────────────────────────────────────────────────────────────

def _fake_settings(**over):
    base = dict(
        apollo_api_key="x",
        apollo_base_url="https://api.apollo.io/api/v1",
        apollo_timeout_s=5.0,
        apollo_webhook_url="",
        apollo_recheck_days=30,
        company_contact_reveal_cap=5,
        company_contact_fallback=True,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _person(**kw) -> ApolloPersonResult:
    return ApolloPersonResult(matched=True, **kw)


# ══════════════════════════════════════════════════════════════════════════════
# ApolloClient new methods
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_search_organization_prefers_domain_match(monkeypatch) -> None:
    client = ApolloClient(_fake_settings())
    calls = {}

    async def _fake_request(path, *, params=None, json=None):
        calls["path"] = path
        calls["json"] = json
        return {"organizations": [
            {"id": "o1", "name": "Acme", "primary_domain": "acme.com"},
            {"id": "o2", "name": "Acme Other", "primary_domain": "other.com"},
        ]}

    monkeypatch.setattr(client, "_request", _fake_request)

    org = await client.search_organization("Acme", domain="other.com")
    assert org is not None and org.id == "o2"
    assert calls["path"] == "/mixed_companies/search"
    assert calls["json"]["q_organization_name"] == "Acme"


@pytest.mark.asyncio
async def test_search_people_sends_titles_and_locations(monkeypatch) -> None:
    client = ApolloClient(_fake_settings())
    calls = {}

    async def _fake_request(path, *, params=None, json=None):
        calls["path"] = path
        calls["json"] = json
        return {"people": [{"id": "p1", "name": "A", "title": "HR Manager"}]}

    monkeypatch.setattr(client, "_request", _fake_request)

    people = await client.search_people(["o1"], ["HR Manager"], person_locations=["India"])
    assert len(people) == 1 and people[0].id == "p1"
    assert calls["path"] == "/mixed_people/api_search"
    assert calls["json"]["organization_ids"] == ["o1"]
    assert calls["json"]["person_titles"] == ["HR Manager"]
    assert calls["json"]["person_locations"] == ["India"]


@pytest.mark.asyncio
async def test_match_person_by_id_posts_id_in_body(monkeypatch) -> None:
    client = ApolloClient(_fake_settings())
    calls = {}

    async def _fake_request(path, *, params=None, json=None):
        calls["path"] = path
        calls["params"] = params
        calls["json"] = json
        return {"person": {"id": "p1", "name": "A", "email": "a@acme.com"}}

    async def _always(*a, **k):
        return True

    monkeypatch.setattr(client, "_request", _fake_request)
    monkeypatch.setattr(client, "_reserve_credit", _always)

    res = await client.match_person_by_id("p1", reveal_email=True)
    assert res.email == "a@acme.com"
    assert calls["json"] == {"id": "p1"}
    assert "reveal_personal_emails" in calls["params"]


@pytest.mark.asyncio
async def test_reveal_methods_short_circuit_when_capped(monkeypatch) -> None:
    """Only the credit-spending reveal calls are gated by the daily cap: when it is
    reached they short-circuit to an empty result without issuing the request."""
    client = ApolloClient(_fake_settings())

    async def _denied(*a, **k):
        return False

    async def _boom(*a, **k):  # must never be reached once the cap denies a reveal
        raise AssertionError("_request should not run when the cap is reached")

    monkeypatch.setattr(client, "_reserve_credit", _denied)
    monkeypatch.setattr(client, "_request", _boom)

    assert (await client.match_person_by_id("p1")).matched is False
    assert (await client.enrich_person_by_linkedin("https://www.linkedin.com/in/x")).matched is False


@pytest.mark.asyncio
async def test_free_lookups_ignore_the_credit_cap(monkeypatch) -> None:
    """Search/enrich calls reveal no contact and cost no Apollo credit, so they must
    run even when the daily cap is exhausted — they never reserve against it."""
    client = ApolloClient(_fake_settings())

    async def _denied(*a, **k):  # cap exhausted — must be irrelevant to free lookups
        return False

    async def _fake_request(path, *, params=None, json=None):
        if path == "/mixed_companies/search":
            return {"organizations": [{"id": "o1", "name": "Acme", "primary_domain": "acme.com"}]}
        if path == "/mixed_people/api_search":
            return {"people": [{"id": "p1", "name": "A", "title": "HR"}]}
        if path == "/organizations/enrich":
            return {"organization": {"id": "o1", "name": "Acme", "primary_domain": "acme.com"}}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(client, "_reserve_credit", _denied)
    monkeypatch.setattr(client, "_request", _fake_request)

    org = await client.search_organization("Acme")
    assert org is not None and org.id == "o1"
    people = await client.search_people(["o1"], ["HR"])
    assert len(people) == 1 and people[0].id == "p1"
    org2 = await client.enrich_organization("acme.com")
    assert org2 is not None and org2.id == "o1"


# ══════════════════════════════════════════════════════════════════════════════
# apollo_company_contact_fallback
# ══════════════════════════════════════════════════════════════════════════════

class _FakeApollo:
    """Configurable fake ApolloClient for the fallback tests."""

    def __init__(self, org=None, people=None, reveals=None):
        self._org = org
        self._people = people or []
        self._reveals = reveals or {}   # person_id -> ApolloPersonResult
        self.reveal_calls = []

    async def enrich_organization(self, domain):
        return self._org

    async def search_organization(self, company_name, domain=""):
        return self._org

    async def search_people(self, org_ids, titles, person_locations=None):
        return self._people

    async def match_person_by_id(self, person_id, reveal_email=True, reveal_phone=False):
        self.reveal_calls.append(person_id)
        return self._reveals.get(person_id, ApolloPersonResult.no_match())


def _unlimited_budget(monkeypatch):
    async def _remaining(*a, **k):
        return None
    monkeypatch.setattr("app.services.apollo_budget.remaining_today", _remaining)


@pytest.mark.asyncio
async def test_fallback_skips_without_api_key(monkeypatch) -> None:
    from app.services.apollo_enrichment import apollo_company_contact_fallback
    res = await apollo_company_contact_fallback(
        settings=_fake_settings(apollo_api_key=""),
        company_name="Acme", location_terms="India",
    )
    assert res.attempted is False and res.email == "" and res.cap_reached is False


@pytest.mark.asyncio
async def test_fallback_requires_location_terms(monkeypatch) -> None:
    from app.services.apollo_enrichment import apollo_company_contact_fallback
    res = await apollo_company_contact_fallback(
        settings=_fake_settings(), company_name="Acme", location_terms="",
    )
    assert res.attempted is False  # never an unconstrained, company-wide search


@pytest.mark.asyncio
async def test_fallback_honours_cooldown(monkeypatch) -> None:
    from app.services.apollo_enrichment import apollo_company_contact_fallback
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    res = await apollo_company_contact_fallback(
        settings=_fake_settings(apollo_recheck_days=30),
        company_name="Acme", location_terms="India", attempted_at=recent,
    )
    assert res.attempted is False


@pytest.mark.asyncio
async def test_fallback_reveals_until_first_email(monkeypatch) -> None:
    from app.services.apollo_enrichment import apollo_company_contact_fallback
    _unlimited_budget(monkeypatch)
    org = ApolloOrgResult(id="o1", name="Acme", domain="acme.com")
    people = [ApolloPersonResult(matched=True, id="p1", name="A", title="HR"),
              ApolloPersonResult(matched=True, id="p2", name="B", title="Recruiter")]
    reveals = {
        "p1": ApolloPersonResult(matched=True, id="p1"),                       # no email
        "p2": ApolloPersonResult(matched=True, id="p2", name="B", email="b@acme.com"),
    }
    fake = _FakeApollo(org=org, people=people, reveals=reveals)

    res = await apollo_company_contact_fallback(
        settings=_fake_settings(), company_name="Acme", location_terms="India",
        domain="acme.com", client=fake,
    )
    assert res.email == "b@acme.com"
    assert res.name == "B"
    assert res.matched is True and res.attempted is True
    assert fake.reveal_calls == ["p1", "p2"]  # stopped at the first hit


@pytest.mark.asyncio
async def test_fallback_no_email_is_cacheable_negative(monkeypatch) -> None:
    from app.services.apollo_enrichment import apollo_company_contact_fallback
    _unlimited_budget(monkeypatch)
    org = ApolloOrgResult(id="o1", name="Acme", domain="acme.com")
    people = [ApolloPersonResult(matched=True, id="p1", name="A", title="HR")]
    fake = _FakeApollo(org=org, people=people, reveals={"p1": ApolloPersonResult.no_match()})

    res = await apollo_company_contact_fallback(
        settings=_fake_settings(), company_name="Acme", location_terms="India", client=fake,
    )
    assert res.email == "" and res.attempted is True and res.cap_reached is False


@pytest.mark.asyncio
async def test_fallback_cap_reached_is_not_a_negative(monkeypatch) -> None:
    from app.services.apollo_enrichment import apollo_company_contact_fallback

    async def _zero(*a, **k):
        return 0
    monkeypatch.setattr("app.services.apollo_budget.remaining_today", _zero)

    fake = _FakeApollo(org=ApolloOrgResult(id="o1"))
    res = await apollo_company_contact_fallback(
        settings=_fake_settings(), company_name="Acme", location_terms="India", client=fake,
    )
    assert res.cap_reached is True and res.attempted is False
    assert fake.reveal_calls == []  # never spent a reveal


# ══════════════════════════════════════════════════════════════════════════════
# Cache CRUD + discovery pass
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cache_upsert_and_needs_search(db_session) -> None:
    from app.services.company_location_contact_service import (
        get_location_contacts,
        location_contact_needs_search,
        upsert_location_contact,
    )
    assert location_contact_needs_search(None, 30) is True

    await upsert_location_contact(
        db_session, company_key="crudco", location_key="bengaluru|karnataka|india",
        company_name="CrudCo", location="Bengaluru, Karnataka, India", domain="crudco.com",
        hr_contact_name="A", hr_contact_title="HR", hr_contact_email="a@crudco.com",
        hr_contact_phone="", attempted=True,
    )
    # Flush (not commit) so this row rolls back after the test — the engine is
    # session-scoped and shared, so committing would leak into other tests.
    await db_session.flush()

    rows = await get_location_contacts(db_session, {("crudco", "bengaluru|karnataka|india")})
    row = rows[("crudco", "bengaluru|karnataka|india")]
    assert row.hr_contact_email == "a@crudco.com"
    # Has an email => never re-search.
    assert location_contact_needs_search(row, 30) is False


@pytest.mark.asyncio
async def test_cache_negative_respects_cooldown(db_session) -> None:
    from app.services.company_location_contact_service import (
        get_location_contacts,
        location_contact_needs_search,
        upsert_location_contact,
    )
    await upsert_location_contact(
        db_session, company_key="crudbeta", location_key="chennai|tamil nadu|india",
        company_name="CrudBeta", location="Chennai, Tamil Nadu, India", domain="",
        hr_contact_name="", hr_contact_title="", hr_contact_email="",
        hr_contact_phone="", attempted=True,
    )
    await db_session.flush()  # roll back after test (shared session-scoped engine)
    row = (await get_location_contacts(db_session, {("crudbeta", "chennai|tamil nadu|india")}))[
        ("crudbeta", "chennai|tamil nadu|india")
    ]
    # Freshly attempted negative -> within cooldown -> don't re-search.
    assert location_contact_needs_search(row, 30) is False
    # ...but eligible again once the cooldown has elapsed.
    assert location_contact_needs_search(row, 0) is True


@pytest.mark.asyncio
async def test_discovery_searches_per_company_location(engine, monkeypatch) -> None:
    import app.services.company_location_contact_service as svc
    from app.services.apollo_enrichment import CompanyContactResult

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(svc, "get_session_factory", lambda settings: factory)

    async def _unlimited(*a, **k):
        return None
    monkeypatch.setattr("app.services.apollo_budget.remaining_today", _unlimited)

    seen_locations = []

    async def _fake_fallback(*, settings, company_name, location_terms, domain="",
                             attempted_at=None, **kw):
        seen_locations.append((company_name, location_terms))
        return CompanyContactResult(
            name="HR " + location_terms, title="Recruiter",
            email=f"hr+{len(seen_locations)}@acme.com", matched=True, attempted=True,
        )
    monkeypatch.setattr(svc, "apollo_company_contact_fallback", _fake_fallback)

    def _job(company, location):
        return types.SimpleNamespace(
            company=company, location=location,
            job_poster_name=None, job_poster_email="", job_poster_company="",
        )

    # Unique company — the service COMMITS cache rows (it manages its own session),
    # and the engine is session-scoped/shared, so a distinct name avoids collisions.
    jobs = [
        _job("ZetaLabs", "Bengaluru, Karnataka, India"),
        _job("ZetaLabs", "Bengaluru, Karnataka, India"),   # same pair -> one search
        _job("ZetaLabs", "Chennai, Tamil Nadu, India"),    # different location -> its own search
        _job("ZetaLabs", "Remote"),                         # no derivable location -> skipped
    ]

    settings = _fake_settings()
    result = await svc.discover_company_location_contacts(jobs, settings)

    assert result["searched"] == 2            # two distinct (company, location) pairs
    assert result["skipped_no_location"] == 1  # the Remote job
    assert {loc for _, loc in seen_locations} == {
        "Bengaluru, Karnataka, India", "Chennai, Tamil Nadu, India",
    }

    # Both pairs are now cached.
    async with factory() as db:
        from app.services.company_location_contact_service import get_location_contacts
        rows = await get_location_contacts(db, {
            ("zetalabs", "bengaluru|karnataka|india"),
            ("zetalabs", "chennai|tamil nadu|india"),
        })
    assert len(rows) == 2
    assert all(r.hr_contact_email for r in rows.values())
