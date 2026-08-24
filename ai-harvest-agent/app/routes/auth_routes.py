"""OTP-based email authentication endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.dependencies import get_current_user, get_db_session, get_email_sender
from app.core.security import create_access_token
from app.models.auth import (
    AuthenticatedUser,
    MessageResponse,
    RequestOTPIn,
    TokenResponse,
    VerifyOTPIn,
)
from app.services.auth_service import AuthService, OTPRequestError, OTPVerifyError
from app.services.email_service import EmailSender
from app.services.session_service import SessionService

router = APIRouter(prefix="/auth", tags=["Auth"])

GENERIC_OTP_SENT_MESSAGE = "If the email address is eligible, an OTP has been sent."


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    """Attach the persistent session as a Secure, HttpOnly, SameSite cookie."""
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_lifetime_days * 86_400,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        domain=settings.session_cookie_domain or None,
        path="/",
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    """Delete the session cookie — attributes must match those it was set with."""
    response.delete_cookie(
        key=settings.session_cookie_name,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        domain=settings.session_cookie_domain or None,
        path="/",
    )


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
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    email_sender: EmailSender = Depends(get_email_sender),
) -> TokenResponse | JSONResponse:
    service = AuthService(db, settings, email_sender)
    try:
        user = await service.verify_otp(payload.email, payload.otp)
    except OTPVerifyError:
        # Returned (not raised) so the failed-attempt increment the service
        # just recorded survives — see AuthService.verify_otp for why.
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid or expired OTP"},
        )

    # Mint the persistent session and set it as an HttpOnly cookie so the browser
    # stays signed in across refreshes/restarts without re-OTP. The access_token
    # is still returned for backward compatibility (Swagger / API clients) — the
    # web app ignores it and relies solely on the cookie.
    session_token = await SessionService(db, settings).create_session(
        user.id, request.headers.get("user-agent", "")
    )
    _set_session_cookie(response, session_token, settings)
    access_token = create_access_token(subject=user.id, email=user.email, settings=settings)
    return TokenResponse(access_token=access_token)


@router.post("/logout", response_model=MessageResponse, summary="Revoke the current session")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> MessageResponse:
    """Revoke the session behind the cookie (if any) and clear it. Safe to call
    without a valid session — always returns 200 so logout never gets stuck."""
    cookie_token = request.cookies.get(settings.session_cookie_name)
    if cookie_token:
        await SessionService(db, settings).revoke(cookie_token)
    _clear_session_cookie(response, settings)
    return MessageResponse(message="Logged out")


@router.get("/me", response_model=AuthenticatedUser, summary="Get the current authenticated user")
async def read_current_user(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    return current_user
