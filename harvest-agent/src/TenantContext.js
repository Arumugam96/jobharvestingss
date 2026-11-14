import { createContext, useContext, useMemo } from "react";

/*
 * Tenant "slip" context — the per-workspace branding + feature flags returned by
 * GET /auth/me (see backend auth_routes.read_current_user). The backend still
 * enforces data access independently (RLS + query scoping); this context only
 * drives presentation: which nav items render, accent color, brand name.
 *
 * Shape: { id, name, type: "internal"|"client", region, theme: {accent, brand},
 *          features: {jobs, history, rules, sources, leads, outreach, analytics,
 *                     ruleEngineRedesign, demand} }
 */

// Default slip = internal, full access — used for the AUTH_ENABLED=false dev
// bypass and as the safe fallback while /auth/me hasn't resolved yet.
export const INTERNAL_TENANT = {
  id: "internal",
  name: "SightSpectrum",
  type: "internal",
  region: "",
  theme: { accent: "#2563EB", brand: "SightSpectrum" },
  features: {
    jobs: true, history: true, sources: true, leads: true, contacts: true,
    outreach: true, rules: true, analytics: true, ruleEngineRedesign: false,
    demand: true,
  },
};

const TenantContext = createContext({ tenant: INTERNAL_TENANT, role: "user" });

// `role` is the signed-in user's role from /auth/me ("user" | "admin") — it
// rides along with the slip so any component can gate admin-only UI.
// `authBypass` is true when auth is disabled (frontend REACT_APP_AUTH_ENABLED=false
// or the backend reporting AUTH_ENABLED=false via /auth/me's auth_bypass) — used to
// show dev-only chrome such as the sidebar's backend-health pill.
export function TenantProvider({ tenant, role = "user", authBypass = false, children }) {
  const value = useMemo(
    () => ({ tenant: tenant || INTERNAL_TENANT, role, authBypass }),
    [tenant, role, authBypass],
  );
  return <TenantContext.Provider value={value}>{children}</TenantContext.Provider>;
}

/**
 * Read the tenant slip with derived helpers: `features`, `theme`, `isClient`,
 * `isAdmin`, `authBypass` and `hasFeature(key)` (clients need an explicit true;
 * internal tenants see a feature unless it is explicitly false).
 */
export function useTenant() {
  const { tenant, role, authBypass = false } = useContext(TenantContext);
  const features = tenant.features || {};
  return {
    tenant,
    features,
    theme: tenant.theme || {},
    isClient: tenant.type === "client",
    // Presentation only — admin endpoints enforce the role server-side.
    isAdmin: role === "admin",
    authBypass,
    // Internal (or a missing features map) keeps today's full nav; clients see
    // only the flags their tenant config sets to true.
    hasFeature: (key) => (tenant.type === "client" ? features[key] === true : features[key] !== false),
  };
}

export default TenantContext;
