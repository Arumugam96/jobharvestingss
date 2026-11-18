"""Full-Time vs Contractual classification for the jobs list "Job type" filter.

Only data the source itself stated is used. Nothing is inferred from title or
description text, which is why the stored `scraped_jobs.job_type` is not used:
BusinessFilterService fills it partly by scanning the description.

Rules, in order:
1. The job's own `employment_type` label (Naukri "Full Time, Permanent" /
   "Full Time, Temporary/Contractual", LinkedIn "Full-time" / "Contract", ...).
   A contract-type word wins over a full-time word ("Full Time, Temporary/
   Contractual" is contractual).
2. No label, LinkedIn or Dice job, and the run searched for one specific type:
   both sites enforce that search filter (f_JT / employmentType), so every job
   they returned has it. Naukri ignores its job-type search filter, so it never
   gets this fallback.
3. Anything else: a label that is neither (part-time, internship, ...) is
   "other"; no label and no enforced search type is "" (not specified).

`classify_job_type` (Python) and `job_type_class_expr` (SQL) implement the same
rules; tests check that they agree.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, case, func, or_, select

FULL_TIME = "full_time"
CONTRACT = "contract"
OTHER = "other"
NOT_SPECIFIED = ""

# Values the GET /jobs `job_type` filter accepts. "other" (part-time,
# internship, ...) is reachable only under "All".
JOB_TYPE_FILTER_VALUES = {FULL_TIME, CONTRACT, "not_specified"}

# Matched as lowercase substrings of the label. Labels are short platform
# vocabularies, so plain substrings are safe (no free-text description here).
_CONTRACT_WORDS = ("contract", "temporary", "freelance", "c2h", "c2c", "1099")
_FULL_TIME_WORDS = ("full time", "full-time", "fulltime", "permanent")

# Run search filter (FiltersConfig.job_type) -> class, and the sources that
# enforce it at search time (see business_filter_service._JT_ENFORCED).
_RUN_FILTER_CLASS = {
    "Contract": CONTRACT,
    "Freelance": CONTRACT,
    "Permanent": FULL_TIME,
    "Full-time": FULL_TIME,
}
_ENFORCING_SOURCES = ("LinkedIn", "Dice")


def classify_job_type(employment_type: str | None, source: str | None,
                      run_job_type: str | None = None) -> str:
    """Return "full_time" | "contract" | "other" | "" for one job (rules in the module doc)."""
    label = (employment_type or "").strip().lower()
    if label:
        if any(w in label for w in _CONTRACT_WORDS):
            return CONTRACT
        if any(w in label for w in _FULL_TIME_WORDS):
            return FULL_TIME
        return OTHER
    if (source or "") in _ENFORCING_SOURCES:
        return _RUN_FILTER_CLASS.get((run_job_type or "").strip(), NOT_SPECIFIED)
    return NOT_SPECIFIED


def run_job_type_subquery(job_model: Any, run_model: Any) -> Any:
    """Correlated scalar subquery: the job's run's search `job_type` filter (or NULL)."""
    return (
        select(run_model.filters_snapshot["job_type"].as_string())
        .where(run_model.id == job_model.run_id)
        .correlate(job_model)
        .scalar_subquery()
    )


def job_type_class_expr(job_model: Any, run_model: Any) -> Any:
    """SQL CASE equal to classify_job_type() over (employment_type, source, run filter)."""
    label = func.lower(func.trim(func.coalesce(job_model.employment_type, "")))
    run_jt = run_job_type_subquery(job_model, run_model)
    no_label = label == ""
    enforced = job_model.source.in_(_ENFORCING_SOURCES)
    contract_runs = [k for k, v in _RUN_FILTER_CLASS.items() if v == CONTRACT]
    full_time_runs = [k for k, v in _RUN_FILTER_CLASS.items() if v == FULL_TIME]
    return case(
        (and_(~no_label, or_(*[label.contains(w) for w in _CONTRACT_WORDS])), CONTRACT),
        (and_(~no_label, or_(*[label.contains(w) for w in _FULL_TIME_WORDS])), FULL_TIME),
        (~no_label, OTHER),
        (and_(enforced, run_jt.in_(contract_runs)), CONTRACT),
        (and_(enforced, run_jt.in_(full_time_runs)), FULL_TIME),
        else_=NOT_SPECIFIED,
    )
