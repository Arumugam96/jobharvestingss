"""Admin API for company classification: per-company overrides and the editable
keyword lists that future harvests apply (app/services/company_classification_service.py).

Endpoints (admins only — 403 otherwise; all scoped to the caller's tenant)
─────────
GET    /company-overrides?q=&source=&page=&page_size=  list (newest-updated first)
PUT    /company-overrides/{company_key}                create / replace a manual override
DELETE /company-overrides/{company_key}                remove it (future harvests fall
                                                       back to keywords + the LLM check)
GET    /classification-keywords                        the tenant's DB terms by list
POST   /classification-keywords                        add a term to staffing|direct|gcc
DELETE /classification-keywords/{id}                   remove a term

Business leads create overrides only through PATCH /jobs/{id} with
``apply_to_company`` (job_edit_routes). Changes apply from the next harvest run —
no restart (the lists are read per run).
"""
from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.company_size import BAND_TO_TIER
from app.core.dependencies import get_db_session, require_admin
from app.core.job_url import normalize_company_key
from app.models.auth import AuthenticatedUser
from app.models.company_override import KEYWORD_LISTS, OVERRIDE_SOURCES
from app.routes.job_edit_routes import COMPANY_SIZE_VALUES, HIRING_ENTITIES, _MAX_LEN
from app.services.company_classification_service import (
    add_keyword,
    delete_keyword,
    delete_override,
    keyword_view,
    list_keywords,
    list_overrides,
    load_overrides_for_keys,
    normalize_term,
    override_view,
    upsert_manual_override,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Company classification"])

_MAX_TERM_LEN = 200


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


class CompanyOverrideRequest(BaseModel):
    """A full manual override: each field null/blank = no override for it."""

    company_name: str | None = Field(default=None, description="Display name (defaults to the existing one / the key)")
    hiring_entity: str | None = Field(default=None, description=" | ".join(HIRING_ENTITIES))
    company_size: str | None = Field(default=None, description='Canonical band, e.g. "51-200 employees"')
    domain: str | None = Field(default=None, description="Free text (≤50 chars)")


class KeywordRequest(BaseModel):
    list: str = Field(description=" | ".join(KEYWORD_LISTS))
    term: str


def _validated_override(body: CompanyOverrideRequest) -> dict[str, Any]:
    """hiring_entity / company_size / domain, trimmed + validated (blank → None). 422 on bad input."""
    out: dict[str, Any] = {}
    entity = (body.hiring_entity or "").strip()
    if entity and entity not in HIRING_ENTITIES:
        raise _unprocessable(f"hiring_entity must be one of: {', '.join(HIRING_ENTITIES)}.")
    out["hiring_entity"] = entity or None
    size = (body.company_size or "").strip()
    if size in BAND_TO_TIER:
        size = f"{size} employees"
    if size and size not in COMPANY_SIZE_VALUES:
        raise _unprocessable(f"company_size must be one of: {', '.join(COMPANY_SIZE_VALUES)} (or empty).")
    out["company_size"] = size or None
    domain = (body.domain or "").strip()
    if len(domain) > _MAX_LEN["domain"]:
        raise _unprocessable(f"domain must be at most {_MAX_LEN['domain']} characters.")
    out["domain"] = None if domain in ("", "Any") else domain
    return out


@router.get("/company-overrides", summary="List company classification overrides (admins)")
async def get_company_overrides(
    q: str = Query(default="", description="Company name / key contains (case-insensitive)"),
    source: str = Query(default="", description="manual | llm (blank = both)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    _admin: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """The tenant's overrides, newest-updated first: ``{items, total, page, page_size}``."""
    if source and source not in OVERRIDE_SOURCES:
        raise _unprocessable(f"source must be one of: {', '.join(OVERRIDE_SOURCES)}.")
    rows, total = await list_overrides(db, q=q, source=source, page=page, page_size=page_size)
    return {"items": [override_view(r) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.put("/company-overrides/{company_key}", summary="Create or replace a manual company override (admins)")
async def put_company_override(
    company_key: str,
    body: CompanyOverrideRequest,
    current_user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Sets hiring_entity / company_size / domain for the company (null/blank clears
    that field). ``company_key`` is an existing row's key, or any company name — it
    is normalized (normalize_company_key). The row becomes source "manual"."""
    fields = _validated_override(body)
    raw = (company_key or "").strip()
    exists = bool(await load_overrides_for_keys(db, [raw]))
    key = raw if exists else normalize_company_key(raw)
    if not key:
        raise _unprocessable("company_key is empty after normalization.")
    # A new row shows the name as typed; an existing one keeps its name unless sent.
    name = (body.company_name or "").strip()[:500] or ("" if exists else raw[:500])
    row = await upsert_manual_override(
        db, company_name=name, company_key=key, fields=fields, email=current_user.email, replace=True,
    )
    view = override_view(row)
    await db.commit()
    logger.info("company_override_saved", company_key=key, user=current_user.email,
                **{k: v for k, v in fields.items()})
    return view


@router.delete("/company-overrides/{company_key}", summary="Delete a company override (admins)")
async def remove_company_override(
    company_key: str,
    current_user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """404 when the tenant has no override with this key."""
    if not await delete_override(db, company_key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Override not found")
    await db.commit()
    logger.info("company_override_deleted", company_key=company_key, user=current_user.email)
    return {"deleted": True, "company_key": company_key}


@router.get("/classification-keywords", summary="List the tenant's classification keywords (admins)")
async def get_classification_keywords(
    _admin: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """``{"lists": {"staffing": [...], "direct": [...], "gcc": [...]}}`` — the DB terms
    only (merged with the built-in JSON master lists at classification time)."""
    lists: dict[str, list[dict]] = {name: [] for name in KEYWORD_LISTS}
    for row in await list_keywords(db):
        lists.setdefault(row.list_name, []).append(keyword_view(row))
    return {"lists": lists}


@router.post("/classification-keywords", status_code=status.HTTP_201_CREATED,
             summary="Add a classification keyword (admins)")
async def post_classification_keyword(
    body: KeywordRequest,
    current_user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Adds ``term`` (lower-cased, matched as whole words in company names) to the
    list. An existing identical term is returned unchanged (``created: false``)."""
    list_name = (body.list or "").strip().lower()
    if list_name not in KEYWORD_LISTS:
        raise _unprocessable(f"list must be one of: {', '.join(KEYWORD_LISTS)}.")
    term = normalize_term(body.term)
    if not term:
        raise _unprocessable("term must contain a letter or digit.")
    if len(term) > _MAX_TERM_LEN:
        raise _unprocessable(f"term must be at most {_MAX_TERM_LEN} characters.")
    row, created = await add_keyword(db, list_name, term, email=current_user.email)
    view = {**keyword_view(row), "created": created}
    await db.commit()
    if created:
        logger.info("classification_keyword_added", list=list_name, term=term, user=current_user.email)
    return view


@router.delete("/classification-keywords/{keyword_id}", summary="Delete a classification keyword (admins)")
async def remove_classification_keyword(
    keyword_id: int,
    current_user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """404 when the tenant has no keyword with this id."""
    if not await delete_keyword(db, keyword_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Keyword not found")
    await db.commit()
    logger.info("classification_keyword_deleted", id=keyword_id, user=current_user.email)
    return {"deleted": True, "id": keyword_id}
