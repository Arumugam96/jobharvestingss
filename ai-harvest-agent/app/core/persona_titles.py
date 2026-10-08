"""B2B persona → Apollo job-title lists for the Recruiter Contact Finder.

The "Who to look up" picker on the Contact Finder page spans the whole B2B
spectrum, but Recruiting & HR is the default (and most-used) persona — so that key
simply reuses the shared HR_TITLES list (app/core/hr_titles.py) that the harvest's
company-contact fallback already uses. Every list is passed to Apollo's
``search_people(person_titles=…)`` (with include_similar_titles=True), ordered
most-canonical first so returned people are already roughly title-ranked.

Resolve a persona with ``titles_for_persona`` — it is tolerant of the exact option
labels the UI sends, and falls back to Recruiting & HR for anything unknown.
"""
from __future__ import annotations

from app.core.hr_titles import HR_TITLES

# Keyed by the lowercased option label the UI sends (see the page's persona <select>).
PERSONA_TITLES: dict[str, list[str]] = {
    "recruiters & talent acquisition": HR_TITLES,
    "hr & people ops": [
        "HR Manager", "Human Resources", "People Operations", "People Ops",
        "HR Business Partner", "Head of People", "Chief People Officer", "CHRO",
    ],
    "hiring managers": [
        "Hiring Manager", "Engineering Manager", "Team Lead", "Department Head", "Director",
    ],
    "head of talent / chro": [
        "Head of Talent", "Head of Talent Acquisition", "VP Talent", "VP People",
        "Chief People Officer", "CHRO", "Chief Human Resources Officer",
    ],
    "founder / ceo / owner": [
        "Founder", "Co-Founder", "CEO", "Chief Executive Officer", "Owner",
        "Managing Director", "President",
    ],
    "c-suite (any)": [
        "CEO", "CTO", "CFO", "COO", "CMO", "CPO", "CRO",
        "Chief Executive Officer", "Chief Technology Officer", "Chief Financial Officer",
    ],
    "vp / director (any)": [
        "VP", "Vice President", "Director", "Senior Director", "Head of",
    ],
    "sales & business development": [
        "VP Sales", "Head of Sales", "Sales Director", "Account Executive",
        "Business Development", "Sales Manager", "Chief Revenue Officer",
    ],
    "marketing & growth": [
        "VP Marketing", "Head of Marketing", "Marketing Director", "Growth",
        "Demand Generation", "CMO", "Chief Marketing Officer",
    ],
    "revenue / revops": [
        "Revenue Operations", "RevOps", "Chief Revenue Officer", "VP Revenue",
        "Sales Operations",
    ],
    "engineering leaders": [
        "VP Engineering", "Head of Engineering", "Director of Engineering",
        "Engineering Manager", "CTO", "Chief Technology Officer",
    ],
    "product leaders": [
        "VP Product", "Head of Product", "Director of Product", "Chief Product Officer",
        "Group Product Manager",
    ],
    "it / security": [
        "CISO", "CIO", "Head of IT", "IT Director", "Head of Security",
        "Chief Information Officer", "Chief Information Security Officer",
    ],
    "operations": [
        "VP Operations", "Head of Operations", "Operations Director", "COO",
        "Chief Operating Officer", "Operations Manager",
    ],
    "finance / procurement": [
        "CFO", "VP Finance", "Head of Finance", "Controller", "Procurement",
        "Head of Procurement", "Chief Financial Officer",
    ],
}

# Grouped labels for the UI (and a future GET /recruiter-finder/personas). Recruiting
# & HR first — it is the default persona.
PERSONA_GROUPS: list[dict] = [
    {"group": "Recruiting & HR", "options": [
        "Recruiters & Talent Acquisition", "HR & People Ops",
        "Hiring Managers", "Head of Talent / CHRO"]},
    {"group": "Leadership", "options": [
        "Founder / CEO / Owner", "C-Suite (any)", "VP / Director (any)"]},
    {"group": "Go-to-market", "options": [
        "Sales & Business Development", "Marketing & Growth", "Revenue / RevOps"]},
    {"group": "Technical & Product", "options": [
        "Engineering leaders", "Product leaders", "IT / Security"]},
    {"group": "Operations & Finance", "options": ["Operations", "Finance / Procurement"]},
]

DEFAULT_PERSONA = "Recruiters & Talent Acquisition"


def titles_for_persona(persona: str | None, custom_titles: list[str] | None = None) -> list[str]:
    """Resolve a persona label to the Apollo title list to search.

    Explicit ``custom_titles`` (from the "Custom job titles…" option) win when
    present; otherwise match the label case-insensitively, defaulting to Recruiting
    & HR (HR_TITLES) for anything unknown or empty.
    """
    if custom_titles:
        cleaned = [t.strip() for t in custom_titles if t and t.strip()]
        if cleaned:
            return cleaned
    key = (persona or "").strip().lower()
    return PERSONA_TITLES.get(key, HR_TITLES)
