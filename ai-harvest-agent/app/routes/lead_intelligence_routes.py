"""
Lead Intelligence API routes.

POST /run-lead-intelligence      — start hybrid LinkedIn + Naukri lead harvest (async)
GET  /download/lead-intelligence/json   — download latest leads JSON file
GET  /download/lead-intelligence/excel  — download latest leads Excel file

GET /lead-intelligence and /lead-intelligence/{id} are served by
app/routes/frontend_routes.py (its router is registered first, so duplicates
here were unreachable and have been removed).

The download files are shared (not tenant-tagged): only an all-access (internal)
tenant may download them; any other tenant gets 404.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.tenant_context import get_current_tenant_id, is_all_access
from app.models.lead_models import LeadIntelligenceRequest, LeadIntelligenceResult

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Lead Intelligence"])

_LEAD_OUTPUT_DIR = Path("data/results/lead_intelligence")

# ── In-memory job tracker for lead intelligence runs ──────────────────────────
# (lightweight — reuses same pattern as JobTracker for harvest runs)
_lead_jobs: dict[str, dict[str, Any]] = {}


# ══════════════════════════════════════════════════════════════════════════════
# POST /run-lead-intelligence
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/run-lead-intelligence",
    status_code = 202,
    summary     = "Start Lead Intelligence Run",
    description = (
        "Launches a background Hybrid Lead Intelligence run.\n\n"
        "Flow: LinkedIn post search → Premium Naukri fallback → "
        "Merge + Dedup → Confidence validation → JSON + Excel output.\n\n"
        "Returns a `job_id` immediately. Poll `GET /lead-intelligence-status/{job_id}` "
        "to track progress."
    ),
    responses = {
        202: {"description": "Lead intelligence run accepted and started"},
        422: {"description": "Validation error in request body"},
    },
)
async def run_lead_intelligence(body: LeadIntelligenceRequest) -> dict[str, Any]:
    """Start a hybrid lead-intelligence run as a detached asyncio task and return its `job_id`.

    Run state is kept only in this process's memory (`_lead_jobs`), so it is lost on restart.
    """
    import uuid
    job_id = uuid.uuid4().hex

    _lead_jobs[job_id] = {
        "job_id":    job_id,
        "status":    "running",
        "progress":  0,
        "message":   "Lead intelligence run started",
        "keyword":   body.keyword,
        "max_leads": body.max_leads,
        "total":     0,
        "json_path": "",
        "excel_path": "",
        "error":     "",
    }

    logger.info(
        "lead_intelligence_run_accepted",
        job_id  = job_id,
        keyword = body.keyword,
    )

    asyncio.create_task(
        _run_lead_intelligence_background(job_id, body),
        name=f"lead_intelligence_{job_id}",
    )

    return {
        "job_id":   job_id,
        "status":   "running",
        "message":  "Lead intelligence run started. Poll /lead-intelligence-status/{job_id}.",
        "keyword":  body.keyword,
    }


async def _run_lead_intelligence_background(
    job_id: str,
    request: LeadIntelligenceRequest,
) -> None:
    """Background coroutine — runs the full lead pipeline and updates _lead_jobs."""
    from app.agents.lead_intelligence_orchestrator import LeadIntelligenceOrchestrator
    from app.core.proactor import run_in_proactor

    _lead_jobs[job_id]["progress"] = 10
    _lead_jobs[job_id]["message"]  = "LinkedIn harvest started…"

    try:
        orch = LeadIntelligenceOrchestrator()
        result: LeadIntelligenceResult = await run_in_proactor(
            lambda: orch.run(request)
        )

        if result.naukri_session_required:
            _lead_jobs[job_id].update({
                "status":   "action_required",
                "progress": 50,
                "message":  "Naukri Premium session not authenticated",
                "action":   result.naukri_session_hint,
                "next_step": "Run POST /naukri-setup-session, log in at recruit.naukri.com, close the browser, then retry.",
                "total":    result.total_leads,
                "high":     result.high_confidence,
                "medium":   result.medium_confidence,
                "low":      result.low_confidence,
                "json_path":  result.json_path,
                "excel_path": result.excel_path,
            })
            logger.warning(
                "lead_intelligence_naukri_session_required",
                job_id  = job_id,
                action  = "POST /naukri-setup-session",
            )
        else:
            _lead_jobs[job_id].update({
                "status":               "success",
                "progress":             100,
                "message":              f"Completed — {result.total_leads} leads found",
                "total":                result.total_leads,
                "high":                 result.high_confidence,
                "medium":               result.medium_confidence,
                "low":                  result.low_confidence,
                "recruiters_enriched":  result.recruiters_enriched,
                "emails_added":         result.emails_added,
                "phones_added":         result.phones_added,
                "locations_added":      result.locations_added,
                "companies_updated":    result.companies_updated,
                "confidence_improved":  result.confidence_improved,
                "json_path":            result.json_path,
                "excel_path":           result.excel_path,
            })
            logger.info("lead_intelligence_run_completed", job_id=job_id, total=result.total_leads)

    except Exception as exc:
        logger.exception("lead_intelligence_run_failed", job_id=job_id, error=str(exc))
        _lead_jobs[job_id].update({
            "status":   "failed",
            "progress": 0,
            "message":  "Lead intelligence run failed",
            "error":    str(exc),
        })


# ══════════════════════════════════════════════════════════════════════════════
# GET /lead-intelligence-status/{job_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/lead-intelligence-status/{job_id}",
    summary = "Poll Lead Intelligence Run Status",
)
async def lead_intelligence_status(job_id: str) -> dict[str, Any]:
    """Return the in-memory state of a lead-intelligence run; 404 if the id is unknown.

    `status` is "running", "success", "action_required" (Naukri Premium session needed, see
    `next_step`) or "failed" (see `error`).
    """
    job = _lead_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return job


# ══════════════════════════════════════════════════════════════════════════════
# GET /download/lead-intelligence/json
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/download/lead-intelligence/json",
    summary     = "Download Lead Intelligence JSON",
    description = "Download the most recent lead intelligence run output as JSON.",
    responses   = {
        200: {"description": "JSON file download"},
        404: {"description": "No lead intelligence output file found"},
    },
)
async def download_lead_intelligence_json() -> FileResponse:
    """Download the newest lead-intelligence JSON output file; 404 if none exists
    or the caller's tenant is not all-access (the file is shared, untagged data)."""
    path = _latest_file("*.json")
    if not path:
        raise HTTPException(status_code=404, detail="No lead intelligence JSON file found.")

    return FileResponse(
        path              = str(path),
        media_type        = "application/json",
        filename          = path.name,
        headers           = {"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


# ══════════════════════════════════════════════════════════════════════════════
# GET /download/lead-intelligence/excel
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/download/lead-intelligence/excel",
    summary     = "Download Lead Intelligence Excel",
    description = "Download the most recent lead intelligence run output as Excel (.xlsx).",
    responses   = {
        200: {"description": "Excel file download"},
        404: {"description": "No lead intelligence Excel file found"},
    },
)
async def download_lead_intelligence_excel() -> FileResponse:
    """Download the newest lead-intelligence Excel (.xlsx) output file; 404 if none
    exists or the caller's tenant is not all-access (the file is shared, untagged data)."""
    path = _latest_file("*.xlsx")
    if not path:
        raise HTTPException(status_code=404, detail="No lead intelligence Excel file found.")

    return FileResponse(
        path       = str(path),
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename   = path.name,
        headers    = {"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _latest_file(glob_pattern: str) -> Path | None:
    """Return the most recently modified file matching glob_pattern in the lead output dir.

    None for a non-all-access tenant: these files are shared across tenants."""
    if not is_all_access(get_current_tenant_id()):
        return None
    files = sorted(
        _LEAD_OUTPUT_DIR.glob(glob_pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ) if _LEAD_OUTPUT_DIR.exists() else []
    return files[0] if files else None
