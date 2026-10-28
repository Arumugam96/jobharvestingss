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
                  carries the desk contact block)
  * dedup guard — outreach_log_service.initial_email_sent (idempotent across runs)
  * do-not-contact — suppression_service.is_suppressed + RecruiterORM.unsubscribed
  * daily limits — one initial email per recipient address per day (any job, any
                  tenant), one per company+location per day for Apollo HR contacts
                  (whose rotation — company_location_contact_service — is advanced
                  after each send); never to anyone who replied, and a company is
                  paused for outreach_company_reply_pause_days after any reply there
  * row shape   — outreach_log_service.build_email_outreach_row (shared with the route)

Unattended send identity: the visible From stays the shared harvest-agent identity;
the configured OUTREACH_AUTO_REPLY_TO (falling back to SMTP_FROM_EMAIL) is the Reply-To
and the recorded `sent_by`. When OUTREACH_AUTO_REPLY_TO lists two or more addresses,
the primary Reply-To rotates through them one per calendar day (day 1 → first address,
day 2 → second, wrapping around), anchored to the first automated send — see
app/services/reply_to_rotation.py. Any OUTREACH_EXTRA_REPLY_TO addresses are still
attached on top of the day's address on every send. There is no per-run cap — every
eligible recruiter is emailed — but a small semaphore bounds concurrency so the
LLM/Mailjet aren't hammered.

This module is best-effort: it never raises, mirroring send_harvest_report /
run_reenrichment_sweep, so a failure here can't break the harvest that triggered it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import structlog

from app.config import get_settings
from app.core.company_size import minimum_floor, size_meets_floor
from app.models.harvest_run import ScrapedJobORM
from app.services.active_clients import classify_client
from app.services.email_service import AUTOMATION_CONTACT_BLOCK, EmailSender, render_outreach_email_html
from app.services.harvest_run_service import (
    contact_origin,
    db_read,
    db_write,
    scraped_job_view,
)
from app.services.llm_service import LLMService
from app.core.location import normalize_location_key
from app.services.company_location_contact_service import (
    location_emailed_since,
    mark_contact_emailed,
)
from app.services.company_service import normalize_company_key
from app.services.outreach_log_service import (
    build_email_outreach_row,
    companies_replied_since,
    initial_email_sent,
    posting_already_emailed,
    recipient_emailed_since,
    recipient_has_replied,
)
from app.services.outreach_service import OutreachService
from app.services.reply_to_rotation import day_start, rotation_index
from app.services.resdex_match_service import (
    format_candidate_block,
    format_profile_count_line,
    latest_match_counts,
    mark_candidates_sent,
    prepare_resdex_candidates,
    render_candidate_cards_html,
)
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


def _dedupe_targets(
    job_rows: list[ScrapedJobORM], floor: int | None = None
) -> tuple[list[dict], int, int]:
    """Reduce this run's job rows to one send target per contact.

    The recipient is scraped_job_view's resolved `outreach_to_email` — the scraped/
    recruiter email, falling back to the company+location Apollo HR contact when the
    job had none (see company_location_contact_service). Multiple jobs can map to the
    same contact — email them once:
      * recruiter/poster email → keyed by recruiter_id (else the lowercased email),
      * company HR contact      → keyed by "cc:"+company+location (the contact isn't the
        poster; one email per company+location however many postings it has).
    Rows with no resolvable email are dropped. Reads only attributes eagerly loaded on
    the ORM rows (recruiter is lazy="selectin"), so it's safe on the detached rows
    handed in from the harvest-completion scope.

    `floor` is the minimum company-size lower bound (from the Rule Engine size
    pills); a contact whose company is KNOWN to be below it is dropped from outreach —
    the job itself stays scraped and stored, only its email is suppressed.
    Unknown/unparseable sizes are still emailed (size_meets_floor → None). Returns
    (targets, skipped_no_email, skipped_below_size)."""
    seen: set[str] = set()
    targets: list[dict] = []
    no_email = 0
    below_size = 0
    for job in job_rows:
        view = scraped_job_view(job)
        email = (view.get("outreach_to_email") or "").strip()
        if not email:
            no_email += 1
            continue
        if size_meets_floor(view.get("company_size", ""), floor) is False:
            below_size += 1
            continue
        # The outreach email came from the company HR contact when no scraped/recruiter
        # email (email_id) was present — that contact is NOT the job's poster.
        is_company = not (view.get("email_id") or "").strip()
        recruiter_id = job.recruiter_id
        if is_company:
            pair = (normalize_company_key(job.company or ""), normalize_location_key(job.location or ""))
            key = "cc:" + ("|".join(pair) if all(pair) else email.lower())
        else:
            key = recruiter_id or email.lower()
        if key in seen:
            continue
        seen.add(key)
        recruiter = getattr(job, "recruiter", None)
        targets.append({
            "view": view,
            "job_id": job.id,
            # A company HR contact is not the poster recruiter — don't attribute the
            # send to that recruiter_id (keeps the dedup/already-sent check per job).
            "recruiter_id": None if is_company else recruiter_id,
            "email": email,
            "contact_kind": "company" if is_company else "recruiter",
            # job_post | recruiter_profile | apollo | apollo_hr — stored on the
            # Resdex search for the detail page's source badge.
            "contact_origin": contact_origin(job),
            "company": view.get("company") or "",
            "location": job.location or "",
            "job_title": view.get("job_title") or "",
            "job_url": view.get("job_url") or "",
            # Recruiter unsubscribe only applies to a real poster; a company contact's
            # opt-out is enforced via the suppression list (is_suppressed) in _process.
            "unsubscribed": (
                bool(getattr(recruiter, "unsubscribed", False))
                if (recruiter and not is_company) else False
            ),
        })
    return targets, no_email, below_size


def _no_email_resdex_targets(job_rows: list[ScrapedJobORM]) -> list[dict]:
    """Resdex-only targets for jobs with NO resolvable email
    (RESDEX_INCLUDE_JOBS_WITHOUT_EMAIL): candidates are searched and stored for the
    job detail page, but these are never handed to the send fan-out — there is no
    recipient. contact_kind="none", one per job_url."""
    seen: set[str] = set()
    out: list[dict] = []
    for job in job_rows:
        view = scraped_job_view(job)
        url = view.get("job_url") or ""
        if (view.get("outreach_to_email") or "").strip() or not url or url in seen:
            continue
        seen.add(url)
        out.append({
            "view": view, "job_id": job.id, "recruiter_id": None, "email": "",
            "contact_kind": "none", "contact_origin": "", "company": view.get("company") or "",
            "location": job.location or "", "job_title": view.get("job_title") or "",
            "job_url": url, "unsubscribed": False,
        })
    return out


async def _skip_reason(
    target: dict, *, replied_companies: set[str], today_start: datetime, claimed: set[str],
    dry_run: bool = False,
) -> str | None:
    """Why this target must NOT be emailed now, or None when it may be.

    The returned verdict maps 1:1 to the ``skipped_<verdict>`` counts key, checked in
    this order: unsubscribed, company_replied, then one DB read for suppressed /
    replied / already / duplicate_posting / recent_recipient / location_today
    ("error" when that read fails — never send unverified), then the in-run
    ``claimed`` address set (recent_recipient). When eligible the address is
    claimed synchronously (no await between check and add) so two concurrent targets
    sharing an address (one careers@ on several postings) can't both send.
    ``dry_run=True`` (the Resdex pre-pass) runs the same checks without claiming."""
    email = target["email"]
    job_id = target["job_id"]
    recruiter_id = target["recruiter_id"]
    job_url = target["job_url"]

    if target["unsubscribed"]:
        return "unsubscribed"
    if normalize_company_key(target["company"]) in replied_companies:
        return "company_replied"
    is_company = target["contact_kind"] == "company"

    # One read for do-not-contact + already-contacted + duplicate-posting
    # + the per-recipient / per-company+location daily limits.
    async def _checks(db):
        if await is_suppressed(db, email):
            return "suppressed"
        if await recipient_has_replied(db, email):
            return "replied"
        if await initial_email_sent(db, job_id=job_id, recruiter_id=recruiter_id):
            return "already"
        # Backstop to the source-level harvest dedup: don't re-email a
        # posting (same job_url) a prior run already emailed — covers
        # the manual sweep + within-run cross-source overlap.
        if await posting_already_emailed(db, job_url):
            return "duplicate_posting"
        if await recipient_emailed_since(db, email, today_start):
            return "recent_recipient"
        if is_company and await location_emailed_since(
            db, target["company"], target["location"], today_start,
        ):
            return "location_today"
        return "ok"

    verdict = await db_read(_checks)
    if verdict is None:      # DB read failed — don't send unverified
        return "error"
    if verdict != "ok":
        return verdict
    if email.lower() in claimed:
        return "recent_recipient"
    if not dry_run:
        claimed.add(email.lower())
    return None


def _resdex_email_mode(settings) -> str:
    """RESDEX_EMAIL_MODE normalized to "count" | "cards". Anything else (typo, blank)
    falls back to "count" — the safe default that never shows profiles — and is logged."""
    raw = str(getattr(settings, "resdex_email_mode", "count") or "").strip().lower()
    if raw in ("count", "cards"):
        return raw
    logger.warning("resdex_email_mode_unknown", value=raw, using="count")
    return "count"


async def _no_login_wait(since: datetime) -> bool:
    """prepare_resdex_candidates login_waiter that never waits (a catch-up pass after
    the harvest's own Resdex pass, which already waited for a login once)."""
    return False


async def run_auto_outreach_after_harvest(
    job_rows: list[ScrapedJobORM], *, run_id: str, company_sizes: list[str] | None = None,
    resdex_login_wait: bool = True,
) -> dict:
    """Send an initial outreach email to every eligible recruiter in `job_rows`.

    Gated by settings.outreach_auto_send_on_harvest — except that with RESDEX_ENABLED
    the Resdex candidate pass still runs (and stores) when sending is off. Never
    raises — returns a summary
    dict of counts (also logged). `job_rows` are the harvest run's ScrapedJobORM rows
    with their recruiter eager-loaded.

    `company_sizes` are the Rule Engine size pills (e.g. ["501-1000"]); their lowest
    band lower-bound is a minimum-size floor below which a KNOWN-size company is not
    emailed (its jobs are still stored). Empty/None ⇒ no size gate.

    `resdex_login_wait=False` makes a Resdex session_expired outcome final instead of
    raising the "Naukri recruiter login needed" request and waiting (used by the
    Apollo catch-up sweep, which runs right after the harvest's own Resdex pass)."""
    counts = {
        "sent": 0, "failed": 0,
        "skipped_suppressed": 0, "skipped_already": 0,
        "skipped_unsubscribed": 0, "skipped_no_email": 0,
        "skipped_below_size": 0, "skipped_duplicate_posting": 0, "skipped_error": 0,
        "skipped_recent_recipient": 0, "skipped_replied": 0,
        "skipped_company_replied": 0, "skipped_location_today": 0,
        "resdex_jobs_with_candidates": 0, "resdex_jobs_with_count": 0,
    }
    try:
        settings = get_settings()
        send = settings.outreach_auto_send_on_harvest
        # With auto-send off the Resdex pass still runs (RESDEX_ENABLED): candidates
        # are searched and stored for the jobs that WOULD be emailed, nothing is sent.
        if not send and not settings.resdex_enabled:
            logger.info("auto_outreach_disabled", run_id=run_id)
            return {"enabled": False, **counts}

        floor = minimum_floor(company_sizes or [])
        targets, no_email, below_size = _dedupe_targets(job_rows or [], floor)
        counts["skipped_no_email"] = no_email
        counts["skipped_below_size"] = below_size
        resdex_only: list[dict] = (
            _no_email_resdex_targets(job_rows or [])
            if settings.resdex_enabled and settings.resdex_include_jobs_without_email else []
        )
        if not targets and not resdex_only:
            logger.info(
                "auto_outreach_no_targets", run_id=run_id,
                skipped_no_email=no_email, skipped_below_size=below_size,
                min_company_size_floor=floor,
            )
            return {"enabled": send, "eligible": 0, **counts}

        # Recipient-level limits: one initial email per address per day, one per
        # company+location per day (Apollo HR contacts), never to someone who
        # replied, and a company pause after anyone there replied.
        today_start = day_start()
        pause_days = int(getattr(settings, "outreach_company_reply_pause_days", 30) or 0)
        replied_companies: set[str] = set()
        if pause_days > 0:
            since = datetime.now(timezone.utc) - timedelta(days=pause_days)
            replied_companies = await db_read(lambda db: companies_replied_since(db, since)) or set()

        claimed: set[str] = set()   # addresses already taken by a target this run

        # Resdex pre-pass (off by default): one batched candidate search for the jobs
        # that will actually be emailed. skip_check runs the same eligibility checks
        # in dry-run mode (claiming into its own set, never `claimed`) so no Resdex
        # quota is spent on targets the fan-out would skip. Never raises; {} → every
        # email goes out exactly as without Resdex.
        resdex: dict[str, list[dict]] = {}
        if settings.resdex_enabled:
            pre_claimed: set[str] = set()

            async def _would_skip(target: dict) -> bool:
                if target["contact_kind"] == "none":   # never emailed — nothing to check
                    return False
                reason = await _skip_reason(
                    target, replied_companies=replied_companies,
                    today_start=today_start, claimed=pre_claimed, dry_run=True,
                )
                if reason is not None:
                    return True
                pre_claimed.add(target["email"].lower())
                return False

            # Only passed when disabling the wait, so the default call is unchanged.
            wait_kw = {} if resdex_login_wait else {"login_waiter": _no_login_wait}
            resdex = await prepare_resdex_candidates(
                targets + resdex_only, run_id=run_id, skip_check=_would_skip, **wait_kw,
            ) or {}
            counts["resdex_jobs_with_candidates"] = sum(1 for v in resdex.values() if v)

        if not send:
            logger.info("auto_outreach_disabled_resdex_only", run_id=run_id,
                        resdex_jobs_with_candidates=counts["resdex_jobs_with_candidates"])
            return {"enabled": False, **counts}
        if not targets:      # only Resdex-only (no-email) jobs — nobody to email
            return {"enabled": True, "eligible": 0, **counts}

        # What the Resdex pass contributes to each email (RESDEX_INCLUDE_IN_EMAIL):
        # "cards" → the picked candidates as profile cards; "count" → one line quoting
        # the newest matched search's Resdex result count (rounded, ≥ min). One read for
        # every target here — covers this run's searches and reused recent ones alike.
        include_resdex = settings.resdex_enabled and settings.resdex_include_in_email
        email_mode = _resdex_email_mode(settings) if include_resdex else ""
        profile_counts: dict[str, int] = {}
        if email_mode == "count":
            profile_counts = await latest_match_counts([t["job_url"] for t in targets])
            counts["resdex_jobs_with_count"] = len({
                t["job_url"] for t in targets
                if format_profile_count_line(
                    profile_counts.get(t["job_url"], 0), t["job_title"], t["job_url"],
                )
            })

        # Unattended send identity: shared From (resolved inside EmailSender), the
        # configured reply-to (or SMTP_FROM_EMAIL) as Reply-To and recorded sent_by.
        # OUTREACH_AUTO_REPLY_TO may hold several comma-separated addresses; when it
        # does, the primary Reply-To rotates through them one per calendar day (day 1 →
        # first address, day 2 → second, …) so recruiter replies alternate between the
        # desk's salespeople day by day. Chosen ONCE here, before the fan-out below, so
        # every send in this run uses the same day's address and the rotation anchor is
        # touched once (no concurrency race). Any OUTREACH_EXTRA_REPLY_TO addresses are
        # still attached on top by EmailSender. The single chosen value flows into
        # sender_email / from_email / reply_to / sent_by below, so the sign-off reach-out
        # line, the Reply-To header, and the logged sent_by all reflect the day's owner.
        reply_addrs = settings.outreach_auto_reply_to_recipients
        if len(reply_addrs) >= 2:
            idx = rotation_index() % len(reply_addrs)
            reply_to = reply_addrs[idx]
            logger.info(
                "auto_outreach_reply_to_rotated",
                run_id=run_id, index=idx, of=len(reply_addrs), reply_to=reply_to,
            )
        elif reply_addrs:
            reply_to = reply_addrs[0]
        else:
            reply_to = (settings.smtp_username or "").strip()
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
                job_url = target["job_url"]

                reason = await _skip_reason(
                    target, replied_companies=replied_companies,
                    today_start=today_start, claimed=claimed,
                )
                if reason is not None:
                    counts[f"skipped_{reason}"] += 1
                    return
                is_company = target["contact_kind"] == "company"

                client_type = classify_client(target["company"])
                # Pass the desk contact block into generation so append_closing places
                # it ABOVE the sign-off (website → contact → Regards). draft.body is then
                # the exact body the recruiter receives — used for BOTH the send and the
                # send-log row, so the Mail-logs UI shows what was actually delivered.
                gen_kwargs = dict(
                    sender_email=reply_to, deck_url=deck_url,
                    contact_block=AUTOMATION_CONTACT_BLOCK,
                )
                # Resdex (pre-pass above) → one block between the body and the contact
                # block. Only passed when non-empty so the call is unchanged for every
                # other email.
                #   cards: the candidate block — labelled text in the body (text part +
                #          stored body), drawn as profile cards in the HTML part.
                #   count: the one-line profile-count note; candidate_html stays "" so
                #          the HTML part renders it as an ordinary body line.
                cands: list[dict] = []
                candidate_block = candidate_html = ""
                if email_mode == "cards":
                    cands = resdex.get(job_url) or []
                    if cands:
                        candidate_block = format_candidate_block(
                            cands, job_title=target["job_title"],
                        )
                        candidate_html = render_candidate_cards_html(
                            cands, job_title=target["job_title"],
                        )
                    if candidate_block:
                        gen_kwargs["candidate_block"] = candidate_block
                elif email_mode == "count":
                    candidate_block = format_profile_count_line(
                        profile_counts.get(job_url, 0), target["job_title"], job_url,
                    )
                    if candidate_block:
                        gen_kwargs["profile_count_line"] = candidate_block
                draft = await outreach.generate_email(
                    target["view"], client_type, _AUTO_TONE, **gen_kwargs,
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
                        as_html=True,
                        job_title=target["job_title"],
                        job_url=target["job_url"],
                        custom_id=outreach_id,
                        is_automation=False,  # contact block already in draft.body (append_closing)
                        candidate_block=candidate_block,
                        candidate_html=candidate_html,
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
                    body_html=render_outreach_email_html(
                        full_body, target["job_title"], target["job_url"],
                        candidate_block=candidate_block, candidate_html=candidate_html,
                    ),
                    fallback_used=draft.fallback_used,
                    status=send_status,
                    error_message=error_message,
                    sent_by=sent_by,
                    job_url=job_url,   # normalized + stored for the posting-dedup backstop
                )

                # db_write awaits the callback, so it must return an awaitable —
                # wrap the synchronous db.add in an async fn. db_write commits.
                async def _persist(db, _row=row):
                    db.add(_row)
                    return _row.id

                await db_write(_persist)

                if send_status == "sent" and cands and candidate_block:
                    # Link the included Resdex candidates to this email (detail page).
                    # Cards mode only — a count-mode email shows no profiles, so
                    # linking any would be false.
                    await mark_candidates_sent(
                        [c["id"] for c in cands if c.get("id")], outreach_id,
                    )

                if send_status == "sent" and is_company:
                    # Advance the company+location rotation to the next HR person.
                    await db_write(lambda db: mark_contact_emailed(
                        db, target["company"], target["location"], email,
                    ))

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
        logger.info(
            "auto_outreach_complete", run_id=run_id, eligible=len(targets),
            min_company_size_floor=floor, **counts,
        )
        return {"enabled": True, "eligible": len(targets), **counts}
    except Exception as exc:  # never let outreach break the harvest completion path
        logger.warning("auto_outreach_failed", run_id=run_id, error=str(exc))
        return {"error": str(exc), **counts}
