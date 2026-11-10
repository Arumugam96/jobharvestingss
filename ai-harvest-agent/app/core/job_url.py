"""Job-URL normalization — a single canonical key for a posting across runs.

Used for cross-run dedup in two places that MUST agree on the key:
  • the LinkedIn harvest skips re-scraping a posting whose normalized URL was
    already harvested within the lookback window (source-level dedup);
  • the outreach log stores + matches on this key as a backstop so the same
    posting is never emailed twice.

The rule mirrors the LinkedIn agent's long-standing in-run `seen_urls` key —
drop any fragment + query string, strip a trailing slash, lowercase — so it is a
drop-in that doesn't change existing dedup behaviour. For a real LinkedIn job the
stable identity lives in the `/jobs/view/<id>` path, which this preserves (only
the tracking query is dropped), so the same posting matches across runs.

``posting_key`` is the coarser, URL-independent identity (company + title + city) used
by the report to count the same posting found on two sources (or reposted under a
new URL) once. ``normalize_company_key`` lives here (re-exported by
app/services/company_service.py) so both keys share one dependency-free module.
"""
from __future__ import annotations

import re

_LEGAL_SUFFIXES = re.compile(
    r"\b(inc|incorporated|ltd|limited|llc|llp|pvt|private|corp|corporation|co|plc|gmbh|"
    r"technologies|technology|solutions|services|systems|labs|software)\b"
)
_PUNCT = re.compile(r"[^\w\s]")
_NON_WORD = re.compile(r"[\W_]+")
_WS = re.compile(r"\s+")
# Old/alternate city spellings seen across LinkedIn and Naukri → one canonical name.
_CITY_ALIASES = {
    "bangalore": "bengaluru", "gurgaon": "gurugram", "bombay": "mumbai",
    "madras": "chennai", "calcutta": "kolkata", "new delhi": "delhi",
    "trivandrum": "thiruvananthapuram", "cochin": "kochi", "mysore": "mysuru",
}
_PARENS = re.compile(r"\([^)]*\)")

def normalize_job_url(url: str | None) -> str:
    """Canonical dedup key for a job posting URL, or "" for a blank/missing url.

    Strips the fragment and query string, a trailing slash, and lowercases —
    identical to the LinkedIn agent's historical normalization, so stored URLs
    and live card URLs compare equal."""
    if not url:
        return ""
    return url.split("#")[0].split("?")[0].strip().rstrip("/").lower()


def normalize_company_key(company_name: str) -> str:
    """Stable dedup key for a company name — lower-cased, legal/industry suffixes
    and punctuation stripped, whitespace collapsed. "" for a blank name."""
    text = (company_name or "").lower()
    text = _LEGAL_SUFFIXES.sub("", text)
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def normalize_title_key(title: str | None) -> str:
    """Lower-cased job title with every non-word run collapsed to one space."""
    return _NON_WORD.sub(" ", (title or "").lower()).strip()


def normalize_city_key(location: str | None) -> str:
    """Primary city of a location string: the first comma/slash segment, without
    parenthesised notes ("(Hybrid)"), lower-cased, aliases folded ("Bangalore" →
    "bengaluru"). LinkedIn's "Bengaluru, Karnataka, India" and Naukri's
    "Bangalore/Bengaluru, Pune" both → "bengaluru". "" for a blank location."""
    text = _PARENS.sub(" ", (location or "").lower())
    first = re.split(r"[,/|;]", text, maxsplit=1)[0]
    city = _NON_WORD.sub(" ", first).strip()
    return _CITY_ALIASES.get(city, city)


def posting_key(
    company: str | None,
    title: str | None,
    url: str | None = None,
    location: str | None = None,
) -> str:
    """URL-independent identity of a posting: ``"<company key>|<title key>|<city>"``,
    so the same role at the same company in the same city found on LinkedIn AND
    Naukri (or reposted under a new URL) collapses to one key, while the same role
    in two cities stays two postings. A blank location just drops the city part.
    When the company or title is blank the pair is too weak to merge on, so this
    falls back to the normalized URL ("" if that is blank too — callers must treat
    "" as "never collapse")."""
    company_key = normalize_company_key(company or "")
    title_key = normalize_title_key(title)
    if company_key and title_key:
        key = f"{company_key}|{title_key}"
        city_key = normalize_city_key(location)
        return f"{key}|{city_key}" if city_key else key
    return normalize_job_url(url)
