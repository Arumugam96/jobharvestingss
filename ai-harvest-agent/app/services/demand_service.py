"""Demand Search — client job requirements, uploaded as a spreadsheet, searched on
Naukri Resdex with the existing candidate-matching pipeline. No harvesting and
no outreach: a Demand job is never emailed, WhatsApp'd or messaged.

A batch reuses the harvest plumbing end to end:

  batch    one HarvestRunORM (source "Demand", run_id ``YYYYMMDD_HHMMSS_demand``)
           under the run_guard single-flight slot — Demand Search drives the same
           recruiter browser and draws on the same Resdex daily quota as harvest
           outreach, so it never runs beside a harvest. Batch metadata (file,
           uploader, invalid rows) and every row's live status live in
           filters_snapshot — no new tables or columns.
  rows     one ScrapedJobORM per valid row (source "Demand", synthetic job_url from
           demand_import.demand_job_url, company "" so bulk_insert_scraped_jobs never
           attaches an Apollo company+location HR contact — the client name lives in
           the snapshot only). Excluded from every harvest view by default
           (harvest_run_service.DEMAND_SOURCE).
  search   resdex_match_service.prepare_resdex_candidates, one row per call so the
           batch page shows per-row progress, with contact_kind "demand" (always
           selected) and the optional Skills / Experience / Location / Client
           columns in the job view as extra LLM context. Pacing, caps, CAPTCHA
           hand-off and stop handling stay in resdex_candidate_agent; between rows
           the same RESDEX_DELAY_* gap / RESDEX_BREAK_* break keeps the cadence a
           single long batch would have.

Row statuses: queued, in_progress (sub_step requirements | searching), matched,
reused, no_results, queries_only, cap_reached, session_expired, error, stopped.

Without a Naukri recruiter login a batch still runs, in queries-only mode: each
row gets the LLM requirements + T1–T3 queries (status queries_only) but Resdex is
not searched (no browser, no quota, no pacing gap). A session that expires
mid-batch gets prepare_resdex_candidates' one login wait; after that the
remaining rows continue as queries only until a newer recorded login appears.
"""
from __future__ import annotations

import asyncio
import copy
import random
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.orm import noload

from app.config import get_settings
from app.core.job_url import normalize_job_url
from app.core.tenant_context import apply_tenant, get_current_tenant_id
from app.models.harvest_run import HarvestRunORM
from app.models.resdex import ResdexSearchORM, ResdexSearchStatus
from app.services import resdex_match_service as rms
from app.services import run_guard
from app.services.harvest_run_service import (
    DEMAND_SOURCE,
    HarvestRunService,
    db_read,
    db_write,
    scraped_job_view,
)
from app.services.job_tracker import JobTracker

logger = structlog.get_logger(__name__)

ROW_QUEUED = "queued"
ROW_IN_PROGRESS = "in_progress"
ROW_MATCHED = "matched"
ROW_REUSED = "reused"
ROW_STOPPED = "stopped"
ROW_QUERIES_ONLY = ResdexSearchStatus.QUERIES_ONLY
ACTIVE_ROW_STATUSES = (ROW_QUEUED, ROW_IN_PROGRESS)
ATTENTION_ROW_STATUSES = (
    ResdexSearchStatus.CAP_REACHED, ResdexSearchStatus.SESSION_EXPIRED, ResdexSearchStatus.ERROR,
)
_STOP_MSG = "harvest stop requested"   # resdex_candidate_agent's stop outcome
_POLL_S = 5.0


class DemandError(Exception):
    """A precondition failed — the route turns it into an HTTP error whose detail
    is {code, message, **details} (the UI keys its pop-ups/banners on ``code``)."""

    def __init__(self, status_code: int, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def to_detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.details}


def make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_demand"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


# ── Reuse lookup (validation preview) ─────────────────────────────────────────

async def reuse_lookup(job_urls: list[str]) -> dict[str, dict]:
    """{normalized job_url: {"searched_at", "candidates"}} — the newest MATCHED
    search with candidates within RESDEX_REUSE_DAYS for this tenant: exactly what
    prepare_resdex_candidates will reuse instead of searching (no quota used)."""
    days = int(get_settings().resdex_reuse_days or 0)
    keys = [k for k in {normalize_job_url(u) for u in job_urls or []} if k]
    if days <= 0 or not keys:
        return {}
    tenant_id = get_current_tenant_id()
    since = _now() - timedelta(days=days)

    async def _q(db):
        stmt = (
            select(ResdexSearchORM)
            .where(
                ResdexSearchORM.tenant_id == tenant_id,
                ResdexSearchORM.job_url.in_(keys),
                ResdexSearchORM.status == ResdexSearchStatus.MATCHED,
                ResdexSearchORM.created_at >= since,
            )
            .order_by(ResdexSearchORM.created_at.desc())
        )
        out: dict[str, dict] = {}
        for s in (await db.execute(stmt)).scalars().all():
            if s.job_url not in out and s.candidates:
                out[s.job_url] = {"searched_at": _iso(s.created_at),
                                  "candidates": len(s.candidates)}
        return out

    return await db_read(_q) or {}


async def annotate_reuse(preview: dict) -> dict:
    """Flag the valid rows a recent match will serve ("Will reuse existing match
    (no quota used)"): preview["reuse"] plus row["reuse"] / preview check "reuse"."""
    found = await reuse_lookup([r["job_url"] for r in preview["rows"]])
    reuse_rows = set()
    preview["reuse"] = []
    for r in preview["rows"]:
        hit = found.get(normalize_job_url(r["job_url"]))
        r["reuse"] = bool(hit)
        if hit:
            reuse_rows.add(r["row"])
            preview["reuse"].append({"row": r["row"], "job_title": r["job_title"], **hit})
    for p in preview["preview"]:
        if p["check"] == "ok" and p["row"] in reuse_rows:
            p["check"] = "reuse"
    preview["new_searches"] = preview["valid_rows"] - len(preview["reuse"])
    preview["reuse_days"] = int(get_settings().resdex_reuse_days or 0)
    return preview


# ── Preconditions ─────────────────────────────────────────────────────────────

def recruiter_session() -> dict:
    """The recorded Naukri recruiter check (no browser): {logged_in,
    login_window_open, checked_at, method, state}."""
    from app.agents.naukri_recruiter_agent import read_status

    st = read_status()
    last = st.get("last_check") or {}
    return {
        "logged_in": bool(st.get("logged_in")),
        "login_window_open": bool(st.get("login_window_open")),
        "checked_at": last.get("checked_at"),
        "method": last.get("method"),
        "has_record": bool(last),
    }


def require_resdex_ready() -> None:
    """Resdex on and no recruiter login window open, else DemandError. A logged-out
    session is fine — the batch then runs in queries-only mode."""
    if not get_settings().resdex_enabled:
        raise DemandError(
            409, "resdex_disabled",
            "Resdex is disabled on this server (RESDEX_ENABLED=false), so Demand Search "
            "can't search for candidates.",
        )
    sess = recruiter_session()
    if sess["login_window_open"]:
        raise DemandError(
            409, "session_busy",
            "A Naukri recruiter login window is open. Finish logging in, then start again.",
        )


async def _harvest_conflict() -> dict | None:
    """Details of a running harvest (in-process first, then the DB backstop) for
    the "A harvest is running" pop-up, or None."""
    conflict = run_guard.active_run() or await run_guard.db_conflict()
    if conflict is None:
        return None
    info = {"job_id": conflict.get("job_id"), "run_id": conflict.get("run_id"),
            "source": conflict.get("source")}
    job_id = conflict.get("job_id")
    if job_id:
        run = await db_read(lambda db: HarvestRunService(db).get_by_job_id(job_id))
        if run is not None:
            info.update(started_at=_iso(run.started_at), progress=run.progress, message=run.message)
    return info


async def _claim(job_id: str | None, run_id: str) -> None:
    """The single-flight slot (DB backstop, then the atomic in-process claim) —
    DemandError 409 "harvest_running" when a harvest or another batch holds it.
    Pair with run_guard.end()."""
    db_conflict = await run_guard.db_conflict()
    if db_conflict is None and run_guard.try_begin(job_id, run_id, DEMAND_SOURCE) is None:
        return
    info = await _harvest_conflict() or db_conflict or {}
    raise DemandError(
        409, "harvest_running",
        "A harvest is running. Demand Search uses the same recruiter browser and Resdex "
        "quota, so it can't run at the same time. Start it again after the harvest finishes.",
        harvest=info,
    )


# ── Snapshot helpers ──────────────────────────────────────────────────────────

def _snapshot(preview: dict, *, batch_name: str, uploaded_by: str) -> dict:
    return {
        "kind": "demand",
        "filename": preview["filename"],
        "batch_name": (batch_name or "").strip()[:200] or preview["filename"].rsplit(".", 1)[0],
        "uploaded_by": uploaded_by or "",
        "size_label": preview.get("size_label", ""),
        "sheet": preview.get("sheet", ""),
        "total_rows": preview["total_rows"],
        "valid_rows": preview["valid_rows"],
        "invalid_rows": len(preview["invalid"]),
        "invalid": preview["invalid"],
        "current_row": None,
        "rows": [
            {
                "row": r["row"], "job_id": None, "job_url": r["job_url"],
                "job_title": r["job_title"], "client": r["client"], "location": r["location"],
                "experience": r["experience"], "skills": r["skills_text"],
                "reuse_expected": bool(r.get("reuse")),
                "status": ROW_QUEUED, "sub_step": "", "candidates": 0, "top_coverage": None,
                "tier": 0, "searches_used": 0, "searched_at": None, "reused_from": None,
                "error": "",
            }
            for r in preview["rows"]
        ],
    }


def _job_dict(r: dict, posted: str) -> dict:
    """scraped_jobs row for one valid upload row. company stays "" (see module doc)."""
    return {
        "source": DEMAND_SOURCE, "job_title": r["job_title"], "company": "",
        "location": r["location"], "experience": r["experience"], "posted_date": posted,
        "job_url": r["job_url"], "job_description": r["job_description"],
        "skills": r["skills"], "scraped_at": _now(),
    }


async def _save(run_pk: str, snap: dict, **fields: Any) -> None:
    """Persist the snapshot (a fresh copy, so the JSON column is always rewritten)."""
    data = copy.deepcopy(snap)
    await db_write(lambda db: HarvestRunService(db).update_run(
        run_pk, filters_snapshot=data, **fields,
    ))


def summarize_rows(rows: list[dict]) -> dict:
    """Header counters of a batch from its rows."""
    by: dict[str, int] = {}
    for r in rows:
        by[r.get("status") or ROW_QUEUED] = by.get(r.get("status") or ROW_QUEUED, 0) + 1
    finished = sum(v for k, v in by.items() if k not in ACTIVE_ROW_STATUSES)
    return {
        "total": len(rows),
        "finished": finished,
        "queued": by.get(ROW_QUEUED, 0),
        "in_progress": by.get(ROW_IN_PROGRESS, 0),
        "matched": by.get(ROW_MATCHED, 0),
        "reused": by.get(ROW_REUSED, 0),
        "no_results": by.get(ResdexSearchStatus.NO_RESULTS, 0),
        "queries_only": by.get(ROW_QUERIES_ONLY, 0),
        "cap_reached": by.get(ResdexSearchStatus.CAP_REACHED, 0),
        "session_expired": by.get(ResdexSearchStatus.SESSION_EXPIRED, 0),
        "error": by.get(ResdexSearchStatus.ERROR, 0),
        "stopped": by.get(ROW_STOPPED, 0),
        "attention": sum(by.get(k, 0) for k in ATTENTION_ROW_STATUSES),
        "clients": len({(r.get("client") or "").strip().lower() for r in rows} - {""}),
        "searches_used": sum(int(r.get("searches_used") or 0) for r in rows),
        "candidates_kept": sum(int(r.get("candidates") or 0) for r in rows
                               if r.get("status") == ROW_MATCHED),
    }


# ── Start ─────────────────────────────────────────────────────────────────────

async def start_batch(preview: dict, *, batch_name: str, uploaded_by: str) -> dict:
    """Create the batch run + job rows and launch the detached search task.
    Raises DemandError (Resdex off, recruiter logged out, harvest running, no valid
    rows). Returns {job_id, run_id}."""
    if not preview["rows"]:
        raise DemandError(422, "no_valid_rows",
                          "No valid rows to search. Fix the rows listed and upload again.")
    require_resdex_ready()
    run_id = make_run_id()
    job_id = uuid4().hex
    await _claim(job_id, run_id)
    try:
        snap = _snapshot(preview, batch_name=batch_name, uploaded_by=uploaded_by)
        started = _now()
        JobTracker.create(job_id, run_id)
        run_pk = await db_write(lambda db: HarvestRunService(db).create_run(
            run_id=run_id, job_id=job_id, source=DEMAND_SOURCE, sources=[DEMAND_SOURCE],
            filters_snapshot=copy.deepcopy(snap), started_at=started,
        ))
        if not run_pk:
            raise DemandError(503, "db_unavailable",
                              "The database is unavailable — nothing was started.")
        posted = started.strftime("%Y-%m-%d")
        dicts = [_job_dict(r, posted) for r in preview["rows"]]
        await db_write(lambda db: HarvestRunService(db).bulk_insert_scraped_jobs(run_pk, dicts))
        jobs = await db_read(lambda db: HarvestRunService(db).list_jobs_for_run(run_pk)) or []
        ids = {normalize_job_url(j.job_url): j.id for j in jobs}
        for r in snap["rows"]:
            r["job_id"] = ids.get(normalize_job_url(r["job_url"]))
        await _save(run_pk, snap, message=f"Queued — {len(snap['rows'])} rows")
        logger.info("demand_batch_queued", job_id=job_id, run_id=run_id, rows=len(snap["rows"]),
                    invalid=snap["invalid_rows"])
        asyncio.create_task(_run_batch(job_id, run_id, run_pk), name=f"demand-{job_id}")
    except Exception:
        # The task never started → its finally: run_guard.end() won't fire.
        run_guard.end()
        raise
    mode = "search" if recruiter_session()["logged_in"] else "queries_only"
    return {"job_id": job_id, "run_id": run_id, "mode": mode}


# ── Background batch ──────────────────────────────────────────────────────────

async def _load_run(run_pk: str) -> HarvestRunORM | None:
    async def _q(db):
        stmt = select(HarvestRunORM).options(noload(HarvestRunORM.jobs)).where(
            HarvestRunORM.id == run_pk,
        )
        return (await db.execute(stmt)).scalars().first()
    return await db_read(_q)


async def _target(job_id: str | None, row: dict) -> dict | None:
    """The prepare_resdex_candidates target for one row: the stored job's view
    (title, JD, Skills, Experience, Location) plus the Client column as context."""
    if not job_id:
        return None
    job = await db_read(lambda db: HarvestRunService(db).get_scraped_job_by_id(job_id))
    if job is None:
        return None
    view = scraped_job_view(job)
    view["client"] = row.get("client") or ""
    return {
        "view": view, "job_id": job.id, "job_url": job.job_url,
        "contact_kind": rms.DEMAND_CONTACT_KIND, "contact_origin": "demand",
        "job_title": job.job_title, "company": row.get("client") or "",
    }


async def _search_ids(norm: str) -> set[str]:
    tenant_id = get_current_tenant_id()

    async def _q(db):
        stmt = select(ResdexSearchORM.id).where(
            ResdexSearchORM.tenant_id == tenant_id, ResdexSearchORM.job_url == norm,
        )
        return set((await db.execute(stmt)).scalars().all())
    return await db_read(_q) or set()


async def _apply_outcome(row: dict, norm: str, before: set[str], out: dict) -> None:
    """Fill a row's final status from the search prepare_resdex_candidates stored
    for it (any search id not seen before the call), or — when none was stored but
    candidates came back — the reused match."""
    tenant_id = get_current_tenant_id()

    async def _q(db):
        stmt = (
            select(ResdexSearchORM)
            .where(ResdexSearchORM.tenant_id == tenant_id, ResdexSearchORM.job_url == norm)
            .order_by(ResdexSearchORM.created_at.desc())
        )
        return list((await db.execute(stmt)).scalars().all())

    searches = await db_read(_q) or []
    new = next((s for s in searches if s.id not in before), None)
    row["sub_step"] = ""
    if new is not None:
        status = new.status
        if status == ResdexSearchStatus.SESSION_BUSY:
            status = ResdexSearchStatus.SESSION_EXPIRED
        elif status == ResdexSearchStatus.SKIPPED_NO_JD:
            status = ResdexSearchStatus.ERROR
        cov = [c.skill_coverage for c in new.candidates if c.skill_coverage is not None]
        row.update(
            status=status, candidates=len(new.candidates), top_coverage=max(cov) if cov else None,
            tier=new.tier or 0, searches_used=new.searches_used or 0,
            searched_at=_iso(new.created_at), reused_from=None, error=(new.error or "")[:300],
        )
        if status == ResdexSearchStatus.ERROR and _STOP_MSG in (new.error or "") \
                and run_guard.is_stop_requested():
            row.update(status=ROW_STOPPED, error="")
        return
    cands = next(iter(out.values()), None) if out else None
    if cands:
        cov = [c.get("skill_coverage") for c in cands if c.get("skill_coverage") is not None]
        src = next((s for s in searches if s.id == cands[0].get("search_id")), None)
        row.update(
            status=ROW_REUSED, candidates=len(cands), top_coverage=max(cov) if cov else None,
            tier=(src.tier if src else 0) or 0, searches_used=0,
            searched_at=_iso(src.created_at) if src else None,
            reused_from=_iso(src.created_at) if src else None, error="",
        )
        return
    row.update(status=ResdexSearchStatus.ERROR, error="Resdex did not search this job.")


async def _sleep_or_stop(seconds: float) -> bool:
    """Sleep in ≤5 s chunks; False as soon as a stop is requested."""
    left = max(0.0, float(seconds))
    while left > 0:
        if run_guard.is_stop_requested():
            return False
        chunk = min(_POLL_S, left)
        await asyncio.sleep(chunk)
        left -= chunk
    return not run_guard.is_stop_requested()


async def _row_gap(searches_total: int, breaks_taken: int) -> tuple[bool, int]:
    """The pacing a single long batch would have between two searched rows: the
    RESDEX_DELAY_* gap, plus a RESDEX_BREAK_* break every RESDEX_BREAK_EVERY_SEARCHES
    searches. Returns (not stopped, breaks taken)."""
    s = get_settings()
    lo = max(0, int(s.resdex_delay_min_ms))
    hi = max(lo, int(s.resdex_delay_max_ms))
    if not await _sleep_or_stop(random.randint(lo, hi) / 1000):
        return False, breaks_taken
    every = int(s.resdex_break_every_searches or 0)
    if every > 0 and searches_total // every > breaks_taken:
        b_lo = max(0.0, float(s.resdex_break_min_s))
        b_hi = max(b_lo, float(s.resdex_break_max_s))
        logger.info("demand_resdex_break", after_searches=searches_total)
        return await _sleep_or_stop(random.uniform(b_lo, b_hi)), searches_total // every
    return True, breaks_taken


async def _search_row(run_id: str, run_pk: str, snap: dict, row: dict, *, reuse: bool,
                      queries_only: bool = False) -> None:
    """One row through prepare_resdex_candidates with live sub-steps."""
    row.update(status=ROW_IN_PROGRESS, sub_step="requirements", error="")
    await _save(run_pk, snap)
    target = await _target(row.get("job_id"), row)
    if target is None:
        row.update(status=ResdexSearchStatus.ERROR, sub_step="", error="The job row is missing.")
        return
    norm = normalize_job_url(target["job_url"])
    before = await _search_ids(norm)

    async def _stage(stage: str) -> None:
        row["sub_step"] = stage
        await _save(run_pk, snap)

    out = await rms.prepare_resdex_candidates(
        [target], run_id=run_id, reuse=reuse, on_stage=_stage, queries_only=queries_only,
    )
    await _apply_outcome(row, norm, before, out)


async def _run_batch(job_id: str, run_id: str, run_pk: str) -> None:
    """Background entry point — always releases the single-flight guard."""
    try:
        await _run_batch_impl(job_id, run_id, run_pk)
    finally:
        run_guard.end()


async def _run_batch_impl(job_id: str, run_id: str, run_pk: str) -> None:
    log = logger.bind(job_id=job_id, run_id=run_id, source=DEMAND_SOURCE)
    run = await _load_run(run_pk)
    if run is None or not isinstance(run.filters_snapshot, dict):
        log.error("demand_batch_missing")
        return
    snap = copy.deepcopy(run.filters_snapshot)
    rows: list[dict] = snap.get("rows") or []
    total = len(rows)
    # Recorded checked_at when a logged-in session turned out expired mid-batch (the
    # one login wait already failed): later rows stay queries-only until a newer
    # recorded login, so no row opens the browser / waits again.
    expired_at: Any = None
    capped = stopped = False
    searches_total = breaks_taken = 0
    searched_prev = False
    try:
        for i, row in enumerate(rows):
            if run_guard.is_stop_requested():
                stopped = True
            if stopped:
                row.update(status=ROW_STOPPED, sub_step="")
                continue
            if capped and not row.get("reuse_expected"):
                row.update(status=ResdexSearchStatus.CAP_REACHED, sub_step="",
                           error="daily Resdex cap reached")
                continue
            if searched_prev and not row.get("reuse_expected"):
                ok, breaks_taken = await _row_gap(searches_total, breaks_taken)
                if not ok:
                    stopped = True
                    row.update(status=ROW_STOPPED, sub_step="")
                    continue
            snap["current_row"] = row["row"]
            msg = f"Row {i + 1} of {total} · {row['job_title']}"
            JobTracker.update(job_id, progress=int(i * 100 / max(1, total)), message=msg)
            await _save(run_pk, snap, message=msg, progress=int(i * 100 / max(1, total)))

            sess = recruiter_session()
            relogged = sess["logged_in"] and sess.get("checked_at") != expired_at
            if expired_at is not None and relogged:
                expired_at = None                     # logged in again (Connect)
            await _search_row(run_id, run_pk, snap, row, reuse=True,
                              queries_only=expired_at is not None)

            searches_total += int(row.get("searches_used") or 0)
            # Pace only after real Resdex traffic (not reused / queries-only rows).
            searched_prev = int(row.get("searches_used") or 0) > 0
            if row["status"] == ROW_STOPPED:
                stopped = True
            elif row["status"] == ResdexSearchStatus.CAP_REACHED:
                capped = True
            elif row["status"] == ROW_QUERIES_ONLY and sess["logged_in"] and expired_at is None:
                # Recorded as logged in, yet the browser found it gone and the one
                # login wait failed — the rest of the batch runs queries-only.
                expired_at = recruiter_session().get("checked_at") or "expired"
            await _save(run_pk, snap)
    except Exception as exc:
        log.exception("demand_batch_failed", error=str(exc))
        for row in rows:
            if row.get("status") in ACTIVE_ROW_STATUSES:
                row.update(status=ResdexSearchStatus.ERROR, sub_step="", error=str(exc)[:300])
        snap["current_row"] = None
        message = f"Demand Search failed: {exc}"
        JobTracker.update(job_id, status="failed", progress=100, message=message, error=str(exc),
                          completed_at=_now().isoformat())
        await _save(run_pk, snap, status="failed", progress=100, message=message,
                    error=str(exc)[:1000], completed_at=_now())
        return

    snap["current_row"] = None
    counts = summarize_rows(rows)
    found = counts["matched"] + counts["reused"]
    queries = f", {counts['queries_only']} queries only" if counts["queries_only"] else ""
    if stopped:
        status, message = "stopped", f"Stopped — {found} of {total} jobs have candidates{queries}"
    else:
        status, message = "success", f"Finished — {found} of {total} jobs have candidates{queries}"
    log.info("demand_batch_completed", status=status, **{k: counts[k] for k in (
        "matched", "reused", "no_results", "queries_only", "attention", "stopped")})
    JobTracker.update(job_id, status=status, progress=100, message=message,
                      completed_at=_now().isoformat())
    await _save(run_pk, snap, status=status, progress=100, message=message, completed_at=_now())


# ── Single-job re-run ─────────────────────────────────────────────────────────

async def _demand_job_and_run(job_id: str):
    """(ScrapedJobORM, HarvestRunORM) of a Demand job visible to this tenant, or
    (None, None)."""
    async def _q(db):
        job = await HarvestRunService(db).get_scraped_job_by_id(job_id)
        if job is None or (job.source or "").lower() != DEMAND_SOURCE.lower():
            return None, None
        stmt = select(HarvestRunORM).options(noload(HarvestRunORM.jobs)).where(
            HarvestRunORM.id == job.run_id,
        )
        return job, (await db.execute(stmt)).scalars().first()
    return await db_read(_q) or (None, None)


def _row_for(snap: dict, job_id: str) -> dict | None:
    return next((r for r in (snap.get("rows") or []) if r.get("job_id") == job_id), None)


async def rerun_job(job_id: str) -> dict:
    """Search Resdex again for one Demand job (fresh search, reuse off) in a
    detached task under the single-flight slot. Raises DemandError. Returns
    {job_id, run_id, status: "in_progress"}."""
    job, run = await _demand_job_and_run(job_id)
    if job is None or run is None or not isinstance(run.filters_snapshot, dict):
        raise DemandError(404, "not_found", f"Demand job '{job_id}' not found.")
    if run.status == "running":
        raise DemandError(409, "batch_running",
                          "This job's batch is still running. Re-run it after the batch finishes.")
    snap = copy.deepcopy(run.filters_snapshot)
    row = _row_for(snap, job_id)
    if row is None:
        raise DemandError(404, "not_found", f"Demand job '{job_id}' is not in its batch.")
    require_resdex_ready()
    await _claim(None, run.run_id)
    try:
        row.update(status=ROW_IN_PROGRESS, sub_step="requirements", error="")
        await _save(run.id, snap)
        asyncio.create_task(_rerun_background(run.run_id, run.id, job_id),
                            name=f"demand-rerun-{job_id}")
    except Exception:
        run_guard.end()
        raise
    logger.info("demand_rerun_started", job_id=job_id, run_id=run.run_id)
    return {"job_id": job_id, "run_id": run.run_id, "status": ROW_IN_PROGRESS}


async def _rerun_background(run_id: str, run_pk: str, job_id: str) -> None:
    try:
        run = await _load_run(run_pk)
        snap = copy.deepcopy(run.filters_snapshot) if run is not None else None
        row = _row_for(snap or {}, job_id)
        if row is None:
            return
        try:
            await _search_row(run_id, run_pk, snap, row, reuse=False)
        except Exception as exc:
            logger.warning("demand_rerun_failed", job_id=job_id, error=str(exc))
            row.update(status=ResdexSearchStatus.ERROR, sub_step="", error=str(exc)[:300])
        await _save(run_pk, snap)
    finally:
        run_guard.end()


# ── Stop ──────────────────────────────────────────────────────────────────────

def stop_batch(run_id: str) -> dict:
    """Cooperative stop of the running batch ``run_id`` (the shared stop signal —
    finishes the current row; finished rows are kept, the rest end as stopped)."""
    active = run_guard.active_run()
    if not active or active.get("run_id") != run_id or active.get("source") != DEMAND_SOURCE:
        return {"status": "idle", "message": "This batch is not running."}
    run_guard.request_stop()
    if active.get("job_id"):
        JobTracker.update(active["job_id"], message="Stop requested — finishing the current row…")
    logger.info("demand_stop_requested", run_id=run_id)
    return {"status": "stopping", "run_id": run_id,
            "message": "Stop requested — the batch stops after the current row."}


# ── Reads ─────────────────────────────────────────────────────────────────────

def _live_rows(run: HarvestRunORM) -> list[dict]:
    """The snapshot rows, with queued / in-progress rows of a batch that is no
    longer running (and has no re-run in flight) shown as stopped — e.g. after a
    server restart (fail_stale_running marks the run failed, not its rows)."""
    snap = run.filters_snapshot if isinstance(run.filters_snapshot, dict) else {}
    rows = [dict(r) for r in (snap.get("rows") or [])]
    active = run_guard.active_run() or {}
    if run.status != "running" and active.get("run_id") != run.run_id:
        for r in rows:
            if r.get("status") in ACTIVE_ROW_STATUSES:
                r.update(status=ROW_STOPPED, sub_step="", error=r.get("error") or "Interrupted")
    return rows


def batch_header(run: HarvestRunORM) -> dict:
    snap = run.filters_snapshot if isinstance(run.filters_snapshot, dict) else {}
    rows = _live_rows(run)
    counts = summarize_rows(rows)
    return {
        "run_id": run.run_id,
        "job_id": run.job_id,
        "status": run.status,
        "message": run.message or "",
        "error": run.error or "",
        "progress": run.progress,
        "filename": snap.get("filename", ""),
        "batch_name": snap.get("batch_name", ""),
        "uploaded_by": snap.get("uploaded_by", ""),
        "sheet": snap.get("sheet", ""),
        "total_rows": snap.get("total_rows", len(rows)),
        "valid_rows": snap.get("valid_rows", len(rows)),
        "invalid_rows": snap.get("invalid_rows", 0),
        "invalid": snap.get("invalid") or [],
        "current_row": snap.get("current_row"),
        "started_at": _iso(run.started_at or run.created_at),
        "completed_at": _iso(run.completed_at),
        "counts": counts,
    }


async def list_batches(limit: int = 100) -> list[dict]:
    async def _q(db):
        stmt = (
            select(HarvestRunORM)
            .options(noload(HarvestRunORM.jobs))
            .where(HarvestRunORM.source == DEMAND_SOURCE)
            .order_by(HarvestRunORM.created_at.desc())
            .limit(limit)
        )
        stmt = apply_tenant(stmt, HarvestRunORM.tenant_id)
        return list((await db.execute(stmt)).scalars().all())

    runs = await db_read(_q)
    if runs is None:
        raise DemandError(503, "db_unavailable", "Database unavailable.")
    out = []
    for r in runs:
        h = batch_header(r)
        h.pop("invalid", None)
        out.append(h)
    return out


async def _get_run(run_id: str) -> HarvestRunORM | None:
    async def _q(db):
        stmt = (
            select(HarvestRunORM)
            .options(noload(HarvestRunORM.jobs))
            .where(HarvestRunORM.run_id == run_id, HarvestRunORM.source == DEMAND_SOURCE)
            .order_by(HarvestRunORM.created_at.desc())
        )
        stmt = apply_tenant(stmt, HarvestRunORM.tenant_id)
        return (await db.execute(stmt)).scalars().first()
    return await db_read(_q)


async def get_batch(run_id: str) -> dict:
    """Batch header + every row with its live / final status."""
    run = await _get_run(run_id)
    if run is None:
        raise DemandError(404, "not_found", f"Demand Search batch '{run_id}' not found.")
    rows = [{k: v for k, v in r.items() if k != "job_url"} for r in _live_rows(run)]
    created = run.created_at or run.started_at
    return {**batch_header(run), "uploaded_on": created.strftime("%Y-%m-%d") if created else "",
            "rows": rows}


async def get_job(job_id: str) -> dict:
    """One Demand job: the stored job, its batch + row, the extra LLM context."""
    job, run = await _demand_job_and_run(job_id)
    if job is None:
        raise DemandError(404, "not_found", f"Demand job '{job_id}' not found.")
    row = (_row_for({"rows": _live_rows(run)}, job_id) or {}) if run is not None else {}
    view = scraped_job_view(job)
    return {
        "job": {k: view.get(k) for k in (
            "id", "job_title", "job_description", "location", "experience", "skills",
            "posted_date", "source", "job_url",
        )},
        "batch": batch_header(run) if run is not None else None,
        "row": {k: v for k, v in row.items() if k != "job_url"},
        "context": {
            "skills": row.get("skills") or "", "experience": row.get("experience") or "",
            "location": row.get("location") or "", "client": row.get("client") or "",
        },
        "job_url": job.job_url,
    }


async def latest_search_view(norm: str, view_fn) -> dict | None:
    """``view_fn`` (frontend_routes._resdex_search_view) of the newest Resdex search
    for this job_url and tenant — the one the job page shows — or None."""
    if not norm:
        return None
    tenant_id = get_current_tenant_id()

    async def _q(db):
        stmt = (
            select(ResdexSearchORM)
            .where(ResdexSearchORM.tenant_id == tenant_id, ResdexSearchORM.job_url == norm)
            .order_by(ResdexSearchORM.created_at.desc())
            .limit(1)
        )
        s = (await db.execute(stmt)).scalars().first()
        return view_fn(s) if s is not None else None
    return await db_read(_q)


async def active_run_info() -> dict | None:
    """The running harvest or batch (source, run_id, started_at, progress, message),
    or None — the page shows it before Start is pressed."""
    return await _harvest_conflict()
