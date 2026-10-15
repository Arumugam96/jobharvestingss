"""
Naukri RECRUITER session agent — capture, verify and report a logged-in
recruit.naukri.com / Resdex session. It never automates the login itself
(no credential typing, no OTP/QR/CAPTCHA solving): a human completes the login
once, this agent keeps and checks the session.

Why a DEDICATED profile (data/naukri_recruiter_profile)
──────────────────────────────────────────────────────
Cookies are context-wide. If recruiter cookies lived in the LinkedIn harvest
profile (the one browser the unified harvest runs its LinkedIn + Naukri tabs in),
the guest Naukri job-harvest tab would silently run AS the recruiter account —
an account-risk / ToS problem and a change in what Naukri serves. So the recruiter
session gets its own persistent Chrome profile, opened in its own window.

How a session gets in (all need a human once)
─────────────────────────────────────────────
1. Manual web login   POST /naukri-recruiter/login opens a HEADED window on
                      recruit.naukri.com in the recruiter profile (visible in the
                      Live Browser / noVNC view on the server, or on the desktop
                      when run locally). The user types credentials and completes
                      OTP / Naukri-Recruiter-app QR approval; the agent polls until
                      the login page is left, then records the result. Works only
                      if the account is NOT restricted to Launcher-only /
                      whitelisted-IP access.
   Launcher login     For Launcher-only accounts the same call runs the official
                      Naukri Launcher instead (app/services/naukri_launcher.py,
                      NAUKRI_RECRUITER_LOGIN_MODE): the human logs in inside it and
                      its auto-login hand-off is opened in this profile.
2. Launcher URL      POST /naukri-recruiter/autologin {url} navigates the recruiter
                      profile to a Launcher-issued naukri.com auto-login URL (one-time
                      secureToken) — only useful if the user can obtain that URL
                      before their default browser consumes it.
3. Legacy file        data/sessions/naukri_session.json (written by
                      /naukri-extract-session) is imported into the profile on verify.

Status is persisted (no cookie VALUES) at data/sessions/naukri_recruiter_status.json.

Recruiter-mode job harvest (NAUKRI_BROWSER_MODE=recruiter)
──────────────────────────────────────────────────────────
The www.naukri.com job harvest runs in THIS profile (recruiter_harvest_browser)
so job pages are browsed with the recruiter session cookies. The harvest first
calls check_recruiter_session(); a dead session fails ONLY the Naukri source with
SESSION_EXPIRED_MSG — it never falls back to guest browsing.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog

if TYPE_CHECKING:
    from app.scrapers.browser_manager import PersistentBrowserManager

logger = structlog.get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SESSIONS_DIR = _PROJECT_ROOT / "data" / "sessions"
STATUS_PATH = _SESSIONS_DIR / "naukri_recruiter_status.json"
LEGACY_SESSION_PATH = _SESSIONS_DIR / "naukri_session.json"

RECRUITER_HOME = "https://recruit.naukri.com/"
RESDEX_HOME = "https://resdex.naukri.com/"

# Logged-out recruit/resdex visits land on www.naukri.com/recruit/login?msg=TO&URL=…
# (verified live 2026-10-11); the Launcher page is login.recruit.naukri.com/Login/secureLogin.
_LOGIN_MARKERS = (
    "/recruit/login", "/nlogin", "accounts.naukri.com", "securelogin",
    "/employer-login", "msg=to&",
)
_RECRUITER_HOSTS = ("recruit.naukri.com", "resdex.naukri.com", "rms.naukri.com")

# A login form RENDERED on the page (same selectors premium_naukri_agent's
# _verify_naukri_session uses). Hidden matches (e.g. a closed change-password
# modal on the dashboard) don't count — only elements with a layout box.
LOGIN_FORM_SELECTORS = (
    'input[type="password"]',
    "form#loginform",
    ".loginPage",
    '[class*="loginForm"]',
)
_LOGIN_FORM_JS = """(sels) => sels.some((sel) => {
    let nodes;
    try { nodes = document.querySelectorAll(sel); } catch (e) { return false; }
    return Array.from(nodes).some((el) => {
        const st = window.getComputedStyle(el);
        return st.visibility !== "hidden" && st.display !== "none"
            && el.getClientRects().length > 0;
    });
})"""

# Messages the harvest reports when recruiter mode can't run (shown verbatim on
# the Naukri source in the Rule Engine "Live Harvest" card).
SESSION_EXPIRED_MSG = "Naukri recruiter session expired — reconnect via Connect in the Rule Engine"
LOGIN_WINDOW_OPEN_MSG = "Naukri recruiter login window is open — finish or close it, then retry"

_login_task: asyncio.Task | None = None


class NaukriRecruiterSessionError(RuntimeError):
    """The recruiter session a recruiter-mode Naukri harvest needs is missing or
    expired, or the login window currently holds the recruiter profile."""


def recruiter_profile_dir() -> Path:
    from app.config import get_settings
    raw = Path(get_settings().naukri_recruiter_profile_dir)
    return raw if raw.is_absolute() else _PROJECT_ROOT / raw


def is_login_url(url: str) -> bool:
    u = (url or "").lower()
    return any(m in u for m in _LOGIN_MARKERS)


def is_recruiter_url(url: str) -> bool:
    """True for a recruiter-area page that is NOT a login/landing page."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host.endswith(_RECRUITER_HOSTS) and not is_login_url(url)


def recruiter_harvest_browser(headless: bool = False, slow_mo: int = 0) -> "PersistentBrowserManager":
    """The recruiter profile as a browser: full bundled Chromium (Akamai denies
    chromium-headless-shell) with an Indian locale/timezone like a real visitor.
    Used by the recruiter-mode harvest AND the login/verify flows, so the session
    is minted and used under the same fingerprint."""
    from app.scrapers.browser_manager import PersistentBrowserManager

    return PersistentBrowserManager(
        profile_dir=recruiter_profile_dir(), headless=headless, slow_mo=slow_mo,
        channel="chromium", locale="en-IN", timezone_id="Asia/Kolkata",
    )


def login_window_open() -> bool:
    """True while the background headed login window (start_login_window) runs.
    It holds the recruiter profile, so a harvest must not start meanwhile."""
    return bool(_login_task and not _login_task.done())


async def login_form_visible(page: Any) -> bool:
    """True if a login form is rendered on `page`. An evaluation error (page
    mid-navigation, closed tab) counts as visible — never report "logged in"
    for a page we couldn't inspect."""
    try:
        return bool(await page.evaluate(_LOGIN_FORM_JS, list(LOGIN_FORM_SELECTORS)))
    except Exception as exc:
        logger.debug("naukri_recruiter_login_form_check_failed", error=str(exc))
        return True


async def is_logged_in_page(page: Any) -> bool:
    """A recruiter-area URL (not a login/landing page) with no login form shown."""
    return is_recruiter_url(page.url) and not await login_form_visible(page)


def validate_autologin_url(url: str) -> str:
    """Only https naukri.com URLs may be opened in the recruiter profile."""
    p = urlparse((url or "").strip())
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not (host == "naukri.com" or host.endswith(".naukri.com")):
        raise ValueError("autologin URL must be an https://*.naukri.com URL")
    return p.geturl()


def summarize_cookies(cookies: list[dict]) -> dict[str, Any]:
    """Names / domains / expiry of naukri cookies — never values."""
    nk = [c for c in cookies if "naukri" in (c.get("domain") or "")]
    expiries = [c["expires"] for c in nk if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0]

    def _iso(ts: float | None) -> str | None:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None

    return {
        "naukri_cookie_count": len(nk),
        "naukri_cookie_names": sorted({c.get("name", "") for c in nk}),
        "session_only_cookies": sum(1 for c in nk if not c.get("expires") or c["expires"] <= 0),
        "earliest_expiry": _iso(min(expiries)) if expiries else None,
        "latest_expiry": _iso(max(expiries)) if expiries else None,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_status(data: dict[str, Any]) -> dict[str, Any]:
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATUS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("naukri_recruiter_status_write_failed", error=str(exc))
    if data.get("logged_in"):
        # Any successful login / verify / launcher hand-off / harvest pre-check
        # settles a pending global "Naukri recruiter login needed" request.
        try:
            from app.services import login_required

            login_required.clear(login_required.SOURCE_NAUKRI_RECRUITER,
                                 kind=login_required.KIND_LOGIN)
        except Exception as exc:
            logger.debug("naukri_recruiter_login_required_clear_failed", error=str(exc))
    return data


def read_status() -> dict[str, Any]:
    """Last known state — no browser launch."""
    last: dict[str, Any] = {}
    if STATUS_PATH.exists():
        try:
            last = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except Exception:
            last = {}
    profile = recruiter_profile_dir()
    return {
        "profile_dir": str(profile),
        "profile_exists": profile.exists(),
        "legacy_session_file_exists": LEGACY_SESSION_PATH.exists(),
        "login_window_open": login_window_open(),
        "last_check": last or None,
        "logged_in": bool(last.get("logged_in")),
    }


async def _import_legacy_cookies(context: Any) -> int:
    """Copy naukri cookies from a /naukri-extract-session storage_state file into
    the recruiter profile (one-way; the file is left in place)."""
    if not LEGACY_SESSION_PATH.exists():
        return 0
    try:
        state = json.loads(LEGACY_SESSION_PATH.read_text(encoding="utf-8"))
        cookies = [c for c in state.get("cookies", []) if "naukri" in (c.get("domain") or "")]
        if cookies:
            await context.add_cookies(cookies)
        return len(cookies)
    except Exception as exc:
        logger.warning("naukri_recruiter_legacy_import_failed", error=str(exc))
        return 0


async def _check_page(page: Any, url: str = RECRUITER_HOME) -> tuple[bool, str, str]:
    await page.goto(url, wait_until="domcontentloaded", timeout=40_000)
    await page.wait_for_timeout(3_000)
    landed = page.url
    title = ""
    try:
        title = (await page.title())[:120]
    except Exception:
        pass
    return await is_logged_in_page(page), landed, title


async def check_recruiter_session(page: Any) -> tuple[bool, str]:
    """Harvest pre-check on an already-open recruiter-profile tab: load
    recruit.naukri.com, decide logged-in (recruiter URL + no login form), record
    the result in the status file and return (logged_in, landed_url). Never
    raises — any error is (False, "<reason>")."""
    try:
        logged_in, landed, title = await _check_page(page)
    except Exception as exc:
        reason = f"recruiter session check failed: {str(exc)[:200] or type(exc).__name__}"
        logger.warning("naukri_recruiter_precheck_failed", error=str(exc))
        _write_status({"checked_at": _now(), "method": "harvest_precheck", "logged_in": False,
                       "state": "error", "error": reason})
        return False, reason
    cookies: dict[str, Any] = {}
    try:
        cookies = summarize_cookies(await page.context.cookies())
    except Exception:
        pass
    _write_status({
        "checked_at": _now(), "method": "harvest_precheck", "logged_in": logged_in,
        "landed_url": landed.split("?")[0], "title": title, **cookies,
    })
    logger.info("naukri_recruiter_precheck", logged_in=logged_in, landed=landed.split("?")[0])
    return logged_in, landed


class NaukriRecruiterAgent:
    """Verify / capture a recruiter session in the dedicated recruiter profile."""

    def __init__(self, headless: bool = True) -> None:
        self._headless = headless

    async def verify(self, import_legacy: bool = True) -> dict[str, Any]:
        """Open the recruiter profile, load recruit.naukri.com and record whether
        it stays logged in. Safe to call any time (read-only on Naukri's side)."""
        async with recruiter_harvest_browser(headless=self._headless) as pbm:
            imported = await _import_legacy_cookies(pbm.context) if import_legacy else 0
            page = await pbm.acquire_page(label="naukri-recruiter")
            logged_in, landed, title = await _check_page(page)
            cookies = summarize_cookies(await pbm.context.cookies())
        result = {
            "checked_at": _now(), "method": "verify", "logged_in": logged_in,
            "landed_url": landed.split("?")[0], "title": title,
            "legacy_cookies_imported": imported, **cookies,
        }
        logger.info("naukri_recruiter_verified", logged_in=logged_in,
                    landed=result["landed_url"], cookies=cookies["naukri_cookie_count"])
        return _write_status(result)

    async def interactive_login(
        self, timeout_s: int = 600, start_url: str = RECRUITER_HOME,
    ) -> dict[str, Any]:
        """HEADED window on the recruiter login; a human logs in (incl. OTP / QR);
        poll until a recruiter page is reached or timeout. No credential autofill."""
        async with recruiter_harvest_browser(headless=False) as pbm:
            page = await pbm.acquire_page(label="naukri-recruiter-login")
            await page.goto(start_url, wait_until="domcontentloaded", timeout=40_000)
            _write_status({"checked_at": _now(), "method": "login", "logged_in": False,
                           "state": "waiting_for_user", "landed_url": page.url.split("?")[0]})
            logged_in = False
            waited = 0
            while waited < timeout_s:
                for p in pbm.context.pages:
                    if not p.is_closed() and await is_logged_in_page(p):
                        logged_in = True
                        break
                if logged_in:
                    break
                await asyncio.sleep(2)
                waited += 2
            if logged_in:
                # Settle, then confirm on a fresh load (a recruiter page can still
                # bounce to login if the session wasn't actually granted).
                await asyncio.sleep(3)
                logged_in, landed, title = await _check_page(page)
            else:
                landed, title = page.url, ""
            cookies = summarize_cookies(await pbm.context.cookies())
        result = {
            "checked_at": _now(), "method": "login", "logged_in": logged_in,
            "state": "done" if logged_in else "timed_out",
            "landed_url": landed.split("?")[0], "title": title, **cookies,
        }
        logger.info("naukri_recruiter_login_finished", logged_in=logged_in)
        return _write_status(result)

    async def autologin(self, url: str) -> dict[str, Any]:
        """Navigate the recruiter profile to a Launcher-issued auto-login URL, then verify."""
        safe = validate_autologin_url(url)
        async with recruiter_harvest_browser(headless=self._headless) as pbm:
            page = await pbm.acquire_page(label="naukri-recruiter-autologin")
            await page.goto(safe, wait_until="domcontentloaded", timeout=40_000)
            await page.wait_for_timeout(5_000)
            logged_in, landed, title = await _check_page(page)
            cookies = summarize_cookies(await pbm.context.cookies())
        result = {
            "checked_at": _now(), "method": "autologin", "logged_in": logged_in,
            "landed_url": landed.split("?")[0], "title": title, **cookies,
        }
        logger.info("naukri_recruiter_autologin_finished", logged_in=logged_in)
        return _write_status(result)


def start_login_window(timeout_s: int = 600) -> bool:
    """Start the headed login in the background (single-flight). False if one is
    already open."""
    global _login_task
    if _login_task and not _login_task.done():
        return False

    async def _run() -> None:
        from app.core.proactor import needs_proactor, run_in_proactor

        from app.services.naukri_launcher import launcher_login, use_launcher

        def _factory():
            if use_launcher():
                return launcher_login(timeout_s=timeout_s)
            return NaukriRecruiterAgent(headless=False).interactive_login(timeout_s=timeout_s)

        try:
            if needs_proactor():   # Windows + uvicorn --reload selector loop
                await run_in_proactor(_factory)
            else:
                await _factory()
        except Exception as exc:
            logger.exception("naukri_recruiter_login_failed", error=str(exc))
            _write_status({"checked_at": _now(), "method": "login", "logged_in": False,
                           "state": "error", "error": str(exc)[:300]})

    _login_task = asyncio.ensure_future(_run())
    return True


async def wait_for_login_window(timeout_s: float, poll_s: float = 2.0) -> bool:
    """Wait (polling, no browser) until the background login window finishes.
    True if it finished within `timeout_s`; the outcome is in read_status()."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while login_window_open():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(poll_s)
    return True
