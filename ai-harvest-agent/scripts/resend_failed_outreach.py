"""
Resend failed outreach emails from the send log — recovery / maintenance tool.
=============================================================================

Why this exists
───────────────
Every outreach send attempt is logged in `email_outreach` (app/models/outreach.py)
with status "sent" or "failed". A transient Mailjet transport blip — the
`ConnectError(BrokenResourceError())` TLS-handshake reset we saw turn ~27 of 43
auto-outreach sends into "failed" rows — permanently marks a row failed even
though the recipient and content are perfectly fine. Because the failed row
already holds everything needed to send (recipient, subject, the exact rendered
body, the reply-to, the CustomID), we can just re-attempt delivery straight from
the DB. No re-generation, no re-scrape.

The email transport itself now retries transient failures (see
email_service._send_via_mailjet + Settings.mailjet_max_attempts), so new failures
should be rare; this script clears the backlog that failed before that fix and is
the go-to whenever a batch of sends fails on a network wobble.

What it does
────────────
    1. Selects failed EMAIL rows (channel="email", status="failed") — optionally
       filtered by --since/--until/--id/--limit.
    2. Skips rows it must not resend:
         * recipient now on the do-not-contact list (suppression_service)
         * a SUCCESSFUL send of the same kind already exists for that
           job/recruiter (so we never double-contact) — disable with --no-dedup
         * the same recipient appearing twice within this batch (first wins)
    3. Re-sends each remaining row via the same EmailSender the app uses (now
       with built-in retry/backoff), preserving the row id as the Mailjet CustomID
       so delivery-event webhooks still map back.
    4. Updates the row IN PLACE on success (status→"sent", records the new
       provider_message_id, seeds delivery_status="delivered", clears
       error_message) — so the Mail-logs "Failed" tile drops. On a repeat failure
       it just refreshes error_message and leaves the row failed.

Usage
─────
    # List failed rows so you can see the backlog (changes nothing):
    python scripts/resend_failed_outreach.py --list

    # Dry run (default) — shows exactly what WOULD be resent / skipped:
    python scripts/resend_failed_outreach.py

    # Actually resend everything failed:
    python scripts/resend_failed_outreach.py --yes

    # Scope it: only failures from a given day, a cap, or one specific row:
    python scripts/resend_failed_outreach.py --yes --since 2026-09-16 --until 2026-09-17
    python scripts/resend_failed_outreach.py --yes --limit 50
    python scripts/resend_failed_outreach.py --yes --id 3f2c...-uuid

    # Tune send burst (default 3 at a time):
    python scripts/resend_failed_outreach.py --yes --concurrency 2

Runs against whatever DATABASE_URL resolves to (the same DB the app uses) — from
inside the container:
    docker compose exec api python scripts/resend_failed_outreach.py --yes
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow `python scripts/resend_failed_outreach.py` from the project root (scripts/
# is added to sys.path[0], not the project root, so `app` wouldn't otherwise import).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.config import get_settings
from app.core.dependencies import get_session_factory
from app.models.harvest_run import ScrapedJobORM
from app.models.outreach import EmailOutreachORM
from app.models.recruiter import RecruiterORM  # noqa: F401 — register mapper for ScrapedJobORM.recruiter
from app.services.email_service import EmailSender
from app.services.outreach_log_service import get_by_id
from app.services.suppression_service import is_suppressed


@dataclass
class _Candidate:
    """A failed row snapshot pulled while the session is open, so the concurrent
    send phase never touches a detached ORM instance."""
    id: str
    job_id: str | None
    recruiter_id: str | None
    to_email: str
    from_email: str
    subject: str
    body: str
    outreach_kind: str
    job_title: str = ""
    job_url: str = ""
    skip_reason: str | None = field(default=None)


def _parse_day(raw: str | None, *, end: bool = False) -> datetime | None:
    """'YYYY-MM-DD' → UTC datetime; start-of-day, or start of the NEXT day when
    `end` (so a created_at < end comparison is inclusive of the whole day)."""
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw)[:10]).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise SystemExit(f"Invalid date {raw!r} — expected YYYY-MM-DD.")
    return d + timedelta(days=1) if end else d


def _base_failed_stmt(*, since=None, until=None, row_id=None, limit=None):
    """`select` for failed EMAIL rows, oldest first, with the CLI filters applied."""
    stmt = select(EmailOutreachORM).where(
        EmailOutreachORM.channel == "email",
        EmailOutreachORM.status == "failed",
    )
    if row_id:
        stmt = stmt.where(EmailOutreachORM.id == row_id)
    df = _parse_day(since)
    dt = _parse_day(until, end=True)
    if df is not None:
        stmt = stmt.where(EmailOutreachORM.created_at >= df)
    if dt is not None:
        stmt = stmt.where(EmailOutreachORM.created_at < dt)
    stmt = stmt.order_by(EmailOutreachORM.created_at.asc())
    if limit:
        stmt = stmt.limit(limit)
    return stmt


async def _load_candidates(db, *, since, until, row_id, limit, dedup) -> list[_Candidate]:
    """Read failed rows and compute a skip_reason for each, all in one session so
    only plain snapshots leave the session scope."""
    failed = (await db.execute(_base_failed_stmt(
        since=since, until=until, row_id=row_id, limit=limit,
    ))).scalars().all()
    if not failed:
        return []

    # Batch: job_id → (title, url) for the bold job-title link in the HTML body.
    job_ids = {r.job_id for r in failed if r.job_id}
    job_meta: dict[str, tuple[str, str]] = {}
    if job_ids:
        jobs = (await db.execute(
            select(ScrapedJobORM).where(ScrapedJobORM.id.in_(job_ids))
        )).scalars().all()
        job_meta = {j.id: (j.job_title or "", j.job_url or "") for j in jobs}

    # Batch: keys of every SUCCESSFUL email send, so we never resend a row whose
    # job/recruiter has since been contacted (same outreach_kind) through any path.
    sent_job_keys: set[tuple[str, str]] = set()
    sent_recruiter_keys: set[tuple[str, str]] = set()
    if dedup:
        sent = (await db.execute(
            select(
                EmailOutreachORM.job_id,
                EmailOutreachORM.recruiter_id,
                EmailOutreachORM.outreach_kind,
            ).where(
                EmailOutreachORM.channel == "email",
                EmailOutreachORM.status == "sent",
            )
        )).all()
        for job_id, recruiter_id, kind in sent:
            k = kind or "initial"
            if job_id:
                sent_job_keys.add((job_id, k))
            if recruiter_id:
                sent_recruiter_keys.add((recruiter_id, k))

    seen_recipients: set[str] = set()  # in-batch dedup (first row per recipient wins)
    candidates: list[_Candidate] = []
    for r in failed:
        kind = r.outreach_kind or "initial"
        title, url = job_meta.get(r.job_id or "", ("", ""))
        cand = _Candidate(
            id=r.id,
            job_id=r.job_id,
            recruiter_id=r.recruiter_id,
            to_email=(r.to_email or "").strip(),
            from_email=(r.from_email or "").strip(),
            subject=r.subject or "",
            body=r.body or "",
            outreach_kind=kind,
            job_title=title,
            job_url=url,
        )

        recipient_key = (r.recruiter_id or cand.to_email.lower())
        if not cand.to_email:
            cand.skip_reason = "no recipient address on row"
        elif dedup and cand.job_id and (cand.job_id, kind) in sent_job_keys:
            cand.skip_reason = "already sent (same job)"
        elif dedup and cand.recruiter_id and (cand.recruiter_id, kind) in sent_recruiter_keys:
            cand.skip_reason = "already sent (same recruiter)"
        elif recipient_key in seen_recipients:
            cand.skip_reason = "duplicate recipient in this batch"
        elif await is_suppressed(db, cand.to_email):
            cand.skip_reason = "recipient unsubscribed / suppressed"
        else:
            seen_recipients.add(recipient_key)
        candidates.append(cand)
    return candidates


async def _resend_one(sender: EmailSender, factory, cand: _Candidate) -> str:
    """Resend one candidate and update its row in place. Returns "sent" or "failed"."""
    message_id, error = None, None
    try:
        message_id = await sender.send_email_with_attachments(
            recipients=[cand.to_email],
            subject=cand.subject,
            # The stored body is the exact, fully-rendered text that was meant to go
            # out (the auto-outreach contact block is already baked into it), so send
            # it verbatim with is_automation=False to avoid appending it twice.
            body=cand.body,
            from_email=cand.from_email or None,
            reply_to=cand.from_email or None,
            as_html=True,
            job_title=cand.job_title,
            job_url=cand.job_url,
            custom_id=cand.id,          # keep webhook correlation on the same row id
            is_automation=False,
        )
        outcome = "sent"
    except Exception as exc:  # noqa: BLE001 — record whatever the transport raised
        outcome, error = "failed", str(exc) or repr(exc)

    async def _update(db):
        row = await get_by_id(db, cand.id)
        if row is None:
            return
        if outcome == "sent":
            row.status = "sent"
            row.provider_message_id = message_id
            row.error_message = None
            # Mirror build_email_outreach_row's optimistic seed: a successful hand-off
            # to Mailjet counts as delivered until a webhook says otherwise.
            row.delivery_status = "delivered"
            row.delivered_at = datetime.now(timezone.utc)
        else:
            row.error_message = error

    async with factory() as db:
        try:
            await _update(db)
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return outcome


def _print_table(cands: list[_Candidate]) -> None:
    print(f"{'to_email':38.38}  {'kind':8}  {'subject':40.40}  status/skip")
    print("-" * 110)
    for c in cands:
        note = c.skip_reason or ("SEND" if not c.skip_reason else "")
        print(f"{c.to_email:38.38}  {c.outreach_kind:8.8}  {(c.subject or ''):40.40}  {note or 'SEND'}")


async def _run(args) -> None:
    settings = get_settings()
    factory = get_session_factory(settings)

    async with factory() as db:
        cands = await _load_candidates(
            db, since=args.since, until=args.until, row_id=args.id,
            limit=args.limit, dedup=not args.no_dedup,
        )

    if not cands:
        print("No failed outreach emails match the given filters.")
        return

    sendable = [c for c in cands if not c.skip_reason]
    skipped = [c for c in cands if c.skip_reason]

    _print_table(cands)
    print(
        f"\n{len(cands)} failed row(s): {len(sendable)} to resend, {len(skipped)} skipped."
    )

    if args.list:
        return
    if not sendable:
        print("Nothing to resend.")
        return
    if not args.yes:
        print("\nDRY RUN — nothing sent. Re-run with --yes to resend the rows marked SEND.")
        return

    sender = EmailSender(settings)
    sem = asyncio.Semaphore(max(1, args.concurrency))

    async def _guarded(c: _Candidate) -> str:
        async with sem:
            return await _resend_one(sender, factory, c)

    print(f"\nResending {len(sendable)} email(s), {args.concurrency} at a time…")
    results = await asyncio.gather(
        *(_guarded(c) for c in sendable), return_exceptions=True
    )

    sent = sum(1 for r in results if r == "sent")
    failed = len(results) - sent
    for c, r in zip(sendable, results):
        if isinstance(r, Exception):
            print(f"  ERROR  {c.to_email:38.38}  {r!r}")
        elif r == "failed":
            print(f"  FAILED {c.to_email:38.38}  (see mailjet_transport_error in logs)")
    print(f"\nDone. {sent} resent successfully, {failed} still failed, {len(skipped)} skipped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resend failed outreach emails from the email_outreach log (dry run by default)."
    )
    parser.add_argument("--list", action="store_true", help="List failed rows and exit (no send).")
    parser.add_argument("--yes", action="store_true", help="Actually resend (default is a dry run).")
    parser.add_argument("--id", help="Resend only this one email_outreach row id.")
    parser.add_argument("--since", help="Only rows created on/after this date (YYYY-MM-DD).")
    parser.add_argument("--until", help="Only rows created on/before this date (YYYY-MM-DD, inclusive).")
    parser.add_argument("--limit", type=int, help="Cap the number of failed rows considered.")
    parser.add_argument("--concurrency", type=int, default=3, help="How many to send at once (default 3).")
    parser.add_argument(
        "--no-dedup", action="store_true",
        help="Resend even if a successful send of the same kind already exists for the job/recruiter.",
    )
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
