"""FastAPI application factory and lifespan."""
from __future__ import annotations
import asyncio
import io
import sys
import structlog

# Force UTF-8 on Windows: prevents charmap encode errors when job titles/descriptions
# contain characters outside cp1252 (e.g. ‑ non-breaking hyphen).
if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect as sa_inspect, text as sa_text
from app.config import get_settings
from app.core.logging_config import configure_logging

configure_logging(get_settings().log_level)

from app.core.dependencies import get_current_user, get_engine
from app.core.exceptions import HarvestException, harvest_exception_handler
from app.core.middleware import LoggingMiddleware, RateLimitMiddleware
import app.models.auth  # noqa: F401 — registers users / otp_verifications on Base.metadata
import app.models.harvest_run  # noqa: F401 — registers harvest_runs / scraped_jobs / llm_calls on Base.metadata
import app.models.recruiter  # noqa: F401 — registers recruiters on Base.metadata
import app.models.outreach  # noqa: F401 — registers email_outreach on Base.metadata
from app.models.harvest import Base
from app.routes import harvest, agents, tasks, health, job_parser, linkedin_harvest
from app.routes.auth_routes import router as auth_router
from app.routes.harvest_routes import router as harvest_agent_router
from app.routes.linkedin_routes import router as linkedin_agent_router
from app.routes.naukri_routes import router as naukri_agent_router
from app.routes.dice_routes import router as dice_agent_router
from app.routes.run_harvest_agent import router as run_harvest_agent_router
from app.routes.frontend_routes import router as frontend_router
from app.routes.prospect_routes import router as prospect_intelligence_router
from app.routes.recruiter_routes import router as recruiter_discovery_router
from app.routes.lead_intelligence_routes import router as lead_intelligence_router
from app.routes.outreach_routes import router as outreach_router
from app.services.job_tracker import JobTracker
from app.services.playwright_service import PlaywrightService
from app.services.scheduler_service import SchedulerService

logger   = structlog.get_logger(__name__)
settings = get_settings()


def _ensure_scraped_jobs_columns(sync_conn) -> None:
    """One-time, idempotent ADD COLUMN backfill for columns added to the
    pre-existing scraped_jobs table (create_all only creates missing tables,
    never alters an existing one)."""
    inspector = sa_inspect(sync_conn)
    if "scraped_jobs" not in inspector.get_table_names():
        return  # brand-new DB — create_all above already made this table with the columns
    existing_cols = {c["name"] for c in inspector.get_columns("scraped_jobs")}
    # (column name, ADD COLUMN DDL) — constant DEFAULTs, portable SQL only.
    # TRUE works as a boolean default on both PostgreSQL and SQLite (>=3.23).
    pending = [
        ("recruiter_id",  "ALTER TABLE scraped_jobs ADD COLUMN recruiter_id VARCHAR(36)"),
        ("passed_filter", "ALTER TABLE scraped_jobs ADD COLUMN passed_filter BOOLEAN NOT NULL DEFAULT TRUE"),
        ("filter_reason", "ALTER TABLE scraped_jobs ADD COLUMN filter_reason VARCHAR(255) NOT NULL DEFAULT ''"),
        ("job_description_html", "ALTER TABLE scraped_jobs ADD COLUMN job_description_html TEXT NOT NULL DEFAULT ''"),
    ]
    for name, ddl in pending:
        if name not in existing_cols:
            sync_conn.execute(sa_text(ddl))
            logger.info("scraped_jobs_column_added", column=name)


def _ensure_recruiter_columns(sync_conn) -> None:
    """One-time, idempotent ADD COLUMN backfill for columns added to the
    pre-existing recruiters table (Apollo enrichment provenance). Mirrors
    _ensure_scraped_jobs_columns — create_all never alters an existing table."""
    inspector = sa_inspect(sync_conn)
    if "recruiters" not in inspector.get_table_names():
        return  # brand-new DB — create_all already made this table with the columns
    existing_cols = {c["name"] for c in inspector.get_columns("recruiters")}
    # Postgres wants TIMESTAMPTZ to match DateTime(timezone=True); SQLite ignores
    # the type affinity, so a plain TIMESTAMP is fine there.
    ts_type = "TIMESTAMPTZ" if sync_conn.dialect.name == "postgresql" else "TIMESTAMP"
    pending = [
        ("enrichment_source",  "ALTER TABLE recruiters ADD COLUMN enrichment_source VARCHAR(50) NOT NULL DEFAULT ''"),
        ("apollo_enriched_at", f"ALTER TABLE recruiters ADD COLUMN apollo_enriched_at {ts_type}"),
        # Extra Apollo people-match details mapped onto the recruiter row.
        ("secondary_email",      "ALTER TABLE recruiters ADD COLUMN secondary_email VARCHAR(255) NOT NULL DEFAULT ''"),
        ("address",              "ALTER TABLE recruiters ADD COLUMN address TEXT NOT NULL DEFAULT ''"),
        ("city",                 "ALTER TABLE recruiters ADD COLUMN city VARCHAR(120) NOT NULL DEFAULT ''"),
        ("state",                "ALTER TABLE recruiters ADD COLUMN state VARCHAR(120) NOT NULL DEFAULT ''"),
        ("country",              "ALTER TABLE recruiters ADD COLUMN country VARCHAR(120) NOT NULL DEFAULT ''"),
        ("company_linkedin_url", "ALTER TABLE recruiters ADD COLUMN company_linkedin_url TEXT NOT NULL DEFAULT ''"),
    ]
    for name, ddl in pending:
        if name not in existing_cols:
            sync_conn.execute(sa_text(ddl))
            logger.info("recruiters_column_added", column=name)


def _ensure_llm_calls_columns(sync_conn) -> None:
    """One-time, idempotent migration for the pre-existing llm_calls table so it
    can also hold ad-hoc outreach LLM calls: add the `call_type` purpose tag and
    drop NOT NULL on `run_id` (outreach generations have no harvest run). Mirrors
    the other _ensure_* helpers — create_all never alters an existing table."""
    inspector = sa_inspect(sync_conn)
    if "llm_calls" not in inspector.get_table_names():
        return  # brand-new DB — create_all already made this table from the model
    columns = {c["name"]: c for c in inspector.get_columns("llm_calls")}
    if "call_type" not in columns:
        sync_conn.execute(sa_text(
            "ALTER TABLE llm_calls ADD COLUMN call_type VARCHAR(30) NOT NULL DEFAULT 'harvest'"
        ))
        logger.info("llm_calls_column_added", column="call_type")
    # Drop NOT NULL on run_id so outreach calls can be stored with run_id=NULL.
    # Postgres-only: SQLite can't ALTER a column's nullability, but a fresh
    # SQLite DB already gets nullable=True straight from the model via create_all.
    if sync_conn.dialect.name == "postgresql":
        run_id_col = columns.get("run_id")
        if run_id_col is not None and not run_id_col.get("nullable", True):
            sync_conn.execute(sa_text("ALTER TABLE llm_calls ALTER COLUMN run_id DROP NOT NULL"))
            logger.info("llm_calls_run_id_nullable")


def _ensure_harvest_runs_columns(sync_conn) -> None:
    """One-time, idempotent ADD COLUMN backfill for columns added to the
    pre-existing harvest_runs table. Mirrors the other _ensure_* helpers —
    create_all never alters an existing table."""
    inspector = sa_inspect(sync_conn)
    if "harvest_runs" not in inspector.get_table_names():
        return  # brand-new DB — create_all already made this table from the model
    existing_cols = {c["name"] for c in inspector.get_columns("harvest_runs")}
    # report_pending: a user-stopped run persists its jobs but defers its report
    # email; the next successful run merges those jobs in and clears the flag.
    # FALSE is a portable boolean default on PostgreSQL and SQLite (>=3.23).
    if "report_pending" not in existing_cols:
        sync_conn.execute(sa_text(
            "ALTER TABLE harvest_runs ADD COLUMN report_pending BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        logger.info("harvest_runs_column_added", column="report_pending")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: launch browser pool + scheduler. Shutdown: clean up both."""
    logger.info("startup", env=settings.app_env, model=settings.extraction_llm_model)

    # Restore any in-flight job states from the previous process
    JobTracker.load_from_disk()

    # ── Database tables ────────────────────────────────────────────────────────
    # No migration tool — SQLAlchemy's create_all is idempotent (skips tables
    # that already exist), so this is safe to run on every startup.
    engine = get_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all only creates missing tables — it never alters an
        # existing one, so columns added to the pre-existing scraped_jobs table
        # (recruiter_id, passed_filter, filter_reason) each need a one-time,
        # idempotent ADD COLUMN here.
        await conn.run_sync(_ensure_scraped_jobs_columns)
        # recruiters gained Apollo provenance columns (enrichment_source,
        # apollo_enriched_at) — same idempotent ADD COLUMN treatment.
        await conn.run_sync(_ensure_recruiter_columns)
        # llm_calls gained a call_type tag + nullable run_id so it can also hold
        # outreach (email/linkedin) generation calls, not just harvest calls.
        await conn.run_sync(_ensure_llm_calls_columns)
        # harvest_runs gained report_pending (deferred report for user-stopped
        # runs) — same idempotent ADD COLUMN treatment.
        await conn.run_sync(_ensure_harvest_runs_columns)

    # Reconcile stale 'running' runs left by a previous process (a harvest runs
    # in a detached task that doesn't survive a restart). Without this, the
    # single-flight guard's DB backstop would treat a dead run as active and
    # reject every new start with 409. Best-effort — never block startup.
    try:
        from app.services.harvest_run_service import HarvestRunService, db_write
        stale = await db_write(lambda db: HarvestRunService(db).fail_stale_running())
        if stale:
            logger.info("stale_running_runs_reconciled", count=stale)
    except Exception as exc:
        logger.warning("stale_run_reconcile_failed", error=str(exc))

    # ── Playwright pool (optional — demo routes create their own browser) ─────
    # On Windows with --reload, uvicorn forces SelectorEventLoop which cannot
    # spawn Playwright's browser subprocess.  The pool is skipped gracefully;
    # scraper routes use run_in_proactor() to launch browsers in a worker thread.
    #
    # We temporarily install a custom asyncio exception handler to suppress the
    # "Task exception was never retrieved" noise that Playwright's internal
    # Connection.run() background Task emits when the subprocess fails on
    # SelectorEventLoop.  The handler is restored to default afterward.
    loop = asyncio.get_event_loop()
    if sys.platform == "win32":
        def _suppress_not_impl(loop, context):
            if isinstance(context.get("exception"), NotImplementedError):
                return
            loop.default_exception_handler(context)
        loop.set_exception_handler(_suppress_not_impl)

    playwright_service = PlaywrightService(settings)
    try:
        await playwright_service.start()
        app.state.playwright = playwright_service
        logger.info("playwright_ready", pool_size=settings.playwright_pool_size)
    except NotImplementedError:
        logger.info(
            "playwright_pool_skipped",
            reason="SelectorEventLoop (uvicorn --reload on Windows) — scraper routes use ProactorEventLoop thread",
        )
        app.state.playwright = None
    except Exception as exc:
        logger.warning("playwright_pool_unavailable", error=str(exc))
        app.state.playwright = None
    finally:
        if sys.platform == "win32":
            loop.set_exception_handler(None)   # restore default handler

    # ── APScheduler ───────────────────────────────────────────────────────────
    scheduler = SchedulerService()
    scheduler.start()
    app.state.scheduler = scheduler

    # Apply schedule from config (if enabled)
    try:
        from app.services.config_service import ConfigService
        from app.routes.harvest_routes import _apply_schedule
        cfg = ConfigService().load()
        if cfg.schedule.enabled:
            await _apply_schedule(scheduler, cfg)
            logger.info("scheduler_schedule_applied", frequency=cfg.schedule.frequency)
    except Exception as exc:
        logger.warning("scheduler_setup_failed", error=str(exc))

    yield  # ← app runs here

    # ── Shutdown ──────────────────────────────────────────────────────────────
    try:
        await playwright_service.stop()
    except Exception:
        pass
    scheduler.stop()
    logger.info("shutdown_complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title        = "AI Harvest Agent — Enterprise Job Intelligence Platform",
        version      = "1.0.0",
        description  = "",
        openapi_tags = [],
        docs_url     = "/docs",
        redoc_url    = None,
        openapi_url  = "/openapi.json",
        swagger_ui_parameters = {
            "defaultModelsExpandDepth": -1,
            "defaultModelExpandDepth":  -1,
            "defaultModelRendering":    "example",
            "docExpansion":             "full",
            "tryItOutEnabled":          True,
            "displayRequestDuration":   True,
            "filter":                   False,
            "showExtensions":           False,
            "showCommonExtensions":     False,
            "syntaxHighlight.theme":    "monokai",
        },
        lifespan = lifespan,
    )

    # ── Middleware ────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins    = settings.cors_origins_list,
        allow_credentials = True,
        allow_methods    = ["*"],
        allow_headers    = ["*"],
    )
    app.add_middleware(LoggingMiddleware)
    if settings.is_production:
        app.add_middleware(RateLimitMiddleware, requests_per_minute=60)

    # ── Exception Handlers ────────────────────────────────────────────────────
    app.add_exception_handler(HarvestException, harvest_exception_handler)  # type: ignore[arg-type]

    # ── Internal routers — hidden from Swagger ────────────────────────────────
    prefix = settings.api_v1_prefix
    app.include_router(health.router,            tags=["Health"],           include_in_schema=False)
    app.include_router(harvest.router,           prefix=f"{prefix}/harvest",        tags=["Harvest"],          include_in_schema=False)
    app.include_router(agents.router,            prefix=f"{prefix}/agents",         tags=["Agents"],           include_in_schema=False)
    app.include_router(tasks.router,             prefix=f"{prefix}/tasks",          tags=["Tasks"],            include_in_schema=False)
    app.include_router(job_parser.router,        prefix=f"{prefix}/jobs",           tags=["Job Parser"],       include_in_schema=False)
    app.include_router(linkedin_harvest.router,  prefix=f"{prefix}/jobs/linkedin",  tags=["LinkedIn Harvest"], include_in_schema=False)

    # ── Public endpoints (Swagger-visible) ────────────────────────────────────
    # auth_router stays open (login must be reachable pre-token). Every other
    # public router requires a logged-in user; when settings.auth_enabled is
    # False, get_current_user short-circuits to a dev user so these still work.
    protected = [Depends(get_current_user)]
    app.include_router(auth_router)                   # POST /auth/request-otp, /auth/verify-otp, GET /auth/me
    app.include_router(frontend_router, dependencies=protected)               # GET /jobs, /lead-intelligence, /download/*, /health
    app.include_router(run_harvest_agent_router, dependencies=protected)      # POST /run-harvest-agent, GET /harvest-status/{id}, /run-history
    app.include_router(linkedin_agent_router, dependencies=protected)         # POST /run-linkedin-agent  +  results endpoints
    app.include_router(harvest_agent_router, dependencies=protected)          # POST /run-harvest  +  management endpoints
    app.include_router(naukri_agent_router, dependencies=protected)           # POST /run-naukri-agent  +  results endpoints
    app.include_router(dice_agent_router, dependencies=protected)             # POST /run-dice-agent  +  dice results endpoints
    app.include_router(prospect_intelligence_router, dependencies=protected)  # POST /run-prospect-intelligence
    app.include_router(recruiter_discovery_router, dependencies=protected)    # POST /run-recruiter-discovery
    app.include_router(lead_intelligence_router, dependencies=protected)      # POST /run-lead-intelligence, GET /lead-intelligence, /download/lead-intelligence/*
    app.include_router(outreach_router, dependencies=protected)               # POST /outreach/generate-email, /generate-linkedin, /send-email

    return app


app = create_app()
