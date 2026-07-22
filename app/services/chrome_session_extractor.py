"""
Chrome Session Extractor — extracts Naukri session from the user's real Chrome profile.

The Naukri Recruiter Launcher authenticates by opening Chrome with a one-time secureToken URL:
  https://www.naukri.com/.../autologin?secureToken=...
This sets Naukri session cookies in Chrome's Default profile.

Extraction strategy (tried in order):
1. CDP (preferred, Chrome running with --remote-debugging-port=9222)
   → Reads cookies via Chrome's native API; bypasses v20 app-bound encryption.
2. PersistentBrowserManager (Chrome must be closed)
   → Playwright Chromium opens the real Chrome User Data dir; decrypts v20 cookies natively.

Usage (from route handler):
    result = await extract_naukri_session_cdp()           # CDP path
    result = await extract_naukri_session_persistent()    # Playwright path
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_CHROME_USER_DATA = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
_SESSION_PATH     = Path("data/sessions/naukri_session.json")
_CDP_PORT         = 9222
_CHROME_EXE_PATHS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"),
    Path("C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"),
]
_LOGIN_PATHS = ("/recruit/login", "/nlogin/", "accounts.naukri.com")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_chrome_exe() -> Path | None:
    return next((p for p in _CHROME_EXE_PATHS if p.exists()), None)


def _chrome_is_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5,
        )
        return "chrome.exe" in out.stdout.lower()
    except Exception:
        return False


def _cdp_is_available(port: int = _CDP_PORT) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return True
    except Exception:
        return False


def _build_diagnostics(
    logged_in: bool | None = None,
    current_url: str = "",
    cookies_found: int = 0,
    method: str = "",
) -> dict[str, Any]:
    chrome_exe = _find_chrome_exe()
    return {
        "chrome_executable":   str(chrome_exe) if chrome_exe else "NOT FOUND",
        "chrome_profile_path": str(_CHROME_USER_DATA),
        "profile_name":        "Default",
        "browser_type":        "Google Chrome",
        "persistent_context":  True,
        "extraction_method":   method,
        "cdp_available":       _cdp_is_available(),
        "chrome_running":      _chrome_is_running(),
        "session_file_exists": _SESSION_PATH.exists(),
        "session_file_path":   str(_SESSION_PATH),
        "logged_in":           logged_in,
        "current_url":         current_url,
        "cookies_found":       cookies_found,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Path 1 — CDP extraction (Chrome running with debug port)
# ══════════════════════════════════════════════════════════════════════════════

async def extract_naukri_session_cdp(
    out_path: Path | str = _SESSION_PATH,
    cdp_port: int = _CDP_PORT,
) -> dict[str, Any]:
    """Connect to Chrome via CDP and extract Naukri session cookies."""
    from playwright.async_api import async_playwright

    out_path = Path(out_path)

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        except Exception as exc:
            raise RuntimeError(
                f"Could not connect to Chrome CDP on port {cdp_port}: {exc}"
            ) from exc

        ctx  = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()

        try:
            await page.goto("https://recruit.naukri.com/", wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(2_000)
        except Exception as exc:
            logger.warning("cdp_navigate_failed", error=str(exc))

        current_url = page.url
        logged_in   = not any(p in current_url for p in _LOGIN_PATHS)

        _log_diagnostics(logged_in, current_url, method="CDP")

        if not logged_in:
            await browser.close()
            return {
                "status":      "action_required",
                "message":     "Authenticated Chrome profile not detected",
                "reason":      "Chrome redirected to the Naukri login page — not logged into recruit.naukri.com",
                "logged_in":   False,
                "current_url": current_url,
                "cookies_found": 0,
                "next_step": [
                    "Click 'Open in Browser' in the Naukri Recruiter Launcher.",
                    "Wait for Chrome to open and show the Naukri dashboard.",
                    "Close ALL Chrome windows (Chrome saves the session to disk on close).",
                    "Call POST /naukri-extract-session again.",
                ],
                "diagnostics": _build_diagnostics(False, current_url, 0, "CDP"),
            }

        storage = await ctx.storage_state()
        naukri_cookies = [c for c in storage.get("cookies", []) if "naukri" in c.get("domain", "")]

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(storage, indent=2, ensure_ascii=False), encoding="utf-8")

        await browser.close()

        logger.info(
            "naukri_session_restored_successfully",
            message       = "Naukri session restored successfully — reusing authenticated Chrome profile",
            method        = "CDP",
            cookies_found = len(naukri_cookies),
            session_file  = str(out_path),
        )

        return {
            "status":       "ready",
            "message":      "Naukri session restored successfully — reusing authenticated Chrome profile",
            "session_file": str(out_path.resolve()),
            "logged_in":    True,
            "current_url":  current_url,
            "cookies_found": len(naukri_cookies),
            "diagnostics":  _build_diagnostics(True, current_url, len(naukri_cookies), "CDP"),
        }


# ══════════════════════════════════════════════════════════════════════════════
# Path 2 — PersistentBrowserManager extraction (Chrome must be closed)
# ══════════════════════════════════════════════════════════════════════════════

async def extract_naukri_session_persistent(
    user_data_dir: Path | str | None = None,
    out_path: Path | str = _SESSION_PATH,
) -> dict[str, Any]:
    """
    Open Playwright Chromium with the real Chrome User Data dir and extract the Naukri session.
    Playwright's Chromium decrypts v20 cookies natively (same C++ path as Chrome).
    Requires Chrome to be closed so the profile is not locked.
    """
    from app.scrapers.browser_manager import PersistentBrowserManager

    if user_data_dir is None:
        user_data_dir = _CHROME_USER_DATA
    user_data_dir = Path(user_data_dir)
    out_path      = Path(out_path)

    logger.info(
        "chrome_session_diagnostics",
        chrome_executable   = str(_find_chrome_exe() or "NOT FOUND"),
        chrome_profile_path = str(user_data_dir),
        profile_name        = "Default",
        browser_type        = "Google Chrome (via Playwright PersistentBrowserManager)",
        persistent_context  = True,
        user_data_dir       = str(user_data_dir),
    )

    async with PersistentBrowserManager(
        profile_dir = str(user_data_dir),
        headless    = True,
    ) as pbm:
        page = await pbm.new_page()

        try:
            await page.goto("https://recruit.naukri.com/", wait_until="domcontentloaded", timeout=25_000)
            await page.wait_for_timeout(2_000)
        except Exception as exc:
            logger.warning("persistent_navigate_failed", error=str(exc))

        current_url = page.url
        logged_in   = not any(p in current_url for p in _LOGIN_PATHS)

        _log_diagnostics(logged_in, current_url, method="PersistentBrowserManager")

        if not logged_in:
            return {
                "status":      "action_required",
                "message":     "Authenticated Chrome profile not detected",
                "reason":      "Playwright/Chromium opened the Chrome profile but Naukri is not logged in",
                "logged_in":   False,
                "current_url": current_url,
                "cookies_found": 0,
                "next_step": [
                    "Click 'Open in Browser' in the Naukri Recruiter Launcher.",
                    "Wait for Chrome to open and show the Naukri Recruiter dashboard.",
                    "Close ALL Chrome windows.",
                    "Call POST /naukri-extract-session again.",
                ],
                "diagnostics": _build_diagnostics(False, current_url, 0, "PersistentBrowserManager"),
            }

        # Logged in — save storage_state (cookies + origins)
        storage = await page.context.storage_state()
        naukri_cookies = [c for c in storage.get("cookies", []) if "naukri" in c.get("domain", "")]

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(storage, indent=2, ensure_ascii=False), encoding="utf-8")

        logger.info(
            "naukri_session_restored_successfully",
            message       = "Naukri session restored successfully — reusing authenticated Chrome profile",
            method        = "PersistentBrowserManager",
            cookies_found = len(naukri_cookies),
            session_file  = str(out_path),
            current_url   = current_url,
        )

        return {
            "status":       "ready",
            "message":      "Naukri session restored successfully — reusing authenticated Chrome profile",
            "session_file": str(out_path.resolve()),
            "logged_in":    True,
            "current_url":  current_url,
            "cookies_found": len(naukri_cookies),
            "diagnostics":  _build_diagnostics(True, current_url, len(naukri_cookies), "PersistentBrowserManager"),
        }


# ══════════════════════════════════════════════════════════════════════════════
# Public dispatcher — route handler calls this
# ══════════════════════════════════════════════════════════════════════════════

async def extract_naukri_session(
    user_data_dir: Path | str | None = None,
    out_path: Path | str = _SESSION_PATH,
) -> dict[str, Any]:
    """
    Dispatcher: try CDP first (Chrome running with debug port), then PersistentBrowserManager.
    If Chrome is running without a debug port, return action_required telling the user to close Chrome.
    """
    chrome_running = _chrome_is_running()
    cdp_available  = _cdp_is_available()

    if cdp_available:
        # Chrome is running with CDP — extract directly (no close required)
        logger.info("naukri_extract_via_cdp", cdp_port=_CDP_PORT)
        return await extract_naukri_session_cdp(out_path=out_path)

    if chrome_running and not cdp_available:
        # Chrome running but no debug port — must close first
        logger.warning(
            "chrome_running_no_cdp",
            hint="Chrome is open without --remote-debugging-port. User must close Chrome.",
        )
        return {
            "status":  "action_required",
            "message": "Chrome is running but remote debugging is not enabled",
            "reason":  (
                "The Naukri Launcher opened Chrome normally (without the CDP debug port). "
                "Chrome's v20 cookie encryption cannot be bypassed while it runs."
            ),
            "next_step": [
                "1. Make sure you are logged into recruit.naukri.com in Chrome (click 'Open in Browser' in the Naukri Launcher).",
                "2. Close ALL Chrome windows — Chrome writes the session to disk on close.",
                "3. Call POST /naukri-extract-session again (takes ~10 seconds).",
                "4. You may then re-open Chrome freely — enrichment will use the saved session file.",
            ],
            "diagnostics": _build_diagnostics(None, "", 0, "blocked"),
        }

    # Chrome is closed — use PersistentBrowserManager with real Chrome profile
    logger.info("naukri_extract_via_persistent_browser", user_data_dir=str(user_data_dir or _CHROME_USER_DATA))
    return await extract_naukri_session_persistent(user_data_dir=user_data_dir, out_path=out_path)


# ══════════════════════════════════════════════════════════════════════════════
# Diagnostics (sync)
# ══════════════════════════════════════════════════════════════════════════════

def session_diagnostics(user_data_dir: Path | str | None = None) -> dict[str, Any]:
    """Return sync diagnostic info about Chrome state (no browser launch needed)."""
    if user_data_dir is None:
        user_data_dir = _CHROME_USER_DATA
    user_data_dir  = Path(user_data_dir)
    chrome_is_up   = _chrome_is_running()
    cdp_available  = _cdp_is_available()
    cookies_db     = user_data_dir / "Default" / "Network" / "Cookies"
    return {
        "chrome_executable":    str(_find_chrome_exe() or "NOT FOUND"),
        "chrome_profile_path":  str(user_data_dir),
        "profile_name":         "Default",
        "browser_type":         "Google Chrome",
        "persistent_context":   True,
        "cookies_db_exists":    cookies_db.exists(),
        "chrome_running":       chrome_is_up,
        "cdp_available":        cdp_available,
        "session_file_exists":  _SESSION_PATH.exists(),
        "session_file_path":    str(_SESSION_PATH),
    }


# ── Internal helper ───────────────────────────────────────────────────────────

def _log_diagnostics(logged_in: bool, current_url: str, method: str) -> None:
    chrome_exe = _find_chrome_exe()
    logger.info(
        "chrome_session_diagnostics",
        chrome_executable   = str(chrome_exe) if chrome_exe else "NOT FOUND",
        chrome_profile_path = str(_CHROME_USER_DATA),
        profile_name        = "Default",
        browser_type        = f"Google Chrome ({method})",
        persistent_context  = True,
        current_url         = current_url,
        logged_in           = logged_in,
        session_valid       = logged_in,
    )
