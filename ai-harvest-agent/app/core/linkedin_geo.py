"""LinkedIn geography → geoId resolution.

LinkedIn scopes a job search by the numeric ``geoId`` parameter, NOT by the
free-text ``location`` string. When a request carries no geoId, LinkedIn falls
back to the logged-in account's home location (for the harvesting account that
is India/Chennai) — so a blank *or* an unrecognized location silently narrows a
"worldwide" search down to one place. Resolving names to a geoId here, and
defaulting a blank location to Worldwide, makes the geographic scope explicit
and predictable instead of dependent on whose account is scraping.

Shared by both LinkedIn scrapers (``LinkedInAgent`` and ``LinkedInFormScraper``)
so their two URL builders can't drift.

Note: only "Worldwide" (92000000) is verified in-app. The country ids below are
the widely-published LinkedIn geoIds; sanity-check one by loading
``https://www.linkedin.com/jobs/search/?geoId=<id>`` before relying on it, and
extend ``_GEO_IDS`` with additional verified ids as needed. An unknown location
is never guessed — it falls through to best-effort free-text (see resolve_geo).
"""
from __future__ import annotations

# LinkedIn's stable geoId for global ("Worldwide") results — verified working.
WORLDWIDE_GEOID = "92000000"

# Known location name (lower-cased) → LinkedIn geoId.
_GEO_IDS: dict[str, str] = {
    "worldwide":            WORLDWIDE_GEOID,
    "united states":        "103644278",
    "usa":                  "103644278",
    "us":                   "103644278",
    "india":                "102713980",
    "united kingdom":       "101165590",
    "uk":                   "101165590",
    "canada":               "101174742",
    "australia":            "101452733",
    "germany":              "101282230",
    "singapore":            "102454443",
    "united arab emirates": "104305776",
    "uae":                  "104305776",
}


def resolve_geo(location: str) -> tuple[str, str]:
    """Resolve a configured location to ``(geo_id, display_label)``.

    • blank / whitespace  → ``(WORLDWIDE_GEOID, "Worldwide")`` — a search with no
      location means "the whole world", never the scraping account's home geo.
    • a known name        → ``(its geoId, the original label)``.
    • an unknown non-blank → ``("", the original label)`` — no geoId; the caller
      passes the label through as best-effort free-text ``location`` and lets
      LinkedIn resolve it server-side.
    """
    loc = (location or "").strip()
    if not loc:
        return WORLDWIDE_GEOID, "Worldwide"
    return _GEO_IDS.get(loc.lower(), ""), loc
