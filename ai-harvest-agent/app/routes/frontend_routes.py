"""
Frontend integration routes — read-only data access layer.

Endpoints
─────────
GET /jobs                  — paginated, filtered, sorted job list
GET /jobs/{id}             — single job by id (DB primary key, or an md5
                              hash of job_url when read from the JSON
                              fallback — see _job_id())
GET /jobs/{id}/outreach-detail — Resdex candidates + outreach emails for a job
                              (DB only)
GET /lead-intelligence     — recruiter intelligence records (paginated)
GET /lead-intelligence/{id}— single recruiter profile
GET /download/json         — harvest JSON report, generated from the DB
GET /download/excel        — harvest Excel report, generated from the DB

Note: GET /health is served by app/routes/health.py (Swagger-visible).
GET /jobs reads from the database (every run ever recorded, see
HarvestRunService.list_scraped_jobs) or the JSON file store, per
Settings.data_source — same DATA_SOURCE toggle as /harvest-status,
/run-history, and /{linkedin,naukri,dice}-results. Every other endpoint
here still reads only from saved result files under data/results/.
No scraping is triggered by anything in this file.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.company_size import band_to_tier
from app.services.harvest_run_service import (
    CONTACT_FILTER_TOKENS,
    JOB_SORT_FIELDS,
    HarvestRunService,
    data_source_mode,
    db_read,
    job_dict_contact,
    resolve_read,
    scraped_job_view,
)
from app.services.report_service import (
    build_excel_report_bytes,
    build_json_report_bytes,
    merged_job_dicts,
    report_basename,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Jobs & Leads"])

_COMBINED_DIR  = Path("data/results/combined")
_LEAD_DIR      = Path("data/results/lead_intelligence")


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _job_id(job: dict) -> str:
    url = job.get("job_url") or ""
    content = url or f"{job.get('job_title','')}{job.get('company','')}"
    return hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()[:16]


def _lead_id(r: dict) -> str:
    url = r.get("linkedin_profile_url") or r.get("linkedin_url") or ""
    if url:
        return hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:16]
    name    = r.get("name") or r.get("recruiter_name") or ""
    company = r.get("company") or r.get("current_company") or ""
    return hashlib.md5(f"{name}::{company}".encode(), usedforsecurity=False).hexdigest()[:16]


def _load_all_jobs() -> list[dict]:
    """Load jobs from the most recent combined JSON file."""
    if not _COMBINED_DIR.exists():
        return []
    files = sorted(
        _COMBINED_DIR.glob("*_combined.json"),
        key     = lambda f: f.stat().st_mtime,
        reverse = True,
    )
    if not files:
        return []
    try:
        raw  = json.loads(files[0].read_text(encoding="utf-8"))
        jobs = raw.get("jobs", [])
        for job in jobs:
            job["id"] = _job_id(job)
        return jobs
    except Exception as exc:
        logger.warning("jobs_load_error", error=str(exc))
        return []


def _load_lead_records() -> list[dict]:
    """Load recruiter records from the most recent lead-intelligence JSON."""
    if not _LEAD_DIR.exists():
        return []

    patterns = ["rcd_*_recruiter_contacts.json", "*_recruiter_contacts.json"]
    files: list[Path] = []
    for pat in patterns:
        files.extend(_LEAD_DIR.glob(pat))
    if not files:
        return []

    files = sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("lead_load_error", error=str(exc))
        return []

    # Support multiple possible record-list keys in the JSON structure
    for key in ("recruiters", "records", "contacts", "prospects", "leads", "results"):
        if isinstance(data.get(key), list):
            records = data[key]
            for r in records:
                r["id"] = _lead_id(r)
            return records

    if isinstance(data, list):
        for r in data:
            r["id"] = _lead_id(r)
        return data

    return []


def _job_matches_contact(job: dict, tokens: list[str]) -> bool:
    """Python mirror of the UI's jobMatchesContact() for the JSON read path.
    Presence comes from job_dict_contact() — the same semantics as the DB
    path's _contact_presence_exprs(): poster/recruiter values plus the
    Company-tagged company(+location) contact. Tokens are OR-combined."""
    c = job_dict_contact(job)
    has_email, has_mobile, has_linkedin = c["email"], c["mobile"], c["linkedin"]
    checks = {
        "email":         has_email,
        "email_poster":  c["email_poster"],
        "email_company": c["email_company"],
        "mobile":        has_mobile,
        "linkedin":      has_linkedin,
        "no_email":      not has_email,
        "no_mobile":     not has_mobile,
        "no_linkedin":   not has_linkedin,
        "none":          not (has_email or has_mobile or has_linkedin),
    }
    return any(checks.get(t, False) for t in tokens)


def _apply_job_filters(jobs: list[dict], **f: Any) -> list[dict]:
    keyword       = (f.get("keyword")       or "").lower()
    company       = (f.get("company")       or "").lower()
    company_exact = f.get("company_exact")  or ""
    job_title     = f.get("job_title")      or ""
    poc           = f.get("poc")            or ""
    contact       = f.get("contact")        or []
    size_tier     = f.get("size_tier")      or ""
    country       = (f.get("country")       or "").lower()
    company_country = (f.get("company_country") or "").lower()
    source        = (f.get("source")        or "").lower()
    hiring_entity = (f.get("hiring_entity") or "").lower()
    work_mode     = (f.get("work_mode")     or "").lower()
    run_id        = (f.get("run_id")        or "")
    date_from     = f.get("date_from")
    date_to       = f.get("date_to")

    result = jobs

    if run_id:
        # The JSON file store isn't run-scoped (it only ever holds the latest
        # combined run and its job dicts carry no run_id), so a run_id filter
        # can only match if a job dict happens to carry one. This keeps the
        # DB path authoritative for run-scoped reads and never leaks another
        # run's jobs when falling back to JSON.
        result = [j for j in result if j.get("run_id") == run_id]
    if keyword:
        result = [
            j for j in result
            if keyword in (j.get("job_title")       or "").lower()
            or keyword in (j.get("job_description") or "").lower()
            or keyword in (j.get("company")         or "").lower()
            or keyword in (j.get("job_poster_name") or "").lower()
            or keyword in (j.get("source")          or "").lower()
            or keyword in (j.get("email_id")        or "").lower()
            or keyword in (j.get("contact_number")  or "").lower()
            or keyword in (j.get("company_contact_name")  or "").lower()
            or keyword in (j.get("company_contact_email") or "").lower()
            or keyword in (j.get("company_contact_phone") or "").lower()
        ]
    if company:
        result = [j for j in result if company in (j.get("company") or "").lower()]
    if company_exact:
        result = [j for j in result if (j.get("company") or "") == company_exact]
    if job_title:
        result = [j for j in result if (j.get("job_title") or "") == job_title]
    if poc:
        # The POC the table shows: poster name, else the company contact's.
        result = [j for j in result if job_dict_contact(j)["poc_name"] == poc.strip()]
    if contact:
        result = [j for j in result if _job_matches_contact(j, contact)]
    if size_tier:
        if size_tier == "unknown":
            result = [j for j in result if not band_to_tier(j.get("company_size") or "")]
        else:
            result = [j for j in result if band_to_tier(j.get("company_size") or "") == size_tier]
    if country:
        result = [j for j in result if (j.get("country") or "").lower() == country]
    if company_country:
        result = [j for j in result if (j.get("company_country") or "").lower() == company_country]
    if source:
        result = [j for j in result if (j.get("source") or "").lower() == source]
    if hiring_entity:
        result = [j for j in result if (j.get("hiring_entity") or "").lower() == hiring_entity]
    if work_mode:
        result = [j for j in result if (j.get("work_mode") or "").lower() == work_mode]
    if date_from:
        result = [j for j in result if _date_gte(j.get("posted_date"), date_from)]
    if date_to:
        result = [j for j in result if _date_lte(j.get("posted_date"), date_to)]

    return result


def _date_gte(posted: str | None, bound: str) -> bool:
    if not posted:
        return True
    try:
        return posted[:10] >= bound[:10]
    except Exception:
        return True


def _date_lte(posted: str | None, bound: str) -> bool:
    if not posted:
        return True
    try:
        return posted[:10] <= bound[:10]
    except Exception:
        return True


def _apply_sort(jobs: list[dict], sort_by: str, sort_order: str) -> list[dict]:
    if sort_by not in JOB_SORT_FIELDS:
        sort_by = "posted_date"
    if sort_by == "job_poster_name":
        # The POC column (poster, else Company-tagged contact) — mirrors the DB
        # path's poc_name_expr() ordering.
        def key(j: dict) -> str:
            return job_dict_contact(j)["poc_name"].lower()
    else:
        def key(j: dict) -> str:
            # str() first: older combined files may hold a non-string in the sort
            # field (e.g. a numeric posted_date) — .lower() alone would 500 the route.
            return str(j.get(sort_by) or "").lower()
    return sorted(jobs, key=key, reverse=sort_order.lower() == "desc")


def _paginate(items: list, page: int, page_size: int) -> tuple[list, int, int]:
    total       = len(items)
    total_pages = max(1, math.ceil(total / page_size)) if page_size > 0 else 1
    start       = (page - 1) * page_size
    return items[start : start + page_size], total, total_pages


# ══════════════════════════════════════════════════════════════════════════════
# GET /jobs
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/jobs",
    status_code = status.HTTP_200_OK,
    summary     = "List jobs",
    description = (
        "Returns a paginated, filterable, sortable list of every harvested "
        "job on record (database), or the most recent combined harvest "
        "run's jobs (JSON fallback) — see Settings.data_source."
    ),
)
async def list_jobs(
    page:          int = Query(1,    ge=1,               description="Page number (1-based)"),
    page_size:     int = Query(100,  ge=1,  le=500,      description="Results per page (max 500)"),
    sort_by:       str = Query("posted_date",             description="Sort field: posted_date | company | job_title | source | hiring_entity | location | job_poster_name"),
    sort_order:    str = Query("desc",                    description="Sort direction: asc | desc"),
    keyword:       str = Query("",                        description="Search in job_title, job_description, company, job_poster_name, source, email_id, contact_number, company_contact_name/email/phone"),
    company:       str = Query("",                        description="Filter by company name (partial match)"),
    company_exact: str = Query("",                        description="Filter by exact company name (UI dropdown)"),
    job_title:     str = Query("",                        description="Filter by exact job title (UI dropdown)"),
    poc:           str = Query("",                        description="Filter by exact POC name (UI dropdown): the job poster, else the company contact"),
    contact:       str = Query("",                        description="Contact availability — comma-separated tokens, OR-combined: email | email_poster | email_company | mobile | linkedin | no_email | no_mobile | no_linkedin | none | resdex (email/mobile include the company contact; resdex = latest Resdex search matched candidates, DB only)"),
    size_tier:     str = Query("",                        description="Company-size tier: Small | Medium | Large | Enterprise | unknown"),
    country:       str = Query("",                        description="Filter by job-location country (UI dropdown), e.g. India"),
    company_country: str = Query("",                      description="Filter by company HQ country (UI dropdown), e.g. United States"),
    source:        str = Query("",                        description="Filter by source: LinkedIn | Naukri | Dice"),
    hiring_entity: str = Query("",                        description="Filter: Direct Client | GCC | Staffing Firm | Ambiguous"),
    work_mode:     str = Query("",                        description="Filter: Remote | Hybrid | Onsite"),
    run_id:        str = Query("",                        description="Filter to a single harvest run's jobs (display run id, e.g. 20260817_110703)"),
    date_from:     str = Query("",                        description="Filter jobs posted on or after this date (YYYY-MM-DD)"),
    date_to:       str = Query("",                        description="Filter jobs posted on or before this date (YYYY-MM-DD)"),
) -> dict:
    """
    Paginated job list — every run on record when read from the database,
    or just the most recent harvest when read from the JSON fallback (that
    file only ever holds the latest run's jobs). See Settings.data_source.

    **Filtering** (all optional, combinable):
    - `keyword` — searches job_title, job_description, company
    - `company`  — company name partial match
    - `source`   — LinkedIn | Naukri | Dice
    - `hiring_entity` — Direct Client | GCC | Staffing Firm | Ambiguous
    - `work_mode` — Remote | Hybrid | Onsite
    - `date_from` / `date_to` — ISO date YYYY-MM-DD

    **Sorting** — set `sort_by` and `sort_order`.
    """
    logger.info(
        "jobs_request",
        page=page, page_size=page_size,
        keyword=keyword, company=company, source=source,
    )

    # Unknown contact tokens are dropped rather than 422ing — an empty result
    # list after validation means "no contact filter".
    contact_tokens = [
        t for t in (tok.strip() for tok in contact.split(","))
        if t in CONTACT_FILTER_TOKENS
    ]

    def _json_fallback() -> tuple[list[dict], int, int]:
        all_jobs = _load_all_jobs()
        filtered = _apply_job_filters(
            all_jobs,
            keyword       = keyword,
            company       = company,
            company_exact = company_exact,
            job_title     = job_title,
            poc           = poc,
            contact       = contact_tokens,
            size_tier     = size_tier,
            country       = country,
            company_country = company_country,
            source        = source,
            hiring_entity = hiring_entity,
            work_mode     = work_mode,
            run_id        = run_id or None,
            date_from     = date_from or None,
            date_to       = date_to   or None,
        )
        sorted_jobs = _apply_sort(filtered, sort_by, sort_order)
        return _paginate(sorted_jobs, page, page_size)

    mode = data_source_mode()
    result, used = await resolve_read(
        mode,
        lambda: db_read(lambda db: HarvestRunService(db).list_scraped_jobs(
            keyword       = keyword       or None,
            company       = company       or None,
            company_exact = company_exact or None,
            job_title     = job_title     or None,
            poc           = poc           or None,
            contact       = contact_tokens or None,
            size_tier     = size_tier     or None,
            country       = country       or None,
            company_country = company_country or None,
            source        = source        or None,
            hiring_entity = hiring_entity or None,
            work_mode     = work_mode     or None,
            run_id        = run_id        or None,
            date_from     = date_from     or None,
            date_to       = date_to       or None,
            sort_by       = sort_by,
            sort_order    = sort_order,
            page          = page,
            page_size     = page_size,
        )),
        _json_fallback,
    )

    if used == "database":
        # db_read() returns None on a DB error even in "database" mode (that
        # mode means "never fall back to JSON", not "assume the DB call
        # succeeded") — guard the unpack the same way linkedin_routes.py's
        # equivalent read does.
        orm_jobs, total = result if result else ([], 0)
        page_items  = [scraped_job_view(j) for j in orm_jobs]
        total_pages = max(1, math.ceil(total / page_size)) if page_size > 0 else 1
    else:
        page_items, total, total_pages = result

    return {
        "total":       total,
        "page":        page,
        "page_size":   page_size,
        "total_pages": total_pages,
        "filters": {
            "keyword":       keyword       or None,
            "company":       company       or None,
            "company_exact": company_exact or None,
            "job_title":     job_title     or None,
            "poc":           poc           or None,
            "contact":       contact_tokens or None,
            "size_tier":     size_tier     or None,
            "country":       country       or None,
            "company_country": company_country or None,
            "source":        source        or None,
            "hiring_entity": hiring_entity or None,
            "work_mode":     work_mode     or None,
            "run_id":        run_id        or None,
            "date_from":     date_from     or None,
            "date_to":       date_to       or None,
        },
        "sort": {"by": sort_by, "order": sort_order},
        "jobs": page_items,
    }


# ══════════════════════════════════════════════════════════════════════════════
# GET /jobs/facets — must be registered before /jobs/{job_id}, or FastAPI
# would capture "facets" as a job id.
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/jobs/facets",
    status_code = status.HTTP_200_OK,
    summary     = "Job filter facets",
    description = (
        "Distinct Company / Job-title / POC dropdown values plus whole-dataset "
        "stat counts for the jobs page. GET /jobs is paginated server-side, so "
        "the UI's filter options and header stats come from here rather than "
        "from the loaded rows."
    ),
)
async def job_facets() -> dict:
    def _facets_from_json() -> dict:
        jobs = _load_all_jobs()

        def _distinct(key: str) -> list[str]:
            return sorted({j.get(key) or "" for j in jobs} - {""})

        # Same contact semantics as the DB path (poster/recruiter + Company-tagged).
        contacts = [job_dict_contact(j) for j in jobs]

        def _count(flag: str) -> int:
            return sum(1 for c in contacts if c[flag])

        return {
            "companies":  _distinct("company"),
            "job_titles": _distinct("job_title"),
            "poc_names":  sorted({c["poc_name"] for c in contacts} - {""}),
            "stats": {
                "total":         len(jobs),
                "companies":     len({j.get("company") or "" for j in jobs} - {""}),
                "pocs":          _count("poc_name"),
                # `is not False` mirrors the UI's `passed_filter !== false`.
                "qualified":     sum(1 for j in jobs if j.get("passed_filter") is not False),
                "flagged":       sum(1 for j in jobs if j.get("passed_filter") is False),
                "with_email":         _count("email"),
                "with_poster_email":  _count("email_poster"),
                "with_company_email": _count("email_company"),
                "with_phone":         _count("mobile"),
                "with_linkedin":      _count("linkedin"),
            },
        }

    mode = data_source_mode()
    result, _used = await resolve_read(
        mode,
        lambda: db_read(lambda db: HarvestRunService(db).job_facets()),
        _facets_from_json,
    )
    # db_read returns None on a DB error in "database" mode — keep the shape.
    return result or {"companies": [], "job_titles": [], "poc_names": [], "stats": {}}


# ══════════════════════════════════════════════════════════════════════════════
# GET /jobs/{id}
# ══════════════════════════════════════════════════════════════════════════════

def _get_job_from_json(job_id: str) -> dict | None:
    for job in _load_all_jobs():
        if job.get("id") == job_id:
            return job
    return None


@router.get(
    "/jobs/{job_id}",
    status_code = status.HTTP_200_OK,
    summary     = "Job detail",
    description = "Returns the complete record for a single harvested job by its id.",
    responses   = {404: {"description": "Job not found"}},
)
async def get_job(job_id: str) -> dict:
    """
    Returns one job record by `id` — the DB row's real primary key when read
    from the database, or a 16-char hex hash of the job URL when read from
    the JSON fallback (see Settings.data_source). The `id` field is included
    in every record returned by **GET /jobs**, using whichever scheme that
    same read produced.
    """
    mode = data_source_mode()
    result, used = await resolve_read(
        mode,
        lambda: db_read(lambda db: HarvestRunService(db).get_scraped_job_by_id(job_id)),
        lambda: _get_job_from_json(job_id),
    )
    job = scraped_job_view(result) if (used == "database" and result is not None) else result
    if job is None:
        raise HTTPException(
            status_code = 404,
            detail      = f"Job '{job_id}' not found. Run a harvest first or check the id.",
        )
    return job


# ══════════════════════════════════════════════════════════════════════════════
# GET /jobs/{id}/outreach-detail
# ══════════════════════════════════════════════════════════════════════════════
# A separate path segment, so it neither shadows nor is shadowed by
# GET /jobs/{job_id} (a path param never matches across "/"). Kept off
# scraped_job_view / GET /jobs so the list view gets no extra queries.

def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


def _resdex_candidate_view(c: Any) -> dict:
    from app.services.resdex_types import (
        clean_profile_url,
        resdex_link_context,
        resdex_results_url,
    )

    # Rows stored before 2026-10-21 hold the result card's /v3/preview link, which
    # only works inside the search's own browser session (Naukri: "link is not
    # valid") — it is dropped here, and its sid / tupleIndex rebuild the
    # reopenable result-list link instead (resdex_types).
    sid, position = resdex_link_context(c.profile_url, getattr(c, "raw", None))
    return {
        "id":                  c.id,
        "rank":                c.rank,
        "candidate_name":      c.candidate_name,
        "headline":            c.headline,
        "current_designation": c.current_designation,
        "current_company":     c.current_company,
        "previous_roles":      c.previous_roles or [],
        "experience_text":     c.experience_text,
        "experience_years":    c.experience_years,
        "location":            c.location,
        "preferred_locations": c.preferred_locations or [],
        "skills":              c.skills or [],
        "education":           c.education,
        "notice_period":       c.notice_period,
        "ctc":                 c.ctc,
        "last_active":         c.last_active,
        # A stable profile link, or "" — never a session-bound /v3/preview link.
        "profile_url":         clean_profile_url(c.profile_url),
        # The Resdex result list this candidate came from (needs the same Naukri
        # recruiter login) and their 1-based place in it when searched; ""/None
        # when the search id is unknown.
        "resdex_url":          resdex_results_url(sid),
        "resdex_position":     position,
        "matched_skills":      c.matched_skills or [],
        "skill_coverage":      c.skill_coverage,
        "extraction_method":   c.extraction_method,
        "outreach_id":         c.outreach_id,
        "emailed":             bool(c.outreach_id),
        # The "✓ Naukri Verified" badge every emailed card carries — mirrors the
        # RESDEX_SHOW_NAUKRI_VERIFIED setting so the job page matches the email.
        "naukri_verified_badge": _naukri_verified_badge(),
    }


def _naukri_verified_badge() -> bool:
    from app.config import get_settings

    return bool(getattr(get_settings(), "resdex_show_naukri_verified", True))


def _resdex_tier_views(s: Any) -> list[dict]:
    """All three ladder tiers for the search's stored requirements — whatever
    RESDEX_SEARCH_TIER is — so the tiers that were not searched can be run by hand.
    The searched tier shows the query + filters actually used (stored row);
    the others are rebuilt from the requirements (deterministic)."""
    from app.config import get_settings
    from app.services.resdex_query import tier_previews

    req = s.requirements if isinstance(s.requirements, dict) else None
    if not req:
        return []
    try:
        tiers = tier_previews(req, int(get_settings().resdex_active_within_months or 0))
    except Exception as exc:  # a malformed stored requirement must not break the page
        logger.warning("resdex_tier_preview_failed", error=str(exc))
        return []
    for t in tiers:
        t["searched"] = bool(s.tier) and t["tier"] == s.tier and bool(s.searches_used)
        if t["searched"] and s.boolean_query:
            t["query"], t["filters"] = s.boolean_query, s.filters or t["filters"]
    return tiers


def _resdex_search_view(s: Any) -> dict:
    candidates = [_resdex_candidate_view(c) for c in (s.candidates or [])]
    return {
        "status":        s.status,
        "searched_at":   _iso(s.created_at),
        "requirements":  s.requirements,
        "boolean_query": s.boolean_query,
        "filters":       s.filters,
        "tier":          s.tier,
        "searches_used": s.searches_used,
        "result_count":  s.result_count,
        "error":         s.error,
        # job_post | recruiter_profile | apollo | apollo_hr ("" for older searches)
        "contact_source": getattr(s, "contact_source", "") or "",
        # Reopenable Resdex result list of this search ("" when no candidate
        # carries its search id — then the UI falls back to the blank form).
        "resdex_url":    next((c["resdex_url"] for c in candidates if c["resdex_url"]), ""),
        # [{tier, query, filters, searched}] for T1..T3 (see _resdex_tier_views).
        "tiers":         _resdex_tier_views(s),
        "candidates":    candidates,
    }


def _outreach_email_view(e: Any, included: list[Any]) -> dict:
    return {
        "id":            e.id,
        # The email row's OWN job / recruiter ids — the /mail/:id thread page
        # loads by these (an auto send matched here by job_url may carry the id
        # of an earlier run's copy of this job).
        "job_id":        e.job_id,
        "recruiter_id":  e.recruiter_id,
        "outreach_kind": e.outreach_kind or "initial",
        "to_email":      e.to_email,
        "from_email":    e.from_email,
        "subject":       e.subject,
        "body":          e.body,
        "body_html":     e.body_html,
        "status":        e.status,
        "error_message": e.error_message,
        "created_at":    _iso(e.created_at),
        "sent_at":       _iso(e.sent_at),
        "delivery_status": e.delivery_status,
        "delivered_at":  _iso(e.delivered_at),
        "opened_at":     _iso(e.opened_at),
        "bounced_at":    _iso(e.bounced_at),
        "replied_at":    _iso(e.replied_at),
        "sent_by":       e.sent_by,
        "candidate_ids": [c.id for c in included],
        # Rank/name of each included candidate, so the UI can label them even
        # when they came from an older search than the one shown under "resdex".
        "candidates_included": [
            {"id": c.id, "rank": c.rank, "candidate_name": c.candidate_name} for c in included
        ],
    }


async def _job_outreach_detail(db: Any, job_id: str) -> dict:
    """One read session: the job, its latest Resdex search (+ candidates) and its
    email sends. Returns {"job": None} when the job isn't visible to the tenant."""
    from sqlalchemy import select

    from app.core.job_url import normalize_job_url
    from app.core.tenant_context import apply_tenant
    from app.models.resdex import ResdexCandidateEmailORM, ResdexCandidateORM, ResdexSearchORM
    from app.services.outreach_log_service import emails_for_job

    job = await HarvestRunService(db).get_scraped_job_by_id(job_id)
    if job is None:
        return {"job": None}

    key = normalize_job_url(job.job_url)
    search = None
    if key:
        search = (await db.execute(
            apply_tenant(select(ResdexSearchORM), ResdexSearchORM.tenant_id)
            .where(ResdexSearchORM.job_url == key)
            .order_by(ResdexSearchORM.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()

    emails = await emails_for_job(db, job.id, job.job_url)
    by_email: dict[str, list[Any]] = {}
    email_ids = [e.id for e in emails]
    if email_ids:
        stamped = (await db.execute(
            apply_tenant(select(ResdexCandidateORM), ResdexCandidateORM.tenant_id)
            .where(ResdexCandidateORM.outreach_id.in_(email_ids))
            .order_by(ResdexCandidateORM.rank.asc())
        )).scalars().all()
        for c in stamped:
            by_email.setdefault(c.outreach_id, []).append(c)
        # A reused match can be in several emails: resdex_candidate_emails links each
        # email to every candidate it carried (outreach_id above keeps only the first).
        linked = (await db.execute(
            apply_tenant(
                select(ResdexCandidateEmailORM.outreach_id, ResdexCandidateORM),
                ResdexCandidateEmailORM.tenant_id,
            )
            .join(ResdexCandidateORM, ResdexCandidateORM.id == ResdexCandidateEmailORM.candidate_id)
            .where(ResdexCandidateEmailORM.outreach_id.in_(email_ids))
        )).all()
        for oid, c in linked:
            cands = by_email.setdefault(oid, [])
            if all(x.id != c.id for x in cands):
                cands.append(c)
        for cands in by_email.values():
            cands.sort(key=lambda c: c.rank)

    return {
        "job":     job,
        "job_url": job.job_url,
        "resdex":  _resdex_search_view(search) if search is not None else None,
        "emails":  [_outreach_email_view(e, by_email.get(e.id, [])) for e in emails],
    }


@router.get(
    "/jobs/{job_id}/outreach-detail",
    status_code = status.HTTP_200_OK,
    summary     = "Job outreach detail",
    description = (
        "Resdex candidates matched for the job (latest search, with the query and "
        "filters used) plus every outreach email sent for it (initial + follow-ups)."
    ),
    responses   = {404: {"description": "Job not found"}, 503: {"description": "Database unavailable"}},
)
async def get_job_outreach_detail(job_id: str) -> dict:
    """Database only — Resdex searches and email sends live solely in the DB,
    so there is no JSON-fallback variant of this read."""
    result = await db_read(lambda db: _job_outreach_detail(db, job_id))
    if result is None:
        raise HTTPException(status_code=503, detail="Database unavailable.")
    if result["job"] is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return {
        "job_id":  job_id,
        "job_url": result["job_url"],
        "resdex":  result["resdex"],
        "emails":  result["emails"],
    }


# ══════════════════════════════════════════════════════════════════════════════
# GET /lead-intelligence
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/lead-intelligence",
    status_code = status.HTTP_200_OK,
    summary     = "Lead intelligence records",
    description = (
        "Returns recruiter intelligence records from the most recent "
        "recruiter discovery run.  Run **POST /run-recruiter-discovery** first."
    ),
)
async def list_lead_intelligence(
    page:      int = Query(1,   ge=1,          description="Page number (1-based)"),
    page_size: int = Query(50,  ge=1,  le=500, description="Results per page (max 500)"),
    keyword:   str = Query("",                  description="Search recruiter name, company, designation"),
    company:   str = Query("",                  description="Filter by company name"),
    source:    str = Query("",                  description="Filter by source"),
) -> dict:
    """
    Paginated recruiter intelligence records.

    Each record includes (when available):

    | Field               | Description                          |
    |---------------------|--------------------------------------|
    | name                | Recruiter full name                  |
    | designation         | Job title / designation              |
    | department          | HR / Talent Acquisition / Leadership |
    | position_level      | Recruiter → Manager → Director → VP  |
    | location            | City / region                        |
    | current_company     | Employer name                        |
    | linkedin_profile_url| LinkedIn profile URL                 |
    | email_id            | Official email (scraped only)        |
    | email_status        | VERIFIED / PUBLIC / NOT_FOUND        |
    | contact_number      | Phone (scraped only)                 |
    | phone_status        | VERIFIED / PUBLIC / NOT_FOUND        |
    | hiring_domain       | AI/ML, Cloud/DevOps, Java, SAP, …   |
    | company_industry    | LinkedIn industry taxonomy           |
    | company_size        | Employee band (e.g. 1,001–5,000)    |
    | years_in_company    | Tenure in current role               |
    | overall_experience  | Total career years                   |
    | reporting_manager   | Direct manager (if public)           |
    | confidence_score    | High / Medium / Low                  |
    | source              | Enrichment sources used              |
    """
    records = _load_lead_records()

    if keyword:
        kw = keyword.lower()
        records = [
            r for r in records
            if kw in (r.get("name") or r.get("recruiter_name") or "").lower()
            or kw in (r.get("company") or r.get("current_company") or "").lower()
            or kw in (r.get("designation") or "").lower()
        ]
    if company:
        co = company.lower()
        records = [
            r for r in records
            if co in (r.get("company") or r.get("current_company") or "").lower()
        ]
    if source:
        records = [
            r for r in records
            if (r.get("source") or "").lower() == source.lower()
        ]

    page_items, total, total_pages = _paginate(records, page, page_size)

    return {
        "total":       total,
        "page":        page,
        "page_size":   page_size,
        "total_pages": total_pages,
        "filters": {
            "keyword": keyword or None,
            "company": company or None,
            "source":  source  or None,
        },
        "records": page_items,
    }


# ══════════════════════════════════════════════════════════════════════════════
# GET /lead-intelligence/{id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/lead-intelligence/{lead_id}",
    status_code = status.HTTP_200_OK,
    summary     = "Single recruiter profile",
    description = "Returns the full recruiter intelligence profile by its stable id.",
    responses   = {404: {"description": "Lead not found"}},
)
async def get_lead(lead_id: str) -> dict:
    """
    Returns one recruiter profile by `id` (hash of linkedin_profile_url or name+company).
    The `id` field is included in every record returned by **GET /lead-intelligence**.
    """
    records = _load_lead_records()
    for r in records:
        if r.get("id") == lead_id:
            return r
    raise HTTPException(
        status_code = 404,
        detail      = f"Lead '{lead_id}' not found. Run recruiter discovery first or check the id.",
    )


# ══════════════════════════════════════════════════════════════════════════════
# GET /download/json
# ══════════════════════════════════════════════════════════════════════════════

async def _report_job_dicts() -> list[dict] | None:
    """Every scraped job on record with its recruiter's contact info merged
    in (see report_service) — the shared dataset behind both downloads.
    Generated fresh from the DB per request; harvest runs no longer persist
    JSON/Excel result files. None means the DB read itself failed."""
    rows = await db_read(lambda db: HarvestRunService(db).list_all_jobs_for_report())
    if rows is None:
        return None
    return merged_job_dicts(rows)


@router.get(
    "/download/json",
    summary     = "Download harvest JSON report",
    description = "Generates the harvest JSON report from the database (jobs merged with recruiter contact info) and returns it as a file download.",
    responses   = {
        200: {"description": "JSON file download"},
        404: {"description": "No harvest results found"},
    },
)
async def download_json() -> Response:
    """
    Builds the JSON report on demand from every scraped job on record,
    with each job's recruiter-enriched email/phone merged in.
    Run a harvest first if the database is empty.
    """
    job_dicts = await _report_job_dicts()
    if not job_dicts:
        raise HTTPException(
            status_code = 404,
            detail      = "No harvest results in the database. Run POST /run-harvest-agent first.",
        )
    filename = f"{report_basename()}.json"
    logger.info("download_json_generated", jobs=len(job_dicts), filename=filename)
    return Response(
        content      = build_json_report_bytes(job_dicts),
        media_type   = "application/json",
        headers      = {"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ══════════════════════════════════════════════════════════════════════════════
# GET /download/excel
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/download/excel",
    summary     = "Download harvest Excel report",
    description = "Generates the harvest Excel report (.xlsx) from the database (jobs merged with recruiter contact info) and returns it as a file download.",
    responses   = {
        200: {"description": "Excel file download"},
        404: {"description": "No harvest results found"},
    },
)
async def download_excel() -> Response:
    """
    Builds the Excel workbook on demand from every scraped job on record,
    with each job's recruiter-enriched email/phone merged in.
    Run a harvest first if the database is empty.
    """
    job_dicts = await _report_job_dicts()
    if not job_dicts:
        raise HTTPException(
            status_code = 404,
            detail      = "No harvest results in the database. Run POST /run-harvest-agent first.",
        )
    excel_bytes = build_excel_report_bytes(job_dicts)
    if not excel_bytes:
        raise HTTPException(
            status_code = 500,
            detail      = "Excel generation unavailable (openpyxl not installed).",
        )
    filename = f"{report_basename()}.xlsx"
    logger.info("download_excel_generated", jobs=len(job_dicts), filename=filename)
    return Response(
        content      = excel_bytes,
        media_type   = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers      = {"Content-Disposition": f'attachment; filename="{filename}"'},
    )
