"""OTP-based email authentication endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.dependencies import get_current_user, get_db_session, get_email_sender
from app.models.auth import (
    AuthenticatedUser,
    MessageResponse,
    RequestOTPIn,
    TokenResponse,
    VerifyOTPIn,
)
from app.services.auth_service import AuthService, OTPRequestError, OTPVerifyError
from app.services.email_service import EmailSender

router = APIRouter(prefix="/auth", tags=["Auth"])

GENERIC_OTP_SENT_MESSAGE = "If the email address is eligible, an OTP has been sent."


@router.post("/request-otp", response_model=MessageResponse, summary="Request a login OTP")
async def request_otp(
    payload: RequestOTPIn,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    email_sender: EmailSender = Depends(get_email_sender),
) -> MessageResponse | JSONResponse:
    service = AuthService(db, settings, email_sender)
    try:
        await service.request_otp(payload.email)
    except OTPRequestError as exc:
        # Returned (not raised): raising here would propagate through
        # get_db_session's dependency and roll back anything the service
        # already persisted for this request.
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": exc.message},
            headers=headers,
        )
    return MessageResponse(message=GENERIC_OTP_SENT_MESSAGE)


@router.post("/verify-otp", response_model=TokenResponse, summary="Verify OTP and obtain an access token")
async def verify_otp(
    payload: VerifyOTPIn,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    email_sender: EmailSender = Depends(get_email_sender),
) -> TokenResponse | JSONResponse:
    service = AuthService(db, settings, email_sender)
    try:
        access_token = await service.verify_otp(payload.email, payload.otp)
    except OTPVerifyError:
        # Returned (not raised) so the failed-attempt increment the service
        # just recorded survives — see AuthService.verify_otp for why.
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid or expired OTP"},
        )
    return TokenResponse(access_token=access_token)


@router.get("/me", response_model=AuthenticatedUser, summary="Get the current authenticated user")
async def read_current_user(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    return current_user
