"""
Tests for the phone-call fixes:

  1. phone insights attribute each call to the number it was made on (phone key);
  2. _upsert_flag is race-safe (INSERT … ON CONFLICT DO NOTHING, then update);
  3. do-not-call email suppressions are tenant-scoped, unsubscribes/bounces global;
  4. clear_calls / follow-up closing match the number by phone key;
  5. update_call reverses the previous outcome's flag effects (unless still justified);
  6. admin "Remove flag" (pvs.unflag_phone / DELETE /phone-verifications/flags).

The in-memory engine is shared by the whole session and log_call commits, so every
test uses its own company name, phone number and email.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.tenant_context import get_current_tenant_id, set_current_tenant
from app.models.auth import AuthenticatedUser
from app.models.harvest_run import HarvestRunORM, ScrapedJobORM
from app.models.phone_call_log import PhoneCallLogORM
from app.models.phone_contact_flag import PhoneContactFlagORM
from app.models.suppression import EmailSuppressionORM
from app.services import phone_verification_service as pvs
from app.services import suppression_service as sup
from app.services.phone_insights_service import compute_phone_outreach_insights

LEAD = AuthenticatedUser(id="u-lead", email="lead.one@example.com", is_active=True,
                         is_verified=True, role="business_lead")
ADMIN = AuthenticatedUser(id="u-admin", email="admin@example.com", is_active=True,
                          is_verified=True, role="admin")


def _uniq() -> str:
    return uuid.uuid4().hex[:10]


def _phone() -> str:
    """A fresh 10-digit number (unique per test) with a +91 prefix."""
    return "+91 9" + str(uuid.uuid4().int)[:9]


def _email() -> str:
    return f"poc.{_uniq()}@acme.test"


async def _run(db) -> str:
    run = HarvestRunORM(id=str(uuid.uuid4()), run_id=f"r_{_uniq()}", status="completed")
    db.add(run)
    await db.flush()
    return run.id


async def _job(db, run_id: str, *, phone: str = "", email: str = "", company_phone: str = "",
               tenant: str = "internal") -> ScrapedJobORM:
    job = ScrapedJobORM(
        id=str(uuid.uuid4()), tenant_id=tenant, run_id=run_id, source="LinkedIn",
        job_title="Engineer", company=f"Co {_uniq()}", location="Chennai",
        job_url=f"https://x/{_uniq()}", contact_number=phone or None, email_id=email or None,
        company_contact_phone=company_phone or None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(job)
    await db.commit()
    return job


async def _log(db, job, *, phone=None, status="answered", outcome="connect_back",
               details=None, **kw) -> PhoneCallLogORM:
    return await pvs.log_call(db, job, LEAD, phone=phone or job.contact_number or "",
                              status=status, outcome=outcome, details=details or {}, **kw)


async def _flag(db, phone: str, tenant: str = "internal") -> PhoneContactFlagORM | None:
    return (await db.execute(select(PhoneContactFlagORM).where(
        PhoneContactFlagORM.tenant_id == tenant,
        PhoneContactFlagORM.phone_key == pvs.normalize_phone(phone),
    ).execution_options(populate_existing=True))).scalar_one_or_none()


async def _supps(db, email: str) -> list[EmailSuppressionORM]:
    return list((await db.execute(select(EmailSuppressionORM).where(
        EmailSuppressionORM.email == email,
    ).execution_options(populate_existing=True))).scalars().all())


async def _edit(db, row, *, status="answered", outcome, details=None):
    return await pvs.update_call(db, row.id, LEAD, status=status, outcome=outcome,
                                 details=details or {})


class _tenant:
    """Run a block under a given current tenant (restored afterwards)."""

    def __init__(self, tid: str) -> None:
        self.tid, self.prev = tid, None

    def __enter__(self):
        self.prev = get_current_tenant_id()
        set_current_tenant(self.tid)

    def __exit__(self, *exc):
        set_current_tenant(self.prev)


# ── 1. Insights: calls attributed by the call's own number ────────────────────

async def test_insights_attribute_call_to_the_number_dialed(db_session):
    run = await _run(db_session)
    p1, p2 = _phone(), _phone()
    job = await _job(db_session, run, phone=p1, company_phone=p2)
    # Only the job's SECOND number is called — and answered.
    await _log(db_session, job, phone=p2.replace(" ", ""), outcome="good_response")
    ins = await compute_phone_outreach_insights(db_session, run, "internal")
    assert ins["total_phones"] == 2
    assert ins["called"] == 1 and ins["answered"] == 1
    posting = next(s for s in ins["sources"] if s["label"] == "Job Posting")
    assert posting["count"] == 1 and posting["called"] == 0  # p1 (job's own) not called


async def test_insights_count_each_number_of_a_multi_number_job(db_session):
    run = await _run(db_session)
    p1, p2 = _phone(), _phone()
    job = await _job(db_session, run, phone=p1, company_phone=p2)
    await _log(db_session, job, phone=p1, status="answered", outcome="good_response")
    await _log(db_session, job, phone=p2, status="unanswered", outcome="busy")
    ins = await compute_phone_outreach_insights(db_session, run, "internal")
    assert (ins["called"], ins["answered"], ins["unanswered"], ins["not_called"]) == (2, 1, 1, 0)


# ── 2. _upsert_flag race safety ───────────────────────────────────────────────

async def test_upsert_flag_with_row_already_inserted_by_a_concurrent_call(db_session):
    """The flag row appears between "no row yet" and our insert (another first call
    on the number): ON CONFLICT DO NOTHING lands on that row — no IntegrityError —
    and set-once stamps keep the other call's time."""
    run = await _run(db_session)
    phone = _phone()
    job = await _job(db_session, run, phone=phone)
    earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db_session.add(PhoneContactFlagORM(tenant_id="internal", phone_key=pvs.normalize_phone(phone),
                                       phone=phone, correct_number="", dnc_at=earlier,
                                       last_outcome="do_not_call", last_call_id=None))
    await db_session.commit()

    row = await _log(db_session, job, outcome="wrong_number",
                     details={"markInvalid": True, "correctNumber": "98400 11111"})
    flag = await _flag(db_session, phone)
    assert pvs._aware(flag.dnc_at) == earlier  # set once — untouched
    assert flag.invalid_at is not None
    assert flag.correct_number == "98400 11111"
    assert (flag.last_outcome, flag.last_call_id) == ("wrong_number", row.id)
    assert flag.updated_by_email == LEAD.email
    rows = (await db_session.execute(select(PhoneContactFlagORM).where(
        PhoneContactFlagORM.phone_key == pvs.normalize_phone(phone)))).scalars().all()
    assert len(rows) == 1


async def test_upsert_flag_called_twice_in_one_transaction(db_session):
    run = await _run(db_session)
    phone = _phone()
    job = await _job(db_session, run, phone=phone)
    r1 = await _log(db_session, job, outcome="good_response")
    r2 = await _log(db_session, job, phone=phone.replace(" ", ""), outcome="do_not_call")
    flag = await _flag(db_session, phone)
    assert flag.warm_at is not None and flag.dnc_at is not None
    assert flag.last_call_id == r2.id != r1.id


# ── 3. Tenant-scoped do-not-call suppressions ─────────────────────────────────

async def test_dnc_suppression_is_tenant_scoped(db_session):
    run = await _run(db_session)
    email = _email()
    job = await _job(db_session, run, phone=_phone(), email=email, tenant="client_us")
    await _log(db_session, job, outcome="do_not_call", details={"dncConfirm": True},
               phone_source="job")
    rows = await _supps(db_session, email)
    assert [(r.tenant_id, r.reason) for r in rows] == [("client_us", "do_not_call")]
    assert await sup.is_suppressed(db_session, email, "client_us")
    assert not await sup.is_suppressed(db_session, email, "client_in")
    assert await sup.is_suppressed(db_session, email, "internal")  # all-access honours all
    with _tenant("client_in"):
        assert not await sup.is_suppressed(db_session, email)
    with _tenant("client_us"):
        assert await sup.is_suppressed(db_session, email)


async def test_two_tenants_can_each_hold_a_dnc_for_one_address(db_session):
    email = _email()
    assert await sup.suppress_do_not_call(db_session, email=email, tenant_id="client_us")
    assert not await sup.suppress_do_not_call(db_session, email=email, tenant_id="client_us")
    assert await sup.suppress_do_not_call(db_session, email=email, tenant_id="client_in")
    await db_session.commit()
    assert {r.tenant_id for r in await _supps(db_session, email)} == {"client_us", "client_in"}


async def test_bounce_and_unsubscribe_stay_global_even_after_a_tenant_dnc(db_session):
    email = _email()
    assert await sup.suppress_do_not_call(db_session, email=email, tenant_id="client_us")
    await db_session.commit()
    assert not await sup.is_suppressed(db_session, email, "client_in")
    # A hard bounce still lands a global row (the DNC row doesn't block it).
    assert await sup.suppress_bounced(db_session, email=email, source="mailjet_webhook")
    await db_session.commit()
    assert await sup.is_suppressed(db_session, email, "client_in")
    # An unsubscribe after a global bounce is a no-op (already globally suppressed).
    assert not await sup.add_suppression(db_session, email=email, source="self_link")
    await db_session.commit()

    other = _email()
    assert await sup.add_suppression(db_session, email=other, source="self_link")
    await db_session.commit()
    for tid in ("client_us", "client_in", "internal"):
        assert await sup.is_suppressed(db_session, other, tid)
    # A tenant DNC on an already globally-suppressed address adds nothing.
    assert not await sup.suppress_do_not_call(db_session, email=other, tenant_id="client_us")


# ── 4. Clear status / follow-ups by phone key ─────────────────────────────────

async def test_clear_calls_matches_number_by_phone_key(db_session):
    run = await _run(db_session)
    phone, other = _phone(), _phone()
    job = await _job(db_session, run, phone=phone)
    await _log(db_session, job, phone=phone, outcome="connect_back")
    await _log(db_session, job, phone=other, outcome="connect_back")
    digits = pvs.normalize_phone(phone)
    assert await pvs.clear_calls(db_session, job.id, f"0{digits[:5]}-{digits[5:]}") == 1
    left = (await db_session.execute(select(PhoneCallLogORM).where(
        PhoneCallLogORM.job_id == job.id, PhoneCallLogORM.cleared_at.is_(None),
    ).execution_options(populate_existing=True))).scalars().all()
    assert [r.phone for r in left] == [other]


async def test_clear_calls_blank_phone_clears_every_number(db_session):
    run = await _run(db_session)
    job = await _job(db_session, run, phone=_phone())
    await _log(db_session, job, outcome="connect_back")
    await _log(db_session, job, phone=_phone(), outcome="connect_back")
    assert await pvs.clear_calls(db_session, job.id, "") == 2


async def test_new_call_closes_followup_logged_with_other_formatting(db_session):
    run = await _run(db_session)
    phone = _phone()
    job = await _job(db_session, run, phone=phone)
    first = await _log(db_session, job, phone=phone, status="unanswered", outcome="busy",
                       reminder_at=datetime.now(timezone.utc) + timedelta(hours=2))
    assert first.follow_up_due_at is not None and first.follow_up_done_at is None
    await _log(db_session, job, phone=pvs.normalize_phone(phone), outcome="connect_back")
    first = await db_session.get(PhoneCallLogORM, first.id, populate_existing=True)
    assert first.follow_up_done_at is not None


async def test_clear_calls_closes_followups_by_phone_key(db_session):
    run = await _run(db_session)
    phone = _phone()
    job = await _job(db_session, run, phone=phone)
    row = await _log(db_session, job, phone=phone, status="unanswered", outcome="busy",
                     reminder_at=datetime.now(timezone.utc) + timedelta(hours=2))
    assert await pvs.clear_calls(db_session, job.id, pvs.normalize_phone(phone)) == 1
    row = await db_session.get(PhoneCallLogORM, row.id, populate_existing=True)
    assert row.cleared_at is not None and row.follow_up_done_at is not None


# ── 5. update_call reverses the previous outcome's effects ────────────────────

async def test_edit_away_from_good_response_clears_warm(db_session):
    run = await _run(db_session)
    phone = _phone()
    job = await _job(db_session, run, phone=phone)
    row = await _log(db_session, job, outcome="good_response")
    assert (await _flag(db_session, phone)).warm_at is not None
    await _edit(db_session, row, outcome="not_interested")
    flag = await _flag(db_session, phone)
    assert flag.warm_at is None and flag.last_outcome == "not_interested"


async def test_edit_keeps_stamp_another_call_still_justifies(db_session):
    run = await _run(db_session)
    phone = _phone()
    job = await _job(db_session, run, phone=phone)
    await _log(db_session, job, outcome="good_response")
    second = await _log(db_session, job, outcome="good_response")
    await _edit(db_session, second, outcome="connect_back")
    assert (await _flag(db_session, phone)).warm_at is not None


async def test_edit_away_from_invalid_number_clears_invalid_reenrich_and_correct_number(db_session):
    run = await _run(db_session)
    phone = _phone()
    job = await _job(db_session, run, phone=phone)
    row = await _log(db_session, job, status="unanswered", outcome="invalid_number",
                     details={"markInvalid": True, "reenrich": True})
    flag = await _flag(db_session, phone)
    assert flag.invalid_at is not None and flag.reenrich_requested_at is not None
    await _edit(db_session, row, status="unanswered", outcome="busy")
    flag = await _flag(db_session, phone)
    assert flag.invalid_at is None and flag.reenrich_requested_at is None

    row2 = await _log(db_session, job, outcome="wrong_number",
                      details={"markInvalid": True, "correctNumber": "98400 22222"})
    assert (await _flag(db_session, phone)).correct_number == "98400 22222"
    await _edit(db_session, row2, outcome="connect_back")
    flag = await _flag(db_session, phone)
    assert flag.invalid_at is None and flag.correct_number == ""


async def test_switch_between_invalid_outcomes_keeps_original_stamp_time(db_session):
    run = await _run(db_session)
    phone = _phone()
    job = await _job(db_session, run, phone=phone)
    row = await _log(db_session, job, outcome="wrong_number", details={"markInvalid": True})
    first_at = (await _flag(db_session, phone)).invalid_at
    await _edit(db_session, row, status="unanswered", outcome="invalid_number",
                details={"markInvalid": True})
    assert (await _flag(db_session, phone)).invalid_at == first_at


async def test_edit_away_from_dnc_clears_flag_and_lifts_its_suppression(db_session):
    run = await _run(db_session)
    phone, email = _phone(), _email()
    job = await _job(db_session, run, phone=phone, email=email)
    row = await _log(db_session, job, outcome="do_not_call", details={"dncConfirm": True},
                     phone_source="job")
    assert (await _flag(db_session, phone)).dnc_at is not None
    assert len(await _supps(db_session, email)) == 1
    await _edit(db_session, row, outcome="not_interested")
    assert (await _flag(db_session, phone)).dnc_at is None
    assert await _supps(db_session, email) == []


async def test_unconfirming_dnc_lifts_suppression_but_keeps_flag(db_session):
    run = await _run(db_session)
    phone, email = _phone(), _email()
    job = await _job(db_session, run, phone=phone, email=email)
    row = await _log(db_session, job, outcome="do_not_call", details={"dncConfirm": True},
                     phone_source="job")
    await _edit(db_session, row, outcome="do_not_call", details={"dncConfirm": False})
    assert (await _flag(db_session, phone)).dnc_at is not None
    assert await _supps(db_session, email) == []


async def test_dnc_suppression_handed_to_another_confirmed_call(db_session):
    run = await _run(db_session)
    phone, email = _phone(), _email()
    job = await _job(db_session, run, phone=phone, email=email)
    first = await _log(db_session, job, outcome="do_not_call", details={"dncConfirm": True},
                       phone_source="job")
    second = await _log(db_session, job, outcome="do_not_call", details={"dncConfirm": True},
                        phone_source="job")
    await _edit(db_session, first, outcome="connect_back")
    assert (await _flag(db_session, phone)).dnc_at is not None  # second still justifies
    rows = await _supps(db_session, email)
    assert len(rows) == 1 and rows[0].raw_payload["call_id"] == second.id


async def test_edit_onto_dnc_still_applies_effects(db_session):
    run = await _run(db_session)
    phone, email = _phone(), _email()
    job = await _job(db_session, run, phone=phone, email=email)
    row = await _log(db_session, job, outcome="connect_back", phone_source="job")
    await _edit(db_session, row, outcome="do_not_call", details={"dncConfirm": True})
    assert (await _flag(db_session, phone)).dnc_at is not None
    rows = await _supps(db_session, email)
    assert len(rows) == 1 and rows[0].raw_payload["call_id"] == row.id


# ── 6. Admin "Remove flag" ────────────────────────────────────────────────────

async def test_unflag_dnc_clears_stamp_and_suppression(db_session):
    run = await _run(db_session)
    phone, email = _phone(), _email()
    job = await _job(db_session, run, phone=phone, email=email)
    await _log(db_session, job, outcome="do_not_call", details={"dncConfirm": True},
               phone_source="job")
    out = await pvs.unflag_phone(db_session, phone.replace(" ", ""), "dnc", ADMIN)
    assert out["cleared"] == 1 and out["suppressions_removed"] == 1
    flag = await _flag(db_session, phone)
    assert flag.dnc_at is None and flag.updated_by_email == ADMIN.email
    assert await _supps(db_session, email) == []


@pytest.mark.parametrize("name,stamp,outcome,status,details", [
    ("warm", "warm_at", "good_response", "answered", {}),
    ("invalid", "invalid_at", "wrong_number", "answered", {"markInvalid": True}),
    ("reenrich", "reenrich_requested_at", "invalid_number", "unanswered",
     {"markInvalid": True, "reenrich": True}),
])
async def test_unflag_other_stamps(db_session, name, stamp, outcome, status, details):
    run = await _run(db_session)
    phone = _phone()
    job = await _job(db_session, run, phone=phone)
    await _log(db_session, job, status=status, outcome=outcome, details=details)
    assert getattr(await _flag(db_session, phone), stamp) is not None
    out = await pvs.unflag_phone(db_session, phone, name, ADMIN)
    assert out["cleared"] == 1
    assert getattr(await _flag(db_session, phone), stamp) is None


async def test_unflag_rejects_bad_input(db_session):
    with pytest.raises(HTTPException) as e:
        await pvs.unflag_phone(db_session, "n/a", "dnc", ADMIN)
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        await pvs.unflag_phone(db_session, _phone(), "bogus", ADMIN)
    assert e.value.status_code == 422


async def test_unflag_route_is_admin_only(db_session):
    from app.core.dependencies import get_current_user, get_db_session
    from app.main import create_app

    run = await _run(db_session)
    phone = _phone()
    job = await _job(db_session, run, phone=phone)
    await _log(db_session, job, outcome="good_response")

    app = create_app()
    current = {"user": LEAD}
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.delete("/phone-verifications/flags", params={"phone": phone, "flag": "warm"})
        assert r.status_code == 403
        current["user"] = ADMIN
        r = await c.delete("/phone-verifications/flags", params={"phone": phone, "flag": "warm"})
        assert r.status_code == 200, r.text
        assert r.json()["cleared"] == 1
    assert (await _flag(db_session, phone)).warm_at is None
    # The table's flags lookup no longer lists the number.
    assert phone not in await pvs.flags_for_phones(db_session, [phone])
