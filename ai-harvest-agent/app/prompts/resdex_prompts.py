"""Prompts + grounding for Resdex candidate matching
(app/services/resdex_match_service.py).

Two narrowly-scoped LLM calls, both through LLMService.extract_json():

  * requirements — the JD (title, skills, experience, location, full text) →
    every requirement it states (mandatory/optional skills with spelling variants,
    equivalent titles, experience, locations, notice, education, domain,
    exclusions). ground_requirements() then keeps only what the job actually
    says; fallback_requirements() builds the same shape with no LLM.
  * profile extraction — one Resdex result card's text → structured candidate
    fields. ground_profile() keeps a value only when it is written in the card.

The model never writes the Boolean query — app/services/resdex_query.py
assembles it deterministically from the grounded requirements.
"""
from __future__ import annotations

import re
from typing import Any

from app.services.resdex_query import MAX_EXP_YEARS, TextMatcher, spelling_key, term_key
from app.services.resdex_types import CANDIDATE_FIELDS

# Bounds on what the requirement extraction may contribute to a query.
MAX_MANDATORY = 8
MAX_OPTIONAL = 10
MAX_VARIANTS = 4
MAX_EXTRA_TITLES = 4
MAX_LOCATIONS = 5
MAX_EXCLUDES = 5
JD_MAX_CHARS = 12_000
CARD_MAX_CHARS = 6_000


# ── Requirements ──────────────────────────────────────────────────────────────

RESDEX_REQUIREMENTS_SYSTEM = """You read a job description and list EVERY candidate requirement it states, so a recruiter can search a resume database for matching candidates.

Rules:
- Use only what the job text states. Never invent a skill, title, location or number that is not written in the title, skills list or description.
- mandatory_skills: the technical skills / tools / technologies the role REQUIRES ("must have", "required", core stack in the title or responsibilities). Use the name exactly as written in the text. Give each a priority (1 = most important; the skill in the job title or listed first is usually 1). Add up to 4 common spelling variants or abbreviations of that SAME skill as variants (e.g. "Spring Boot" → ["SpringBoot"], "Kubernetes" → ["K8s"], "JavaScript" → ["JS"]). Variants must be the same skill, never a different or related technology.
- optional_skills: skills described as nice-to-have / preferred / plus / added advantage, with variants the same way.
- job_titles: the posted job title first, then up to 4 equivalent designations a matching candidate might hold (e.g. "Senior Java Developer" → "Senior Software Engineer", "Java Lead").
- min_exp / max_exp: years of experience as integers when stated (e.g. "5-8 years" → 5 and 8, "5+ years" → 5 and null). null when not stated.
- locations: job / work locations written in the text (city names). remote_ok: true only when the text says remote / work from home is allowed.
- notice_period_max_days: the maximum notice period in days when stated ("immediate joiners" → 0, "30 days" → 30), else null.
- education: the stated degree requirement copied as written (e.g. "B.Tech / B.E."), else null.
- domain: the industry / business domain if stated (e.g. "Banking", "Healthcare"), else null.
- exclude_terms: up to 5 short terms whose profiles clearly do NOT fit (e.g. "Fresher", "Intern" for a senior role). Empty list when nothing clearly applies.

Return ONLY a JSON object with exactly the keys of the schema. No markdown, no explanation."""

RESDEX_REQUIREMENTS_SCHEMA = (
    '{"mandatory_skills": [{"name": str, "variants": [str], "priority": int}], '
    '"optional_skills": [{"name": str, "variants": [str]}], '
    '"job_titles": [str], "min_exp": int|null, "max_exp": int|null, '
    '"locations": [str], "remote_ok": bool, "notice_period_max_days": int|null, '
    '"education": str|null, "domain": str|null, "exclude_terms": [str]}'
)


def _skills_text(skills: Any) -> str:
    if isinstance(skills, (list, tuple)):
        return ", ".join(str(s).strip() for s in skills if str(s or "").strip())
    return str(skills or "").strip()


def build_requirements_content(job: dict) -> str:
    """The extract_json content for one job — header fields, then the full JD
    (bounded: keeps the head, which carries the requirement sections)."""
    jd = (job.get("job_description") or "").strip()
    if len(jd) > JD_MAX_CHARS:
        jd = jd[:JD_MAX_CHARS] + " …"
    return (
        f"Job title: {(job.get('job_title') or '').strip() or 'unknown'}\n"
        f"Skills listed on the posting: {_skills_text(job.get('skills')) or 'none'}\n"
        f"Experience: {(job.get('experience') or '').strip() or 'not stated'}\n"
        f"Location: {(job.get('location') or '').strip() or 'not stated'}\n\n"
        f"Job description:\n{jd}"
    )


def _job_matcher(job: dict) -> TextMatcher:
    return TextMatcher(
        job.get("job_title") or "", job.get("skills") or [],
        job.get("job_description") or "", job.get("location") or "",
    )


def _str(v: Any, max_len: int = 120) -> str:
    if not isinstance(v, (str, int, float)) or isinstance(v, bool):
        return ""
    s = " ".join(str(v).split())
    return s if len(s) <= max_len else ""


def _str_list(v: Any) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [s for s in (_str(x) for x in v) if s]


def _int_or_none(v: Any, lo: int, hi: int) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def _ground_skills(raw: Any, matcher: TextMatcher, seen: set[str], limit: int,
                   with_priority: bool) -> list[dict]:
    """Keep skills whose canonical name is in the job text; variants survive only as
    alternatives of a kept skill. ``seen`` de-dups across mandatory/optional."""
    items = raw if isinstance(raw, list) else []
    kept: list[tuple[int, int, dict]] = []
    for idx, item in enumerate(items):
        if isinstance(item, dict):
            name, variants = _str(item.get("name")), item.get("variants")
            prio = item.get("priority")
        else:
            name, variants, prio = _str(item), [], None
        key = term_key(name)
        if not name or not key or key in seen or not matcher.contains(name):
            continue
        seen.add(key)
        var_out: list[str] = []
        var_keys = {spelling_key(name)}      # distinct spellings are distinct keywords
        for v in _str_list(variants):
            vk = spelling_key(v)
            if term_key(v) and vk not in var_keys and len(v) <= 40:
                var_keys.add(vk)
                var_out.append(v)
            if len(var_out) >= MAX_VARIANTS:
                break
        p = _int_or_none(prio, 1, 1000)
        kept.append((p if p is not None else 1000 + idx, idx, {"name": name, "variants": var_out}))
    if with_priority:
        kept.sort(key=lambda x: (x[0], x[1]))
    out = [d for _, _, d in kept[:limit]]
    if with_priority:
        for rank, d in enumerate(out, start=1):
            d["priority"] = rank
    return out


def _ground_exp(req: dict) -> tuple[int | None, int | None]:
    lo = _int_or_none(req.get("min_exp"), 0, MAX_EXP_YEARS)
    hi = _int_or_none(req.get("max_exp"), 0, MAX_EXP_YEARS)
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return lo, hi


def ground_requirements(req: dict, job: dict) -> dict:
    """Keep only requirements the job actually states.

    Canonical skill names, the locations and (when the job has no title) the
    titles must appear — case-insensitively, space/punctuation tolerant — in the
    job's title / skills / description / location; anything else is dropped. The
    posted title is always ``job_titles[0]``; other titles and skill variants are
    kept only as OR-alternatives of a grounded term (de-duplicated, ≤4 each).
    Experience is clamped to 0..40 with min ≤ max; exclusions must be short."""
    req = req if isinstance(req, dict) else {}
    job = job or {}
    matcher = _job_matcher(job)
    seen: set[str] = set()
    mandatory = _ground_skills(req.get("mandatory_skills"), matcher, seen, MAX_MANDATORY, True)
    optional = _ground_skills(req.get("optional_skills"), matcher, seen, MAX_OPTIONAL, False)

    posted = _str(job.get("job_title"), 200)
    titles: list[str] = []
    title_keys: set[str] = set()
    if posted:
        titles.append(posted)
        title_keys.add(term_key(posted))
    for t in _str_list(req.get("job_titles")):
        k = term_key(t)
        if not k or k in title_keys or len(t.split()) > 8:
            continue
        if not posted and not matcher.contains(t):
            continue                       # no posted title to be an alternative of
        title_keys.add(k)
        titles.append(t)
        if len(titles) >= 1 + MAX_EXTRA_TITLES:
            break

    locations: list[str] = []
    loc_keys: set[str] = set()
    for loc in _str_list(req.get("locations")):
        k = term_key(loc)
        if k and k not in loc_keys and matcher.contains(loc):
            loc_keys.add(k)
            locations.append(loc)
        if len(locations) >= MAX_LOCATIONS:
            break

    excludes: list[str] = []
    ex_keys: set[str] = set()
    for term in _str_list(req.get("exclude_terms")):
        k = term_key(term)
        if k and k not in ex_keys and len(term.split()) <= 3 and k not in seen:
            ex_keys.add(k)
            excludes.append(term)
        if len(excludes) >= MAX_EXCLUDES:
            break

    education = _str(req.get("education"), 200)
    domain = _str(req.get("domain"), 120)
    min_exp, max_exp = _ground_exp(req)
    return {
        "mandatory_skills": mandatory,
        "optional_skills": optional,
        "job_titles": titles,
        "min_exp": min_exp,
        "max_exp": max_exp,
        "locations": locations,
        "remote_ok": req.get("remote_ok") is True,
        "notice_period_max_days": _int_or_none(req.get("notice_period_max_days"), 0, 365),
        "education": education if education and matcher.contains(education) else None,
        "domain": domain if domain and matcher.contains(domain) else None,
        "exclude_terms": excludes,
    }


_EXP_RANGE_RE = re.compile(r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})")
_EXP_MIN_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:yrs?|years?)", re.I)


def parse_experience_range(text: str | None) -> tuple[int | None, int | None]:
    """"5-10 Yrs" → (5, 10); "5+ years" → (5, None); anything else → (None, None)."""
    t = text or ""
    m = _EXP_RANGE_RE.search(t)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (min(lo, hi), max(lo, hi)) if max(lo, hi) <= MAX_EXP_YEARS else (None, None)
    m = _EXP_MIN_RE.search(t)
    if m and int(m.group(1)) <= MAX_EXP_YEARS:
        return int(m.group(1)), None
    return None, None


def fallback_requirements(job: dict) -> dict:
    """No-LLM requirements from the posting's own fields: the first 4 listed skills
    are mandatory (priority by order), the next 4 optional, the posted title, the
    experience range when the posting states one, and the location's city."""
    job = job or {}
    raw_skills = job.get("skills") or []
    if isinstance(raw_skills, str):
        raw_skills = raw_skills.split(",")
    skills: list[str] = []
    keys: set[str] = set()
    for s in raw_skills:
        name = _str(s)
        k = term_key(name)
        if name and k and k not in keys:
            keys.add(k)
            skills.append(name)
    title = _str(job.get("job_title"), 200)
    min_exp, max_exp = parse_experience_range(job.get("experience"))
    city = (job.get("location") or "").split(",")[0].strip()
    return {
        "mandatory_skills": [
            {"name": n, "variants": [], "priority": i} for i, n in enumerate(skills[:4], start=1)
        ],
        "optional_skills": [{"name": n, "variants": []} for n in skills[4:8]],
        "job_titles": [title] if title else [],
        "min_exp": min_exp,
        "max_exp": max_exp,
        "locations": [city] if city else [],
        "remote_ok": False,
        "notice_period_max_days": None,
        "education": None,
        "domain": None,
        "exclude_terms": [],
    }


# ── Profile extraction ────────────────────────────────────────────────────────

RESDEX_PROFILE_EXTRACT_SYSTEM = """You extract a candidate's details from the text of ONE resume-database search result card.

Rules:
- Copy every value exactly as it is written in the card text. Never guess, infer, complete, translate or reformat a value. If something is not written, use null (or an empty list).
- candidate_name: the candidate's name as shown.
- headline: the profile headline / summary line as shown.
- current_designation / current_company: the current role and employer as shown.
- previous_roles: earlier roles as shown, one string each (e.g. "Java Developer at Infosys").
- experience_text: total experience exactly as shown (e.g. "7y 3m", "7 Years").
- location: current location; preferred_locations: preferred locations as shown.
- skills: key skills as shown, one per item.
- education, notice_period, ctc, last_active: exactly as shown
  (e.g. "30 Days", "12 Lacs", "Active 2d ago").

Return ONLY a JSON object with exactly the keys of the schema. No markdown, no explanation."""

RESDEX_PROFILE_EXTRACT_SCHEMA = (
    '{"candidate_name": str|null, "headline": str|null, "current_designation": str|null, '
    '"current_company": str|null, "previous_roles": [str], "experience_text": str|null, '
    '"location": str|null, "preferred_locations": [str], "skills": [str], '
    '"education": str|null, "notice_period": str|null, "ctc": str|null, '
    '"last_active": str|null}'
)

_PROFILE_LIST_FIELDS = ("previous_roles", "preferred_locations", "skills")
_PROFILE_STR_FIELDS = tuple(
    f for f in CANDIDATE_FIELDS
    if f not in _PROFILE_LIST_FIELDS and f not in ("experience_years", "profile_url")
)


def build_profile_content(card_text: str) -> str:
    text = (card_text or "").strip()
    if len(text) > CARD_MAX_CHARS:
        text = text[:CARD_MAX_CHARS]
    return f"Search result card text:\n{text}"


_YM_RE = re.compile(
    r"(\d{1,2}(?:\.\d{1,2})?)\s*(?:y|yr|yrs|year|years)\b"
    r"(?:\s*(\d{1,2})\s*(?:m|mo|mos|month|months)\b)?",
    re.I,
)


def parse_experience_years(text: str | None) -> float | None:
    """"7y 3m" → 7.25, "7 Years" → 7.0, "7.5 yrs" → 7.5; None when not parseable."""
    m = _YM_RE.search(text or "")
    if not m:
        return None
    years = float(m.group(1))
    if m.group(2):
        years += min(int(m.group(2)), 11) / 12
    years = round(years, 2)
    return years if 0 <= years <= 60 else None


def _ground_value(v: Any, matcher: TextMatcher, max_len: int = 500) -> str:
    s = _str(v, max_len)
    return s if s and matcher.contains(s) else ""


def ground_profile(fields: dict, card_text: str) -> dict:
    """Keep each extracted value only when it is written in ``card_text``
    (normalized match); list items individually. ``experience_years`` is parsed
    from the kept ``experience_text`` — never taken from the model. Returns every
    CANDIDATE_FIELDS key except ``profile_url`` ("" / [] / None when absent)."""
    fields = fields if isinstance(fields, dict) else {}
    matcher = TextMatcher(card_text or "")
    out: dict[str, Any] = {}
    for f in _PROFILE_STR_FIELDS:
        out[f] = _ground_value(fields.get(f), matcher)
    for f in _PROFILE_LIST_FIELDS:
        items: list[str] = []
        keys: set[str] = set()
        raw = fields.get(f)
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict):           # e.g. {"designation", "company"}
                parts = [_ground_value(x, matcher, 200) for x in item.values()]
                value = " at ".join(p for p in parts if p)
            else:
                value = _ground_value(item, matcher, 200)
            k = term_key(value)
            if value and k not in keys:
                keys.add(k)
                items.append(value)
        out[f] = items
    out["experience_years"] = parse_experience_years(out.get("experience_text"))
    return out
