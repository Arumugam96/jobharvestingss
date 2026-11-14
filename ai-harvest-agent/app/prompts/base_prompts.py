"""Shared prompt utilities and template helpers."""
from __future__ import annotations

from datetime import datetime, timezone


def today_str() -> str:
    """Return today's UTC date as YYYY-MM-DD."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def truncate(text: str, max_chars: int = 4000) -> str:
    """Cap ``text`` at ``max_chars`` and append a note saying how many chars were cut.

    Text within the limit is returned unchanged.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars]"


_AGENT_PERSONA_TEMPLATE = (
    "You are an expert web harvesting AI. "
    "You are precise, methodical, and always return structured data. "
    "Today's date is {date}."
)


def agent_persona() -> str:
    """Shared system-prompt persona stamped with TODAY's UTC date.

    Built on every call, so a long-running process never sends a stale date.
    Prefer this over the AGENT_PERSONA attribute in prompt builders."""
    return _AGENT_PERSONA_TEMPLATE.format(date=today_str())


def __getattr__(name: str) -> str:
    """Module attribute hook (PEP 562): AGENT_PERSONA is rebuilt with the
    current date on every access/import instead of being frozen at import time.
    A module-level f-string that interpolates it still freezes its own copy,
    so long-lived prompt constants should call agent_persona() instead."""
    if name == "AGENT_PERSONA":
        return agent_persona()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
