"""
Chrome Session Extractor — extracts Naukri cookies from an authenticated Chrome profile
and saves them as a Playwright-compatible storage_state JSON file.

Works while Chrome is running (uses SQLite immutable mode + Windows DPAPI via ctypes).
No pywin32 or external dependencies beyond 'cryptography' (already in requirements).

Usage:
    from app.services.chrome_session_extractor import extract_naukri_session
    path = extract_naukri_session()   # returns path to saved session file
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import os
import sqlite3
import struct
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_CHROME_USER_DATA = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
_EDGE_USER_DATA   = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"
_SESSION_PATH     = Path("data/sessions/naukri_session.json")


# ── Windows DPAPI via ctypes (no pywin32 required) ────────────────────────────

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_decrypt(cipher_bytes: bytes) -> bytes:
    """Decrypt bytes using Windows DPAPI (CryptUnprotectData)."""
    blob_in  = _DATA_BLOB(len(cipher_bytes), ctypes.cast(ctypes.c_char_p(cipher_bytes), ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None, None, None, None, 0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise RuntimeError(f"DPAPI decryption failed (error {ctypes.GetLastError()})")
    result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return result


# ── Chrome AES-256-GCM cookie decryption ─────────────────────────────────────

def _get_chrome_aes_key(user_data_dir: Path) -> bytes | None:
    """Read and decrypt Chrome's AES master key from Local State."""
    local_state_path = user_data_dir / "Local State"
    if not local_state_path.exists():
        return None
    try:
        ls = json.loads(local_state_path.read_text(encoding="utf-8"))
        enc_key_b64 = ls.get("os_crypt", {}).get("encrypted_key")
        if not enc_key_b64:
            return None
        enc_key = base64.b64decode(enc_key_b64)
        # First 5 bytes are "DPAPI" prefix marker — strip them
        if enc_key[:5] != b"DPAPI":
            return None
        return _dpapi_decrypt(enc_key[5:])
    except Exception as exc:
        logger.warning("chrome_aes_key_failed", error=str(exc))
        return None


def _decrypt_cookie_value(encrypted_value: bytes, aes_key: bytes | None) -> str:
    """Decrypt a Chrome cookie value (v10/v20 AES-GCM or legacy DPAPI)."""
    if not encrypted_value:
        return ""
    try:
        if encrypted_value[:3] in (b"v10", b"v20") and aes_key:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce      = encrypted_value[3:15]           # 12-byte nonce
            ciphertext = encrypted_value[15:]            # ciphertext + 16-byte auth tag
            return AESGCM(aes_key).decrypt(nonce, ciphertext, b"").decode("utf-8", errors="replace")
        else:
            # Legacy DPAPI-encrypted value (Chrome < v80)
            return _dpapi_decrypt(encrypted_value).decode("utf-8", errors="replace")
    except Exception:
        return ""


# ── SQLite read with lock-detection ──────────────────────────────────────────

_NAUKRI_COOKIE_QUERY = (
    "SELECT host_key, name, path, encrypted_value, expires_utc, "
    "is_httponly, is_secure, samesite "
    "FROM cookies "
    "WHERE host_key LIKE '%naukri%' OR host_key LIKE '%recruit%'"
)


def _chrome_is_running() -> bool:
    """Return True if any Chrome process is holding the cookie DB."""
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5,
        )
        return "chrome.exe" in out.stdout.lower()
    except Exception:
        return False


def _read_cookies_locked(cookies_db: Path) -> list[tuple]:
    """
    Read Chrome's cookies SQLite.

    Tries:
    1. Direct open  (works when Chrome is closed)
    2. Temp-file copy  (works when Chrome is closed)
    3. Raises a descriptive error when Chrome is running and locking the file.
    """
    # Approach 1 — direct open
    try:
        conn = sqlite3.connect(str(cookies_db), timeout=3)
        rows = conn.execute(_NAUKRI_COOKIE_QUERY).fetchall()
        conn.close()
        return rows
    except Exception:
        pass

    # Approach 2 — temp copy (Chrome not running but WAL files present)
    import shutil, tempfile
    try:
        tmp = Path(tempfile.mktemp(suffix=".cookies.db"))
        shutil.copy2(cookies_db, tmp)
        conn = sqlite3.connect(str(tmp), timeout=3)
        rows = conn.execute(_NAUKRI_COOKIE_QUERY).fetchall()
        conn.close()
        tmp.unlink(missing_ok=True)
        return rows
    except OSError as exc:
        if "32" in str(exc):  # WinError 32 — file locked by Chrome
            running = _chrome_is_running()
            raise RuntimeError(
                "Chrome is currently running and has locked the cookies database.\n"
                "To extract your Naukri session:\n"
                "  1. Close ALL Chrome windows.\n"
                "  2. Call POST /naukri-extract-session again (takes <5 seconds).\n"
                "  3. Re-open Chrome as usual.\n"
                "Chrome will NOT lose your login — the session is saved in its profile.\n"
                f"  [chrome_running={running}, cookies_db={cookies_db}]"
            ) from exc
        raise RuntimeError(f"Could not read Chrome cookies: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Could not read Chrome cookies: {exc}") from exc


# ── Chrome epoch → Unix timestamp ─────────────────────────────────────────────

def _chrome_epoch_to_unix(chrome_epoch: int) -> float:
    """Chrome stores time as microseconds since 1601-01-01. Convert to Unix seconds."""
    if not chrome_epoch:
        return -1.0
    _EPOCH_DELTA = 11_644_473_600_000_000  # microseconds between 1601-01-01 and 1970-01-01
    return (chrome_epoch - _EPOCH_DELTA) / 1_000_000


# ── SameSite int → string ─────────────────────────────────────────────────────

_SAMESITE = {-1: "Unspecified", 0: "No restriction", 1: "Lax", 2: "Strict"}


# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def extract_naukri_session(
    user_data_dir: Path | str | None = None,
    out_path: Path | str = _SESSION_PATH,
) -> str:
    """
    Extract Naukri cookies from Chrome and save as Playwright storage_state JSON.

    Parameters
    ----------
    user_data_dir : path to Chrome User Data dir (auto-detects if None)
    out_path      : where to write the session JSON (default: data/sessions/naukri_session.json)

    Returns the path to the saved session file.
    Raises RuntimeError if no Naukri cookies are found.
    """
    # ── Resolve Chrome profile path ────────────────────────────────────────────
    if user_data_dir is None:
        for candidate in (_CHROME_USER_DATA, _EDGE_USER_DATA):
            if candidate.exists():
                user_data_dir = candidate
                break
        if user_data_dir is None:
            raise RuntimeError("Could not locate Chrome / Edge user data directory")

    user_data_dir = Path(user_data_dir)
    cookies_db    = user_data_dir / "Default" / "Network" / "Cookies"

    if not cookies_db.exists():
        raise RuntimeError(f"Cookies database not found at {cookies_db}")

    logger.info(
        "chrome_session_extract_started",
        user_data_dir = str(user_data_dir),
        cookies_db    = str(cookies_db),
    )

    # ── Get AES decryption key ─────────────────────────────────────────────────
    aes_key = _get_chrome_aes_key(user_data_dir)
    if not aes_key:
        logger.warning("chrome_aes_key_missing", hint="Will attempt DPAPI fallback per cookie")

    # ── Read and decrypt Naukri cookies ───────────────────────────────────────
    raw_rows = _read_cookies_locked(cookies_db)
    if not raw_rows:
        raise RuntimeError(
            "No Naukri cookies found in Chrome profile. "
            "Make sure you are logged into recruit.naukri.com in Chrome."
        )

    pw_cookies: list[dict[str, Any]] = []
    for host_key, name, path, enc_val, expires, httponly, secure, samesite in raw_rows:
        value = _decrypt_cookie_value(enc_val, aes_key)
        if not value:
            continue
        pw_cookies.append({
            "name":     name,
            "value":    value,
            "domain":   host_key,
            "path":     path or "/",
            "expires":  _chrome_epoch_to_unix(expires),
            "httpOnly": bool(httponly),
            "secure":   bool(secure),
            "sameSite": _SAMESITE.get(samesite, "Lax"),
        })

    if not pw_cookies:
        raise RuntimeError(
            "Naukri cookies found in Chrome but could not be decrypted. "
            "Reason: wrong profile path, session expired, or DPAPI error."
        )

    session = {"cookies": pw_cookies, "origins": []}

    # ── Save to session file ───────────────────────────────────────────────────
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info(
        "naukri_session_extracted",
        cookies      = len(pw_cookies),
        saved_to     = str(out_path),
        user_data_dir= str(user_data_dir),
    )
    return str(out_path.resolve())


def session_diagnostics(user_data_dir: Path | str | None = None) -> dict[str, Any]:
    """Return diagnostic info about the Chrome session state."""
    if user_data_dir is None:
        user_data_dir = _CHROME_USER_DATA if _CHROME_USER_DATA.exists() else _EDGE_USER_DATA
    user_data_dir = Path(user_data_dir)

    chrome_exe = next(
        (p for p in [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"),
        ] if p.exists()),
        None,
    )

    cookies_db   = user_data_dir / "Default" / "Network" / "Cookies"
    chrome_is_up = _chrome_is_running()

    naukri_count     = 0
    cookies_readable = False
    try:
        rows = _read_cookies_locked(cookies_db)
        naukri_count     = len(rows)
        cookies_readable = True
    except Exception:
        pass

    return {
        "chrome_executable":     str(chrome_exe) if chrome_exe else "NOT FOUND",
        "chrome_profile_path":   str(user_data_dir),
        "profile_name":          "Default",
        "browser_type":          "Google Chrome",
        "persistent_context":    True,
        "cookies_db_exists":     cookies_db.exists(),
        "cookies_readable":      cookies_readable,
        "naukri_cookies_found":  naukri_count,
        "chrome_running":        chrome_is_up,
        "session_file_exists":   _SESSION_PATH.exists(),
        "session_file_path":     str(_SESSION_PATH),
    }
