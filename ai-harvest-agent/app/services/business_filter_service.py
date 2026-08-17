"""
Business Filter Service — post-scraping rule pipeline.

Loads classification rules from data/master/ JSON files at startup.
Applies domain / hiring-entity / GCC classification, then filters.

Pipeline
────────
1. classify_all()   → annotate domain / hiring_entity / is_gcc / job_type
2. apply_all()      → keep only jobs that satisfy active filter rules
3. track_ambiguous()→ append unknown companies to ambiguous_companies.json

Master files (data/master/)
────────────────────────────
domain_keywords.json       – domain → keyword list
gcc_master_list.json       – known GCC company names
staffing_firm_master_list.json – known staffing firm names + generic keywords
direct_client_master_list.json – known direct client company names
ambiguous_companies.json   – append-only log of unclassified companies
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import structlog

from app.core.domain_keywords import (
    domain_matches as _domain_matches,
    infer_domain as _infer_domain_shared,
)
from app.models.harvest_models import FiltersConfig
from app.models.unified_job import UnifiedJob

logger = structlog.get_logger(__name__)

_MASTER_DIR = Path(__file__).resolve().parents[2] / "data" / "master"


# ── Master-list loader ────────────────────────────────────────────────────────

def _load_json(filename: str, key: str | None = None) -> Any:
    path = _MASTER_DIR / filename
    if not path.exists():
        logger.warning("master_file_missing", file=str(path))
        return [] if key else {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data[key] if key else data
    except Exception as exc:
        logger.warning("master_file_load_error", file=str(path), error=str(exc))
        return [] if key else {}


def _load_set(filename: str, key: str = "companies") -> frozenset[str]:
    items = _load_json(filename, key)
    return frozenset(s.lower().strip() for s in items if isinstance(s, str))


# Load once at import time — refreshed by restarting the server. Domain
# classification/matching lives in app.core.domain_keywords (shared with
# LinkedInAgent's search-time refinement and in-scrape early-stop).
_KNOWN_GCC:          frozenset[str]       = _load_set("gcc_master_list.json")
_KNOWN_STAFFING:     frozenset[str]       = _load_set("staffing_firm_master_list.json")
_STAFFING_KW:        frozenset[str]       = frozenset(
    s.lower() for s in _load_json("staffing_firm_master_list.json", "keywords") if isinstance(s, str)
)
_KNOWN_DIRECT:       frozenset[str]       = _load_set("direct_client_master_list.json")

# GCC phrase + abbreviation detection
_GCC_PHRASES: frozenset[str] = frozenset({
    "global capability center", "global capability centre",
    "global service center", "global service centre",
    "captive center", "captive centre",
    "center of excellence", "global delivery center",
    "global in-house center", "gic",
})
_GCC_ABBR: frozenset[str] = frozenset({"gcc", "gsc", "coe"})

# Generic staffing single-token keywords
_STAFFING_TOKENS: frozenset[str] = frozenset({
    "staffing", "recruitment", "recruiter", "manpower",
    "placement", "outsourcing", "consulting",
})

# ── Source-aware search-time enforcement ──────────────────────────────────────
# Each source agent already pushes job_type / work_mode into its OWN search URL,
# but with different coverage (see each agent's _WORK_MODE_MAP / _JOB_TYPE_MAP).
# A job from a source that ENFORCED the requested value at search time is
# trusted and NOT re-judged here — this stops free-text re-inference from
# flagging valid LinkedIn/Dice results (e.g. a genuine contract job) as failing.
# Naukri's gaps — Onsite work-mode and Freelance/Full-time job-type, none of
# which Naukri's URL can express — stay judged post-scrape.
_WM_ENFORCED: dict[str, frozenset[str]] = {
    "Remote": frozenset({"LinkedIn", "Dice", "Naukri"}),
    "Hybrid": frozenset({"LinkedIn", "Dice", "Naukri"}),
    "Onsite": frozenset({"LinkedIn", "Dice"}),          # Naukri omits Onsite
}
_JT_ENFORCED: dict[str, frozenset[str]] = {
    "Contract":  frozenset({"LinkedIn", "Dice", "Naukri"}),
    "Permanent": frozenset({"LinkedIn", "Dice", "Naukri"}),
    "Part-time": frozenset({"LinkedIn", "Dice", "Naukri"}),
    "Full-time": frozenset({"LinkedIn", "Dice"}),        # Naukri has no such param
    "Freelance": frozenset({"LinkedIn", "Dice"}),        # Naukri has no such param
}


def _tok(s: str) -> str:
    return (s or "").lower().strip()


def _tokens(s: str) -> set[str]:
    return set(re.split(r"[\W_]+", _tok(s))) - {""}


# ── Ambiguous company tracker ──────────────────────────────────────────────────

_AMBIGUOUS_FILE = _MASTER_DIR / "ambiguous_companies.json"


def _append_ambiguous(company: str) -> None:
    """Add a company to ambiguous_companies.json (deduplicated, best-effort)."""
    try:
        _MASTER_DIR.mkdir(parents=True, exist_ok=True)
        existing: list[str] = []
        if _AMBIGUOUS_FILE.exists():
            existing = json.loads(_AMBIGUOUS_FILE.read_text(encoding="utf-8"))
        company = company.strip()
        if company and company not in existing:
            existing.append(company)
            _AMBIGUOUS_FILE.write_text(
                json.dumps(sorted(existing), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
    except Exception as exc:
        logger.debug("ambiguous_append_failed", error=str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# BusinessFilterService
# ══════════════════════════════════════════════════════════════════════════════

class BusinessFilterService:
    """
    Annotate and filter a list of UnifiedJob instances according to
    the active FiltersConfig rules.
    """

    # ── Public API ─────────────────────────────────────────────────────────────

    def classify_all(self, jobs: list[UnifiedJob], cfg: FiltersConfig) -> list[UnifiedJob]:
        """Annotate every job with domain / hiring_entity / is_gcc / job_type."""
        for j in jobs:
            if not j.domain or j.domain == "Any":
                j.domain = self._infer_domain(j)
            if not j.hiring_entity or j.hiring_entity == "Any":
                j.is_gcc, j.hiring_entity = self._infer_hiring_entity(j)
            j.job_type = self._infer_job_type(j)
        return jobs

    def apply_all(self, jobs: list[UnifiedJob], cfg: FiltersConfig) -> list[UnifiedJob]:
        """Annotate every job with pass/fail against the active rules — WITHOUT
        removing any. Each job is tagged `passed_filter` + `filter_reason` (the
        first criterion it fails, with the offending value) and ALL jobs are
        returned so nothing is lost: they are persisted and shown, and the UI
        filters client-side on these fields. Deduplication is kept — it removes
        true duplicate postings, not business-qualified data."""
        before = len(jobs)
        jobs   = _deduplicate(jobs)

        # (stage name, per-job predicate, reason builder) — evaluated in order;
        # the first failing stage owns the reason (matches the old sequential
        # remove order, so per-stage counts are unchanged in meaning).
        stages: list[tuple[str, Any, Any]] = [
            ("work_mode",     self._passes_work_mode,
             lambda j: f"work_mode: got '{j.work_mode}', wanted '{cfg.work_mode}'"),
            ("job_type",      self._passes_job_type,
             lambda j: f"job_type: got '{j.job_type}', wanted '{cfg.job_type}'"),
            ("domain",        self._passes_domain,
             lambda j: f"domain: got '{j.domain}', wanted '{cfg.domain}'"),
            ("gcc",           self._passes_gcc,
             lambda j: f"gcc_mode: is_gcc={j.is_gcc}, mode '{cfg.gcc_mode}'"),
            ("hiring_entity", self._passes_hiring_entity,
             lambda j: f"hiring_entity: got '{j.hiring_entity}', wanted '{cfg.hiring_entity}'"),
            ("salary",        self._passes_salary,
             lambda j: f"salary: '{j.salary}' outside "
                       f"[{cfg.salary_min}, {cfg.salary_max}] {cfg.salary_currency}"),
        ]

        fail_counts: dict[str, int] = {name: 0 for name, _, _ in stages}
        passed_count = 0
        for j in jobs:
            j.passed_filter = True
            j.filter_reason = ""
            for name, predicate, reason_fn in stages:
                if not predicate(j, cfg):
                    j.passed_filter = False
                    j.filter_reason = reason_fn(j)
                    fail_counts[name] += 1
                    break
            if j.passed_filter:
                passed_count += 1

        for name, _, _ in stages:
            logger.info("filter_stage", stage=name, flagged=fail_counts[name])
        logger.info(
            "business_filter_complete",
            before=before, after=len(jobs),
            passed=passed_count, flagged=len(jobs) - passed_count,
        )
        return jobs

    # ── Domain ─────────────────────────────────────────────────────────────────
    # Classification + match logic live in app.core.domain_keywords (shared with
    # LinkedInAgent's in-scrape early-stop) so the two can't drift.

    def _infer_domain(self, j: UnifiedJob) -> str:
        return _infer_domain_shared(j.job_title, j.job_description, j.skills, j.domain_hint)

    def _passes_domain(self, j: UnifiedJob, cfg: FiltersConfig) -> bool:
        return _domain_matches(j.domain, cfg.domain)

    # ── Hiring entity ──────────────────────────────────────────────────────────

    def _infer_hiring_entity(self, j: UnifiedJob) -> tuple[bool, str]:
        """Returns (is_gcc, hiring_entity_label)."""
        c_lower  = _tok(j.company)
        d_lower  = _tok(j.job_description)
        c_tokens = _tokens(j.company)

        # ── GCC: well-known brand match ───────────────────────────────────────
        for known in _KNOWN_GCC:
            if known in c_lower:
                return True, "GCC"

        # ── GCC: phrase match in company name or description ──────────────────
        for phrase in _GCC_PHRASES:
            if phrase in c_lower or phrase in d_lower:
                return True, "GCC"

        # ── GCC: abbreviation token match ─────────────────────────────────────
        if c_tokens & _GCC_ABBR:
            return True, "GCC"

        # ── Staffing: well-known firm match ───────────────────────────────────
        for known in _KNOWN_STAFFING:
            if known in c_lower:
                return False, "Staffing Firm"

        # ── Staffing: generic keyword phrases ─────────────────────────────────
        for kw in _STAFFING_KW:
            if kw in c_lower:
                return False, "Staffing Firm"

        # ── Staffing: single-token match ──────────────────────────────────────
        if c_tokens & _STAFFING_TOKENS:
            return False, "Staffing Firm"

        # ── Direct Client: known brand match ──────────────────────────────────
        for known in _KNOWN_DIRECT:
            if known in c_lower:
                return False, "Direct Client"

        # ── Ambiguous: not in any list ────────────────────────────────────────
        _append_ambiguous(j.company)
        return False, "Ambiguous"

    def _passes_hiring_entity(self, j: UnifiedJob, cfg: FiltersConfig) -> bool:
        if cfg.hiring_entity == "Any":
            return True
        return j.hiring_entity == cfg.hiring_entity

    # ── GCC mode ───────────────────────────────────────────────────────────────

    def _passes_gcc(self, j: UnifiedJob, cfg: FiltersConfig) -> bool:
        if cfg.gcc_mode == "include_gcc":
            return True
        if cfg.gcc_mode == "gcc_only":
            return j.is_gcc
        return not j.is_gcc   # exclude_gcc

    # ── Work mode ──────────────────────────────────────────────────────────────

    def _passes_work_mode(self, j: UnifiedJob, cfg: FiltersConfig) -> bool:
        if cfg.work_mode == "Any":
            return True
        # Trust the source's own search-time f_WT/wfhType filter (see
        # _WM_ENFORCED) rather than re-judging inferred work_mode text.
        if j.source in _WM_ENFORCED.get(cfg.work_mode, frozenset()):
            return True
        return j.work_mode.lower() == cfg.work_mode.lower() or j.work_mode == "not_specified"

    # ── Job type ───────────────────────────────────────────────────────────────

    _JT_KEYWORDS: dict[str, list[str]] = {
        "contract":   ["contract", "c2c", "1099", "temp", "contractor", "temporary"],
        "freelance":  ["freelance", "independent", "self-employed", "gig"],
        "part-time":  ["part-time", "part time", "parttime"],
        "permanent":  ["permanent", "regular", "full-time", "fulltime", "full time"],
        "full-time":  ["permanent", "regular", "full-time", "fulltime", "full time"],
    }

    # "Permanent" (Naukri's term) and "Full-time" (LinkedIn's term) are the
    # same employment type under two different platform vocabularies.
    _JT_ALIASES: dict[str, str] = {"full-time": "permanent"}

    def _infer_job_type(self, j: UnifiedJob) -> str:
        # j.job_type may already hold a native platform label (e.g. LinkedIn's
        # "Full-time"/"Contract", scraped verbatim from the job page) — that
        # authoritative hint is checked before falling back to scanning the
        # free-text title/description.
        text = f"{j.job_type} {j.job_title} {j.job_description}".lower()
        for job_type, keywords in self._JT_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                return job_type
        return "not_specified"

    def _passes_job_type(self, j: UnifiedJob, cfg: FiltersConfig) -> bool:
        if cfg.job_type == "Any":
            return True
        # Trust the source's own search-time f_JT/jobType filter (see
        # _JT_ENFORCED) rather than re-judging inferred job_type text.
        if j.source in _JT_ENFORCED.get(cfg.job_type, frozenset()):
            return True
        target = self._JT_ALIASES.get(cfg.job_type.lower(), cfg.job_type.lower())
        jt     = self._JT_ALIASES.get(j.job_type.lower(), j.job_type.lower())
        return jt == target or j.job_type == "not_specified"

    # ── Salary ─────────────────────────────────────────────────────────────────

    def _passes_salary(self, j: UnifiedJob, cfg: FiltersConfig) -> bool:
        if not cfg.salary_min and not cfg.salary_max:
            return True
        parsed = _parse_salary_lpa(j.salary)
        if parsed is None:
            return cfg.include_undisclosed_salary
        mid_lpa = (parsed[0] + parsed[1]) / 2
        min_lpa = (cfg.salary_min / 100_000) if cfg.salary_min else None
        max_lpa = (cfg.salary_max / 100_000) if cfg.salary_max else None
        if min_lpa and mid_lpa < min_lpa:
            return False
        if max_lpa and mid_lpa > max_lpa:
            return False
        return True


# ── Deduplication ─────────────────────────────────────────────────────────────

def _deduplicate(jobs: list[UnifiedJob]) -> list[UnifiedJob]:
    """
    Remove duplicate jobs using three signals (all three checked):
    1. Exact job_url match
    2. Numeric job ID extracted from URL
    3. Normalised company + title combination
    """
    seen_urls: set[str]       = set()
    seen_ids:  set[str]       = set()
    seen_ct:   set[str]       = set()
    result:    list[UnifiedJob] = []

    _id_re = re.compile(r"[/-](\d{7,})")

    for j in jobs:
        url = (j.job_url or "").split("?")[0].rstrip("/").lower()
        jid = m.group(1) if (m := _id_re.search(url)) else ""
        ct  = re.sub(r"\s+", " ", f"{j.company} {j.job_title}".lower().strip())

        if url and url in seen_urls:
            continue
        if jid and jid in seen_ids:
            continue
        if ct and ct in seen_ct:
            continue

        if url:
            seen_urls.add(url)
        if jid:
            seen_ids.add(jid)
        if ct:
            seen_ct.add(ct)
        result.append(j)

    removed = len(jobs) - len(result)
    if removed:
        logger.info("deduplication_complete", removed=removed, kept=len(result))
    return result


# ── Salary parser ──────────────────────────────────────────────────────────────

_LPA_RANGE_RE  = re.compile(r"([\d.]+)\s*[-–to]+\s*([\d.]+)\s*(?:lpa|lacs?|lakhs?|l\b)", re.I)
_LPA_SINGLE_RE = re.compile(r"([\d.]+)\s*(?:lpa|lacs?|lakhs?|l\b)", re.I)
_NOT_DISCLOSED = {"not disclosed", "not specified", "n/a", "na", ""}


def _parse_salary_lpa(raw: str) -> tuple[float, float] | None:
    if not raw or raw.strip().lower() in _NOT_DISCLOSED:
        return None
    m = _LPA_RANGE_RE.search(raw)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = _LPA_SINGLE_RE.search(raw)
    if m:
        v = float(m.group(1))
        return v, v
    return None
