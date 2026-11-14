"""Shared prompt utilities and template helpers."""
from __future__ import annotations

from datetime import datetime


def today_str() -> str:
    """Return today's UTC date as YYYY-MM-DD."""
    return datetime.utcnow().strftime("%Y-%m-%d")


def truncate(text: str, max_chars: int = 4000) -> str:
    """Cap ``text`` at ``max_chars`` and append a note saying how many chars were cut.

    Text within the limit is returned unchanged.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars]"


AGENT_PERSONA = (
    "You are an expert web harvesting AI. "
    "You are precise, methodical, and always return structured data. "
    "Today's date is {date}."
).format(date=today_str())
