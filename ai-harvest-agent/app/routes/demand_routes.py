"""Demand Search API (the /demand page) — see app/services/demand_service.py.

Endpoints (all under the shared auth guard; "demand/" is in harvest-agent/nginx.conf):
  POST /demand/validate                    parse + map + check an upload (writes nothing)
  POST /demand/upload                      start a batch → 202 {job_id, run_id}
  POST /demand/validate-json               check manually entered jobs (writes nothing)
  POST /demand/create                      start a batch from manual jobs → 202
  GET  /demand/batches                     this tenant's batches, newest first (filters, paged)
  GET  /demand/jobs                        every Demand job across batches (filters, paged)
  GET  /demand/batches/{run_id}            batch header + every row's live/final status
  POST /demand/batches/{run_id}/stop       stop the running batch after the current row
  GET  /demand/jobs/{job_id}               one Demand job + its batch row + LLM context
  POST /demand/jobs/{job_id}/rerun-resdex  fresh Resdex search for one job → 202
  GET  /demand/resdex-usage                today's shared Resdex quota + recruiter session

Errors carry {"detail": {code, message, …}}: 409 harvest_running (the UI's
"A harvest is running" pop-up — nothing is started or queued), 409
resdex_disabled / session_busy, 422 for an unusable file (plus the file error's
own fields) or invalid manual jobs (code "invalid_jobs" + errors [{job, field,
key, message}]). A logged-out recruiter session is not an error: the batch runs in
queries-only mode (requirements + T1–T3 queries, no Resdex search). Thin layer — the logic lives in
demand_service / demand_import.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from app.config import get_settings
from app.core.dependencies import get_current_user
from app.core.job_url import normalize_job_url
from app.models.auth import AuthenticatedUser
from app.services import demand_service as ds
from app.services.demand_import import (
    MAX_FILE_BYTES,
    MAX_ROWS,
    DemandFileError,
    metadata_errors,
    parse_and_validate,
    parse_and_validate_rows,
)
from app.services.harvest_run_service import HarvestRunService, db_read
from app.services.job_assignment_service import ensure_job_access
from app.services.resdex_budget import usage_today

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/demand", tags=["Demand Search"])


def _raise(exc: ds.DemandError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.to_detail()) from exc


async def _ensure_job_access(job_id: str, user: AuthenticatedUser) -> None:
    """403 when the job is assigned ("Assign to me") to someone else. A job that is
    missing (or a failed read) passes here, so demand_service raises its usual 404."""
    async def _q(db):
        job = await HarvestRunService(db).get_scraped_job_by_id(job_id)
        return job.assigned_to_email if job is not None else None
    ensure_job_access(await db_read(_q), user)


async def _read_upload(file: UploadFile) -> dict:
    """Parsed + reuse-annotated preview, or HTTP 422 with the file error."""
    # One byte over the limit is enough to reject — never buffer a huge upload.
    content = await file.read(MAX_FILE_BYTES + 1)
    try:
        preview = parse_and_validate(content, file.filename or "upload.csv")
    except DemandFileError as exc:
        detail = exc.to_dict()
        detail.setdefault("filename", file.filename or "")
        raise HTTPException(status_code=422, detail=detail) from exc
    return await ds.annotate_reuse(preview)


def _without_jds(preview: dict) -> dict:
    # The full JDs stay server-side; the preview already carries a snippet.
    return {**preview, "rows": [
        {k: v for k, v in r.items() if k != "job_description"} for r in preview["rows"]
    ]}


@router.post("/validate")
async def validate_upload(file: UploadFile = File(...)) -> Any:
    """Parse the file, map the header synonyms, check every row and flag the rows a
    recent match (RESDEX_REUSE_DAYS) will serve. Writes nothing to the database."""
    return _without_jds(await _read_upload(file))


# ── Manual entry ──────────────────────────────────────────────────────────────

class ManualJob(BaseModel):
    job_title: str = Field("", max_length=300)
    job_description: str = Field("", max_length=50_000)
    # Any → the model validator reports non-text values ("must be text") instead of
    # pydantic coercing them or rejecting them with a generic message.
    metadata: dict[str, Any] = Field(default_factory=dict)


class ManualBatchRequest(BaseModel):
    """POST /demand/validate-json and /demand/create body. Title / JD problems are
    row checks (listed as invalid rows, like an upload); the metadata dictionary
    rules (demand_import.metadata_errors) and the job count are request errors."""
    batch_name: str = Field("", max_length=200)
    jobs: list[ManualJob] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> ManualBatchRequest:
        errors: list[dict] = []
        if not self.jobs:
            errors.append({"job": None, "field": "jobs", "key": "",
                           "message": "Add at least one job."})
        elif len(self.jobs) > MAX_ROWS:
            errors.append({"job": None, "field": "jobs", "key": "",
                           "message": f"{len(self.jobs)} jobs entered; a batch holds up to "
                                      f"{MAX_ROWS}."})
        for i, job in enumerate(self.jobs):
            errors += [{"job": i + 1, "field": "metadata", **e}
                       for e in metadata_errors(job.metadata)]
        if errors:
            raise PydanticCustomError("invalid_jobs", "{message}", {
                "message": _invalid_message(errors), "errors": errors,
            })
        return self


def _invalid_message(errors: list[dict]) -> str:
    n = len(errors)
    return (f"{n} {'problem' if n == 1 else 'problems'} in the jobs — fix "
            f"{'it' if n == 1 else 'them'} and try again.")


def _manual_error_detail(exc: ValidationError) -> dict:
    """{"code": "invalid_jobs", "message", "errors": [{job, field, key, message}]}
    from the model's own rule errors or pydantic's type / length errors."""
    errors: list[dict] = []
    for e in exc.errors():
        if e.get("type") == "invalid_jobs":
            errors += (e.get("ctx") or {}).get("errors") or []
            continue
        loc = list(e.get("loc") or ())
        in_job = len(loc) > 1 and loc[0] == "jobs" and isinstance(loc[1], int)
        errors.append({
            "job": loc[1] + 1 if in_job else None,
            "field": str(loc[2]) if in_job and len(loc) > 2 else str(loc[0] if loc else ""),
            "key": str(loc[3]) if in_job and len(loc) > 3 else "",
            "message": e.get("msg", ""),
        })
    return {"code": "invalid_jobs", "message": _invalid_message(errors), "errors": errors}


async def _read_manual(payload: Any) -> tuple[ManualBatchRequest, dict]:
    """(request, reuse-annotated preview) or HTTP 422."""
    try:
        req = ManualBatchRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_manual_error_detail(exc)) from exc
    try:
        preview = parse_and_validate_rows([j.model_dump() for j in req.jobs])
    except DemandFileError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    return req, await ds.annotate_reuse(preview)


@router.post("/validate-json")
async def validate_manual(payload: Any = Body(...)) -> Any:
    """Check manually entered jobs — the same response as POST /demand/validate
    (filename "Manual entry", no sheet / column mapping, source_kind "manual").
    Writes nothing."""
    _, preview = await _read_manual(payload)
    return _without_jds(preview)


@router.post("/create", status_code=status.HTTP_202_ACCEPTED)
async def create_manual(payload: Any = Body(...), current_user=Depends(get_current_user)) -> Any:
    """Start a batch from manually entered jobs — the same checks, response and
    errors as POST /demand/upload (harvest_running, resdex_disabled, session_busy,
    no_valid_rows)."""
    req, preview = await _read_manual(payload)
    try:
        started = await ds.start_batch(preview, batch_name=req.batch_name,
                                       uploaded_by=getattr(current_user, "email", "") or "")
    except ds.DemandError as exc:
        _raise(exc)
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={
        **started, "status": "running", "source": "Demand",
        "message": "Demand Search started — poll GET /demand/batches/{run_id}",
    })


@router.post("/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_and_start(
    file: UploadFile = File(...),
    batch_name: str = Form(""),
    current_user=Depends(get_current_user),
) -> Any:
    """Re-validate the file, check Resdex (on, no login window open) and the
    single-flight slot, then create the batch (HarvestRun source "Demand" + one job
    row per valid row) and start the detached search. Poll GET
    /demand/batches/{run_id}."""
    preview = await _read_upload(file)
    try:
        started = await ds.start_batch(preview, batch_name=batch_name,
                                       uploaded_by=getattr(current_user, "email", "") or "")
    except ds.DemandError as exc:
        _raise(exc)
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={
        **started, "status": "running", "source": "Demand",
        "message": "Demand Search started — poll GET /demand/batches/{run_id}",
    })


@router.get("/batches")
async def list_batches(
    q: str = Query("", max_length=200),
    status_: str = Query("", alias="status", max_length=20),
    source: Literal["", "file", "manual"] = Query(""),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Any:
    """This tenant's batches, newest first. ``total`` is the number matching the
    filters (q: batch name / file / uploader / run id; status: running |
    completed | stopped | failed; source: file | manual; uploaded date range,
    inclusive UTC days)."""
    try:
        return await ds.search_batches(q=q, status=status_, source=source, date_from=date_from,
                                       date_to=date_to, limit=limit, offset=offset)
    except ds.DemandError as exc:
        _raise(exc)


@router.get("/jobs")
async def list_jobs(
    q: str = Query("", max_length=200),
    status_: str = Query("", alias="status", max_length=20),
    source: Literal["", "file", "manual"] = Query(""),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Any:
    """Every Demand job of this tenant across batches (the batch page's row shape +
    run_id, batch_name, source_kind, batch_status, uploaded_on, started_at).
    status takes the jobs table's filter values (all | active | finished | matched |
    reused | no_results | queries_only | attention | cap_reached | session_expired
    | error | stopped). ``counts`` ignore the status filter."""
    try:
        return await ds.list_jobs(q=q, status=status_, source=source, date_from=date_from,
                                  date_to=date_to, limit=limit, offset=offset)
    except ds.DemandError as exc:
        _raise(exc)


@router.get("/batches/{run_id}")
async def get_batch(run_id: str) -> Any:
    try:
        return await ds.get_batch(run_id)
    except ds.DemandError as exc:
        _raise(exc)


@router.post("/batches/{run_id}/stop")
async def stop_batch(run_id: str) -> Any:
    return ds.stop_batch(run_id)


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str, current_user: AuthenticatedUser = Depends(get_current_user),
) -> Any:
    """The job, its batch + row status, the optional columns sent to the LLM and its
    latest Resdex search (requirements, tiers, candidates — the same view as GET
    /jobs/{id}/outreach-detail). 403 when the job is assigned to someone else."""
    from app.routes.frontend_routes import _resdex_search_view

    await _ensure_job_access(job_id, current_user)
    try:
        out = await ds.get_job(job_id)
    except ds.DemandError as exc:
        _raise(exc)
    out["resdex"] = await ds.latest_search_view(normalize_job_url(out.pop("job_url")),
                                                _resdex_search_view)
    return out


@router.post("/jobs/{job_id}/rerun-resdex", status_code=status.HTTP_202_ACCEPTED)
async def rerun_resdex(
    job_id: str, current_user: AuthenticatedUser = Depends(get_current_user),
) -> Any:
    """Search Resdex again for this job only — a fresh search (the 14-day reuse is
    skipped). Same session / single-flight checks as a batch start. 403 when the job
    is assigned to someone else."""
    await _ensure_job_access(job_id, current_user)
    try:
        started = await ds.rerun_job(job_id)
    except ds.DemandError as exc:
        _raise(exc)
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=started)


@router.get("/resdex-usage")
async def resdex_usage() -> Any:
    """Today's ONE account-wide Resdex quota (per UTC day, shared with harvest
    outreach) and the recorded Naukri recruiter session (no browser launch)."""
    s = get_settings()
    return {
        "resdex_enabled": bool(s.resdex_enabled),
        "reuse_days": int(s.resdex_reuse_days or 0),
        "usage": await usage_today(),
        "session": ds.recruiter_session(),
        "active_run": await ds.active_run_info(),
    }
