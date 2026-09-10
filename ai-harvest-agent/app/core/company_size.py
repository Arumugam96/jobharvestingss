"""Company-size band parsing + tier mapping.

LinkedIn shows a company's headcount as one of eight fixed employee-range bands
(e.g. "51-200 employees", "1,001-5,000 employees", "10,001+ employees"). This is
the same vocabulary already scraped in app/agents/prospect_intelligence_agent.py;
it is centralised here so the LinkedIn job harvest can capture a per-job size band
and so the API can expose a friendly Small/Medium/Large/Enterprise tier over it.

Nothing here filters or drops jobs — parsing/classification only. The band is
captured at harvest time (LLM extraction, regex fallback) and stored on the job;
the UI filters by tier purely as a display concern.
"""
from __future__ import annotations

import re

# The eight canonical LinkedIn employee-range bands as (low, high, token); a high
# of None marks the open-ended top band. The token is the exact BAND_TO_TIER key
# (defined below). Numbers read out of a free-form value are mapped to the nearest
# band by these bounds — which tolerates the comma-less / "to"-separated / unit-
# varied forms the LLM (and some LinkedIn renderings) emit, e.g.
# "1001-5000 employees", "1,000 to 5,000 members".
_TIER_BANDS: list[tuple[int, int | None, str]] = [
    (2, 10, "2-10"),
    (11, 50, "11-50"),
    (51, 200, "51-200"),
    (201, 500, "201-500"),
    (501, 1000, "501-1,000"),
    (1001, 5000, "1,001-5,000"),
    (5001, 10000, "5,001-10,000"),
    (10001, None, "10,001+"),
]

_NUM = r"\d[\d,]*"
_UNIT = r"(?:employees?|members?|people|staff|headcount|emp)"
# A headcount range ("N-M" / "N to M") or an open band ("N+"). Comma-optional.
_RANGE_RE = re.compile(rf"({_NUM})\s*(?:[–\-]|to)\s*({_NUM})|({_NUM})\s*\+", re.IGNORECASE)
# Same, but anchored to a size unit — safe to run over noisy text (job insights),
# where a salary/experience range must NOT be misread as a headcount band.
_SIZE_RE = re.compile(
    rf"(?:({_NUM})\s*(?:[–\-]|to)\s*({_NUM})|({_NUM})\s*\+)\s*{_UNIT}", re.IGNORECASE
)


def _to_int(raw: str) -> int:
    return int(raw.replace(",", ""))


def _band_from_numbers(low: int, high: int | None) -> str:
    """Map a headcount (low, high) to the nearest canonical band token. high=None
    is an open-ended "N+". Returns "" only for input below the smallest band."""
    if high is None:
        if low >= 10001:
            return "10,001+"
        for blo, bhi, tok in _TIER_BANDS:
            if bhi is not None and blo <= low <= bhi:
                return tok
        return ""
    if low >= 10001 or high >= 10001:
        return "10,001+"
    best, best_dist = "", None
    for blo, bhi, tok in _TIER_BANDS:
        if bhi is None:
            continue
        dist = abs(blo - low) + abs(bhi - high)
        if best_dist is None or dist < best_dist:
            best, best_dist = tok, dist
    return best


def _match_band(regex: re.Pattern, text: str) -> str:
    """Find the first range / open-band in ``text`` via ``regex`` and map it to a
    canonical band token, or "" when none is present."""
    m = regex.search(text or "")
    if not m:
        return ""
    if m.group(3) is not None:  # matched the "N+" alternative
        return _band_from_numbers(_to_int(m.group(3)), None)
    return _band_from_numbers(_to_int(m.group(1)), _to_int(m.group(2)))

# Canonical band tokens (hyphen separator, no "employees" suffix) → their friendly
# tier. Grouping confirmed with the user:
#   Small      = 2-10, 11-50, 51-200
#   Medium     = 201-500, 501-1,000
#   Large      = 1,001-5,000, 5,001-10,000
#   Enterprise = 10,001+
BAND_TO_TIER: dict[str, str] = {
    "2-10": "Small",
    "11-50": "Small",
    "51-200": "Small",
    "201-500": "Medium",
    "501-1,000": "Medium",
    "1,001-5,000": "Large",
    "5,001-10,000": "Large",
    "10,001+": "Enterprise",
}

# Reverse view (tier → its bands) for documentation / UI helper text.
TIER_TO_BANDS: dict[str, list[str]] = {
    "Small": ["2-10", "11-50", "51-200"],
    "Medium": ["201-500", "501-1,000"],
    "Large": ["1,001-5,000", "5,001-10,000"],
    "Enterprise": ["10,001+"],
}

TIERS = ("Small", "Medium", "Large", "Enterprise")


def parse_company_size_band(text: str) -> str:
    """Return the first employee-range band found in free text (e.g. LinkedIn's
    "About the company" / job-insights block), formatted as "<band> employees"
    (matching the convention already stored on RecruiterORM.company_size), or ""
    when none is present. Anchored to a size unit so a salary/experience range
    isn't misread as a headcount. Never guesses — extraction only."""
    band = _match_band(_SIZE_RE, text)
    return f"{band} employees" if band else ""


def normalize_company_size(value: str) -> str:
    """Normalise a company-size value to its canonical band token, tolerating a
    missing thousands comma, an en-dash / hyphen / "to" separator, and any unit
    suffix — e.g. "1,001–5,000 employees", "1001-5000 employees", and
    "1,000 to 5,000 members" all → "1,001-5,000". Returns "" when the value carries
    no recognisable range."""
    return _match_band(_RANGE_RE, value)


def band_to_tier(value: str) -> str:
    """Map a band string (any accepted format) to Small/Medium/Large/Enterprise,
    or "" when it can't be recognised (⇒ the UI shows "Unknown")."""
    return BAND_TO_TIER.get(normalize_company_size(value), "")
