import { useEffect, useState } from "react";
import HarvestAgent from "./HarvestAgent";
import LoginPage from "./LoginPage";
import { AUTH_ENABLED, getToken, clearToken } from "./auth";
import { getMe } from "./api";

function App() {
  const [token, setToken] = useState(getToken);

  // api.js fires "auth:logout" (and clears the token) whenever a non-auth call
  // returns 401 — i.e. the session expired or was revoked. Fall back to login.
  useEffect(() => {
    const onLogout = () => setToken(null);
    window.addEventListener("auth:logout", onLogout);
    return () => window.removeEventListener("auth:logout", onLogout);
  }, []);

  // Session restore: validate a stored token on load so an expired one bounces
  // to login immediately rather than on the first data fetch. A 401 here is
  // handled inside api.js (clears token + fires auth:logout).
  useEffect(() => {
    if (AUTH_ENABLED && getToken()) getMe().catch(() => {});
  }, []);

  if (!AUTH_ENABLED) return <HarvestAgent />; // dev bypass — pair with backend AUTH_ENABLED=false
  if (!token) return <LoginPage onAuthenticated={() => setToken(getToken())} />;
  return (
    <HarvestAgent
      onLogout={() => {
        clearToken();
        setToken(null);
      }}
    />
  );
}

export default App;
