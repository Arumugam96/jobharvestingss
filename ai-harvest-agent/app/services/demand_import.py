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
ALLOWED_EXTENSIONS = (".xlsx", ".xls", ".csv")

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


def demand_job_url(title: str, jd: str) -> str:
    """Synthetic, stable job_url of a requirement: the same title + JD always maps
    to the same key, so a repeated JD reuses its Resdex match across batches
    (normalize_job_url keeps it as-is: no query/fragment/trailing slash)."""
    digest = hashlib.sha1(
        (title.strip().lower() + "\n" + jd.strip()).encode("utf-8"), usedforsecurity=False,
    ).hexdigest()
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


def _read_frame(content: bytes, filename: str):
    """(DataFrame, sheet name or "") — raises DemandFileError."""
    import pandas as pd  # lazy — heavy import

    name = (filename or "").lower()
    buf = io.BytesIO(content)
    try:
        if name.endswith(".csv"):
            return pd.read_csv(buf, dtype=str, keep_default_na=False), ""
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
            ".xls or .csv and upload it again.",
        ) from exc


def parse_and_validate(content: bytes, filename: str) -> dict:
    """Parse an uploaded .xlsx/.xls/.csv of job requirements.

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
            f"{filename} is a {ext}. Save the sheet as .xlsx, .xls or .csv and upload it again.",
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

    rows: list[dict] = []
    invalid: list[dict] = []
    first_row: dict[str, int] = {}
    preview: list[dict] = []
    for r in parsed:
        title, jd = r["job_title"], r["job_description"]
        reason = fix = ""
        check = "ok"
        if not title:
            reason, fix, check = "Job title is empty.", (
                f"Add a title in the {mapping['job_title']} column."), "no_title"
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
        if reason:
            invalid.append({"row": r["row"], "reason": reason, "fix": fix})
        else:
            rows.append({
                "row": r["row"], "job_title": title, "job_description": jd,
                "skills": split_skills(r["skills"]), "skills_text": r["skills"],
                "experience": r["experience"], "location": r["location"], "client": r["client"],
                "job_url": demand_job_url(title, jd),
            })
        if len(preview) < PREVIEW_ROWS:
            preview.append({
                "row": r["row"], "job_title": title, "client": r["client"],
                "location": r["location"], "experience": r["experience"],
                "job_description": jd[:160], "skills": r["skills"], "check": check,
            })

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
    }
    logger.info("demand_import_parsed", filename=filename, total=len(parsed), valid=len(rows),
                invalid=len(invalid))
    return result
