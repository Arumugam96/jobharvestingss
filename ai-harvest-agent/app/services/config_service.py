"""
Config service — load and save harvest_config.json.

The config file lives at  data/config/harvest_config.json.
If the file does not exist, default values from HarvestConfig are returned
and nothing is written until an explicit save is requested.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import structlog

from app.models.harvest_models import HarvestConfig

logger = structlog.get_logger(__name__)

# Anchored to the ai-harvest-agent project root (parent of this file's
# app/services/ directory) so the config file — and the chrome_profile path
# it stores — resolve to the same place regardless of the process's CWD.
# Without this, running once from inside ai-harvest-agent/ (per the README)
# and once from a container WORKDIR that happens to differ silently reads/
# writes two different harvest_config.json files and, worse, two different
# Chrome profiles (one of which may have no saved LinkedIn session at all).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH  = _PROJECT_ROOT / "data" / "config" / "harvest_config.json"


# Matches a Windows drive-absolute path like "C:\..." or "C:/...".
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _resolve_chrome_profile(value: str) -> str:
    """Resolve the configured chrome_profile to an absolute path anchored at
    _PROJECT_ROOT.

    - A relative value is anchored under _PROJECT_ROOT.
    - A genuine same-OS absolute override is honored untouched.
    - A path that is absolute for a *different* OS than the one we're running on
      (a Windows "C:\\..."/backslash value seen inside a Linux container, or a
      POSIX "/..." value on Windows) is neither valid-absolute nor sanely
      relative here: anchoring it verbatim silently creates a garbage profile
      dir (e.g. "/app/C:\\app\\data\\chrome_profile") that has no saved session,
      which surfaces as a spurious LinkedIn re-login on every run. Detect that
      cross-platform-contaminated shape and normalize it to
      _PROJECT_ROOT/data/<basename> with a loud warning instead.
    """
    raw = value or ""
    on_windows = os.name == "nt"
    looks_windows_abs = bool(_WINDOWS_ABS_RE.match(raw)) or "\\" in raw
    looks_posix_abs   = raw.startswith("/")
    foreign = (looks_windows_abs and not on_windows) or (looks_posix_abs and on_windows)

    if foreign:
        # Final path segment regardless of separator style; keep it under data/.
        basename = re.split(r"[\\/]", raw.rstrip("\\/"))[-1] or "chrome_profile"
        fallback = _PROJECT_ROOT / "data" / basename
        logger.warning(
            "chrome_profile_foreign_path_normalized",
            configured = raw,
            normalized = str(fallback),
            hint = "chrome_profile looked absolute for a different OS "
                   "(cross-platform config contamination) — normalized to the "
                   "project data/ dir so the saved session profile is still found.",
        )
        return str(fallback)

    p = Path(raw)
    return str(p) if p.is_absolute() else str(_PROJECT_ROOT / p)


class ConfigService:
    """Load and persist the agent's harvest configuration."""

    # ── Read ──────────────────────────────────────────────────────────────────

    def load(self) -> HarvestConfig:
        """
        Read harvest_config.json and return a validated HarvestConfig.
        Falls back to default values when the file is missing or malformed.
        """
        if not _CONFIG_PATH.exists():
            logger.warning("config_not_found", path=str(_CONFIG_PATH), using="defaults")
            config = HarvestConfig()
        else:
            try:
                raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
                config = HarvestConfig(**raw)
                logger.info("config_loaded", path=str(_CONFIG_PATH))
            except Exception as exc:
                logger.error("config_load_error", path=str(_CONFIG_PATH), error=str(exc))
                config = HarvestConfig()
        config.browser.chrome_profile = _resolve_chrome_profile(config.browser.chrome_profile)
        return config

    # ── Write ─────────────────────────────────────────────────────────────────

    def save(self, config: HarvestConfig) -> None:
        """Persist a HarvestConfig to harvest_config.json (creates directories)."""
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(
            json.dumps(config.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("config_saved", path=str(_CONFIG_PATH))
