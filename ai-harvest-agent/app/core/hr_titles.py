"""HR / recruiting job titles used to search a company's people on Apollo.

Lifted from the standalone enrichment_using_company.py proof script and shared by
the company+location HR-contact fallback (app/services/apollo_enrichment.py::
apollo_company_contact_fallback). Passed to Apollo's mixed_people/api_search as
``person_titles`` (with include_similar_titles=True), ordered most-canonical first so
the people Apollo returns are already roughly ranked by title relevance.
"""
from __future__ import annotations

HR_TITLES: list[str] = [
    "HR Manager",
    "Human Resources Manager",
    "Human Resources",
    "HR",
    "Talent Acquisition Manager",
    "Talent Acquisition",
    "Recruiter",
    "Technical Recruiter",
    "Recruitment Manager",
    "Recruitment",
    "Talent Acquisition Specialist",
    "HR Business Partner",
    "Human Resources Business Partner",
]

# Shortlist ranking tiers (lower = contacted first): people who actually recruit,
# then HR managers / business partners, then any other HR title.
_TIER_RECRUITING = ("talent acquisition", "recruit", "sourcing", "staffing")
_TIER_HR_LEAD = (
    "hr manager", "human resources manager", "hr business partner",
    "human resources business partner", "hrbp", "head of hr", "hr head", "people partner",
)


def hr_title_tier(title: str | None) -> int:
    """0 = recruiting / talent acquisition, 1 = HR manager / HRBP, 2 = other HR."""
    t = (title or "").lower()
    if any(k in t for k in _TIER_RECRUITING):
        return 0
    if any(k in t for k in _TIER_HR_LEAD):
        return 1
    return 2
