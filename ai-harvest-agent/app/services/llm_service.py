"""Claude wrapper with tool-use support, plus a local-LLM (Ollama) extraction path."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import anthropic
import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import Settings
from app.core.exceptions import LLMError

logger = structlog.get_logger(__name__)

_PROVIDER_CLAUDE = "claude"
_PROVIDER_OLLAMA = "ollama"

# Ollama can be slow on CPU-only hosts — generous timeout vs. Claude's default.
_LOCAL_LLM_TIMEOUT_S = 120.0


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
    """Anthropic Claude client with retry logic and tool-use support."""

    def __init__(self, settings: Settings) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model
        self._max_tokens = settings.anthropic_max_tokens
        self._temperature = settings.anthropic_temperature
        self._extraction_llm_model = settings.extraction_llm_model
        self._local_llm_url = settings.local_llm_url
        self._local_llm_model = settings.local_llm_model
        self._debug_seq = 0

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
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
                "model": model or self._model,
                "max_tokens": self._max_tokens,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system
            if tools:
                kwargs["tools"] = tools
            response = await self._client.messages.create(**kwargs)
            logger.debug(
                "llm_response",
                stop_reason=response.stop_reason,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            return response
        except anthropic.APIError as exc:
            raise LLMError(f"Anthropic API error: {exc}") from exc

    async def complete_text(self, prompt: str, system: str = "", model: str | None = None) -> str:
        """Convenience wrapper returning plain text."""
        response = await self.complete(
            messages=[LLMMessage.user(prompt)],
            system=system,
            model=model,
        )
        for block in response.content:
            if block.type == "text":
                return block.text
        return ""

    # ── Provider selection ────────────────────────────────────────────────────

    def _resolve_extraction_target(self) -> tuple[str, str]:
        """
        Decide which provider/model extract_json() should call, driven entirely
        by EXTRACTION_LLM_MODEL (with LOCAL_LLM_URL / LOCAL_LLM_MODEL as the
        local-LLM connection details):

          ""            -> Claude, using the default anthropic_model
          "claude"      -> Claude, using the default anthropic_model
          "claude-*"    -> Claude, using that specific model id
          "ollama"      -> local LLM at local_llm_url, model = local_llm_model
          anything else -> local LLM at local_llm_url, using that value as the model name
        """
        raw = (self._extraction_llm_model or "").strip()
        lowered = raw.lower()
        if not raw or lowered == _PROVIDER_CLAUDE:
            return _PROVIDER_CLAUDE, self._model
        if lowered.startswith("claude-"):
            return _PROVIDER_CLAUDE, raw
        if lowered == _PROVIDER_OLLAMA:
            return _PROVIDER_OLLAMA, self._local_llm_model
        return _PROVIDER_OLLAMA, raw

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def _complete_text_local(self, prompt: str, system: str, model: str) -> str:
        """Call a locally-deployed Ollama model and return its plain-text response."""
        url = f"{self._local_llm_url.rstrip('/')}/api/generate"
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        }
        if system:
            payload["system"] = system

        logger.info("local_llm_request_started", url=url, model=model, prompt_chars=len(prompt))
        try:
            async with httpx.AsyncClient(timeout=_LOCAL_LLM_TIMEOUT_S) as client:
                response = await client.post(url, json=payload)
            response.raise_for_status()
        except httpx.ConnectError as exc:
            logger.error("local_llm_connection_failed", url=url, model=model, error=str(exc))
            raise LLMError(f"Local LLM unreachable at {url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            logger.error("local_llm_timeout", url=url, model=model, timeout_s=_LOCAL_LLM_TIMEOUT_S)
            raise LLMError(f"Local LLM ({model}) at {url} timed out after {_LOCAL_LLM_TIMEOUT_S}s") from exc
        except httpx.HTTPStatusError as exc:
            logger.error(
                "local_llm_http_error", url=url, model=model,
                status=exc.response.status_code, body=exc.response.text[:300],
            )
            raise LLMError(f"Local LLM returned HTTP {exc.response.status_code}: {exc.response.text[:300]}") from exc

        data = response.json()
        logger.info(
            "local_llm_request_succeeded", url=url, model=model,
            eval_count=data.get("eval_count"), total_duration_ns=data.get("total_duration"),
        )
        return data.get("response", "")

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
    ) -> dict[str, Any]:
        """
        Ask the configured extraction LLM (Claude or a local Ollama model,
        selected via EXTRACTION_LLM_MODEL) to extract structured JSON from
        arbitrary content. Returns the same dict shape regardless of provider.
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

        try:
            if provider == _PROVIDER_OLLAMA:
                text = await self._complete_text_local(prompt, system, model)
            else:
                text = await self.complete_text(prompt, system=system, model=model)
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
        return parsed

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
