"""Baseline schema — creates every table registered on Base.metadata.

Uses create_all(checkfirst=True) rather than explicit op.create_table calls so
this revision is safe in BOTH bootstrap orders:
  * fresh database, `alembic upgrade head` run first  -> creates all tables
  * database already initialised by the app's startup create_all
    (app/main.py lifespan)                            -> skips existing tables
    and just stamps the version

Tables (all UUID-string PKs): users, otp_verifications, harvest_jobs,
harvest_results, harvest_runs, scraped_jobs, llm_calls, recruiters,
recruiter_discovery_runs.

Revision ID: 0001
Revises:
Create Date: 2026-08-21

"""
from __future__ import annotations

from alembic import op

# Import every module that registers tables on the shared Base.metadata —
# same set app/main.py imports before its own create_all.
import app.models.auth  # noqa: F401
import app.models.harvest_run  # noqa: F401
import app.models.recruiter  # noqa: F401
import app.models.outreach  # noqa: F401
from app.models.harvest import Base

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, checkfirst=True)
