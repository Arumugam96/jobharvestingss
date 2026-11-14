"""
Naukri RECRUITER session routes (see app/agents/naukri_recruiter_agent.py).

  GET  /naukri-recruiter/status     last known session state (no browser launch)
  POST /naukri-recruiter/verify     open the recruiter profile, check recruit.naukri.com
  POST /naukri-recruiter/login      open a HEADED login window (human completes login/OTP) —
                                    the Naukri Launcher when available (login mode)
  POST /naukri-recruiter/autologin  open a Launcher-issued naukri.com auto-login URL

None of these type credentials or kill any Chrome; all use the dedicated
recruiter profile (settings.naukri_recruiter_profile_dir).
"""
from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.agents.naukri_recruiter_agent import (
    NaukriRecruiterAgent,
    read_status,
    start_login_window,
    validate_autologin_url,
)
from app.core.proactor import needs_proactor, run_in_proactor
from app.services.naukri_launcher import use_launcher

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/naukri-recruiter", tags=["Naukri Recruiter Session"])


class LoginRequest(BaseModel):
    timeout_s: int = Field(600, ge=60, le=1800)


class AutologinRequest(BaseModel):
    url: str


async def _run(factory) -> dict:
    """Await the coroutine built by `factory`, on a Proactor loop thread when the current loop
    can't spawn browser subprocesses (Windows reload mode).
    """
    if needs_proactor():
        return await run_in_proactor(factory)
    return await factory()


@router.get("/status", status_code=status.HTTP_200_OK)
async def recruiter_status() -> Any:
    """Return the last recorded recruiter-session state; cheap, never launches a browser.

    Includes the profile directory, whether a login window is open and the browser mode, so the UI
    can tell whether Connect is usable.
    """
    return read_status()


@router.post("/verify", status_code=status.HTTP_200_OK)
async def recruiter_verify() -> Any:
    """Open the recruiter profile headless and check whether recruit.naukri.com is logged in.

    Returns `status` "ready" or "action_required" merged with the check result, "busy" (plus the
    current status) while a login window is open, or "failed" with a truncated error message.
    Always HTTP 200.
    """
    st = read_status()
    if st["login_window_open"]:
        return {"status": "busy", "message": "A recruiter login window is open — finish it first.", **st}
    try:
        result = await _run(lambda: NaukriRecruiterAgent(headless=True).verify())
        return {"status": "ready" if result["logged_in"] else "action_required", **result}
    except Exception as exc:
        logger.error("naukri_recruiter_verify_failed", error=str(exc))
        return {"status": "failed", "message": str(exc)[:300]}


@router.post("/login", status_code=status.HTTP_200_OK)
async def recruiter_login(body: LoginRequest | None = None) -> Any:
    """Start a headed recruiter login window in the background for a human to complete.

    Uses the Naukri Launcher when enabled ("mode": "launcher"), otherwise a plain browser window
    on recruit.naukri.com ("mode": "browser"). Returns immediately with "started" (then poll
    GET /status), or "busy" if a login window is already open. `timeout_s` defaults to 600 when
    no body is sent.
    """
    timeout_s = body.timeout_s if body else 600
    if not start_login_window(timeout_s=timeout_s):
        return {"status": "busy", "message": "A recruiter login window is already open."}
    if use_launcher():
        return {
            "status": "started",
            "mode": "launcher",
            "message": (
                "The Naukri Launcher is opening (server: watch it in the Live Browser view). "
                "Log in inside the Launcher with your credentials and OTP; the session is "
                "picked up automatically. Poll GET /naukri-recruiter/status."
            ),
            "timeout_s": timeout_s,
        }
    return {
        "status": "started",
        "mode": "browser",
        "message": (
            "A browser window is opening on recruit.naukri.com (server: watch it in the Live "
            "Browser view). Log in yourself, complete any OTP / Naukri Recruiter app approval, "
            "then poll GET /naukri-recruiter/status."
        ),
        "timeout_s": timeout_s,
    }


@router.post("/autologin", status_code=status.HTTP_200_OK)
async def recruiter_autologin(body: AutologinRequest) -> Any:
    """Open a Launcher-issued naukri.com auto-login URL in the recruiter profile.

    The URL is validated first ("failed" with the reason if rejected). Returns "busy" while a login
    window is open, otherwise "ready"/"action_required" with the post-login check, or "failed" on
    error. Always HTTP 200.
    """
    try:
        url = validate_autologin_url(body.url)
    except ValueError as exc:
        return {"status": "failed", "message": str(exc)}
    if read_status()["login_window_open"]:
        return {"status": "busy", "message": "A recruiter login window is open — finish it first."}
    try:
        result = await _run(lambda: NaukriRecruiterAgent(headless=True).autologin(url))
        return {"status": "ready" if result["logged_in"] else "action_required", **result}
    except Exception as exc:
        logger.error("naukri_recruiter_autologin_failed", error=str(exc))
        return {"status": "failed", "message": str(exc)[:300]}
