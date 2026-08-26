"""Active-client classification — decides whether a company we're about to email
is an existing Sightspectrum client (data/master/ss_active_clients.json) or a
new prospect, so the outreach flow (app/services/outreach_service.py) picks the
right message template.

Mirrors the master-list loader in business_filter_service.py; normalization
matches recruiter_service._slug (lowercase, strip legal suffixes + punctuation)
so "SLK Software Pvt Ltd" resolves to the same key as the list's "Slk Software".
Deliberately conservative — industry words (software/analytics/…) are NOT
stripped, and short list codes (e.g. "ZS", "CTS") match only exactly, so
"Tiger Global" never collides with "Tiger Analytics".
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_MASTER_DIR = Path(__file__).resolve().parents[2] / "data" / "master"
_ACTIVE_CLIENTS_FILE = "ss_active_clients.json"

_LEGAL_SUFFIXES = re.compile(r"\b(inc|incorporated|ltd|limited|llc|llp|pvt|private|corp|corporation|co)\b")
_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def _normalize(name: str) -> str:
    text = (name or "").lower()
    text = _LEGAL_SUFFIXES.sub("", text)
    text = _PUNCTUATION.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


def _load_active_clients() -> frozenset[str]:
    path = _MASTER_DIR / _ACTIVE_CLIENTS_FILE
    if not path.exists():
        logger.warning("active_clients_file_missing", file=str(path))
        return frozenset()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("active_clients_load_error", file=str(path), error=str(exc))
        return frozenset()
    names = data.get("companies", []) if isinstance(data, dict) else data
    return frozenset(n for raw in names if isinstance(raw, str) and (n := _normalize(raw)))


# Loaded once at import — refreshed by restarting the server (same lifecycle as
# business_filter_service's master lists).
_ACTIVE_CLIENTS: frozenset[str] = _load_active_clients()


def classify_client(company: str) -> str:
    """Return "active" | "new" | "unknown" for a company name.

    "unknown" — empty/unresolvable company (nothing to personalize on).
    "active"  — normalized name matches the active-clients master list.
    "new"     — a real company name not on the list.
    """
    norm = _normalize(company)
    if not norm:
        return "unknown"
    if norm in _ACTIVE_CLIENTS:
        return "active"
    # Phrase-boundary containment for multi-word/long entries so "Slk Software"
    # still matches "SLK Software India"; short codes require the exact hit above.
    padded = f" {norm} "
    for entry in _ACTIVE_CLIENTS:
        if len(entry) >= 5 and f" {entry} " in padded:
            return "active"
    return "new"
