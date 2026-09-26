/*
 * Dev-only tenant selection for the auth bypass (REACT_APP_AUTH_ENABLED=false
 * paired with backend AUTH_ENABLED=false). The chosen tenant id is sent as the
 * X-Dev-Tenant header on every API call (see api.js); the backend's dev bypass
 * binds its tenant context to it, so /auth/me returns that tenant's real slip
 * and all reads/writes are scoped/stamped accordingly — no OTP login needed to
 * preview a client workspace.
 *
 * Stored in localStorage (key ss_dev_tenant): the switcher swaps tenants via a
 * full page reload, and localStorage survives it and stays consistent across
 * tabs. A valid ?tenant= URL param wins and is persisted — shareable deep links
 * like http://localhost:3000/?tenant=client_us. Inert in production builds:
 * everything is gated on !AUTH_ENABLED, and the Docker image builds with auth on.
 */

export const DEV_TENANTS = [
  { id: "internal", label: "Internal" },
  { id: "client_us", label: "US" },
  { id: "client_in", label: "India" },
];

const KEY = "ss_dev_tenant";
const VALID = new Set(DEV_TENANTS.map((t) => t.id));

export function getDevTenant() {
  try {
    const fromUrl = new URLSearchParams(window.location.search).get("tenant");
    if (fromUrl && VALID.has(fromUrl)) {
      localStorage.setItem(KEY, fromUrl); // param wins and persists
      return fromUrl;
    }
    const stored = localStorage.getItem(KEY);
    if (stored && VALID.has(stored)) return stored;
  } catch {
    /* storage unavailable — fall through to internal */
  }
  return "internal";
}

export function setDevTenant(id) {
  try {
    localStorage.setItem(KEY, VALID.has(id) ? id : "internal");
  } catch {
    /* ignore */
  }
  // Full reload is the cache invalidation: every data context refetches under
  // the new X-Dev-Tenant header, including the /auth/me slip.
  window.location.reload();
}
