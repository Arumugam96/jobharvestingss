"""Pure-logic tests for the Contact Finder: persona→titles resolution and the
reveal→(email, phone, cost) mapping."""
from __future__ import annotations

from app.core.hr_titles import HR_TITLES
from app.core.persona_titles import titles_for_persona
from app.services.recruiter_finder_service import _reveal_flags


def test_persona_defaults_to_recruiting_hr() -> None:
    assert titles_for_persona("Recruiters & Talent Acquisition") == HR_TITLES
    assert titles_for_persona("totally unknown persona") == HR_TITLES
    assert titles_for_persona(None) == HR_TITLES


def test_persona_other_personas_differ() -> None:
    sales = titles_for_persona("Sales & Business Development")
    assert sales != HR_TITLES
    assert "VP Sales" in sales


def test_custom_titles_win() -> None:
    assert titles_for_persona("anything", custom_titles=["CEO", "CTO"]) == ["CEO", "CTO"]
    # Empty/blank custom titles fall back to the persona.
    assert titles_for_persona("Recruiters & Talent Acquisition", custom_titles=["  ", ""]) == HR_TITLES


def test_reveal_flags_and_cost() -> None:
    assert _reveal_flags("email") == (True, False, 1)
    assert _reveal_flags("phone") == (False, True, 1)
    assert _reveal_flags("both") == (True, True, 2)
    assert _reveal_flags("") == (True, False, 1)   # defaults to email
    assert _reveal_flags(None) == (True, False, 1)
