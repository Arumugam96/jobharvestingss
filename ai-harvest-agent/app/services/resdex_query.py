"""Deterministic Resdex query construction + candidate skill coverage (pure).

The LLM only extracts *requirements* from a JD (app/prompts/resdex_prompts.py);
everything Resdex actually receives is assembled here from that structure, so the
Boolean syntax (quoting, parentheses, AND/OR/NOT) is always valid no matter what
the model wrote, and the same requirements always produce the same query.

Relaxation ladder (one Resdex search per tier, at most RESDEX_MAX_SEARCHES_PER_JOB):
  T1  every mandatory skill group ANDed, AND the title group, NOT the excludes
  T2  lowest-priority mandatory skill dropped (≥1 kept); titles + optional skills
      merged into ONE OR group that is ANDed in
  T3  same query as T2 — the *filters* relax instead (experience ±2 yrs, no
      location filter)

Term matching (grounding, coverage) is token based and space/punctuation
tolerant: "Spring Boot" matches "SpringBoot" / "spring-boot", but "Java" never
matches "JavaScript" (a term must cover whole tokens).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from app.services.resdex_types import ResdexTier

# Resdex keyword-box limit (observed ~500 chars) — the builder trims to fit.
DEFAULT_MAX_QUERY_LEN = 500
# Ladder bounds.
MAX_TIERS = 3
T3_EXP_WIDEN_YEARS = 2
MAX_EXP_YEARS = 40

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9+#]+")


# ── Normalized term matching ──────────────────────────────────────────────────

def term_tokens(text: str | None) -> list[str]:
    """Lowercased alphanumeric tokens ("+" / "#" kept so C++ / C# survive)."""
    return [t for t in _TOKEN_SPLIT_RE.split((text or "").lower()) if t]


def term_key(text: str | None) -> str:
    """Space/punctuation-insensitive key — "Spring Boot", "spring-boot" and
    "SpringBoot" share one key. Used for matching and to de-duplicate *skills*.
    Search strings use spelling_key instead: Resdex treats "SpringBoot" and
    "Spring Boot" as different keywords, so both are worth OR-ing."""
    return "".join(term_tokens(text))


def spelling_key(text: str | None) -> str:
    """Case-insensitive, whitespace-collapsed key — de-duplicates search strings
    ("JAVA" ≡ "Java") while keeping distinct spellings ("SpringBoot" ≠ "Spring Boot")."""
    return " ".join(str(text or "").split()).casefold()


class TextMatcher:
    """Answers "does this term appear in the text?" tolerant of spacing and
    punctuation, but only on whole-token boundaries: the term's compact form must
    equal the concatenation of a contiguous run of the text's tokens."""

    def __init__(self, *texts: Any) -> None:
        parts: list[str] = []
        for t in texts:
            if isinstance(t, (list, tuple, set)):
                parts.extend(str(x) for x in t if x is not None)
            elif t is not None:
                parts.append(str(t))
        self._tokens = term_tokens(" \n ".join(parts))
        self._compact = "".join(self._tokens)

    def contains(self, term: str | None) -> bool:
        needle = term_key(term)
        if not needle or needle not in self._compact:
            return False
        toks = self._tokens
        for i in range(len(toks)):
            if not needle.startswith(toks[i]):
                continue
            acc = ""
            for j in range(i, len(toks)):
                acc += toks[j]
                if acc == needle:
                    return True
                if len(acc) >= len(needle) or not needle.startswith(acc):
                    break
        return False


# ── Boolean query ─────────────────────────────────────────────────────────────

def _clean_term(term: Any) -> str:
    """Strip quotes (they would break the quoted phrase) and collapse whitespace."""
    return " ".join(str(term or "").replace('"', " ").split())


def _dedup_terms(terms: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in terms:
        c = _clean_term(t)
        k = spelling_key(c)
        if c and term_key(c) and k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _or_group(terms: list[str]) -> str:
    return "(" + " OR ".join(f'"{t}"' for t in terms) + ")" if terms else ""


def _skill_terms(skill: Any) -> list[str]:
    """[canonical name, *variants] of one requirement skill entry."""
    if isinstance(skill, dict):
        variants = skill.get("variants") or []
        if not isinstance(variants, list):
            variants = []
        return _dedup_terms([skill.get("name"), *variants])
    return _dedup_terms([skill])


def _sorted_mandatory(req: dict) -> list[Any]:
    """Mandatory skills by ascending priority (1 = highest); stable for ties."""
    items = list(req.get("mandatory_skills") or [])

    def _prio(pair: tuple[int, Any]) -> tuple[int, int]:
        idx, s = pair
        p = s.get("priority") if isinstance(s, dict) else None
        return (p if isinstance(p, int) and not isinstance(p, bool) else 10_000 + idx, idx)

    return [s for _, s in sorted(enumerate(items), key=_prio)]


class _QueryParts:
    """Mutable query structure so the length trimmer can drop pieces in a fixed
    order and re-render; rendering is the only place syntax is produced."""

    def __init__(self, req: dict, tier: int) -> None:
        groups = [g for g in (_skill_terms(s) for s in _sorted_mandatory(req)) if g]
        self.merged = tier >= 2
        if self.merged and len(groups) > 1:
            groups = groups[:-1]                      # T2/T3: drop lowest-priority mandatory
        self.mandatory: list[list[str]] = groups
        self.titles: list[str] = _dedup_terms(req.get("job_titles") or [])
        optional: list[str] = []
        if self.merged:
            for s in req.get("optional_skills") or []:
                optional.extend(_skill_terms(s))
        taken = {spelling_key(t) for t in self.titles} | {
            spelling_key(t) for g in self.mandatory for t in g
        }
        self.optional: list[str] = [
            t for t in _dedup_terms(optional) if spelling_key(t) not in taken
        ]
        self.exclude: list[str] = _dedup_terms(req.get("exclude_terms") or [])

    def render(self) -> str:
        clauses = [_or_group(g) for g in self.mandatory]
        second = self.titles + self.optional if self.merged else self.titles
        if second:
            clauses.append(_or_group(second))
        q = " AND ".join(c for c in clauses if c)
        if q and self.exclude:
            q = f"{q} NOT {_or_group(self.exclude)}"
        return q

    # Trimming steps, in the order they are tried (each removes one piece).
    def _pop_optional(self) -> bool:
        if self.optional:
            self.optional.pop()
            return True
        return False

    def _pop_variant(self) -> bool:
        for g in reversed(self.mandatory):            # lowest priority first
            if len(g) > 1:
                g.pop()
                return True
        return False

    def _pop_extra_title(self) -> bool:
        if len(self.titles) > 1:
            self.titles.pop()
            return True
        return False

    def _pop_exclude(self) -> bool:
        if self.exclude:
            self.exclude.pop()
            return True
        return False

    def _pop_last_title(self) -> bool:
        if self.titles and self.mandatory:            # never leave the query empty
            self.titles.pop()
            return True
        return False

    def _pop_mandatory(self) -> bool:
        if len(self.mandatory) > 1:
            self.mandatory.pop()
            return True
        return False

    def trimmers(self) -> list[Callable[[], bool]]:
        return [
            self._pop_optional, self._pop_variant, self._pop_extra_title,
            self._pop_exclude, self._pop_last_title, self._pop_mandatory,
        ]

    def truncate_single(self, max_len: int) -> None:
        """Last resort: one over-long term left — cut it on a word boundary."""
        budget = max(1, max_len - 4)                   # ( " " )
        target = self.mandatory[0] if self.mandatory else self.titles
        if not target:
            return
        term = target[0][:budget]
        if " " in term and len(target[0]) > budget:
            term = term.rsplit(" ", 1)[0]
        target[:] = [term.strip() or target[0][:budget]]


def build_boolean_query(req: dict, tier: int, max_len: int = DEFAULT_MAX_QUERY_LEN) -> str:
    """Resdex Boolean query for ``req`` at ladder ``tier`` (1-3), e.g.
    ``("Java" OR "J2EE") AND ("Spring Boot") AND ("Senior Java Developer") NOT ("Fresher")``.

    Over ``max_len`` the lowest-priority pieces go first: optional skills, then
    skill variants (lowest-priority skill first), extra titles, exclusions, the
    title group, and finally lower-priority mandatory skills. Never empties a
    non-empty query (returns "" only when ``req`` has no skill and no title)."""
    parts = _QueryParts(req or {}, tier)
    query = parts.render()
    while len(query) > max_len:
        if not any(trim() for trim in parts.trimmers()):
            parts.truncate_single(max_len)
            query = parts.render()
            break
        query = parts.render()
    return query


# ── Filters ───────────────────────────────────────────────────────────────────

def _int_or_none(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


def build_filters(req: dict, tier: int, active_within_months: int) -> dict[str, Any]:
    """Resdex search filters derived from the same requirements:
    {"min_exp", "max_exp", "locations", "notice_period_max_days",
    "active_within_months"} — None / empty values omitted. T3 widens experience by
    ±2 years (floor 0) and drops the location filter. A remote-friendly role
    (``remote_ok``) gets no location filter at any tier."""
    req = req or {}
    min_exp = _int_or_none(req.get("min_exp"))
    max_exp = _int_or_none(req.get("max_exp"))
    locations = [
        loc for loc in (req.get("locations") or []) if isinstance(loc, str) and loc.strip()
    ]
    if tier >= 3:
        if min_exp is not None:
            min_exp = max(0, min_exp - T3_EXP_WIDEN_YEARS)
        if max_exp is not None:
            max_exp = min(MAX_EXP_YEARS + T3_EXP_WIDEN_YEARS, max_exp + T3_EXP_WIDEN_YEARS)
        locations = []
    if req.get("remote_ok") is True:
        locations = []
    out: dict[str, Any] = {
        "min_exp": min_exp,
        "max_exp": max_exp,
        "locations": locations or None,
        "notice_period_max_days": _int_or_none(req.get("notice_period_max_days")),
        "active_within_months": (
            int(active_within_months) if active_within_months and active_within_months > 0 else None
        ),
    }
    return {k: v for k, v in out.items() if v is not None}


def build_tiers(req: dict, max_tiers: int, active_within_months: int) -> list[ResdexTier]:
    """The relaxation ladder T1..T{min(3, max_tiers)}; a tier identical to the
    previous one (same query AND filters — e.g. a single mandatory skill and no
    location/experience to relax) is skipped so no search is wasted on it."""
    tiers: list[ResdexTier] = []
    for t in range(1, min(MAX_TIERS, max(1, int(max_tiers or 1))) + 1):
        query = build_boolean_query(req, t)
        if not query:
            continue
        filters = build_filters(req, t, active_within_months)
        if tiers and tiers[-1].query == query and tiers[-1].filters == filters:
            continue
        tiers.append(ResdexTier(tier=t, query=query, filters=filters))
    return tiers


# ── Coverage ──────────────────────────────────────────────────────────────────

def skill_coverage(candidate: dict, req: dict) -> tuple[float, list[str]]:
    """Share of the mandatory skill groups the candidate shows — a group counts when
    its canonical name or any variant appears (normalized) in the candidate's
    skills, headline or current designation. Returns (coverage 0..1, matched
    canonical names in priority order). No mandatory skills → (1.0, [])."""
    groups = [g for g in (_skill_terms(s) for s in _sorted_mandatory(req or {})) if g]
    if not groups:
        return 1.0, []
    cand = candidate or {}
    matcher = TextMatcher(
        cand.get("skills") or [], cand.get("headline") or "", cand.get("current_designation") or "",
    )
    matched = [g[0] for g in groups if any(matcher.contains(t) for t in g)]
    return len(matched) / len(groups), matched
