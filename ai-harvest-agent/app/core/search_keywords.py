"""Search-keyword composition shared by the job-board agents.

Every source agent turns the Rule Engine selection (typed keyword, coarse domain,
IT/Non-IT sub-categories, custom "Others" titles) into ONE search-box value. The
decision of *what* to search is identical across boards; only the syntax that
joins several terms differs:

  • LinkedIn's ``keywords=`` takes a boolean query → ``"A" OR "B"``
    (see :func:`linkedin_or_joiner`);
  • Naukri's ``k=`` takes comma-separated terms → ``A, B``
    (see :func:`comma_joiner`).

So the priority logic lives here once (:func:`compose_keyword_query`) and each
agent passes its own joiner.
"""
from __future__ import annotations

import re
from typing import Callable

# Canonical Rule Engine sub-category lists per coarse domain — MIRRORS
# harvest-agent/src/lib/ruleEngineOptions.js (IT_JOB_CATEGORIES /
# NON_IT_JOB_CATEGORIES), minus each list's "Other …" catch-all. Used ONLY to
# detect "All <domain> Jobs": when the selected job_categories cover the whole
# list, the search uses the coarse domain ("IT"/"Non-IT") instead of joining
# every category (see compose_keyword_query). This keeps all-vs-subset correct
# even against a stale saved config that still holds the full list (the frontend
# also sends [] for "all"). Keep in sync with the frontend — a drift only affects
# this detection, never filtering/classification.
IT_CATEGORY_SET: frozenset[str] = frozenset(c.lower() for c in (
    "Software Development", "Frontend Development", "Backend Development",
    "Full Stack Development", "Mobile Development", "DevOps", "Cloud Engineering",
    "Infrastructure", "System Administration", "Network Engineering",
    "Cyber Security", "Data Engineering", "Data Science", "AI / ML",
    "Database Administration", "QA / Testing", "Automation Testing", "SAP", "ERP",
    "UI / UX", "Technical Support", "Solution Architecture", "Business Intelligence",
))
NON_IT_CATEGORY_SET: frozenset[str] = frozenset(c.lower() for c in (
    "Human Resources", "Recruitment / Talent Acquisition", "Finance", "Accounting",
    "Sales", "Marketing", "Digital Marketing", "Operations", "Administration",
    "Customer Support", "Business Development", "Legal", "Procurement",
    "Supply Chain", "Logistics", "Healthcare", "Education", "Banking", "Insurance",
    "Retail", "Manufacturing", "Construction", "Real Estate", "Hospitality",
    "Media / Content",
))
CANONICAL_CATEGORY_SETS: dict[str, frozenset[str]] = {
    "IT": IT_CATEGORY_SET,
    "Non-IT": NON_IT_CATEGORY_SET,
}

# "Other IT" / "Other Non-IT" style catch-all buckets carry no searchable meaning
# — they are dropped rather than searched literally.
CATEGORY_CATCHALL_RE = re.compile(r"^\s*other\b", re.IGNORECASE)

# Max terms joined into one search value (keeps the URL sane). Must stay above the
# largest canonical category list, so a subset selection is searched in full —
# "any of the selected" — rather than silently truncated; a selection covering
# the whole list never gets here (it collapses to the coarse domain instead).
DEFAULT_TERM_CAP = 30

Joiner = Callable[[list[str]], str]


def linkedin_or_joiner(terms: list[str]) -> str:
    """LinkedIn boolean OR: each term quoted (multi-word / slash forms like
    "AI / ML" search as a phrase), joined with OR."""
    return " OR ".join(f'"{t}"' for t in terms)


def comma_joiner(terms: list[str]) -> str:
    """Naukri multi-keyword form: plain comma-separated terms (Naukri ORs them)."""
    return ", ".join(terms)


def join_terms(
    terms: list[str] | None,
    joiner: Joiner = linkedin_or_joiner,
    cap: int = DEFAULT_TERM_CAP,
) -> str:
    """Clean + cap selected category / custom-title terms, drop catch-all buckets,
    and join them with the board's `joiner`. Returns "" when nothing usable."""
    cleaned = [
        t.strip() for t in (terms or [])
        if t and t.strip() and not CATEGORY_CATCHALL_RE.match(t)
    ]
    if not cleaned:
        return ""
    return joiner(cleaned[:cap])


def categories_cover_domain(domain: str, job_categories: list[str] | None) -> bool:
    """True when the selected job_categories include EVERY canonical category for
    the coarse domain (ignoring the "Other …" catch-all) — i.e. the user chose
    "All IT Jobs" / "All Non-IT Jobs". The search must then use the coarse domain,
    not a join of every category."""
    canonical = CANONICAL_CATEGORY_SETS.get(domain)
    if not canonical or not job_categories:
        return False
    selected = {
        c.strip().lower() for c in job_categories
        if c and c.strip() and not CATEGORY_CATCHALL_RE.match(c)
    }
    return selected >= canonical


def compose_keyword_query(
    keyword: str,
    domain: str,
    job_categories: list[str] | None = None,
    custom_job_titles: list[str] | None = None,
    joiner: Joiner = linkedin_or_joiner,
) -> str:
    """The search-box value for a board.

    Priority:
      1. A user-typed keyword always wins.
      2. Otherwise, when a SUBSET of sub-categories or custom "Others" titles were
         chosen in the Rule Engine, join them (custom titles first, then IT/Non-IT
         sub-categories). "All <domain> Jobs" — an empty list OR a selection
         covering the whole canonical category set — is NOT treated as a subset;
         it falls through to the coarse domain below.
      3. Otherwise the chosen domain label is used as-is — e.g. "Data
         Engineering", "AI/ML", "IT". "Any" and "Non-IT" have no meaningful
         search term, so they contribute nothing.
    """
    keyword = (keyword or "").strip()
    if keyword:
        return keyword
    titles_query = join_terms(custom_job_titles, joiner)
    if titles_query:
        return titles_query
    if not categories_cover_domain(domain, job_categories):
        categories_query = join_terms(job_categories, joiner)
        if categories_query:
            return categories_query
    if domain in ("", "Any", "Non-IT"):
        return ""
    return domain
