"""Tests for _extract_linkedin_contact_info — the recruiter contact-info
extractor. Guards the fix for the "wrong person's email" bug:

  • the capture is scoped to the profile owner's own nodes (contact modal +
    top card) — `main`/`body`/the activity feed are never read;
  • the LLM is the sole authority for email/phone when available, so an LLM
    `null` correctly yields no email even if a stranger's address is on the page.
"""
from __future__ import annotations

import pytest

from app.agents.prospect_intelligence_agent import (
    _extract_linkedin_contact_info,
    _LI_CONTACT_SELECTORS,
    _LI_MODAL_SELECTORS,
)

_OWNER_TOPCARD = (".pv-top-card", "section.artdeco-card.pv-top-card", ".pv-text-details__left-panel")
_LEAK_SELECTORS = ("main", "main .scaffold-layout__main", "body")


class _El:
    def __init__(self, text: str = "") -> None:
        self._text = text

    async def inner_text(self) -> str:
        return self._text

    async def click(self) -> None:
        pass


class _FakeResp:
    status = 200


class _FakePage:
    """Minimal Playwright-page stand-in. The stranger email lives ONLY in the
    body/main region (via inner_text); if the code reads those, the leak recurs
    — so the tests assert those are never touched."""

    def __init__(self, modal_text: str = "", topcard_text: str | None = None) -> None:
        self.url = "https://www.linkedin.com/in/someone/"
        self.queried: list[str] = []
        self.inner_text_called = False
        self._modal_text = modal_text
        self._topcard_text = topcard_text

    async def goto(self, url, wait_until=None, timeout=None):
        return _FakeResp()

    async def wait_for_timeout(self, ms):
        pass

    async def query_selector(self, sel):
        self.queried.append(sel)
        if sel in _LI_CONTACT_SELECTORS:
            return _El("Contact info")           # clickable -> opens modal
        if sel in _OWNER_TOPCARD:
            return _El(self._topcard_text) if self._topcard_text else None
        return None

    async def query_selector_all(self, sel):
        self.queried.append(sel)
        if sel in _LI_MODAL_SELECTORS:
            return [_El(self._modal_text)] if self._modal_text else []
        return []

    async def inner_text(self, sel):
        # This is the whole-body scan that used to leak the activity-feed email.
        self.inner_text_called = True
        return "STRANGER renu@othercorp.com +91 99999 88888"


class _LLM:
    def __init__(self, email=None, phone=None):
        self._email = email
        self._phone = phone

    async def extract_json(self, **kwargs):
        return {"email": self._email, "phone": self._phone}


@pytest.mark.asyncio
async def test_body_and_main_are_never_read() -> None:
    page = _FakePage(modal_text="Contact info\nProfile: linkedin.com/in/someone")
    await _extract_linkedin_contact_info(page, "https://www.linkedin.com/in/someone/",
                                         company_domain="acme.com", llm_service=_LLM())
    assert page.inner_text_called is False
    assert not any(sel in page.queried for sel in _LEAK_SELECTORS)


@pytest.mark.asyncio
async def test_llm_null_yields_no_email() -> None:
    # Modal has no email; LLM returns null -> email must stay empty (no regex leak).
    page = _FakePage(modal_text="Contact info\nProfile: linkedin.com/in/someone")
    out = await _extract_linkedin_contact_info(page, "https://www.linkedin.com/in/someone/",
                                               company_domain="acme.com", llm_service=_LLM())
    assert out["email"] == ""
    assert out["phone"] == ""


@pytest.mark.asyncio
async def test_llm_value_is_returned() -> None:
    page = _FakePage(modal_text="Email\njane@acme.com")
    out = await _extract_linkedin_contact_info(page, "https://www.linkedin.com/in/someone/",
                                               company_domain="acme.com",
                                               llm_service=_LLM(email="jane@acme.com"))
    assert out["email"] == "jane@acme.com"
    assert out["contact_section_found"] is True


@pytest.mark.asyncio
async def test_no_llm_falls_back_to_modal_regex_only() -> None:
    # No LLM: regex may run, but ONLY over the modal text (owner's own).
    page = _FakePage(modal_text="Email jane@acme.com Phone +91 90000 10000")
    out = await _extract_linkedin_contact_info(page, "https://www.linkedin.com/in/someone/",
                                               company_domain="acme.com", llm_service=None)
    assert out["email"] == "jane@acme.com"
    assert page.inner_text_called is False


@pytest.mark.asyncio
async def test_no_llm_no_modal_email_stays_empty() -> None:
    page = _FakePage(modal_text="Contact info\nProfile: linkedin.com/in/someone")
    out = await _extract_linkedin_contact_info(page, "https://www.linkedin.com/in/someone/",
                                               company_domain="acme.com", llm_service=None)
    assert out["email"] == ""
    assert page.inner_text_called is False
