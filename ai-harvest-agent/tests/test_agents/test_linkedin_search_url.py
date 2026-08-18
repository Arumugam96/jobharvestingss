"""Tests for LinkedInAgent._build_search_url / _compose_keyword_query.

Covers the search behaviour so LinkedIn's own filtered results are the source
of truth:
  • the chosen domain is placed into the `keywords=` box as-is (no boolean
    OR-set, no native f_F function filter);
  • a user-typed keyword always wins over the domain;
  • location is optional — only emitted when a value is provided.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app.agents.linkedin_agent import LinkedInAgent
from app.models.harvest_models import FiltersConfig


def _params(**kw) -> dict[str, list[str]]:
    url = LinkedInAgent._build_search_url(FiltersConfig(**kw))
    return parse_qs(urlparse(url).query, keep_blank_values=True)


def test_domain_placed_as_is_in_keywords() -> None:
    p = _params(domain="Data Engineering", keyword="")
    assert p["keywords"] == ["Data Engineering"]


def test_domain_with_slash_placed_as_is() -> None:
    p = _params(domain="AI/ML", keyword="")
    assert p["keywords"] == ["AI/ML"]


def test_coarse_it_domain_placed_as_is() -> None:
    p = _params(domain="IT", keyword="")
    assert p["keywords"] == ["IT"]


def test_no_boolean_or_and_no_function_filter() -> None:
    url = LinkedInAgent._build_search_url(FiltersConfig(domain="Data Engineering"))
    assert " OR " not in url and "%20OR%20" not in url
    assert "f_F=" not in url


def test_user_keyword_wins_over_domain() -> None:
    p = _params(domain="Data Engineering", keyword="senior data engineer")
    assert p["keywords"] == ["senior data engineer"]


def test_any_domain_no_keyword_is_blank() -> None:
    p = _params(domain="Any", keyword="")
    assert p["keywords"] == [""]


def test_non_it_domain_no_keyword_is_blank() -> None:
    p = _params(domain="Non-IT", keyword="")
    assert p["keywords"] == [""]


def test_location_omitted_when_empty() -> None:
    url = LinkedInAgent._build_search_url(FiltersConfig(domain="Data Engineering", location=""))
    assert "location=" not in url


def test_location_included_when_provided() -> None:
    p = _params(domain="Data Engineering", location="Chennai")
    assert p["location"] == ["Chennai"]


def test_time_window_only_via_f_tpr_not_in_keyword() -> None:
    # 24h window must be applied via LinkedIn's native f_TPR filter, never mixed
    # into the keyword (e.g. "Data Engineering posted in the past 24 hours").
    p = _params(domain="Data Engineering", keyword="", search_window_hours=24)
    assert p["f_TPR"] == ["r86400"]
    kw = p["keywords"][0].lower()
    for banned in ("hour", "past", "posted", "24"):
        assert banned not in kw
