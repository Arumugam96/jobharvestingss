"""
Generic Playwright browser lifecycle manager with anti-detection stealth patches.

BrowserManager is an async context manager that owns one browser session.
Every module that needs a browser should acquire it through here — never
launch Playwright directly in agent or route code.

Usage::

    async with BrowserManager(headless=False) as bm:
        page = await bm.new_page()
        await page.goto("https://www.linkedin.com/jobs")
"""
from __future__ import annotations

import asyncio
import os
import random
import signal
import socket
from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

logger = structlog.get_logger(__name__)

# One asyncio.Lock per resolved profile directory, shared across every
# PersistentBrowserManager instance in this process. Chromium's own
# process-singleton check (process_singleton_posix.cc) correctly refuses to
# launch a second instance against a profile that's genuinely in use — but by
# then it's already a hard crash for whichever caller lost the race (e.g. a
# double-clicked "Run Now", or a manual run overlapping a scheduled one).
# Serializing access here means the second caller queues and waits instead.
#
# This lock only coordinates requests *within this process* though — it can't
# help if a PREVIOUS process crashed (or was force-killed) after spawning
# Chromium but before Playwright's context.close() ran, leaving an orphaned
# Chromium subprocess that will hold the lock forever since nothing will ever
# release it. _reclaim_stale_chrome_lock() below handles that separately by
# reading who actually holds the OS-level lock and killing it if it's dead
# weight from this same container.
_profile_locks: dict[str, asyncio.Lock] = {}


def _get_profile_lock(profile_key: str) -> asyncio.Lock:
    lock = _profile_locks.get(profile_key)
    if lock is None:
        lock = asyncio.Lock()
        _profile_locks[profile_key] = lock
    return lock


async def _reclaim_stale_chrome_lock(profile_dir: Path) -> None:
    """
    Chromium's SingletonLock is a symlink whose target literally encodes
    "<hostname>-<pid>" (see process_singleton_posix.cc). If that PID belongs
    to this same container/host and is dead or truly orphaned (e.g. left
    behind by a crash that killed the Python process without giving
    Playwright a chance to close the browser), kill it — otherwise the lock
    can never be released and every future launch fails forever.

    Never touches a lock written by a different host — that would mean a
    genuinely different machine/container legitimately owns the profile.
    """
    lock_path = profile_dir / "SingletonLock"
    # Path.exists() follows the symlink and checks whether its TARGET exists
    # on disk — but SingletonLock's target is just an encoded "hostname-pid"
    # string, never a real path, so exists() is always False here and this
    # function silently no-ops on every call. os.path.lexists() checks for
    # the symlink itself instead.
    if not os.path.lexists(lock_path):
        return

    try:
        target = os.readlink(str(lock_path))
        host, _, pid_str = target.rpartition("-")
        pid = int(pid_str)
    except (OSError, ValueError) as exc:
        logger.debug("chrome_profile_lock_unreadable", error=str(exc))
        return

    if host != socket.gethostname():
        logger.warning(
            "chrome_profile_lock_foreign_host",
            lock_host=host, this_host=socket.gethostname(),
            hint="SingletonLock belongs to a different host — leaving it alone.",
        )
        return

    try:
        os.kill(pid, signal.SIGKILL)
        logger.warning(
            "chrome_profile_stale_process_killed", pid=pid, profile_dir=str(profile_dir),
            hint="Killed an orphaned Chromium process holding this profile's lock.",
        )
        await asyncio.sleep(0.5)   # give the OS a moment to actually release the lock
    except ProcessLookupError:
        logger.debug("chrome_profile_stale_process_already_gone", pid=pid)
    except PermissionError as exc:
        logger.error("chrome_profile_stale_process_kill_failed", pid=pid, error=str(exc))


# ── Browser fingerprint constants ─────────────────────────────────────────────

# Fallback UA used only when the live Chromium version can't be read. Kept
# reasonably current — a stale major (the old value was Chrome/124) is itself a
# weak bot signal, and a UA that lags the real engine mismatches the Sec-CH-UA
# client hints Chromium reports.
_USER_AGENT_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
# Back-compat alias — kept for any external importer of the old name.
_USER_AGENT = _USER_AGENT_FALLBACK

_UA_TEMPLATE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/{major}.0.0.0 Safari/537.36"
)


def _build_user_agent(version: str | None) -> str:
    """Build a Windows-Chrome UA whose MAJOR version matches the live Chromium
    engine (Playwright's ``browser.version``, e.g. "140.0.7259.5"), so the UA
    agrees with the Sec-CH-UA client hints Chromium reports and never leaks a
    "HeadlessChrome" marker. Falls back to _USER_AGENT_FALLBACK when the version
    isn't a parseable number."""
    if version:
        major = version.split(".", 1)[0].strip()
        if major.isdigit():
            return _UA_TEMPLATE.format(major=major)
    return _USER_AGENT_FALLBACK


# Base viewport; jittered per context (see _jittered_viewport) so runs aren't a
# byte-identical fingerprint.
_BASE_VIEWPORT = {"width": 1366, "height": 900}


def _jittered_viewport() -> dict[str, int]:
    """A slightly randomized viewport around _BASE_VIEWPORT — a fixed viewport
    repeated across every run/session is a (weak) automation signal. Kept within
    a few dozen px so layout and selectors are unaffected."""
    return {
        "width":  _BASE_VIEWPORT["width"] + random.randint(-40, 40),
        "height": _BASE_VIEWPORT["height"] + random.randint(-30, 30),
    }

_LAUNCH_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    # Headed Chromium under Xvfb has no real GPU. Without this it tries to
    # init hardware-accelerated rendering anyway, which is unstable here —
    # observed as either a solid-black window or the browser process dying
    # outright shortly after launch (CRBrowser disconnects, driver crashes
    # with TargetClosedError). This forces the SwiftShader software-rendering
    # fallback instead. Do NOT also pass --disable-software-rasterizer — that
    # removes the SwiftShader fallback itself, leaving headed Chromium with
    # no rasterizer to paint with, which crashes it on launch every time.
    "--disable-gpu",
]

_STEALTH_SCRIPTS: list[str] = [
    # Hide navigator.webdriver
    "Object.defineProperty(navigator,'webdriver',{ get:()=>undefined });",

    # Fake a populated plugins list
    """Object.defineProperty(navigator,'plugins',{
        get:()=>({length:5,
            0:{name:'Chrome PDF Plugin'},
            1:{name:'Chrome PDF Viewer'},
            2:{name:'Native Client'},
            3:{name:'Widevine'},
            4:{name:'MetaMask'}
        })
    });""",

    # Realistic language preferences
    "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en','en-GB']});",

    # Spoof WebGL renderer (headless fingerprint)
    """const _getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p){
        if(p===37445) return 'Intel Inc.';
        if(p===37446) return 'Intel(R) Iris(TM) Plus Graphics 640';
        return _getParam.call(this,p);
    };""",

    # Remove Playwright-specific window properties
    "delete window.__playwright; delete window.__pw_manual;",

    # Consistent desktop hardware fingerprint — headless/VM Chromium can report
    # unusual (or zero) values for these, which stands out. Values chosen to
    # match a typical Windows desktop and agree with the Win-Chrome UA above.
    "Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});",
    "Object.defineProperty(navigator,'deviceMemory',{get:()=>8});",
    "Object.defineProperty(navigator,'platform',{get:()=>'Win32'});",

    # Light canvas-fingerprint noise: perturb bit 0 of a sparse pixel sample in
    # the ImageData RETURNED by getImageData (a fresh object each call — the live
    # canvas is never mutated, so rendering is unaffected). Enough to break a
    # stable canvas hash without visual or perf impact. Fully wrapped so it can
    # never throw into page scripts.
    """(() => {
        try {
            const proto = CanvasRenderingContext2D && CanvasRenderingContext2D.prototype;
            if (!proto || !proto.getImageData) return;
            const orig = proto.getImageData;
            proto.getImageData = function(...args){
                const res = orig.apply(this, args);
                try {
                    const d = res.data;
                    for (let i = 0; i < d.length; i += 1024 * 4) { d[i] = d[i] ^ 1; }
                } catch (e) { /* leave data untouched on any error */ }
                return res;
            };
        } catch (e) { /* never break the page */ }
    })();""",
]


# ══════════════════════════════════════════════════════════════════════════════
# BrowserManager
# ══════════════════════════════════════════════════════════════════════════════

class BrowserManager:
    """
    Owns one Playwright Chromium session for the lifetime of an `async with` block.

    Parameters
    ──────────
    headless    Run without a visible window (False = visible, better anti-detect).
    slow_mo     Extra ms delay between Playwright actions (0 for maximum speed).
    """

    def __init__(self, headless: bool = False, slow_mo: int = 0, storage_state: str | None = None) -> None:
        self._headless:      bool               = headless
        self._slow_mo:       int                = slow_mo
        self._storage_state: str | None         = storage_state
        self._pw:            Playwright    | None = None
        self._browser:       Browser       | None = None
        self._context:       BrowserContext | None = None

    async def __aenter__(self) -> "BrowserManager":
        self._pw = await async_playwright().start()

        self._browser = await self._pw.chromium.launch(
            headless = self._headless,
            slow_mo  = self._slow_mo,
            args     = _LAUNCH_ARGS,
        )

        ctx_kwargs: dict = dict(
            viewport            = _jittered_viewport(),
            user_agent          = _build_user_agent(self._browser.version),
            locale              = "en-US",
            timezone_id         = "Europe/London",
            color_scheme        = "light",
            java_script_enabled = True,
        )
        if self._storage_state:
            ctx_kwargs["storage_state"] = self._storage_state

        self._context = await self._browser.new_context(**ctx_kwargs)

        for script in _STEALTH_SCRIPTS:
            await self._context.add_init_script(script)

        logger.info("browser_started", headless=self._headless, slow_mo=self._slow_mo,
                    session_loaded=bool(self._storage_state))
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        logger.info("browser_stopped")

    async def new_page(self) -> Page:
        """Open and return a fresh browser tab."""
        if not self._context:
            raise RuntimeError("BrowserManager must be used as an async context manager")
        return await self._context.new_page()


# ══════════════════════════════════════════════════════════════════════════════
# PersistentBrowserManager
# ══════════════════════════════════════════════════════════════════════════════

class PersistentBrowserManager:
    """
    Playwright persistent browser context using launch_persistent_context().

    Stores cookies and session data in a dedicated Chrome profile directory so
    users only need to log in once manually.  The profile is created on first
    use; subsequent runs reuse the saved session automatically.

    Usage::

        async with PersistentBrowserManager(profile_dir="data/chrome_profile") as pbm:
            page = await pbm.new_page()
            await page.goto("https://www.linkedin.com/jobs")

    NOTE: The profile directory must not be open in another Chrome instance
    at the same time.  Use a dedicated profile for the harvest agent, not
    your default Chrome profile.
    """

    def __init__(
        self,
        profile_dir: "str | Path",
        headless:    bool = False,
        slow_mo:     int  = 0,
        channel:     str  = "chromium",
    ) -> None:
        self._profile_dir: Path              = Path(profile_dir)
        self._headless:    bool              = headless
        self._slow_mo:     int               = slow_mo
        self._channel:     str               = channel
        self._pw:          Playwright | None = None
        self._context:     BrowserContext | None = None
        self._lock                            = _get_profile_lock(str(self._profile_dir.resolve()))
        self._lock_acquired:  bool            = False

    async def __aenter__(self) -> "PersistentBrowserManager":
        if self._lock.locked():
            logger.info(
                "chrome_profile_lock_wait",
                profile_dir = str(self._profile_dir),
                hint        = "Another harvest is already using this Chrome profile — waiting for it to finish.",
            )
        await self._lock.acquire()
        self._lock_acquired = True

        try:
            self._profile_dir.mkdir(parents=True, exist_ok=True)

            # Kill a genuinely orphaned Chromium process still holding the OS-level
            # singleton lock (e.g. left behind by a crash that killed this app
            # without giving Playwright a chance to close the browser). This is
            # only reached while we hold _lock, so it can never race with a
            # concurrent request from this process — only with dead weight from
            # a previous, now-gone process.
            await _reclaim_stale_chrome_lock(self._profile_dir)

            # Clear stale lock files before launching. This is only reached
            # while we hold _lock, so any SingletonLock found here can only be
            # left by a process outside this app entirely (e.g. a previous
            # container that was killed/rebuilt) — never by a concurrent
            # request from this process, which the lock above already
            # serializes. Chromium's own process-singleton check would still
            # correctly refuse to launch against a genuinely live process
            # regardless of this cleanup.
            for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                lock_path = self._profile_dir / lock_name
                # lexists(), not exists() — these are symlinks to encoded
                # strings/socket paths that don't themselves resolve, so
                # exists() (which follows the link) is always False here.
                if os.path.lexists(lock_path):
                    try:
                        lock_path.unlink()
                        logger.info("chrome_profile_lock_cleared", lock=lock_name, profile_dir=str(self._profile_dir))
                    except Exception as exc:
                        logger.debug("chrome_profile_lock_clear_failed", lock=lock_name, error=str(exc))

            self._pw = await async_playwright().start()

            # channel="chromium" → use Playwright's bundled Chromium (not system Chrome).
            # System Chrome (channel="chrome") exits immediately via --remote-debugging-pipe
            # on Windows when the profile directory is new — confirmed by runtime test.
            self._context = await self._pw.chromium.launch_persistent_context(
                user_data_dir       = str(self._profile_dir),
                headless            = self._headless,
                slow_mo             = self._slow_mo,
                args                = [
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-infobars",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    # See _LAUNCH_ARGS above — avoids headed Chromium crashing/
                    # rendering black under Xvfb's lack of a real GPU. Do NOT
                    # add --disable-software-rasterizer alongside this.
                    "--disable-gpu",
                ],
                ignore_https_errors = True,
                viewport            = _jittered_viewport(),
                # Persistent context exposes no browser handle to read the live
                # version, so use the current fallback UA (still far better than
                # the old stale Chrome/124).
                user_agent          = _build_user_agent(None),
                locale              = "en-US",
                timezone_id         = "Europe/London",
                color_scheme        = "light",
                java_script_enabled = True,
            )

            for script in _STEALTH_SCRIPTS:
                await self._context.add_init_script(script)

            logger.info(
                "persistent_browser_started",
                profile_dir = str(self._profile_dir),
                headless    = self._headless,
                channel     = "chromium",
            )
            return self
        except Exception:
            self._lock.release()
            self._lock_acquired = False
            raise

    async def __aexit__(self, *_: Any) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        if self._lock_acquired:
            self._lock.release()
            self._lock_acquired = False
        logger.info("persistent_browser_stopped")

    async def new_page(self) -> Page:
        """Open and return a fresh browser tab."""
        if not self._context:
            raise RuntimeError("PersistentBrowserManager must be used as an async context manager")
        return await self._context.new_page()

    @property
    def context(self) -> BrowserContext:
        if not self._context:
            raise RuntimeError("PersistentBrowserManager must be used as an async context manager")
        return self._context
