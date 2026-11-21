"""Company classification overrides, editable keyword lists and the LLM
hiring-entity check for Ambiguous companies.

DB side (tenant-scoped by an explicit ``tenant_id`` filter — "internal" means the
internal tenant's own rows here, not all-access, because a classification
override is a per-tenant decision):

  load_classification_context  one overrides query + one keywords query → the
                               BusinessFilterService ClassificationContext for a run
  load_overrides_for_keys      overrides for a batch of company keys (insert time)
  upsert_manual_override       admin edit / job edit "apply to company" — always
                               wins over (and replaces the source of) an llm row
  save_llm_overrides           cache LLM verdicts — never touches an existing row,
                               so a manual value is never overwritten
  keyword CRUD                 classification_keywords (staffing / direct / gcc)

LLM side (llm_classify_ambiguous): after keyword classification, the distinct
Ambiguous companies without an override (capped per run) go to the extraction LLM
in batched prompts; recognised answers become source "llm" overrides (Unknown is
cached with every field NULL so it isn't asked again) and are applied to the run's
jobs. Never raises — a failure leaves those companies Ambiguous.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.company_domain import is_placeholder_company
from app.core.job_url import normalize_company_key
from app.core.tenant_context import get_current_tenant_id
from app.models.company_override import (
    KEYWORD_LISTS,
    OVERRIDE_SOURCE_LLM,
    OVERRIDE_SOURCE_MANUAL,
    ClassificationKeywordORM,
    CompanyOverrideORM,
)
from app.models.harvest_run import LlmCallType
from app.models.unified_job import UnifiedJob
from app.services.business_filter_service import ClassificationContext, CompanyOverride

logger = structlog.get_logger(__name__)

OVERRIDE_FIELDS = ("hiring_entity", "company_size", "domain")
_WS = re.compile(r"\s+")
_HAS_WORD = re.compile(r"[^\W_]", re.UNICODE)
_KEY_CHUNK = 500  # IN-list chunk for company_key lookups


def _tenant(tenant_id: str | None) -> str:
    """The tenant to scope to: the given one, else the current context's."""
    return tenant_id or get_current_tenant_id()


def to_override(row: CompanyOverrideORM) -> CompanyOverride:
    """The immutable classification view of a company_overrides row."""
    return CompanyOverride(
        company_key=row.company_key,
        company_name=row.company_name or "",
        hiring_entity=row.hiring_entity or None,
        company_size=row.company_size or None,
        domain=row.domain or None,
        source=row.source or OVERRIDE_SOURCE_MANUAL,
    )


def override_view(row: CompanyOverrideORM) -> dict[str, Any]:
    """JSON shape of an override row for the admin API."""
    return {
        "id": row.id,
        "company_key": row.company_key,
        "company_name": row.company_name or row.company_key,
        "hiring_entity": row.hiring_entity,
        "company_size": row.company_size,
        "domain": row.domain,
        "source": row.source,
        "updated_by_email": row.updated_by_email or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


# ── Loading (per run / per insert batch) ──────────────────────────────────────

async def load_overrides_for_keys(
    db: AsyncSession, keys: Iterable[str], tenant_id: str | None = None,
) -> dict[str, CompanyOverrideORM]:
    """The tenant's override rows for ``keys`` (blank keys ignored), by company_key."""
    wanted = sorted({k for k in keys if k})
    if not wanted:
        return {}
    tid = _tenant(tenant_id)
    out: dict[str, CompanyOverrideORM] = {}
    for i in range(0, len(wanted), _KEY_CHUNK):
        res = await db.execute(
            select(CompanyOverrideORM).where(
                CompanyOverrideORM.tenant_id == tid,
                CompanyOverrideORM.company_key.in_(wanted[i:i + _KEY_CHUNK]),
            )
        )
        for row in res.scalars():
            out[row.company_key] = row
    return out


async def load_keywords(db: AsyncSession, tenant_id: str | None = None) -> dict[str, frozenset[str]]:
    """The tenant's DB keyword terms grouped by list ("staffing" / "direct" / "gcc")."""
    tid = _tenant(tenant_id)
    res = await db.execute(
        select(ClassificationKeywordORM.list_name, ClassificationKeywordORM.term)
        .where(ClassificationKeywordORM.tenant_id == tid)
    )
    grouped: dict[str, set[str]] = {}
    for list_name, term in res.all():
        if list_name in KEYWORD_LISTS and term:
            grouped.setdefault(list_name, set()).add(term)
    return {k: frozenset(v) for k, v in grouped.items()}


async def load_classification_context(
    db: AsyncSession,
    tenant_id: str | None = None,
    company_keys: Iterable[str] | None = None,
) -> ClassificationContext:
    """The tenant's overrides (all of them, or only ``company_keys``) and keyword
    terms as a ClassificationContext — one overrides query, one keywords query."""
    tid = _tenant(tenant_id)
    if company_keys is None:
        res = await db.execute(select(CompanyOverrideORM).where(CompanyOverrideORM.tenant_id == tid))
        rows = list(res.scalars())
    else:
        rows = list((await load_overrides_for_keys(db, company_keys, tid)).values())
    return ClassificationContext(
        overrides={r.company_key: to_override(r) for r in rows if r.company_key},
        keywords=await load_keywords(db, tid),
    )


# ── Writes ────────────────────────────────────────────────────────────────────

def _clean_field(name: str, value: Any) -> str | None:
    """Stored form of an override field: blank / "Any" (domain) → None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or (name == "domain" and text == "Any"):
        return None
    return text


async def upsert_manual_override(
    db: AsyncSession,
    *,
    company_name: str,
    fields: dict[str, Any],
    email: str = "",
    company_key: str | None = None,
    replace: bool = False,
    tenant_id: str | None = None,
) -> CompanyOverrideORM | None:
    """Create or update the tenant's manual override for a company.

    ``fields`` holds any of hiring_entity / company_size / domain (blank → NULL).
    ``replace=False`` (job edit "apply to company") changes only the fields given
    and keeps the row's others — including values an llm row held, which the
    editor saw and kept; ``replace=True`` (admin PUT) sets all three from
    ``fields`` (missing → NULL). The row's source becomes "manual" either way, so
    no later llm write can touch it. Returns the row (flushed), or None when the
    company has no usable key."""
    key = company_key or normalize_company_key(company_name)
    if not key:
        return None
    tid = _tenant(tenant_id)
    now = datetime.now(timezone.utc)  # set in Python: no expired server defaults after flush
    row = (await load_overrides_for_keys(db, [key], tid)).get(key)
    if row is None:
        row = CompanyOverrideORM(tenant_id=tid, company_key=key, company_name=company_name or key,
                                 created_at=now)
        db.add(row)
    elif company_name:
        row.company_name = company_name
    for name in OVERRIDE_FIELDS:
        if name in fields:
            setattr(row, name, _clean_field(name, fields[name]))
        elif replace:
            setattr(row, name, None)
    row.source = OVERRIDE_SOURCE_MANUAL
    row.updated_by_email = email or ""
    row.updated_at = now
    await db.flush()
    return row


async def save_llm_overrides(
    db: AsyncSession, overrides: Iterable[CompanyOverride], tenant_id: str | None = None,
) -> int:
    """Insert LLM verdicts as source "llm" rows. A company that already has any
    row (manual or llm) is skipped — an llm write never overwrites. Returns rows added."""
    items = {o.company_key: o for o in overrides if o.company_key}
    if not items:
        return 0
    tid = _tenant(tenant_id)
    existing = await load_overrides_for_keys(db, items.keys(), tid)
    now = datetime.now(timezone.utc)
    added = 0
    for key, o in items.items():
        if key in existing:
            continue
        db.add(CompanyOverrideORM(
            tenant_id=tid, company_key=key, company_name=o.company_name or key,
            hiring_entity=o.hiring_entity, company_size=None, domain=None,
            source=OVERRIDE_SOURCE_LLM, updated_by_email="", created_at=now, updated_at=now,
        ))
        added += 1
    if added:
        await db.flush()
    return added


async def list_overrides(
    db: AsyncSession,
    *,
    q: str = "",
    source: str = "",
    page: int = 1,
    page_size: int = 50,
    tenant_id: str | None = None,
) -> tuple[list[CompanyOverrideORM], int]:
    """(rows, total) of the tenant's overrides, newest-updated first; ``q`` matches
    company name or key (case-insensitive substring), ``source`` filters manual/llm."""
    tid = _tenant(tenant_id)
    conds = [CompanyOverrideORM.tenant_id == tid]
    term = (q or "").strip().lower()
    if term:
        like = f"%{term}%"
        conds.append(or_(
            func.lower(CompanyOverrideORM.company_name).like(like),
            CompanyOverrideORM.company_key.like(like),
        ))
    if source:
        conds.append(CompanyOverrideORM.source == source)
    total = (await db.execute(select(func.count()).select_from(CompanyOverrideORM).where(*conds))).scalar() or 0
    res = await db.execute(
        select(CompanyOverrideORM).where(*conds)
        .order_by(CompanyOverrideORM.updated_at.desc(), CompanyOverrideORM.id.desc())
        .offset(max(0, page - 1) * page_size).limit(page_size)
    )
    return list(res.scalars()), int(total)


async def delete_override(db: AsyncSession, company_key: str, tenant_id: str | None = None) -> bool:
    """Delete the tenant's override for ``company_key``; False when there was none."""
    tid = _tenant(tenant_id)
    res = await db.execute(
        delete(CompanyOverrideORM).where(
            CompanyOverrideORM.tenant_id == tid, CompanyOverrideORM.company_key == company_key,
        )
    )
    return bool(res.rowcount)


# ── Keyword lists ─────────────────────────────────────────────────────────────

def normalize_term(term: str | None) -> str:
    """Stored form of a keyword term: lower-cased, whitespace collapsed; "" when it
    holds no letter/digit (it could never match a company name)."""
    text = _WS.sub(" ", (term or "").strip().lower())
    return text if _HAS_WORD.search(text) else ""


async def list_keywords(db: AsyncSession, tenant_id: str | None = None) -> list[ClassificationKeywordORM]:
    """The tenant's keyword rows ordered by list then term."""
    tid = _tenant(tenant_id)
    res = await db.execute(
        select(ClassificationKeywordORM).where(ClassificationKeywordORM.tenant_id == tid)
        .order_by(ClassificationKeywordORM.list_name, ClassificationKeywordORM.term)
    )
    return list(res.scalars())


async def add_keyword(
    db: AsyncSession, list_name: str, term: str, email: str = "", tenant_id: str | None = None,
) -> tuple[ClassificationKeywordORM, bool]:
    """(row, created) — adds ``term`` (already normalized) to ``list_name``; an
    existing identical term is returned with created=False."""
    tid = _tenant(tenant_id)
    existing = (await db.execute(
        select(ClassificationKeywordORM).where(
            ClassificationKeywordORM.tenant_id == tid,
            ClassificationKeywordORM.list_name == list_name,
            ClassificationKeywordORM.term == term,
        )
    )).scalars().first()
    if existing is not None:
        return existing, False
    row = ClassificationKeywordORM(tenant_id=tid, list_name=list_name, term=term,
                                   created_by_email=email or "", created_at=datetime.now(timezone.utc))
    db.add(row)
    await db.flush()
    return row, True


async def delete_keyword(db: AsyncSession, keyword_id: int, tenant_id: str | None = None) -> bool:
    """Delete one of the tenant's keyword rows by id; False when not found."""
    tid = _tenant(tenant_id)
    res = await db.execute(
        delete(ClassificationKeywordORM).where(
            ClassificationKeywordORM.tenant_id == tid, ClassificationKeywordORM.id == keyword_id,
        )
    )
    return bool(res.rowcount)


def keyword_view(row: ClassificationKeywordORM) -> dict[str, Any]:
    """JSON shape of a keyword row for the admin API."""
    return {
        "id": row.id,
        "list": row.list_name,
        "term": row.term,
        "created_by_email": row.created_by_email or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# ── LLM check for Ambiguous companies ─────────────────────────────────────────

@dataclass
class LlmClassificationOutcome:
    """What one run's LLM hiring-entity check produced."""
    overrides: list[CompanyOverride] = field(default_factory=list)  # to cache as source "llm"
    call_log:  list[dict] = field(default_factory=list)             # LLMService call-log entries
    asked:     int = 0     # companies sent to the LLM
    classified: int = 0    # of those, answered with a label other than Unknown
    jobs_updated: int = 0  # run jobs moved off Ambiguous
    token_usage: dict = field(default_factory=dict)                 # LLMService.get_usage_summary()


def _candidates(jobs: list[UnifiedJob], context: ClassificationContext, cap: int) -> list[dict[str, Any]]:
    """Distinct Ambiguous companies (first job each) that have no override row,
    in first-seen order, at most ``cap``."""
    from app.prompts.company_classification_prompts import jd_snippet

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for j in jobs:
        if j.hiring_entity != "Ambiguous" or is_placeholder_company(j.company):
            continue
        key = normalize_company_key(j.company)
        if not key or key in seen or key in context.overrides:
            continue
        seen.add(key)
        out.append({
            "key": key, "company": (j.company or "").strip(),
            "title": (j.job_title or "").strip()[:150], "snippet": jd_snippet(j.job_description),
        })
        if len(out) >= cap:
            break
    return out


async def llm_classify_ambiguous(
    jobs: list[UnifiedJob],
    context: ClassificationContext,
    *,
    settings: Any = None,
    llm: Any = None,
) -> LlmClassificationOutcome:
    """Ask the extraction LLM about this run's Ambiguous companies that have no
    override yet (≤ COMPANY_LLM_CLASSIFY_MAX_COMPANIES, batches of
    COMPANY_LLM_CLASSIFY_BATCH_SIZE, each call capped at COMPANY_LLM_CLASSIFY_TIMEOUT_S).

    Recognised answers are added to ``context.overrides`` and returned in
    ``outcome.overrides`` for the caller to persist (save_llm_overrides); jobs still
    Ambiguous whose company got a label are updated in place (hiring_entity +
    is_gcc). Skipped when disabled or no LLM provider is configured. Never raises."""
    outcome = LlmClassificationOutcome()
    try:
        if settings is None:
            from app.config import get_settings
            settings = get_settings()
        if not getattr(settings, "company_llm_classify_enabled", False):
            return outcome
        cap = max(0, int(getattr(settings, "company_llm_classify_max_companies", 50) or 0))
        batch_size = max(1, int(getattr(settings, "company_llm_classify_batch_size", 25) or 25))
        timeout_s = float(getattr(settings, "company_llm_classify_timeout_s", 90.0) or 90.0)
        items = _candidates(jobs, context, cap)
        if not items:
            return outcome
        if llm is None:
            from app.services.jd_llm_enrichment import llm_configured
            if not llm_configured(settings):
                logger.info("company_llm_classify_skipped", reason="no LLM provider configured",
                            companies=len(items))
                return outcome
            from app.services.llm_service import LLMService
            llm = LLMService(settings)

        from app.prompts.company_classification_prompts import (
            COMPANY_CLASSIFY_SCHEMA,
            COMPANY_CLASSIFY_SYSTEM_PROMPT,
            build_company_classify_content,
            parse_label,
        )

        verdicts: dict[str, str | None] = {}
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            numbered = [{**it, "id": i + 1} for i, it in enumerate(batch)]
            outcome.asked += len(batch)
            try:
                raw = await asyncio.wait_for(
                    llm.extract_json(
                        build_company_classify_content(numbered),
                        COMPANY_CLASSIFY_SCHEMA,
                        system=COMPANY_CLASSIFY_SYSTEM_PROMPT,
                        call_type=LlmCallType.COMPANY_CLASSIFY,
                    ),
                    timeout=timeout_s,
                )
            except Exception as exc:
                # Provider down / timeout / bad JSON — these companies stay Ambiguous
                # and uncached, so a later run asks again.
                logger.warning("company_llm_classify_batch_failed", companies=len(batch),
                               error=str(exc) or type(exc).__name__)
                continue
            answers = raw.get("companies") if isinstance(raw, dict) else None
            by_id = {it["id"]: it for it in numbered}
            for ans in answers if isinstance(answers, list) else []:
                if not isinstance(ans, dict):
                    continue
                try:
                    it = by_id.get(int(ans.get("id")))
                except (TypeError, ValueError):
                    it = None
                if it is None or it["key"] in verdicts:
                    continue
                ok, entity = parse_label(ans.get("classification"))
                if ok:
                    verdicts[it["key"]] = entity

        names = {it["key"]: it["company"] for it in items}
        for key, entity in verdicts.items():
            ov = CompanyOverride(company_key=key, company_name=names.get(key, key),
                                 hiring_entity=entity, source=OVERRIDE_SOURCE_LLM)
            outcome.overrides.append(ov)
            context.overrides.setdefault(key, ov)
            if entity:
                outcome.classified += 1
        for j in jobs:
            if j.hiring_entity != "Ambiguous":
                continue
            entity = verdicts.get(normalize_company_key(j.company))
            if entity:
                j.hiring_entity = entity
                j.is_gcc = entity == "GCC"
                outcome.jobs_updated += 1
        logger.info("company_llm_classify_done", asked=outcome.asked, answered=len(verdicts),
                    classified=outcome.classified, jobs_updated=outcome.jobs_updated)
    except Exception as exc:  # never fail the harvest
        logger.warning("company_llm_classify_failed", error=str(exc) or type(exc).__name__)
    finally:
        if llm is not None and hasattr(llm, "get_call_log"):
            try:
                outcome.call_log = [c for c in llm.get_call_log()
                                    if c.get("call_type") == LlmCallType.COMPANY_CLASSIFY]
            except Exception:
                outcome.call_log = []
        if llm is not None and hasattr(llm, "get_usage_summary"):
            try:
                outcome.token_usage = dict(llm.get_usage_summary() or {})
            except Exception:
                outcome.token_usage = {}
    return outcome
