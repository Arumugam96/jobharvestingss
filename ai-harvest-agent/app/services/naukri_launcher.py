"""
Naukri Launcher login (Linux container).

Naukri moved recruiter accounts to Launcher-only login: recruit.naukri.com shows
"You now require Naukri Launcher to log in" and the web form no longer signs in.
The Launcher is Naukri's own Electron desktop app. A human logs in inside it
(credentials + OTP); it then hands the default browser a one-time auto-login URL
through the OS "open URL" call — xdg-open on Linux.

How this module uses it (POST /naukri-recruiter/login when the login mode is
"launcher"; see naukri_recruiter_agent.start_login_window):
  1. Unpack the AppImage (data/naukri_launcher/naukri-launcher.AppImage, from
     Naukri's Linux download link) into naukri_launcher_install_dir. Containers
     have no FUSE, so the embedded squashfs is unpacked with unsquashfs.
  2. Start it on the Live Browser display (DISPLAY=:99, seen through noVNC). Its
     config dir is kept under data/ so the install survives image rebuilds.
  3. The image's /usr/local/bin/xdg-open (docker/xdg-open) only records the URL
     the Launcher opens to HANDOFF_PATH. Each https *.naukri.com URL found there
     is opened in the recruiter profile (NaukriRecruiterAgent.autologin) — what a
     desktop does when that profile is the default browser.
  4. Done once the recruiter profile is logged in; the Launcher is then closed.
"""
from __future__ import annotations

import asyncio
import os
import signal
import struct
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import structlog

logger = structlog.get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
HANDOFF_PATH = _PROJECT_ROOT / "data" / "sessions" / "naukri_launcher_handoff.url"

LAUNCHER_CLOSED_MSG = "Naukri Launcher was closed before it handed over a login"
NO_HANDOFF_MSG = "Login not completed in the Naukri Launcher within the time limit"


def _resolve(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else _PROJECT_ROOT / p


def appimage_path() -> Path:
    from app.config import get_settings
    return _resolve(get_settings().naukri_launcher_appimage)


def install_dir() -> Path:
    from app.config import get_settings
    return _resolve(get_settings().naukri_launcher_install_dir)


def config_dir() -> Path:
    from app.config import get_settings
    return _resolve(get_settings().naukri_launcher_config_dir)


def launcher_available() -> bool:
    """The Launcher can run here: a Linux host with the AppImage in place."""
    return sys.platform.startswith("linux") and appimage_path().is_file()


def use_launcher() -> bool:
    from app.config import get_settings
    mode = get_settings().naukri_recruiter_login_mode
    if mode == "auto":
        return launcher_available()
    return mode == "launcher"


def squashfs_offset(header: bytes) -> int:
    """Byte offset of the squashfs image inside a type-2 AppImage: the end of the
    ELF64 runtime (section header table offset + entry size × count)."""
    if header[:4] != b"\x7fELF" or header[4] != 2:
        raise ValueError("not an ELF64 AppImage")
    (shoff,) = struct.unpack_from("<Q", header, 0x28)
    shentsize, shnum = struct.unpack_from("<HH", header, 0x3A)
    return shoff + shentsize * shnum


async def ensure_unpacked() -> Path:
    """Unpack the AppImage once (no-op when already unpacked). Returns the dir."""
    target = install_dir()
    if (target / "AppRun").is_file():
        return target
    src = appimage_path()
    if not src.is_file():
        raise FileNotFoundError(f"Naukri Launcher AppImage not found at {src}")
    with src.open("rb") as f:
        offset = squashfs_offset(f.read(64))
    tmp = target.with_name(target.name + ".tmp")
    proc = await asyncio.create_subprocess_exec(
        "unsquashfs", "-q", "-f", "-o", str(offset), "-d", str(tmp), str(src),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"unsquashfs failed: {err.decode(errors='replace')[:300]}")
    tmp.rename(target)
    logger.info("naukri_launcher_unpacked", dir=str(target))
    return target


def new_handoff_urls(lines: list[str], seen: int) -> list[str]:
    """https *.naukri.com URLs among handoff lines after the first `seen`."""
    urls = []
    for line in lines[seen:]:
        url = line.strip()
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if p.scheme == "https" and (host == "naukri.com" or host.endswith(".naukri.com")):
            urls.append(url)
    return urls


def _read_handoff() -> list[str]:
    try:
        return HANDOFF_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _clear_handoff() -> None:
    try:
        HANDOFF_PATH.unlink()
    except FileNotFoundError:
        pass


async def _ensure_dbus() -> str | None:
    """Start a session dbus-daemon if none is running. Returns the bus address."""
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if addr:
        return addr
    try:
        proc = await asyncio.create_subprocess_exec(
            "dbus-daemon", "--session", "--print-address", "--fork",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        addr = out.decode().strip()
        if addr:
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = addr
            logger.info("dbus_session_started", address=addr)
            return addr
    except Exception as exc:
        logger.debug("dbus_session_start_failed", error=str(exc))
    return None


def _find_electron_binary(app_dir: Path) -> Path:
    """Find the actual Electron binary inside the unpacked AppImage.
    AppRun is a shell wrapper that doesn't forward CLI flags, so we call
    the binary directly."""
    for candidate in app_dir.iterdir():
        if candidate.is_file() and os.access(candidate, os.X_OK) and candidate.name not in (
            "AppRun", "chrome-sandbox", "chrome_crashpad_handler",
            "libEGL.so", "libGLESv2.so", "libvk_swiftshader.so",
            "libvulkan.so.1",
        ) and not candidate.name.startswith("lib") and not candidate.suffix:
            return candidate
    return app_dir / "AppRun"


async def _start(app_dir: Path) -> asyncio.subprocess.Process:
    cfg = config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    log_path = appimage_path().parent / "launcher.log"
    binary = _find_electron_binary(app_dir)
    logger.info("naukri_launcher_binary", binary=str(binary))
    env = {
        **os.environ,
        "APPDIR": str(app_dir),
        "XDG_CONFIG_HOME": str(cfg),
        "DISPLAY": os.environ.get("DISPLAY", ":99"),
        "NAUKRI_LAUNCHER_HANDOFF": str(HANDOFF_PATH),
        # Suppress all D-Bus connection attempts — no bus in the container.
        "DBUS_SESSION_BUS_ADDRESS": "disabled:",
        "DBUS_SYSTEM_BUS_ADDRESS": "disabled:",
    }
    with log_path.open("wb") as log:
        return await asyncio.create_subprocess_exec(
            str(binary),
            "--no-sandbox",
            "--disable-gpu",
            "--disable-gpu-compositing",
            "--in-process-gpu",
            "--disable-dev-shm-usage",
            "--ozone-platform=x11",
            env=env, stdout=log, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )


async def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    for sig, wait_s in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=wait_s)
            return
        except asyncio.TimeoutError:
            continue


async def launcher_login(timeout_s: int = 600) -> dict[str, Any]:
    """Run the Launcher until its hand-off logs the recruiter profile in, the
    Launcher is closed, or `timeout_s` passes. Writes the recruiter status file."""
    from app.agents.naukri_recruiter_agent import NaukriRecruiterAgent, _now, _write_status

    app_dir = await ensure_unpacked()
    _clear_handoff()
    proc = await _start(app_dir)
    _write_status({"checked_at": _now(), "method": "launcher", "logged_in": False,
                   "state": "waiting_for_user"})
    logger.info("naukri_launcher_started", pid=proc.pid)

    result: dict[str, Any] | None = None
    seen, waited, error = 0, 0, NO_HANDOFF_MSG
    try:
        while waited < timeout_s:
            # Sampled BEFORE reading the hand-off file, so a URL written just
            # before the Launcher exits is still picked up on this pass.
            exited = proc.returncode is not None
            lines = _read_handoff()
            urls = new_handoff_urls(lines, seen)
            seen = len(lines)
            for url in urls:
                p = urlparse(url)
                logger.info("naukri_launcher_handoff", host=p.hostname, path=p.path)
                result = await NaukriRecruiterAgent(headless=False).autologin(url)
                if result.get("logged_in"):
                    break
            if result and result.get("logged_in"):
                break
            if exited:
                error = LAUNCHER_CLOSED_MSG
                break
            await asyncio.sleep(2)
            waited += 2
    finally:
        await _stop(proc)
        _clear_handoff()   # one-time tokens — don't leave them on disk

    if result and result.get("logged_in"):
        return _write_status({**result, "method": "launcher", "state": "done"})
    logger.info("naukri_launcher_login_finished", logged_in=False, reason=error)
    return _write_status({"checked_at": _now(), "method": "launcher", "logged_in": False,
                          "state": "timed_out" if error == NO_HANDOFF_MSG else "error",
                          "error": error})
