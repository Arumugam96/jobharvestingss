"""One-shot, re-runnable SQLite -> PostgreSQL data migration.

Reads the whole SQLite database and copies every table known to the app's
SQLAlchemy metadata into PostgreSQL, preserving IDs and relationships.
Designed to run unchanged locally and on EC2 — all configuration comes from
environment variables:

    SQLITE_DATABASE_PATH   path to the source SQLite file (opened READ-ONLY;
                           this script can never modify or delete it)
    DATABASE_URL           destination PostgreSQL URL, e.g.
                           postgresql+asyncpg://user:pass@localhost:5432/harvest_db

Usage:
    python scripts/migrate_sqlite_to_postgres.py            # migrate
    python scripts/migrate_sqlite_to_postgres.py --dry-run  # read + convert only, no writes
    python scripts/migrate_sqlite_to_postgres.py --wipe-target
        # TRUNCATE the destination tables first (needed to re-run after a
        # previous full or partial migration)

Behaviour:
    * Destination tables must already exist (run `alembic upgrade head` first).
    * Aborts before writing anything if any destination table already has rows,
      unless --wipe-target is given.
    * Tables are copied in FK-dependency order (parents before children).
    * SQLite values are converted per destination column type: 0/1 -> boolean,
      ISO strings -> (timezone-aware) datetimes, JSON text -> jsonb-compatible
      objects.
    * After copying, any integer PK sequences are re-synced with setval()
      (all current PKs are UUID strings, so this is a no-op today — kept so a
      future integer-PK table migrates correctly).
    * Ends with a source-vs-destination row-count report; exits 1 on mismatch.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the app package importable when run as `python scripts/...` from the
# ai-harvest-agent directory (or anywhere else).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import Boolean, DateTime, Integer, insert, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

# Import every module that registers tables on the shared Base.metadata —
# same set app/main.py imports before create_all.
import app.models.auth  # noqa: F401,E402
import app.models.harvest_run  # noqa: F401,E402
import app.models.recruiter  # noqa: F401,E402
from app.models.harvest import Base  # noqa: E402

BATCH_SIZE = 500


def log(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# ── Configuration ─────────────────────────────────────────────────────────────


def resolve_config() -> tuple[Path, str]:
    # .env (next to the app) fills in anything not already exported; real
    # environment variables always win — load_dotenv never overrides them.
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    sqlite_path = os.environ.get("SQLITE_DATABASE_PATH")
    database_url = os.environ.get("DATABASE_URL")
    if not sqlite_path:
        fail("SQLITE_DATABASE_PATH is not set (path to the source SQLite file)")
    if not database_url:
        fail("DATABASE_URL is not set (destination PostgreSQL URL)")
    if database_url.startswith("sqlite"):
        fail(f"DATABASE_URL points at SQLite ({database_url}) — expected PostgreSQL")

    # Accept plain postgres:// / postgresql:// URLs and route them to asyncpg.
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+asyncpg://" + database_url[len("postgresql://"):]

    path = Path(sqlite_path)
    if not path.is_absolute():
        path = (Path(__file__).resolve().parents[1] / path).resolve()
    if not path.exists():
        fail(f"SQLite database not found: {path}")
    return path, database_url


# ── Source (SQLite, read-only) ────────────────────────────────────────────────


def open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    # mode=ro guarantees the source can never be modified by this script.
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def sqlite_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ── Value conversion ──────────────────────────────────────────────────────────


def convert_value(value, column):
    """Convert a raw SQLite value to what the destination column expects."""
    if value is None:
        return None
    coltype = column.type
    if isinstance(coltype, Boolean):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "t", "yes")
        return bool(value)
    if isinstance(coltype, DateTime):
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            try:
                dt = datetime.fromisoformat(str(value))
            except ValueError:
                fail(f"unparseable datetime {value!r} in column {column.name}")
        if getattr(coltype, "timezone", False) and dt.tzinfo is None:
            # SQLite's CURRENT_TIMESTAMP is UTC; make tz-aware columns explicit.
            dt = dt.replace(tzinfo=timezone.utc)
        if not getattr(coltype, "timezone", False) and dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    if isinstance(coltype, Integer) and isinstance(value, str):
        return int(value)
    # JSON columns arrive from SQLite as text.
    if coltype.__class__.__name__ in ("JSON", "JSONB") and isinstance(value, (str, bytes)):
        return json.loads(value)
    return value


# ── Migration ─────────────────────────────────────────────────────────────────


async def migrate(sqlite_path: Path, database_url: str, wipe_target: bool, dry_run: bool) -> None:
    src = open_sqlite_readonly(sqlite_path)
    src_tables = sqlite_tables(src)

    # FK-dependency order: parents before children.
    tables = [t for t in Base.metadata.sorted_tables if t.name in src_tables]
    skipped = sorted(src_tables - {t.name for t in tables})
    if skipped:
        log(f"NOTE: source tables not in the app schema, skipped: {', '.join(skipped)}")

    engine = create_async_engine(database_url, echo=False)
    src_counts: dict[str, int] = {}
    dest_counts: dict[str, int] = {}

    try:
        async with engine.connect() as conn:
            # Destination must have the schema already (alembic upgrade head).
            missing = []
            for table in tables:
                exists = await conn.scalar(text("SELECT to_regclass(:t)"), {"t": table.name})
                if exists is None:
                    missing.append(table.name)
            if missing:
                fail(
                    f"destination tables missing: {', '.join(missing)} — "
                    "run `alembic upgrade head` first"
                )

            # Refuse to mix into non-empty destination tables.
            non_empty = []
            for table in tables:
                n = await conn.scalar(text(f'SELECT COUNT(*) FROM "{table.name}"'))
                if n:
                    non_empty.append(f"{table.name}({n})")
            if non_empty and not wipe_target:
                fail(
                    "destination not empty: " + ", ".join(non_empty)
                    + " — re-run with --wipe-target to truncate destination tables first"
                )

        if dry_run:
            log("--dry-run: converting source rows without writing...")

        async with engine.begin() as conn:
            if wipe_target and not dry_run:
                names = ", ".join(f'"{t.name}"' for t in reversed(tables))
                log(f"Truncating destination tables: {names}")
                await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))

            for table in tables:
                src_cols = sqlite_columns(src, table.name)
                # Copy only columns both sides know; anything new on the PG
                # side gets its column default.
                cols = [c for c in table.columns if c.name in src_cols]
                dropped = src_cols - {c.name for c in cols}
                if dropped:
                    log(f"NOTE: {table.name}: source columns not in app schema, ignored: {sorted(dropped)}")

                rows = src.execute(
                    f'SELECT {", ".join(c.name for c in cols)} FROM {table.name}'
                ).fetchall()
                src_counts[table.name] = len(rows)

                converted = [
                    {c.name: convert_value(row[c.name], c) for c in cols} for row in rows
                ]
                if dry_run:
                    log(f"{table.name}: {len(converted)} rows converted OK (not written)")
                    continue

                for i in range(0, len(converted), BATCH_SIZE):
                    batch = converted[i : i + BATCH_SIZE]
                    if batch:
                        await conn.execute(insert(table), batch)
                log(f"{table.name}: {len(converted)} rows copied")

            if not dry_run:
                # Re-sync integer-PK sequences after explicit ID inserts.
                for table in tables:
                    for col in table.primary_key.columns:
                        if isinstance(col.type, Integer):
                            seq = await conn.scalar(
                                text("SELECT pg_get_serial_sequence(:t, :c)"),
                                {"t": table.name, "c": col.name},
                            )
                            if seq:
                                await conn.execute(
                                    text(
                                        f"SELECT setval(:seq, COALESCE((SELECT MAX(\"{col.name}\") "
                                        f'FROM "{table.name}"), 0) + 1, false)'
                                    ),
                                    {"seq": seq},
                                )
                                log(f"{table.name}.{col.name}: sequence {seq} re-synced")

        # ── Verification: source vs destination row counts ────────────────────
        if not dry_run:
            async with engine.connect() as conn:
                for table in tables:
                    dest_counts[table.name] = await conn.scalar(
                        text(f'SELECT COUNT(*) FROM "{table.name}"')
                    )

            log("")
            log(f"{'table':<28} {'sqlite':>10} {'postgres':>10}  status")
            log("-" * 60)
            mismatched = False
            for table in tables:
                s, d = src_counts[table.name], dest_counts[table.name]
                ok = s == d
                mismatched |= not ok
                log(f"{table.name:<28} {s:>10} {d:>10}  {'OK' if ok else 'MISMATCH'}")
            log("-" * 60)
            if mismatched:
                fail("row-count mismatch — migration NOT verified")
            log(f"Migration verified: {sum(src_counts.values())} rows across {len(tables)} tables.")
    finally:
        src.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wipe-target", action="store_true",
                        help="TRUNCATE destination tables before copying (source is never touched)")
    parser.add_argument("--dry-run", action="store_true",
                        help="read + convert source rows, write nothing")
    args = parser.parse_args()

    sqlite_path, database_url = resolve_config()
    safe_url = database_url.split("@")[-1]
    log(f"Source     : {sqlite_path} (read-only)")
    log(f"Destination: postgresql+asyncpg://...@{safe_url}")
    asyncio.run(migrate(sqlite_path, database_url, args.wipe_target, args.dry_run))


if __name__ == "__main__":
    main()
