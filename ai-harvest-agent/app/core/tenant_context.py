"""Per-request tenant context + scoping helpers for multi-tenancy.

A ContextVar carries the current request's tenant so that:
  * the shared db_read/db_write sessions (which open their OWN sessions, outside
    FastAPI's dependency graph) can bind Postgres `app.tenant_id` for RLS, and
  * the query methods can apply an app-level `WHERE tenant_id = ...` filter
without threading the tenant through every call site.

`internal` (and the `__all__` sentinel / unset) mean all-access — the internal
team keeps seeing every tenant's rows, preserving pre-multi-tenant behaviour.
Client tenants (`client_us`, `client_in`) are scoped to their own rows.
"""
from __future__ import annotations

from contextvars import ContextVar

from app.models.tenant import ALL_ACCESS, INTERNAL_TENANT_ID

_current_tenant: ContextVar[str] = ContextVar("current_tenant", default=INTERNAL_TENANT_ID)


def set_current_tenant(tenant_id: str | None) -> None:
    _current_tenant.set(tenant_id or INTERNAL_TENANT_ID)


def get_current_tenant_id() -> str:
    return _current_tenant.get()


def is_all_access(tenant_id: str | None) -> bool:
    """Internal / unset / sentinel → sees every tenant's rows."""
    return not tenant_id or tenant_id in (INTERNAL_TENANT_ID, ALL_ACCESS, "")


def db_tenant_setting() -> str:
    """Value to bind to Postgres `app.tenant_id` for the current request: the
    all-access sentinel for internal/admin, else the client tenant id."""
    tid = get_current_tenant_id()
    return ALL_ACCESS if is_all_access(tid) else tid


def apply_tenant(stmt, tenant_column):
    """App-level scoping (belt-and-suspenders alongside RLS): filter by the
    current tenant unless it's all-access. `tenant_column` is e.g.
    `ScrapedJobORM.tenant_id`. Returns the (possibly) filtered statement."""
    tid = get_current_tenant_id()
    if is_all_access(tid):
        return stmt
    return stmt.where(tenant_column == tid)


def _is_postgres() -> bool:
    from app.config import get_settings

    return "postgresql" in (get_settings().database_url or "")


async def bind_session_tenant(db) -> None:
    """Best-effort: bind Postgres `app.tenant_id` on this session's transaction so
    RLS scopes every subsequent query. `set_config(..., is_local => true)` is the
    parameterised form (SET LOCAL takes no bind params). No-op on non-Postgres."""
    if not _is_postgres():
        return
    from sqlalchemy import text as _text

    try:
        await db.execute(
            _text("SELECT set_config('app.tenant_id', :t, true)"),
            {"t": db_tenant_setting()},
        )
    except Exception:  # pragma: no cover — RLS binding is best-effort
        pass
