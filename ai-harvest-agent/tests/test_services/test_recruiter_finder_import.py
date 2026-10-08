"""Tests for the upload parser / intelligent column mapping / validation
(app/services/recruiter_finder_import.py)."""
from __future__ import annotations

import pytest

pytest.importorskip("pandas")  # the parser imports pandas lazily

from app.services.recruiter_finder_import import parse_and_validate


def test_column_mapping_and_validation() -> None:
    csv = (
        b"Account Name,Contact,HQ City,Profile URL\n"
        b"Stripe,Sarah Chen,San Francisco,linkedin.com/in/sarah\n"      # valid (person + linkedin)
        b",Nobody,New York,\n"                                           # missing_company
        b"Stripe,Sarah Chen,San Francisco,linkedin.com/in/sarah\n"      # duplicate (same linkedin)
        b"Datadog,,New York,\n"                                          # valid (company-only)
    )
    res = parse_and_validate(csv, "targets.csv", max_rows=100)

    assert res["mapping"]["company"] == "Account Name"
    assert res["mapping"]["person_name"] == "Contact"
    assert res["mapping"]["linkedin_url"] == "Profile URL"
    assert res["mapping"]["location"] == "HQ City"

    s = res["summary"]
    assert s["ready"] == 2
    assert s["missing_company"] == 1
    assert s["duplicate"] == 1


def test_missing_company_column_raises() -> None:
    csv = b"Name,City\nSarah,SF\n"
    with pytest.raises(ValueError):
        parse_and_validate(csv, "bad.csv", max_rows=10)


def test_linkedin_not_mapped_to_domain() -> None:
    # A 'LinkedIn URL' header must be claimed by linkedin_url, never domain.
    csv = b"Company,LinkedIn URL\nStripe,linkedin.com/in/x\n"
    res = parse_and_validate(csv, "f.csv", max_rows=10)
    assert res["mapping"]["linkedin_url"] == "LinkedIn URL"
    assert res["mapping"]["domain"] is None


def test_sanitizes_and_dedupes_company_only_rows() -> None:
    csv = b"Company\nStripe\nstripe\nDatadog\n"
    res = parse_and_validate(csv, "f.csv", max_rows=10)
    # "Stripe" and "stripe" dedupe to one (case-insensitive company key).
    assert res["summary"]["ready"] == 2
    assert res["summary"]["duplicate"] == 1
