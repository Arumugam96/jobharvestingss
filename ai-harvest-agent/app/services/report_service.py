"""On-demand harvest report generation from the database.

Replaces the harvest-time result files (data/results/combined/*.json,
data/results/excel/*.xlsx, per-source data/results/<source>/*.json): the
JSON/Excel report is now built in memory, whenever it's needed (GET
/download/json, GET /download/excel, the post-harvest report email), from
ScrapedJobORM rows with each job's RecruiterORM contact info merged in —
scraped_job_view() fills email_id/contact_number from the linked recruiter's
official_email_id/contact_number when the job row itself scraped none.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

from app.models.harvest_run import ScrapedJobORM
from app.services.excel_export_service import ExcelExportService
from app.services.harvest_run_service import scraped_job_view

logger = structlog.get_logger(__name__)


def merged_job_dicts(jobs: list[ScrapedJobORM]) -> list[dict[str, Any]]:
    """ScrapedJobORM rows -> job dicts with recruiter contact info merged in
    (same view GET /jobs serves, so UI / downloads / email always agree).

    Drops job_description_html: the reports keep only the plain-text
    job_description so the JSON/Excel downloads stay tag-free — the HTML field
    exists purely for the UI's rich rendering."""
    out: list[dict[str, Any]] = []
    for j in jobs:
        view = scraped_job_view(j)
        view.pop("job_description_html", None)
        out.append(view)
    return out


def build_json_report_bytes(
    job_dicts: list[dict[str, Any]],
    *,
    run_id: str = "",
    executed_at: str = "",
    filters: dict | None = None,
) -> bytes:
    """The combined-JSON report payload (same shape the old
    data/results/combined/*_combined.json files had), as UTF-8 bytes."""
    payload = {
        "run_id":       run_id,
        "executed_at":  executed_at or datetime.now(timezone.utc).isoformat(),
        "total_found":  len(job_dicts),
        "sources":      sorted({(j.get("source") or "") for j in job_dicts if j.get("source")}),
        "filters":      filters or {},
        "jobs":         job_dicts,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")


def build_excel_report_bytes(job_dicts: list[dict[str, Any]]) -> bytes:
    """The multi-sheet Excel workbook (same layout ExcelExportService always
    produced), as .xlsx bytes. Returns b"" if openpyxl is unavailable."""
    return ExcelExportService().build_bytes(job_dicts)


def report_basename(run_id: str = "") -> str:
    """Consistent attachment/download filename stem for one report."""
    if run_id:
        return f"{run_id}_harvest"
    return f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_harvest"
