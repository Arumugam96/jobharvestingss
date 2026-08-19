"""
Tests for LinkedInAgent._llm_fallback_extract — the LLM extraction path used
for every LinkedIn job-detail page (selectors are never matched — LinkedIn's
CSS classes drift/obfuscate too often to rely on).

Uses a sample Job Card text + a sample Detailed Job Page HTML (standing in
for page.content(), which the agent strips down to text via _html_to_text —
see the rationale in LinkedInAgent._llm_fallback_extract) and drives the
extraction against a mocked Claude endpoint and a mocked local Ollama
endpoint, asserting both:
  * the complete job description and recruiter contact details come through
  * the page's <script>/<style> content never reaches the LLM prompt
  * the two providers return an identical schema/shape
"""
from __future__ import annotations

import json
import os
import sys
import pytest

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
sys.path.insert(0, PROJECT_ROOT)

from app.agents.linkedin_agent import LinkedInAgent
from app.config import Settings
from app.services.llm_service import LLMService

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"

# ── Sample "Job Card" (search/listing page) visible text ──────────────────────
SAMPLE_CARD_TEXT = (
    "Senior Data Engineer\n"
    "Acme Analytics · Bengaluru, Karnataka, India (Hybrid)\n"
    "Posted by Jane Doe · Talent Acquisition Lead\n"
    "2 days ago · 45 applicants"
)
SAMPLE_CARD_LINKS = [{"text": "Jane Doe", "href": "https://www.linkedin.com/in/janedoe-recruiter"}]

# ── Sample "Detailed Job" page HTML (obfuscated-CSS build) — the agent reads
# this via page.content() and strips it down to text with _html_to_text.
# Includes a <script>/<style> block to prove those never reach the LLM.
SAMPLE_DETAIL_HTML = """
<html>
<head>
<style>.a1b2c3 { color: red; }</style>
<script>window.__STATE__ = {leaked: "should-not-appear-in-prompt"};</script>
</head>
<body class="x9y8z7">
<h1 class="q1w2">Senior Data Engineer</h1>
<div class="e3r4t5">Acme Analytics &middot; Bengaluru, Karnataka, India &middot; Hybrid</div>
<script>console.log("also should not appear");</script>
<div class="desc-obfuscated">
About the job
We are looking for a Senior Data Engineer to join our growing data platform
team. You will design, build and maintain scalable ETL pipelines, partner
with analytics and product teams, and own data quality end to end.
Requirements: 5+ years in data engineering, strong SQL and Python,
experience with Airflow and Spark.
</div>
<div class="hiring-team-obfuscated">
Meet the hiring team
Jane Doe &middot; Talent Acquisition Lead at Acme Analytics
Interested candidates can also reach out at jane.doe@acmeanalytics.com
</div>
<a href="https://www.linkedin.com/company/acme-analytics">Acme Analytics</a>
</body>
</html>
"""
SAMPLE_DETAIL_LINKS = [{"text": "Jane Doe", "href": "https://www.linkedin.com/in/janedoe-recruiter"}]
SAMPLE_COMPANY_LINKS = [{"text": "Acme Analytics", "href": "https://www.linkedin.com/company/acme-analytics"}]

EXPECTED_EXTRACTION = {
    "title": "Senior Data Engineer",
    "company": "Acme Analytics",
    "company_url": "https://www.linkedin.com/company/acme-analytics",
    "location": "Bengaluru, Karnataka, India · Hybrid",
    "posted": None,
    "description": (
        "We are looking for a Senior Data Engineer to join our growing data "
        "platform team. You will design, build and maintain scalable ETL "
        "pipelines, partner with analytics and product teams, and own data "
        "quality end to end. Requirements: 5+ years in data engineering, "
        "strong SQL and Python, experience with Airflow and Spark."
    ),
    "employment_type": "Full-time",
    "salary": "",
    "job_insights": "Full-time | Mid-Senior level",
    "skills": ["SQL", "Python", "Airflow", "Spark"],
    "recruiter_name": "Jane Doe",
    "recruiter_title": "Talent Acquisition Lead",
    "recruiter_company": "Acme Analytics",
    "recruiter_url": "https://www.linkedin.com/in/janedoe-recruiter",
    "recruiter_email": "jane.doe@acmeanalytics.com",
    "recruiter_phone": None,
}


class FakeDetailPage:
    """Stand-in for a Playwright Page — only the calls _llm_fallback_extract makes."""

    def __init__(self, html: str, profile_links: list[dict], company_links: list[dict]) -> None:
        self._html = html
        self._profile_links = profile_links
        self._company_links = company_links

    async def content(self):
        return self._html

    async def eval_on_selector_all(self, selector: str, script: str):
        if "/company/" in selector:
            return self._company_links
        return self._profile_links


def _settings(extraction_llm_model: str) -> Settings:
    return Settings(
        anthropic_api_key="test-anthropic-key",
        anthropic_model="claude-sonnet-4-6",
        extraction_llm_model=extraction_llm_model,
        local_llm_url="http://localhost:11434",
        local_llm_model="llama3.1:8b",
    )


def _anthropic_response(text: str) -> dict:
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _fake_page() -> FakeDetailPage:
    return FakeDetailPage(SAMPLE_DETAIL_HTML, SAMPLE_DETAIL_LINKS, SAMPLE_COMPANY_LINKS)


async def _run_fallback(llm_service: LLMService) -> dict:
    agent = LinkedInAgent(llm_service=llm_service)
    return await agent._llm_fallback_extract(
        _fake_page(), idx=0, url="https://www.linkedin.com/jobs/view/123456/",
        card_text=SAMPLE_CARD_TEXT, card_links=SAMPLE_CARD_LINKS,
    )


@pytest.mark.asyncio
async def test_llm_fallback_claude_extracts_description_and_recruiter(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST", url=ANTHROPIC_MESSAGES_URL,
        json=_anthropic_response(json.dumps(EXPECTED_EXTRACTION)),
    )
    result = await _run_fallback(LLMService(_settings("claude")))

    assert result["title"] == "Senior Data Engineer"
    assert result["company"] == "Acme Analytics"
    assert result["company_url"] == "https://www.linkedin.com/company/acme-analytics"
    assert result["description"] == EXPECTED_EXTRACTION["description"]
    assert result["job_insights"] == "Full-time | Mid-Senior level"
    assert result["recruiter_name"] == "Jane Doe"
    assert result["recruiter_title"] == "Talent Acquisition Lead"
    assert result["recruiter_company"] == "Acme Analytics"
    assert result["recruiter_url"] == "https://www.linkedin.com/in/janedoe-recruiter"
    assert result["recruiter_email"] == "jane.doe@acmeanalytics.com"
    assert "recruiter_phone" not in result  # null -> omitted, never fabricated
    assert "posted" not in result  # null -> omitted

    # The card's own text/links, and the detail page's stripped text, were
    # forwarded into the prompt sent to the LLM — but never its raw markup.
    sent_body = json.loads(httpx_mock.get_requests()[0].content)
    sent_prompt = sent_body["messages"][0]["content"]
    assert "Jane Doe" in sent_prompt
    assert "45 applicants" in sent_prompt  # from the card
    assert "Requirements: 5+ years" in sent_prompt  # from the detail page
    assert "acme-analytics" in sent_prompt  # company link candidate
    assert "leaked" not in sent_prompt  # <script> content stripped
    assert "should-not-appear-in-prompt" not in sent_prompt
    assert "color: red" not in sent_prompt  # <style> content stripped
    assert "q1w2" not in sent_prompt  # obfuscated class names stripped


@pytest.mark.asyncio
async def test_llm_fallback_ollama_returns_identical_shape(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST", url="http://localhost:11434/api/generate",
        json={"response": json.dumps(EXPECTED_EXTRACTION), "done": True},
    )
    ollama_result = await _run_fallback(LLMService(_settings("ollama")))

    httpx_mock.add_response(
        method="POST", url=ANTHROPIC_MESSAGES_URL,
        json=_anthropic_response(json.dumps(EXPECTED_EXTRACTION)),
    )
    claude_result = await _run_fallback(LLMService(_settings("claude")))

    assert ollama_result == claude_result
    assert set(ollama_result.keys()) == set(claude_result.keys())


@pytest.mark.asyncio
async def test_llm_fallback_parses_description_html(httpx_mock) -> None:
    """The LLM's clean-HTML description variant is parsed into the result so
    _extract_cards can use it for job_description_html."""
    payload = dict(EXPECTED_EXTRACTION)
    payload["description_html"] = "<h3>About</h3><p>We build things.</p><ul><li>SQL</li></ul>"
    httpx_mock.add_response(
        method="POST", url=ANTHROPIC_MESSAGES_URL,
        json=_anthropic_response(json.dumps(payload)),
    )
    result = await _run_fallback(LLMService(_settings("claude")))

    assert result["description_html"] == "<h3>About</h3><p>We build things.</p><ul><li>SQL</li></ul>"


@pytest.mark.asyncio
async def test_llm_fallback_respects_per_run_call_cap(httpx_mock) -> None:
    # No mock registered — the cap must short-circuit before any HTTP call is made.
    agent = LinkedInAgent(llm_service=LLMService(_settings("claude")))
    agent._llm_fallback_calls = agent._LLM_FALLBACK_MAX_CALLS_PER_RUN

    result = await agent._llm_fallback_extract(
        _fake_page(), idx=0, url="https://www.linkedin.com/jobs/view/123456/",
        card_text=SAMPLE_CARD_TEXT, card_links=SAMPLE_CARD_LINKS,
    )

    assert result == {}
    assert len(httpx_mock.get_requests()) == 0
