"""Harvest-style JD enrichment for Demand Search jobs (app/services/demand_service.py).

A Demand job is inserted with only what the user gave (title, JD, optional Skills /
Experience / Location / Client). This module fills the rest of the job row the way
a harvest would derive it from a job description — reusing the harvest's own
extractors, never re-implementing them — and then back-fills from the Resdex
requirements extraction:

  enrich_from_jd         (before the Resdex search, so its prompt sees the result)
    contacts             naukri_agent.extract_contacts_from_text (regex, verbatim)
                         + jd_llm_enrichment.llm_extract_contacts (LLM, grounded in
                         the JD) → email_id, contact_number, job_poster_name /
                         _designation, current_company (the recruiter's company)
    company              the row's Client, else the grounded company from the JD
    description          job_description_html (llm_restructure_description for a
                         flat blob, else the deterministic formatting)
    classification       BusinessFilterService.classify_all (track_ambiguous=False,
                         so client names never reach ambiguous_companies.json) →
                         domain, hiring_entity, is_gcc, job_type
    the rest             company_size (parse_company_size_band), work_mode
                         (_infer_work_mode), employment_type (metadata only)
  backfill_from_requirements   (after the search) — skills, experience, location,
                         work_mode from the newest ResdexSearchORM.requirements

Rules: fill-only-empty (a user-supplied value is never overwritten, so a re-run is
idempotent); emails/phones only when literally present in the JD; NO Apollo of any
kind — values are written with HarvestRunService.update_scraped_job_fields, which
attaches no recruiter / company cache / HR contact (the row is inserted with company
"" for the same reason). Best-effort: callers log and continue on any failure.
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select

from app.config import get_settings
from app.core.company_size import normalize_company_size, parse_company_size_band
from app.core.tenant_context import get_current_tenant_id
from app.core.text_formatting import description_text_to_html, format_job_description
from app.models.harvest_config import FiltersConfig
from app.models.resdex import ResdexSearchORM
from app.models.unified_job import UnifiedJob
from app.services.business_filter_service import BusinessFilterService
from app.services.harvest_run_service import HarvestRunService, db_read, db_write
from app.services.jd_llm_enrichment import (
    GroundedContacts,
    choose_email,
    has_contact_signals,
    is_flat_description,
    llm_configured,
    llm_extract_contacts,
    llm_restructure_description,
)

logger = structlog.get_logger(__name__)

# Column defaults that mean "not derived yet" (fill-only-empty treats them as empty).
_UNSET = {"", "Any", "not_specified"}
# Metadata keys (manual entry / extra upload columns) read as a stated value.
_META_WORK_MODE = ("work_mode", "work mode", "workmode", "mode")
_META_EMPLOYMENT = ("employment_type", "employment type", "employment", "job_type", "job type")
_META_COMPANY_SIZE = ("company_size", "company size")


def _empty(value: Any) -> bool:
    """True for None, blank or placeholder strings ("Any", "not_specified"), empty containers."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in _UNSET
    if isinstance(value, (list, tuple, dict)):
        return not value
    return False


def _meta(row: dict, keys: tuple[str, ...]) -> str:
    """First non-blank string in the row's ``metadata`` under any of ``keys``.

    Metadata keys are matched case-insensitively; returns "" when none is found.
    """
    meta = row.get("metadata") or {}
    if not isinstance(meta, dict):
        return ""
    lowered = {str(k).strip().lower(): v for k, v in meta.items()}
    for k in keys:
        v = lowered.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _fill(job: Any, values: dict, field: str, value: Any) -> None:
    """Stage ``value`` for ``field`` when the stored value is empty."""
    if _empty(value) or not _empty(getattr(job, field, None)) or field in values:
        return
    values[field] = value


def _default_llm(settings: Any) -> Any | None:
    """The extraction LLM (LLMService), or None when no provider is configured."""
    if not llm_configured(settings):
        return None
    from app.services.llm_service import LLMService
    return LLMService(settings)


async def _load(job_id: str):
    """The scraped job row by id, or None when missing or the DB read failed."""
    return await db_read(lambda db: HarvestRunService(db).get_scraped_job_by_id(job_id))


async def _write(job_id: str, values: dict) -> list[str]:
    """Persist the staged field values; returns the sorted field names written.

    Returns [] when nothing was staged or the update did not succeed.
    """
    if not values:
        return []
    ok = await db_write(lambda db: HarvestRunService(db).update_scraped_job_fields(job_id, values))
    return sorted(values) if ok else []


async def _llm_contacts(llm: Any, text: str, ref: str, timeout_s: float) -> GroundedContacts:
    """LLM contact extraction from the JD, grounded against the JD text itself.

    Uses Naukri's junk-address filter, so only addresses present in the text survive.
    """
    from app.agents.naukri_agent import _clean_email   # Naukri's junk-address filter
    return await llm_extract_contacts(
        llm, text, sources=[text], job_ref=ref, timeout_s=timeout_s, email_filter=_clean_email,
    )


# ── Before the search: JD extraction ──────────────────────────────────────────

async def enrich_from_jd(job_id: str | None, row: dict, *, run_pk: str | None = None) -> list[str]:
    """Derive the harvest fields of one Demand job from its JD (see module doc)
    and write the empty ones. Returns the fields written. LLM steps respect
    NAUKRI_LLM_CONTACT_EXTRACTION / NAUKRI_LLM_DESCRIPTION_RESTRUCTURE /
    NAUKRI_LLM_TIMEOUT_S and run only when a provider is configured; an LLM
    failure keeps the deterministic values."""
    from app.agents.naukri_agent import _infer_work_mode, extract_contacts_from_text

    if not job_id:
        return []
    job = await _load(job_id)
    if job is None:
        return []
    settings = get_settings()
    ref = job.job_url or job_id
    text = (job.job_description or "").strip()
    values: dict[str, Any] = {}

    # Contacts — deterministic (verbatim) first, then the grounded LLM pass.
    email, phone = extract_contacts_from_text(text)
    grounded = GroundedContacts()
    need_contacts = any(_empty(getattr(job, f)) for f in (
        "email_id", "contact_number", "job_poster_name", "job_poster_designation",
        "current_company"))
    want_contacts = bool(settings.naukri_llm_contact_extraction) and need_contacts \
        and has_contact_signals(text)
    want_restructure = bool(settings.naukri_llm_description_restructure) \
        and _empty(job.job_description_html) and is_flat_description(text)
    llm = None
    if want_contacts or want_restructure:
        try:
            llm = _default_llm(settings)
        except Exception as exc:
            logger.warning("demand_llm_unavailable", job=ref, error=str(exc))
    timeout_s = float(settings.naukri_llm_timeout_s)
    if llm is not None and want_contacts:
        try:
            grounded = await _llm_contacts(llm, text, ref, timeout_s)
        except Exception as exc:
            logger.warning("demand_llm_contacts_failed", job=ref,
                           error=str(exc) or type(exc).__name__, note="regex result kept")
    chosen, _origin = choose_email(email, grounded)
    _fill(job, values, "email_id", chosen)
    _fill(job, values, "contact_number", phone or (grounded.phones[0] if grounded.phones else None))
    _fill(job, values, "job_poster_name", grounded.name)
    _fill(job, values, "job_poster_designation", grounded.designation)
    _fill(job, values, "current_company", grounded.company)
    company = (row.get("client") or "").strip() or (grounded.company or "")
    _fill(job, values, "company", company)

    # Description HTML — the LLM re-lining for a flat blob, else deterministic.
    if _empty(job.job_description_html) and text:
        formatted = None
        if llm is not None and want_restructure:
            try:
                formatted = await llm_restructure_description(llm, text, job_ref=ref,
                                                              timeout_s=timeout_s)
            except Exception as exc:
                logger.warning("demand_llm_restructure_failed", job=ref,
                               error=str(exc) or type(exc).__name__)
        _fill(job, values, "job_description_html",
              description_text_to_html(formatted or format_job_description(text)))

    # Stated values (metadata) win over inference from the JD.
    size = normalize_company_size(_meta(row, _META_COMPANY_SIZE))
    _fill(job, values, "company_size",
          f"{size} employees" if size else parse_company_size_band(text))
    _fill(job, values, "employment_type", _meta(row, _META_EMPLOYMENT)[:100])
    for source in (_meta(row, _META_WORK_MODE), job.location or "", text):
        mode = _infer_work_mode(source)
        if mode != "not_specified":
            _fill(job, values, "work_mode", mode)
            break

    # Classification — the harvest's classifier, without the ambiguous-list append.
    employment = values.get("employment_type", job.employment_type or "")
    uj = UnifiedJob(
        job_title=job.job_title or "", company=values.get("company", job.company or ""),
        location=job.location or "", salary=job.salary or "", experience=job.experience or "",
        posted_date=job.posted_date or "", job_url=job.job_url or "", job_description=text,
        skills=list(job.skills or []), work_mode=values.get("work_mode", job.work_mode or ""),
        source=job.source or "", employment_type=employment,
        job_type=employment,                 # a stated type is scanned before the JD
    )
    BusinessFilterService().classify_all([uj], FiltersConfig(), track_ambiguous=False)
    if _empty(job.domain):
        _fill(job, values, "domain", uj.domain)
    if _empty(job.hiring_entity):
        _fill(job, values, "hiring_entity", uj.hiring_entity)
        if "hiring_entity" in values:
            values["is_gcc"] = bool(uj.is_gcc)
    _fill(job, values, "job_type", uj.job_type)

    written = await _write(job_id, values)
    if llm is not None and run_pk and hasattr(llm, "get_call_log"):
        calls = llm.get_call_log()
        if calls:
            await db_write(lambda db: HarvestRunService(db).bulk_insert_llm_calls(run_pk, calls))
    logger.info("demand_jd_enriched", job=ref, fields=written,
                llm_contacts=bool(llm is not None and want_contacts),
                llm_restructure=bool(llm is not None and want_restructure))
    return written


# ── After the search: Resdex requirements back-fill ───────────────────────────

def _experience_text(lo: Any, hi: Any) -> str:
    """Format an experience range as "3-5 Yrs" / "3 Yrs" / "3+ Yrs" / "0-5 Yrs", or ""."""
    if isinstance(lo, int) and isinstance(hi, int):
        return f"{lo}-{hi} Yrs" if lo != hi else f"{lo} Yrs"
    if isinstance(lo, int):
        return f"{lo}+ Yrs"
    if isinstance(hi, int):
        return f"0-{hi} Yrs"
    return ""


def requirements_fields(req: dict | None) -> dict[str, Any]:
    """Job-row values a grounded requirements dict (resdex_prompts.ground_requirements)
    states: skills (mandatory then optional names), experience, location, work_mode."""
    req = req if isinstance(req, dict) else {}
    skills: list[str] = []
    for item in (req.get("mandatory_skills") or []) + (req.get("optional_skills") or []):
        name = (item.get("name") if isinstance(item, dict) else item) or ""
        name = str(name).strip()
        if name and name.lower() not in {s.lower() for s in skills}:
            skills.append(name)
    locations = [str(x).strip() for x in (req.get("locations") or []) if str(x).strip()]
    return {
        "skills": skills,
        "experience": _experience_text(req.get("min_exp"), req.get("max_exp")),
        "location": ", ".join(locations),
        "work_mode": "remote" if req.get("remote_ok") is True else "",
    }


async def _latest_requirements(norm: str) -> dict | None:
    """Newest non-null Resdex requirements stored for this job URL in the current tenant.

    None when there is none or the read failed.
    """
    tenant_id = get_current_tenant_id()

    async def _q(db):
        """Select the newest requirements JSON for (tenant, job_url)."""
        stmt = (
            select(ResdexSearchORM.requirements)
            .where(ResdexSearchORM.tenant_id == tenant_id, ResdexSearchORM.job_url == norm,
                   ResdexSearchORM.requirements.isnot(None))
            .order_by(ResdexSearchORM.created_at.desc())
            .limit(1)
        )
        return (await db.execute(stmt)).scalars().first()
    return await db_read(_q)


async def backfill_from_requirements(job_id: str | None, norm: str) -> list[str]:
    """Fill the Demand job's EMPTY skills / experience / location / work_mode from
    the newest Resdex requirements stored for its job_url. Returns the fields written."""
    if not job_id or not norm:
        return []
    req = await _latest_requirements(norm)
    if not req:
        return []
    job = await _load(job_id)
    if job is None:
        return []
    values: dict[str, Any] = {}
    for field, value in requirements_fields(req).items():
        _fill(job, values, field, value)
    written = await _write(job_id, values)
    if written:
        logger.info("demand_requirements_backfilled", job=job.job_url, fields=written)
    return written
