"""Tests for the location-key helpers used by the company+location HR-contact cache."""
from __future__ import annotations

from app.core.location import location_search_terms, normalize_location_key


def test_normalize_location_key_full_tuple() -> None:
    assert normalize_location_key("Bengaluru, Karnataka, India") == "bengaluru|karnataka|india"


def test_normalize_location_key_is_case_and_space_stable() -> None:
    a = normalize_location_key("  bengaluru ,  Karnataka , INDIA ")
    b = normalize_location_key("Bengaluru, Karnataka, India")
    assert a == b == "bengaluru|karnataka|india"


def test_normalize_location_key_distinct_cities_differ() -> None:
    assert normalize_location_key("Chennai, Tamil Nadu, India") != normalize_location_key(
        "Bengaluru, Karnataka, India"
    )


def test_normalize_location_key_blank_and_remote_are_empty() -> None:
    # No derivable location -> "" so the discovery pass skips (never company-wide).
    assert normalize_location_key("") == ""
    assert normalize_location_key("Remote") == ""
    assert normalize_location_key("   ") == ""


def test_normalize_location_key_drops_work_mode_suffix() -> None:
    # parse_location strips "; On-site" / ", Remote" work-mode tokens.
    assert normalize_location_key("Chennai, Tamil Nadu, India; On-site") == "chennai|tamil nadu|india"


def test_location_search_terms_finest_granularity() -> None:
    assert location_search_terms("Bengaluru, Karnataka, India") == "Bengaluru, Karnataka, India"


def test_location_search_terms_country_only() -> None:
    assert location_search_terms("Singapore") == "Singapore"


def test_location_search_terms_blank_remote_empty() -> None:
    assert location_search_terms("Remote") == ""
    assert location_search_terms("") == ""
