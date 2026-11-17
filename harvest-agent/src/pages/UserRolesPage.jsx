import { useEffect, useMemo, useState } from "react";
import { Briefcase, RefreshCw, Search, ShieldCheck, User } from "lucide-react";
import { getRoles, listUsers, setUserRole } from "../api";
import { C } from "../theme";
import { StatCard, PlainHeader } from "../components/ui";
import { useCurrentUserEmail } from "../components/AssignJob";

/* Admin-only User roles: pick each person's role (User / Admin / Business
 * Lead — the list comes from GET /auth/roles) via GET /auth/users and
 * PATCH /auth/users/{id}/role, all 403 for non-admins.
 * People appear after their first sign-in. You can't change your own role. */

const WORKSPACES = { internal: "Internal", client_us: "US", client_in: "India" };

/** Locale date ("medium") for an ISO timestamp; "—" when empty. */
const fmtDate = (iso) => {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" }); } catch { return "—"; }
};

/* Pill colours + glyph per role key; anything unknown renders like "user". */
const ROLE_STYLES = {
  admin: { color: "#6D28D9", bg: "#F5F3FF", border: "#DDD6FE", Glyph: ShieldCheck, label: "Admin" },
  business_lead: { color: "#0E7490", bg: "#ECFEFF", border: "#A5F3FC", Glyph: Briefcase, label: "Business Lead" },
  user: { color: "#475569", bg: "#F1F5F9", border: "#E2E8F0", Glyph: User, label: "User" },
};

/** Pill showing a role's label (Admin purple, Business Lead teal, User grey). */
function RolePill({ role, label }) {
  const st = ROLE_STYLES[role] || ROLE_STYLES.user;
  const { Glyph } = st;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5, padding: "2px 10px", borderRadius: 999,
      fontSize: 12, fontWeight: 700, whiteSpace: "nowrap",
      color: st.color, background: st.bg, border: `1px solid ${st.border}`,
    }}>
      <Glyph size={12} /> {label || st.label || role}
    </span>
  );
}

/**
 * Admin-only User roles page: user / admin / business-lead counts, an email search and a table
 * of signed-in users (GET /auth/users) with a role picker per row (not for yourself).
 */
export default function UserRolesPage() {
  const myEmail = useCurrentUserEmail();
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState(""); // success message after a role change
  const [roles, setRoles] = useState([]);
  const [rolesError, setRolesError] = useState("");
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

  useEffect(() => {
    let cancelled = false;
    getRoles()
      .then((res) => { if (!cancelled) { setRoles(res?.items || []); setRolesError(""); } })
      .catch((err) => { if (!cancelled) setRolesError(err?.message || "Couldn't load the role list."); });
    return () => { cancelled = true; };
  }, [reloadKey]);

  // Auto-hide the success notice after a few seconds.
  useEffect(() => {
    if (!notice) return undefined;
    const t = setTimeout(() => setNotice(""), 4000);
    return () => clearTimeout(t);
  }, [notice]);

  /** Display label for a role key: GET /auth/roles label, else the user's role_label, else the key. */
  const roleLabel = (name, fallback) =>
    roles.find((r) => r.name === name)?.label || fallback || ROLE_STYLES[name]?.label || name;

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? users.filter((u) => u.email.toLowerCase().includes(q)) : users;
  }, [users, query]);
  const adminCount = users.filter((u) => u.role === "admin").length;
  const leadCount = users.filter((u) => u.role === "business_lead").length;

  /** Set a user's role (PATCH /auth/users/{id}/role) and merge the updated user into the list. */
  const changeRole = async (user, role) => {
    if (!role || role === user.role) return;
    setSaving(user.id);
    setError("");
    setNotice("");
    try {
      const updated = await setUserRole(user.id, role);
      setUsers((list) => list.map((u) => (u.id === user.id ? { ...u, role, ...updated } : u)));
      setNotice(`${user.email} is now ${roleLabel(updated?.role || role, updated?.role_label)}.`);
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
          <p className="ha-sub">Admins can see every Phone log and the call results on jobs self-assigned by others; Business Leads see Phone logs for their own calls. People appear here after their first sign-in.</p>
        </div>
        <div className="ha-pagehead-actions">
          <button className="ha-btn ha-btn-secondary" onClick={() => setReloadKey((k) => k + 1)} disabled={loading} title="Refresh">
            <RefreshCw size={16} className={loading ? "ha-spin" : ""} /> Refresh
          </button>
        </div>
      </div>

      <div className="ha-pagebody">
        {error && <div className="ha-errbanner">{error}</div>}
        {rolesError && <div className="ha-errbanner">{rolesError}</div>}
        {notice && (
          <div role="status" style={{
            background: "#ECFDF3", border: "1px solid #A7F3D0", color: "#047857", borderRadius: 10,
            padding: "10px 16px", fontSize: 13, fontWeight: 500,
          }}>{notice}</div>
        )}

        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          <StatCard value={users.length} label="Users" color={C.primary} />
          <StatCard value={adminCount} label="Admins" color="#7C3AED" />
          <StatCard value={leadCount} label="Business leads" color="#0891B2" />
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
            <table className="ha-table" style={{ minWidth: 860 }}>
              <thead className="ha-thead">
                <tr>
                  <PlainHeader label="Email" />
                  <PlainHeader label="Workspace" width={120} />
                  <PlainHeader label="Role" width={200} />
                  <PlainHeader label="First sign-in" width={140} />
                  <PlainHeader label="Change role" align="right" width={230} />
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
                  // Keep the user's current role selectable even if /auth/roles failed or lacks it.
                  const options = roles.some((r) => r.name === u.role)
                    ? roles
                    : [{ name: u.role, label: roleLabel(u.role, u.role_label) }, ...roles];
                  return (
                    <tr key={u.id} className="ha-row">
                      <td className="ha-td" style={{ color: C.text, fontWeight: 500 }}>
                        {u.email}{isMe && <span style={{ marginLeft: 8, fontSize: 12, color: C.textSoft }}>(you)</span>}
                      </td>
                      <td className="ha-td" style={{ color: C.textSoft }}>{WORKSPACES[u.tenant_id] || u.tenant_id}</td>
                      <td className="ha-td"><RolePill role={u.role} label={roleLabel(u.role, u.role_label)} /></td>
                      <td className="ha-td" style={{ color: C.textSoft, whiteSpace: "nowrap" }}>{fmtDate(u.created_at)}</td>
                      <td className="ha-td" style={{ textAlign: "right" }}>
                        {isMe ? (
                          <span style={{ fontSize: 12.5, color: "#94A3B8" }}>You can't change your own role</span>
                        ) : (
                          <div style={{ display: "inline-flex", alignItems: "center", gap: 8, justifyContent: "flex-end" }}>
                            {saving === u.id && <span style={{ fontSize: 12.5, color: C.textSoft }}>Saving…</span>}
                            <select className="ha-input ha-select" style={{ width: 210, flex: "0 0 auto" }}
                              aria-label={`Role for ${u.email}`} value={u.role}
                              disabled={saving !== null || roles.length === 0}
                              title={roles.length === 0 ? "Role list unavailable" : undefined}
                              onChange={(e) => changeRole(u, e.target.value)}>
                              {options.map((r) => (
                                <option key={r.name} value={r.name} title={r.description || undefined}>{r.label || r.name}</option>
                              ))}
                            </select>
                          </div>
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
