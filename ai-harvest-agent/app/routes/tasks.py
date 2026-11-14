"""Celery background task management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.security import require_api_key
from app.models.response import APIResponse
from app.models.task import Task, TaskStatus
from app.services.queue_service import get_task_status, revoke_task

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.get(
    "/{task_id}",
    response_model=APIResponse[Task],
    summary="Get Celery task status",
)
async def get_task(task_id: str) -> APIResponse[Task]:
    """Return the Celery status of `task_id`, with its result when ready or error when failed.

    A status outside TaskStatus (e.g. "UNKNOWN" when Celery isn't installed) is reported
    as PENDING — Celery's own answer for an id it knows nothing about — with the reason
    in `error`, instead of failing the request with a 500.
    """
    info = get_task_status(task_id)
    try:
        task_status = TaskStatus(info["status"])
    except ValueError:
        task_status = TaskStatus.PENDING
    task = Task(
        task_id=task_id,
        task_name="harvest.run_job",
        status=task_status,
        result=info.get("result"),
        error=info.get("error"),
    )
    return APIResponse(data=task)


@router.post(
    "/{task_id}/cancel",
    response_model=APIResponse[dict],
    summary="Cancel a running task",
)
async def cancel_task(task_id: str) -> APIResponse[dict]:
    """Revoke `task_id`, terminating it if running. A no-op when Celery is unavailable."""
    revoke_task(task_id, terminate=True)
    return APIResponse(data={"task_id": task_id, "cancelled": True})
