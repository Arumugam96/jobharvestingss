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
"""
from __future__ import annotations


def normalize_job_url(url: str | None) -> str:
    """Canonical dedup key for a job posting URL, or "" for a blank/missing url.

    Strips the fragment and query string, a trailing slash, and lowercases —
    identical to the LinkedIn agent's historical normalization, so stored URLs
    and live card URLs compare equal."""
    if not url:
        return ""
    return url.split("#")[0].split("?")[0].strip().rstrip("/").lower()
