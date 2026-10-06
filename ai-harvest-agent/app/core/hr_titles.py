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
