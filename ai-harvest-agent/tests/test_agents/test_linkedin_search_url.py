"""Tests for LinkedInAgent._build_search_url.

Covers the two behaviours changed so LinkedIn's own filtered search is the
source of truth:
  • domain is applied via LinkedIn's native job-function filter (f_F), not by
    OR-ing domain keywords into the query;
  • location is optional — only emitted when a value is provided.
"""
from __future__ import annotations

from app.agents.linkedin_agent import LinkedInAgent
from app.models.harvest_models import FiltersConfig


def _url(**kw) -> str:
    return LinkedInAgent._build_search_url(FiltersConfig(**kw))


def test_it_domain_uses_native_function_filter_no_or_set() -> None:
    url = _url(domain="IT", keyword="")
    assert "f_F=it" in url
    # keyword is empty and NOT replaced by a domain OR-set
    assert "keywords=&" in url or url.endswith("keywords=") or "keywords=&sortBy" in url
    assert " OR " not in url and "%20OR%20" not in url


def test_keyword_passed_verbatim_with_function_filter() -> None:
    url = _url(domain="IT", keyword="developer", location="Chennai")
    assert "keywords=developer" in url
    assert "f_F=it" in url
    assert "location=Chennai" in url


def test_location_omitted_when_empty() -> None:
    url = _url(domain="IT", keyword="developer", location="")
    assert "location=" not in url


def test_location_included_when_provided() -> None:
    url = _url(domain="Any", keyword="nurse", location="Mumbai")
    assert "location=Mumbai" in url


def test_any_domain_emits_no_function_filter() -> None:
    url = _url(domain="Any", keyword="data engineer")
    assert "f_F=" not in url


def test_non_it_domain_emits_no_function_filter() -> None:
    url = _url(domain="Non-IT", keyword="teacher")
    assert "f_F=" not in url


def test_uxui_domain_maps_to_design_function() -> None:
    url = _url(domain="UX/UI", keyword="")
    assert "f_F=dsgn" in url
