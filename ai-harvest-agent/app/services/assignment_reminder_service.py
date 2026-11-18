"""Self-assignment reminder: "you've held these jobs for N days".

Each self-assigned job reminds its assignee once, `settings.assignment_reminder_days`
(default 7) after the claim. It works like the call follow-up reminders
(phone_verification_service): nothing is scheduled. A reminder is due when the bell
polls and finds

    assigned_to_email = the user   AND   assigned_at <= now - N days
    AND assignment_reminder_done_at IS NULL

so the reminder always follows the job's current assignee:
  * one per assignment, never duplicated: it lives on the job row itself;
  * releasing the job (or a language-barrier release) cancels it, because the job
    is no longer assigned to that user;
  * a new claim (assign_job / claiming on a logged call) stamps a new assigned_at
    and clears the dismissal, so the timer restarts; the owner editing the note
    keeps both.

The bell shows only the count of due reminders (a user can claim 100 jobs in a
week), and dismissing closes every reminder due at that moment.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.tenant_context import apply_tenant
from app.models.auth import AuthenticatedUser
from app.models.harvest_run import ScrapedJobORM


def reminder_days() -> int:
    """Days after a claim the reminder falls due (at least 1)."""
    return max(1, int(get_settings().assignment_reminder_days or 7))


def _due_conditions(user: AuthenticatedUser, now: datetime) -> list[Any]:
    """WHERE clauses for the user's due, undismissed assignment reminders."""
    cutoff = now - timedelta(days=reminder_days())
    return [
        func.lower(ScrapedJobORM.assigned_to_email) == (user.email or "").lower(),
        ScrapedJobORM.assigned_at.is_not(None),
        ScrapedJobORM.assigned_at <= cutoff,
        ScrapedJobORM.assignment_reminder_done_at.is_(None),
    ]


async def due_summary(db: AsyncSession, user: AuthenticatedUser) -> dict[str, Any]:
    """{"due": count, "days": N, "oldest_assigned_at": iso | None} for the bell."""
    if not user.email:
        return {"due": 0, "days": reminder_days(), "oldest_assigned_at": None}
    now = datetime.now(timezone.utc)
    stmt = apply_tenant(
        select(func.count(), func.min(ScrapedJobORM.assigned_at)).where(*_due_conditions(user, now)),
        ScrapedJobORM.tenant_id,
    )
    count, oldest = (await db.execute(stmt)).one()
    return {
        "due": int(count or 0),
        "days": reminder_days(),
        "oldest_assigned_at": oldest.isoformat() if oldest else None,
    }


async def dismiss_due(db: AsyncSession, user: AuthenticatedUser) -> int:
    """Close every reminder due for `user` right now; returns how many were closed."""
    if not user.email:
        return 0
    now = datetime.now(timezone.utc)
    result = await db.execute(apply_tenant(
        update(ScrapedJobORM)
        .where(*_due_conditions(user, now))
        .values(assignment_reminder_done_at=now)
        .execution_options(synchronize_session=False),
        ScrapedJobORM.tenant_id,
    ))
    await db.commit()
    return int(result.rowcount or 0)
