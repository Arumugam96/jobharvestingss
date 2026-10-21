"""Standalone checks for the LLM configured in .env.

Usage:
    python check_local_llm.py                      # health check (default)
    python check_local_llm.py email                # every prompt variant x every sample job
    python check_local_llm.py email --variant short --job naukri_new --runs 3
    python check_local_llm.py email --show-prompt  # also print the prompts sent

Health check — reads LOCAL_LLM_URL / LOCAL_LLM_MODEL from .env (via app.config.Settings) and:
  1. Pings the Ollama server (GET /api/tags) to confirm it's up.
  2. Confirms the configured model is pulled/available.
  3. Runs a tiny real generation against /api/generate.

Email test — generates outreach emails with different prompt tunings (see VARIANTS /
build_variant) and prints each one on screen, assembled exactly like
OutreachService.generate_email does it (platform fix → greeting + intro + closing), plus
a few quick quality checks. It goes through LLMService, so it uses the same provider as
production (EXTRACTION_LLM_MODEL: local LLM / OpenRouter / Claude). To try a new tuning,
add a name to VARIANTS and a branch for it in build_variant.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time

import httpx

from app.config import get_settings
from app.prompts import outreach_prompts
from app.services.llm_service import LLMService
from app.services.outreach_service import OutreachService


def check_health() -> int:
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


# ── Email prompt-tuning test ─────────────────────────────────────────────────

# Sample jobs: (client_type, job dict). Covers each platform, an existing client, and an
# unknown company/poster/source (the neutral-wording path).
SAMPLE_JOBS: dict[str, tuple[str, dict]] = {
    "naukri_new": ("new", {
        "company": "Acme Technologies",
        "job_title": "Senior Data Engineer",
        "source": "Naukri",
        "job_url": "https://www.naukri.com/job-listings-senior-data-engineer-123",
        "job_poster_name": "Priya Sharma",
    }),
    "linkedin_active": ("active", {
        "company": "Globex Corporation",
        "job_title": "Azure DevOps Engineer",
        "source": "LinkedIn",
        "job_url": "https://www.linkedin.com/jobs/view/456",
        "job_poster_name": "Rahul Verma",
    }),
    "dice_new": ("new", {
        "company": "Initech",
        "job_title": "Java Full Stack Developer (Spring Boot, React)",
        "source": "Dice",
        "job_url": "https://www.dice.com/job-detail/789",
        "job_poster_name": "",
    }),
    "unknown": ("unknown", {
        "company": "",
        "job_title": "SAP FICO Consultant",
        "source": "",
        "job_url": "",
        "job_poster_name": "",
    }),
}

# Prompt tunings to compare. "baseline" is exactly what production sends today.
VARIANTS = (
    "baseline",       # production system + user prompt, Formal tone
    "friendly",       # same prompts, Friendly tone
    "direct",         # same prompts, Direct tone
    "short",          # baseline + hard cap of ~50 words
    "no_references",  # system prompt with the EXAMPLE email removed
    "regenerate",     # baseline + the "distinctly different variation" suffix (UI Regenerate)
    "custom",         # CUSTOM_SYSTEM_PROMPT below — edit it freely to try your own wording
)

# Edit this to try a hand-written system prompt (used by the "custom" variant).
CUSTOM_SYSTEM_PROMPT = outreach_prompts.EMAIL_SYSTEM_PROMPT


def _without_style_references(system: str) -> str:
    """EMAIL_SYSTEM_PROMPT minus its EXAMPLE block (up to KEEP IN MIND:)."""
    return re.sub(r"EXAMPLE \(.*?(?=KEEP IN MIND:)", "", system, flags=re.S)


def build_variant(variant: str, client_type: str, job: dict, sender_email: str) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for one prompt tuning."""
    system = outreach_prompts.EMAIL_SYSTEM_PROMPT
    if variant == "baseline":
        user = outreach_prompts.build_email_prompt(client_type, "Formal", job, sender_email)
    elif variant == "friendly":
        user = outreach_prompts.build_email_prompt(client_type, "Friendly", job, sender_email)
    elif variant == "direct":
        user = outreach_prompts.build_email_prompt(client_type, "Direct", job, sender_email)
    elif variant == "short":
        user = outreach_prompts.build_email_prompt(client_type, "Formal", job, sender_email)
        user += "\n\nKeep the whole body under 50 words — one sentence per paragraph."
    elif variant == "no_references":
        system = _without_style_references(system)
        user = outreach_prompts.build_email_prompt(client_type, "Formal", job, sender_email)
    elif variant == "regenerate":
        user = outreach_prompts.build_email_prompt(client_type, "Formal", job, sender_email)
        user += "\n\nProduce a distinctly different variation from any previous version."
    elif variant == "custom":
        system = CUSTOM_SYSTEM_PROMPT
        user = outreach_prompts.build_email_prompt(client_type, "Formal", job, sender_email)
    else:
        raise ValueError(f"unknown variant {variant!r} — choose from {', '.join(VARIANTS)}")
    return system, user


def _quality_checks(pitch: str, job: dict) -> list[tuple[str, bool]]:
    """Quick pass/fail checks on the LLM-written body (before the closing is added)."""
    title = (job.get("job_title") or "").strip()
    platform = outreach_prompts.job_platform(job)
    others = [p for p in outreach_prompts.PLATFORM_NAMES if p != platform]
    words = len(pitch.split())
    lower = pitch.lower()
    forbidden = others if platform else outreach_prompts.PLATFORM_NAMES
    return [
        (f"job title verbatim ({title!r})", not title or title in pitch),
        ("no [bracket] placeholders", not re.search(r"\[[^\]]+\]", pitch)),
        ("no greeting written by model", not re.match(r"\s*(hi|hello|hey|dear)\b", lower)),
        ("no self-intro / sign-off",
         not outreach_prompts._SELF_INTRO_RE.match(pitch.strip()) and "regards" not in lower),
        (f"no wrong platform (job is {platform or 'unknown'})",
         not any(re.search(rf"\b{p.lower()}\b", lower) for p in forbidden)),
        ("no 'SightSpectrum' in body (intro already names it)", "sightspectrum" not in lower),
        ("no 'contract (IT) professionals'", not re.search(r"contract (it )?professionals", lower)),
        ("no 'next week'", "next week" not in lower),
        (f"length 40-90 words (got {words})", 40 <= words <= 90),
    ]


async def _run_one(llm: LLMService, variant: str, job_key: str, sender_email: str, show_prompt: bool) -> bool:
    client_type, job = SAMPLE_JOBS[job_key]
    system, user = build_variant(variant, client_type, job, sender_email)

    print("=" * 80)
    print(f"VARIANT: {variant}   JOB: {job_key}   (client_type={client_type})")
    print("=" * 80)
    if show_prompt:
        print("--- system prompt ---")
        print(system.strip())
        print("--- user prompt ---")
        print(user.strip())
        print()

    t0 = time.monotonic()
    try:
        text, in_tok, out_tok, provider, model = await llm.generate_text(user, system, json_mode=True)
    except Exception as exc:
        print(f"FAILED: LLM call raised: {exc}\n")
        return False
    elapsed = time.monotonic() - t0
    print(f"provider={provider} model={model} tokens={in_tok}/{out_tok} time={elapsed:.1f}s")

    parsed = OutreachService._parse_email_json(text)
    if not parsed:
        print("FAILED: response was not valid {subject, body} JSON — production would use the fallback.")
        print("--- raw response ---")
        print(text)
        print()
        return False

    llm_subject, pitch = parsed
    fixed_pitch, corrections = outreach_prompts.correct_platform_mentions(pitch, job)

    print(f"\nLLM subject (ignored in prod): {llm_subject}")
    print("\n--- LLM body (raw) ---")
    print(pitch)
    if corrections:
        print(f"\n(platform post-check rewrote {corrections} mention(s))")

    # Same assembly as OutreachService.generate_email.
    subject = outreach_prompts.build_email_subject(job)
    body = outreach_prompts.append_closing(
        fixed_pitch, sender_email,
        intro=outreach_prompts.build_intro(sender_email, job),
        greeting=outreach_prompts.build_greeting(job),
    )
    print("\n--- FINAL EMAIL ---")
    print(f"Subject: {subject}\n")
    print(body)

    print("\n--- checks ---")
    all_ok = True
    for label, ok in _quality_checks(fixed_pitch, job):
        all_ok &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print()
    return all_ok


async def email_test(variants: list[str], job_keys: list[str], runs: int, show_prompt: bool) -> int:
    settings = get_settings()
    llm = LLMService(settings)
    provider, model = llm.resolve_target()
    sender_email = outreach_prompts.ACTIVE_SENDER_EMAIL
    print(f"Generating with provider={provider} model={model} (EXTRACTION_LLM_MODEL={settings.extraction_llm_model!r})")
    print(f"Variants: {', '.join(variants)}   Jobs: {', '.join(job_keys)}   Runs each: {runs}\n")

    results: list[tuple[str, str, int, bool]] = []
    for variant in variants:
        for job_key in job_keys:
            for run in range(1, runs + 1):
                ok = await _run_one(llm, variant, job_key, sender_email, show_prompt)
                results.append((variant, job_key, run, ok))

    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    for variant, job_key, run, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {variant:<14} {job_key:<16} run {run}")
    passed = sum(ok for *_, ok in results)
    print(f"\n{passed}/{len(results)} generations passed every check.")
    return 0 if passed == len(results) else 1


def main() -> int:
    # Subjects/bodies contain em-dashes and curly quotes; the Windows console defaults to cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", nargs="?", default="health", choices=("health", "email"))
    parser.add_argument("--variant", choices=VARIANTS, action="append",
                        help="prompt variant to test (repeatable; default: all)")
    parser.add_argument("--job", choices=tuple(SAMPLE_JOBS), action="append",
                        help="sample job to use (repeatable; default: all)")
    parser.add_argument("--runs", type=int, default=1, help="generations per variant/job pair")
    parser.add_argument("--show-prompt", action="store_true", help="print the system + user prompts")
    args = parser.parse_args()

    if args.mode == "email":
        return asyncio.run(email_test(
            args.variant or list(VARIANTS), args.job or list(SAMPLE_JOBS), max(1, args.runs), args.show_prompt,
        ))
    else:
        return check_health()


if __name__ == "__main__":
    sys.exit(main())
