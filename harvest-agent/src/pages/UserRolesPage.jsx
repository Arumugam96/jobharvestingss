import { useEffect, useMemo, useState } from "react";
import { RefreshCw, Search, ShieldCheck, User } from "lucide-react";
import { listUsers, setUserRole } from "../api";
import { C } from "../theme";
import { StatCard, PlainHeader } from "../components/ui";
import { useCurrentUserEmail } from "../components/AssignJob";

/* Admin-only User roles: promote people to admin or back to user
 * (GET /auth/users, PATCH /auth/users/{id}/role — both 403 for non-admins).
 * People appear after their first sign-in. You can't change your own role. */

const WORKSPACES = { internal: "Internal", client_us: "US", client_in: "India" };

const fmtDate = (iso) => {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" }); } catch { return "—"; }
};

function RolePill({ role }) {
  const admin = role === "admin";
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5, padding: "2px 10px", borderRadius: 999,
      fontSize: 12, fontWeight: 700, whiteSpace: "nowrap",
      color: admin ? "#6D28D9" : "#475569", background: admin ? "#F5F3FF" : "#F1F5F9",
      border: `1px solid ${admin ? "#DDD6FE" : "#E2E8F0"}`,
    }}>
      {admin ? <ShieldCheck size={12} /> : <User size={12} />} {admin ? "Admin" : "User"}
    </span>
  );
}

export default function UserRolesPage() {
  const myEmail = useCurrentUserEmail();
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [saving, setSaving] = useState(null); // user id being updated
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listUsers()
      .then((res) => { if (!cancelled) { setUsers(res?.items || []); setError(""); } })
      .catch((err) => { if (!cancelled) setError(err?.message || "Couldn't load users."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [reloadKey]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? users.filter((u) => u.email.toLowerCase().includes(q)) : users;
  }, [users, query]);
  const adminCount = users.filter((u) => u.role === "admin").length;

  const changeRole = async (user, role) => {
    setSaving(user.id);
    setError("");
    try {
      const updated = await setUserRole(user.id, role);
      setUsers((list) => list.map((u) => (u.id === user.id ? { ...u, ...updated } : u)));
    } catch (err) {
      setError(err?.message || "Couldn't change the role.");
    } finally {
      setSaving(null);
    }
  };

  return (
    <main className="ha-main">
      <div className="ha-pagehead">
        <div className="ha-pagehead-titles">
          <h1>User roles</h1>
          <p className="ha-sub">Admins can see Phone logs and the call results on jobs self-assigned by others. People appear here after their first sign-in.</p>
        </div>
        <div className="ha-pagehead-actions">
          <button className="ha-btn ha-btn-secondary" onClick={() => setReloadKey((k) => k + 1)} disabled={loading} title="Refresh">
            <RefreshCw size={16} className={loading ? "ha-spin" : ""} /> Refresh
          </button>
        </div>
      </div>

      <div className="ha-pagebody">
        {error && <div className="ha-errbanner">{error}</div>}

        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          <StatCard value={users.length} label="Users" color={C.primary} />
          <StatCard value={adminCount} label="Admins" color="#7C3AED" />
        </div>

        <div className="ha-card ha-filterbar" style={{ padding: "11px 14px", gridTemplateColumns: "minmax(0, 1fr)" }}>
          <div className="ha-filter-search">
            <Search size={16} />
            <input className="ha-input" value={query} onChange={(e) => setQuery(e.target.value)}
              placeholder="Search by email…" aria-label="Search users" />
          </div>
        </div>

        <div className="ha-card" style={{ overflow: "hidden" }}>
          <div className="ha-table-scroll">
            <table className="ha-table" style={{ minWidth: 720 }}>
              <thead className="ha-thead">
                <tr>
                  <PlainHeader label="Email" />
                  <PlainHeader label="Workspace" width={120} />
                  <PlainHeader label="Role" width={120} />
                  <PlainHeader label="First sign-in" width={140} />
                  <PlainHeader label="Action" align="right" width={160} />
                </tr>
              </thead>
              <tbody>
                {loading && users.length === 0 && (
                  <tr><td className="ha-td" colSpan={5} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>Loading users…</td></tr>
                )}
                {!loading && visible.length === 0 && (
                  <tr><td className="ha-td" colSpan={5} style={{ textAlign: "center", padding: "48px 16px", color: "#94A3B8" }}>No users match.</td></tr>
                )}
                {visible.map((u) => {
                  const isMe = !!myEmail && u.email.toLowerCase() === myEmail.toLowerCase();
                  const next = u.role === "admin" ? "user" : "admin";
                  return (
                    <tr key={u.id} className="ha-row">
                      <td className="ha-td" style={{ color: C.text, fontWeight: 500 }}>
                        {u.email}{isMe && <span style={{ marginLeft: 8, fontSize: 12, color: C.textSoft }}>(you)</span>}
                      </td>
                      <td className="ha-td" style={{ color: C.textSoft }}>{WORKSPACES[u.tenant_id] || u.tenant_id}</td>
                      <td className="ha-td"><RolePill role={u.role} /></td>
                      <td className="ha-td" style={{ color: C.textSoft, whiteSpace: "nowrap" }}>{fmtDate(u.created_at)}</td>
                      <td className="ha-td" style={{ textAlign: "right" }}>
                        {isMe ? (
                          <span style={{ fontSize: 12.5, color: "#94A3B8" }}>You can't change your own role</span>
                        ) : (
                          <button className="ha-btn ha-btn-secondary" disabled={saving === u.id}
                            onClick={() => changeRole(u, next)}>
                            {saving === u.id ? "Saving…" : next === "admin" ? "Make admin" : "Remove admin"}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </main>
  );
}
