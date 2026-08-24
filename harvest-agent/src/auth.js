/*
 * Auth flag helper. Authentication tokens are NEVER stored in JavaScript
 * (localStorage/sessionStorage) — the backend issues a Secure, HttpOnly session
 * cookie on OTP verification that the browser sends automatically (see api.js,
 * which uses `credentials: "include"`). JS can't read that cookie by design, so
 * the app derives its authenticated state from GET /auth/me at startup.
 *
 * AUTH_ENABLED mirrors the backend's AUTH_ENABLED setting. To bypass login in
 * development, set REACT_APP_AUTH_ENABLED=false here AND AUTH_ENABLED=false on
 * the backend — both processes must agree, since the API is protected too.
 */
export const AUTH_ENABLED = process.env.REACT_APP_AUTH_ENABLED !== "false";
