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
         * when a recruiter has failed rows to BOTH its scraped-job email and its
           official recruiter email, only the scraped-addressed one is resent — the
           recruiter-addressed duplicate is skipped (scraped email is preferred)
    3. Re-sends each remaining row via the same EmailSender the app uses (now
       with built-in retry/backoff), preserving the row id as the Mailjet CustomID
       so delivery-event webhooks still map back.
    4. Updates the row IN PLACE on success (status→"sent", records the new
       provider_message_id, seeds delivery_status="delivered", clears
       error_message) — so the Mail-logs "Failed" tile drops. On a repeat failure
       it just refreshes error_message and leaves the row failed.

Never-contacted mode (--unsent --tenant <id>)
─────────────────────────────────────────────
The flags above retry rows that were *attempted* and failed. `--unsent` flips the
tool to reach the *backlog that was never attempted at all*: it scans one tenant's
scraped jobs and, for every recruiter/job that has a resolvable email but has NEVER
received a successful initial outreach, it GENERATES the email (same LLM + template
fallback + desk contact block as the end-of-harvest auto-outreach in
app/services/auto_outreach_service.py), sends it, and logs a fresh `email_outreach`
row.

    * --tenant is REQUIRED here — a run can only ever touch (and mark) one tenant's
      recruiters. It both scopes the scan (apply_tenant) and stamps every new row's
      tenant_id (build_email_outreach_row reads the tenant context), so the sends
      show up under that client in the Mail-logs UI.
    * Dedup is per-RECIPIENT (recruiter id OR email address) against every prior
      successful initial send — so a recruiter already contacted on ANY earlier job
      is skipped (unlike the end-of-harvest guard, which keys on job+recruiter and
      would re-mail on a fresh posting).
    * The scraped-job email is PREFERRED over the recruiter's official email: for any
      recipient we send to the address scraped from the posting when present, and
      never additionally to the recruiter's official email. When a recruiter has one
      posting with a scraped email and one without, the posting with the scraped
      email wins the per-recruiter dedup.
    * do-not-contact (suppression_service) and RecruiterORM.unsubscribed are honoured.
    * Dry-run by default like the failed path; --yes actually sends. --since/--until
      bound the scan by job created_at; --limit caps how many recent jobs are scanned.

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

    # NEVER-CONTACTED backlog for one tenant — list, dry-run, then send:
    python scripts/resend_failed_outreach.py --unsent --tenant client_us --list
    python scripts/resend_failed_outreach.py --unsent --tenant client_us          # dry run
    python scripts/resend_failed_outreach.py --unsent --tenant client_us --yes
    python scripts/resend_failed_outreach.py --unsent --tenant client_in --yes --since 2026-09-01 --limit 200

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
from uuid import uuid4

# Allow `python scripts/resend_failed_outreach.py` from the project root (scripts/
# is added to sys.path[0], not the project root, so `app` wouldn't otherwise import).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.config import get_settings
from app.core.dependencies import get_session_factory
from app.core.tenant_context import apply_tenant, get_current_tenant_id, set_current_tenant
from app.models.harvest_run import ScrapedJobORM
from app.models.outreach import EmailOutreachORM
from app.models.recruiter import RecruiterORM  # noqa: F401 — register mapper for ScrapedJobORM.recruiter
from app.services.active_clients import classify_client
from app.services.email_service import (
    AUTOMATION_CONTACT_BLOCK,
    EmailSender,
    render_outreach_email_html,
)
from app.services.harvest_run_service import scraped_job_view
from app.services.llm_service import LLMService
from app.services.outreach_log_service import build_email_outreach_row, get_by_id
from app.services.outreach_service import OutreachService
from app.services.reply_to_rotation import rotation_index
from app.services.suppression_service import is_suppressed

# Tone used for every generated send, matching the auto-outreach default.
_AUTO_TONE = "Formal"
# Pause after each send so a large backlog doesn't burst the relay (Brevo enforces a
# per-second rate limit) — mirrors auto_outreach_service._SEND_DELAY_SECONDS.
_SEND_DELAY_SECONDS = 2.0


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


@dataclass
class _UnsentTarget:
    """A never-contacted scraped job/recruiter snapshot for the --unsent mode.

    Unlike _Candidate (which carries the stored body of a failed send), there is no
    email content yet — `view` is the full scraped_job_view dict handed to
    OutreachService.generate_email at send time. Snapshotted while the session is
    open so the concurrent send phase never touches a detached ORM instance."""
    job_id: str
    recruiter_id: str | None
    email: str
    company: str
    job_title: str
    job_url: str
    view: dict
    # Which source the chosen `email` came from: "scraped" (email on the job posting,
    # preferred) or "recruiter" (RecruiterORM.official_email_id, only when no scraped
    # email exists). Shown in --list so the scraped-first preference is visible.
    email_source: str = "scraped"
    unsubscribed: bool = False
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

    # Batch: job_id → (title, url) for the bold job-title link in the HTML body, plus
    # job_id → the job's OWN scraped email (lowercased) so we can tell whether a failed
    # row was addressed to the scraped-job email or to the recruiter's official email.
    job_ids = {r.job_id for r in failed if r.job_id}
    job_meta: dict[str, tuple[str, str]] = {}
    job_scraped_email: dict[str, str] = {}
    if job_ids:
        jobs = (await db.execute(
            select(ScrapedJobORM).where(ScrapedJobORM.id.in_(job_ids))
        )).scalars().all()
        job_meta = {j.id: (j.job_title or "", j.job_url or "") for j in jobs}
        job_scraped_email = {j.id: (j.email_id or "").strip().lower() for j in jobs}

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

    # Prefer the SCRAPED-job email over the recruiter's official email: if a recruiter
    # has more than one failed row — one addressed to the job's scraped email and one
    # to the recruiter's official email — resend only the scraped-addressed one. A
    # stable sort floats scraped-addressed rows first so the per-recipient in-batch
    # dedup below keeps them and marks the recruiter-addressed duplicate as skipped.
    # Rows keep their created_at order within each group.
    def _prefers_scraped(row) -> int:
        scraped = job_scraped_email.get(row.job_id or "", "")
        return 0 if scraped and (row.to_email or "").strip().lower() == scraped else 1
    failed = sorted(failed, key=_prefers_scraped)

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

    # Throttle between sends so a large backlog doesn't burst the relay (Brevo enforces a
    # per-second send-rate limit). Awaited — NOT time.sleep(), which would block the event
    # loop and stall every other concurrent resend. Placed after the DB commit so a kill
    # during the pause can't leave a delivered email recorded as "failed" (→ double-send).
    await asyncio.sleep(2)
    return outcome


def _print_table(cands: list[_Candidate]) -> None:
    print(f"{'to_email':38.38}  {'kind':8}  {'subject':40.40}  status/skip")
    print("-" * 110)
    for c in cands:
        note = c.skip_reason or ("SEND" if not c.skip_reason else "")
        print(f"{c.to_email:38.38}  {c.outreach_kind:8.8}  {(c.subject or ''):40.40}  {note or 'SEND'}")


async def _run_failed(args) -> None:
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


# ── Never-contacted backlog (--unsent) ──────────────────────────────────────────

async def _load_unsent_targets(db, *, since, until, limit) -> list[_UnsentTarget]:
    """Scan the current tenant's scraped jobs and return one send target per
    never-contacted recipient, each with a skip_reason if it must not be sent.

    All reads happen in this one session so only plain snapshots leave scope. The
    query is tenant-scoped via apply_tenant (the caller sets the tenant context), so
    a client tenant only ever sees — and can only ever mail — its own jobs."""
    stmt = apply_tenant(select(ScrapedJobORM), ScrapedJobORM.tenant_id)
    df = _parse_day(since)
    dt = _parse_day(until, end=True)
    if df is not None:
        stmt = stmt.where(ScrapedJobORM.created_at >= df)
    if dt is not None:
        stmt = stmt.where(ScrapedJobORM.created_at < dt)
    # Newest jobs first so a --limit scan reaches the freshest postings, and so the
    # first row kept per recipient (in-batch dedup below) is the most recent one.
    stmt = stmt.order_by(ScrapedJobORM.created_at.desc())
    if limit:
        stmt = stmt.limit(limit)
    jobs = (await db.execute(stmt)).scalars().all()
    if not jobs:
        return []

    # Prefer the SCRAPED-job email over the recruiter's official email. When one
    # recruiter has several postings — some carrying an email scraped from the posting,
    # some resolving only to RecruiterORM.official_email_id — a stable sort floats the
    # postings that HAVE a scraped email first, so the per-recruiter dedup below keeps
    # one of those and the send goes to the scraped address, never separately to the
    # recruiter's. (For a single posting that has both, scraped_job_view's email_id is
    # already scraped-first.) Stable → created_at desc order is kept within each group.
    jobs = sorted(jobs, key=lambda j: 0 if (j.email_id or "").strip() else 1)

    # Batch: every recipient already sent a SUCCESSFUL initial email, keyed BOTH by
    # recruiter id and by lowercased address (tenant-scoped). Per-recipient — not
    # per-job — so a recruiter contacted on any earlier posting is never re-mailed.
    sent = (await db.execute(
        apply_tenant(
            select(EmailOutreachORM.recruiter_id, EmailOutreachORM.to_email),
            EmailOutreachORM.tenant_id,
        ).where(
            EmailOutreachORM.channel == "email",
            EmailOutreachORM.outreach_kind == "initial",
            EmailOutreachORM.status == "sent",
        )
    )).all()
    sent_recruiter_ids = {rid for rid, _ in sent if rid}
    sent_emails = {(em or "").strip().lower() for _, em in sent if (em or "").strip()}

    seen: set[str] = set()  # in-batch dedup (first/newest row per recipient wins)
    targets: list[_UnsentTarget] = []
    for job in jobs:
        view = scraped_job_view(job)
        email = (view.get("email_id") or "").strip()
        # view["email_id"] is scraped-first; record which source the chosen address
        # came from so --list can show it and prove the scraped email is preferred.
        email_source = "scraped" if (view.get("email_scraped") or "").strip() else "recruiter"
        recruiter = getattr(job, "recruiter", None)
        tgt = _UnsentTarget(
            job_id=job.id,
            recruiter_id=job.recruiter_id,
            email=email,
            company=view.get("company") or "",
            job_title=view.get("job_title") or "",
            job_url=view.get("job_url") or "",
            view=view,
            email_source=email_source,
            unsubscribed=bool(getattr(recruiter, "unsubscribed", False)) if recruiter else False,
        )
        key = job.recruiter_id or email.lower()
        if not email:
            tgt.skip_reason = "no recipient address on job"
        elif tgt.unsubscribed:
            tgt.skip_reason = "recruiter unsubscribed"
        elif job.recruiter_id and job.recruiter_id in sent_recruiter_ids:
            tgt.skip_reason = "already contacted (recruiter)"
        elif email.lower() in sent_emails:
            tgt.skip_reason = "already contacted (email)"
        elif key in seen:
            tgt.skip_reason = "duplicate recipient in this batch"
        elif await is_suppressed(db, email):
            tgt.skip_reason = "recipient unsubscribed / suppressed"
        else:
            seen.add(key)
        targets.append(tgt)
    return targets


def _resolve_reply_to(settings) -> tuple[str, str]:
    """Pick the unattended Reply-To / recorded sent_by exactly like auto-outreach:
    rotate the primary Reply-To through OUTREACH_AUTO_REPLY_TO one address per day
    when several are configured, else the single address, else SMTP_FROM_EMAIL.
    Chosen once per run, before the fan-out, so every send uses the same day's owner
    and the rotation anchor is touched once."""
    reply_addrs = settings.outreach_auto_reply_to_recipients
    if len(reply_addrs) >= 2:
        reply_to = reply_addrs[rotation_index() % len(reply_addrs)]
    elif reply_addrs:
        reply_to = reply_addrs[0]
    else:
        reply_to = (settings.smtp_username or "").strip()
    return reply_to, (reply_to or "auto-harvest-backfill")


async def _send_unsent_one(
    sender: EmailSender, outreach: OutreachService, factory, tgt: _UnsentTarget,
    *, reply_to: str, sent_by: str, deck_url: str,
) -> str:
    """Generate, send, and log one never-contacted target. Returns "sent"/"failed".

    Mirrors auto_outreach_service._process's send half: same generation (with the
    desk contact block baked into draft.body by append_closing), same transport, and
    build_email_outreach_row for the log row (which stamps the current tenant_id)."""
    client_type = classify_client(tgt.company)
    draft = await outreach.generate_email(
        tgt.view, client_type, _AUTO_TONE,
        sender_email=reply_to, deck_url=deck_url,
        contact_block=AUTOMATION_CONTACT_BLOCK,
    )
    full_body = draft.body

    outreach_id = str(uuid4())
    status, error, message_id = "sent", None, None
    try:
        message_id = await sender.send_email_with_attachments(
            recipients=[tgt.email],
            subject=draft.subject,
            body=full_body,
            from_email=reply_to or None,
            reply_to=reply_to or None,
            as_html=True,
            job_title=tgt.job_title,
            job_url=tgt.job_url,
            custom_id=outreach_id,      # keep webhook correlation on this row id
            is_automation=False,        # contact block already in draft.body
        )
    except Exception as exc:  # noqa: BLE001 — record whatever the transport raised
        status, error = "failed", str(exc) or repr(exc)

    row = build_email_outreach_row(
        id=outreach_id,
        job_id=tgt.job_id,
        recruiter_id=tgt.recruiter_id,
        provider_message_id=message_id,
        company=tgt.company,
        client_type=client_type,
        tone=_AUTO_TONE,
        to_email=tgt.email,
        from_email=reply_to,
        subject=draft.subject,
        body=full_body,
        body_html=render_outreach_email_html(full_body, tgt.job_title, tgt.job_url),
        fallback_used=draft.fallback_used,
        status=status,
        error_message=error,
        sent_by=sent_by,
    )

    async def _persist(db, _row=row):
        db.add(_row)

    async with factory() as db:
        try:
            await _persist(db)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    # Throttle after the commit (see failed path) so the relay isn't burst and a kill
    # during the pause can't leave a delivered email unrecorded.
    await asyncio.sleep(_SEND_DELAY_SECONDS)
    return status


def _print_unsent_table(targets: list[_UnsentTarget]) -> None:
    print(f"{'to_email':38.38}  {'src':9}  {'company':20.20}  {'job_title':26.26}  status/skip")
    print("-" * 112)
    for t in targets:
        print(
            f"{t.email:38.38}  {t.email_source:9.9}  {(t.company or ''):20.20}  "
            f"{(t.job_title or ''):26.26}  {t.skip_reason or 'SEND'}"
        )


async def _run_unsent(args) -> None:
    settings = get_settings()
    factory = get_session_factory(settings)
    tenant = get_current_tenant_id()

    async with factory() as db:
        targets = await _load_unsent_targets(
            db, since=args.since, until=args.until, limit=args.limit,
        )

    if not targets:
        print(f"No scraped jobs for tenant {tenant!r} match the given filters.")
        return

    sendable = [t for t in targets if not t.skip_reason]
    skipped = [t for t in targets if t.skip_reason]

    _print_unsent_table(targets)
    print(
        f"\nTenant {tenant!r}: {len(targets)} job(s) scanned — "
        f"{len(sendable)} never-contacted to email, {len(skipped)} skipped."
    )

    if args.list:
        return
    if not sendable:
        print("Nothing to send.")
        return
    if not args.yes:
        print("\nDRY RUN — nothing sent. Re-run with --yes to email the rows marked SEND.")
        return

    reply_to, sent_by = _resolve_reply_to(settings)
    deck_url = settings.outreach_deck_url
    sender = EmailSender(settings)
    outreach = OutreachService(LLMService(settings))
    sem = asyncio.Semaphore(max(1, args.concurrency))

    async def _guarded(t: _UnsentTarget) -> str:
        async with sem:
            return await _send_unsent_one(
                sender, outreach, factory, t,
                reply_to=reply_to, sent_by=sent_by, deck_url=deck_url,
            )

    print(
        f"\nSending {len(sendable)} new outreach email(s) as tenant {tenant!r}, "
        f"{args.concurrency} at a time…"
    )
    results = await asyncio.gather(
        *(_guarded(t) for t in sendable), return_exceptions=True
    )

    sent = sum(1 for r in results if r == "sent")
    failed = len(results) - sent
    for t, r in zip(sendable, results):
        if isinstance(r, Exception):
            print(f"  ERROR  {t.email:38.38}  {r!r}")
        elif r == "failed":
            print(f"  FAILED {t.email:38.38}  (see logs)")
    print(f"\nDone. {sent} sent, {failed} failed, {len(skipped)} skipped.")


async def _run(args) -> None:
    """Dispatch to the never-contacted backlog (--unsent) or the failed-resend path.
    Tenant context is set ONLY for --unsent so the failed path stays byte-for-byte
    as it was (all-tenant, no scoping)."""
    if args.unsent:
        set_current_tenant(args.tenant)  # required — validated in main()
        await _run_unsent(args)
    else:
        await _run_failed(args)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Resend failed outreach emails, OR (--unsent --tenant <id>) send fresh "
            "outreach to never-contacted recruiters/jobs for one tenant. Dry run by default."
        )
    )
    parser.add_argument("--list", action="store_true", help="List rows and exit (no send).")
    parser.add_argument("--yes", action="store_true", help="Actually send (default is a dry run).")
    parser.add_argument("--id", help="[failed mode] Resend only this one email_outreach row id.")
    parser.add_argument("--since", help="Only rows/jobs created on/after this date (YYYY-MM-DD).")
    parser.add_argument("--until", help="Only rows/jobs created on/before this date (YYYY-MM-DD, inclusive).")
    parser.add_argument("--limit", type=int, help="Cap how many rows (failed) / recent jobs (--unsent) are considered.")
    parser.add_argument("--concurrency", type=int, default=3, help="How many to send at once (default 3).")
    parser.add_argument(
        "--no-dedup", action="store_true",
        help="[failed mode] Resend even if a successful send of the same kind already exists for the job/recruiter.",
    )
    parser.add_argument(
        "--unsent", action="store_true",
        help="Send NEW outreach to never-contacted recruiters/jobs (generates content). Requires --tenant.",
    )
    parser.add_argument(
        "--tenant",
        help="Tenant id (e.g. client_us, client_in, internal). REQUIRED with --unsent: scopes the "
             "scan and stamps every new row's tenant_id. Ignored in the failed-resend mode.",
    )
    args = parser.parse_args()
    if args.unsent and not (args.tenant or "").strip():
        parser.error(
            "--unsent requires --tenant (e.g. --tenant client_us) so sends are scoped and "
            "marked to exactly one tenant."
        )
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
