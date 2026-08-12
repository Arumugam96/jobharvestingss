"""
Tests for LLMService's multi-provider extraction (Claude vs local Ollama LLM).

Covers:
  * Provider selection driven by EXTRACTION_LLM_MODEL / LOCAL_LLM_URL / LOCAL_LLM_MODEL
  * extract_json() against a mocked Claude endpoint
  * extract_json() against a mocked local Ollama endpoint
  * Identical output schema across both providers
  * Debug artifact persistence (data/debug/linkedin style directory)
  * Local LLM connectivity / timeout error handling
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.core.exceptions import LLMError
from app.services.llm_service import LLMService

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _settings(**overrides) -> Settings:
    base = dict(
        anthropic_api_key="test-anthropic-key",
        anthropic_model="claude-sonnet-4-6",
        extraction_llm_model="claude",
        local_llm_url="http://localhost:11434",
        local_llm_model="llama3.1:8b",
        openrouter_api_key="test-openrouter-key",
        openrouter_model="google/gemma-3-27b-it:free",
    )
    base.update(overrides)
    return Settings(**base)


def _openrouter_response(text: str, prompt_tokens: int = 20, completion_tokens: int = 8) -> dict:
    return {
        "id": "gen_test",
        "model": "google/gemma-3-27b-it:free",
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def _anthropic_response(text: str) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


SAMPLE_SCHEMA = (
    '{"description": str, "recruiter_name": str or null, '
    '"recruiter_email": str or null, "recruiter_phone": str or null}'
)
SAMPLE_EXTRACTED = {
    "description": "Full job description text.",
    "recruiter_name": "Jane Doe",
    "recruiter_email": None,
    "recruiter_phone": None,
}


# ══════════════════════════════════════════════════════════════════════════════
# Provider selection
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "extraction_llm_model,expected_provider,expected_model",
    [
        ("", "claude", "claude-sonnet-4-6"),
        ("claude", "claude", "claude-sonnet-4-6"),
        ("Claude", "claude", "claude-sonnet-4-6"),
        ("claude-opus-4-8", "claude", "claude-opus-4-8"),
        ("ollama", "ollama", "llama3.1:8b"),
        ("llama3.1:8b", "ollama", "llama3.1:8b"),
        ("mistral", "ollama", "mistral"),
        ("openrouter", "openrouter", "google/gemma-3-27b-it:free"),
        ("OpenRouter", "openrouter", "google/gemma-3-27b-it:free"),
        ("openrouter/google/gemma-3-27b-it:free", "openrouter", "google/gemma-3-27b-it:free"),
        ("openrouter/anthropic/claude-sonnet-4.6", "openrouter", "anthropic/claude-sonnet-4.6"),
    ],
)
def test_resolve_extraction_target(extraction_llm_model, expected_provider, expected_model) -> None:
    svc = LLMService(_settings(extraction_llm_model=extraction_llm_model))
    provider, model = svc._resolve_extraction_target()
    assert provider == expected_provider
    assert model == expected_model


# ══════════════════════════════════════════════════════════════════════════════
# Claude extraction path
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_extract_json_claude_path(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=ANTHROPIC_MESSAGES_URL,
        json=_anthropic_response(json.dumps(SAMPLE_EXTRACTED)),
    )
    svc = LLMService(_settings(extraction_llm_model="claude"))

    result = await svc.extract_json(content="<html>job card + detail text</html>", schema_description=SAMPLE_SCHEMA)

    assert result == SAMPLE_EXTRACTED


@pytest.mark.asyncio
async def test_extract_json_claude_strips_markdown_fence(httpx_mock) -> None:
    fenced = "```json\n" + json.dumps(SAMPLE_EXTRACTED) + "\n```"
    httpx_mock.add_response(method="POST", url=ANTHROPIC_MESSAGES_URL, json=_anthropic_response(fenced))
    svc = LLMService(_settings(extraction_llm_model="claude"))

    result = await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)

    assert result == SAMPLE_EXTRACTED


@pytest.mark.asyncio
async def test_extract_json_claude_invalid_json_raises(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", url=ANTHROPIC_MESSAGES_URL, json=_anthropic_response("not json"))
    svc = LLMService(_settings(extraction_llm_model="claude"))

    with pytest.raises(LLMError):
        await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)


# ══════════════════════════════════════════════════════════════════════════════
# Local (Ollama) extraction path
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_extract_json_ollama_path(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:11434/api/generate",
        json={"model": "llama3.1:8b", "response": json.dumps(SAMPLE_EXTRACTED), "done": True},
    )
    svc = LLMService(_settings(extraction_llm_model="ollama"))

    result = await svc.extract_json(content="<html>job card + detail text</html>", schema_description=SAMPLE_SCHEMA)

    assert result == SAMPLE_EXTRACTED

    request = httpx_mock.get_requests()[0]
    payload = json.loads(request.content)
    assert payload["model"] == "llama3.1:8b"
    assert payload["stream"] is False
    assert payload["format"] == "json"


@pytest.mark.asyncio
async def test_extract_json_ollama_uses_extraction_model_name_directly(httpx_mock) -> None:
    """EXTRACTION_LLM_MODEL="qwen2.5:14b" -> Ollama, model name = the value itself."""
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:11434/api/generate",
        json={"response": json.dumps(SAMPLE_EXTRACTED), "done": True},
    )
    svc = LLMService(_settings(extraction_llm_model="qwen2.5:14b"))

    await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)

    request = httpx_mock.get_requests()[0]
    payload = json.loads(request.content)
    assert payload["model"] == "qwen2.5:14b"


@pytest.mark.asyncio
async def test_extract_json_ollama_connection_error(httpx_mock) -> None:
    # _complete_text_local retries up to 3x (same pattern as the Claude path) —
    # register the failure as reusable so every retry attempt hits it too.
    httpx_mock.add_exception(httpx.ConnectError("connection refused"), is_reusable=True)
    svc = LLMService(_settings(extraction_llm_model="ollama"))

    with pytest.raises(LLMError, match="unreachable"):
        await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)


@pytest.mark.asyncio
async def test_extract_json_ollama_timeout(httpx_mock) -> None:
    httpx_mock.add_exception(httpx.ReadTimeout("timed out"), is_reusable=True)
    svc = LLMService(_settings(extraction_llm_model="ollama"))

    with pytest.raises(LLMError, match="timed out"):
        await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)


# ══════════════════════════════════════════════════════════════════════════════
# OpenRouter extraction path
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_extract_json_openrouter_path(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url=OPENROUTER_URL,
        json=_openrouter_response(json.dumps(SAMPLE_EXTRACTED)),
    )
    svc = LLMService(_settings(extraction_llm_model="openrouter"))

    result = await svc.extract_json(content="<html>job card + detail text</html>", schema_description=SAMPLE_SCHEMA)

    assert result == SAMPLE_EXTRACTED

    request = httpx_mock.get_requests()[0]
    assert request.headers["authorization"] == "Bearer test-openrouter-key"
    payload = json.loads(request.content)
    assert payload["model"] == "google/gemma-3-27b-it:free"
    assert payload["messages"][-1] == {"role": "user", "content": payload["messages"][-1]["content"]}
    assert payload["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_extract_json_openrouter_explicit_model_id(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", url=OPENROUTER_URL, json=_openrouter_response(json.dumps(SAMPLE_EXTRACTED)))
    svc = LLMService(_settings(extraction_llm_model="openrouter/anthropic/claude-sonnet-4.6"))

    await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)

    payload = json.loads(httpx_mock.get_requests()[0].content)
    assert payload["model"] == "anthropic/claude-sonnet-4.6"


@pytest.mark.asyncio
async def test_extract_json_openrouter_missing_api_key_raises(httpx_mock) -> None:
    svc = LLMService(_settings(extraction_llm_model="openrouter", openrouter_api_key=""))

    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)


@pytest.mark.asyncio
async def test_extract_json_openrouter_inline_error_raises(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST", url=OPENROUTER_URL,
        json={"error": {"code": 503, "message": "model temporarily unavailable"}},
        is_reusable=True,
    )
    svc = LLMService(_settings(extraction_llm_model="openrouter"))

    with pytest.raises(LLMError, match="model temporarily unavailable"):
        await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)


@pytest.mark.asyncio
async def test_extract_json_openrouter_connection_error(httpx_mock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("connection refused"), is_reusable=True)
    svc = LLMService(_settings(extraction_llm_model="openrouter"))

    with pytest.raises(LLMError, match="unreachable"):
        await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)


# ══════════════════════════════════════════════════════════════════════════════
# Schema parity across providers
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_claude_and_ollama_return_identical_shape(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", url=ANTHROPIC_MESSAGES_URL, json=_anthropic_response(json.dumps(SAMPLE_EXTRACTED)))
    claude_svc = LLMService(_settings(extraction_llm_model="claude"))
    claude_result = await claude_svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)

    httpx_mock.add_response(
        method="POST", url="http://localhost:11434/api/generate",
        json={"response": json.dumps(SAMPLE_EXTRACTED), "done": True},
    )
    ollama_svc = LLMService(_settings(extraction_llm_model="ollama"))
    ollama_result = await ollama_svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)

    assert claude_result == ollama_result
    assert set(claude_result.keys()) == set(ollama_result.keys())


# ══════════════════════════════════════════════════════════════════════════════
# Debug artifact persistence
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_extract_json_saves_debug_artifact(httpx_mock, tmp_path) -> None:
    httpx_mock.add_response(method="POST", url=ANTHROPIC_MESSAGES_URL, json=_anthropic_response(json.dumps(SAMPLE_EXTRACTED)))
    svc = LLMService(_settings(extraction_llm_model="claude"))

    await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA, debug_dir=tmp_path)

    artifacts = list(tmp_path.glob("llm_extraction_claude_*.json"))
    assert len(artifacts) == 1
    saved = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert saved["provider"] == "claude"
    assert json.loads(saved["response"]) == SAMPLE_EXTRACTED


@pytest.mark.asyncio
async def test_extract_json_without_debug_dir_saves_nothing(httpx_mock, tmp_path) -> None:
    httpx_mock.add_response(method="POST", url=ANTHROPIC_MESSAGES_URL, json=_anthropic_response(json.dumps(SAMPLE_EXTRACTED)))
    svc = LLMService(_settings(extraction_llm_model="claude"))

    await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)

    assert list(tmp_path.iterdir()) == []


# ══════════════════════════════════════════════════════════════════════════════
# Per-call audit log (get_call_log()) — feeds the llm_calls DB table
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_call_log_records_successful_claude_call(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", url=ANTHROPIC_MESSAGES_URL, json=_anthropic_response(json.dumps(SAMPLE_EXTRACTED)))
    svc = LLMService(_settings(extraction_llm_model="claude"))

    await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA, job_url="https://example.com/job/1")

    log = svc.get_call_log()
    assert len(log) == 1
    call = log[0]
    assert call["provider"] == "claude"
    assert call["model"] == "claude-sonnet-4-6"
    assert call["prompt"].startswith("Extract the following data")
    assert json.loads(call["response"]) == SAMPLE_EXTRACTED
    assert call["prompt_chars"] == len(call["prompt"])
    assert call["response_chars"] == len(call["response"])
    assert call["input_tokens"] == 10
    assert call["output_tokens"] == 5
    assert call["success"] is True
    assert call["error_message"] is None
    assert call["retry_count"] == 0
    assert call["latency_ms"] >= 0
    assert call["job_url"] == "https://example.com/job/1"


@pytest.mark.asyncio
async def test_call_log_records_successful_ollama_call(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:11434/api/generate",
        json={"response": json.dumps(SAMPLE_EXTRACTED), "done": True, "prompt_eval_count": 42, "eval_count": 7},
    )
    svc = LLMService(_settings(extraction_llm_model="ollama"))

    await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)

    call = svc.get_call_log()[0]
    assert call["provider"] == "ollama"
    assert call["input_tokens"] == 42
    assert call["output_tokens"] == 7
    assert call["success"] is True
    assert call["job_url"] is None


@pytest.mark.asyncio
async def test_call_log_records_successful_openrouter_call(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST", url=OPENROUTER_URL,
        json=_openrouter_response(json.dumps(SAMPLE_EXTRACTED), prompt_tokens=42, completion_tokens=7),
    )
    svc = LLMService(_settings(extraction_llm_model="openrouter"))

    await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)

    call = svc.get_call_log()[0]
    assert call["provider"] == "openrouter"
    assert call["model"] == "google/gemma-3-27b-it:free"
    assert call["input_tokens"] == 42
    assert call["output_tokens"] == 7
    assert call["success"] is True
    assert call["job_url"] is None


@pytest.mark.asyncio
async def test_call_log_records_failure_on_invalid_json(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", url=ANTHROPIC_MESSAGES_URL, json=_anthropic_response("not json"))
    svc = LLMService(_settings(extraction_llm_model="claude"))

    with pytest.raises(LLMError):
        await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)

    call = svc.get_call_log()[0]
    assert call["success"] is False
    assert call["error_message"] is not None
    assert call["response"] == "not json"  # raw text is still recorded even though parsing failed


@pytest.mark.asyncio
async def test_call_log_records_failure_and_retry_count_on_connection_error(httpx_mock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("connection refused"), is_reusable=True)
    svc = LLMService(_settings(extraction_llm_model="ollama"))

    with pytest.raises(LLMError):
        await svc.extract_json(content="content", schema_description=SAMPLE_SCHEMA)

    call = svc.get_call_log()[0]
    assert call["success"] is False
    assert call["response"] is None
    assert "unreachable" in call["error_message"]
    assert call["retry_count"] == 2  # stop_after_attempt(3) -> 3 attempts -> 2 retries


@pytest.mark.asyncio
async def test_call_log_accumulates_across_multiple_calls(httpx_mock) -> None:
    httpx_mock.add_response(method="POST", url=ANTHROPIC_MESSAGES_URL, json=_anthropic_response(json.dumps(SAMPLE_EXTRACTED)))
    httpx_mock.add_response(method="POST", url=ANTHROPIC_MESSAGES_URL, json=_anthropic_response(json.dumps(SAMPLE_EXTRACTED)))
    svc = LLMService(_settings(extraction_llm_model="claude"))

    await svc.extract_json(content="content-1", schema_description=SAMPLE_SCHEMA)
    await svc.extract_json(content="content-2", schema_description=SAMPLE_SCHEMA)

    assert len(svc.get_call_log()) == 2
