"""Pre-migration length check: find SQLite values that won't fit their
destination PostgreSQL VARCHAR(n) columns.

The SQLite -> PostgreSQL migration fails with

    asyncpg.exceptions.StringDataRightTruncationError:
    value too long for type character varying(255)

whenever a scraped free-text value is longer than the String(n) length
declared on the ORM model. SQLite ignores VARCHAR lengths, so these only
surface on the PostgreSQL side. Run this against the SAME SQLite file the
migration will read to catch every offender up front (seconds, read-only) —
before the slow full migrate.

Usage (reads SQLITE_DATABASE_PATH / .env, same as the migration script):
    python scripts/check_column_lengths.py

Exits 0 if everything fits, 1 if any column has values exceeding its declared
length (printed with table.column, the limit, the worst length, and how many
rows overflow).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import String  # noqa: E402

import app.models.auth  # noqa: F401,E402
import app.models.harvest_run  # noqa: F401,E402
import app.models.recruiter  # noqa: F401,E402
from app.models.harvest import Base  # noqa: E402


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    sqlite_path = os.environ.get("SQLITE_DATABASE_PATH")
    if not sqlite_path:
        print("ERROR: SQLITE_DATABASE_PATH is not set", file=sys.stderr)
        sys.exit(1)
    path = Path(sqlite_path)
    if not path.is_absolute():
        path = (Path(__file__).resolve().parents[1] / path).resolve()
    if not path.exists():
        print(f"ERROR: SQLite database not found: {path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    src_tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }

    print(f"Source: {path} (read-only)\n")
    problems = []
    for table in Base.metadata.sorted_tables:
        if table.name not in src_tables:
            continue
        src_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table.name})")}
        for col in table.columns:
            # Only bounded String(n) columns can truncate; Text is unbounded.
            length = getattr(col.type, "length", None)
            if not isinstance(col.type, String) or not length or col.name not in src_cols:
                continue
            worst, over = conn.execute(
                f'SELECT MAX(LENGTH("{col.name}")), '
                f'SUM(CASE WHEN LENGTH("{col.name}") > ? THEN 1 ELSE 0 END) '
                f'FROM "{table.name}"',
                (length,),
            ).fetchone()
            if over:
                problems.append((table.name, col.name, length, worst, over))
                print(
                    f"  OVERFLOW  {table.name}.{col.name}: "
                    f"limit={length} worst={worst} rows_over={over}"
                )
    conn.close()

    if problems:
        print(
            f"\n{len(problems)} column(s) would truncate. Widen them to Text "
            "(or String(>=worst)) in the model, recreate the schema "
            "(alembic downgrade base && alembic upgrade head), then re-migrate."
        )
        sys.exit(1)
    print("OK: every String(n) column fits its declared length.")


if __name__ == "__main__":
    main()
