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
from sqlalchemy import String, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import aliased
from sqlalchemy.sql.functions import FunctionElement

from app.config import get_settings
from app.core.contact_keys import phone_key
from app.core.tenant_context import apply_tenant, get_current_tenant_id
from app.models.auth import BUSINESS_LEAD_ROLE, AuthenticatedUser, UserORM
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
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite (tests) hands back naive datetimes; everything stored is UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    """ISO-8601 string for ``dt`` (naive values treated as UTC), or None."""
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def _same_user(email: str | None, user: AuthenticatedUser) -> bool:
    """Case-insensitive match of ``email`` against the user; False if email is empty."""
    return bool(email) and email.lower() == (user.email or "").lower()


def _loads(raw: str | None, default):
    """Decode a JSON text column, returning ``default`` when it is empty, invalid, or
    decodes to a different type than ``default``.
    """
    try:
        value = json.loads(raw) if raw else default
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def normalize_phone(phone: str | None) -> str:
    """The phone_contact_flags key and the per-number call-history key: the shared
    unique-contact key (app.core.contact_keys.phone_key — trailing 10 digits), so
    "+91 98400 12345" (LinkedIn) and "9840012345" (Naukri) are one number.

    Not app.core.contact_normalize.normalize_phone (the scrapers' cleaner)."""
    return phone_key(phone)


class _PhoneKeySQL(FunctionElement):
    """SQL twin of contact_keys.phone_key over a column: digits only, trailing 10."""
    type = String()
    inherit_cache = True
    name = "phone_key"


@compiles(_PhoneKeySQL)
def _phone_key_sql_default(element, compiler, **kw):
    """SQLite (tests): no regexp_replace, so strip the separators phone numbers carry
    in practice. substr(x, -10) is the trailing 10 chars (all of them when shorter)."""
    expr = compiler.process(list(element.clauses)[0], **kw)
    for ch in (" ", "+", "-", "(", ")", ".", "/", "\t"):
        expr = f"replace({expr}, '{ch}', '')"
    return f"substr({expr}, -10)"


@compiles(_PhoneKeySQL, "postgresql")
def _phone_key_sql_pg(element, compiler, **kw):
    """PostgreSQL: exact — every non-digit removed. right(x, 10) of a shorter string is
    the whole string, like phone_key. Matches the ix_phone_call_logs_tenant_phone_key
    expression index (app.main._ensure_phone_contact_keys)."""
    expr = compiler.process(list(element.clauses)[0], **kw)
    return f"right(regexp_replace({expr}, '\\D', '', 'g'), 10)"


def phone_key_sql(col=None):
    """SQL expression for a call's phone key (contact_keys.phone_key in SQL)."""
    return _PhoneKeySQL(col if col is not None else PhoneCallLogORM.phone)


# ── One-time re-key of phone_contact_flags (Step 0 of per-contact history) ──────

_FLAG_STAMPS = ("invalid_at", "dnc_at", "warm_at", "reenrich_requested_at")


def rekey_phone_contact_flags(sync_conn) -> int:
    """Re-key phone_contact_flags from the old all-digits key to normalize_phone's
    (trailing 10 digits). Idempotent: a second run finds nothing to change.

    Rows that collapse onto one (tenant, new key) are merged into one (the unique
    index ux_phone_contact_flags_tenant_phone forbids two): earliest non-null of each
    *_at stamp and of created_at, a non-empty correct_number (the newer row's first),
    last_outcome / last_call_id / updated_by_email / updated_at / phone from the row
    with the newer updated_at. The others are deleted before the survivor's key is
    updated. Sync (run via conn.run_sync); does not commit. Returns rows changed."""
    from sqlalchemy import inspect as sa_inspect

    if "phone_contact_flags" not in sa_inspect(sync_conn).get_table_names():
        return 0
    t = PhoneContactFlagORM.__table__
    rows = sync_conn.execute(select(t)).mappings().all()
    groups: dict[tuple[str, str], list[Any]] = {}
    for r in rows:
        groups.setdefault((r["tenant_id"], phone_key(r["phone_key"])), []).append(r)

    changed = 0
    for (_tenant, key), group in groups.items():
        if len(group) == 1 and group[0]["phone_key"] == key:
            continue
        if not key:
            continue  # a key with no digits can't be re-keyed (normalize_phone never writes one)
        newest = max(group, key=lambda g: (_aware(g["updated_at"]) or _EPOCH, g["id"]))
        keeper = next((g for g in group if g["phone_key"] == key), newest)
        values: dict[str, Any] = {"phone_key": key}
        for col in (*_FLAG_STAMPS, "created_at"):
            present = [g[col] for g in group if g[col] is not None]
            values[col] = min(present, key=_aware) if present else None
        by_newest = sorted(group, key=lambda g: (_aware(g["updated_at"]) or _EPOCH, g["id"]),
                           reverse=True)
        values["correct_number"] = next(
            (g["correct_number"] for g in by_newest if (g["correct_number"] or "").strip()), "")
        for col in ("last_outcome", "last_call_id", "updated_by_email", "updated_at", "phone"):
            values[col] = newest[col]
        others = [g["id"] for g in group if g["id"] != keeper["id"]]
        if others:
            sync_conn.execute(t.delete().where(t.c.id.in_(others)))
        sync_conn.execute(t.update().where(t.c.id == keeper["id"]).values(**values))
        changed += len(group)
    return changed


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


def _normalize_call_input(
    *,
    status: str,
    remarks: str = "",
    outcome: str = "",
    details: dict[str, Any] | None = None,
    proof: list[dict[str, Any]] | None = None,
    require_phone: str | None = None,
) -> tuple[str, str, str, dict[str, Any], str, list[dict[str, str]]]:
    """Validation + normalisation shared by log_call and update_call (422 on bad input).

    Returns ``(call_status, remarks, outcome, details, details_json, proof_items)``.
    The attempt-dependent proof rule (_validate_outcome) is left to the caller, which
    knows the call's attempt number. ``require_phone`` (log_call only) 422s when blank."""
    call_status = (status or "").strip().lower()
    if call_status not in CALL_STATUSES:
        raise HTTPException(
            status_code=422, detail=f"status must be one of {', '.join(CALL_STATUSES)}"
        )
    if require_phone is not None and not require_phone:
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
    return call_status, remarks, outcome, details, details_json, proof_items


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
        "owner_id": row.assigned_to_user_id,
    }


def _close_open_followups_stmt(job_id: str, phone: str, now: datetime):
    """Tenant-scoped UPDATE marking all open follow-ups for this job+phone done at ``now``."""
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
    phone = (phone or "").strip()[:PHONE_MAX_LEN]
    call_status, remarks, outcome, details, details_json, proof_items = _normalize_call_input(
        status=status, remarks=remarks, outcome=outcome, details=details, proof=proof,
        require_phone=phone,
    )
    if outcome:
        attempt_no = (await count_calls(db, [job.id])).get(job.id, 0) + 1
        _validate_outcome(outcome, details, sum(1 for p in proof_items if p["name"]), attempt_no)
    if outcome == "do_not_call":
        reminder_at, follow_up = None, False  # the contact is suppressed — nothing to schedule

    now = _now()
    await db.execute(_close_open_followups_stmt(job.id, phone, now))
    # Logging a status claims the record for the caller, in this same transaction:
    # the call row's owner is the authenticated user (never a client-supplied id),
    # and a still-unassigned job is self-assigned to them.
    owner_id = await _owner_id(db, user)
    await _claim_job_if_unassigned(db, job, user, now)

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
        assigned_to_user_id=owner_id,
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


async def _owner_id(db: AsyncSession, user: AuthenticatedUser) -> str | None:
    """users.id to own a new call row: the caller's id when they have a users row
    (the auth-off dev user doesn't — its row stays unowned rather than break the FK)."""
    if not user.id:
        return None
    return (await db.execute(
        select(UserORM.id).where(UserORM.id == user.id)
    )).scalar_one_or_none()


async def _claim_job_if_unassigned(
    db: AsyncSession, job: ScrapedJobORM, user: AuthenticatedUser, now: datetime,
) -> None:
    """Self-assign `job` to the caller when nobody holds it (same fields as
    job_assignment_service.assign_job, without its commit). A conditional UPDATE, so a
    teammate who claimed it a moment earlier keeps it; a job that is already assigned
    is never reassigned."""
    if not user.email:
        return
    result = await db.execute(apply_tenant(
        update(ScrapedJobORM)
        .where(ScrapedJobORM.id == job.id,
               or_(ScrapedJobORM.assigned_to_email.is_(None),
                   ScrapedJobORM.assigned_to_email == ""))
        .values(assigned_to_email=user.email,
                assigned_to_name=display_name_from_email(user.email),
                assigned_at=now, assignment_note="", assignment_reminder_done_at=None)
        .execution_options(synchronize_session=False),
        ScrapedJobORM.tenant_id,
    ))
    if result.rowcount:
        job.assigned_to_email = user.email
        job.assigned_to_name = display_name_from_email(user.email)
        job.assigned_at = now
        job.assignment_note = ""
        job.assignment_reminder_done_at = None


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
    """The job's linked recruiter row, or None when it has none (or it no longer exists)."""
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
            job.assignment_reminder_done_at = None


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


# ── Per-number call history (across jobs) ──────────────────────────────────────
# The same recruiter posts many jobs, and every harvest makes new scraped_jobs rows,
# so these look calls up by the number's key (normalize_phone / phone_key_sql), not
# by job. They inform, never block, and are NOT limited to the viewer's own calls:
# business leads see teammates' outcomes on a shared number (outcome, reason, revisit,
# caller, date, job) — but not their proof images (contact_history strips proof src
# unless can_edit_log), and edits still go through can_edit_log.

# "Revisit" choices of a not_interested call (REVISIT in PhoneVerify.jsx) → days.
# "Never" has no window: never due, and every later call falls inside it.
REVISIT_DAYS = {"1 mo": 30, "3 mo": 90, "6 mo": 180}
CONTACT_HISTORY_MAX = 200


def _contact_call_columns():
    """Minimal call + job columns for a per-number call brief (_call_brief)."""
    L, J = PhoneCallLogORM, ScrapedJobORM
    return select(
        L.id, L.job_id, L.phone, L.contact_name, L.call_status, L.call_outcome,
        L.outcome_details, L.remarks, L.proof, L.created_at, L.called_by_email,
        L.called_by_name, L.assigned_to_user_id, J.job_title, J.company,
    ).select_from(L).outerjoin(J, J.id == L.job_id)


def _call_brief(r: Any) -> dict[str, Any]:
    """{status, outcome, reason, revisit, called_by, called_at, job_id, job_title,
    company, contact_name, phone} of one call — what a teammate may see of it."""
    details = _loads(r.outcome_details, {})
    return {
        "id": r.id,
        "status": r.call_status,
        "outcome": r.call_outcome or "",
        "reason": str(details.get("reason") or ""),
        "revisit": str(details.get("revisit") or ""),
        "called_by": {"email": r.called_by_email,
                      "name": r.called_by_name or display_name_from_email(r.called_by_email)},
        "called_at": _iso(r.created_at),
        "job_id": r.job_id,
        "job_title": r.job_title or "",
        "company": r.company or "",
        "contact_name": r.contact_name or "",
        "phone": r.phone,
    }


async def contact_summaries(
    db: AsyncSession, phones: list[str], exclude_job_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """{phone as given: {"calls", "jobs", "latest": _call_brief}} for the numbers with
    at least one non-cleared call on any job (tenant-scoped; every caller's calls).
    Numbers match on normalize_phone, so "+91 98400 12345" and "9840012345" share one
    summary. `exclude_job_id` leaves that job's calls out. At most FLAGS_MAX_PHONES."""
    by_key: dict[str, list[str]] = {}
    for p in list(dict.fromkeys(p for p in phones if p))[:FLAGS_MAX_PHONES]:
        key = normalize_phone(p)
        if key:
            by_key.setdefault(key, []).append(p)
    if not by_key:
        return {}
    L = PhoneCallLogORM
    k = phone_key_sql()
    conds = [k.in_(list(by_key)), L.cleared_at.is_(None)]
    if exclude_job_id:
        conds.append(L.job_id != exclude_job_id)
    agg = (await db.execute(apply_tenant(
        select(k.label("k"), func.count(L.id), func.count(func.distinct(L.job_id)),
               func.max(L.id)).where(*conds).group_by(k),
        L.tenant_id,
    ))).all()
    if not agg:
        return {}
    latest = {r.id: r for r in (await db.execute(apply_tenant(
        _contact_call_columns().where(L.id.in_([a[3] for a in agg])), L.tenant_id,
    ))).all()}
    out: dict[str, dict[str, Any]] = {}
    for key, calls, jobs, latest_id in agg:
        row = latest.get(latest_id)
        if row is None:
            continue
        summary = {"calls": int(calls), "jobs": int(jobs), "latest": _call_brief(row)}
        for p in by_key.get(key, []):
            out[p] = summary
    return out


async def contact_history(
    db: AsyncSession, phone: str, viewer: AuthenticatedUser | None,
) -> dict[str, Any]:
    """Every non-cleared call on this number across jobs, newest first (tenant-scoped,
    every caller's): _call_brief + details, remarks, proof and can_edit. Proof images
    (`src`) are blanked unless the viewer may edit that log (its owner, or an admin) —
    the file names stay so the timeline can say proof exists."""
    key = normalize_phone(phone)
    empty = {"phone": phone, "key": key, "calls": 0, "jobs": 0, "items": []}
    if not key:
        return empty
    L = PhoneCallLogORM
    rows = (await db.execute(apply_tenant(
        _contact_call_columns()
        .where(phone_key_sql() == key, L.cleared_at.is_(None))
        .order_by(L.created_at.desc(), L.id.desc())
        .limit(CONTACT_HISTORY_MAX),
        L.tenant_id,
    ))).all()
    items = []
    for r in rows:
        can_edit = can_edit_log(r.assigned_to_user_id, r.called_by_email, viewer)
        proof = _loads(r.proof, [])
        if not can_edit:
            proof = [{**p, "src": ""} for p in proof if isinstance(p, dict)]
        items.append({**_call_brief(r), "details": _loads(r.outcome_details, {}),
                      "remarks": r.remarks or "", "proof": proof, "can_edit": can_edit})
    return {**empty, "calls": len(items), "jobs": len({i["job_id"] for i in items}),
            "items": items}


def _in_revisit_window(prev_details: str | None, prev_at: datetime | None,
                       at: datetime | None) -> bool:
    """Was a call at ``at`` placed inside the revisit window a not_interested call
    (``prev_*``) asked for? "Never" (or no recorded revisit) → always inside."""
    revisit = str(_loads(prev_details, {}).get("revisit") or "")
    days = REVISIT_DAYS.get(revisit)
    if days is None:
        return True
    prev_at, at = _aware(prev_at), _aware(at)
    return bool(prev_at and at and at < prev_at + timedelta(days=days))


async def _attach_repeat(db: AsyncSession, items: list[dict[str, Any]]) -> None:
    """Set each Phone logs item's ``repeat``: when the same number (normalize_phone) had
    an earlier non-cleared call on a DIFFERENT job (any caller), ``{n, prev_outcome,
    prev_status, prev_by, prev_at, prev_job_id}`` — n = this call's position among all
    calls to the number, prev = the newest such earlier call. Otherwise None."""
    for it in items:
        it["repeat"] = None
    keys = {normalize_phone(it["phone"]) for it in items} - {""}
    if not keys:
        return
    L = PhoneCallLogORM
    calls = (await db.execute(apply_tenant(
        select(L.id, L.job_id, L.phone, L.call_status, L.call_outcome, L.created_at,
               L.called_by_email, L.called_by_name)
        .where(phone_key_sql().in_(list(keys)), L.cleared_at.is_(None))
        .order_by(L.id),
        L.tenant_id,
    ))).all()
    by_key: dict[str, list[Any]] = {}
    for c in calls:
        by_key.setdefault(normalize_phone(c.phone), []).append(c)
    for it in items:
        earlier = [c for c in by_key.get(normalize_phone(it["phone"]), []) if c.id < it["id"]]
        prev = next((c for c in reversed(earlier) if c.job_id != it["job_id"]), None)
        if prev is not None:
            it["repeat"] = {
                "n": len(earlier) + 1,
                "prev_outcome": prev.call_outcome or "",
                "prev_status": prev.call_status,
                "prev_by": prev.called_by_name or display_name_from_email(prev.called_by_email),
                "prev_at": _iso(prev.created_at),
                "prev_job_id": prev.job_id,
            }


async def _repeat_call_ids(db: AsyncSession) -> set[int]:
    """Ids of calls placed to a number whose PREVIOUS call (any job, any caller) was
    not_interested and inside its revisit window — the admin "Repeat calls" card."""
    L = PhoneCallLogORM
    k = phone_key_sql()
    win = {"partition_by": k, "order_by": L.id}
    seq = apply_tenant(
        select(L.id, L.created_at,
               func.lag(L.call_outcome).over(**win).label("prev_outcome"),
               func.lag(L.outcome_details).over(**win).label("prev_details"),
               func.lag(L.created_at, type_=L.created_at.type).over(**win).label("prev_at"))
        .where(L.cleared_at.is_(None)),
        L.tenant_id,
    ).subquery()
    rows = (await db.execute(
        select(seq.c.id, seq.c.created_at, seq.c.prev_details, seq.c.prev_at)
        .where(seq.c.prev_outcome == "not_interested")
    )).all()
    return {r.id for r in rows if _in_revisit_window(r.prev_details, r.prev_at, r.created_at)}


LOG_ASSIGNMENTS = ("assigned", "unassigned")


# Trailing 10 digits, so "+91 98400 12345" and "9840012345" are one contact — the
# shared unique-contact key (app.core.contact_keys), also used by the jobs pages.
_phone_dedup_key = phone_key


PHONE_SOURCES = ("job", "company", "recruiter")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _filled(col):
    """SQL condition: ``col`` is neither NULL nor empty."""
    return and_(col.is_not(None), col != "")


async def phone_inventory(db: AsyncSession) -> list[dict[str, Any]]:
    """Every phone number the tenant holds, newest first: each scraped job's poster
    number ("job") and company HR contact number ("company"), and each recruiter's
    number ("recruiter"). Not deduplicated here: every filled phone field is an
    entry, so a number on both a job and its recruiter is two entries. Only
    values with no digits ("n/a") are skipped. Its size is the "Total contacts"
    figure on Phone logs and the Harvested Jobs footer (count_phone_entries)."""
    J, R = ScrapedJobORM, RecruiterORM
    jobs = (await db.execute(apply_tenant(
        select(J.id, J.job_title, J.company, J.contact_number, J.company_contact_phone,
               J.job_poster_name, J.company_contact_name, J.recruiter_id, J.created_at)
        .where(or_(_filled(J.contact_number), _filled(J.company_contact_phone))),
        J.tenant_id,
    ))).all()
    entries: list[dict[str, Any]] = []
    for j in jobs:
        for phone, source, name in ((j.contact_number, "job", j.job_poster_name),
                                    (j.company_contact_phone, "company", j.company_contact_name)):
            if not _phone_dedup_key(phone):
                continue
            entries.append({"phone": phone.strip(), "source": source, "name": name or "",
                            "company": j.company or "", "job_title": j.job_title or "",
                            "job_id": j.id, "recruiter_id": j.recruiter_id,
                            "added_at": _aware(j.created_at)})

    recruiters = (await db.execute(apply_tenant(
        select(R.id, R.person_name, R.company_name, R.contact_number, R.created_at)
        .where(_filled(R.contact_number)),
        R.tenant_id,
    ))).all()
    for rec in recruiters:
        if not _phone_dedup_key(rec.contact_number):
            continue
        entries.append({"phone": rec.contact_number.strip(), "source": "recruiter",
                        "name": rec.person_name or "", "company": rec.company_name or "",
                        "job_title": "", "job_id": None, "recruiter_id": rec.id,
                        "added_at": _aware(rec.created_at)})
    entries.sort(key=lambda e: e["added_at"] or _EPOCH, reverse=True)
    return entries


async def count_phone_entries(db: AsyncSession) -> int:
    """The "Total contacts" figure shared by Phone logs and the Harvested Jobs footer:
    every phone entry the tenant holds (phone_inventory's size)."""
    return len(await phone_inventory(db))


async def list_phones(
    db: AsyncSession, *, source: str = "", q: str = "", page: int = 1, page_size: int = 50,
) -> dict[str, Any]:
    """The Phone logs "Total contacts" drill-down: phone_inventory filtered by `source`
    and free-text `q` (number, name, company, job title), paginated. `by_source`
    counts ignore both filters."""
    entries = await phone_inventory(db)
    by_source = dict.fromkeys(PHONE_SOURCES, 0)
    for e in entries:
        by_source[e["source"]] += 1
    if source in PHONE_SOURCES:
        entries = [e for e in entries if e["source"] == source]
    term = (q or "").strip().lower()
    if term:
        digits = re.sub(r"\D", "", term)
        entries = [e for e in entries if any(term in (e[k] or "").lower() for k in
                                             ("phone", "name", "company", "job_title"))
                   or (digits and digits in re.sub(r"\D", "", e["phone"]))]
    page, page_size = max(1, int(page)), max(1, int(page_size))
    start = (page - 1) * page_size
    items = [{**e, "added_at": _iso(e["added_at"])} for e in entries[start:start + page_size]]
    return {"items": items, "total": len(entries), "page": page, "page_size": page_size,
            "by_source": by_source}


def _is_assigned():
    """SQL condition: the job is currently self-assigned to someone."""
    return _filled(ScrapedJobORM.assigned_to_email)


async def count_assigned_jobs(db: AsyncSession, email: str | None = None,
                              since: datetime | None = None) -> int:
    """Jobs currently self-assigned in the tenant, whether or not anyone has called them
    (only those assigned to `email` when given — one person's count; only those
    assigned on or after `since` when given — the Phone logs Date filter)."""
    conds = [_is_assigned()]
    if email is not None:
        conds.append(func.lower(ScrapedJobORM.assigned_to_email) == email.lower())
    if since is not None:
        conds.append(ScrapedJobORM.assigned_at >= since)
    stmt = apply_tenant(
        select(func.count(ScrapedJobORM.id)).where(*conds), ScrapedJobORM.tenant_id,
    )
    return int((await db.execute(stmt)).scalar_one() or 0)


async def list_assigned_jobs(
    db: AsyncSession, *, q: str = "", page: int = 1, page_size: int = 50,
) -> dict[str, Any]:
    """The Phone logs "Self-assigned jobs" drill-down: every job currently assigned in
    the tenant (called or not), most recently assigned first, with its number, call
    count and latest call. `q` matches job title, company and assignee."""
    J, R = ScrapedJobORM, RecruiterORM
    conds = [_is_assigned()]
    term = (q or "").strip().lower()
    if term:
        like = f"%{term}%"
        conds.append(or_(*(func.lower(col).like(like) for col in (
            J.job_title, J.company, J.assigned_to_email, J.assigned_to_name))))
    total = (await db.execute(apply_tenant(
        select(func.count(J.id)).where(*conds), J.tenant_id))).scalar_one()
    page, page_size = max(1, int(page)), max(1, int(page_size))
    rows = (await db.execute(apply_tenant(
        select(J.id, J.job_title, J.company, J.source, J.assigned_to_email, J.assigned_to_name,
               J.assigned_at, J.assignment_note, J.contact_number, J.company_contact_phone,
               R.contact_number.label("recruiter_phone"))
        .outerjoin(R, R.id == J.recruiter_id)
        .where(*conds)
        .order_by(J.assigned_at.desc().nulls_last(), J.id)
        .offset((page - 1) * page_size).limit(page_size),
        J.tenant_id,
    ))).all()
    ids = [r.id for r in rows]
    counts = await count_calls(db, ids)
    latest = await latest_for_jobs(db, ids)
    items = []
    for r in rows:
        call = latest.get(r.id)
        items.append({
            "job_id": r.id,
            "job_title": r.job_title or "",
            "company": r.company or "",
            "source": r.source or "",
            "assigned_to": {"email": r.assigned_to_email,
                            "name": r.assigned_to_name
                            or display_name_from_email(r.assigned_to_email)},
            "assigned_at": _iso(r.assigned_at),
            "note": r.assignment_note or "",
            "phone": next((p.strip() for p in (r.contact_number, r.recruiter_phone,
                                               r.company_contact_phone) if (p or "").strip()), ""),
            "calls": counts.get(r.id, 0),
            "last_call": ({"status": call["status"], "outcome": call["outcome"],
                           "called_at": call["called_at"], "called_by": call["called_by"]}
                          if call else None),
        })
    return {"items": items, "total": int(total or 0), "page": page, "page_size": page_size}


# A call's reminder, as the Phone logs page shows it. There is no delivery record:
# a reminder reaches the caller's bell once due (due_followups), so "delivered"
# means "due and still open". A closed reminder was either dismissed by the caller
# or closed by a newer call on the same job + number (log_call stamps the old row's
# follow_up_done_at with the new row's created_at) — "cancelled" when that call
# came before the reminder fell due.
REMINDER_STATES = ("none", "scheduled", "delivered", "dismissed", "called_again", "cancelled")


def _closed_by_call():
    """SQL EXISTS: a newer call on the same job + number closed this row's reminder."""
    newer = aliased(PhoneCallLogORM)
    return select(newer.id).where(
        newer.job_id == PhoneCallLogORM.job_id,
        newer.phone == PhoneCallLogORM.phone,
        newer.id != PhoneCallLogORM.id,
        newer.created_at == PhoneCallLogORM.follow_up_done_at,
    ).exists()


def _reminder_cond(state: str, now: datetime):
    """SQL condition selecting calls whose reminder is in ``state`` (REMINDER_STATES)."""
    L = PhoneCallLogORM
    due, done = L.follow_up_due_at, L.follow_up_done_at
    return {
        "none": due.is_(None),
        "scheduled": and_(due.is_not(None), done.is_(None), due > now),
        "delivered": and_(due.is_not(None), done.is_(None), due <= now),
        "dismissed": and_(due.is_not(None), done.is_not(None), ~_closed_by_call()),
        "called_again": and_(due.is_not(None), done.is_not(None), done >= due, _closed_by_call()),
        "cancelled": and_(due.is_not(None), done.is_not(None), done < due, _closed_by_call()),
    }[state]


def _reminder_view(due: datetime | None, done: datetime | None, closed_by_call: bool,
                   now: datetime) -> dict[str, Any]:
    """{state, at}: the reminder's state and the one time the page shows for it —
    the due time while open, the close time once closed."""
    due, done = _aware(due), _aware(done)
    if due is None:
        state, at = "none", None
    elif done is None:
        state, at = ("scheduled" if due > now else "delivered"), due
    elif not closed_by_call:
        state, at = "dismissed", done
    else:
        state, at = ("cancelled" if done < due else "called_again"), done
    return {"state": state, "at": _iso(at), "due_at": _iso(due), "closed_at": _iso(done)}


def can_edit_log(assigned_to_user_id: str | None, called_by_email: str | None,
                 user: AuthenticatedUser | None) -> bool:
    """May ``user`` edit this call log? Admins: any. Others (business leads): only rows
    they own — assigned_to_user_id is theirs, or, for an unowned row, they logged it
    (called_by_email, case-insensitive)."""
    if user is None:
        return False
    if user.is_admin:
        return True
    if assigned_to_user_id:
        return assigned_to_user_id == user.id
    return _same_user(called_by_email, user)


def _log_columns():
    """The Phone logs row SELECT (what _log_item reads); pair with _log_joins."""
    L = PhoneCallLogORM
    return select(
        L.id, L.job_id, L.call_status, L.call_outcome, L.outcome_details, L.phone,
        L.phone_source, L.contact_name, L.remarks, L.proof, L.follow_up,
        L.created_at, L.called_by_email, L.called_by_name,
        L.follow_up_due_at, L.follow_up_done_at,
        _closed_by_call().label("closed_by_call"),
        ScrapedJobORM.job_title, ScrapedJobORM.company,
        ScrapedJobORM.assigned_to_email, ScrapedJobORM.assigned_to_name,
        L.assigned_to_user_id, UserORM.email.label("owner_email"),
    )


def _log_joins(stmt):
    """FROM phone_call_logs + outer joins to its job (may be deleted) and owner user."""
    return (stmt.select_from(PhoneCallLogORM)
            .outerjoin(ScrapedJobORM, ScrapedJobORM.id == PhoneCallLogORM.job_id)
            .outerjoin(UserORM, UserORM.id == PhoneCallLogORM.assigned_to_user_id))


def _log_item(r: Any, now: datetime, viewer: AuthenticatedUser | None) -> dict[str, Any]:
    """One Phone logs item (GET /phone-verifications/logs `items[]`, and the PATCH
    response) from a _log_columns() row."""
    return {
        "id": r.id,
        "job_id": r.job_id,
        "status": r.call_status,
        "outcome": r.call_outcome or "",
        "details": _loads(r.outcome_details, {}),
        "phone": r.phone,
        "phone_source": r.phone_source or "",
        "contact_name": r.contact_name or "",
        "remarks": r.remarks or "",
        "proof": _loads(r.proof, []),
        "follow_up": bool(r.follow_up),
        "reminder_at": _iso(r.follow_up_due_at),
        "called_at": _iso(r.created_at),
        "called_by": {"email": r.called_by_email,
                      "name": r.called_by_name or display_name_from_email(r.called_by_email)},
        "reminder": _reminder_view(r.follow_up_due_at, r.follow_up_done_at,
                                   bool(r.closed_by_call), now),
        "job_title": r.job_title or "",
        "company": r.company or "",
        "assigned_to": ({"email": r.assigned_to_email,
                         "name": r.assigned_to_name or display_name_from_email(r.assigned_to_email)}
                        if r.assigned_to_email else None),
        "owner": ({"id": r.assigned_to_user_id, "email": r.owner_email,
                   "name": display_name_from_email(r.owner_email)}
                  if r.assigned_to_user_id and r.owner_email else None),
        "can_edit": can_edit_log(r.assigned_to_user_id, r.called_by_email, viewer),
    }


async def get_log_item(db: AsyncSession, log_id: int,
                       viewer: AuthenticatedUser | None) -> dict[str, Any] | None:
    """One non-cleared call as a Phone logs item (tenant-scoped), or None."""
    row = (await db.execute(apply_tenant(
        _log_joins(_log_columns()).where(PhoneCallLogORM.id == log_id,
                                         PhoneCallLogORM.cleared_at.is_(None)),
        PhoneCallLogORM.tenant_id,
    ))).first()
    if row is None:
        return None
    item = _log_item(row, _now(), viewer)
    await _attach_repeat(db, [item])
    return item


async def update_call(
    db: AsyncSession,
    log_id: int,
    user: AuthenticatedUser,
    *,
    status: str,
    remarks: str = "",
    follow_up: bool = False,
    outcome: str = "",
    details: dict[str, Any] | None = None,
    proof: list[dict[str, Any]] | None = None,
    reminder_at: datetime | None = None,
) -> dict[str, Any]:
    """Edit an existing call log IN PLACE (PATCH /phone-verifications/logs/{id}).

    Same validation as log_call (_normalize_call_input + _validate_outcome, with the
    attempt number = this call's position among the job's non-cleared calls). The
    reminder is recomputed like log_call's; when its due time changes,
    follow_up_done_at is reset so it fires again. do_not_call clears it. When the
    outcome changed, _apply_outcome_effects re-runs (idempotent: flag upsert keyed on
    the number, stamps set once, suppression / recruiter upserts) — the previous
    outcome's effects are not undone. phone, phone_source, contact_name, job_id,
    called_by_*, the owner and created_at are never touched.

    404 when missing, cleared or another tenant's; 403 unless can_edit_log. Returns
    the updated row as a Phone logs item (_log_item)."""
    row = (await db.execute(apply_tenant(
        select(PhoneCallLogORM).where(PhoneCallLogORM.id == log_id), PhoneCallLogORM.tenant_id,
    ))).scalar_one_or_none()
    if row is None or row.cleared_at is not None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail=f"Call log {log_id} not found"
        )
    if not can_edit_log(row.assigned_to_user_id, row.called_by_email, user):
        raise HTTPException(status_code=http_status.HTTP_403_FORBIDDEN,
                            detail="You can only edit your own call logs.")

    call_status, remarks, outcome, details, details_json, proof_items = _normalize_call_input(
        status=status, remarks=remarks, outcome=outcome, details=details, proof=proof,
    )
    if outcome:
        # This call's own attempt number: its position among the job's live calls.
        attempt_no = int((await db.execute(apply_tenant(
            select(func.count(PhoneCallLogORM.id)).where(
                PhoneCallLogORM.job_id == row.job_id,
                PhoneCallLogORM.cleared_at.is_(None),
                PhoneCallLogORM.id <= row.id,
            ),
            PhoneCallLogORM.tenant_id,
        ))).scalar_one() or 1)
        _validate_outcome(outcome, details, sum(1 for p in proof_items if p["name"]), attempt_no)
    if outcome == "do_not_call":
        reminder_at, follow_up = None, False  # the contact is suppressed — nothing to schedule

    now = _now()
    old_due = _aware(row.follow_up_due_at)
    due = None
    if reminder_at is not None:
        due = _aware(reminder_at)
        follow_up = True
    elif call_status == "unanswered" and follow_up:
        # Default-delay reminder: keep the one already scheduled rather than pushing
        # it out on every edit.
        due = old_due or now + timedelta(hours=get_settings().phone_followup_delay_hours)

    old_outcome = row.call_outcome or ""
    row.call_status = call_status
    row.call_outcome = outcome
    row.outcome_details = details_json
    row.proof = json.dumps(proof_items, ensure_ascii=False) if proof_items else ""
    row.remarks = remarks
    row.follow_up = bool(follow_up)
    if due != old_due:
        row.follow_up_due_at = due
        row.follow_up_done_at = None  # a new reminder time fires again
    row.updated_at = now

    if outcome and outcome != old_outcome:
        job = (await db.execute(apply_tenant(
            select(ScrapedJobORM).where(ScrapedJobORM.id == row.job_id),
            ScrapedJobORM.tenant_id,
        ))).scalar_one_or_none()
        if job is not None:  # a since-deleted job has nothing left to act on
            await db.flush()
            await _apply_outcome_effects(db, job, user, row, details, now)
    await db.commit()
    item = await get_log_item(db, log_id, user)
    if item is None:  # cannot happen: just updated, non-cleared, same tenant
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND,
                            detail=f"Call log {log_id} not found")
    return item


async def list_logs(
    db: AsyncSession,
    *,
    status: str = "",
    assignment: str = "",
    reminder: str = "",
    q: str = "",
    since: datetime | None = None,
    page: int = 1,
    page_size: int = 50,
    owner_id: str | None = None,
    own_email: str | None = None,
    viewer: AuthenticatedUser | None = None,
) -> dict[str, Any]:
    """Every non-cleared call in the tenant, newest first, with its job's title,
    company and assignee, its outcome and its reminder (the admin Phone logs page).
    Jobs since deleted still list (outer join) and count as unassigned. `summary`'s
    call counts apply every filter except `status` and `reminder`, so the
    answered / unanswered split stays visible.
    `summary.assigned_jobs` (jobs currently self-assigned, called or not — assigned
    within `since` and, with `owner_id`, to that user), `called_assigned_jobs` (those
    with a call matching the filters) and `total_phones` (every phone entry held —
    count_phone_entries, workspace-wide) fill the tiles.

    `owner_id` keeps only the calls owned by that user (assigned_to_user_id) — the
    admin's assignee filter, or the forced scope of a business lead. `own_email`
    marks the business-lead view: `assigned_jobs` then counts only their own
    self-assigned jobs and `total_phones` (a workspace-wide figure) is None.
    `viewer` (the caller) sets each item's `can_edit` (see can_edit_log); without
    one every item reads `can_edit: false`. Items are shaped by _log_item."""
    now = _now()
    is_assigned = _is_assigned()
    conds = _log_filter_conds(owner_id=owner_id, since=since, assignment=assignment, q=q)

    def scoped(stmt, *extra):
        """Apply the call-log -> job outer join, the shared filters plus ``extra`` conditions,
        and tenant scoping to ``stmt``.
        """
        return apply_tenant(_log_joins(stmt).where(*conds, *extra), PhoneCallLogORM.tenant_id)

    by_status = dict((await db.execute(scoped(
        select(PhoneCallLogORM.call_status, func.count(PhoneCallLogORM.id))
    ).group_by(PhoneCallLogORM.call_status))).all())
    # The self-assigned card: an admin's follows the Assignee and Date filters like the
    # call cards; a business lead's is all-time (it opens My Workspace, which lists every
    # job assigned to them), and so is its "N called" line.
    assigned_since = since if own_email is None else None
    called_conds = conds if own_email is None else _log_filter_conds(
        owner_id=owner_id, assignment=assignment, q=q)
    called_assigned_jobs = (await db.execute(apply_tenant(_log_joins(
        select(func.count(func.distinct(PhoneCallLogORM.job_id)))).where(*called_conds, is_assigned),
        PhoneCallLogORM.tenant_id))).scalar_one()
    assignee_email = own_email
    if assignee_email is None and owner_id is not None:
        assignee_email = (await db.execute(
            select(UserORM.email).where(UserORM.id == owner_id))).scalar_one_or_none() or ""
    assigned_jobs = await count_assigned_jobs(db, assignee_email, assigned_since)
    total_phones = None if own_email is not None else await count_phone_entries(db)
    answered, unanswered = int(by_status.get("answered", 0)), int(by_status.get("unanswered", 0))

    row_conds = [PhoneCallLogORM.call_status == status] if status in CALL_STATUSES else []
    if reminder in REMINDER_STATES:
        row_conds.append(_reminder_cond(reminder, now))
    total = (await db.execute(scoped(
        select(func.count(PhoneCallLogORM.id)), *row_conds))).scalar_one()
    page = max(1, int(page))
    page_size = max(1, int(page_size))
    rows = (await db.execute(scoped(
        _log_columns(),
        *row_conds,
    ).order_by(PhoneCallLogORM.created_at.desc(), PhoneCallLogORM.id.desc())
     .offset((page - 1) * page_size).limit(page_size))).all()

    items = [_log_item(r, now, viewer) for r in rows]
    await _attach_repeat(db, items)
    repeat_calls = None
    if own_email is None:  # admin-only figure
        repeat_ids = await _repeat_call_ids(db)
        repeat_calls = int((await db.execute(scoped(
            select(func.count(PhoneCallLogORM.id)), PhoneCallLogORM.id.in_(repeat_ids),
        ))).scalar_one() or 0) if repeat_ids else 0
    return {
        "items": items,
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
        "summary": {"total": answered + unanswered, "answered": answered,
                    "unanswered": unanswered, "assigned_jobs": assigned_jobs,
                    "called_assigned_jobs": int(called_assigned_jobs or 0),
                    "total_phones": total_phones, "repeat_calls": repeat_calls},
        "scope": "own" if own_email is not None else "all",
    }


def _log_filter_conds(*, owner_id: str | None = None, since: datetime | None = None,
                      assignment: str = "", q: str = "") -> list:
    """The Phone logs filters shared by the Calls and By-contact views (pair with
    _log_joins): non-cleared, owner, since, job assignment and free text."""
    conds: list = [PhoneCallLogORM.cleared_at.is_(None)]
    if owner_id is not None:
        conds.append(PhoneCallLogORM.assigned_to_user_id == owner_id)
    if since is not None:
        conds.append(PhoneCallLogORM.created_at >= since)
    if assignment == "assigned":
        conds.append(_is_assigned())
    elif assignment == "unassigned":
        conds.append(or_(ScrapedJobORM.assigned_to_email.is_(None),
                         ScrapedJobORM.assigned_to_email == ""))
    term = (q or "").strip().lower()
    if term:
        like = f"%{term}%"
        conds.append(or_(*(func.lower(col).like(like) for col in (
            ScrapedJobORM.job_title, ScrapedJobORM.company, PhoneCallLogORM.phone,
            PhoneCallLogORM.contact_name, PhoneCallLogORM.remarks,
        ))))
    return conds


def _revisit_due_cond(L, now: datetime):
    """SQL: call ``L`` is not_interested and its revisit window has passed ("Never" and
    no revisit are never due). outcome_details is json.dumps text ('"revisit": "3 mo"')."""
    return and_(L.call_outcome == "not_interested", or_(*(
        and_(L.outcome_details.like(f'%"revisit": "{label}"%'),
             L.created_at <= now - timedelta(days=days))
        for label, days in REVISIT_DAYS.items()
    )))


async def list_contacts(
    db: AsyncSession,
    *,
    status: str = "",
    assignment: str = "",
    reminder: str = "",
    q: str = "",
    since: datetime | None = None,
    outcome: str = "",
    revisit_due: bool = False,
    page: int = 1,
    page_size: int = 50,
    owner_id: str | None = None,
    own_email: str | None = None,
) -> dict[str, Any]:
    """Phone logs "By contact" view: one row per number (normalize_phone key), most
    recently called first.

    The Calls view's filters (status, assignment, reminder, since, q, owner) pick which
    numbers appear — any number with a matching call. A business lead is pinned to
    owner_id = themselves, so they get the numbers THEY called; admins get all (or one
    assignee's). Each row's figures then cover EVERY non-cleared call on that number in
    the tenant, teammates' included: `calls`, `jobs` (distinct), `first_called_at` and
    `latest` (_call_brief). `outcome` keeps rows whose latest call has that outcome;
    `revisit_due` keeps rows whose latest call is not_interested with its revisit
    window passed. Paginated server-side."""
    now = _now()
    L = PhoneCallLogORM
    k = phone_key_sql()
    conds = _log_filter_conds(owner_id=owner_id, since=since, assignment=assignment, q=q)
    if status in CALL_STATUSES:
        conds.append(L.call_status == status)
    if reminder in REMINDER_STATES:
        conds.append(_reminder_cond(reminder, now))
    keys = apply_tenant(_log_joins(select(k)).where(*conds), L.tenant_id)
    agg = apply_tenant(
        select(k.label("k"), func.count(L.id).label("calls"),
               func.count(func.distinct(L.job_id)).label("jobs"),
               func.min(L.created_at).label("first_at"),
               func.max(L.id).label("latest_id"))
        .where(L.cleared_at.is_(None), k.in_(keys.scalar_subquery()))
        .group_by(k),
        L.tenant_id,
    ).subquery()
    Latest = aliased(PhoneCallLogORM)
    row_conds = []
    if outcome:
        row_conds.append(Latest.call_outcome == outcome)
    if revisit_due:
        row_conds.append(_revisit_due_cond(Latest, now))
    base = (select(agg.c.k, agg.c.calls, agg.c.jobs, agg.c.first_at, agg.c.latest_id)
            .select_from(agg).join(Latest, Latest.id == agg.c.latest_id).where(*row_conds))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    page, page_size = max(1, int(page)), max(1, int(page_size))
    groups = (await db.execute(base.order_by(agg.c.latest_id.desc())
                               .offset((page - 1) * page_size).limit(page_size))).all()
    latest = {r.id: r for r in (await db.execute(
        _contact_call_columns().where(L.id.in_([g.latest_id for g in groups]))
    )).all()} if groups else {}
    items = []
    for g in groups:
        r = latest.get(g.latest_id)
        if r is None:
            continue
        items.append({"key": g.k, "phone": r.phone, "contact_name": r.contact_name or "",
                      "company": r.company or "", "calls": int(g.calls), "jobs": int(g.jobs),
                      "first_called_at": _iso(g.first_at), "latest": _call_brief(r)})
    return {"items": items, "total": int(total or 0), "page": page, "page_size": page_size,
            "scope": "own" if own_email is not None else "all"}


async def list_assignees(db: AsyncSession, q: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """Business leads the admin's Phone logs Assignee filter offers: active users with
    the business_lead role in the tenant scope, matching `q` on email / derived name,
    sorted by name. Only id, name and email are exposed."""
    stmt = apply_tenant(
        select(UserORM.id, UserORM.email)
        .where(UserORM.role == BUSINESS_LEAD_ROLE, UserORM.is_active.is_(True)),
        UserORM.tenant_id,
    )
    term = (q or "").strip().lower()
    out = []
    for uid, email in (await db.execute(stmt)).all():
        name = display_name_from_email(email)
        if term and term not in (email or "").lower() and term not in name.lower():
            continue
        out.append({"id": uid, "name": name, "email": email})
    out.sort(key=lambda a: (a["name"].lower(), a["email"]))
    return out[:limit]


EXPORT_MAX_ROWS = 50_000
EXPORT_MAX_COLS = 40
_XLSX_FORMULA_START = re.compile(r"^[=+\-@\t\r]")
_XLSX_PHONE_LIKE = re.compile(r"^[+-]?[0-9 ().-]*[0-9][0-9 ().-]*$")


def rows_to_xlsx(sheet: str, head: list[str], rows: list[list[Any]]) -> bytes:
    """One-sheet .xlsx of `head` + `rows` (the Phone logs page's Excel export — the page
    builds the same columns it puts in its CSV). Text that a spreadsheet would run as a
    formula gets a leading "'" (same rule as harvest-agent/src/lib/csv.js), phone-like
    values excepted. Header bold and frozen; columns sized to their content."""
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Font

    def cell(value: Any) -> Any:
        """Cell value: numbers/bools as-is, everything else as neutralised text."""
        if value is None:
            return ""
        if isinstance(value, (int, float, bool)):
            return value
        text = str(value)
        if _XLSX_FORMULA_START.match(text) and not _XLSX_PHONE_LIKE.match(text):
            text = "'" + text
        return text

    wb = Workbook()
    ws = wb.active
    ws.title = (re.sub(r"[\[\]:*?/\\]", " ", sheet or "Sheet").strip() or "Sheet")[:31]
    ws.append([cell(h) for h in head])
    for c in ws[1]:
        c.font = Font(bold=True)
    for row in rows:
        ws.append([cell(v) for v in row])
    ws.freeze_panes = "A2"
    for col in ws.columns:
        width = max((len(str(c.value or "")) for c in col[:500]), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


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
    """Filters for ``user``'s follow-ups that are due by ``now``: scheduled, not done and
    not cleared.
    """
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
