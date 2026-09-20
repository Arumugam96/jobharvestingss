"""
Send one test email through the app's SMTP transport (Brevo relay) — a pre-flight check.
=======================================================================================

Verifies, using the SAME EmailSender + settings the app uses, that:
  * the SMTP relay accepts our login (SMTP login + SMTP key), and
  * a message goes out carrying the ``X-Mailin-custom`` header (the id Brevo echoes back
    on its transactional webhooks, which /outreach/brevo-events matches on).

Run it BEFORE flipping the whole app to email_provider="smtp".

Usage (inside the api container, where DATABASE_URL/.env resolve):
    docker compose exec api python check_brevo_smtp.py --to you@example.com
    # force the SMTP path even if EMAIL_PROVIDER is still "mailjet":
    docker compose exec api python check_brevo_smtp.py --to you@example.com --provider smtp

Reads SMTP_* / EMAIL_PROVIDER from the environment/.env via the app settings, so there is
nothing to hardcode. Prints the provider used and the returned provider_message_id.
"""
from __future__ import annotations

import argparse
import asyncio
import uuid

from app.config import get_settings
from app.services.email_service import EmailSender


async def _run(to_email: str, provider: str | None) -> None:
    settings = get_settings()
    if provider:
        settings = settings.model_copy(update={"email_provider": provider})

    print(f"email_provider = {settings.email_provider!r}")
    print(f"smtp_host      = {settings.smtp_host!r}  port={settings.smtp_port}  tls={settings.smtp_use_tls}")
    print(f"smtp_username  = {settings.smtp_username!r}")
    print(f"smtp_from      = {settings.smtp_from_email!r}")

    sender = EmailSender(settings)
    custom_id = f"check-{uuid.uuid4()}"
    print(f"\nSending test email to {to_email} (X-Mailin-custom={custom_id}) …")

    ref = await sender.send_email_with_attachments(
        recipients=[to_email],
        subject="Brevo SMTP transport check",
        body=(
            "This is a test email sent through the app's SMTP transport.\n\n"
            "If you received it, SMTP auth + delivery work. Open it (and, if you like, "
            "click this link https://sightspectrum.com ) then check the Mail-logs UI / "
            "Brevo Transactional logs for the open/click event.\n"
        ),
        from_email=settings.smtp_from_email or None,
        reply_to=settings.smtp_from_email or None,
        as_html=True,
        custom_id=custom_id,
    )
    print(f"\nOK — sent. provider_message_id = {ref!r}")
    print("Now: open the email, then confirm the open/click reaches /outreach/brevo-events.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send one test email via the app's SMTP transport.")
    parser.add_argument("--to", required=True, help="Recipient address for the test email.")
    parser.add_argument(
        "--provider", choices=["mailjet", "smtp"], default="smtp",
        help="Override email_provider for this send (default: smtp).",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.to, args.provider))


if __name__ == "__main__":
    main()
