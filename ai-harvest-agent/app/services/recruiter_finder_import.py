"""Parse, intelligently column-map, sanitize and validate an uploaded CSV/XLSX for
the Recruiter Contact Finder — BEFORE any Apollo credit is spent.

Flow (app/routes/recruiter_finder_routes.py::validate / upload):
  raw bytes → DataFrame (pandas) → fuzzy header → supported fields
            → sanitize each cell → per-row validation verdict → summary + preview

Supported fields: company (MANDATORY), person_name, linkedin_url, domain, location,
title. A row with a person/linkedin enriches that person; a company-only row finds
the chosen persona at that company. A row with no company is ``missing_company`` and
is excluded from enrichment; an exact repeat is ``duplicate``.

pandas + openpyxl are already project deps; pandas is imported lazily so importing
this module stays cheap.
"""
from __future__ import annotations

import io
import re
from urllib.parse import urlparse

import structlog

logger = structlog.get_logger(__name__)

# Supported field → header synonyms (all compared in normalized form: lowercased,
# non-alphanumerics collapsed to single spaces). Order is the matching PRIORITY, so
# linkedin is claimed before the looser domain/website synonyms.
_FIELD_SYNONYMS: list[tuple[str, list[str]]] = [
    ("company", ["company", "company name", "account", "account name", "organization",
                 "organisation", "employer", "current company", "org", "business"]),
    ("linkedin_url", ["linkedin", "linkedin url", "linkedin profile", "linkedin profile url",
                      "profile url", "profile", "li url", "linkedin link"]),
    ("person_name", ["name", "full name", "person", "person name", "contact", "contact name",
                     "recruiter", "recruiter name", "fullname", "lead"]),
    ("domain", ["domain", "website", "company domain", "company website", "web", "site", "url"]),
    ("location", ["location", "city", "hq", "hq city", "headquarters", "region", "country",
                  "geo", "based in", "area"]),
    ("title", ["title", "job title", "role", "designation", "position"]),
]

_FIELDS = [f for f, _ in _FIELD_SYNONYMS]

# Values that look empty in exported sheets but aren't literally blank. Exports routinely
# fill unknown cells with an em/en dash or "N/A"; treating those as real data is what
# collapsed every row to the same dedupe key (a LinkedIn column full of "—" → one key for
# the whole file). Compared case-insensitively against the trimmed cell.
_PLACEHOLDERS = {"", "-", "--", "---", "–", "—", "n/a", "na", "n.a.", "null", "none", "nil"}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(h).lower()).strip()


def _map_columns(headers: list[str]) -> dict[str, str | None]:
    """Best-effort map each supported field to an original header. Each header is
    claimed by at most one field; linkedin wins over domain for a 'linkedin…' header."""
    norm_headers = [(_norm(h), h) for h in headers]
    used: set[str] = set()
    mapping: dict[str, str | None] = {f: None for f in _FIELDS}

    for field, synonyms in _FIELD_SYNONYMS:
        syn = set(synonyms)
        chosen: str | None = None
        # 1) exact normalized match
        for nh, orig in norm_headers:
            if orig in used:
                continue
            if nh in syn:
                chosen = orig
                break
        # 2) contains a synonym token (e.g. "Account Name (HQ)")
        if chosen is None:
            for nh, orig in norm_headers:
                if orig in used:
                    continue
                if field == "domain" and "linkedin" in nh:
                    continue  # never map a linkedin column to domain
                if any(s in nh for s in syn):
                    chosen = orig
                    break
        if chosen is not None:
            mapping[field] = chosen
            used.add(chosen)
    return mapping


def _clean_domain(value: str) -> str:
    v = value.strip()
    if not v:
        return ""
    if "//" in v or v.startswith("www.") or "/" in v:
        parsed = urlparse(v if "//" in v else f"//{v}", scheme="")
        host = (parsed.netloc or parsed.path).strip("/")
    else:
        host = v
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split("/")[0]


def _clean_linkedin(value: str) -> str:
    v = value.strip()
    if not v:
        return ""
    if "linkedin.com" in v.lower() and "//" not in v:
        v = "https://" + v.lstrip("/")
    return v


def _dedupe_key(row: dict) -> str:
    # Only treat the LinkedIn field as an identity when it is an ACTUAL profile URL — a
    # stray placeholder/garbage value must never become the dedupe key for the whole file.
    if "linkedin.com" in row["linkedin_url"].lower():
        return "li:" + row["linkedin_url"].lower().rstrip("/")
    return "cp:" + row["company"].lower() + "|" + row["person_name"].lower()


def parse_and_validate(content: bytes, filename: str, *, max_rows: int = 1000) -> dict:
    """Parse an uploaded CSV/XLSX and return a validation preview.

    Returns ``{mapping, columns, rows, summary}`` where each row carries the
    sanitized fields + a ``validation_status`` of valid | missing_company | duplicate.
    Raises ``ValueError`` on an unreadable/empty/unsupported file."""
    import pandas as pd  # lazy — heavy import

    name = (filename or "").lower()
    buf = io.BytesIO(content)
    try:
        if name.endswith(".csv") or name.endswith(".txt"):
            df = pd.read_csv(buf, dtype=str, keep_default_na=False)
        elif name.endswith(".xlsx") or name.endswith(".xls"):
            df = pd.read_excel(buf, dtype=str, keep_default_na=False)
        else:
            # Fall back to CSV sniffing for unknown extensions.
            df = pd.read_csv(buf, dtype=str, keep_default_na=False)
    except Exception as exc:
        raise ValueError(f"Could not read '{filename}': {exc}") from exc

    if df.empty or len(df.columns) == 0:
        raise ValueError("The uploaded file has no rows.")

    headers = [str(c) for c in df.columns]
    mapping = _map_columns(headers)
    if not mapping.get("company"):
        raise ValueError(
            "Couldn't find a Company column. Add a column named 'Company' (or "
            "Account / Organization / Employer) and re-upload."
        )

    truncated = 0
    if len(df) > max_rows:
        truncated = len(df) - max_rows
        df = df.iloc[:max_rows]

    def cell(record, field):
        col = mapping.get(field)
        if not col:
            return ""
        val = record.get(col, "")
        s = "" if val is None else str(val).strip()
        return "" if s.lower() in _PLACEHOLDERS else s

    rows: list[dict] = []
    seen: set[str] = set()
    counts = {"valid": 0, "missing_company": 0, "duplicate": 0}

    for idx, record in enumerate(df.to_dict(orient="records")):
        row = {
            "row_index": idx,
            "company": cell(record, "company"),
            "person_name": cell(record, "person_name"),
            "linkedin_url": _clean_linkedin(cell(record, "linkedin_url")),
            "domain": _clean_domain(cell(record, "domain")),
            "location": cell(record, "location"),
            "title": cell(record, "title"),
        }
        # Skip fully-blank rows entirely (don't count them).
        if not any(row[f] for f in _FIELDS):
            continue

        if not row["company"]:
            status = "missing_company"
        else:
            key = _dedupe_key(row)
            if key in seen:
                status = "duplicate"
            else:
                seen.add(key)
                status = "valid"
        row["validation_status"] = status
        counts[status] += 1
        rows.append(row)

    summary = {
        "total_rows": len(rows),
        "ready": counts["valid"],
        "missing_company": counts["missing_company"],
        "duplicate": counts["duplicate"],
        "truncated": truncated,
    }
    logger.info("recruiter_finder_import_parsed", filename=filename, **summary)
    return {"mapping": mapping, "columns": headers, "rows": rows, "summary": summary}
