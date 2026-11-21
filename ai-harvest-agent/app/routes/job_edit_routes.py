"""
Manual edit of a harvested job's classification / company / point-of-contact fields.

Endpoint
────────
PATCH /jobs/{id} — update any of hiring_entity, company, company_size, domain,
                   job_poster_name, job_poster_designation (only the fields sent
                   change). With ``apply_to_company: true`` the company-level
                   fields sent (hiring_entity, company_size, domain — never the
                   name/POC fields) are also written to every other job of the
                   same company (normalize_company_key match, tenant-scoped)
                   AND saved as the tenant's manual company override
                   (company_overrides), so future harvests apply them too.

Access: admins may edit any job; business leads only unassigned jobs or jobs
assigned to themselves (ensure_job_access → 403); everyone else 403.

Returns ``{"job": <GET /jobs/{id} view>, "updated_other_jobs": n,
"company_override": <override view or null>}``.
"""
from __future__ import annotations

import re
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.company_size import BAND_TO_TIER
from app.core.dependencies import get_db_session, require_phone_logs_access
from app.core.job_url import normalize_company_key
from app.core.tenant_context import apply_tenant
from app.models.auth import AuthenticatedUser
from app.models.harvest_run import ScrapedJobORM
from app.services.company_classification_service import override_view, upsert_manual_override
from app.services.harvest_run_service import HarvestRunService, scraped_job_view
from app.services.job_assignment_service import ensure_job_access

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Jobs & Leads"])

HIRING_ENTITIES = ("Direct Client", "GCC", "Staffing Firm", "Ambiguous")
# Canonical stored company-size values ("51-200 employees", …) — the same
# vocabulary bulk_insert_scraped_jobs / the size filter use.
COMPANY_SIZE_VALUES = tuple(f"{band} employees" for band in BAND_TO_TIER)
# Column limits (app/models/harvest_run.py); the poster fields are Text, capped
# here only to keep hand-typed values sane.
_MAX_LEN = {
    "company": 500,
    "domain": 50,
    "job_poster_name": 500,
    "job_poster_designation": 1000,
}
# Fields copied to the company's other jobs when apply_to_company is set.
COMPANY_LEVEL_FIELDS = ("hiring_entity", "company_size", "domain")
_LIKE_ESCAPE = re.compile(r"([\\%_])")


class JobEditRequest(BaseModel):
    """Only the fields present in the body are changed."""

    hiring_entity: str | None = Field(default=None, description=" | ".join(HIRING_ENTITIES))
    company: str | None = None
    company_size: str | None = Field(default=None, description='Canonical band, e.g. "51-200 employees"; "" clears it')
    domain: str | None = Field(default=None, description='Free text (≤50 chars); "" resets to "Any"')
    job_poster_name: str | None = None
    job_poster_designation: str | None = None
    apply_to_company: bool = Field(
        default=False,
        description="Also apply the hiring_entity / company_size / domain sent to every other job of this company.",
    )


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def _validated_values(body: JobEditRequest) -> dict[str, Any]:
    """The sent fields, trimmed + validated, as column → new value. 422 on bad input."""
    sent = body.model_dump(exclude_unset=True, exclude={"apply_to_company"})
    values: dict[str, Any] = {}
    for field, raw in sent.items():
        if raw is None:
            continue  # explicit null = leave unchanged
        value = str(raw).strip()
        limit = _MAX_LEN.get(field)
        if limit is not None and len(value) > limit:
            raise _unprocessable(f"{field} must be at most {limit} characters.")
        if field == "hiring_entity":
            if value not in HIRING_ENTITIES:
                raise _unprocessable(f"hiring_entity must be one of: {', '.join(HIRING_ENTITIES)}.")
        elif field == "company":
            if not value:
                raise _unprocessable("Company name can't be empty.")
        elif field == "company_size":
            if value in BAND_TO_TIER:  # bare band token → stored form
                value = f"{value} employees"
            if value and value not in COMPANY_SIZE_VALUES:
                raise _unprocessable(f"company_size must be one of: {', '.join(COMPANY_SIZE_VALUES)} (or empty).")
        elif field == "domain":
            value = value or "Any"
        elif field in ("job_poster_name", "job_poster_designation"):
            value = value or None  # nullable columns: blank clears
        values[field] = value
    # is_gcc mirrors the GCC hiring entity (BusinessFilterService sets both together).
    if "hiring_entity" in values:
        values["is_gcc"] = values["hiring_entity"] == "GCC"
    return values


async def _apply_to_company_jobs(
    db: AsyncSession, job: ScrapedJobORM, values: dict[str, Any]
) -> int:
    """Write the company-level ``values`` to every other (tenant) job whose
    normalize_company_key matches ``job``'s. Returns how many rows changed."""
    key = normalize_company_key(job.company)
    if not key or not values:
        return 0
    # SQL pre-filter on the key's longest word, then confirm the exact key in Python
    # (the key strips legal suffixes/punctuation, so it can't be matched in SQL).
    # Every key word is a substring of lower(company) (punctuation only became spaces).
    word = _LIKE_ESCAPE.sub(r"\\\1", max(key.split(), key=len))
    pattern = f"%{word}%"
    cols = [getattr(ScrapedJobORM, c) for c in values]
    stmt = apply_tenant(
        select(ScrapedJobORM.id, ScrapedJobORM.company, *cols).where(
            ScrapedJobORM.id != job.id,
            func.lower(ScrapedJobORM.company).like(pattern, escape="\\"),
        ),
        ScrapedJobORM.tenant_id,
    )
    rows = (await db.execute(stmt)).all()
    ids = [
        r.id for r in rows
        if normalize_company_key(r.company) == key
        and any(getattr(r, c) != v for c, v in values.items())
    ]
    for i in range(0, len(ids), 500):
        await db.execute(
            update(ScrapedJobORM)
            .where(ScrapedJobORM.id.in_(ids[i:i + 500]))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
    return len(ids)


@router.patch(
    "/jobs/{job_id}",
    status_code=status.HTTP_200_OK,
    summary="Edit a job's details",
    responses={403: {"description": "Not allowed to edit this job"},
               404: {"description": "Job not found"},
               422: {"description": "Invalid field value"}},
)
async def edit_job(
    job_id: str,
    body: JobEditRequest,
    current_user: AuthenticatedUser = Depends(require_phone_logs_access),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Update the hiring entity, company name/size, domain and POC name/designation
    of one job (only the fields sent change). Admins may edit any job; business
    leads only unassigned jobs or their own. ``apply_to_company`` also copies the
    hiring_entity / company_size / domain sent to all other jobs of the same
    company (matched after the edit, by normalized company name) and saves them as
    the company's manual override for future harvests.
    Returns ``{"job": <job view>, "updated_other_jobs": n, "company_override": …}``."""
    values = _validated_values(body)
    job = await HarvestRunService(db).get_scraped_job_by_id(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Job {job_id} not found")
    if not current_user.is_admin:
        ensure_job_access(job.assigned_to_email, current_user)

    changes = {
        field: [getattr(job, field), value]
        for field, value in values.items()
        if getattr(job, field) != value
    }
    for field, (_old, new) in changes.items():
        setattr(job, field, new)

    updated_other_jobs = 0
    override = None
    if body.apply_to_company:
        company_values = {
            c: values[c] for c in (*COMPANY_LEVEL_FIELDS, "is_gcc") if c in values
        }
        updated_other_jobs = await _apply_to_company_jobs(db, job, company_values)
        # Remember the company-level fields for future harvests (manual override:
        # beats keyword rules and any llm verdict for this company).
        override_fields = {c: values[c] for c in COMPANY_LEVEL_FIELDS if c in values}
        if override_fields:
            override = await upsert_manual_override(
                db, company_name=job.company or "", fields=override_fields,
                email=current_user.email,
            )

    await db.flush()
    view = scraped_job_view(job)
    override_out = override_view(override) if override is not None else None
    await db.commit()
    logger.info(
        "job_edited",
        job_id=job_id,
        user=current_user.email,
        changes=changes,
        apply_to_company=body.apply_to_company,
        updated_other_jobs=updated_other_jobs,
        company_override=override_out,
    )
    return {"job": view, "updated_other_jobs": updated_other_jobs, "company_override": override_out}
