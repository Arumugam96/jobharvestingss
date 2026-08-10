"""
Config service — load and save harvest_config.json.

The config file lives at  data/config/harvest_config.json.
If the file does not exist, default values from HarvestConfig are returned
and nothing is written until an explicit save is requested.
"""
from __future__ import annotations

import json
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


def _resolve_chrome_profile(value: str) -> str:
    """Anchor a relative chrome_profile path to _PROJECT_ROOT; leave an
    already-absolute path (e.g. an operator-supplied override) untouched."""
    p = Path(value)
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
