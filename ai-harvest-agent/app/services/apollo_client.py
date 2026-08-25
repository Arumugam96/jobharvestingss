"""Thin async Apollo.io REST client — isolated so it's easy to mock/swap.

Used only as a last-resort contact-enrichment tier: given a person's LinkedIn
URL, ask Apollo to reveal a verified email (phone optional/async — see below).

Design mirrors the existing httpx idiom in
``llm_service._complete_text_openrouter``: per-call ``httpx.AsyncClient``,
``tenacity`` retry with exponential backoff, ``raise_for_status``, typed
exceptions, and structlog. No new heavy dependencies.

Credits: email/phone reveals cost Apollo credits (we're on the Basic plan), so
every call that reveals data emits an ``apollo_credits_used`` log line. Callers
are responsible for gating/caching (see ``apollo_enrichment.apollo_contact_fallback``).

Phone reveal note: Apollo returns phone numbers *asynchronously* via a webhook,
not in the match response. We pass ``reveal_phone_number`` when asked, but with
no webhook receiver configured phones will not resolve synchronously — only
emails do. Kept off by default (``settings.apollo_reveal_phone``).
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings
from app.core.exceptions import HarvestException

logger = structlog.get_logger(__name__)

# Apollo caps bulk_match at 10 records per request.
_BULK_MAX = 10
# Apollo returns this sentinel address when an email exists but wasn't unlocked
# (no reveal flag / out of credits) — treat it as "no email", never a real hit.
_LOCKED_EMAIL_MARKER = "email_not_unlocked"


class ApolloAPIError(HarvestException):
    """A non-retryable Apollo failure (bad key, permanent 4xx, or a transient
    error that survived all retries). Subclasses HarvestException so the central
    harvest_exception_handler renders it if it ever reaches a route."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message=message,
            code="APOLLO_API_ERROR",
            status_code=502,  # bad gateway — upstream provider
            details=details or {},
        )


class _ApolloRetryable(Exception):
    """Internal-only: transient failure (429 / 5xx / network) that should be
    retried. Never escapes the client — translated to ApolloAPIError if it
    survives the retry budget."""


# ── Response models ───────────────────────────────────────────────────────────

class ApolloOrgResult(BaseModel):
    name: str | None = None
    domain: str | None = None
    website: str | None = None
    industry: str | None = None
    size: int | None = None

    @classmethod
    def from_dict(cls, org: dict[str, Any] | None) -> "ApolloOrgResult | None":
        if not org:
            return None
        return cls(
            name=org.get("name"),
            domain=org.get("primary_domain") or org.get("domain"),
            website=org.get("website_url"),
            industry=org.get("industry"),
            size=org.get("estimated_num_employees"),
        )


class ApolloPersonResult(BaseModel):
    """Normalized Apollo /people match result. `matched=False` represents
    Apollo's 200-with-no-match response (not an error)."""

    matched: bool = False
    id: str | None = None
    name: str | None = None
    title: str | None = None
    linkedin_url: str | None = None
    email: str | None = None
    email_status: str | None = None
    phone: str | None = None
    organization: ApolloOrgResult | None = None

    @classmethod
    def no_match(cls) -> "ApolloPersonResult":
        return cls(matched=False)

    @classmethod
    def from_person(cls, person: dict[str, Any] | None) -> "ApolloPersonResult":
        if not person:
            return cls.no_match()
        return cls(
            matched=True,
            id=person.get("id"),
            name=person.get("name")
            or " ".join(p for p in (person.get("first_name"), person.get("last_name")) if p) or None,
            title=person.get("title"),
            linkedin_url=person.get("linkedin_url"),
            email=_clean_email(person.get("email")),
            email_status=person.get("email_status"),
            phone=_first_phone(person.get("phone_numbers")),
            organization=ApolloOrgResult.from_dict(person.get("organization")),
        )

    @property
    def has_email(self) -> bool:
        return bool(self.email)

    @property
    def has_phone(self) -> bool:
        return bool(self.phone)


def _clean_email(email: str | None) -> str | None:
    """Drop Apollo's locked-email sentinel; return a real address or None."""
    if not email:
        return None
    if _LOCKED_EMAIL_MARKER in email.lower():
        return None
    return email.strip() or None


def _first_phone(phone_numbers: list[dict[str, Any]] | None) -> str | None:
    """Apollo delivers phones asynchronously via webhook, so this is usually
    absent synchronously — but parse the first sanitized number if present."""
    if not phone_numbers:
        return None
    for pn in phone_numbers:
        num = pn.get("sanitized_number") or pn.get("raw_number")
        if num:
            return str(num).strip()
    return None


# ── Client ──────────────────────────────────────────────────────────────────

class ApolloClient:
    """Async wrapper around Apollo's people-match REST API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._api_key = settings.apollo_api_key
        self._base_url = settings.apollo_base_url.rstrip("/")
        self._timeout = settings.apollo_timeout_s

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    # ── Public API ────────────────────────────────────────────────────────────

    async def enrich_person_by_linkedin(
        self,
        linkedin_url: str,
        *,
        reveal_email: bool = True,
        reveal_phone: bool = False,
        name: str = "",
        company: str = "",
        domain: str = "",
    ) -> ApolloPersonResult:
        """POST /people/match — match one person by LinkedIn URL (name/company/
        domain are secondary hints) and optionally reveal email/phone. Returns a
        no-match result (not an error) when Apollo finds nobody."""
        if not self.enabled:
            raise ApolloAPIError("Apollo is not configured (APOLLO_API_KEY is empty)")
        if not linkedin_url:
            return ApolloPersonResult.no_match()

        params: dict[str, Any] = {
            "linkedin_url": linkedin_url,
            "reveal_personal_emails": _bool(reveal_email),
            "reveal_phone_number": _bool(reveal_phone),
        }
        if name:
            params["name"] = name
        if company:
            params["organization_name"] = company
        if domain:
            params["domain"] = domain

        data = await self._request("/people/match", params=params)
        result = ApolloPersonResult.from_person(data.get("person"))
        self._log_result("people/match", linkedin_url, result)
        return result

    async def bulk_enrich_people(
        self,
        items: list[dict[str, Any]],
        *,
        reveal_email: bool = True,
        reveal_phone: bool = False,
    ) -> list[ApolloPersonResult]:
        """POST /people/bulk_match in batches of <=10. Each item is a dict of
        match hints, e.g. {"linkedin_url": ...} or {"name","organization_name"}.
        Returns one ApolloPersonResult per input item, order-preserved."""
        if not self.enabled:
            raise ApolloAPIError("Apollo is not configured (APOLLO_API_KEY is empty)")
        results: list[ApolloPersonResult] = []
        for start in range(0, len(items), _BULK_MAX):
            chunk = items[start:start + _BULK_MAX]
            payload = {
                "details": chunk,
                "reveal_personal_emails": reveal_email,
                "reveal_phone_number": reveal_phone,
            }
            data = await self._request("/people/bulk_match", json=payload)
            matches = data.get("matches")
            if not isinstance(matches, list):
                matches = []
            for i, person in enumerate(chunk):
                match = matches[i] if i < len(matches) else None
                result = ApolloPersonResult.from_person(match)
                self._log_result("people/bulk_match", person.get("linkedin_url", ""), result)
                results.append(result)
        return results

    # ── Internals ──────────────────────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        retry=retry_if_exception_type(_ApolloRetryable),  # only transient (429/5xx/network)
        reraise=True,
    )
    async def _request_once(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {
            "X-Api-Key": self._api_key,
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, params=params, json=json, headers=headers)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning("apollo_network_error", path=path, error=str(exc))
            raise _ApolloRetryable(str(exc)) from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            logger.warning("apollo_rate_limited", path=path, retry_after=retry_after)
            raise _ApolloRetryable(f"Apollo rate-limited (429), retry_after={retry_after}")
        if response.status_code >= 500:
            logger.warning("apollo_server_error", path=path, status=response.status_code)
            raise _ApolloRetryable(f"Apollo {response.status_code}")
        if response.status_code >= 400:
            # Permanent (bad key/params) — do not retry.
            raise ApolloAPIError(
                f"Apollo returned HTTP {response.status_code}: {response.text[:300]}",
                details={"status": response.status_code, "path": path},
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ApolloAPIError(f"Apollo returned non-JSON body: {response.text[:200]}") from exc

    async def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return await self._request_once(path, params=params, json=json)
        except _ApolloRetryable as exc:
            # Exhausted the retry budget on a transient failure.
            raise ApolloAPIError(f"Apollo unavailable after retries: {exc}") from exc

    def _log_result(self, endpoint: str, linkedin_url: str, result: ApolloPersonResult) -> None:
        if not result.matched:
            logger.info("apollo_no_match", endpoint=endpoint, linkedin_url=linkedin_url)
            return
        if result.has_email or result.has_phone:
            logger.info(
                "apollo_credits_used",
                endpoint=endpoint,
                linkedin_url=linkedin_url,
                email_revealed=result.has_email,
                phone_revealed=result.has_phone,
            )
        else:
            logger.info("apollo_matched_no_contact", endpoint=endpoint, linkedin_url=linkedin_url)


def _bool(v: bool) -> str:
    # Apollo expects lowercase string booleans in query params.
    return "true" if v else "false"
