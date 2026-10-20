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

# Resdex advSearch `phoneStatus` for a Naukri-verified phone. Matched live
# (2026-10-14) against the rendered card: phoneStatus 3 ⇔ "Verified phone & email"
# (title "Verified by naukri.com"), 0 ⇔ "Verified Email" only.
NAUKRI_PHONE_VERIFIED = 3


def naukri_verified(raw: Any) -> bool:
    """True only when Naukri itself marks the candidate's phone AND email verified:
    the stored card `raw` has emailVerified is True and phoneStatus == 3 (an int,
    never a bool/str), or — text cards — Naukri's rendered verification status
    mentions both "verified phone" and "email". Anything else (email-only,
    missing flags, rows stored before the flags were captured) is False.

    NOTE: not used for display — the email/job-page "Naukri Verified" badge is
    shown on every card (RESDEX_SHOW_NAUKRI_VERIFIED). Kept as the truthful
    per-candidate rule should the badge be gated again."""
    if not isinstance(raw, dict):
        return False
    status = raw.get("phoneStatus")
    if (raw.get("emailVerified") is True and type(status) is int
            and status == NAUKRI_PHONE_VERIFIED):
        return True
    text = str(raw.get("verificationStatus") or "").lower()
    return "verified phone" in text and "email" in text


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
