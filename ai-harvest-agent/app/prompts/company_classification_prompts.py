"""Prompt for the harvest's LLM hiring-entity check (company_classification_service).

Only companies the keyword rules left "Ambiguous" (and that have no override yet)
are sent — in ONE batched prompt of at most COMPANY_LLM_CLASSIFY_BATCH_SIZE
companies, each with one job title + a short JD snippet as context. The answer is
cached per tenant as a company_overrides row (source "llm"), so a company is asked
once.
"""
from __future__ import annotations

import re
from typing import Any

# Labels the model may answer with → stored hiring_entity (None = Unknown: the
# company stays Ambiguous, but is still cached so it isn't asked again).
LLM_LABELS: dict[str, str | None] = {
    "direct client": "Direct Client",
    "direct": "Direct Client",
    "end client": "Direct Client",
    "staffing firm": "Staffing Firm",
    "staffing": "Staffing Firm",
    "gcc": "GCC",
    "global capability center": "GCC",
    "global capability centre": "GCC",
    "unknown": None,
}

COMPANY_CLASSIFY_SYSTEM_PROMPT = (
    "You classify the companies that posted job openings (mostly India / US IT jobs) "
    "by WHO is hiring. Use your knowledge of the company plus the job context given.\n"
    "Answer for each company with exactly one label:\n"
    '  "Direct Client" — the company hires for its own product/business (end client).\n'
    '  "Staffing Firm" — a staffing / recruitment / contract-placement agency or an IT '
    "services vendor supplying contractors to other companies.\n"
    '  "GCC" — a Global Capability Center: the captive in-house India tech/ops centre '
    "of a foreign (usually multinational) company.\n"
    '  "Unknown" — you are not reasonably confident.\n'
    "Prefer Unknown over guessing. Never invent companies; answer only for the ids given."
)

COMPANY_CLASSIFY_SCHEMA = (
    '{"companies": [{"id": <integer id from the list>, '
    '"classification": "Direct Client" | "Staffing Firm" | "GCC" | "Unknown"}]}'
)

SNIPPET_CHARS = 300
_WS = re.compile(r"\s+")


def jd_snippet(text: str | None, limit: int = SNIPPET_CHARS) -> str:
    """The first ``limit`` characters of a job description, whitespace collapsed."""
    flat = _WS.sub(" ", text or "").strip()
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def build_company_classify_content(items: list[dict[str, Any]]) -> str:
    """The batch as numbered lines: ``id. Company — Job title — JD snippet``.
    ``items`` are dicts with id / company / title / snippet."""
    lines = []
    for it in items:
        parts = [f"{it['id']}. Company: {it['company']}"]
        if it.get("title"):
            parts.append(f"Job title: {it['title']}")
        if it.get("snippet"):
            parts.append(f"JD: {it['snippet']}")
        lines.append(" | ".join(parts))
    return "Companies:\n" + "\n".join(lines)


def parse_label(raw: Any) -> tuple[bool, str | None]:
    """(recognised, hiring_entity) for one model answer. Unrecognised text →
    (False, None) so the caller can skip caching it."""
    key = _WS.sub(" ", str(raw or "")).strip().lower()
    if key in LLM_LABELS:
        return True, LLM_LABELS[key]
    return False, None
