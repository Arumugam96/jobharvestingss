"""Tests for /auth/request-otp, /auth/verify-otp and the get_current_user dependency."""
from __future__ import annotations

import pytest
from httpx import AsyncClient

VALID_EMAIL = "jane.doe@sightspectrum.com"


@pytest.mark.asyncio
async def test_request_otp_rejects_wrong_domain(client: AsyncClient) -> None:
    resp = await client.post("/auth/request-otp", json={"email": "jane@gmail.com"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_request_otp_rejects_lookalike_domain(client: AsyncClient) -> None:
    # Multi-label suffixes and subdomain-of-evil look-alikes stay rejected — their
    # registrable domain isn't sightspectrum.*.
    for email in (
        "jane@sightspectrum.co.in",
        "jane@sightspectrum.com.evil.com",
        "jane@sightspectrum.evil.com",
        "jane@notsightspectrum.com",
    ):
        resp = await client.post("/auth/request-otp", json={"email": email})
        assert resp.status_code == 422, email


@pytest.mark.asyncio
async def test_request_otp_accepts_any_sightspectrum_tld(
    client: AsyncClient, mock_email_sender
) -> None:
    # The company uses more than .com — any single-label TLD is accepted.
    for email in ("jane@sightspectrum.in", "jane@sightspectrum.org", "jane@sightspectrum.io"):
        resp = await client.post("/auth/request-otp", json={"email": email})
        assert resp.status_code == 200, email
    assert {r for r, _ in mock_email_sender.sent} == {
        "jane@sightspectrum.in",
        "jane@sightspectrum.org",
        "jane@sightspectrum.io",
    }


@pytest.mark.asyncio
async def test_request_otp_success_sends_otp_and_generic_message(
    client: AsyncClient, mock_email_sender
) -> None:
    resp = await client.post("/auth/request-otp", json={"email": VALID_EMAIL})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"message": "If the email address is eligible, an OTP has been sent."}

    assert len(mock_email_sender.sent) == 1
    recipient, otp = mock_email_sender.sent[0]
    assert recipient == VALID_EMAIL
    assert otp.isdigit()
    assert len(otp) == 6


@pytest.mark.asyncio
async def test_request_otp_cooldown_blocks_immediate_resend(
    client: AsyncClient, mock_email_sender
) -> None:
    first = await client.post("/auth/request-otp", json={"email": VALID_EMAIL})
    assert first.status_code == 200

    second = await client.post("/auth/request-otp", json={"email": VALID_EMAIL})
    assert second.status_code == 429
    assert len(mock_email_sender.sent) == 1


@pytest.mark.asyncio
async def test_verify_otp_success_issues_token(client: AsyncClient, mock_email_sender) -> None:
    await client.post("/auth/request-otp", json={"email": VALID_EMAIL})
    otp = mock_email_sender.last_otp

    resp = await client.post("/auth/verify-otp", json={"email": VALID_EMAIL, "otp": otp})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


@pytest.mark.asyncio
async def test_verify_otp_cannot_be_replayed(client: AsyncClient, mock_email_sender) -> None:
    await client.post("/auth/request-otp", json={"email": VALID_EMAIL})
    otp = mock_email_sender.last_otp

    first = await client.post("/auth/verify-otp", json={"email": VALID_EMAIL, "otp": otp})
    assert first.status_code == 200

    second = await client.post("/auth/verify-otp", json={"email": VALID_EMAIL, "otp": otp})
    assert second.status_code == 401


@pytest.mark.asyncio
async def test_verify_otp_wrong_code_rejected(client: AsyncClient, mock_email_sender) -> None:
    await client.post("/auth/request-otp", json={"email": VALID_EMAIL})
    otp = mock_email_sender.last_otp
    wrong = "0" * len(otp) if otp != "0" * len(otp) else "1" * len(otp)

    resp = await client.post("/auth/verify-otp", json={"email": VALID_EMAIL, "otp": wrong})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_verify_otp_locks_out_after_max_attempts(client: AsyncClient, mock_email_sender) -> None:
    await client.post("/auth/request-otp", json={"email": VALID_EMAIL})
    otp = mock_email_sender.last_otp
    wrong = "0" * len(otp) if otp != "0" * len(otp) else "1" * len(otp)

    for _ in range(5):  # OTP_MAX_ATTEMPTS default
        resp = await client.post("/auth/verify-otp", json={"email": VALID_EMAIL, "otp": wrong})
        assert resp.status_code == 401

    # Even the correct OTP is now rejected — attempts are exhausted.
    resp = await client.post("/auth/verify-otp", json={"email": VALID_EMAIL, "otp": otp})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_requires_bearer_token(client: AsyncClient) -> None:
    resp = await client.get("/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_current_user_with_valid_token(
    client: AsyncClient, mock_email_sender
) -> None:
    await client.post("/auth/request-otp", json={"email": VALID_EMAIL})
    otp = mock_email_sender.last_otp
    token = (
        await client.post("/auth/verify-otp", json={"email": VALID_EMAIL, "otp": otp})
    ).json()["access_token"]

    resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == VALID_EMAIL
    assert body["is_verified"] is True


@pytest.mark.asyncio
async def test_me_rejects_garbage_token(client: AsyncClient) -> None:
    resp = await client.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401
