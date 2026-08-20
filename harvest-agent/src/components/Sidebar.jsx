import React from "react";
import {
  SlidersHorizontal, LayoutList, History, Send, BarChart3, Radar, UserSearch, LogOut,
} from "lucide-react";
import HealthBadge from "./HealthBadge";

/*
 * App sidebar — the single source of truth for the left navigation.
 *
 * Every top-level page (Rule Engine, Harvested Jobs, Run History, Source Runs,
 * Lead Intelligence) renders this one component, so a change here shows up on
 * all of them. It carries its own <style> block (the `ha-sidebar`/`ha-nav`
 * rules that used to live inline in HarvestAgent's ThemeStyles) so it renders
 * identically no matter which page's theme CSS is loaded — including
 * RuleEngineConfig, which has its own separate `rec-` design system.
 *
 * Nav keys line up with HarvestAgent's `activePage` values
 * ("rules" | "jobs" | "history" | "sources" | "leads"); pass the current one as
 * `activePage` to highlight it. "Outreach" and "Analytics" have no target yet,
 * so they render as inert items (no onClick), matching the previous behaviour.
 */
function NavItem({ glyph: Glyph, children, active, badge, onClick }) {
  return (
    <button className={"ha-nav" + (active ? " ha-nav-active" : "")} onClick={onClick}>
      <Glyph size={18} />
      <span style={{ flex: 1, textAlign: "left" }}>{children}</span>
      {badge != null && <span className="ha-badge">{badge}</span>}
    </button>
  );
}

export default function Sidebar({ activePage, onNavigate = () => {}, jobsCount, runsCount, onLogout }) {
  return (
    <aside className="ha-sidebar">
      <style>{styles}</style>
      <div style={{ padding: "0 5px", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center" }}>
        <img className="ha-logo-img" src={`${process.env.PUBLIC_URL}/sight_spectrum_logo.jpg`} alt="SS jobharvesting Agent" width="150" height="150" />
        <div className="ha-tagline">Contract Sourcing Automation</div>
      </div>
      <nav style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 24, flex: 1 }}>
        <div>
          <div className="ha-navhead">Configuration</div>
          <NavItem glyph={SlidersHorizontal} active={activePage === "rules"} onClick={() => onNavigate("rules")}>Rule Engine</NavItem>
        </div>
        <div>
          <div className="ha-navhead">Operations</div>
          <NavItem glyph={LayoutList} active={activePage === "jobs"} badge={jobsCount} onClick={() => onNavigate("jobs")}>Harvested Jobs</NavItem>
          <NavItem glyph={History} active={activePage === "history"} badge={runsCount} onClick={() => onNavigate("history")}>Run History</NavItem>
          <NavItem glyph={Radar} active={activePage === "sources"} onClick={() => onNavigate("sources")}>Source Runs</NavItem>
          <NavItem glyph={UserSearch} active={activePage === "leads"} onClick={() => onNavigate("leads")}>Lead Intelligence</NavItem>
          <NavItem glyph={Send}>Outreach</NavItem>
        </div>
        <div>
          <div className="ha-navhead">Reports</div>
          <NavItem glyph={BarChart3}>Analytics</NavItem>
        </div>
      </nav>
      <div style={{ padding: "0 12px", display: "flex", flexDirection: "column", gap: 8 }}>
        {onLogout && (
          <button className="ha-nav ha-logout" onClick={onLogout}>
            <LogOut size={18} />
            <span style={{ flex: 1, textAlign: "left" }}>Sign out</span>
          </button>
        )}
        <HealthBadge />
      </div>
    </aside>
  );
}

/* Sidebar-only styles — previously duplicated across HarvestAgent's ThemeStyles
   and RuleEngineConfig's `rec-sidebar` block; now defined once, here. */
const styles = `
  .ha-sidebar{display:none;width:240px;flex-shrink:0;flex-direction:column;padding:5px 5px;background:#1E293B;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;position:sticky;top:0;align-self:flex-start;height:100vh;overflow-y:auto;}
  @media(min-width:768px){.ha-sidebar{display:flex;}}
  .ha-logo-img{width:150px;height:150px;max-width:100%;object-fit:cover;display:block;border-radius:50%;padding:5px 0px 5px;}
  .ha-tagline{margin-top:4px;font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:#94A3B8;white-space:nowrap;}
  .ha-navhead{padding:0 12px 8px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.14em;color:#64748B;}
  .ha-nav{display:flex;width:100%;align-items:center;gap:12px;border:0;background:transparent;cursor:pointer;border-radius:8px;padding:8px 12px;font-size:14px;color:#CBD5E1;transition:.15s;}
  .ha-nav:hover{background:rgba(255,255,255,.06);color:#fff;}
  .ha-nav-active{background:rgba(37,99,235,.28);color:#fff;box-shadow:inset 0 0 0 1px rgba(37,99,235,.55);}
  .ha-badge{background:#F59E0B;color:#1E293B;border-radius:999px;padding:2px 8px;font-size:12px;font-weight:700;}
  .ha-logout:hover{background:rgba(239,68,68,.14);color:#FCA5A5;}
`;
