"""Custom exceptions and FastAPI exception handlers."""
from __future__ import annotations
from typing import Any
from fastapi import Request, status
from fastapi.responses import JSONResponse


class HarvestException(Exception):
    """Base exception for all Harvest Agent errors."""

    def __init__(
        self,
        message: str,
        code: str = "HARVEST_ERROR",
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}


class JobNotFoundError(HarvestException):
    def __init__(self, job_id: str) -> None:
        super().__init__(
            message=f"Harvest job '{job_id}' not found",
            code="JOB_NOT_FOUND",
            status_code=status.HTTP_404_NOT_FOUND,
        )


class JobAlreadyRunningError(HarvestException):
    def __init__(self, job_id: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=(
                "A harvest is already running — only one can run at a time "
                "(Playwright cannot drive the same Chrome profile twice). "
                "Wait for the current run to finish."
            ),
            code="JOB_ALREADY_RUNNING",
            status_code=status.HTTP_409_CONFLICT,
            details=details or {"job_id": job_id},
        )


class DailyJobLimitExceededError(HarvestException):
    def __init__(self, used: int, limit: int) -> None:
        super().__init__(
            message=(
                f"Daily harvest limit reached — {used} of {limit} jobs already "
                f"scraped today. Try again after the daily reset (UTC midnight) "
                f"or raise MAX_JOBS_PER_DAY."
            ),
            code="DAILY_LIMIT_EXCEEDED",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            details={"used": used, "limit": limit},
        )


class AgentError(HarvestException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            code="AGENT_ERROR",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=details,
        )


class PlaywrightError(HarvestException):
    def __init__(self, message: str, url: str = "") -> None:
        super().__init__(
            message=message,
            code="PLAYWRIGHT_ERROR",
            status_code=status.HTTP_502_BAD_GATEWAY,
            details={"url": url},
        )


class LLMError(HarvestException):
    def __init__(self, message: str) -> None:
        super().__init__(
            message=message,
            code="LLM_ERROR",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class LLMUnavailableError(LLMError):
    """The extraction LLM provider itself is unreachable/down (connection
    refused, timeout, HTTP 5xx, missing credentials, provider-side error) —
    as opposed to plain LLMError, which signals a single call went wrong
    (e.g. the model returned invalid JSON) and is safe to skip past.

    This distinction is load-bearing: a bare LLMError still just skips the one
    job, but an LLMUnavailableError aborts the entire harvest run (all sources)
    because retrying every remaining job against a dead provider is pointless.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "LLM_UNAVAILABLE"


class ValidationError(HarvestException):
    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(
            message=message,
            code="VALIDATION_ERROR",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            details={"field": field},
        )


async def harvest_exception_handler(request: Request, exc: HarvestException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": str(exc), "details": {}}},
    )
