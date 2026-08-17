"""
UnifiedJob — common internal representation used across all source agents,
business-filter pipeline, and verification agent.

Every source (LinkedIn, Naukri, Dice, …) converts its scraped records to
this type before any post-processing.  The API response models are built
from UnifiedJob at the route layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UnifiedJob:
    """
    Single job listing in a source-agnostic format.

    Fields populated by source agents
    ──────────────────────────────────
    job_title, company, location, salary, experience, posted_date,
    job_url, job_description, skills, work_mode, source

    Fields populated by BusinessFilterService (after scraping)
    ──────────────────────────────────────────────────────────
    domain, hiring_entity, is_gcc, job_type (inferred from job content)

    Fields populated by VerificationAgent (optional)
    ─────────────────────────────────────────────────
    verification_status
    """

    # ── Source agent fills these ──────────────────────────────────────────────
    job_title:       str
    company:         str
    location:        str
    salary:          str
    experience:      str
    posted_date:     str
    job_url:         str
    job_description: str
    skills:          list[str]
    work_mode:       str        # "remote" | "hybrid" | "onsite" | "not_specified"
    source:          str        # "LinkedIn" | "Naukri" | "Dice"

    # Present on LinkedIn/Dice's scraped dataclasses; persisted to
    # ScrapedJobORM.company_url / employment_type — without these here the
    # orchestrator flow silently dropped them before the DB insert.
    company_url:     str  = ""
    employment_type: str  = ""

    # ── BusinessFilterService fills these ─────────────────────────────────────
    job_type:       str  = ""           # "contract" | "permanent" | … | "not_specified" (inferred)
    domain:         str  = "Any"        # "IT" | "Finance" | "Engineering" | …
    hiring_entity:  str  = "Any"        # "Direct Client" | "GCC" | "Staffing Firm" | "Ambiguous"
    is_gcc:         bool = False

    # ── Raw platform hints consumed by BusinessFilterService, not user-facing ──
    domain_hint:    str  = ""           # e.g. LinkedIn's native "Industries" tag

    # ── VerificationAgent fills this ──────────────────────────────────────────
    # "pending" | "verified" | "not_verified" | "career_page_not_found" | "skipped"
    verification_status: str = "pending"

    # ── BusinessFilterService annotation (does NOT remove jobs) ───────────────
    # passed_filter is True when the job satisfies every active filter rule;
    # when False, filter_reason names the first failing stage + the offending
    # value (e.g. "hiring_entity: got 'GCC', wanted 'Direct Client'"). All jobs
    # are retained/persisted regardless — the UI filters on these fields.
    passed_filter: bool = True
    filter_reason: str  = ""

    # ── Lead Intelligence (populated by source agents, optional) ─────────────
    job_poster_name:        str | None = None   # Recruiter / Hiring Manager name
    job_poster_designation: str | None = None   # Recruiter title / designation
    linkedin_profile_url:   str | None = None   # Recruiter LinkedIn profile URL
    current_company:        str | None = None   # Recruiter's current company
    email_id:               str | None = None   # Recruiter email
    contact_number:         str | None = None   # Recruiter phone / mobile

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_title":              self.job_title,
            "company":                self.company,
            "location":               self.location,
            "salary":                 self.salary,
            "experience":             self.experience,
            "posted_date":            self.posted_date,
            "job_url":                self.job_url,
            "job_description":        self.job_description,
            "skills":                 self.skills,
            "work_mode":              self.work_mode,
            "source":                 self.source,
            "company_url":            self.company_url,
            "employment_type":        self.employment_type,
            "job_type":               self.job_type,
            "domain":                 self.domain,
            "hiring_entity":          self.hiring_entity,
            "is_gcc":                 self.is_gcc,
            "verification_status":    self.verification_status,
            "passed_filter":          self.passed_filter,
            "filter_reason":          self.filter_reason,
            "job_poster_name":        self.job_poster_name,
            "job_poster_designation": self.job_poster_designation,
            "linkedin_profile_url":   self.linkedin_profile_url,
            "current_company":        self.current_company,
            "email_id":               self.email_id,
            "contact_number":         self.contact_number,
        }
