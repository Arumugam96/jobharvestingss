import { createContext, useContext } from "react";

/*
 * Tenant "slip" context — the per-workspace branding + feature flags returned by
 * GET /auth/me (see backend auth_routes.read_current_user). The backend still
 * enforces data access independently (RLS + query scoping); this context only
 * drives presentation: which nav items render, accent color, brand name.
 *
 * Shape: { id, name, type: "internal"|"client", region, theme: {accent, brand},
 *          features: {jobs, history, rules, sources, leads, outreach, analytics,
 *                     ruleEngineRedesign} }
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
    jobs: true, history: true, sources: true, leads: true,
    outreach: true, rules: true, analytics: true, ruleEngineRedesign: false,
  },
};

const TenantContext = createContext(INTERNAL_TENANT);

export function TenantProvider({ tenant, children }) {
  return (
    <TenantContext.Provider value={tenant || INTERNAL_TENANT}>
      {children}
    </TenantContext.Provider>
  );
}

export function useTenant() {
  const tenant = useContext(TenantContext);
  const features = tenant.features || {};
  return {
    tenant,
    features,
    theme: tenant.theme || {},
    isClient: tenant.type === "client",
    // Internal (or a missing features map) keeps today's full nav; clients see
    // only the flags their tenant config sets to true.
    hasFeature: (key) => (tenant.type === "client" ? features[key] === true : features[key] !== false),
  };
}

export default TenantContext;
