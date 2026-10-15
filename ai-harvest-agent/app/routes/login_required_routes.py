"""
Global "login needed" requests (see app/services/login_required.py).

  GET  /login-required          pending requests for the app-wide banner — reads one
                                small JSON file, never launches a browser (cheap to poll)
  POST /login-required/open     "Open Live Browser to log in": starts that source's
                                EXISTING login flow (Naukri recruiter → the login window
                                of POST /naukri-recruiter/login; LinkedIn → the
                                /linkedin-setup-session flow, in the background) or, when
                                a run's own browser is already waiting on the login page /
                                a challenge, just tells the UI to open the Live Browser
  POST /login-required/dismiss  hide a request (it comes back if a run raises it again)
"""
from __future__ import annotations

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, status
from pydantic import BaseModel

from app.config import get_settings
from app.services import login_required as lr

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/login-required", tags=["Login Required"])

# Background /linkedin-setup-session runs started from the banner, per account.
_linkedin_setup_tasks: dict[str, asyncio.Task] = {}


class LoginTarget(BaseModel):
    source: str
    account: str | None = None
    kind: str | None = None


def _find(source: str, account: str | None, kind: str | None) -> dict | None:
    for e in lr.current():
        if e.get("source") != source:
            continue
        if account and (e.get("account") or "") != account:
            continue
        if kind and e.get("kind") != kind:
            continue
        return e
    return None


@router.get("", status_code=status.HTTP_200_OK)
async def list_login_required() -> Any:
    return {
        "items": lr.current(),
        "now": lr._iso(lr._now()),
        "login_wait_s": int(get_settings().login_wait_s or 0),
    }


def _live_browser(message: str) -> dict:
    return {"status": "live_browser", "open_live_browser": True, "message": message}


async def _open_naukri_recruiter() -> dict:
    from app.agents.naukri_recruiter_agent import login_window_open, start_login_window
    from app.services.naukri_launcher import use_launcher

    if login_window_open():
        return {"status": "started", "open_live_browser": True, "already_open": True,
                "message": "The Naukri recruiter login window is already open — finish logging in."}
    timeout_s = max(60, int(get_settings().login_wait_s or 600))
    start_login_window(timeout_s=timeout_s)
    where = "Naukri Launcher" if use_launcher() else "recruit.naukri.com login window"
    return {"status": "started", "open_live_browser": True, "timeout_s": timeout_s,
            "message": f"The {where} is opening in the Live Browser — log in there "
                       "(OTP / app approval included). The session is picked up automatically."}


async def _open_linkedin_setup(account: str) -> dict:
    from app.services import run_guard

    task = _linkedin_setup_tasks.get(account)
    if task is not None and not task.done():
        return {"status": "started", "open_live_browser": True, "already_open": True,
                "message": f"The LinkedIn account {account} login window is already open."}
    if run_guard.active_run() is not None:
        # The running harvest holds the LinkedIn Chrome profile.
        return {"status": "busy", "open_live_browser": False,
                "message": "A harvest is running and holds the LinkedIn browser profile — log in "
                           "when it asks in the Live Browser, or after it finishes."}

    async def _run() -> None:
        from app.routes.linkedin_routes import SetupSessionRequest, setup_linkedin_session
        try:
            res = await setup_linkedin_session(SetupSessionRequest(account=account))
            logger.info("login_required_linkedin_setup_finished", account=account,
                        status=(res or {}).get("status") if isinstance(res, dict) else "response")
        except Exception as exc:
            logger.warning("login_required_linkedin_setup_failed", account=account, error=str(exc))

    _linkedin_setup_tasks[account] = asyncio.create_task(_run(), name=f"linkedin-setup-{account}")
    return {"status": "started", "open_live_browser": True,
            "message": f"A LinkedIn login window (account {account}) is opening in the Live "
                       "Browser — log in there; the session is saved automatically."}


@router.post("/open", status_code=status.HTTP_200_OK)
async def open_login(body: LoginTarget) -> Any:
    entry = _find(body.source, body.account, body.kind)
    kind = (entry or {}).get("kind") or body.kind or lr.KIND_LOGIN
    action = (entry or {}).get("action") or lr.default_action(body.source, body.account, kind)
    try:
        if action.get("type") == lr.ACTION_LIVE_BROWSER:
            return _live_browser("Open the Live Browser and complete the step shown there.")
        if action.get("type") == lr.ACTION_NAUKRI_RECRUITER_LOGIN or body.source == lr.SOURCE_NAUKRI_RECRUITER:
            return await _open_naukri_recruiter()
        if action.get("type") == lr.ACTION_LINKEDIN_SETUP or body.source == lr.SOURCE_LINKEDIN:
            account = str(action.get("account") or body.account or (entry or {}).get("account") or "1")
            if account not in ("1", "2"):
                return {"status": "failed", "open_live_browser": False,
                        "message": f"Unknown LinkedIn account '{account}'."}
            return await _open_linkedin_setup(account)
        return _live_browser("Open the Live Browser and log in there.")
    except Exception as exc:
        logger.error("login_required_open_failed", source=body.source, error=str(exc))
        return {"status": "failed", "open_live_browser": False, "message": str(exc)[:300]}


@router.post("/dismiss", status_code=status.HTTP_200_OK)
async def dismiss_login(body: LoginTarget) -> Any:
    removed = lr.clear(body.source, body.account, kind=body.kind)
    return {"status": "ok", "removed": removed}
