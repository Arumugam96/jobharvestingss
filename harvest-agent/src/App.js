import { useEffect, useState } from "react";
import { BrowserRouter } from "react-router-dom";
import HarvestAgent from "./HarvestAgent";
import LoginPage from "./LoginPage";
import { AUTH_ENABLED } from "./auth";
import { getMe, logout, ApiError } from "./api";
import { TenantProvider, INTERNAL_TENANT } from "./TenantContext";
import DevTenantSwitcher from "./components/DevTenantSwitcher";
import { PAGE_BG_STYLE } from "./theme";

// Only these mean "no valid session". Anything else from /auth/me — a network
// error (laptop waking, Wi-Fi reconnecting), a 502/504 while the api restarts,
// a 500 — says nothing about the cookie, and must NOT drop the user to OTP:
// that's why a later refresh used to land straight back in the app.
const isAuthRejection = (err) =>
  err instanceof ApiError && (err.status === 401 || err.status === 403);

// Backoff for transient /auth/me failures on startup (~30s total).
const SESSION_RETRY_DELAYS_MS = [1000, 2000, 4000, 8000, 15000];

function App() {
  // "checking" while we validate the session cookie on startup, then "authed"
  // or "anon"; "unreachable" when the backend couldn't be reached to decide.
  // With auth disabled (dev) we skip straight to the app.
  const [status, setStatus] = useState(AUTH_ENABLED ? "checking" : "authed");
  const [reconnecting, setReconnecting] = useState(false);
  const [restoreAttempt, setRestoreAttempt] = useState(0); // bump to re-run the restore
  // The tenant "slip" from GET /auth/me (branding + feature flags). Defaults to
  // the internal full-access slip until /auth/me resolves (dev bypass keeps it).
  const [tenant, setTenant] = useState(INTERNAL_TENANT);
  // True when the BACKEND reports it runs with AUTH_ENABLED=false (see
  // /auth/me's auth_bypass flag). Detected at runtime so a production build —
  // compiled with auth on — still shows the tenant-switcher pill when pointed
  // at a bypassed backend. Always false against a normally-authed backend.
  const [devBypass, setDevBypass] = useState(false);

  // api.js fires "auth:logout" whenever a non-auth call returns 401. Confirm with
  // /auth/me before dropping to login, so one stray 401 can't log the user out
  // while the session cookie is still valid. (/auth/* 401s don't re-fire it.)
  useEffect(() => {
    let checking = false;
    const onLogout = async () => {
      if (checking) return;
      checking = true;
      try {
        await getMe();
      } catch (err) {
        if (isAuthRejection(err)) setStatus("anon");
      } finally {
        checking = false;
      }
    };
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
  // 401/403 (missing/expired/revoked) drops us to the login screen. Transient
  // failures retry with backoff, then show a Retry screen — never the OTP page.
  useEffect(() => {
    if (!AUTH_ENABLED) return;
    let cancelled = false;
    let timer = null;
    setStatus("checking");
    const attempt = (i) => {
      getMe()
        .then((me) => {
          if (cancelled) return;
          if (me && me.tenant) setTenant(me.tenant);
          setDevBypass(!!(me && me.auth_bypass));
          setReconnecting(false);
          setStatus("authed");
        })
        .catch((err) => {
          if (cancelled) return;
          if (isAuthRejection(err)) {
            setReconnecting(false);
            setStatus("anon");
          } else if (i < SESSION_RETRY_DELAYS_MS.length) {
            setReconnecting(true);
            timer = setTimeout(() => attempt(i + 1), SESSION_RETRY_DELAYS_MS[i]);
          } else {
            setReconnecting(false);
            setStatus("unreachable");
          }
        });
    };
    attempt(0);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [restoreAttempt]);

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

  if (status === "checking" || status === "unreachable") {
    return (
      <div style={{
        minHeight: "100vh", display: "flex", flexDirection: "column", gap: 12,
        alignItems: "center", justifyContent: "center",
        ...PAGE_BG_STYLE, color: "#64748B", fontFamily: 'ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif',
        fontSize: 14,
      }}>
        {status === "unreachable"
          ? "Couldn't reach the server to restore your session."
          : reconnecting ? "Reconnecting to the server…" : "Restoring your session…"}
        {status === "unreachable" && (
          <button type="button" onClick={() => setRestoreAttempt((n) => n + 1)} style={{
            padding: "8px 18px", borderRadius: 8, border: "1px solid #CBD5E1",
            background: "#fff", color: "#0F172A", fontSize: 14, cursor: "pointer",
          }}>
            Retry
          </button>
        )}
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
            setDevBypass(!!(me && me.auth_bypass));
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
          // With the backend bypass active there's no session to revoke and the
          // login page is pointless — hide Sign out and show the switcher pill.
          onLogout={devBypass ? undefined : async () => {
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
        {devBypass && <DevTenantSwitcher />}
      </BrowserRouter>
    </TenantProvider>
  );
}

export default App;
