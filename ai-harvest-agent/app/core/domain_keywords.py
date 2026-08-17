"""
Single source of truth for data/master/domain_keywords.json.

Consumed by two places that must agree on the same keyword vocabulary:
  • BusinessFilterService (post-scrape domain classification / annotation)
  • LinkedInAgent (search-time keyword refinement — pushes domain terms into
    the LinkedIn jobs-search query so fewer irrelevant jobs are scraped)

Kept here in app.core (not in business_filter_service) so linkedin_agent can
reuse it without importing the filter service — importing that module the
wrong direction would pull in the full master-list load and risk a cycle.
This module depends only on stdlib + structlog, so no import cycle is possible.
"""
from __future__ import annotations

import json
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_FILE = Path(__file__).resolve().parents[2] / "data" / "master" / "domain_keywords.json"


def load_domain_keywords() -> dict[str, list[str]]:
    """Load domain → keyword list from JSON, lowercasing every term.

    Returns an empty dict (never raises) when the file is missing or malformed,
    matching the prior best-effort behaviour in business_filter_service.
    """
    try:
        raw = json.loads(_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("domain_keywords_missing", file=str(_FILE))
        return {}
    except Exception as exc:  # noqa: BLE001 — best-effort load, never fatal
        logger.warning("domain_keywords_load_error", file=str(_FILE), error=str(exc))
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: [s.lower() for s in v] for k, v in raw.items()}


# Loaded once at import (same lifecycle as the old module-level load) —
# refreshed by restarting the server.
DOMAIN_KEYWORDS: dict[str, list[str]] = load_domain_keywords()

# Every bucket in domain_keywords.json is an IT/tech sub-domain, so the coarse
# "IT" group means "any of these buckets, plus the generic IT fallback label".
IT_DOMAINS: frozenset[str] = frozenset(DOMAIN_KEYWORDS.keys()) | {"IT"}


# Subset of LinkedIn's own "Industries" taxonomy (company-level job-insight tag)
# that indicates an IT employer. Coarse fallback used by infer_domain() only when
# no domain_keywords.json term matches — it can't distinguish SAP/Cloud/AI-ML,
# only IT vs. Non-IT.
_IT_INDUSTRIES: frozenset[str] = frozenset({
    "it services and it consulting", "software development",
    "information technology and services",
    "technology, information and internet",
    "computer and network security", "semiconductor manufacturing",
    "telecommunications",
})


def infer_domain(title: str, description: str, skills: list[str] | None = None,
                 domain_hint: str = "") -> str:
    """Classify a job into a domain bucket by keyword-scoring its text against
    DOMAIN_KEYWORDS; fall back to "IT" via the LinkedIn industry hint, else
    "Non-IT". Single source of truth shared by BusinessFilterService (final
    filter) and LinkedInAgent (in-scrape early-stop) so they can't drift."""
    text = f"{title} {description} {' '.join(skills or [])}".lower()
    scores = {domain: sum(1 for kw in keywords if kw in text)
              for domain, keywords in DOMAIN_KEYWORDS.items()}
    if scores and max(scores.values()) > 0:
        return max(scores, key=lambda d: scores[d])
    hint = (domain_hint or "").lower()
    if any(industry in hint for industry in _IT_INDUSTRIES):
        return "IT"
    return "Non-IT"


def domain_matches(job_domain: str, cfg_domain: str) -> bool:
    """Does a classified job domain satisfy the configured domain filter?
    "Any" → always; "IT"/"Non-IT" → set membership over IT_DOMAINS (coarse
    groups the classifier never assigns directly); otherwise exact match."""
    if cfg_domain == "Any":
        return True
    if cfg_domain == "IT":
        return job_domain in IT_DOMAINS
    if cfg_domain == "Non-IT":
        return job_domain not in IT_DOMAINS
    return job_domain == cfg_domain


# How many buckets the coarse "IT" umbrella samples for its search OR-set — one
# leading term per bucket, so this bounds how many IT sub-domains are represented.
_IT_UMBRELLA_CAP = 10


def domain_search_terms(domain: str, cap: int = 5) -> list[str]:
    """Representative LinkedIn search terms for a domain choice.

    • Specific bucket (Cloud, SAP, AI/ML, …): the first `cap` keywords in JSON
      order — files are curated so leading terms are the highest-signal
      (role + core-tech) ones.
    • "IT" (umbrella over every bucket): the leading term of each bucket, so the
      coarse "IT" choice still narrows the search to tech roles instead of
      fetching the whole board. Built from DOMAIN_KEYWORDS so it stays in sync.
    • Any / Non-IT / any label with no bucket: empty list — "don't refine the
      search, fall back to the post-scrape check".
    """
    if domain == "IT":
        umbrella = [terms[0] for terms in DOMAIN_KEYWORDS.values() if terms]
        return umbrella[:_IT_UMBRELLA_CAP]
    return DOMAIN_KEYWORDS.get(domain, [])[:cap]
