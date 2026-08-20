/*
 * Auth token + flag helpers. Deliberately dependency-free (no import of api.js)
 * so both api.js (bearer-header injection) and App.js (the login gate) can
 * import it without a circular dependency.
 *
 * AUTH_ENABLED mirrors the backend's AUTH_ENABLED setting. To bypass login in
 * development, set REACT_APP_AUTH_ENABLED=false here AND AUTH_ENABLED=false on
 * the backend — both processes must agree, since the API is protected too.
 */
const KEY = "ha_access_token";

export const AUTH_ENABLED = process.env.REACT_APP_AUTH_ENABLED !== "false";

export function getToken() {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function setToken(token) {
  try {
    localStorage.setItem(KEY, token);
  } catch {
    /* storage unavailable (private mode / disabled) — token stays in memory only */
  }
}

export function clearToken() {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* nothing to clear */
  }
}
