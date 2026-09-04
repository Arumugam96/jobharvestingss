"""
Tests for LLMService's two-provider failover (Part 1 of the local-LLM outage
resilience work): local LLM first, then a single configurable fallback
(EXTRACTION_FALLBACK_MODEL). Covers the provider chain, a successful failover,
per-attempt call-log recording, and the all-providers-down error.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.core.exceptions import LLMError, LLMUnavailableError
from app.services.llm_service import LLMService

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
OLLAMA_URL = "http://localhost:11434/api/generate"

SAMPLE_SCHEMA = '{"description": str}'
SAMPLE_EXTRACTED = {"description": "Full job description text."}


def _settings(**overrides) -> Settings:
    base = dict(
        anthropic_api_key="test-anthropic-key",
        anthropic_model="claude-sonnet-4-6",
        extraction_llm_model="ollama",
        local_llm_url="http://localhost:11434",
        local_llm_model="llama3.1:8b",
        openrouter_api_key="test-openrouter-key",
        openrouter_model="google/gemma-3-27b-it:free",
    )
    base.update(overrides)
    return Settings(**base)


def _anthropic_response(text: str) -> dict:
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5},
    }


# ── Provider chain ────────────────────────────────────────────────────────────

def test_provider_chain_no_fallback() -> None:
    svc = LLMService(_settings(extraction_llm_model="ollama", extraction_fallback_model=""))
    assert svc._provider_chain() == [("ollama", "llama3.1:8b")]


def test_provider_chain_with_fallback() -> None:
    svc = LLMService(_settings(extraction_llm_model="ollama", extraction_fallback_model="claude"))
    assert svc._provider_chain() == [("ollama", "llama3.1:8b"), ("claude", "claude-sonnet-4-6")]


def test_provider_chain_skips_duplicate_provider() -> None:
    # Fallback resolves to the same provider as primary → no second attempt.
    svc = LLMService(_settings(extraction_llm_model="ollama", extraction_fallback_model="mistral"))
    assert svc._provider_chain() == [("ollama", "llama3.1:8b")]


# ── Failover behaviour ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failover_local_down_uses_fallback(httpx_mock) -> None:
    """Local LLM refused → extraction succeeds via the configured fallback, and
    BOTH attempts are recorded in the call log (failed local + successful fallback)."""
    httpx_mock.add_exception(httpx.ConnectError("refused"), url=OLLAMA_URL, is_reusable=True)
    httpx_mock.add_response(method="POST", url=ANTHROPIC_MESSAGES_URL,
                            json=_anthropic_response(json.dumps(SAMPLE_EXTRACTED)))
    svc = LLMService(_settings(extraction_llm_model="ollama", extraction_fallback_model="claude"))

    result = await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)

    assert result == SAMPLE_EXTRACTED
    log = svc.get_call_log()
    assert [c["provider"] for c in log] == ["ollama", "claude"]
    assert log[0]["success"] is False
    assert log[1]["success"] is True


@pytest.mark.asyncio
async def test_failover_all_providers_down_raises(httpx_mock) -> None:
    """Local down AND fallback down → LLMUnavailableError (every provider exhausted)."""
    httpx_mock.add_exception(httpx.ConnectError("refused"), url=OLLAMA_URL, is_reusable=True)
    httpx_mock.add_exception(httpx.ConnectError("refused"), url=ANTHROPIC_MESSAGES_URL, is_reusable=True)
    svc = LLMService(_settings(extraction_llm_model="ollama", extraction_fallback_model="claude"))

    with pytest.raises(LLMError):  # LLMUnavailableError is an LLMError subclass
        await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)


@pytest.mark.asyncio
async def test_no_fallback_local_down_raises_unavailable(httpx_mock) -> None:
    """With no fallback configured, a local outage surfaces as LLMUnavailableError
    (the signal the harvest degrades on)."""
    httpx_mock.add_exception(httpx.ConnectError("refused"), url=OLLAMA_URL, is_reusable=True)
    svc = LLMService(_settings(extraction_llm_model="ollama", extraction_fallback_model=""))

    with pytest.raises(LLMUnavailableError):
        await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)
