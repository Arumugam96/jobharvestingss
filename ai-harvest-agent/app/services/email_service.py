"""SMTP delivery for OTP emails.

Uses the stdlib ``smtplib`` (run off the event loop via ``asyncio.to_thread``)
rather than a third-party async SMTP client, since the project has no such
dependency today and this keeps the login flow dependency-free. AuthService
depends on the ``EmailSender`` interface only — it never touches smtplib.
"""
from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

import structlog

from app.config import Settings

logger = structlog.get_logger(__name__)

OTP_EMAIL_SUBJECT = "Your Sightspectrum Login OTP"


def render_otp_email(otp: str, expiry_seconds: int) -> str:
    """Plain-text OTP email body. Edit this to restyle the email."""
    minutes = max(1, expiry_seconds // 60)
    return (
        "Hello,\n\n"
        "Your one-time password for signing in is:\n\n"
        f"{otp}\n\n"
        f"This OTP expires in {minutes} minute(s).\n\n"
        "If you did not request this OTP, please ignore this email.\n\n"
        "Regards,\n"
        "Sightspectrum"
    )


class EmailSender:
    """SMTP abstraction. AuthService only ever calls ``send_otp``."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send_otp(self, recipient: str, otp: str) -> None:
        settings = self._settings
        message = EmailMessage()
        message["Subject"] = OTP_EMAIL_SUBJECT
        message["From"] = settings.smtp_from_email
        message["To"] = recipient
        message.set_content(render_otp_email(otp, settings.otp_expiry_seconds))

        await asyncio.to_thread(self._send_sync, message)
        logger.info("otp_email_sent", recipient=recipient)

    def _send_sync(self, message: EmailMessage) -> None:
        settings = self._settings
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(message)
