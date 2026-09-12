"""Free-text job-location → (city, state, country) parsing.

Job boards emit a single free-text location string per job — e.g.
``"Chennai, Tamil Nadu, India; On-site"``, ``"Bengaluru, Karnataka, India"``,
``"Singapore"``, ``"London, England, United Kingdom"``, or ``"Remote"``. The UI
still shows that raw string as-is; this module derives a normalized **country**
(and best-effort state/city) purely for a display-time country filter/facet —
the same "parse/classify only, never drop a job" role app/core/company_size.py
plays for the size band.

Extraction only — the country is set ONLY when the trailing comma-token matches
the known-country vocabulary (or a US state, which implies United States). An
unrecognised location yields ``country=""`` rather than a guess, so the Country
dropdown stays clean. Extend ``_COUNTRY_ALIASES`` / ``_US_STATES`` as new
locations appear (same philosophy as the company-size band list).
"""
from __future__ import annotations

import re

# Canonical country name → set of lower-cased aliases (including the canonical
# name itself, lower-cased). Seeded from app/core/linkedin_geo.py::_GEO_IDS and
# broadened to the countries that show up across LinkedIn/Naukri/Dice data.
_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "India":                ("india", "bharat"),
    "United States":        ("united states", "united states of america", "usa",
                             "u.s.a", "u.s.a.", "u.s.", "us", "america"),
    "United Kingdom":       ("united kingdom", "uk", "u.k.", "great britain",
                             "britain", "england", "scotland", "wales",
                             "northern ireland"),
    "United Arab Emirates": ("united arab emirates", "uae", "u.a.e", "u.a.e."),
    "Canada":               ("canada",),
    "Australia":            ("australia",),
    "Germany":              ("germany", "deutschland"),
    "Singapore":            ("singapore",),
    "Ireland":              ("ireland",),
    "France":               ("france",),
    "Netherlands":          ("netherlands", "the netherlands", "holland"),
    "Spain":                ("spain",),
    "Italy":                ("italy",),
    "Poland":               ("poland",),
    "Portugal":             ("portugal",),
    "Switzerland":          ("switzerland",),
    "Sweden":               ("sweden",),
    "Norway":               ("norway",),
    "Denmark":              ("denmark",),
    "Finland":              ("finland",),
    "Belgium":              ("belgium",),
    "Austria":              ("austria",),
    "Romania":              ("romania",),
    "Czech Republic":       ("czech republic", "czechia"),
    "Hungary":              ("hungary",),
    "Japan":                ("japan",),
    "China":                ("china",),
    "Hong Kong":            ("hong kong", "hong kong sar"),
    "Taiwan":               ("taiwan",),
    "South Korea":          ("south korea", "korea, republic of", "republic of korea"),
    "Malaysia":             ("malaysia",),
    "Indonesia":            ("indonesia",),
    "Philippines":          ("philippines", "the philippines"),
    "Thailand":             ("thailand",),
    "Vietnam":              ("vietnam", "viet nam"),
    "Pakistan":             ("pakistan",),
    "Bangladesh":           ("bangladesh",),
    "Sri Lanka":            ("sri lanka",),
    "Nepal":                ("nepal",),
    "Saudi Arabia":         ("saudi arabia", "ksa"),
    "Qatar":                ("qatar",),
    "Kuwait":               ("kuwait",),
    "Bahrain":              ("bahrain",),
    "Oman":                 ("oman",),
    "Israel":               ("israel",),
    "Turkey":               ("turkey", "türkiye", "turkiye"),
    "Egypt":                ("egypt",),
    "South Africa":         ("south africa",),
    "Nigeria":              ("nigeria",),
    "Kenya":                ("kenya",),
    "Brazil":               ("brazil", "brasil"),
    "Mexico":               ("mexico", "méxico"),
    "Argentina":            ("argentina",),
    "Chile":                ("chile",),
    "Colombia":             ("colombia",),
    "New Zealand":          ("new zealand",),
}

# Reverse index: lower-cased alias → canonical country name.
_ALIAS_TO_COUNTRY: dict[str, str] = {
    alias: canon for canon, aliases in _COUNTRY_ALIASES.items() for alias in aliases
}

# US states (+ DC) full name → itself, and 2-letter code → full name. A trailing
# US-state token (with no explicit country) implies "United States".
_US_STATE_NAMES: tuple[str, ...] = (
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
    "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
    "Washington", "West Virginia", "Wisconsin", "Wyoming", "District of Columbia",
)
_US_STATE_CODES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
_US_STATE_LOOKUP: dict[str, str] = {
    **{name.lower(): name for name in _US_STATE_NAMES},
    **{code.lower(): name for code, name in _US_STATE_CODES.items()},
}

# Tokens that describe a work mode / "anywhere", never a place — dropped before
# resolving so e.g. "Chennai, India, Remote" still resolves country=India.
_WORK_MODE_TOKENS: frozenset[str] = frozenset({
    "remote", "on-site", "onsite", "on site", "hybrid", "worldwide", "anywhere",
    "work from home", "wfh",
})


def _canonical_country(token: str) -> str:
    """Return the canonical country name for a location token, or "" if it isn't
    a recognised country."""
    return _ALIAS_TO_COUNTRY.get(token.strip().lower(), "")


def _us_state(token: str) -> str:
    """Return the canonical US-state name for a token (full name or 2-letter
    code), or "" if it isn't one."""
    return _US_STATE_LOOKUP.get(token.strip().lower(), "")


def parse_location(text: str) -> tuple[str, str, str]:
    """Parse a free-text location into ``(city, state, country)``.

    ``country`` is normalized to a canonical name and set ONLY when the trailing
    place-token is a known country (or a US state ⇒ "United States"); otherwise
    it is "". ``state``/``city`` are best-effort (used for display, not
    filtering). All three default to "" for blank / Remote-only / unrecognised
    input. Never guesses a country.
    """
    if not text:
        return "", "", ""

    # Drop everything after a work-mode/parenthetical separator: "; On-site",
    # "· On-site", "(On-site)".
    geo = re.split(r"[;·(]", text, maxsplit=1)[0]

    tokens = [t.strip() for t in geo.split(",") if t.strip()]
    # Drop pure work-mode / "anywhere" tokens ("Chennai, India, Remote").
    tokens = [t for t in tokens if t.lower() not in _WORK_MODE_TOKENS]
    if not tokens:
        return "", "", ""

    country = ""
    state = ""
    rest = tokens

    last = tokens[-1]
    canon = _canonical_country(last)
    if canon:
        country = canon
        rest = tokens[:-1]
    else:
        us = _us_state(last)
        if us:
            country = "United States"
            state = us
            rest = tokens[:-1]

    if rest:
        if not state and len(rest) >= 2:
            state = rest[-1]
        city = rest[0]
    else:
        city = ""

    return city, state, country


def country_of(text: str) -> str:
    """Convenience: just the normalized country from a free-text location."""
    return parse_location(text)[2]
