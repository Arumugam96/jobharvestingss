import { useCallback, useEffect, useState } from "react";
import { Building2, Check, Plus, RefreshCw, Search, Tags, Trash2, X, Pencil } from "lucide-react";
import {
  listCompanyOverrides, saveCompanyOverride, deleteCompanyOverride,
  listClassificationKeywords, addClassificationKeyword, deleteClassificationKeyword,
} from "../api";
import { DOMAINS } from "../lib/ruleEngineOptions";
import { C } from "../theme";
import { PlainHeader, fmtDate } from "../components/ui";

/* Admin-only Company classification. Two tabs:
 *   Company overrides — per-company hiring entity / size / domain that every future
 *     harvest applies (GET/PUT/DELETE /company-overrides). "Manual" rows come from
 *     here or a job edit with "apply to all jobs"; "LLM" rows are the harvest's
 *     cached LLM verdicts for companies the keyword rules left Ambiguous.
 *   Keywords — terms added to the built-in staffing / direct client / GCC lists
 *     (GET/POST/DELETE /classification-keywords), matched as whole words in company
 *     names from the next harvest run — no restart.
 * All endpoints 403 for non-admins; the route is wrapped in AdminGate. */

const HIRING_ENTITIES = ["Direct Client", "GCC", "Staffing Firm", "Ambiguous"];
const COMPANY_SIZES = [
  "2-10", "11-50", "51-200", "201-500", "501-1,000", "1,001-5,000", "5,001-10,000", "10,001+",
].map((band) => `${band} employees`);
const DOMAIN_SUGGESTIONS = DOMAINS.filter((d) => d !== "Any");
const PAGE_SIZE = 50;

const KEYWORD_LISTS = [
  { key: "staffing", label: "Staffing firm terms", hint: "Company names containing these words are Staffing Firm (e.g. \"body shop\", \"talent partners\")." },
  { key: "direct", label: "Direct client terms", hint: "Company names containing these are Direct Client." },
  { key: "gcc", label: "GCC terms", hint: "Company names containing these are GCC (checked first)." },
];

const SOURCE_STYLES = {
  manual: { color: "#6D28D9", bg: "#F5F3FF", border: "#DDD6FE", label: "Manual" },
  llm: { color: "#0E7490", bg: "#ECFEFF", border: "#A5F3FC", label: "LLM" },
};

const styles = `
.cc-tabs{display:inline-flex;gap:4px;padding:4px;border-radius:10px;background:rgba(241,245,249,.85);border:1px solid ${C.border};}
.cc-tab{display:inline-flex;align-items:center;gap:7px;border:0;background:transparent;border-radius:7px;padding:7px 14px;font-size:13.5px;font-weight:600;color:${C.textSoft};cursor:pointer;font-family:inherit;}
.cc-tab.is-active{background:#fff;color:${C.primary};box-shadow:0 1px 3px rgba(15,23,42,.12);}
.cc-pill{display:inline-flex;align-items:center;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:700;white-space:nowrap;}
.cc-iconbtn{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:7px;border:1px solid ${C.border};background:#fff;color:${C.textSoft};cursor:pointer;}
.cc-iconbtn:hover:not(:disabled){color:${C.primary};border-color:#BFDBFE;}
.cc-iconbtn.danger:hover:not(:disabled){color:#B91C1C;border-color:#FCA5A5;}
.cc-iconbtn:disabled{opacity:.5;cursor:default;}
.cc-cellinput{width:100%;box-sizing:border-box;}
.cc-kwgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;}
.cc-kwcard{padding:14px 16px;display:flex;flex-direction:column;gap:10px;}
.cc-kwcard h3{margin:0;font-size:14.5px;color:${C.text};}
.cc-kwhint{margin:0;font-size:12.5px;color:${C.textSoft};}
.cc-kwadd{display:flex;gap:8px;}
.cc-kwadd .ha-input{flex:1;min-width:0;}
.cc-chips{display:flex;flex-wrap:wrap;gap:6px;}
.cc-chip{display:inline-flex;align-items:center;gap:6px;padding:3px 6px 3px 10px;border-radius:999px;background:#EFF6FF;border:1px solid #BFDBFE;color:#1E40AF;font-size:12.5px;font-weight:600;}
.cc-chip button{display:inline-flex;border:0;background:transparent;color:#64748B;cursor:pointer;padding:1px;border-radius:999px;}
.cc-chip button:hover{color:#B91C1C;background:#FEE2E2;}
.cc-pager{display:flex;align-items:center;justify-content:flex-end;gap:10px;padding:10px 14px;font-size:13px;color:${C.textSoft};}
`;

/** Pill showing an override's source (Manual purple, LLM teal). */
function SourcePill({ source }) {
  const st = SOURCE_STYLES[source] || SOURCE_STYLES.manual;
  return (
    <span className="cc-pill" style={{ color: st.color, background: st.bg, border: `1px solid ${st.border}` }}>
      {st.label}
    </span>
  );
}

/** Green auto-hiding success banner. */
function Notice({ text }) {
  if (!text) return null;
  return (
    <div role="status" style={{
      background: "#ECFDF3", border: "1px solid #A7F3D0", color: "#047857", borderRadius: 10,
      padding: "10px 16px", fontSize: 13, fontWeight: 500,
    }}>{text}</div>
  );
}

/** The three editable override fields as selects / a domain input with suggestions. */
function OverrideFields({ draft, setDraft, idPrefix }) {
  const set = (field) => (e) => setDraft((d) => ({ ...d, [field]: e.target.value }));
  const sizeOptions = draft.company_size && !COMPANY_SIZES.includes(draft.company_size)
    ? [draft.company_size, ...COMPANY_SIZES] : COMPANY_SIZES;
  return (
    <>
      <td className="ha-td">
        <select className="ha-input ha-select cc-cellinput" value={draft.hiring_entity} onChange={set("hiring_entity")} aria-label="Hiring entity">
          <option value="">— No override —</option>
          {HIRING_ENTITIES.map((v) => <option key={v} value={v}>{v}</option>)}
        </select>
      </td>
      <td className="ha-td">
        <select className="ha-input ha-select cc-cellinput" value={draft.company_size} onChange={set("company_size")} aria-label="Company size">
          <option value="">— No override —</option>
          {sizeOptions.map((v) => <option key={v} value={v}>{v}</option>)}
        </select>
      </td>
      <td className="ha-td">
        <input className="ha-input cc-cellinput" value={draft.domain} onChange={set("domain")} maxLength={50}
          list={`${idPrefix}-domains`} placeholder="No override" aria-label="Domain" />
        <datalist id={`${idPrefix}-domains`}>
          {DOMAIN_SUGGESTIONS.map((d) => <option key={d} value={d} />)}
        </datalist>
      </td>
    </>
  );
}

const draftFrom = (row) => ({
  hiring_entity: row?.hiring_entity || "",
  company_size: row?.company_size || "",
  domain: row?.domain || "",
});

const bodyFrom = (draft, extra = {}) => ({
  hiring_entity: draft.hiring_entity || null,
  company_size: draft.company_size || null,
  domain: draft.domain.trim() || null,
  ...extra,
});

/** Company overrides tab: search + source filter, paged table with inline edit / delete, and "Add override". */
function OverridesTab() {
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [q, setQ] = useState(""); // debounced query
  const [source, setSource] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState(null); // company_key being edited
  const [draft, setDraft] = useState(draftFrom(null));
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDraft, setNewDraft] = useState(draftFrom(null));
  const [busy, setBusy] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    const t = setTimeout(() => { setQ(query.trim()); setPage(1); }, 300);
    return () => clearTimeout(t);
  }, [query]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listCompanyOverrides({ q, source, page, page_size: PAGE_SIZE })
      .then((res) => {
        if (cancelled) return;
        setRows(res?.items || []);
        setTotal(res?.total || 0);
        setError("");
      })
      .catch((err) => { if (!cancelled) setError(err?.message || "Couldn't load overrides."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [q, source, page, reloadKey]);

  useEffect(() => {
    if (!notice) return undefined;
    const t = setTimeout(() => setNotice(""), 4000);
    return () => clearTimeout(t);
  }, [notice]);

  const reload = () => setReloadKey((k) => k + 1);

  /** PUT the edited row (full replace, source becomes manual) and merge the result in. */
  const saveEdit = async (row) => {
    setBusy(true);
    setError("");
    try {
      const saved = await saveCompanyOverride(row.company_key, bodyFrom(draft));
      setRows((list) => list.map((r) => (r.company_key === row.company_key ? saved : r)));
      setEditing(null);
      setNotice(`Saved ${saved.company_name}. Applies from the next harvest.`);
    } catch (err) {
      setError(err?.message || "Couldn't save the override.");
    } finally {
      setBusy(false);
    }
  };

  /** Create a manual override for a typed company name. */
  const saveNew = async () => {
    const name = newName.trim();
    if (!name) return;
    setBusy(true);
    setError("");
    try {
      const saved = await saveCompanyOverride(name, bodyFrom(newDraft, { company_name: name }));
      setAdding(false);
      setNewName("");
      setNewDraft(draftFrom(null));
      setNotice(`Saved ${saved.company_name}. Applies from the next harvest.`);
      reload();
    } catch (err) {
      setError(err?.message || "Couldn't save the override.");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (row) => {
    if (!window.confirm(`Remove the override for ${row.company_name}? Future harvests fall back to the keyword rules.`)) return;
    setBusy(true);
    setError("");
    try {
      await deleteCompanyOverride(row.company_key);
      setNotice(`Removed the override for ${row.company_name}.`);
      reload();
    } catch (err) {
      setError(err?.message || "Couldn't remove the override.");
    } finally {
      setBusy(false);
    }
  };

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const dash = <span style={{ color: "#94A3B8" }}>—</span>;

  return (
    <>
      {error && <div className="ha-errbanner">{error}</div>}
      <Notice text={notice} />

      <div className="ha-card ha-filterbar" style={{ padding: "11px 14px", gridTemplateColumns: "minmax(0, 1fr) 180px auto auto" }}>
        <div className="ha-filter-search" style={{ gridColumn: "auto" }}>
          <Search size={16} />
          <input className="ha-input" value={query} onChange={(e) => setQuery(e.target.value)}
            placeholder="Search company…" aria-label="Search companies" />
        </div>
        <select className="ha-input ha-select" value={source} onChange={(e) => { setSource(e.target.value); setPage(1); }} aria-label="Source">
          <option value="">All sources</option>
          <option value="manual">Manual</option>
          <option value="llm">LLM</option>
        </select>
        <button className="ha-btn ha-btn-secondary" onClick={reload} disabled={loading} title="Refresh">
          <RefreshCw size={16} className={loading ? "ha-spin" : ""} /> Refresh
        </button>
        <button className="ha-btn ha-btn-primary" onClick={() => setAdding(true)} disabled={adding}>
          <Plus size={16} /> Add override
        </button>
      </div>

      <div className="ha-card" style={{ overflow: "hidden" }}>
        <div className="ha-table-scroll" style={{ minHeight: 0 }}>
          <table className="ha-table" style={{ minWidth: 980 }}>
            <thead className="ha-thead">
              <tr>
                <PlainHeader label="Company" />
                <PlainHeader label="Hiring entity" width={170} />
                <PlainHeader label="Company size" width={190} />
                <PlainHeader label="Domain" width={170} />
                <PlainHeader label="Source" width={90} />
                <PlainHeader label="Updated" width={190} />
                <PlainHeader label="" align="right" width={90} />
              </tr>
            </thead>
            <tbody>
              {adding && (
                <tr className="ha-row">
                  <td className="ha-td">
                    <input className="ha-input cc-cellinput" value={newName} onChange={(e) => setNewName(e.target.value)}
                      placeholder="Company name" aria-label="Company name" autoFocus maxLength={500} />
                  </td>
                  <OverrideFields draft={newDraft} setDraft={setNewDraft} idPrefix="cc-new" />
                  <td className="ha-td"><SourcePill source="manual" /></td>
                  <td className="ha-td" />
                  <td className="ha-td" style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    <button className="cc-iconbtn" title="Save" onClick={saveNew} disabled={busy || !newName.trim()}><Check size={15} /></button>{" "}
                    <button className="cc-iconbtn" title="Cancel" onClick={() => { setAdding(false); setNewName(""); setNewDraft(draftFrom(null)); }} disabled={busy}><X size={15} /></button>
                  </td>
                </tr>
              )}
              {loading && rows.length === 0 && (
                <tr><td className="ha-td" colSpan={7} style={{ textAlign: "center", padding: "40px 16px", color: "#94A3B8" }}>Loading overrides…</td></tr>
              )}
              {!loading && rows.length === 0 && !adding && (
                <tr><td className="ha-td" colSpan={7} style={{ textAlign: "center", padding: "40px 16px", color: "#94A3B8" }}>
                  No overrides{q || source ? " match" : " yet — they're created here, from a job's \"Apply to all jobs\" edit, or by the harvest's LLM check"}.
                </td></tr>
              )}
              {rows.map((r) => {
                const isEditing = editing === r.company_key;
                return (
                  <tr key={r.company_key} className="ha-row">
                    <td className="ha-td" style={{ color: C.text, fontWeight: 500 }} title={r.company_key}>{r.company_name}</td>
                    {isEditing ? (
                      <OverrideFields draft={draft} setDraft={setDraft} idPrefix={`cc-${r.id}`} />
                    ) : (
                      <>
                        <td className="ha-td">{r.hiring_entity || (r.source === "llm" ? <span style={{ color: "#94A3B8" }}>Unknown</span> : dash)}</td>
                        <td className="ha-td">{r.company_size || dash}</td>
                        <td className="ha-td">{r.domain || dash}</td>
                      </>
                    )}
                    <td className="ha-td"><SourcePill source={r.source} /></td>
                    <td className="ha-td" style={{ color: C.textSoft, fontSize: 12.5 }}>
                      {fmtDate(r.updated_at)}
                      {r.updated_by_email && <div style={{ overflow: "hidden", textOverflow: "ellipsis" }} title={r.updated_by_email}>{r.updated_by_email}</div>}
                    </td>
                    <td className="ha-td" style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      {isEditing ? (
                        <>
                          <button className="cc-iconbtn" title="Save" onClick={() => saveEdit(r)} disabled={busy}><Check size={15} /></button>{" "}
                          <button className="cc-iconbtn" title="Cancel" onClick={() => setEditing(null)} disabled={busy}><X size={15} /></button>
                        </>
                      ) : (
                        <>
                          <button className="cc-iconbtn" title="Edit" onClick={() => { setEditing(r.company_key); setDraft(draftFrom(r)); }} disabled={busy}><Pencil size={14} /></button>{" "}
                          <button className="cc-iconbtn danger" title="Delete" onClick={() => remove(r)} disabled={busy}><Trash2 size={14} /></button>
                        </>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {total > PAGE_SIZE && (
          <div className="cc-pager">
            <span>{total} companies · page {page} of {pages}</span>
            <button className="ha-btn ha-btn-secondary" onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1 || loading}>Prev</button>
            <button className="ha-btn ha-btn-secondary" onClick={() => setPage((p) => Math.min(pages, p + 1))} disabled={page >= pages || loading}>Next</button>
          </div>
        )}
      </div>
    </>
  );
}

/** One keyword list: add box + removable chips. */
function KeywordCard({ list, items, onAdd, onRemove, busy }) {
  const [term, setTerm] = useState("");
  const submit = async (e) => {
    e.preventDefault();
    if (!term.trim()) return;
    if (await onAdd(list.key, term)) setTerm("");
  };
  return (
    <div className="ha-card cc-kwcard">
      <h3>{list.label} <span style={{ color: C.textSoft, fontWeight: 500 }}>({items.length})</span></h3>
      <p className="cc-kwhint">{list.hint}</p>
      <form className="cc-kwadd" onSubmit={submit}>
        <input className="ha-input" value={term} onChange={(e) => setTerm(e.target.value)} maxLength={200}
          placeholder="Add a word or phrase" aria-label={`Add to ${list.label}`} />
        <button className="ha-btn ha-btn-secondary" type="submit" disabled={busy || !term.trim()}><Plus size={15} /> Add</button>
      </form>
      <div className="cc-chips">
        {items.length === 0 && <span style={{ fontSize: 12.5, color: "#94A3B8" }}>No added terms — the built-in list applies.</span>}
        {items.map((k) => (
          <span key={k.id} className="cc-chip" title={k.created_by_email ? `Added by ${k.created_by_email}` : undefined}>
            {k.term}
            <button type="button" onClick={() => onRemove(k)} disabled={busy} aria-label={`Remove ${k.term}`}><X size={12} /></button>
          </span>
        ))}
      </div>
    </div>
  );
}

/** Keywords tab: the tenant's added staffing / direct / GCC terms. */
function KeywordsTab() {
  const [lists, setLists] = useState({ staffing: [], direct: [], gcc: [] });
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    return listClassificationKeywords()
      .then((res) => { setLists({ staffing: [], direct: [], gcc: [], ...(res?.lists || {}) }); setError(""); })
      .catch((err) => setError(err?.message || "Couldn't load keywords."))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!notice) return undefined;
    const t = setTimeout(() => setNotice(""), 4000);
    return () => clearTimeout(t);
  }, [notice]);

  const add = async (list, term) => {
    setBusy(true);
    setError("");
    try {
      const res = await addClassificationKeyword(list, term);
      setNotice(res?.created === false ? `"${res.term}" is already in the list.` : `Added "${res.term}". Applies from the next harvest.`);
      await load();
      return true;
    } catch (err) {
      setError(err?.message || "Couldn't add the term.");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const remove = async (k) => {
    setBusy(true);
    setError("");
    try {
      await deleteClassificationKeyword(k.id);
      setNotice(`Removed "${k.term}".`);
      await load();
    } catch (err) {
      setError(err?.message || "Couldn't remove the term.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {error && <div className="ha-errbanner">{error}</div>}
      <Notice text={notice} />
      <p className="ha-sub" style={{ margin: 0 }}>
        Terms are matched as whole words in company names, on top of the built-in lists. An unmatched company is
        Ambiguous and goes to the harvest's LLM check. Per-company corrections belong in Company overrides.
        {loading && " Loading…"}
      </p>
      <div className="cc-kwgrid">
        {KEYWORD_LISTS.map((l) => (
          <KeywordCard key={l.key} list={l} items={lists[l.key] || []} onAdd={add} onRemove={remove} busy={busy} />
        ))}
      </div>
    </>
  );
}

/**
 * Admin-only Company classification page: tabs for per-company overrides and the
 * editable keyword lists that future harvests apply.
 */
export default function CompanyClassificationPage() {
  const [tab, setTab] = useState("overrides");
  return (
    <main className="ha-main">
      <style>{styles}</style>
      <div className="ha-pagehead">
        <div className="ha-pagehead-titles">
          <h1>Company classification</h1>
          <p className="ha-sub">How harvests label who is hiring. A company override always wins (manual over LLM); otherwise the keyword lists decide, and companies they can't place are checked by the LLM once and cached here.</p>
        </div>
      </div>
      <div className="ha-pagebody">
        <div className="cc-tabs" role="tablist">
          <button role="tab" aria-selected={tab === "overrides"} className={"cc-tab" + (tab === "overrides" ? " is-active" : "")} onClick={() => setTab("overrides")}>
            <Building2 size={15} /> Company overrides
          </button>
          <button role="tab" aria-selected={tab === "keywords"} className={"cc-tab" + (tab === "keywords" ? " is-active" : "")} onClick={() => setTab("keywords")}>
            <Tags size={15} /> Keywords
          </button>
        </div>
        {tab === "overrides" ? <OverridesTab /> : <KeywordsTab />}
      </div>
    </main>
  );
}
