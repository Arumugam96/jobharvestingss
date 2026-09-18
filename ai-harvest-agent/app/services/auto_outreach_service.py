"""Automated end-of-harvest recruiter outreach.

Runs once at the end of every completed (non-stopped) harvest run — hooked from
app/routes/run_harvest_agent.py right after the harvest-report email. For each
harvested job whose recruiter has a resolvable email, it generates and sends an
INITIAL outreach email and logs an `email_outreach` row, so the send shows up in
the "/outreach" (Mail logs) UI exactly like a manual send.

Everything is reused from the manual per-recruiter flow (app/routes/outreach_routes.py):
  * content     — OutreachService.generate_email (same LLM provider + fallback), with
                  the desk contact block passed in so it lands above the sign-off
  * transport   — EmailSender.send_email_with_attachments (delivered body already
                  carries the desk contact block, plus the configured BCC trackers)
  * dedup guard — outreach_log_service.initial_email_sent (idempotent across runs)
  * do-not-contact — suppression_service.is_suppressed + RecruiterORM.unsubscribed
  * row shape   — outreach_log_service.build_email_outreach_row (shared with the route)

Unattended send identity: the visible From stays the shared harvest-agent identity;
the configured OUTREACH_AUTO_REPLY_TO (falling back to SMTP_FROM_EMAIL) is the Reply-To
and the recorded `sent_by`. There is no per-run cap — every eligible recruiter is
emailed — but a small semaphore bounds concurrency so the LLM/Mailjet aren't hammered.

This module is best-effort: it never raises, mirroring send_harvest_report /
run_reenrichment_sweep, so a failure here can't break the harvest that triggered it.
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

import structlog

from app.config import get_settings
from app.models.harvest_run import ScrapedJobORM
from app.services.active_clients import classify_client
from app.services.email_service import AUTOMATION_CONTACT_BLOCK, EmailSender
from app.services.harvest_run_service import db_read, db_write, scraped_job_view
from app.services.llm_service import LLMService
from app.services.outreach_log_service import build_email_outreach_row, initial_email_sent
from app.services.outreach_service import OutreachService
from app.services.suppression_service import is_suppressed

logger = structlog.get_logger(__name__)

# Bounds how many recruiters are generated+sent at once — protects the LLM provider
# and Mailjet from a burst on a large harvest. NOT a per-run cap: all eligible
# recruiters are still emailed, just a few at a time.
_CONCURRENCY = 3
# Pause after each send attempt before the slot picks up the next recruiter — spaces
# sends so a large harvest doesn't burst the relay (Brevo enforces a per-second send-rate
# limit). Applied per semaphore slot; skipped rows (suppressed/already/…) don't wait.
_SEND_DELAY_SECONDS = 2.0
# Tone used for every automated send (matches the manual composer's default).
_AUTO_TONE = "Formal"


def _dedupe_targets(job_rows: list[ScrapedJobORM]) -> tuple[list[dict], int]:
    """Reduce this run's job rows to one send target per recruiter.

    Multiple harvested jobs can map to the same recruiter — email them once. Keeps
    the first row seen per recruiter (keyed by recruiter_id, else the lowercased
    email). Rows with no resolvable email are dropped. Reads only attributes eagerly
    loaded on the ORM rows (recruiter is lazy="selectin"), so it's safe on the
    detached rows handed in from the harvest-completion scope. Returns
    (targets, skipped_no_email)."""
    seen: set[str] = set()
    targets: list[dict] = []
    no_email = 0
    for job in job_rows:
        view = scraped_job_view(job)
        email = (view.get("email_id") or "").strip()
        if not email:
            no_email += 1
            continue
        recruiter_id = job.recruiter_id
        key = recruiter_id or email.lower()
        if key in seen:
            continue
        seen.add(key)
        recruiter = getattr(job, "recruiter", None)
        targets.append({
            "view": view,
            "job_id": job.id,
            "recruiter_id": recruiter_id,
            "email": email,
            "company": view.get("company") or "",
            "job_title": view.get("job_title") or "",
            "job_url": view.get("job_url") or "",
            "unsubscribed": bool(getattr(recruiter, "unsubscribed", False)) if recruiter else False,
        })
    return targets, no_email


async def run_auto_outreach_after_harvest(
    job_rows: list[ScrapedJobORM], *, run_id: str
) -> dict:
    """Send an initial outreach email to every eligible recruiter in `job_rows`.

    Gated by settings.outreach_auto_send_on_harvest. Never raises — returns a summary
    dict of counts (also logged). `job_rows` are the harvest run's ScrapedJobORM rows
    with their recruiter eager-loaded."""
    counts = {
        "sent": 0, "failed": 0,
        "skipped_suppressed": 0, "skipped_already": 0,
        "skipped_unsubscribed": 0, "skipped_no_email": 0, "skipped_error": 0,
    }
    try:
        settings = get_settings()
        if not settings.outreach_auto_send_on_harvest:
            logger.info("auto_outreach_disabled", run_id=run_id)
            return {"enabled": False, **counts}

        targets, no_email = _dedupe_targets(job_rows or [])
        counts["skipped_no_email"] = no_email
        if not targets:
            logger.info("auto_outreach_no_targets", run_id=run_id, skipped_no_email=no_email)
            return {"enabled": True, "eligible": 0, **counts}

        # Unattended send identity: shared From (resolved inside EmailSender), the
        # configured reply-to (or SMTP_FROM_EMAIL) as Reply-To and recorded sent_by.
        reply_to = (settings.outreach_auto_reply_to or settings.smtp_username or "").strip()
        sent_by = reply_to or "auto-harvest"
        deck_url = settings.outreach_deck_url

        email_sender = EmailSender(settings)
        outreach = OutreachService(LLMService(settings))
        sem = asyncio.Semaphore(_CONCURRENCY)

        async def _process(target: dict) -> None:
            async with sem:
                email = target["email"]
                job_id = target["job_id"]
                recruiter_id = target["recruiter_id"]

                if target["unsubscribed"]:
                    counts["skipped_unsubscribed"] += 1
                    return

                # One read for both do-not-contact + already-contacted guards.
                async def _checks(db):
                    if await is_suppressed(db, email):
                        return "suppressed"
                    if await initial_email_sent(db, job_id=job_id, recruiter_id=recruiter_id):
                        return "already"
                    return "ok"

                verdict = await db_read(_checks)
                if verdict is None:      # DB read failed — don't send unverified
                    counts["skipped_error"] += 1
                    return
                if verdict == "suppressed":
                    counts["skipped_suppressed"] += 1
                    return
                if verdict == "already":
                    counts["skipped_already"] += 1
                    return

                client_type = classify_client(target["company"])
                # Pass the desk contact block into generation so append_closing places
                # it ABOVE the sign-off (website → contact → Regards). draft.body is then
                # the exact body the recruiter receives — used for BOTH the send and the
                # send-log row, so the Mail-logs UI shows what was actually delivered.
                draft = await outreach.generate_email(
                    target["view"], client_type, _AUTO_TONE,
                    sender_email=reply_to, deck_url=deck_url,
                    contact_block=AUTOMATION_CONTACT_BLOCK,
                )
                full_body = draft.body

                outreach_id = str(uuid4())
                send_status, error_message, message_id = "sent", None, None
                try:
                    message_id = await email_sender.send_email_with_attachments(
                        recipients=[email],
                        subject=draft.subject,
                        body=full_body,
                        from_email=reply_to or None,
                        reply_to=reply_to or None,
                        bcc=settings.outreach_bcc_recipients,
                        as_html=True,
                        job_title=target["job_title"],
                        job_url=target["job_url"],
                        custom_id=outreach_id,
                        is_automation=False,  # contact block already in draft.body (append_closing)
                    )
                except Exception as exc:  # Mailjet/config failure — log the failed row
                    send_status, error_message = "failed", str(exc)
                    logger.warning("auto_outreach_send_failed", to=email, error=str(exc))

                row = build_email_outreach_row(
                    id=outreach_id,
                    job_id=job_id,
                    recruiter_id=recruiter_id,
                    provider_message_id=message_id,
                    company=target["company"],
                    client_type=client_type,
                    tone=_AUTO_TONE,
                    to_email=email,
                    from_email=reply_to,
                    subject=draft.subject,
                    body=full_body,
                    fallback_used=draft.fallback_used,
                    status=send_status,
                    error_message=error_message,
                    sent_by=sent_by,
                )

                # db_write awaits the callback, so it must return an awaitable —
                # wrap the synchronous db.add in an async fn. db_write commits.
                async def _persist(db, _row=row):
                    db.add(_row)
                    return _row.id

                await db_write(_persist)

                if send_status == "sent":
                    counts["sent"] += 1
                else:
                    counts["failed"] += 1

                # Throttle before this slot picks up the next recruiter so a large harvest
                # doesn't burst the relay. Awaited — a blocking sleep would freeze the loop.
                # Placed after the send-log row is persisted, and only send attempts reach
                # here (the early-skip paths above return first, so skipped rows don't wait).
                await asyncio.sleep(_SEND_DELAY_SECONDS)

        await asyncio.gather(*(_process(t) for t in targets), return_exceptions=True)
        logger.info("auto_outreach_complete", run_id=run_id, eligible=len(targets), **counts)
        return {"enabled": True, "eligible": len(targets), **counts}
    except Exception as exc:  # never let outreach break the harvest completion path
        logger.warning("auto_outreach_failed", run_id=run_id, error=str(exc))
        return {"error": str(exc), **counts}
