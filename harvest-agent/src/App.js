import { useEffect, useState } from "react";
import HarvestAgent from "./HarvestAgent";
import LoginPage from "./LoginPage";
import { AUTH_ENABLED } from "./auth";
import { getMe, logout } from "./api";

function App() {
  // "checking" while we validate the session cookie on startup, then "authed"
  // or "anon". With auth disabled (dev) we skip straight to the app.
  const [status, setStatus] = useState(AUTH_ENABLED ? "checking" : "authed");

  // api.js fires "auth:logout" whenever a non-auth call returns 401 — i.e. the
  // session cookie expired or was revoked server-side. Fall back to login.
  useEffect(() => {
    const onLogout = () => setStatus("anon");
    window.addEventListener("auth:logout", onLogout);
    return () => window.removeEventListener("auth:logout", onLogout);
  }, []);

  // Session restore: on every startup, validate the HttpOnly session cookie via
  // GET /auth/me. A live session restores the authenticated state with no OTP; a
  // 401 (missing/expired/revoked) drops us to the login screen.
  useEffect(() => {
    if (!AUTH_ENABLED) return;
    let cancelled = false;
    getMe()
      .then(() => { if (!cancelled) setStatus("authed"); })
      .catch(() => { if (!cancelled) setStatus("anon"); });
    return () => { cancelled = true; };
  }, []);

  if (!AUTH_ENABLED) return <HarvestAgent />; // dev bypass — pair with backend AUTH_ENABLED=false

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

  if (status !== "authed") return <LoginPage onAuthenticated={() => setStatus("authed")} />;

  return (
    <HarvestAgent
      onLogout={async () => {
        try {
          await logout();
        } catch {
          /* revoke best-effort — clear the UI regardless */
        }
        setStatus("anon");
      }}
    />
  );
}

export default App;
