"""Phone call verification ("Verify mobile number" on the Harvested Jobs table).

A user dials a job's phone number and logs the result: Answered or Unanswered,
the call outcome under it (CALL_OUTCOMES), outcome-specific details, proof of
the call and optionally a reminder time. Every log is an append-only
phone_call_logs row (app/models/phone_call_log.py):

  * The newest non-cleared row per job is the badge the whole tenant team sees
    (latest_for_jobs), including who called.
  * A call with a reminder gets follow_up_due_at = the picked time (older
    clients: Unanswered + Follow-up gets now + settings.phone_followup_delay_hours).
    Once that passes it shows in the
    CALLER's notification bell (due_followups) — computed at poll time, so no
    scheduler job is involved. A newer call on the same job+phone, the caller
    dismissing it, or "Clear status" closes it (follow_up_done_at).
  * "Clear status" soft-deletes (cleared_at) the job+phone's rows.

The server re-checks the popup's Save gate (_validate_outcome: the outcome's
required fields, and proof of call where the outcome needs it) and keeps only the
outcome's known detail fields (_clean_details). Some outcomes then act beyond the
log, in the same transaction (_apply_outcome_effects):

  * good_response   — the number is flagged warm (and its recruiter's crm_status).
  * wrong_number / invalid_number — the number is flagged invalid; invalid_number
    can also request re-enrichment (a flag only — no Apollo spend here).
  * do_not_call     — the number is flagged DNC, the contact's email is added to
    email_suppressions (every send path already checks it) and no reminder is kept.
  * not_right_poc   — the referred person becomes a recruiters row.
  * language_barrier + reassign — the caller's assignment on the job is released.

Number-level state lives in phone_contact_flags (one row per tenant + digits), so
it follows the number onto every job that shows it (flags_for_phones).

job_id is a soft link to scraped_jobs.id; a run reconcile that rewrites a run's
rows re-points it via relink_job_ids(). Matching on the caller is
case-insensitive on email, like job_assignment_service.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.tenant_context import apply_tenant, get_current_tenant_id
from app.models.auth import AuthenticatedUser
from app.models.harvest_run import ScrapedJobORM
from app.models.phone_call_log import PhoneCallLogORM
from app.models.phone_contact_flag import PhoneContactFlagORM
from app.models.recruiter import RecruiterORM
from app.models.suppression import EmailSuppressionORM
from app.services import recruiter_service, suppression_service
from app.services.job_assignment_service import display_name_from_email

CALL_STATUSES = ("answered", "unanswered")
# Mirrors OUTCOMES in harvest-agent/src/components/PhoneVerify.jsx.
CALL_OUTCOMES = {
    "answered": frozenset({
        "good_response", "connect_back", "share_details", "not_interested", "wrong_number",
        "not_right_poc", "position_closed", "language_barrier", "do_not_call",
    }),
    "unanswered": frozenset({
        "no_answer", "busy", "switched_off", "unreachable", "invalid_number",
        "disconnected", "voicemail",
    }),
}
DETAILS_MAX_LEN = 4000
PROOF_MAX_FILES = 3
PROOF_SRC_MAX_LEN = 450_000  # a ~300 KB image as a base64 data: URL
REMARKS_MAX_LEN = 2000
PHONE_MAX_LEN = 50
PHONE_SOURCE_MAX_LEN = 20
CONTACT_NAME_MAX_LEN = 255
DETAIL_TEXT_MAX_LEN = 255
DETAIL_LIST_MAX_ITEMS = 10
FLAGS_MAX_PHONES = 200

# Outcome-specific detail fields the popup sends (OutcomePanel in PhoneVerify.jsx),
# by panel. Each value is "str", "list" (of str) or "bool"; anything else is dropped.
_PANEL_FIELDS: dict[str, dict[str, str]] = {
    "callback": {"window": "str"},
    "next": {"nextSteps": "list", "requirement": "str"},
    "email": {"sendTo": "str", "sendWhat": "list"},
    "reason": {"reason": "str", "revisit": "str"},
    "wrong": {"markInvalid": "bool", "correctNumber": "str"},
    "redirect": {"referredName": "str", "referredPhone": "str", "referredTeam": "str"},
    "lang": {"language": "str", "reassign": "bool"},
    "dnc": {"dncConfirm": "bool"},
    "retry": {"preset": "str"},
    "invalid": {"markInvalid": "bool", "reenrich": "bool"},
}
# outcome → its panel (None: no outcome-specific fields).
_OUTCOME_PANEL: dict[str, str | None] = {
    "good_response": "next", "connect_back": "callback", "share_details": "email",
    "not_interested": "reason", "wrong_number": "wrong", "not_right_poc": "redirect",
    "position_closed": None, "language_barrier": "lang", "do_not_call": "dnc",
    "no_answer": "retry", "busy": "retry", "switched_off": "retry", "unreachable": "retry",
    "invalid_number": "invalid", "disconnected": "retry", "voicemail": "retry",
}
# Proof of call: always required for these; from the 2nd attempt on a job for the
# unanswered retry outcomes (mirrors `proof` in PhoneVerify.jsx's OUTCOMES).
PROOF_REQUIRED = frozenset({"wrong_number", "do_not_call", "invalid_number"})
PROOF_FROM_SECOND = frozenset({
    "no_answer", "busy", "switched_off", "unreachable", "disconnected", "voicemail",
})
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Which job field holds the email of the person whose number the Mobile cell showed
# (phone_source); "recruiter" resolves through recruiters.official_email_id.
_SOURCE_EMAIL_FIELD = {"job": "email_id", "contact": "email_id", "company": "company_contact_email"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite (tests) hands back naive datetimes; everything stored is UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def _same_user(email: str | None, user: AuthenticatedUser) -> bool:
    return bool(email) and email.lower() == (user.email or "").lower()


def _loads(raw: str | None, default):
    try:
        value = json.loads(raw) if raw else default
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def normalize_phone(phone: str | None) -> str:
    """The number's digits only — the phone_contact_flags key."""
    return re.sub(r"\D", "", phone or "")[:50]


def _clean_details(outcome: str, details: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only the detail fields the outcome's panel defines, coerced to their
    type (text capped, lists of text, booleans). Unknown keys are dropped."""
    fields = _PANEL_FIELDS.get(_OUTCOME_PANEL.get(outcome) or "", {})
    details = details or {}
    clean: dict[str, Any] = {}
    for key, kind in fields.items():
        if key not in details:
            continue
        value = details[key]
        if kind == "bool":
            clean[key] = bool(value)
        elif kind == "list":
            items = value if isinstance(value, list) else []
            clean[key] = [str(v).strip()[:DETAIL_TEXT_MAX_LEN] for v in items
                          if str(v).strip()][:DETAIL_LIST_MAX_ITEMS]
        elif isinstance(value, (str, int, float)):
            clean[key] = str(value).strip()[:DETAIL_TEXT_MAX_LEN]
    return clean


def _validate_outcome(outcome: str, details: dict[str, Any], proof_count: int,
                      attempt_no: int) -> None:
    """The popup's Save gate, enforced server-side (422 saying what is missing)."""
    msg = ""
    if outcome == "not_interested" and not details.get("reason"):
        msg = "Select a reason"
    elif outcome == "do_not_call" and not details.get("dncConfirm"):
        msg = "Confirm the do-not-call request"
    elif outcome == "good_response" and not details.get("nextSteps"):
        msg = "Pick at least one next step"
    elif outcome == "share_details" and not _EMAIL_RE.match(details.get("sendTo") or ""):
        msg = "Add a valid email to send details to"
    elif outcome == "not_right_poc" and not details.get("referredName"):
        msg = "Add who you were referred to"
    elif not proof_count and (
        outcome in PROOF_REQUIRED or (outcome in PROOF_FROM_SECOND and attempt_no >= 2)
    ):
        msg = "Attach proof of the call to save"
    if msg:
        raise HTTPException(status_code=422, detail=msg)


def call_view(row: PhoneCallLogORM, *, proof_src: bool = True) -> dict[str, Any]:
    """API view of one logged call. proof_src=False drops the inline proof images
    (the table's batch /latest only needs the count)."""
    proof = _loads(row.proof, [])
    if not proof_src:
        proof = [{**p, "src": ""} for p in proof]
    return {
        "id": row.id,
        "job_id": row.job_id,
        "phone": row.phone,
        "phone_source": row.phone_source or "",
        "contact_name": row.contact_name or "",
        "status": row.call_status,
        "outcome": row.call_outcome or "",
        "details": _loads(row.outcome_details, {}),
        "proof": proof,
        "remarks": row.remarks or "",
        "follow_up": bool(row.follow_up),
        "follow_up_due_at": _iso(row.follow_up_due_at),
        "follow_up_done_at": _iso(row.follow_up_done_at),
        "called_by": {
            "email": row.called_by_email,
            "name": row.called_by_name or display_name_from_email(row.called_by_email),
        },
        "called_at": _iso(row.created_at),
    }


def _close_open_followups_stmt(job_id: str, phone: str, now: datetime):
    return apply_tenant(
        update(PhoneCallLogORM)
        .where(
            PhoneCallLogORM.job_id == job_id,
            PhoneCallLogORM.phone == phone,
            PhoneCallLogORM.follow_up_due_at.is_not(None),
            PhoneCallLogORM.follow_up_done_at.is_(None),
        )
        .values(follow_up_done_at=now, updated_at=now),
        PhoneCallLogORM.tenant_id,
    )


async def log_call(
    db: AsyncSession,
    job: ScrapedJobORM,
    user: AuthenticatedUser,
    *,
    phone: str,
    phone_source: str = "",
    contact_name: str = "",
    status: str,
    remarks: str = "",
    follow_up: bool = False,
    outcome: str = "",
    details: dict[str, Any] | None = None,
    proof: list[dict[str, Any]] | None = None,
    reminder_at: datetime | None = None,
) -> PhoneCallLogORM:
    """Record one call against `job`'s `phone`. Supersedes (closes) any open
    follow-up on the same job+phone, then inserts the new row. `reminder_at`
    schedules the follow-up at that time (any status)."""
    call_status = (status or "").strip().lower()
    if call_status not in CALL_STATUSES:
        raise HTTPException(
            status_code=422, detail=f"status must be one of {', '.join(CALL_STATUSES)}"
        )
    phone = (phone or "").strip()[:PHONE_MAX_LEN]
    if not phone:
        raise HTTPException(status_code=422, detail="phone is required")
    remarks = (remarks or "").strip()[:REMARKS_MAX_LEN]
    outcome = (outcome or "").strip().lower()
    if outcome and outcome not in CALL_OUTCOMES[call_status]:
        raise HTTPException(status_code=422, detail=f"Unknown outcome '{outcome}' for a {call_status} call")
    # The outcome records what happened; without one (older clients) an answered
    # call still needs remarks.
    if call_status == "answered" and not outcome and not remarks:
        raise HTTPException(status_code=422, detail="Remarks are required for an answered call")
    # Calls without an outcome (older clients) keep their details as sent.
    details = _clean_details(outcome, details) if outcome else dict(details or {})
    details_json = json.dumps(details, ensure_ascii=False)
    if len(details_json) > DETAILS_MAX_LEN:
        raise HTTPException(status_code=422, detail="Outcome details are too long")
    proof_items = []
    for p in (proof or [])[:PROOF_MAX_FILES]:
        src = str(p.get("src") or "")
        if src and (not src.startswith("data:image/") or len(src) > PROOF_SRC_MAX_LEN):
            src = ""  # keep the file's name, drop anything that isn't a small inline image
        proof_items.append({
            "name": str(p.get("name") or "")[:255],
            "size": str(p.get("size") or "")[:20],
            "type": str(p.get("type") or "")[:40],
            "src": src,
        })
    if outcome:
        attempt_no = (await count_calls(db, [job.id])).get(job.id, 0) + 1
        _validate_outcome(outcome, details, sum(1 for p in proof_items if p["name"]), attempt_no)
    if outcome == "do_not_call":
        reminder_at, follow_up = None, False  # the contact is suppressed — nothing to schedule

    now = _now()
    await db.execute(_close_open_followups_stmt(job.id, phone, now))

    due = None
    if reminder_at is not None:
        due = _aware(reminder_at)
        follow_up = True
    elif call_status == "unanswered" and follow_up:
        due = now + timedelta(hours=get_settings().phone_followup_delay_hours)

    row = PhoneCallLogORM(
        # The job's tenant, not the caller's: an internal (all-access) user
        # logging a call on a client job must not hide it from that client.
        tenant_id=getattr(job, "tenant_id", None) or get_current_tenant_id(),
        job_id=job.id,
        phone=phone,
        phone_source=(phone_source or "").strip()[:PHONE_SOURCE_MAX_LEN],
        contact_name=(contact_name or "").strip()[:CONTACT_NAME_MAX_LEN],
        call_status=call_status,
        call_outcome=outcome,
        outcome_details=details_json,
        proof=json.dumps(proof_items, ensure_ascii=False) if proof_items else "",
        remarks=remarks,
        follow_up=bool(follow_up),
        follow_up_due_at=due,
        called_by_email=user.email or "",
        called_by_name=display_name_from_email(user.email or ""),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    if outcome:
        await db.flush()  # row.id, for the flag's last_call_id
        await _apply_outcome_effects(db, job, user, row, details, now)
    await db.commit()
    await db.refresh(row)
    return row


# ── Outcome side effects ───────────────────────────────────────────────────────

async def _upsert_flag(
    db: AsyncSession, row: PhoneCallLogORM, user: AuthenticatedUser, now: datetime,
    *, stamps: tuple[str, ...] = (), correct_number: str = "",
) -> PhoneContactFlagORM | None:
    """Create or update the called number's flag row. Each stamp (invalid_at,
    dnc_at…) is set once — the first call that established it keeps its time."""
    key = normalize_phone(row.phone)
    if not key:
        return None
    flag = (await db.execute(select(PhoneContactFlagORM).where(
        PhoneContactFlagORM.tenant_id == row.tenant_id, PhoneContactFlagORM.phone_key == key,
    ))).scalar_one_or_none()
    if flag is None:
        flag = PhoneContactFlagORM(tenant_id=row.tenant_id, phone_key=key, phone=row.phone,
                                   correct_number="", created_at=now)
        db.add(flag)
    for stamp in stamps:
        if getattr(flag, stamp) is None:
            setattr(flag, stamp, now)
    if correct_number:
        flag.correct_number = correct_number[:50]
    flag.last_outcome = row.call_outcome
    flag.last_call_id = row.id
    flag.updated_by_email = user.email or ""
    flag.updated_at = now
    return flag


async def _job_recruiter(db: AsyncSession, job: ScrapedJobORM) -> RecruiterORM | None:
    if not job.recruiter_id:
        return None
    return (await db.execute(
        select(RecruiterORM).where(RecruiterORM.id == job.recruiter_id)
    )).scalar_one_or_none()


async def _contact_email_for(db: AsyncSession, job: ScrapedJobORM, phone_source: str) -> str:
    """The email of the person whose number was dialed — the same contact variant
    the Mobile cell took the number from."""
    if phone_source == "recruiter":
        recruiter = await _job_recruiter(db, job)
        return ((recruiter.official_email_id if recruiter else "") or "").strip()
    field = _SOURCE_EMAIL_FIELD.get(phone_source, "email_id")
    return (getattr(job, field, "") or "").strip()


async def _apply_outcome_effects(
    db: AsyncSession, job: ScrapedJobORM, user: AuthenticatedUser,
    row: PhoneCallLogORM, details: dict[str, Any], now: datetime,
) -> None:
    """What an outcome does beyond the log (see the module docstring). Runs inside
    log_call's transaction — nothing here commits."""
    outcome = row.call_outcome

    if outcome == "good_response":
        await _upsert_flag(db, row, user, now, stamps=("warm_at",))
        if row.phone_source == "recruiter":
            recruiter = await _job_recruiter(db, job)
            if recruiter is not None:
                recruiter.crm_status = "Warm"

    elif outcome in ("wrong_number", "invalid_number"):
        stamps: tuple[str, ...] = ("invalid_at",) if details.get("markInvalid", True) else ()
        if outcome == "invalid_number" and details.get("reenrich"):
            stamps += ("reenrich_requested_at",)
        await _upsert_flag(db, row, user, now, stamps=stamps,
                           correct_number=details.get("correctNumber", ""))

    elif outcome == "do_not_call":
        await _upsert_flag(db, row, user, now, stamps=("dnc_at",))
        email = await _contact_email_for(db, job, row.phone_source)
        if email and await suppression_service.suppress_bounced(
            db, email=email, reason="do_not_call", source="phone_dnc",
            recruiter_id=job.recruiter_id if row.phone_source == "recruiter" else None,
            raw_payload={"call_id": row.id, "job_id": job.id, "phone": row.phone,
                         "by": user.email or ""},
            event_time=now,
        ):
            # suppress_bounced leaves the 'internal' server default; own the new row
            # to the job's tenant so that tenant's suppression list shows it.
            for obj in db.new:
                if isinstance(obj, EmailSuppressionORM):
                    obj.tenant_id = row.tenant_id

    elif outcome == "not_right_poc":
        recruiter = await recruiter_service.upsert_recruiter(
            db, person_name=details.get("referredName", ""), company_name=job.company or "",
            designation=details.get("referredTeam", ""), harvest_source="call_referral",
            tenant_id=row.tenant_id,
        )
        if recruiter is not None:
            referred_phone = details.get("referredPhone", "")
            if referred_phone and not recruiter.contact_number:
                # Given on the call — real, but neither scraped nor verified.
                recruiter.contact_number = referred_phone[:50]
                recruiter.phone_status = "REFERRED"
            row.outcome_details = json.dumps(
                {**details, "referredRecruiterId": recruiter.id}, ensure_ascii=False)

    elif outcome == "language_barrier" and details.get("reassign"):
        # Release the job so a teammate who speaks the language can pick it up. Only
        # the caller's own lock (unassign_job's fields, without its separate commit).
        if _same_user(job.assigned_to_email, user):
            job.assigned_to_email = None
            job.assigned_to_name = ""
            job.assigned_at = None
            job.assignment_note = ""


def flag_view(flag: PhoneContactFlagORM | None) -> dict[str, Any]:
    """API view of a number's flags (all off for a number without a row)."""
    return {
        "invalid": bool(flag and flag.invalid_at),
        "dnc": bool(flag and flag.dnc_at),
        "warm": bool(flag and flag.warm_at),
        "reenrich_requested": bool(flag and flag.reenrich_requested_at),
        "correct_number": (flag.correct_number if flag else "") or "",
    }


async def flags_for_phones(db: AsyncSession, phones: list[str]) -> dict[str, dict[str, Any]]:
    """{phone as given: flag_view} for the numbers that carry a flag (tenant-scoped).
    Numbers match on digits, so formatting differences don't matter."""
    by_key: dict[str, list[str]] = {}
    for p in list(dict.fromkeys(p for p in phones if p))[:FLAGS_MAX_PHONES]:
        key = normalize_phone(p)
        if key:
            by_key.setdefault(key, []).append(p)
    if not by_key:
        return {}
    rows = (await db.execute(apply_tenant(
        select(PhoneContactFlagORM).where(PhoneContactFlagORM.phone_key.in_(list(by_key))),
        PhoneContactFlagORM.tenant_id,
    ))).scalars().all()
    out: dict[str, dict[str, Any]] = {}
    for flag in rows:
        view = flag_view(flag)
        if view["invalid"] or view["dnc"] or view["warm"] or view["reenrich_requested"]:
            for p in by_key.get(flag.phone_key, []):
                out[p] = view
    return out


async def clear_calls(db: AsyncSession, job_id: str, phone: str | None = None) -> int:
    """"Clear status": soft-delete the job's active rows for `phone` (all phones
    when blank) and close their open follow-ups. Returns rows cleared."""
    now = _now()
    conds = [PhoneCallLogORM.job_id == job_id, PhoneCallLogORM.cleared_at.is_(None)]
    phone = (phone or "").strip()
    if phone:
        conds.append(PhoneCallLogORM.phone == phone)

    await db.execute(apply_tenant(
        update(PhoneCallLogORM)
        .where(*conds, PhoneCallLogORM.follow_up_done_at.is_(None),
               PhoneCallLogORM.follow_up_due_at.is_not(None))
        .values(follow_up_done_at=now),
        PhoneCallLogORM.tenant_id,
    ))
    result = await db.execute(apply_tenant(
        update(PhoneCallLogORM).where(*conds).values(cleared_at=now, updated_at=now),
        PhoneCallLogORM.tenant_id,
    ))
    await db.commit()
    return result.rowcount or 0


async def latest_for_jobs(db: AsyncSession, job_ids: list[str]) -> dict[str, dict[str, Any]]:
    """{job_id: call_view} — the newest non-cleared call per job (tenant-scoped).
    Ids are autoincrement, so max(id) per job is the newest row."""
    ids = [j for j in dict.fromkeys(job_ids) if j]
    if not ids:
        return {}
    newest = apply_tenant(
        select(func.max(PhoneCallLogORM.id))
        .where(PhoneCallLogORM.job_id.in_(ids), PhoneCallLogORM.cleared_at.is_(None))
        .group_by(PhoneCallLogORM.job_id),
        PhoneCallLogORM.tenant_id,
    )
    rows = (await db.execute(
        select(PhoneCallLogORM).where(PhoneCallLogORM.id.in_(newest.scalar_subquery()))
    )).scalars().all()
    counts = await count_calls(db, [r.job_id for r in rows])
    return {r.job_id: {**call_view(r, proof_src=False), "attempts": counts.get(r.job_id, 1)} for r in rows}


async def count_calls(db: AsyncSession, job_ids: list[str]) -> dict[str, int]:
    """{job_id: number of non-cleared calls} — the "Attempt N" shown on the badge."""
    ids = [j for j in dict.fromkeys(job_ids) if j]
    if not ids:
        return {}
    stmt = apply_tenant(
        select(PhoneCallLogORM.job_id, func.count(PhoneCallLogORM.id))
        .where(PhoneCallLogORM.job_id.in_(ids), PhoneCallLogORM.cleared_at.is_(None))
        .group_by(PhoneCallLogORM.job_id),
        PhoneCallLogORM.tenant_id,
    )
    return {job_id: int(n) for job_id, n in (await db.execute(stmt)).all()}


async def history(db: AsyncSession, job_id: str) -> list[dict[str, Any]]:
    """All non-cleared calls on a job, newest first (tenant-scoped)."""
    stmt = apply_tenant(
        select(PhoneCallLogORM)
        .where(PhoneCallLogORM.job_id == job_id, PhoneCallLogORM.cleared_at.is_(None))
        .order_by(PhoneCallLogORM.created_at.desc(), PhoneCallLogORM.id.desc()),
        PhoneCallLogORM.tenant_id,
    )
    return [call_view(r) for r in (await db.execute(stmt)).scalars().all()]


async def relink_job_ids(db: AsyncSession, mapping: dict[str, str]) -> None:
    """Re-point call logs from old scraped_jobs ids to new ones after a run
    reconcile rewrote the run's rows. Does NOT commit — the caller owns the
    transaction. Not tenant-filtered: job ids are uuids, and the reconcile may
    run outside a request's tenant context."""
    for old_id, new_id in (mapping or {}).items():
        if not old_id or not new_id or old_id == new_id:
            continue
        await db.execute(
            update(PhoneCallLogORM)
            .where(PhoneCallLogORM.job_id == old_id)
            .values(job_id=new_id)
        )


def _due_conditions(user: AuthenticatedUser, now: datetime) -> list:
    return [
        func.lower(PhoneCallLogORM.called_by_email) == (user.email or "").lower(),
        PhoneCallLogORM.follow_up_due_at.is_not(None),
        PhoneCallLogORM.follow_up_due_at <= now,
        PhoneCallLogORM.follow_up_done_at.is_(None),
        PhoneCallLogORM.cleared_at.is_(None),
    ]


async def due_followups(
    db: AsyncSession, user: AuthenticatedUser, limit: int = 20
) -> tuple[int, list[dict[str, Any]]]:
    """(count, items) of `user`'s follow-up calls that are now due, newest due
    first. Each item is a call_view plus type/company/job_title (the job may
    have been deleted, hence the outer join)."""
    now = _now()
    conds = _due_conditions(user, now)
    count = (await db.execute(apply_tenant(
        select(func.count(PhoneCallLogORM.id)).where(*conds), PhoneCallLogORM.tenant_id,
    ))).scalar_one()

    stmt = apply_tenant(
        select(PhoneCallLogORM, ScrapedJobORM.company, ScrapedJobORM.job_title)
        .outerjoin(ScrapedJobORM, ScrapedJobORM.id == PhoneCallLogORM.job_id)
        .where(*conds)
        .order_by(PhoneCallLogORM.follow_up_due_at.desc(), PhoneCallLogORM.id.desc())
        .limit(max(1, int(limit or 20))),
        PhoneCallLogORM.tenant_id,
    )
    items = []
    for row, company, job_title in (await db.execute(stmt)).all():
        view = call_view(row)
        view.update(
            type="call_followup",
            contact_name=row.contact_name or "",
            company=company or "",
            job_title=job_title or "",
        )
        items.append(view)
    return int(count or 0), items


async def dismiss_followup(
    db: AsyncSession, log_id: int, user: AuthenticatedUser
) -> PhoneCallLogORM:
    """Mark one follow-up done. 404 if missing, 403 if `user` isn't the caller."""
    row = (await db.execute(apply_tenant(
        select(PhoneCallLogORM).where(PhoneCallLogORM.id == log_id), PhoneCallLogORM.tenant_id,
    ))).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail=f"Call log {log_id} not found"
        )
    if not _same_user(row.called_by_email, user):
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Only the person who logged this call can dismiss its follow-up.",
        )
    if row.follow_up_done_at is None:
        now = _now()
        row.follow_up_done_at = now
        row.updated_at = now
        await db.commit()
        await db.refresh(row)
    return row


async def dismiss_all(db: AsyncSession, user: AuthenticatedUser) -> int:
    """Mark every currently-due follow-up of `user` done. Returns rows updated."""
    now = _now()
    result = await db.execute(apply_tenant(
        update(PhoneCallLogORM)
        .where(*_due_conditions(user, now))
        .values(follow_up_done_at=now, updated_at=now),
        PhoneCallLogORM.tenant_id,
    ))
    await db.commit()
    return result.rowcount or 0
