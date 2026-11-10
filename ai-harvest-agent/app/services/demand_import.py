"""Parse and validate a Demand Search upload — client job requirements, one role
per row — BEFORE anything is saved or searched.

Flow (app/routes/demand_routes.py::validate / upload):
  raw bytes → DataFrame (pandas) → header synonyms → per-row checks → summary

Columns (header matching is case- and punctuation-insensitive):
  required  Job Title (Title / Role / Position / Designation)
            Job Description (Description / JD)
  optional  Skills (Key Skills / Skill Set), Experience (Exp / Exp (yrs) / Years of
            Experience), Location (City), Client (Company / Client Name)

File-level problems raise DemandFileError (nothing is parsed further): wrong type,
too large, unreadable, empty, a missing required column (the headers found are
listed), more than MAX_ROWS data rows. Row-level problems — an empty title, a JD
shorter than MIN_JD_CHARS, a duplicate title+JD — are listed (row number + reason)
and the row is excluded; the valid rows proceed.

Manual entry (parse_and_validate_rows) runs the same row checks on jobs typed into
the UI — title, JD and a flat text-only metadata dictionary (metadata_errors).

Same approach as app/services/recruiter_finder_import.py (pandas dtype=str,
placeholder cells treated as blank); pandas is imported lazily.
"""
from __future__ import annotations

import hashlib
import io
import re

import structlog

logger = structlog.get_logger(__name__)

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ROWS = 50
MIN_JD_CHARS = 50
PREVIEW_ROWS = 7
ALLOWED_EXTENSIONS = (".xlsx", ".xls", ".csv", ".docx")

# Field → header synonyms in normalized form (lowercased, non-alphanumerics → one
# space). Order is the matching priority.
_FIELD_SYNONYMS: list[tuple[str, list[str]]] = [
    ("job_title", ["job title", "title", "role", "position", "designation"]),
    ("job_description", ["job description", "description", "jd"]),
    ("skills", ["skills", "key skills", "skill set", "skillset"]),
    ("experience", ["experience", "exp", "exp yrs", "years of experience"]),
    ("location", ["location", "city"]),
    ("client", ["client", "company", "client name"]),
]
REQUIRED_FIELDS = ("job_title", "job_description")
FIELD_LABELS = {
    "job_title": "Job Title", "job_description": "Job Description", "skills": "Skills",
    "experience": "Experience", "location": "Location", "client": "Client",
}
_MISSING_HINT = {
    "job_title": "Each row needs a job title. Rename the column that holds it to Job Title, "
                 "Title, Role, Position or Designation, then upload again.",
    "job_description": "Each row needs a job description. Rename the column that holds the "
                       "JD to Job Description, Description or JD, then upload again.",
}

# Cells that look filled in exports but mean "nothing" (same set as the Contact
# Finder import).
_PLACEHOLDERS = {"", "-", "--", "---", "–", "—", "n/a", "na", "n.a.", "null", "none", "nil", "nan"}
_SKILL_SPLIT_RE = re.compile(r"[,;|\n]+")


class DemandFileError(ValueError):
    """The file can't be used at all. ``code`` is one of unsupported_type |
    too_large | unreadable | empty | missing_column | too_many_rows; ``extra``
    carries what the UI shows with it (e.g. headers_found for missing_column)."""

    def __init__(self, code: str, title: str, message: str, **extra) -> None:
        super().__init__(message)
        self.code = code
        self.title = title
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        return {"code": self.code, "title": self.title, "message": self.message, **self.extra}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(h).lower()).strip()


def map_columns(headers: list[str]) -> dict[str, str | None]:
    """Map each field to an original header: exact normalized matches first (for
    every field), then headers that contain a synonym ("Job Title (required)").
    Each header is claimed by at most one field."""
    norm_headers = [(_norm(h), h) for h in headers]
    used: set[str] = set()
    mapping: dict[str, str | None] = {f: None for f, _ in _FIELD_SYNONYMS}
    compact = {f: {x.replace(" ", "") for x in syn} for f, syn in _FIELD_SYNONYMS}
    for exact in (True, False):
        for field, synonyms in _FIELD_SYNONYMS:
            if mapping[field] is not None:
                continue
            for nh, orig in norm_headers:
                if orig in used or not nh:
                    continue
                # Exact also ignores the spaces punctuation left ("J.D." → "jd").
                hit = (nh in synonyms or nh.replace(" ", "") in compact[field]) if exact else any(
                    re.search(rf"\b{re.escape(s)}\b", nh) for s in synonyms
                )
                if hit:
                    mapping[field] = orig
                    used.add(orig)
                    break
    return mapping


def demand_job_url(title: str, jd: str, context: dict[str, str] | None = None) -> str:
    """Synthetic, stable job_url of a requirement: the same title + JD + context
    (skills/experience/location/client and any metadata) always maps to the same
    key, so a repeated requirement reuses its Resdex match across batches, while a
    change to any context value triggers a fresh search. With no context the key
    is the historical title + JD hash, so earlier matches still reuse
    (normalize_job_url keeps it as-is: no query/fragment/trailing slash)."""
    basis = title.strip().lower() + "\n" + jd.strip()
    ctx = {
        k.strip().lower(): " ".join(str(v).split())
        for k, v in (context or {}).items()
        if str(v or "").strip()
    }
    if ctx:
        basis += "\n" + "\n".join(f"{k}={ctx[k]}" for k in sorted(ctx))
    digest = hashlib.sha1(basis.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"demand://jd/{digest[:20]}"


def split_skills(text: str) -> list[str]:
    seen: list[str] = []
    for part in _SKILL_SPLIT_RE.split(text or ""):
        p = " ".join(part.split())
        if p and p.lower() not in {s.lower() for s in seen}:
            seen.append(p)
    return seen


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{max(1, round(n / 1024))} KB"


def _read_docx_frame(content: bytes, filename: str):
    """Extract the first table from a .docx Word document as a DataFrame.
    The first row of the table is used as the header."""
    import pandas as pd

    try:
        from docx import Document
    except ImportError:
        raise DemandFileError(
            "unsupported_type", "Word documents are not supported on this server",
            f"{filename} is a .docx file, but the python-docx library is not installed. "
            "Save the document as .xlsx or .csv and upload it again.",
        ) from None

    try:
        doc = Document(io.BytesIO(content))
    except Exception as exc:
        raise DemandFileError(
            "unreadable", "The file could not be read",
            f"{filename} could not be read as a Word document ({str(exc)[:200]}). "
            "Save it again as .xlsx or .csv and upload it again.",
        ) from exc

    if not doc.tables:
        raise DemandFileError(
            "empty", "No table found in the document",
            f"{filename} does not contain a table. The Word document needs a table with a "
            "header row (Job Title, Job Description, …) and data rows. Alternatively, save "
            "the data as .xlsx or .csv.",
        )

    table = doc.tables[0]
    rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
    if len(rows) < 2:
        raise DemandFileError(
            "empty", "No data rows in the table",
            f"{filename} has a table but only a header row with no data. Add at least one "
            "row with a Job Title and a Job Description.",
        )
    headers = rows[0]
    data = rows[1:]
    return pd.DataFrame(data, columns=headers, dtype=str), ""


def _read_frame(content: bytes, filename: str):
    """(DataFrame, sheet name or "") — raises DemandFileError."""
    import pandas as pd  # lazy — heavy import

    name = (filename or "").lower()
    buf = io.BytesIO(content)
    try:
        if name.endswith(".csv"):
            return pd.read_csv(buf, dtype=str, keep_default_na=False), ""
        if name.endswith(".docx"):
            return _read_docx_frame(content, filename)
        if name.endswith(".xls"):
            try:
                import xlrd  # noqa: F401 — pandas' engine for legacy .xls
            except ImportError:
                raise DemandFileError(
                    "unsupported_type", "This file type is not supported",
                    f"{filename} is an old .xls workbook, which this server can't read. "
                    "Save the sheet as .xlsx or .csv and upload it again.",
                ) from None
        book = pd.ExcelFile(buf)
        sheet = book.sheet_names[0] if book.sheet_names else ""
        return book.parse(sheet, dtype=str, keep_default_na=False), sheet
    except DemandFileError:
        raise
    except Exception as exc:
        raise DemandFileError(
            "unreadable", "The file could not be read",
            f"{filename} could not be read ({str(exc)[:200]}). Save it again as .xlsx, "
            ".xls, .csv or .docx and upload it again.",
        ) from exc


def _check_rows(
    parsed: list[dict], *, no_title_fix: str,
) -> tuple[list[dict], list[dict], list[dict]]:
    """The per-row checks shared by file uploads and manual entry: an empty title,
    a JD shorter than MIN_JD_CHARS or a duplicate title+JD excludes the row.
    Returns (valid rows, invalid [{row, reason, fix}], preview)."""
    rows: list[dict] = []
    invalid: list[dict] = []
    first_row: dict[str, int] = {}
    preview: list[dict] = []
    for r in parsed:
        title, jd = r["job_title"], r["job_description"]
        reason = fix = ""
        check = "ok"
        if not title:
            reason, fix, check = "Job title is empty.", no_title_fix, "no_title"
        elif len(jd) < MIN_JD_CHARS:
            reason = (f"Job description is too short ({len(jd)} characters, needs at least "
                      f"{MIN_JD_CHARS}).")
            fix, check = "Paste the full JD so the requirements can be extracted.", "short_jd"
        else:
            key = title.strip().lower() + "\n" + jd.strip()
            if key in first_row:
                reason = (f"Duplicate of row {first_row[key]} (same title and description).")
                fix, check = f"Only row {first_row[key]} will be searched.", "duplicate"
            else:
                first_row[key] = r["row"]
        metadata = dict(r.get("metadata") or {})
        if reason:
            invalid.append({"row": r["row"], "reason": reason, "fix": fix})
        else:
            rows.append({
                "row": r["row"], "job_title": title, "job_description": jd,
                "skills": split_skills(r["skills"]), "skills_text": r["skills"],
                "experience": r["experience"], "location": r["location"], "client": r["client"],
                "metadata": metadata,
                "job_url": demand_job_url(title, jd, {
                    "skills": r["skills"], "experience": r["experience"],
                    "location": r["location"], "client": r["client"], **metadata,
                }),
            })
        if len(preview) < PREVIEW_ROWS:
            preview.append({
                "row": r["row"], "job_title": title, "client": r["client"],
                "location": r["location"], "experience": r["experience"],
                "job_description": jd[:160], "skills": r["skills"], "check": check,
                "metadata": metadata,
            })
    return rows, invalid, preview


# ── Manual entry (POST /demand/validate-json, /demand/create) ─────────────────

SOURCE_FILE = "file"
SOURCE_MANUAL = "manual"
MANUAL_SOURCE_LABEL = "Manual entry"
# Metadata keys that fill the job's own fields; every other key is extra LLM context.
KNOWN_METADATA_FIELDS = ("skills", "experience", "location", "client")
RESERVED_METADATA_KEYS = ("job_title", "job_description")
METADATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
METADATA_MAX_KEYS = 20
METADATA_MAX_VALUE_CHARS = 500


def metadata_errors(meta: object) -> list[dict]:
    """The dictionary rules for a manual job's metadata — the UI enforces the same
    rules live. A flat JSON object of at most METADATA_MAX_KEYS keys; each key
    matches ^[a-z][a-z0-9_]{0,39}$ and is unique case-insensitively; each value is
    non-empty text of at most METADATA_MAX_VALUE_CHARS characters (numbers,
    booleans, null, nested objects and arrays are rejected, not coerced).
    Returns [{key, message}] (key "" for an error about the whole object)."""
    if meta is None:
        return []
    if not isinstance(meta, dict):
        return [{"key": "", "message": "Metadata must be a JSON object of text fields, "
                                       'like {"location": "Pune"}.'}]
    errors: list[dict] = []
    if len(meta) > METADATA_MAX_KEYS:
        errors.append({"key": "", "message": f"Metadata has {len(meta)} fields; the limit is "
                                             f"{METADATA_MAX_KEYS}."})
    seen: set[str] = set()
    for key, value in meta.items():
        k = str(key)
        if not METADATA_KEY_RE.match(k):
            errors.append({"key": k, "message": (
                "Keys use lowercase letters, digits and underscores, start with a letter and "
                "are at most 40 characters (e.g. work_mode).")})
        elif k in RESERVED_METADATA_KEYS:
            errors.append({"key": k, "message": f"{k} is set by its own field, not metadata."})
        if k.lower() in seen:
            errors.append({"key": k, "message": "Duplicate key (keys are case-insensitive)."})
        seen.add(k.lower())
        if isinstance(value, (dict, list, tuple)):
            errors.append({"key": k, "message": "Nested objects and arrays are not allowed — "
                                                "use plain text (e.g. \"Java, Spring\")."})
        elif not isinstance(value, str):
            errors.append({"key": k, "message": "The value must be text — put it in quotes."})
        elif not value.strip():
            errors.append({"key": k, "message": "The value is empty."})
        elif len(value.strip()) > METADATA_MAX_VALUE_CHARS:
            errors.append({"key": k, "message": f"The value is {len(value.strip())} characters; "
                                                f"the limit is {METADATA_MAX_VALUE_CHARS}."})
    return errors


def parse_and_validate_rows(jobs: list[dict], source_label: str = MANUAL_SOURCE_LABEL) -> dict:
    """Validate manually entered jobs ({job_title, job_description, metadata}) —
    the same preview dict as parse_and_validate (filename = ``source_label``, no
    sheet / column mapping) and the same row checks, MAX_ROWS and job_url key.

    Metadata keys skills / experience / location / client fill the row's fields;
    the rest stay in row["metadata"] (sent to the LLM as extra context). The
    metadata is expected to have passed metadata_errors() already (the request
    model enforces it); non-text values are dropped defensively here. ``row`` is
    the job's 1-based position. Raises DemandFileError (empty / too_many_rows)."""
    parsed: list[dict] = []
    for idx, job in enumerate(jobs or []):
        job = job or {}
        meta_in = job.get("metadata") or {}
        meta: dict[str, str] = {}
        if isinstance(meta_in, dict):
            for k, v in meta_in.items():
                if isinstance(v, str) and v.strip():
                    meta[str(k).strip().lower()] = v.strip()
        r = {
            "job_title": " ".join(str(job.get("job_title") or "").split()),
            "job_description": str(job.get("job_description") or "").strip(),
        }
        for f in KNOWN_METADATA_FIELDS:
            r[f] = meta.pop(f, "")
        r["metadata"] = meta
        if not (r["job_title"] or r["job_description"] or meta
                or any(r[f] for f in KNOWN_METADATA_FIELDS)):
            continue                          # a fully blank job — not counted
        r["row"] = idx + 1
        parsed.append(r)

    if not parsed:
        raise DemandFileError(
            "empty", "No jobs entered",
            "Add at least one job with a Job title and a Job description.",
        )
    if len(parsed) > MAX_ROWS:
        raise DemandFileError(
            "too_many_rows", f"More than {MAX_ROWS} jobs",
            f"{len(parsed)} jobs were entered. A batch can hold up to {MAX_ROWS} jobs; split "
            "them into separate batches.", total_rows=len(parsed),
        )

    rows, invalid, preview = _check_rows(parsed, no_title_fix="Add a job title.")
    result = {
        "filename": source_label,
        "size_bytes": 0,
        "size_label": "",
        "sheet": "",
        "total_rows": len(parsed),
        "valid_rows": len(rows),
        "invalid": invalid,
        "mapping": {},
        "headers_found": [],
        "ignored_headers": [],
        "rows": rows,
        "preview": preview,
        "source_kind": SOURCE_MANUAL,
    }
    logger.info("demand_manual_parsed", total=len(parsed), valid=len(rows), invalid=len(invalid))
    return result


def parse_and_validate(content: bytes, filename: str) -> dict:
    """Parse an uploaded .xlsx/.xls/.csv/.docx of job requirements.

    Returns {filename, size_bytes, size_label, sheet, total_rows, valid_rows,
    invalid: [{row, reason, fix}], mapping, headers_found, ignored_headers, rows
    (the valid rows: {row, job_title, job_description, skills, skills_text,
    experience, location, client, job_url}), preview}. ``row`` is the spreadsheet
    row number (the header is row 1). Raises DemandFileError for a file that
    can't be used at all. Writes nothing."""
    filename = filename or "upload.csv"
    size = len(content or b"")
    lower = filename.lower()
    if not lower.endswith(ALLOWED_EXTENSIONS):
        ext = lower.rsplit(".", 1)[-1].upper() if "." in lower else "file without an extension"
        raise DemandFileError(
            "unsupported_type", "This file type is not supported",
            f"{filename} is a {ext}. Save the data as .xlsx, .xls, .csv or .docx and upload it again.",
            size_label=_fmt_size(size),
        )
    if size > MAX_FILE_BYTES:
        raise DemandFileError(
            "too_large", "The file is larger than 10 MB",
            f"{filename} is {_fmt_size(size)}. Delete extra sheets and columns, or split it "
            f"into files of up to {MAX_ROWS} rows each.",
            size_label=_fmt_size(size),
        )
    if size == 0:
        raise DemandFileError(
            "empty", "No rows found",
            f"{filename} is empty. Add a header row and at least one row with a Job Title "
            "and a Job Description.",
            size_label=_fmt_size(size),
        )

    df, sheet = _read_frame(content, filename)
    headers = [str(c) for c in df.columns]
    mapping = map_columns(headers)
    missing = [f for f in REQUIRED_FIELDS if not mapping.get(f)]
    if missing:
        first = missing[0]
        raise DemandFileError(
            "missing_column", f"{FIELD_LABELS[first]} column not found", _MISSING_HINT[first],
            headers_found=headers, mapping=mapping, missing=missing, size_label=_fmt_size(size),
        )

    def cell(record: dict, field: str) -> str:
        col = mapping.get(field)
        if not col:
            return ""
        val = record.get(col, "")
        s = "" if val is None else str(val).strip()
        return "" if s.lower() in _PLACEHOLDERS else s

    parsed: list[dict] = []
    for idx, record in enumerate(df.to_dict(orient="records")):
        r = {f: cell(record, f) for f, _ in _FIELD_SYNONYMS}
        if not any(r.values()):
            continue                          # fully blank row — not counted
        r["row"] = idx + 2                    # row 1 is the header
        parsed.append(r)

    if not parsed:
        raise DemandFileError(
            "empty", "No rows found",
            f"{filename} has a header row but no data. Add at least one row with a Job Title "
            "and a Job Description.",
            size_label=_fmt_size(size), mapping=mapping, headers_found=headers,
        )
    if len(parsed) > MAX_ROWS:
        raise DemandFileError(
            "too_many_rows", f"More than {MAX_ROWS} rows",
            f"{filename} has {len(parsed)} rows. A batch can hold up to {MAX_ROWS} rows; split "
            "the file and upload each part as its own batch.",
            size_label=_fmt_size(size), total_rows=len(parsed),
        )

    rows, invalid, preview = _check_rows(
        parsed, no_title_fix=f"Add a title in the {mapping['job_title']} column.")

    mapped = {h for h in mapping.values() if h}
    result = {
        "filename": filename,
        "size_bytes": size,
        "size_label": _fmt_size(size),
        "sheet": sheet,
        "total_rows": len(parsed),
        "valid_rows": len(rows),
        "invalid": invalid,
        "mapping": mapping,
        "headers_found": headers,
        "ignored_headers": [h for h in headers if h not in mapped],
        "rows": rows,
        "preview": preview,
        "source_kind": SOURCE_FILE,
    }
    logger.info("demand_import_parsed", filename=filename, total=len(parsed), valid=len(rows),
                invalid=len(invalid))
    return result
