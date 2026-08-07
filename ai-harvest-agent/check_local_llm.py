"""Standalone check that the local LLM (Ollama) configured in .env is reachable.

Usage:
    python check_local_llm.py

Reads LOCAL_LLM_URL / LOCAL_LLM_MODEL from .env (via app.config.Settings) and:
  1. Pings the Ollama server (GET /api/tags) to confirm it's up.
  2. Confirms the configured model is pulled/available.
  3. Runs a tiny real generation against /api/generate.
"""
from __future__ import annotations

import sys

import httpx

from app.config import get_settings


def main() -> int:
    settings = get_settings()
    url = settings.local_llm_url.rstrip("/")
    model = settings.local_llm_model

    print(f"LOCAL_LLM_URL   = {url}")
    print(f"LOCAL_LLM_MODEL = {model}")
    print(f"EXTRACTION_LLM_MODEL = {settings.extraction_llm_model!r} "
          f"({'routes to Ollama' if settings.extraction_llm_model.lower() not in ('', 'claude') and not settings.extraction_llm_model.lower().startswith('claude-') else 'routes to Claude, not Ollama'})")
    print()

    # 1. Server reachable?
    print(f"[1/3] Checking Ollama server at {url} ...")
    try:
        resp = httpx.get(f"{url}/api/tags", timeout=90.0)
        resp.raise_for_status()
    except httpx.ConnectError as exc:
        print(f"FAILED: could not connect to Ollama at {url}: {exc}")
        print("        Is Ollama running? Try: ollama serve")
        return 1
    except httpx.HTTPError as exc:
        print(f"FAILED: Ollama responded with an error: {exc}")
        return 1
    print("OK: Ollama server is up.")

    # 2. Model available?
    print(f"\n[2/3] Checking that model '{model}' is pulled ...")
    tags = resp.json().get("models", [])
    available = [m.get("name") for m in tags]
    if model not in available:
        print(f"FAILED: '{model}' not found. Available models: {available or '(none)'}")
        print(f"        Pull it with: ollama pull {model}")
        return 1
    print(f"OK: '{model}' is available.")

    # 3. Real generation call.
    print(f"\n[3/3] Running a test generation against {url}/api/generate ...")
    payload = {
        "model": model,
        "prompt": "Reply with exactly one word: OK",
        "stream": False,
    }
    try:
        resp = httpx.post(f"{url}/api/generate", json=payload, timeout=120.0)
        resp.raise_for_status()
    except httpx.TimeoutException:
        print("FAILED: generation request timed out after 120s.")
        return 1
    except httpx.HTTPError as exc:
        print(f"FAILED: generation request failed: {exc}")
        return 1

    text = resp.json().get("response", "").strip()
    print(f"OK: model responded: {text!r}")
    print("\nLocal LLM is configured correctly and responding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
