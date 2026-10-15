"""Shared contract between the Resdex browser runner
(app/agents/resdex_candidate_agent.run_resdex_batch) and the match service
(app/services/resdex_match_service.prepare_resdex_candidates).

Candidate field names match ResdexCandidateORM columns (app/models/resdex.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Normalized candidate keys (subset of ResdexCandidateORM columns) a card may carry.
CANDIDATE_FIELDS = (
    "candidate_name", "headline", "current_designation", "current_company",
    "previous_roles", "experience_text", "experience_years", "location",
    "preferred_locations", "skills", "education", "notice_period", "ctc",
    "last_active", "profile_url",
)


@dataclass
class ResdexTier:
    """One relaxation-ladder step: the Boolean query and the Resdex filters."""
    tier: int                        # 1-based
    query: str
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResdexJobQuery:
    job_url: str                     # normalized
    tiers: list[ResdexTier]          # tried in order until enough results


@dataclass
class ResdexCard:
    """One search-result card.

    method="json": `fields` is already normalized (CANDIDATE_FIELDS keys) from the
    intercepted Resdex results JSON. method="text": only `text` (the card's
    innerText) is known — the match service extracts fields with the LLM,
    grounded in that text. `profile_url` is absolute when known."""
    method: str                      # "json" | "text"
    fields: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    profile_url: str = ""
    raw: dict[str, Any] | None = None


@dataclass
class ResdexBatchResult:
    status: str                      # a ResdexSearchStatus value
    tier: int = 0                    # tier whose results are in `cards` (0 = none searched)
    searches_used: int = 0
    query: str = ""
    filters: dict[str, Any] = field(default_factory=dict)
    result_count: int = 0            # total results Resdex reported for that tier (if known)
    cards: list[ResdexCard] = field(default_factory=list)
    error: str | None = None
