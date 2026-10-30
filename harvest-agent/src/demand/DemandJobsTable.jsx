import { useMemo, useState } from "react";
import { Search } from "lucide-react";
import { Select, SortHeader, PlainHeader, SourceChip } from "../components/ui";
import { RowStatusPill, ATTENTION, isActiveRow, pct, subStepLabel } from "./demandUi";

/* The Demand jobs table of one batch: the jobs of the batch (live and final
 * statuses), Resdex-only columns and filters — no Company / POC / Email / Mobile
 * / contact actions (Demand jobs have no contact). Queued and in-progress rows
 * show their status and a non-clickable title; finished rows open the Demand job
 * page. The status filter is controlled by the batch page (its stat cards set it). */

export const STATUS_FILTERS = [
  { value: "all", label: "All" },
  { value: "active", label: "Queued / in progress" },
  { value: "finished", label: "Finished" },
  { value: "matched", label: "Matched" },
  { value: "reused", label: "Reused" },
  { value: "no_results", label: "No match" },
  { value: "queries_only", label: "Queries only" },
  { value: "attention", label: "Needs attention" },
  { value: "cap_reached", label: "Daily cap reached" },
  { value: "session_expired", label: "Login needed" },
  { value: "error", label: "Error" },
  { value: "stopped", label: "Stopped" },
];

export function rowMatchesStatus(r, f) {
  if (f === "all") return true;
  if (f === "active") return isActiveRow(r);
  if (f === "finished") return !isActiveRow(r);
  if (f === "attention") return ATTENTION.includes(r.status);
  return r.status === f;
}

function CoverageCell({ value }) {
  const v = pct(value);
  if (v == null) return <span className="dm-muted">—</span>;
  return (
    <div className="dm-cov">
      <div className="dm-track"><i className="amber" style={{ width: `${v}%` }} /></div>
      <b>{v}%</b>
    </div>
  );
}

export default function DemandJobsTable({ rows, uploadedOn, status, onStatusChange, onOpenJob }) {
  const [jobFilter, setJobFilter] = useState("all");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState({ col: "row", dir: "asc" });

  const jobOptions = useMemo(
    () => [{ value: "all", label: "All" }, ...rows.filter((r) => r.job_id).map((r) => ({ value: r.job_id, label: r.job_title }))],
    [rows],
  );

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const list = rows.filter((r) => rowMatchesStatus(r, status)
      && (jobFilter === "all" || r.job_id === jobFilter)
      && (!needle || `${r.job_title} ${r.client} ${r.location} ${r.skills}`.toLowerCase().includes(needle)));
    if (sort.col === "job_title") {
      list.sort((a, b) => (a.job_title || "").localeCompare(b.job_title || "") * (sort.dir === "asc" ? 1 : -1));
    }
    return list;
  }, [rows, status, jobFilter, q, sort]);

  const clients = new Set(rows.map((r) => (r.client || "").trim().toLowerCase()).filter(Boolean)).size;

  return (
    <div className="ha-card dm-card" style={{ minWidth: 0 }}>
      <div className="dm-tbl-head">
        <div className="t">Demand jobs in this batch</div>
        <div className="stats"><span><b>{rows.length}</b> jobs</span><span><b>{clients}</b> {clients === 1 ? "client" : "clients"}</span></div>
      </div>
      <div className="ha-filterbar" style={{ marginBottom: 10 }}>
        <Select label="Resdex status" value={status} onChange={onStatusChange} options={STATUS_FILTERS} />
        <Select label="Job" value={jobFilter} onChange={setJobFilter} options={jobOptions} />
        <div className="ha-filter-search">
          <Search size={15} />
          <input className="ha-input" aria-label="Search jobs" placeholder="Search title, client, skill…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      </div>
      <div className="dm-tbl-wrap">
        <div className="dm-tbl-scroll">
          <table className="ha-table" style={{ minWidth: 1240 }}>
            <thead>
              <tr>
                <SortHeader label="Job title" col="job_title" sort={sort} setSort={setSort} width={250} />
                <PlainHeader label="Client" width={150} />
                <PlainHeader label="Location" width={140} />
                <PlainHeader label="Experience" width={110} />
                <PlainHeader label="Resdex status" width={190} />
                <PlainHeader label="Candidates" width={110} />
                <PlainHeader label="Top skill coverage" width={160} />
                <PlainHeader label="Source" width={100} />
                <PlainHeader label="Uploaded" width={130} />
              </tr>
            </thead>
            <tbody>
              {shown.length === 0 ? (
                <tr><td className="ha-td dm-empty" colSpan={9}>No jobs match your filters. Clear a filter to see more.</td></tr>
              ) : shown.map((r) => {
                const active = isActiveRow(r);
                const searchedOrReused = !active && !["stopped", "queries_only"].includes(r.status);
                return (
                  <tr key={r.job_id || r.row} className="ha-row">
                    <td className="ha-td">
                      {active || !r.job_id ? (
                        <span className="dm-ttl-off">{r.job_title}<small>{r.status === "queued" ? "Opens when searched" : subStepLabel(r)}</small></span>
                      ) : (
                        <button type="button" className="ha-link" title="View details" onClick={() => onOpenJob(r)}>{r.job_title}</button>
                      )}
                    </td>
                    <td className="ha-td">{r.client || <span className="dm-muted">—</span>}</td>
                    <td className="ha-td">{r.location || <span className="dm-muted">—</span>}</td>
                    <td className="ha-td dm-num">{r.experience || <span className="dm-muted">—</span>}</td>
                    <td className="ha-td"><RowStatusPill row={r} /></td>
                    <td className="ha-td dm-num">{searchedOrReused ? <b>{r.candidates || 0}</b> : <span className="dm-muted">—</span>}</td>
                    <td className="ha-td">{searchedOrReused ? <CoverageCell value={r.top_coverage} /> : <span className="dm-muted">—</span>}</td>
                    <td className="ha-td"><SourceChip source="Demand" /></td>
                    <td className="ha-td dm-soft dm-num" style={{ whiteSpace: "nowrap" }}>{uploadedOn || "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
      <div className="dm-pager">
        <span>Showing {shown.length} of {rows.length}</span>
        <span>Company, POC, Email, Mobile and Contact columns are hidden for Demand jobs.</span>
      </div>
    </div>
  );
}
