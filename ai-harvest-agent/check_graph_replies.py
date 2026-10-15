"""
Microsoft Graph reply-mailbox pre-flight check — list (and optionally ingest) replies.
=======================================================================================

Exercises the SAME path the Graph reply poller uses (app/services/graph_reply_poller.py):
mint an app-only OAuth2 token from the GRAPH_* credentials, then read messages from the
configured reply mailbox over Microsoft Graph. Use it to confirm the whole chain works —
credentials → token → mailbox access → message mapping — without waiting for the scheduler.

It reuses the poller's own helpers (_get_token, _to_item), so a green run here means the
live poller will see the same messages.

What the exit tells you (mirrors the poller's log lines):
  * token minted + messages listed .......... everything works ✅
  * HTTP 401 ................................. credentials wrong (tenant/client/secret)
  * HTTP 403 (ErrorAccessDenied) ............ token OK but no mailbox access — grant the
                                              Mail.Read *Application* permission + admin
                                              consent, and/or fix the Application Access Policy
  * HTTP 404 ................................. GRAPH_MAILBOX address is wrong

Usage (inside the api container, where .env / DATABASE_URL resolve):
    # quickest: confirm credentials are correct AND the mailbox is reachable
    # (lists nothing; exit code 0 = PASS, 1 = FAIL):
    docker compose exec api python check_graph_replies.py --check

    # list the 10 most recent UNREAD messages (read-only, nothing is stored):
    docker compose exec api python check_graph_replies.py --unread

    # list the 20 most recent messages regardless of read state:
    docker compose exec api python check_graph_replies.py --limit 20

    # peek a different folder:
    docker compose exec api python check_graph_replies.py --folder "Junk Email"

    # ACTUALLY run one full poll cycle through the capture pipeline (writes to the DB,
    # stamps replied_at, forwards to the alert mailbox) — the real end-to-end test:
    docker compose exec api python check_graph_replies.py --ingest

Reads REPLY_TRACKING_MODE / GRAPH_* / SMTP_SENDER_MAIL from the environment via app
settings, so there is nothing to hardcode.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

import httpx

from app.config import get_settings
from app.services.graph_reply_poller import (
    _GRAPH_BASE,
    _HTTP_TIMEOUT,
    _get_token,
    _to_item,
    poll_once,
)


def _mailbox(settings) -> str:
    return (settings.graph_mailbox or settings.smtp_sender_mail or "").strip()


async def _fetch(settings, token: str, *, unread: bool, limit: int, folder: str) -> list[dict]:
    """Read the newest `limit` messages from the mailbox folder, newest first. When
    `unread`, restrict to isRead eq false. Read-only — no state is written."""
    mailbox = _mailbox(settings)
    folder = (folder or settings.graph_mailbox_folder or "inbox").strip()
    url = f"{_GRAPH_BASE}/users/{mailbox}/mailFolders/{folder}/messages"
    params = {
        "$select": "internetMessageId,subject,from,toRecipients,receivedDateTime,isRead,"
        "bodyPreview,internetMessageHeaders",
        "$orderby": "receivedDateTime desc",
        "$top": str(max(1, limit)),
    }
    if unread:
        params["$filter"] = "isRead eq false"
    headers = {
        "Authorization": f"Bearer {token}",
        "Prefer": 'outlook.body-content-type="text"',
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(url, params=params, headers=headers)
    resp.raise_for_status()
    return list(resp.json().get("value") or [])


async def _health_check(settings, folder: str) -> bool:
    """Definitive credentials+reachability probe: mint a token (proves the GRAPH_*
    credentials + tenant), then read the mailbox folder's metadata (proves the app can
    actually reach THIS mailbox with Mail.Read). Lists no messages. Returns True on PASS."""
    mailbox = _mailbox(settings)
    folder = (folder or settings.graph_mailbox_folder or "inbox").strip()
    now = datetime.now(timezone.utc)

    # 1) credentials → token
    try:
        token = await _get_token(settings, now)
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:300] if exc.response is not None else ""
        print(f"\n✗ CREDENTIALS FAIL — token request rejected (HTTP {exc.response.status_code if exc.response else '?'}).")
        print(f"  {body}")
        print("  → check GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET in .env.")
        return False
    except Exception as exc:  # network/DNS/etc.
        print(f"\n✗ CREDENTIALS FAIL — could not reach the token endpoint: {exc}")
        return False
    print("\n✓ Credentials OK — app-only token minted (tenant + client id + secret all valid).")

    # 2) token → mailbox reachability (folder metadata, incl. unread count as proof)
    url = f"{_GRAPH_BASE}/users/{mailbox}/mailFolders/{folder}"
    params = {"$select": "displayName,totalItemCount,unreadItemCount"}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        body = exc.response.text[:300] if exc.response is not None else ""
        print(f"\n✗ MAILBOX UNREACHABLE — read rejected (HTTP {status}).")
        print(f"  {body}")
        if status == 403:
            print("  → token is valid but the app can't read this mailbox. Grant the Mail.Read")
            print("    *Application* permission + admin consent, and check the Application Access Policy.")
        elif status == 404:
            print(f"  → mailbox '{mailbox}' or folder '{folder}' not found — check GRAPH_MAILBOX / folder name.")
        return False
    except Exception as exc:
        print(f"\n✗ MAILBOX UNREACHABLE — {exc}")
        return False

    data = resp.json()
    print(f"✓ Mailbox reachable — folder '{data.get('displayName', folder)}' on {mailbox}: "
          f"{data.get('totalItemCount', '?')} total, {data.get('unreadItemCount', '?')} unread.")
    print("\n✅ ALL CHECKS PASSED — credentials are correct and the mailbox is reachable.")
    return True


def _print_message(idx: int, msg: dict) -> None:
    """One message as the poller would see it — including the reply-matching headers
    (In-Reply-To / References) that get matched against a send's provider_message_id."""
    item = _to_item(msg)  # exact mapping the capture pipeline consumes
    from_addr = (item["From"] or {}).get("Address", "")
    from_name = (item["From"] or {}).get("Name", "")
    flag = "UNREAD" if not msg.get("isRead", True) else "read  "
    in_reply_to = item["In-Reply-To"] or "(none — would fall back to sender-email match)"
    preview = (msg.get("bodyPreview") or item["RawTextBody"] or "").replace("\n", " ").strip()
    if len(preview) > 140:
        preview = preview[:139] + "…"

    print(f"\n  [{idx}] {flag}  {msg.get('receivedDateTime', '')}")
    print(f"       From       : {from_name + ' ' if from_name else ''}<{from_addr}>")
    print(f"       Subject    : {item['Subject'] or '(no subject)'}")
    print(f"       In-Reply-To: {in_reply_to}")
    print(f"       Preview    : {preview or '(empty body)'}")


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    mailbox = _mailbox(settings)

    print("── Graph reply-mailbox check ─────────────────────────────────────────")
    print(f"reply_tracking_mode = {settings.reply_tracking_mode!r}  (poller runs only when 'graph')")
    print(f"tenant_id           = {settings.graph_tenant_id[:8]}…" if settings.graph_tenant_id else "tenant_id           = (MISSING)")
    print(f"client_id           = {settings.graph_client_id[:8]}…" if settings.graph_client_id else "client_id           = (MISSING)")
    print(f"client_secret       = {'set (' + str(len(settings.graph_client_secret)) + ' chars)' if settings.graph_client_secret else '(MISSING)'}")
    print(f"mailbox             = {mailbox or '(MISSING — set GRAPH_MAILBOX or SMTP_SENDER_MAIL)'}")
    print(f"folder              = {(args.folder or settings.graph_mailbox_folder or 'inbox')}")
    print("──────────────────────────────────────────────────────────────────────")

    if not (settings.graph_tenant_id and settings.graph_client_id and settings.graph_client_secret):
        print("\n✗ Missing GRAPH_* credentials — fill them in .env first.")
        return
    if not mailbox:
        print("\n✗ No mailbox configured — set GRAPH_MAILBOX (or SMTP_SENDER_MAIL).")
        return

    # ── Quick verdict: credentials correct + mailbox reachable (lists nothing) ───
    if args.check:
        ok = await _health_check(settings, args.folder)
        raise SystemExit(0 if ok else 1)

    now = datetime.now(timezone.utc)

    # ── Full end-to-end: run the real poll cycle (writes to the DB) ──────────────
    if args.ingest:
        print("\nRunning ONE real poll_once() cycle (this writes captured replies to the DB)…")
        result = await poll_once(now)
        print(f"\n  poll_once result → {result}")
        if result["received"] == 0:
            print("  (nothing new since the last high-water mark — send a fresh reply, or use the")
            print("   read-only list below to confirm the mailbox is reachable.)")
        else:
            print(f"  ✓ received={result['received']} stored={result['stored']} "
                  f"matched={result['matched']} forwarded={result['forwarded']}")
        return

    # ── Read-only: mint a token and list messages ───────────────────────────────
    try:
        token = await _get_token(settings, now)
        print("\n✓ token minted — credentials + tenant are correct.")
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:300] if exc.response is not None else ""
        print(f"\n✗ token request failed (HTTP {exc.response.status_code if exc.response else '?'}): {body}")
        print("  → credentials wrong: check GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET.")
        return

    try:
        messages = await _fetch(settings, token, unread=args.unread, limit=args.limit, folder=args.folder)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        body = exc.response.text[:300] if exc.response is not None else ""
        print(f"\n✗ mailbox read failed (HTTP {status}): {body}")
        if status == 403:
            print("  → token is valid but the app can't read this mailbox. Grant the Mail.Read")
            print("    *Application* permission + admin consent, and check the Application Access Policy.")
        elif status == 404:
            print(f"  → mailbox '{mailbox}' not found — check the GRAPH_MAILBOX address / folder name.")
        return

    scope = "UNREAD" if args.unread else "most-recent"
    print(f"✓ mailbox read OK — {len(messages)} {scope} message(s) in '{(args.folder or settings.graph_mailbox_folder or 'inbox')}':")
    if not messages:
        print("  (none — if you expected replies, send a test reply TO this mailbox and re-run.)")
    for i, msg in enumerate(messages, 1):
        _print_message(i, msg)

    print("\nRead-only — nothing was stored. Add --ingest to run the real capture pipeline.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test the Microsoft Graph reply mailbox.")
    parser.add_argument("--check", action="store_true",
                        help="verdict only: confirm credentials are correct AND the mailbox is "
                             "reachable (lists no messages); exit 0 = PASS, 1 = FAIL")
    parser.add_argument("--unread", action="store_true", help="only list unread messages (isRead eq false)")
    parser.add_argument("--limit", type=int, default=10, help="max messages to list (default 10)")
    parser.add_argument("--folder", default="", help="mailbox folder (default: GRAPH_MAILBOX_FOLDER or 'inbox')")
    parser.add_argument("--ingest", action="store_true",
                        help="run one real poll_once() cycle through the capture pipeline (WRITES to the DB)")
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
