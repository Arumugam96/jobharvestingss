"""
Send one realistic outreach test email — a pre-flight check of the CURRENT outreach setup.
==========================================================================================

Builds the email exactly the way the end-of-harvest auto-outreach does
(app/services/auto_outreach_service.py) — same sender identity (SENDER_IDENTITIES), intro,
desk contact block, deck link, signature card, From (SMTP_SENDER_MAIL + SMTP_ENVELOPE_NAME)
and Reply-To (OUTREACH_AUTO_REPLY_TO + OUTREACH_EXTRA_REPLY_TO) — and sends it to one
address so you can see what a recruiter would receive.

Transports (``--provider``):
  * brevo-api (default) — Brevo v3 HTTP API (POST /v3/smtp/email) using BREVO_API_KEY.
    The app itself has no Brevo-API transport; this script translates the app's message
    into a Brevo API payload. The inline signature logo is omitted on this path (the
    Brevo API has no CID inline-image support), everything else is identical.
  * smtp — the app's own SMTP transport (SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD).
  * mailjet — the app's Mailjet transport.

Usage (inside the api container, or locally from ai-harvest-agent/ with .env present):
    python check_brevo_smtp.py --to you@example.com
    python check_brevo_smtp.py --to you@example.com --provider smtp
    python check_brevo_smtp.py --to you@example.com --sender dharshana@sightspectrum.com
    python check_brevo_smtp.py --to you@example.com --llm      # LLM-written body
    python check_brevo_smtp.py --to you@example.com --dry-run  # print only, don't send

``--sender`` picks whose name/title/email appear in the intro + signature (a key of
SENDER_IDENTITIES). It defaults to what auto-outreach uses: the day's
OUTREACH_AUTO_REPLY_TO address. The Reply-To header always stays the configured one.
"""
from __future__ import annotations

import argparse
import asyncio
import uuid

import httpx

from app.config import get_settings
from app.prompts import outreach_prompts
from app.services import email_service
from app.services.active_clients import classify_client
from app.services.email_service import AUTOMATION_CONTACT_BLOCK, EmailSender

_BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"


class BrevoApiSender(EmailSender):
    """EmailSender whose transport is the Brevo v3 HTTP API. Message building (From,
    Reply-To list, HTML render, CustomID) is inherited unchanged — only _dispatch differs."""

    async def _dispatch(self, message: dict, log=None, fallback_message_id: str | None = None) -> str | None:
        api_key = (self._settings.brevo_api_key or "").strip()
        if not api_key:
            raise RuntimeError("BREVO_API_KEY is not set.")

        sender = message.get("From") or {}
        payload: dict = {
            "sender": {k: v for k, v in {"email": sender.get("Email"), "name": sender.get("Name")}.items() if v},
            "to": [{"email": t["Email"]} for t in message.get("To") or []],
            "subject": message.get("Subject") or "",
            "textContent": message.get("TextPart") or "",
        }
        if message.get("HTMLPart"):
            payload["htmlContent"] = message["HTMLPart"]

        headers = dict(message.get("Headers") or {})
        multi_reply = headers.pop("Reply-To", "")   # Brevo API rejects a Reply-To header
        if message.get("ReplyTo"):                  # single Reply-To
            payload["replyTo"] = {"email": message["ReplyTo"]["Email"]}
        elif multi_reply:                           # Brevo allows only one: keep the primary
            payload["replyTo"] = {"email": multi_reply.split(",")[0].strip()}

        elif "Reply-To" in headers:                # several Reply-To addresses
            first = headers["Reply-To"].split(",")[0].strip()
            payload["replyTo"] = {"email": first}  # Brevo's field is single-valued
        if message.get("CustomID"):
            headers["X-Mailin-custom"] = message["CustomID"]  # echoed on /outreach/brevo-events
        if headers:
            payload["headers"] = headers

        attachments = message.get("Attachments") or []
        if attachments:
            payload["attachment"] = [
                {"content": a["Base64Content"], "name": a["Filename"]} for a in attachments
            ]

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                _BREVO_SEND_URL, json=payload,
                headers={"api-key": api_key, "accept": "application/json"},
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Brevo API rejected the send ({resp.status_code}): {resp.text[:500]}")
        return (resp.json() or {}).get("messageId") or fallback_message_id


def _sample_job(args) -> dict:
    return {
        "job_title": args.job_title,
        "company": args.company,
        "source": args.source,
        "job_url": args.job_url,
        "outreach_to_name": args.recipient_name,
        "outreach_to_email": args.to,
    }


async def _run(args) -> None:
    settings = get_settings()
    provider = args.provider
    if provider in ("smtp", "mailjet"):
        settings = settings.model_copy(update={"email_provider": provider})

    # Same Reply-To choice as auto-outreach (first address; with 2+ addresses the app
    # rotates daily — not reproduced here so the rotation anchor file isn't touched).
    reply_addrs = settings.outreach_auto_reply_to_recipients
    reply_to = reply_addrs[0] if reply_addrs else (settings.smtp_username or "").strip()
    identity_email = (args.sender or reply_to).strip()
    identity = outreach_prompts.resolve_identity(identity_email)

    job = _sample_job(args)
    client_type = classify_client(job["company"])
    deck_url = settings.outreach_deck_url
    if args.llm:
        from app.services.llm_service import LLMService
        from app.services.outreach_service import OutreachService

        draft = await OutreachService(LLMService(settings)).generate_email(
            job, client_type, "Formal",
            sender_email=identity_email, deck_url=deck_url, contact_block=AUTOMATION_CONTACT_BLOCK,
        )
        subject, body = draft.subject, draft.body
        print(f"body source    = {'template fallback (LLM failed)' if draft.fallback_used else 'LLM'}")
    else:
        subject, body = outreach_prompts.render_email_fallback(
            client_type, job, identity_email, deck_url, AUTOMATION_CONTACT_BLOCK,
        )
        print("body source    = deterministic template (pass --llm for an LLM-written body)")

    from_ident = email_service._shared_identity_from(settings)
    all_reply_to = [reply_to] + [
        a for a in settings.outreach_extra_reply_to_recipients if a.lower() != reply_to.lower()
    ]
    print(f"transport      = {provider}")
    print(f"From           = {from_ident.get('Name', '')!r} <{from_ident.get('Email')}>")
    print(f"Reply-To       = {', '.join(all_reply_to)}")
    print(f"identity email = {identity_email}  ->  {identity}")
    print(f"subject        = {subject}")
    print("-" * 70)
    print(body)
    print("-" * 70)
    if args.dry_run:
        print("\n--dry-run: nothing sent.")
        return

    if provider == "brevo-api":
        email_service._load_logo_bytes = lambda: None   # no CID inline images on the Brevo API
        sender = BrevoApiSender(settings)
    else:
        sender = EmailSender(settings)

    custom_id = f"check-{uuid.uuid4()}"
    print(f"\nSending to {args.to} (X-Mailin-custom={custom_id}) …")
    ref = await sender.send_email_with_attachments(
        recipients=[args.to],
        subject=subject,
        body=body,
        from_email=reply_to or None,
        reply_to=reply_to or None,
        as_html=True,
        job_title=job["job_title"],
        job_url=job["job_url"],
        custom_id=custom_id,
        is_automation=False,  # contact block already in the body, as in auto-outreach
    )
    print(f"\nOK — sent. provider_message_id = {ref!r}")
    print("Check the received mail: From name, intro/signature name, and hit Reply to confirm")
    print("it addresses the Reply-To mailbox(es) above.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send one realistic outreach test email.")
    parser.add_argument("--to", required=True, help="Recipient address for the test email.")
    parser.add_argument(
        "--provider", choices=["brevo-api", "smtp", "mailjet"], default="brevo-api",
        help="Transport for this send (default: brevo-api, using BREVO_API_KEY).",
    )
    parser.add_argument("--sender", default="", help="Identity email for intro/signature (SENDER_IDENTITIES key).")
    parser.add_argument("--llm", action="store_true", help="Generate the body with the configured LLM.")
    parser.add_argument("--dry-run", action="store_true", help="Build and print the email, don't send.")
    parser.add_argument("--job-title", default="Senior Data Engineer")
    parser.add_argument("--company", default="Acme Corp")
    parser.add_argument("--source", default="Naukri", help="LinkedIn | Naukri | Dice")
    parser.add_argument("--job-url", default="https://www.naukri.com/")
    parser.add_argument("--recipient-name", default="Priya Sharma")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
