import React, { useEffect, useMemo, useRef, useState } from "react";
import { Search, ChevronDown, Check, Minus } from "lucide-react";

/*
 * Reusable searchable checkbox multi-select — collapsed trigger button that
 * opens a scrollable, searchable checkbox panel below it. Used for the Job
 * Domain redesign's IT / Non-IT category pickers (RuleEngineConfig.jsx), but
 * takes no domain-specific knowledge itself so it can be reused elsewhere.
 *
 * `selected`/`onChange` are lifted state — this component holds no selection
 * state of its own, only the open/closed flag and the search text, so
 * selections survive a search-text change untouched.
 */
export default function SearchableMultiSelect({
  options, selected, onChange, allLabel, selectAllLabel, searchPlaceholder, ariaLabel,
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef(null);
  const searchRef = useRef(null);
  const selectAllRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e) => { if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useEffect(() => {
    if (open) { setQuery(""); searchRef.current?.focus(); }
  }, [open]);

  const allChecked = options.length > 0 && selected.length === options.length;
  const someChecked = selected.length > 0 && !allChecked;
  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = someChecked;
  }, [someChecked, open]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter((o) => o.toLowerCase().includes(q));
  }, [options, query]);

  const toggleOne = (name) => {
    onChange(selected.includes(name) ? selected.filter((s) => s !== name) : [...selected, name]);
  };
  const toggleAll = () => { onChange(allChecked ? [] : [...options]); };

  const summary = allChecked
    ? allLabel
    : selected.length === 0
    ? "None selected"
    : selected.length === 1
    ? selected[0]
    : selected.length <= 2
    ? selected.join(", ")
    : `${selected.slice(0, 2).join(", ")} +${selected.length - 2} more`;

  return (
    <div className="sms-root" ref={rootRef}>
      <button type="button" className="sms-trigger" aria-haspopup="listbox" aria-expanded={open}
        aria-label={ariaLabel} onClick={() => setOpen((v) => !v)}>
        <span className="sms-trigger-label">{summary}</span>
        <ChevronDown size={16} className={"sms-caret" + (open ? " is-open" : "")} />
      </button>

      {open && (
        <div className="sms-panel" role="listbox" aria-label={ariaLabel}>
          <div className="sms-search">
            <Search size={14} />
            <input ref={searchRef} type="text" value={query} placeholder={searchPlaceholder}
              onChange={(e) => setQuery(e.target.value)} />
          </div>

          <label className="sms-row sms-row--all">
            <input ref={selectAllRef} type="checkbox" checked={allChecked} onChange={toggleAll} />
            <span className="sms-check"><Check size={12} /><Minus size={12} className="sms-check-mid" /></span>
            {selectAllLabel}
          </label>

          <div className="sms-list">
            {filtered.length === 0 && <div className="sms-empty">No categories match “{query}”.</div>}
            {filtered.map((name) => (
              <label className="sms-row" key={name}>
                <input type="checkbox" checked={selected.includes(name)} onChange={() => toggleOne(name)} />
                <span className="sms-check"><Check size={12} /></span>
                {name}
              </label>
            ))}
          </div>

          <div className="sms-footer">{selected.length} of {options.length} selected</div>
        </div>
      )}
      <style>{styles}</style>
    </div>
  );
}

const styles = `
  .sms-root { position:relative; }
  .sms-trigger {
    width:100%; height:40px; padding:0 12px; display:flex; align-items:center; justify-content:space-between; gap:8px;
    background:rgba(255,255,255,.72); border:1px solid rgba(148,163,184,.55); box-shadow:inset 0 1px 2px rgba(15,23,42,.04);
    border-radius:9px; font-size:14px; color:var(--text); cursor:pointer; text-align:left; transition:border-color .15s,box-shadow .15s;
  }
  .sms-trigger:hover { border-color:#CBD5E1; }
  .sms-trigger[aria-expanded="true"] { border-color:var(--primary); box-shadow:0 0 0 3px rgba(37,99,235,.12); background:rgba(255,255,255,.96); }
  .sms-trigger-label { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .sms-caret { color:#94A3B8; flex:none; transition:transform .15s; }
  .sms-caret.is-open { transform:rotate(180deg); }

  .sms-panel {
    position:absolute; top:calc(100% + 6px); left:0; right:0; z-index:30;
    background:rgba(255,255,255,.98); border:1px solid rgba(148,163,184,.5); border-radius:11px;
    box-shadow:0 14px 34px -12px rgba(15,23,42,.28); overflow:hidden;
    -webkit-backdrop-filter:blur(10px); backdrop-filter:blur(10px);
  }
  .sms-search { display:flex; align-items:center; gap:8px; padding:10px 12px; border-bottom:1px solid var(--line); color:#94A3B8; }
  .sms-search input { flex:1; border:none; outline:none; background:none; font-size:13.5px; color:var(--text); }
  .sms-row--all { border-bottom:1px solid var(--line); background:rgba(248,250,252,.7); }
  .sms-row {
    display:flex; align-items:center; gap:9px; padding:9px 12px; font-size:13.5px; color:var(--text);
    cursor:pointer; user-select:none; transition:background .12s;
  }
  .sms-row:hover { background:rgba(241,245,249,.8); }
  .sms-row input[type="checkbox"] { position:absolute; opacity:0; width:0; height:0; }
  .sms-check {
    flex:none; width:16px; height:16px; border-radius:4px; border:1.5px solid #CBD5E1; background:#fff;
    display:grid; place-items:center; color:#fff; position:relative; transition:background .15s,border-color .15s;
  }
  .sms-check svg { display:none; }
  .sms-check .sms-check-mid { color:#fff; }
  .sms-row input:checked + .sms-check { background:var(--green); border-color:var(--green); }
  .sms-row input:checked + .sms-check svg:first-child { display:block; }
  .sms-row input:indeterminate + .sms-check { background:var(--green); border-color:var(--green); }
  .sms-row input:indeterminate + .sms-check svg:first-child { display:none; }
  .sms-row input:indeterminate + .sms-check .sms-check-mid { display:block; }
  .sms-row input:not(:checked):not(:indeterminate) + .sms-check .sms-check-mid { display:none; }

  .sms-list { max-height:220px; overflow-y:auto; }
  .sms-empty { padding:16px 12px; font-size:12.8px; color:var(--muted); text-align:center; }
  .sms-footer { padding:8px 12px; font-size:11.5px; font-weight:600; color:#94A3B8; border-top:1px solid var(--line); background:rgba(248,250,252,.7); }

  @supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
    .sms-panel { background:#fff; }
  }
`;
