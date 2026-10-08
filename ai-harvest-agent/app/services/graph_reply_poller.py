"""Microsoft Graph reply poller — ingest inbound replies from a Microsoft 365 mailbox.

M365 blocks password/app-password IMAP (Basic Auth is disabled), so this is the
required path for a Microsoft mailbox. Like the IMAP poller it needs NO DNS/MX
change — it reads a mailbox you already own — and it funnels every new message
through the EXACT SAME capture pipeline the Brevo webhook and the IMAP poller use
(outreach_reply_service.record_inbound_replies). Only the ingestion source differs:
the Microsoft Graph REST API instead of a webhook or IMAP.

Auth is app-only (OAuth2 client credentials): register an app in Microsoft Entra ID,
grant it the APPLICATION permission "Mail.Read" (admin consent), add a client secret,
and scope it to just the reply mailbox with an Exchange Application Access Policy. No
interactive login, no refresh token.

Enabled by REPLY_TRACKING_MODE="graph"; the scheduler (app/main.py lifespan) runs
poll_once() on a timer.

Idempotency — a received-time high-water mark on disk (graph_reply_state.json). Each
poll asks Graph for messages newer than the mark (minus a small overlap window to not
miss same-second arrivals), ordered oldest-first, and a Message-Id dedup drops anything
already stored. The mark advances only after a clean DB commit.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import structlog
from sqlalchemy import select

from app.config import Settings, get_settings
from app.core.dependencies import get_session_factory
from app.core.tenant_context import bind_session_tenant
from app.models.outreach_reply import OutreachReplyORM
from app.services.email_service import EmailSender
from app.services.outreach_reply_service import record_inbound_replies

logger = structlog.get_logger(__name__)

_STATE_FILENAME = "graph_reply_state.json"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TOKEN_SCOPE = "https://graph.microsoft.com/.default"
# Re-query this far before the last received mark so a reply arriving in the same
# second as the previous batch's newest message is never skipped (dedup removes the
# re-seen ones).
_OVERLAP_SECONDS = 15
_HTTP_TIMEOUT = 30.0

# Module-level token cache (app-only token is tenant-wide; good for ~60 min).
_token_cache: dict[str, Any] = {"value": "", "expires_at": datetime(1970, 1, 1, tzinfo=timezone.utc)}


# ── On-disk received-time high-water state ────────────────────────────────────

def _state_path(settings: Settings) -> str:
    base = settings.storage_local_dir or "./data/results"
    return os.path.join(base, _STATE_FILENAME)


def _load_last_received(settings: Settings) -> datetime | None:
    try:
        with open(_state_path(settings), "r", encoding="utf-8") as fh:
            raw = json.load(fh).get("last_received")
        if not raw:
            return None
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (FileNotFoundError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _save_last_received(settings: Settings, dt: datetime) -> None:
    path = _state_path(settings)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"last_received": dt.astimezone(timezone.utc).isoformat()}, fh)
    os.replace(tmp, path)


# ── OAuth2 app-only token ─────────────────────────────────────────────────────

async def _get_token(settings: Settings, now: datetime) -> str:
    """Fetch (or reuse) an app-only Graph token via client-credentials. Cached in
    memory until ~2 min before expiry."""
    if _token_cache["value"] and now < _token_cache["expires_at"]:
        return _token_cache["value"]

    url = f"https://login.microsoftonline.com/{settings.graph_tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": settings.graph_client_id,
        "client_secret": settings.graph_client_secret,
        "scope": _TOKEN_SCOPE,
        "grant_type": "client_credentials",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(url, data=data)
    resp.raise_for_status()
    payload = resp.json()
    token = payload.get("access_token", "")
    expires_in = int(payload.get("expires_in", 3600))
    _token_cache["value"] = token
    _token_cache["expires_at"] = now + timedelta(seconds=max(60, expires_in - 120))
    return token


# ── Graph message → the dict shape record_inbound_replies consumes ────────────

def _emailaddr(obj: Any) -> dict[str, str]:
    """Graph's {emailAddress:{address,name}} → the {Address,Name} shape _addr reads."""
    ea = (obj or {}).get("emailAddress") or {}
    return {"Address": (ea.get("address") or "").strip(), "Name": (ea.get("name") or "").strip()}


def _header(headers: list[dict], name: str) -> str:
    """Case-insensitive lookup in Graph's internetMessageHeaders list."""
    target = name.lower()
    for h in headers or []:
        if str(h.get("name", "")).lower() == target:
            return str(h.get("value") or "").strip()
    return ""


def _to_item(msg: dict[str, Any]) -> dict[str, Any]:
    """Map one Graph message to the loosely-typed dict the Brevo inbound-parse webhook
    posts, so record_inbound_replies consumes it unchanged."""
    headers = msg.get("internetMessageHeaders") or []
    body = msg.get("body") or {}
    content = (body.get("content") or "").strip()
    is_html = (body.get("contentType") or "").lower() == "html"
    recipients = [_emailaddr(r) for r in (msg.get("toRecipients") or [])]

    return {
        "From": _emailaddr(msg.get("from") or msg.get("sender")),
        "To": recipients or [{"Address": "", "Name": ""}],
        "Subject": (msg.get("subject") or "").strip(),
        "In-Reply-To": _header(headers, "In-Reply-To"),
        "References": _header(headers, "References"),
        # internetMessageId already carries the <...> brackets.
        "Message-Id": (msg.get("internetMessageId") or _header(headers, "Message-ID") or "").strip(),
        "Date": (msg.get("receivedDateTime") or "").strip(),
        "RawTextBody": "" if is_html else content,
        "RawHtmlBody": content if is_html else "",
    }


# ── Graph fetch ───────────────────────────────────────────────────────────────

async def _fetch_new_messages(
    settings: Settings, token: str, since: datetime | None
) -> list[dict[str, Any]]:
    """List messages in the configured folder newer than `since` (oldest first),
    capped at graph_max_messages_per_poll. Returns raw Graph message dicts."""
    mailbox = (settings.graph_mailbox or settings.smtp_sender_mail or "").strip()
    folder = (settings.graph_mailbox_folder or "inbox").strip()
    url = f"{_GRAPH_BASE}/users/{mailbox}/mailFolders/{folder}/messages"

    top = max(1, settings.graph_max_messages_per_poll)
    params = {
        "$select": "id,internetMessageId,subject,from,sender,toRecipients,receivedDateTime,body,internetMessageHeaders",
        "$orderby": "receivedDateTime asc",
        "$top": str(top),
    }
    if since is not None:
        floor = (since - timedelta(seconds=_OVERLAP_SECONDS)).astimezone(timezone.utc)
        # Graph wants the Z suffix, not +00:00.
        params["$filter"] = f"receivedDateTime gt {floor.strftime('%Y-%m-%dT%H:%M:%SZ')}"

    headers = {
        "Authorization": f"Bearer {token}",
        # Return plain-text bodies instead of HTML soup (cleaner to store + match).
        "Prefer": 'outlook.body-content-type="text"',
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(url, params=params, headers=headers)
    resp.raise_for_status()
    return list(resp.json().get("value") or [])


# ── Async orchestration (called by the scheduler) ─────────────────────────────

async def _filter_already_stored(db, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop any item whose Message-Id we've already captured (guards the overlap
    window and a re-poll after a crash between fetch and commit)."""
    ids = [i.get("Message-Id") for i in items if i.get("Message-Id")]
    if not ids:
        return items
    rows = (
        await db.execute(select(OutreachReplyORM.message_id).where(OutreachReplyORM.message_id.in_(ids)))
    ).scalars().all()
    seen = {r for r in rows if r}
    return [i for i in items if i.get("Message-Id") not in seen]


def _max_received(messages: list[dict[str, Any]]) -> datetime | None:
    best: datetime | None = None
    for m in messages:
        raw = m.get("receivedDateTime")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if best is None or dt > best:
            best = dt
    return best


async def poll_once(now: datetime | None = None) -> dict[str, int]:
    """One poll cycle: pull new messages from the M365 mailbox via Graph, record them
    through the shared reply pipeline, and advance the received-time high-water mark.
    Best-effort — any failure is logged and swallowed so an outage never breaks the
    scheduler. Returns {received, stored, matched, forwarded}."""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    empty = {"received": 0, "stored": 0, "matched": 0, "forwarded": 0}

    if settings.reply_tracking_mode != "graph":
        return empty
    if not (settings.graph_tenant_id and settings.graph_client_id and settings.graph_client_secret):
        logger.warning("graph_poll_skipped", reason="missing_credentials")
        return empty
    if not (settings.graph_mailbox or settings.smtp_sender_mail):
        logger.warning("graph_poll_skipped", reason="no_mailbox")
        return empty

    try:
        token = await _get_token(settings, now)
        since = _load_last_received(settings)
        messages = await _fetch_new_messages(settings, token, since)
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:400] if exc.response is not None else ""
        logger.warning("graph_fetch_failed", status=exc.response.status_code if exc.response else None, body=body)
        return empty
    except Exception as exc:
        logger.warning("graph_fetch_failed", error=str(exc))
        return empty

    if not messages:
        return empty

    items = [_to_item(m) for m in messages]
    high_water = _max_received(messages)

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
        logger.warning("graph_record_failed", error=str(exc), received=len(items))
        return empty

    # Advance only after a clean commit so a mid-batch failure re-fetches next tick.
    if high_water is not None:
        _save_last_received(settings, high_water)
    logger.info(
        "graph_poll_complete",
        received=len(items), **{k: result.get(k, 0) for k in ("stored", "matched", "forwarded")},
    )
    return {"received": len(items), **{k: result.get(k, 0) for k in ("stored", "matched", "forwarded")}}
