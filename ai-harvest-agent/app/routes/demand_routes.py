"""Demand Search API (the /demand page) — see app/services/demand_service.py.

Endpoints (all under the shared auth guard; "demand/" is in harvest-agent/nginx.conf):
  POST /demand/validate                    parse + map + check an upload (writes nothing)
  POST /demand/upload                      start a batch → 202 {job_id, run_id}
  GET  /demand/batches                     this tenant's batches, newest first
  GET  /demand/batches/{run_id}            batch header + every row's live/final status
  POST /demand/batches/{run_id}/stop       stop the running batch after the current row
  GET  /demand/jobs/{job_id}               one Demand job + its batch row + LLM context
  POST /demand/jobs/{job_id}/rerun-resdex  fresh Resdex search for one job → 202
  GET  /demand/resdex-usage                today's shared Resdex quota + recruiter session

Errors carry {"detail": {code, message, …}}: 409 harvest_running (the UI's
"A harvest is running" pop-up — nothing is started or queued), 409
resdex_disabled / session_busy, 422 for an unusable file (plus the file error's
own fields). A logged-out recruiter session is not an error: the batch runs in
queries-only mode (requirements + T1–T3 queries, no Resdex search). Thin layer — the logic lives in
demand_service / demand_import.
"""
from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.core.dependencies import get_current_user
from app.core.job_url import normalize_job_url
from app.services import demand_service as ds
from app.services.demand_import import MAX_FILE_BYTES, DemandFileError, parse_and_validate
from app.services.resdex_budget import usage_today

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/demand", tags=["Demand Search"])


def _raise(exc: ds.DemandError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.to_detail()) from exc


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


@router.post("/validate")
async def validate_upload(file: UploadFile = File(...)) -> Any:
    """Parse the file, map the header synonyms, check every row and flag the rows a
    recent match (RESDEX_REUSE_DAYS) will serve. Writes nothing to the database."""
    preview = await _read_upload(file)
    # The full JDs stay server-side; the preview already carries a snippet.
    return {**preview, "rows": [
        {k: v for k, v in r.items() if k != "job_description"} for r in preview["rows"]
    ]}


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
async def list_batches() -> Any:
    try:
        batches = await ds.list_batches()
    except ds.DemandError as exc:
        _raise(exc)
    return {"total": len(batches), "batches": batches}


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
async def get_job(job_id: str) -> Any:
    """The job, its batch + row status, the optional columns sent to the LLM and its
    latest Resdex search (requirements, tiers, candidates — the same view as GET
    /jobs/{id}/outreach-detail)."""
    from app.routes.frontend_routes import _resdex_search_view

    try:
        out = await ds.get_job(job_id)
    except ds.DemandError as exc:
        _raise(exc)
    out["resdex"] = await ds.latest_search_view(normalize_job_url(out.pop("job_url")),
                                                _resdex_search_view)
    return out


@router.post("/jobs/{job_id}/rerun-resdex", status_code=status.HTTP_202_ACCEPTED)
async def rerun_resdex(job_id: str) -> Any:
    """Search Resdex again for this job only — a fresh search (the 14-day reuse is
    skipped). Same session / single-flight checks as a batch start."""
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
