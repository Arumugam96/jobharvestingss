"""Tenant (workspace) model — the anchor of multi-tenancy.

A tenant is a first-class entity (NOT a lookup/code table): every content row
carries a `tenant_id` pointing here, and each tenant owns its branding + feature
flags in `config` (the "slip" the frontend reads via /auth/me). RLS and the
app-level scoping helper both key off `tenant_id`.

Content is isolated per tenant; `companies` (a public enrichment cache) stays
global. Auth/session tables derive their tenant through the owning user.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import JSON, Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.harvest import Base  # shared metadata — one create_all() for all tables

# The default tenant every existing row/user is backfilled to. Also the internal
# team's tenant. Kept as a short, immutable code (used as the FK value and read
# by RLS via current_setting('app.tenant_id')).
INTERNAL_TENANT_ID = "internal"

# Sentinel written to `app.tenant_id` for internal/all-access requests — the RLS
# policies treat it as "see every tenant" (see app/main.py::_ensure_rls_policies).
ALL_ACCESS = "__all__"


class TenantType:
    INTERNAL = "internal"
    CLIENT = "client"


class TenantORM(Base):
    __tablename__ = "tenants"

    # Short, human-readable, IMMUTABLE code — the FK value everywhere and the
    # string RLS reads. Never rename; `name` carries the display label.
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    type: Mapped[str] = mapped_column(String(20), nullable=False, default=TenantType.CLIENT)
    region: Mapped[str] = mapped_column(String(10), nullable=False, default="")
    # Branding + feature flags + limits — the source the frontend renders from.
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ── Seed tenants ─────────────────────────────────────────────────────────────
# Created + upserted at startup (app/main.py::_ensure_tenants_seeded). `config`
# is the per-tenant slip: theme (accent/brand) + feature flags the UI gates on.
# Only flags set True are shown; the backend still enforces access independently.
SEED_TENANTS = [
    {
        "id": INTERNAL_TENANT_ID,
        "name": "SightSpectrum",
        "type": TenantType.INTERNAL,
        "region": "",
        "config": {
            "theme": {"accent": "#2563EB", "brand": "SightSpectrum"},
            "features": {
                "jobs": True, "history": True, "sources": True, "leads": True,
                "outreach": True, "rules": True, "analytics": True,
                "ruleEngineRedesign": False,
            },
        },
    },
    {
        "id": "client_us",
        "name": "Northwind Talent",
        "type": TenantType.CLIENT,
        "region": "US",
        "config": {
            "theme": {"accent": "#2563EB", "brand": "Northwind Talent"},
            # US client: Harvested Jobs + Run History + the REDESIGNED Rule Engine.
            "features": {
                "jobs": True, "history": True, "rules": True,
                "ruleEngineRedesign": True,
            },
        },
    },
    {
        "id": "client_in",
        "name": "Meridian Staffing",
        "type": TenantType.CLIENT,
        "region": "IN",
        "config": {
            "theme": {"accent": "#0D9488", "brand": "Meridian Staffing"},
            # India client: Harvested Jobs + Run History only.
            "features": {"jobs": True, "history": True},
        },
    },
]


# Email second-level label → tenant id. Governs BOTH who may log in (the OTP
# validator accepts any label here) and which tenant a new user is assigned to on
# first login. Add a client's domain here to onboard them (no admin UI this pass).
EMAIL_DOMAIN_TENANTS = {
    "sightspectrum": INTERNAL_TENANT_ID,
    "northwindtalent": "client_us",
    "meridianstaffing": "client_in",
}


# Login-page workspace switch → tenant id. FOR NOW the workspace a user picks at
# login decides their tenant (any allowed user may enter any workspace) — the
# email-domain map above is only the fallback when no workspace is sent. To
# re-tighten later: ignore the workspace in AuthService._get_or_create_user and
# let tenant_for_email() win again.
WORKSPACE_TENANTS = {
    "internal": INTERNAL_TENANT_ID,
    "us": "client_us",
    "in": "client_in",
}


def tenant_for_email(email: str) -> str | None:
    """Resolve an email's tenant from its second-level domain label (e.g.
    'name@northwindtalent.com' -> 'client_us'), or None if not an allowed
    workspace domain."""
    parts = email.strip().lower().rsplit("@", 1)
    if len(parts) != 2 or not parts[1]:
        return None
    label = parts[1].split(".", 1)[0]
    return EMAIL_DOMAIN_TENANTS.get(label)


class Tenant(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    type: str
    region: str
    config: dict
    is_active: bool
