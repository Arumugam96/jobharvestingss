/*
 * Tenant selection for the backend's auth bypass (AUTH_ENABLED=false). The
 * chosen tenant id is sent as the X-Dev-Tenant header on every API call (see
 * api.js) — from dev AND production builds; the backend only reads it in its
 * bypass branch, so it's inert whenever auth is on. Under the bypass, /auth/me
 * returns that tenant's real slip and all reads/writes are scoped/stamped
 * accordingly — no OTP login needed to preview a client workspace. The floating
 * switcher pill renders whenever the backend reports the bypass (see App.js /
 * DevTenantSwitcher.jsx), so a production deployment can flip AUTH_ENABLED=false
 * to demo tenants and back on to lock it down.
 *
 * Stored in localStorage (key ss_dev_tenant): the switcher swaps tenants via a
 * full page reload, and localStorage survives it and stays consistent across
 * tabs. A valid ?tenant= URL param wins and is persisted — shareable deep links
 * like http://localhost:3000/?tenant=client_us.
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
