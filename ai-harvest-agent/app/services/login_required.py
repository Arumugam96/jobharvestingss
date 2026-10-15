"""Global "a human must log in" registry — one place for every login-based source.

Sources that need a person (Naukri recruiter / Resdex, LinkedIn accounts, …)
raise a request here when a run discovers the session is gone; the UI polls
GET /login-required (cheap: reads one small JSON file, never a browser) and shows
an app-wide banner with an "Open Live Browser to log in" button per request.
A run that can pause for the human calls wait_for_login(), which polls the
source's OWN persisted session state (no browser) for up to settings.login_wait_s.

    raise_login_required(source, account=None, reason)   flag (idempotent; keeps `since`)
    clear(source, account=None, kind=None)               drop the flag(s)
    current()                                            pending flags for the UI
    await wait_for_login(source, account, timeout_s)     True once logged in AFTER the request

Kinds: "login" (session expired / logged out) and "human_needed" (a CAPTCHA or
challenge a person must clear in the Live Browser; expires on its own).

State lives in data/sessions/login_required.json so it survives reloads/restarts
(no DB table). Writes are atomic and guarded by a thread lock — flags are raised
from Playwright code that may run in a ProactorEventLoop worker thread (Windows
uvicorn --reload). Never raises into callers: a registry problem must not break a
harvest or an outreach pass.

Sessions are only CHECKED by the callers' existing flows (user Connect / verify /
setup, the harvest-start pre-flight, the in-harvest checks, the Resdex step).
wait_for_login() itself only reads persisted status — no periodic browser polling.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = _PROJECT_ROOT / "data" / "sessions" / "login_required.json"

KIND_LOGIN = "login"
KIND_HUMAN_NEEDED = "human_needed"

SOURCE_NAUKRI_RECRUITER = "naukri_recruiter"
SOURCE_LINKEDIN = "linkedin"

# What the UI's "Open Live Browser to log in" button does for an entry
# (POST /login-required/open dispatches on it).
ACTION_NAUKRI_RECRUITER_LOGIN = "naukri_recruiter_login"   # start the recruiter login window
ACTION_LINKEDIN_SETUP = "linkedin_setup_session"            # start /linkedin-setup-session
ACTION_LIVE_BROWSER = "live_browser"                        # a run's own browser is waiting — just watch

_lock = threading.RLock()


# ── helpers ───────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm_account(account: Any) -> str | None:
    s = str(account).strip() if account is not None else ""
    return s or None


def _key(source: str, account: str | None, kind: str) -> str:
    return f"{source}|{account or ''}|{kind}"


def source_label(source: str, account: str | None = None) -> str:
    if source == SOURCE_NAUKRI_RECRUITER:
        return "Naukri recruiter"
    if source == SOURCE_LINKEDIN:
        return f"LinkedIn account {account or '1'}"
    return (source or "Source").replace("_", " ").title()


def default_action(source: str, account: str | None, kind: str) -> dict[str, Any]:
    if kind == KIND_HUMAN_NEEDED:
        return {"type": ACTION_LIVE_BROWSER}
    if source == SOURCE_NAUKRI_RECRUITER:
        return {"type": ACTION_NAUKRI_RECRUITER_LOGIN}
    if source == SOURCE_LINKEDIN:
        return {"type": ACTION_LINKEDIN_SETUP, "account": account or "1"}
    return {"type": ACTION_LIVE_BROWSER}


def _load() -> dict[str, dict]:
    try:
        if not STATE_PATH.exists():
            return {}
        data = json.loads(STATE_PATH.read_text(encoding="utf-8") or "{}")
        entries = data.get("entries") if isinstance(data, dict) else None
        return {k: v for k, v in (entries or {}).items() if isinstance(v, dict)}
    except Exception as exc:
        logger.warning("login_required_state_read_failed", error=str(exc))
        return {}


def _save(entries: dict[str, dict]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"entries": entries}, indent=2)
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        try:
            os.replace(tmp, STATE_PATH)
        except OSError:          # Windows: target briefly open by a reader
            STATE_PATH.write_text(payload, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
    except Exception as exc:
        logger.warning("login_required_state_write_failed", error=str(exc))


def _expired(entry: dict, now: datetime) -> bool:
    exp = _parse(entry.get("expires_at"))
    return exp is not None and exp <= now


# ── public API ────────────────────────────────────────────────────────────────

def raise_login_required(
    source: str, account: Any = None, reason: str = "", *, kind: str = KIND_LOGIN,
    action: dict[str, Any] | None = None, waiting_s: float | None = None,
    expires_s: float | None = None,
) -> dict[str, Any]:
    """Flag that `source` (/`account`) needs a person. Idempotent: re-raising keeps
    the original `since` and refreshes reason/action. `waiting_s` marks a run as
    waiting (the UI shows a countdown); `expires_s` makes the flag vanish on its
    own (used for human_needed). Returns the entry ({} on failure). Never raises."""
    try:
        account = _norm_account(account)
        now = _now()
        with _lock:
            entries = _load()
            key = _key(source, account, kind)
            prev = entries.get(key) or {}
            waiting_until = _iso(now + timedelta(seconds=waiting_s)) if waiting_s else None
            if waiting_until is None:
                prev_wait = _parse(prev.get("waiting_until"))
                waiting_until = _iso(prev_wait) if prev_wait and prev_wait > now else None
            entry = {
                "source": source,
                "account": account,
                "kind": kind,
                "label": source_label(source, account),
                "reason": reason or prev.get("reason") or "",
                "since": prev.get("since") or _iso(now),
                "raised_at": _iso(now),
                "waiting_until": waiting_until,
                "expires_at": _iso(now + timedelta(seconds=expires_s)) if expires_s else None,
                "action": action or default_action(source, account, kind),
            }
            entries[key] = entry
            _save(entries)
        logger.info("login_required_raised", source=source, account=account, kind=kind,
                    reason=entry["reason"][:200], waiting_until=waiting_until)
        return entry
    except Exception as exc:
        logger.warning("login_required_raise_failed", source=source, error=str(exc))
        return {}


def clear(source: str, account: Any = None, kind: str | None = None) -> int:
    """Drop the flag(s) for `source`. account=None → every account of the source;
    kind=None → every kind. Returns how many were removed. Never raises."""
    try:
        account = _norm_account(account)
        with _lock:
            entries = _load()
            drop = [
                k for k, e in entries.items()
                if e.get("source") == source
                and (account is None or _norm_account(e.get("account")) == account)
                and (kind is None or e.get("kind") == kind)
            ]
            if not drop:
                return 0
            for k in drop:
                entries.pop(k, None)
            _save(entries)
        logger.info("login_required_cleared", source=source, account=account, kind=kind,
                    removed=len(drop))
        return len(drop)
    except Exception as exc:
        logger.warning("login_required_clear_failed", source=source, error=str(exc))
        return 0


def current() -> list[dict[str, Any]]:
    """Pending flags, oldest first (expired human_needed flags dropped)."""
    now = _now()
    with _lock:
        entries = _load()
        live = {k: e for k, e in entries.items() if not _expired(e, now)}
        if len(live) != len(entries):
            _save(live)
    return sorted(live.values(), key=lambda e: str(e.get("since") or ""))


def pending(source: str, account: Any = None, kind: str = KIND_LOGIN) -> dict[str, Any] | None:
    """The live flag for source/account/kind (account=None → any account), or None."""
    account = _norm_account(account)
    for e in current():
        if e.get("source") == source and e.get("kind") == kind and (
            account is None or _norm_account(e.get("account")) == account
        ):
            return e
    return None


def set_waiting(
    source: str, account: Any = None, until: datetime | None = None, *,
    kind: str = KIND_LOGIN, action: dict[str, Any] | None = None,
) -> None:
    """Set / clear (until=None) the "a run is waiting" deadline on an existing flag,
    optionally swapping its action. No-op when the flag is gone. Never raises."""
    try:
        account = _norm_account(account)
        with _lock:
            entries = _load()
            entry = entries.get(_key(source, account, kind))
            if entry is None:
                return
            entry["waiting_until"] = _iso(until)
            if action is not None:
                entry["action"] = action
            _save(entries)
    except Exception as exc:
        logger.warning("login_required_set_waiting_failed", source=source, error=str(exc))


# ── "is it logged in now?" — persisted state only, never a browser ────────────

def _naukri_recruiter_logged_in(account: str | None, since: datetime) -> bool:
    """read_status() says logged in, from a check newer than `since`, and no login
    window still holds the recruiter profile."""
    from app.agents.naukri_recruiter_agent import read_status

    st = read_status()
    if not st.get("logged_in") or st.get("login_window_open"):
        return False
    checked = _parse((st.get("last_check") or {}).get("checked_at"))
    return checked is not None and checked > since


def _linkedin_logged_in(account: str | None, since: datetime) -> bool:
    """/linkedin-setup-session saved the account's session file after `since`."""
    from app.services.session_manager import SessionManager

    path = SessionManager("linkedin", account or "1").session_path
    try:
        return path.exists() and path.stat().st_mtime > since.timestamp()
    except OSError:
        return False


CHECKERS: dict[str, Callable[[str | None, datetime], bool]] = {
    SOURCE_NAUKRI_RECRUITER: _naukri_recruiter_logged_in,
    SOURCE_LINKEDIN: _linkedin_logged_in,
}


def _logged_in_since(source: str, account: str | None, since: datetime, had_flag: bool) -> bool:
    checker = CHECKERS.get(source)
    try:
        if checker is not None:
            return bool(checker(account, since))
    except Exception as exc:
        logger.debug("login_required_check_failed", source=source, error=str(exc))
        return False
    # Unknown source: a flag that existed and has since been cleared = logged in.
    return had_flag and pending(source, account, KIND_LOGIN) is None


async def wait_for_login(
    source: str, account: Any = None, timeout_s: float | None = None, poll_s: float = 5.0,
    *, since: datetime | None = None, action: dict[str, Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """Wait (polling persisted status only) until `source` reports logged in AFTER
    the request was raised (`since`, default: the flag's raised_at, else now).
    Marks the flag as waiting (UI countdown) for the duration. True → logged in
    (the flag is cleared); False → timed out / should_stop() / cancelled (the flag
    stays so the user can still log in, without the countdown)."""
    from app.config import get_settings

    account = _norm_account(account)
    if timeout_s is None:
        timeout_s = float(getattr(get_settings(), "login_wait_s", 600) or 0)
    flag = pending(source, account, KIND_LOGIN)
    baseline = since or _parse((flag or {}).get("raised_at")) or _now()
    if baseline.tzinfo is None:
        baseline = baseline.replace(tzinfo=timezone.utc)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(timeout_s))
    set_waiting(source, account, _now() + timedelta(seconds=max(0.0, float(timeout_s))),
                action=action)
    logger.info("login_required_waiting", source=source, account=account, timeout_s=timeout_s)
    ok = False
    try:
        while True:
            if _logged_in_since(source, account, baseline, flag is not None):
                ok = True
                break
            if should_stop is not None:
                try:
                    if should_stop():
                        break
                except Exception:
                    pass
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(max(poll_s, 0.01), remaining))
    finally:
        if ok:
            clear(source, account, KIND_LOGIN)
        else:
            set_waiting(source, account, None)
    logger.info("login_required_wait_done", source=source, account=account, logged_in=ok)
    return ok
