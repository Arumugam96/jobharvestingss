"""
Frontend integration routes — read-only data access layer.

Endpoints
─────────
GET /jobs                  — paginated, filtered, sorted job list
GET /jobs/{id}             — single job by id (DB primary key, or an md5
                              hash of job_url when read from the JSON
                              fallback — see _job_id())
GET /lead-intelligence     — recruiter intelligence records (paginated)
GET /lead-intelligence/{id}— single recruiter profile
GET /download/json         — download latest combined harvest JSON
GET /download/excel        — download latest Excel report

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
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.services.harvest_run_service import (
    JOB_SORT_FIELDS,
    HarvestRunService,
    data_source_mode,
    db_read,
    resolve_read,
    scraped_job_view,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Jobs & Leads"])

_COMBINED_DIR  = Path("data/results/combined")
_LEAD_DIR      = Path("data/results/lead_intelligence")
_RESULTS_ROOT  = Path("data/results")


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


def _apply_job_filters(jobs: list[dict], **f: Any) -> list[dict]:
    keyword       = (f.get("keyword")       or "").lower()
    company       = (f.get("company")       or "").lower()
    source        = (f.get("source")        or "").lower()
    hiring_entity = (f.get("hiring_entity") or "").lower()
    work_mode     = (f.get("work_mode")     or "").lower()
    date_from     = f.get("date_from")
    date_to       = f.get("date_to")

    result = jobs

    if keyword:
        result = [
            j for j in result
            if keyword in (j.get("job_title")       or "").lower()
            or keyword in (j.get("job_description") or "").lower()
            or keyword in (j.get("company")         or "").lower()
        ]
    if company:
        result = [j for j in result if company in (j.get("company") or "").lower()]
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
    return sorted(
        jobs,
        key     = lambda j: (j.get(sort_by) or "").lower(),
        reverse = sort_order.lower() == "desc",
    )


def _paginate(items: list, page: int, page_size: int) -> tuple[list, int, int]:
    total       = len(items)
    total_pages = max(1, math.ceil(total / page_size)) if page_size > 0 else 1
    start       = (page - 1) * page_size
    return items[start : start + page_size], total, total_pages


def _latest_excel() -> Path | None:
    xlsx_files: list[Path] = list(_RESULTS_ROOT.rglob("*.xlsx"))
    if not xlsx_files:
        return None
    return max(xlsx_files, key=lambda f: f.stat().st_mtime)


def _latest_json() -> Path | None:
    if not _COMBINED_DIR.exists():
        return None
    files = sorted(
        _COMBINED_DIR.glob("*_combined.json"),
        key     = lambda f: f.stat().st_mtime,
        reverse = True,
    )
    return files[0] if files else None


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
    page_size:     int = Query(50,   ge=1,  le=500,      description="Results per page (max 500)"),
    sort_by:       str = Query("posted_date",             description="Sort field: posted_date | company | job_title | source | hiring_entity | location"),
    sort_order:    str = Query("desc",                    description="Sort direction: asc | desc"),
    keyword:       str = Query("",                        description="Search in job_title, job_description, company"),
    company:       str = Query("",                        description="Filter by company name (partial match)"),
    source:        str = Query("",                        description="Filter by source: LinkedIn | Naukri | Dice"),
    hiring_entity: str = Query("",                        description="Filter: Direct Client | GCC | Staffing Firm | Ambiguous"),
    work_mode:     str = Query("",                        description="Filter: Remote | Hybrid | Onsite"),
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

    def _json_fallback() -> tuple[list[dict], int, int]:
        all_jobs = _load_all_jobs()
        filtered = _apply_job_filters(
            all_jobs,
            keyword       = keyword,
            company       = company,
            source        = source,
            hiring_entity = hiring_entity,
            work_mode     = work_mode,
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
            source        = source        or None,
            hiring_entity = hiring_entity or None,
            work_mode     = work_mode     or None,
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
            "source":        source        or None,
            "hiring_entity": hiring_entity or None,
            "work_mode":     work_mode     or None,
            "date_from":     date_from     or None,
            "date_to":       date_to       or None,
        },
        "sort": {"by": sort_by, "order": sort_order},
        "jobs": page_items,
    }


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

@router.get(
    "/download/json",
    summary     = "Download latest harvest JSON",
    description = "Returns the most recent combined harvest JSON as a file download.",
    responses   = {
        200: {"description": "JSON file download"},
        404: {"description": "No harvest results found"},
    },
)
async def download_json() -> FileResponse:
    """
    Downloads the latest `*_combined.json` from `data/results/combined/`.
    Run a harvest first if no file is found.
    """
    path = _latest_json()
    if path is None:
        raise HTTPException(
            status_code = 404,
            detail      = "No harvest JSON found. Run POST /run-harvest-agent first.",
        )
    logger.info("download_json", path=str(path))
    return FileResponse(
        path         = str(path),
        media_type   = "application/json",
        filename     = path.name,
        headers      = {"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


# ══════════════════════════════════════════════════════════════════════════════
# GET /download/excel
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/download/excel",
    summary     = "Download latest harvest Excel",
    description = "Returns the most recent harvest Excel report (.xlsx) as a file download.",
    responses   = {
        200: {"description": "Excel file download"},
        404: {"description": "No Excel report found"},
    },
)
async def download_excel() -> FileResponse:
    """
    Downloads the latest `.xlsx` file from `data/results/`.
    Run a harvest first if no file is found.
    """
    path = _latest_excel()
    if path is None:
        raise HTTPException(
            status_code = 404,
            detail      = "No Excel report found. Run POST /run-harvest-agent first.",
        )
    logger.info("download_excel", path=str(path))
    return FileResponse(
        path         = str(path),
        media_type   = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename     = path.name,
        headers      = {"Content-Disposition": f'attachment; filename="{path.name}"'},
    )
