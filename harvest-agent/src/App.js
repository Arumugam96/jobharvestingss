import { useEffect, useState } from "react";
import { BrowserRouter } from "react-router-dom";
import HarvestAgent from "./HarvestAgent";
import LoginPage from "./LoginPage";
import { AUTH_ENABLED } from "./auth";
import { getMe, logout } from "./api";
import { TenantProvider, INTERNAL_TENANT } from "./TenantContext";
import DevTenantSwitcher from "./components/DevTenantSwitcher";

function App() {
  // "checking" while we validate the session cookie on startup, then "authed"
  // or "anon". With auth disabled (dev) we skip straight to the app.
  const [status, setStatus] = useState(AUTH_ENABLED ? "checking" : "authed");
  // The tenant "slip" from GET /auth/me (branding + feature flags). Defaults to
  // the internal full-access slip until /auth/me resolves (dev bypass keeps it).
  const [tenant, setTenant] = useState(INTERNAL_TENANT);

  // api.js fires "auth:logout" whenever a non-auth call returns 401 — i.e. the
  // session cookie expired or was revoked server-side. Fall back to login.
  useEffect(() => {
    const onLogout = () => setStatus("anon");
    window.addEventListener("auth:logout", onLogout);
    return () => window.removeEventListener("auth:logout", onLogout);
  }, []);

  // Dev bypass slip: fetch the real tenant slip via GET /auth/me — the backend
  // resolves the X-Dev-Tenant header api.js attaches (see devTenant.js), so the
  // preview matches exactly what that tenant's users see. Backend unreachable →
  // keep the INTERNAL_TENANT fallback (today's behavior).
  useEffect(() => {
    if (AUTH_ENABLED) return;
    let cancelled = false;
    getMe()
      .then((me) => {
        if (!cancelled && me && me.tenant) setTenant(me.tenant);
      })
      .catch(() => { /* slip is presentation-only — keep the fallback */ });
    return () => { cancelled = true; };
  }, []);

  // Session restore: on every startup, validate the HttpOnly session cookie via
  // GET /auth/me. A live session restores the authenticated state with no OTP; a
  // 401 (missing/expired/revoked) drops us to the login screen.
  useEffect(() => {
    if (!AUTH_ENABLED) return;
    let cancelled = false;
    getMe()
      .then((me) => {
        if (cancelled) return;
        if (me && me.tenant) setTenant(me.tenant);
        setStatus("authed");
      })
      .catch(() => { if (!cancelled) setStatus("anon"); });
    return () => { cancelled = true; };
  }, []);

  if (!AUTH_ENABLED) {
    // dev bypass — pair with backend AUTH_ENABLED=false. The slip comes from
    // /auth/me under the selected X-Dev-Tenant (effect above); the floating
    // switcher flips tenants without any login.
    return (
      <TenantProvider tenant={tenant}>
        <BrowserRouter>
          <HarvestAgent />
          <DevTenantSwitcher />
        </BrowserRouter>
      </TenantProvider>
    );
  }

  if (status === "checking") {
    return (
      <div style={{
        minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center",
        background: "#F8FAFC", color: "#64748B", fontFamily: 'ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif',
        fontSize: 14,
      }}>
        Restoring your session…
      </div>
    );
  }

  if (status !== "authed") {
    return (
      <LoginPage
        onAuthenticated={async () => {
          // Fetch the slip for the just-signed-in user, then enter the app. The
          // landing page uses the fresh-login flag to play its intro once.
          try {
            const me = await getMe();
            if (me && me.tenant) setTenant(me.tenant);
          } catch { /* slip is presentation-only — enter with the default */ }
          try { sessionStorage.setItem("ss_fresh_login", "1"); } catch { /* ignore */ }
          setStatus("authed");
        }}
      />
    );
  }

  // The router lives INSIDE the authed branch: LoginPage and the "checking"
  // spinner stay non-routed short-circuits, so a deep link never renders the app
  // shell before the session is confirmed. Cookie-based auth is unchanged.
  return (
    <TenantProvider tenant={tenant}>
      <BrowserRouter>
        <HarvestAgent
          onLogout={async () => {
            try {
              await logout();
            } catch {
              /* revoke best-effort — clear the UI regardless */
            }
            try { sessionStorage.removeItem("ss_intro_played"); } catch { /* ignore */ }
            setTenant(INTERNAL_TENANT);
            setStatus("anon");
          }}
        />
      </BrowserRouter>
    </TenantProvider>
  );
}

export default App;
