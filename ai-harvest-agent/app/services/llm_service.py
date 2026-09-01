"""LLM wrapper with tool-use support for claude, local-LLM (Ollama) and OpenRouter
extraction paths."""
from __future__ import annotations

import json
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
import httpx
import structlog
from tenacity import (
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings
from app.core.exceptions import LLMError, LLMUnavailableError

logger = structlog.get_logger(__name__)

_PROVIDER_CLAUDE = "claude"
_PROVIDER_OLLAMA = "ollama"
_PROVIDER_OPENROUTER = "openrouter"
_LOCAL_LLM_TIMEOUT_S = 500.0
_OPENROUTER_TIMEOUT_S = 90.0
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Tracks tenacity's attempt count for the retry-decorated call made by the
# current extract_json() invocation. A ContextVar (not an instance attribute)
# keeps this race-free across concurrent asyncio tasks (LinkedIn/Naukri/Dice
# scrape concurrently).
_retry_attempts: ContextVar[int] = ContextVar("llm_retry_attempts", default=0)


def _track_attempt(retry_state: Any) -> None:
    _retry_attempts.set(retry_state.attempt_number)


# ── Token usage tracking ──────────────────────────────────────────────────────

@dataclass
class _ProviderUsage:
    """Cumulative token counters for one provider (Claude or Ollama or openrouter)."""
    calls:         int = 0
    input_tokens:  int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "calls":         self.calls,
            "input_tokens":  self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens":  self.total_tokens,
        }


def empty_usage_summary() -> dict[str, dict[str, int]]:
    """Zeroed usage summary shape — used by callers that never touched an
    LLMService instance (e.g. a harvest run that hit no LLM fallback)."""
    zero = _ProviderUsage().as_dict()
    return {
        "claude": dict(zero),
        "ollama": dict(zero),
        "openrouter": dict(zero),
        "total": dict(zero),
    }


# ── Tool definitions the LLM can call ────────────────────────────────────────────

HARVEST_TOOLS: list[dict[str, Any]] = [
    {
        "name": "navigate",
        "description": "Navigate the browser to a URL and get the page content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to navigate to"},
                "wait_until": {
                    "type": "string",
                    "enum": ["load", "networkidle", "domcontentloaded"],
                    "description": "When to consider navigation complete",
                    "default": "networkidle",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "click",
        "description": "Click an element on the current page by CSS selector.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector of the element to click"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "extract_data",
        "description": "Extract structured data from the current page content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "schema": {
                    "type": "object",
                    "description": "JSON schema describing the data to extract",
                },
                "instructions": {
                    "type": "string",
                    "description": "Natural language extraction instructions",
                },
            },
            "required": ["instructions"],
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the page to load more content (infinite scroll).",
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Number of scroll actions", "default": 3},
            },
        },
    },
    {
        "name": "fill_form",
        "description": "Fill a form and submit it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "object",
                    "description": "Mapping of CSS selector → value",
                },
                "submit_selector": {
                    "type": "string",
                    "description": "CSS selector of the submit button",
                },
            },
            "required": ["fields", "submit_selector"],
        },
    },
    {
        "name": "finish",
        "description": "Signal that harvesting is complete and return the final structured result.",
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {
                    "type": "object",
                    "description": "The final harvested and structured data",
                },
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what was harvested",
                },
                "pages_visited": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of URLs visited",
                },
            },
            "required": ["data", "summary"],
        },
    },
]


class LLMMessage:
    """Helper to build messages list."""

    @staticmethod
    def user(content: str) -> dict[str, Any]:
        return {"role": "user", "content": content}

    @staticmethod
    def assistant(content: str) -> dict[str, Any]:
        return {"role": "assistant", "content": content}

    @staticmethod
    def tool_result(tool_use_id: str, content: str) -> dict[str, Any]:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                }
            ],
        }


class LLMService:
    """Anthropic Claude/openrouter/LocalLLM client with retry logic and tool-use support."""

    def __init__(self, settings: Settings) -> None:
        self.anthropic_client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.anthropic_model = settings.anthropic_model
        self.anthropic_max_tokens = settings.anthropic_max_tokens
        self.anthropic_temperature = settings.anthropic_temperature
        self._extraction_llm_model = settings.extraction_llm_model
        self._local_llm_url = settings.local_llm_url
        self._local_llm_model = settings.local_llm_model
        self._openrouter_api_key = settings.openrouter_api_key
        self._openrouter_model = settings.openrouter_model
        self._debug_seq = 0
        self._usage: dict[str, _ProviderUsage] = {
            _PROVIDER_CLAUDE: _ProviderUsage(),
            _PROVIDER_OLLAMA: _ProviderUsage(),
            _PROVIDER_OPENROUTER: _ProviderUsage(),
        }
        self._call_log: list[dict[str, Any]] = []

    def get_call_log(self) -> list[dict[str, Any]]:
        """Per-call audit log recorded by extract_json() so far (this instance's
        lifetime) — provider, model, prompt/response text, tokens, latency,
        success/error, retry count. Independent of the file-based debug
        artifact, which only exists when a debug_dir is passed."""
        return list(self._call_log)

    def _record_usage(self, provider: str, input_tokens: int, output_tokens: int) -> None:
        bucket = self._usage.setdefault(provider, _ProviderUsage())
        bucket.calls += 1
        bucket.input_tokens += input_tokens or 0
        bucket.output_tokens += output_tokens or 0

    def get_usage_summary(self) -> dict[str, dict[str, int]]:
        """Cumulative token usage recorded by this LLMService instance so far,
        broken down per provider (claude / ollama / openrouter) plus a combined
        total. Scoped to this instance's lifetime — callers that create one
        LLMService per harvest run get a per-run total for free."""
        claude = self._usage.get(_PROVIDER_CLAUDE, _ProviderUsage())
        ollama = self._usage.get(_PROVIDER_OLLAMA, _ProviderUsage())
        openrouter = self._usage.get(_PROVIDER_OPENROUTER, _ProviderUsage())
        return {
            "claude": claude.as_dict(),
            "ollama": ollama.as_dict(),
            "openrouter": openrouter.as_dict(),
            "total": {
                "calls":         claude.calls + ollama.calls + openrouter.calls,
                "input_tokens":  claude.input_tokens + ollama.input_tokens + openrouter.input_tokens,
                "output_tokens": claude.output_tokens + ollama.output_tokens + openrouter.output_tokens,
                "total_tokens":  claude.total_tokens + ollama.total_tokens + openrouter.total_tokens,
            },
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_not_exception_type(LLMUnavailableError),
        reraise=True,
        after=_track_attempt,
    )
    async def complete(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> anthropic.types.Message:
        """Call Claude and return the raw Message."""
        try:
            kwargs: dict[str, Any] = {
                "model": model or self.anthropic_model,
                "max_tokens": self.anthropic_max_tokens,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system
            if tools:
                kwargs["tools"] = tools
            response = await self.anthropic_client.messages.create(**kwargs)
            self._record_usage(
                _PROVIDER_CLAUDE,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
            logger.debug(
                "llm_response",
                stop_reason=response.stop_reason,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            return response
        except anthropic.APIError as exc:
            # Provider-level failure — surface the raw Anthropic error and stop
            # everything that depends on the LLM (see LLMUnavailableError).
            raise LLMUnavailableError(f"Anthropic API error: {exc}") from exc

    async def complete_text(self, prompt: str, system: str = "", model: str | None = None) -> str:
        """Convenience wrapper returning plain text, routed through the centrally
        configured provider (EXTRACTION_LLM_MODEL) — see generate_text(). `model`
        overrides the Claude model id only (ignored for local/OpenRouter)."""
        text, _in, _out, _provider, anthropic_model = await self.generate_text(
            prompt, system, json_mode=False, model=model,
        )
        return text

    async def generate_text(
        self,
        prompt: str,
        system: str = "",
        *,
        json_mode: bool = False,
        model: str | None = None,
    ) -> tuple[str, int, int, str, str]:
        """Free-text (or JSON) generation routed through the centrally configured
        provider — Returns (text, input_tokens, output_tokens, provider, model). Set
        json_mode=True to ask the provider for a JSON object (Ollama format=json /
        OpenRouter response_format); leave False for plain text. `model` overrides
        only the Claude model id (local/OpenRouter models come from config)."""
        provider, resolved = self.resolve_target()
        if provider == _PROVIDER_OLLAMA:
            text, input_tokens, output_tokens = await self._complete_text_local(
                prompt, system, resolved, json_mode=json_mode
            )
        elif provider == _PROVIDER_OPENROUTER:
            text, input_tokens, output_tokens = await self._complete_text_openrouter(
                prompt, system, resolved, json_mode=json_mode
            )
        else:
            resolved = model or resolved
            response = await self.complete(
                messages=[LLMMessage.user(prompt)], system=system, model=resolved
            )
            text = self.get_text(response)
            input_tokens, output_tokens = response.usage.input_tokens, response.usage.output_tokens
        return text, input_tokens, output_tokens, provider, resolved

    # ── Provider selection ────────────────────────────────────────────────────

    def resolve_target(self) -> tuple[str, str]:
        """Public accessor for the centrally configured (provider, model) pair —
        the same selection extraction and generation use. Callers (e.g.
        OutreachService) use it to stamp the provider/model on an audit row even
        when the call itself fails before generate_text() can report them back."""
        return self._resolve_extraction_target()

    def _resolve_extraction_target(self) -> tuple[str, str]:
        """
        Decide which provider/model extract_json() should call, driven entirely
        by EXTRACTION_LLM_MODEL (with LOCAL_LLM_URL / LOCAL_LLM_MODEL as the
        local-LLM connection details, and OPENROUTER_MODEL as the OpenRouter
        default):

          ""                          -> Claude, using the default anthropic_model
          "claude"                    -> Claude, using the default anthropic_model
          "claude-*"                  -> Claude, using that specific model id
          "ollama"                    -> local LLM at local_llm_url, model = local_llm_model
          "openrouter"                -> OpenRouter, model = openrouter_model
          "openrouter/<model-id>"     -> OpenRouter, using that model id directly
                                          (OpenRouter model ids themselves contain
                                          slashes, e.g. "google/gemma-3-27b-it:free")
          anything else               -> local LLM at local_llm_url, using that value
                                          as the model name
        """
        raw = (self._extraction_llm_model or "").strip()
        lowered = raw.lower()
        if not raw or lowered == _PROVIDER_CLAUDE:
            return _PROVIDER_CLAUDE, self.anthropic_model
        if lowered == _PROVIDER_OLLAMA:
            return _PROVIDER_OLLAMA, self._local_llm_model
        if lowered == _PROVIDER_OPENROUTER:
            return _PROVIDER_OPENROUTER, self._openrouter_model
        return _PROVIDER_OLLAMA, raw

    def _local_llm_unavailable_msg(self, model: str, url: str, reason: str) -> str:
        name = self._local_llm_model or model
        return (
            f"SightSpectrum's Local LLM '{name}' at is unavailable ({reason}) — the server "
            f"may be shut down. Contact the admin team."
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        # A down/misconfigured local server won't recover within the backoff —
        # fail fast instead of retrying (esp. the 300s timeout, 3×).
        retry=retry_if_not_exception_type(LLMUnavailableError),
        reraise=True,
        after=_track_attempt,
    )
    async def _complete_text_local(
        self, prompt: str, system: str, model: str, json_mode: bool = True
    ) -> tuple[str, int, int]:
        """Call a locally-deployed Ollama model. Returns (text, prompt_eval_count, eval_count).

        json_mode=True forces Ollama's JSON output (format=json) — correct for
        extraction and the outreach email. Pass False for free-text generation
        (harvest strategy planning, LinkedIn messages) so the model isn't coerced
        into emitting JSON."""
        url = f"{self._local_llm_url.rstrip('/')}/api/generate"
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
        if json_mode:
            payload["format"] = "json"
        if system:
            payload["system"] = system

        logger.info("local_llm_request_started", url=url, model=model, prompt_chars=len(prompt))
        try:
            async with httpx.AsyncClient(timeout=_LOCAL_LLM_TIMEOUT_S) as client:
                response = await client.post(url, json=payload)
            response.raise_for_status()
        except httpx.ConnectError as exc:
            logger.error("local_llm_connection_failed", url=url, model=model, error=str(exc))
            raise LLMUnavailableError(self._local_llm_unavailable_msg(model, url, "connection refused")) from exc
        except httpx.TimeoutException as exc:
            logger.error("local_llm_timeout", url=url, model=model, timeout_s=_LOCAL_LLM_TIMEOUT_S)
            raise LLMUnavailableError(
                self._local_llm_unavailable_msg(model, url, f"timed out after {_LOCAL_LLM_TIMEOUT_S}s")
            ) from exc
        except httpx.HTTPStatusError as exc:
            logger.error(
                "local_llm_http_error", url=url, model=model,
                status=exc.response.status_code, body=exc.response.text[:300],
            )
            raise LLMUnavailableError(
                self._local_llm_unavailable_msg(model, url, f"HTTP {exc.response.status_code}")
            ) from exc

        data = response.json()
        prompt_eval_count = data.get("prompt_eval_count", 0)
        eval_count = data.get("eval_count", 0)
        self._record_usage(_PROVIDER_OLLAMA, prompt_eval_count, eval_count)
        logger.info(
            "local_llm_request_succeeded", url=url, model=model,
            prompt_eval_count=prompt_eval_count,
            eval_count=eval_count, total_duration_ns=data.get("total_duration"),
        )
        return data.get("response", ""), prompt_eval_count, eval_count

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        # Provider down/unavailable → fail fast rather than retrying.
        retry=retry_if_not_exception_type(LLMUnavailableError),
        reraise=True,
        after=_track_attempt,
    )
    async def _complete_text_openrouter(
        self, prompt: str, system: str, model: str, json_mode: bool = True
    ) -> tuple[str, int, int]:
        """Call an OpenRouter chat-completions model (OpenAI-compatible REST API).
        Returns (text, prompt_tokens, completion_tokens).

        json_mode=True requests a JSON object (response_format) — correct for
        extraction and the outreach email. Pass False for free-text generation."""
        if not self._openrouter_api_key:
            raise LLMUnavailableError("OpenRouter extraction requested but OPENROUTER_API_KEY is not set")

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self._openrouter_api_key}",
            "Content-Type": "application/json",
        }

        logger.info("openrouter_request_started", model=model, prompt_chars=len(prompt))
        try:
            async with httpx.AsyncClient(timeout=_OPENROUTER_TIMEOUT_S) as client:
                response = await client.post(_OPENROUTER_URL, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.ConnectError as exc:
            logger.error("openrouter_connection_failed", model=model, error=str(exc))
            raise LLMUnavailableError(f"OpenRouter unreachable: {exc}") from exc
        except httpx.TimeoutException as exc:
            logger.error("openrouter_timeout", model=model, timeout_s=_OPENROUTER_TIMEOUT_S)
            raise LLMUnavailableError(f"OpenRouter ({model}) timed out after {_OPENROUTER_TIMEOUT_S}s") from exc
        except httpx.HTTPStatusError as exc:
            logger.error(
                "openrouter_http_error", model=model,
                status=exc.response.status_code, body=exc.response.text[:300],
            )
            raise LLMUnavailableError(
                f"OpenRouter returned HTTP {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc

        data = response.json()
        if data.get("error"):
            # OpenRouter can return HTTP 200 with an inline error object (e.g. the
            # requested model/provider is unavailable) — surface it as a failure
            # rather than passing an empty completion on to JSON parsing.
            logger.error("openrouter_inline_error", model=model, error=data["error"])
            raise LLMUnavailableError(f"OpenRouter error: {data['error']}")

        choices = data.get("choices") or []
        text = choices[0]["message"].get("content", "") if choices else ""
        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        self._record_usage(_PROVIDER_OPENROUTER, prompt_tokens, completion_tokens)
        logger.info(
            "openrouter_request_succeeded", model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        )
        return text or "", prompt_tokens, completion_tokens

    def _save_extraction_debug_artifact(
        self, debug_dir: str | Path | None, provider: str, model: str, prompt: str, response_text: str,
    ) -> None:
        """Persist the raw request/response for a caller-supplied debug directory
        (e.g. data/debug/linkedin). No-op when debug_dir is not provided, so
        callers that don't need this (scraper_agent, harvest_agent) are unaffected."""
        if not debug_dir:
            return
        try:
            d = Path(debug_dir)
            d.mkdir(parents=True, exist_ok=True)
            self._debug_seq += 1
            stamp = f"{int(time.time())}_{self._debug_seq:04d}"
            artifact_path = d / f"llm_extraction_{provider}_{stamp}.json"
            artifact_path.write_text(
                json.dumps(
                    {
                        "provider": provider,
                        "model": model,
                        "prompt_chars": len(prompt),
                        "response_chars": len(response_text),
                        "prompt": prompt,
                        "response": response_text,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.debug("llm_extraction_debug_saved", path=str(artifact_path))
        except Exception as exc:
            logger.debug("llm_extraction_debug_save_failed", error=str(exc))

    async def extract_json(
        self,
        content: str,
        schema_description: str,
        system: str = "",
        debug_dir: str | Path | None = None,
        job_url: str | None = None,
        call_type: str = "job_harvest",
    ) -> dict[str, Any]:
        """
        Ask the configured extraction LLM (Claude or a local Ollama model,
        selected via EXTRACTION_LLM_MODEL) to extract structured JSON from
        arbitrary content. Returns the same dict shape regardless of provider.

        job_url is an optional correlation key (e.g. the LinkedIn job this
        call is extracting for) recorded on the call-log entry — see
        get_call_log().

        call_type tags which workflow this call belongs to for the llm_calls
        audit table — one of LlmCallType's values (models.harvest_run). Defaults
        to "job_harvest"; contact-extraction callers pass "contact_harvest".
        Stamped onto the call-log entry so bulk_insert_llm_calls persists it.
        """
        provider, model = self._resolve_extraction_target()
        prompt = (
            f"Extract the following data from the content below.\n\n"
            f"Schema: {schema_description}\n\n"
            f"Content:\n{content}\n\n"
            f"Return only valid JSON, no explanation."
        )
        logger.info(
            "llm_extraction_started", provider=provider, model=model,
            content_chars=len(content), prompt_chars=len(prompt),
        )

        retry_token = _retry_attempts.set(0)
        start = time.monotonic()
        text: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        error_message: str | None = None
        success = False
        try:
            try:
                if provider == _PROVIDER_OLLAMA:
                    text, input_tokens, output_tokens = await self._complete_text_local(prompt, system, model)
                elif provider == _PROVIDER_OPENROUTER:
                    text, input_tokens, output_tokens = await self._complete_text_openrouter(prompt, system, model)
                else:
                    response = await self.complete(messages=[LLMMessage.user(prompt)], system=system, model=model)
                    text = self.get_text(response)
                    input_tokens, output_tokens = response.usage.input_tokens, response.usage.output_tokens
            except LLMError:
                raise
            except Exception as exc:
                logger.error("llm_extraction_request_failed", provider=provider, model=model, error=str(exc))
                raise LLMError(f"{provider} extraction request failed: {exc}") from exc

            logger.info(
                "llm_extraction_response_received", provider=provider, model=model,
                response_chars=len(text),
            )
            self._save_extraction_debug_artifact(debug_dir, provider, model, prompt, text)

            # Strip markdown code fences if present
            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "llm_extraction_json_parse_failed", provider=provider, model=model,
                    error=str(exc), raw_preview=cleaned[:300],
                )
                raise LLMError(f"LLM returned invalid JSON: {exc}") from exc

            logger.info(
                "llm_extraction_succeeded", provider=provider, model=model,
                extracted_fields=list(parsed.keys()),
            )
            success = True
            return parsed
        except LLMError as exc:
            error_message = str(exc)
            raise
        finally:
            self._call_log.append({
                "provider": provider,
                "model": model,
                "prompt": prompt,
                "response": text,
                "prompt_chars": len(prompt),
                "response_chars": len(text) if text else 0,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": int((time.monotonic() - start) * 1000),
                "success": success,
                "error_message": error_message,
                "retry_count": max(0, _retry_attempts.get() - 1),
                "job_url": job_url,
                "call_type": call_type,
            })
            _retry_attempts.reset(retry_token)

    def get_tool_use(
        self, response: anthropic.types.Message
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Return (tool_use_id, tool_name, tool_input) if the model wants to use a tool."""
        for block in response.content:
            if block.type == "tool_use":
                return block.id, block.name, block.input  # type: ignore[union-attr]
        return None

    def get_text(self, response: anthropic.types.Message) -> str:
        """Extract plain text from a response."""
        for block in response.content:
            if block.type == "text":
                return block.text
        return ""
