"""IMAP reply poller — ingest inbound prospect replies by POLLING a mailbox.

This is the DNS-free alternative to the Brevo inbound-parse webhook
(app/routes/outreach_routes.py :: /outreach/inbound-reply). Brevo inbound-parse
needs a dedicated MX record pointed at Brevo's mail servers; when the domain can't
take another MX record, we instead read the reply mailbox directly over IMAP and
feed each new message through the EXACT SAME capture pipeline the webhook uses
(outreach_reply_service.record_inbound_replies) — matching, storage, replied_at
stamping, and the forward-to-alert-mailbox are all unchanged. Only the ingestion
source differs: pull (IMAP) instead of push (webhook).

Enabled by REPLY_TRACKING_MODE="imap"; the scheduler (app/main.py lifespan) runs
poll_once() on an interval. Connection + fetch are blocking imaplib calls, so they
run in a worker thread (asyncio.to_thread); the async DB write happens back on the
event loop.

Idempotency — we keep a per-mailbox UID high-water mark on disk
(imap_reply_state.json). Each poll fetches only messages with UID greater than the
last processed one, so a human opening a message in webmail (which would clear the
\\Seen flag approach) never causes a miss, and a reply is never ingested twice. A
UIDVALIDITY change (mailbox rebuilt/renamed by the provider) resets the mark.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import json
import os
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from typing import Any

import structlog
from sqlalchemy import select

from app.config import Settings, get_settings
from app.core.dependencies import get_session_factory
from app.core.tenant_context import bind_session_tenant
from app.models.outreach_reply import OutreachReplyORM
from app.services.email_service import EmailSender
from app.services.outreach_reply_service import record_inbound_replies

logger = structlog.get_logger(__name__)

_STATE_FILENAME = "imap_reply_state.json"


# ── On-disk UID high-water state ──────────────────────────────────────────────

def _state_path(settings: Settings) -> str:
    base = settings.storage_local_dir or "./data/results"
    return os.path.join(base, _STATE_FILENAME)


def _load_state(settings: Settings) -> dict[str, int]:
    try:
        with open(_state_path(settings), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {
            "uidvalidity": int(data.get("uidvalidity", 0)),
            "last_uid": int(data.get("last_uid", 0)),
        }
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
        return {"uidvalidity": 0, "last_uid": 0}


def _save_state(settings: Settings, uidvalidity: int, last_uid: int) -> None:
    path = _state_path(settings)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"uidvalidity": uidvalidity, "last_uid": last_uid}, fh)
    os.replace(tmp, path)  # atomic on POSIX + Windows


# ── MIME → the dict shape record_inbound_replies consumes ─────────────────────

def _decode(value: str | None) -> str:
    """Decode an RFC 2047 encoded-word header (e.g. =?UTF-8?B?...?=) to plain text."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def _best_bodies(msg: Message) -> tuple[str, str]:
    """Return (text_plain, text_html) for the message, walking multipart alternatives
    and skipping attachments. Either may be empty."""
    text = html = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                chunk = payload.decode(charset, errors="replace")
            except (LookupError, TypeError):
                chunk = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain" and not text:
                text = chunk
            elif ctype == "text/html" and not html:
                html = chunk
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            try:
                body = payload.decode(charset, errors="replace")
            except (LookupError, TypeError):
                body = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html = body
            else:
                text = body
    return text.strip(), html.strip()


def _to_item(raw_bytes: bytes) -> dict[str, Any]:
    """Parse one raw RFC 822 message into the same loosely-typed dict the Brevo
    inbound-parse webhook posts, so record_inbound_replies can consume it unchanged.
    Keys mirror Brevo's: From/To as {Address,Name}, the threading headers top-level,
    and RawTextBody/RawHtmlBody for the body."""
    msg = email.message_from_bytes(raw_bytes)

    from_addr, from_name = parseaddr(msg.get("From", ""))
    to_addr, to_name = parseaddr(msg.get("To", ""))
    text, html = _best_bodies(msg)

    return {
        "From": {"Address": from_addr, "Name": _decode(from_name)},
        "To": [{"Address": to_addr, "Name": _decode(to_name)}],
        "Subject": _decode(msg.get("Subject", "")),
        # record_inbound_replies reads these top-level (case-insensitive, both spellings).
        "In-Reply-To": (msg.get("In-Reply-To") or "").strip(),
        "References": (msg.get("References") or "").strip(),
        "Message-Id": (msg.get("Message-ID") or msg.get("Message-Id") or "").strip(),
        "Date": (msg.get("Date") or "").strip(),
        "RawTextBody": text,
        "RawHtmlBody": html,
    }


# ── Blocking IMAP fetch (runs in a worker thread) ─────────────────────────────

def _fetch_new_messages(settings: Settings) -> tuple[list[dict[str, Any]], int, int]:
    """Connect, select the mailbox, and fetch every message with UID greater than the
    stored high-water mark. Returns (items, uidvalidity, max_uid_fetched). Pure read +
    parse — no DB, no state write (the caller persists the mark only after a successful
    DB commit). Blocking; call via asyncio.to_thread."""
    state = _load_state(settings)
    conn: imaplib.IMAP4 | imaplib.IMAP4_SSL
    if settings.imap_use_ssl:
        conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    else:
        conn = imaplib.IMAP4(settings.imap_host, settings.imap_port)
        try:
            conn.starttls()
        except (imaplib.IMAP4.error, OSError):
            pass  # server may not offer STARTTLS on 143; proceed on the plain channel

    try:
        conn.login(settings.imap_username, settings.imap_password)
        # readonly=True: never touch \Seen (humans still see replies as unread in
        # webmail) — our idempotency rides on the UID high-water mark, not flags.
        conn.select(settings.imap_mailbox, readonly=True)

        # UIDVALIDITY comes back as an untagged response to SELECT. (STATUS is not
        # permitted on the currently-selected mailbox, so we read it from here.)
        uidvalidity = 0
        _typ, uv = conn.response("UIDVALIDITY")
        if uv and uv[0]:
            try:
                uidvalidity = int(uv[0])
            except (ValueError, TypeError):
                uidvalidity = 0

        # A UIDVALIDITY change means UIDs were renumbered — the old mark is meaningless.
        last_uid = state["last_uid"] if uidvalidity == state["uidvalidity"] else 0

        typ, data = conn.uid("search", None, f"UID {last_uid + 1}:*")
        if typ != "OK" or not data or not data[0]:
            return [], uidvalidity, last_uid

        # "<n>:*" always returns at least the highest message even when its UID < n,
        # so filter client-side to strictly-greater UIDs.
        uids = sorted({int(x) for x in data[0].split()})
        uids = [u for u in uids if u > last_uid]
        if not uids:
            return [], uidvalidity, last_uid

        cap = max(1, settings.imap_max_messages_per_poll)
        uids = uids[:cap]

        items: list[dict[str, Any]] = []
        max_uid = last_uid
        for uid in uids:
            typ, fetched = conn.uid("fetch", str(uid), "(RFC822)")
            if typ != "OK" or not fetched:
                continue
            raw = next(
                (part[1] for part in fetched if isinstance(part, tuple) and part[1]),
                None,
            )
            if not raw:
                continue
            try:
                items.append(_to_item(raw))
            except Exception as exc:  # one unparseable message must not sink the batch
                logger.warning("imap_message_parse_failed", uid=uid, error=str(exc))
            max_uid = max(max_uid, uid)
        return items, uidvalidity, max_uid
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ── Async orchestration (called by the scheduler) ─────────────────────────────

async def _filter_already_stored(db, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop any item whose Message-Id we've already captured (belt-and-suspenders on
    top of the UID mark — guards a re-poll after a crash between fetch and commit)."""
    ids = [i.get("Message-Id") for i in items if i.get("Message-Id")]
    if not ids:
        return items
    rows = (
        await db.execute(select(OutreachReplyORM.message_id).where(OutreachReplyORM.message_id.in_(ids)))
    ).scalars().all()
    seen = {r for r in rows if r}
    return [i for i in items if i.get("Message-Id") not in seen]


async def poll_once() -> dict[str, int]:
    """One poll cycle: fetch new messages over IMAP, record them through the shared
    reply pipeline, and advance the UID high-water mark. Best-effort — any failure is
    logged and swallowed so a mail-server hiccup never breaks the scheduler. Returns
    {received, stored, matched, forwarded}."""
    settings = get_settings()
    empty = {"received": 0, "stored": 0, "matched": 0, "forwarded": 0}

    if settings.reply_tracking_mode != "imap":
        return empty
    if not (settings.imap_host and settings.imap_username and settings.imap_password):
        logger.warning("imap_poll_skipped", reason="missing_credentials")
        return empty

    try:
        items, uidvalidity, max_uid = await asyncio.to_thread(_fetch_new_messages, settings)
    except Exception as exc:
        logger.warning("imap_fetch_failed", error=str(exc))
        return empty

    if not items:
        # Still advance the mark so a bumped UIDVALIDITY / empty poll is recorded.
        _save_state(settings, uidvalidity, max_uid)
        return empty

    session_factory = get_session_factory(settings)
    try:
        async with session_factory() as db:
            await bind_session_tenant(db)  # all-access: match replies across every tenant
            fresh = await _filter_already_stored(db, items)
            result = empty
            if fresh:
                result = await record_inbound_replies(db, {"items": fresh}, EmailSender(settings))
            await db.commit()
    except Exception as exc:
        logger.warning("imap_record_failed", error=str(exc), received=len(items))
        return empty

    # Advance only after a clean commit, so a mid-batch failure re-fetches next tick.
    _save_state(settings, uidvalidity, max_uid)
    logger.info("imap_poll_complete", received=len(items), **{k: result.get(k, 0) for k in ("stored", "matched", "forwarded")})
    return {"received": len(items), **{k: result.get(k, 0) for k in ("stored", "matched", "forwarded")}}
