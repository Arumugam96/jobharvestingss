import { AUTH_ENABLED } from "../auth";
import { DEV_TENANTS, getDevTenant, setDevTenant } from "../devTenant";

/*
 * Floating dev-only tenant switcher — visible ONLY in the auth bypass
 * (REACT_APP_AUTH_ENABLED=false). Lets developers flip between the internal
 * (all-access) view and each client workspace without OTP login. Clicking a
 * tenant persists it (localStorage, see devTenant.js) and reloads so every data
 * fetch — including the /auth/me slip — reruns under the new X-Dev-Tenant
 * header. Production builds compile this to null (AUTH_ENABLED is true).
 */
export default function DevTenantSwitcher() {
  if (AUTH_ENABLED) return null;
  const current = getDevTenant();
  return (
    <div
      style={{
        position: "fixed",
        bottom: 16,
        left: 16,
        zIndex: 9999,
        display: "flex",
        alignItems: "center",
        gap: 6,
        padding: "6px 10px",
        borderRadius: 999,
        background: "#0F172A",
        boxShadow: "0 4px 14px rgba(15,23,42,.35)",
        fontFamily: 'ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif',
        fontSize: 12,
      }}
    >
      <span style={{ color: "#F59E0B", fontWeight: 700, letterSpacing: ".08em" }}>DEV</span>
      {DEV_TENANTS.map((t) => {
        const active = t.id === current;
        return (
          <button
            key={t.id}
            type="button"
            disabled={active}
            onClick={() => setDevTenant(t.id)}
            title={active ? `Viewing as ${t.label}` : `View as ${t.label}`}
            style={{
              border: "none",
              borderRadius: 999,
              padding: "4px 10px",
              cursor: active ? "default" : "pointer",
              fontWeight: 600,
              color: active ? "#0F172A" : "#CBD5E1",
              background: active ? "#F8FAFC" : "transparent",
            }}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}
