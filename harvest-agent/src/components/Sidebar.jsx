import React, { useEffect, useState } from "react";
import {
  SlidersHorizontal, LayoutList, History, Send, BarChart3, Radar, UserSearch, LogOut,
  ChevronsLeft, ChevronsRight, Contact,
} from "lucide-react";
import HealthBadge from "./HealthBadge";
import { useTenant } from "../TenantContext";

/*
 * App sidebar — the single source of truth for the left navigation.
 *
 * Collapsible rail: rests at 72px (icons only). Hovering (or keyboard focus) PEEKS
 * it open to 240px; that peek collapses again when the pointer/focus leaves. Clicking
 * a NAV OPTION commits it open, so it then stays open while you work in the main page
 * — main-page clicks never close it — persisted in localStorage under
 * "ha.sidebar.pinned" so it survives a reload. When open, the top chevron points left
 * ("«") and closes the rail. Expansion is IN-FLOW — the `ha-sb-slot` wrapper's width
 * animates, so the main page slides with the sidebar rather than being overlaid. The
 * aside itself is sticky inside the slot and simply fills it.
 *
 * Every top-level page (Rule Engine, Harvested Jobs, Run History, Source Runs,
 * Lead Intelligence) renders this one component, so a change here shows up on
 * all of them. It carries its own <style> block so it renders identically no
 * matter which page's theme CSS is loaded — including the Rule Engine pages,
 * which have their own separate design systems.
 *
 * Multi-tenant: nav items are gated by the tenant slip's feature flags
 * (TenantContext) — internal sees the full nav, client tenants only the pages
 * their tenant config enables. Branding comes from the slip too: the dark
 * glass gradient is tinted from theme.accent (blue for client_us, teal for
 * client_in; internal keeps neutral slate), and the active nav item renders
 * as a white "attached tab" — full-bleed to the right edge with concave
 * fillets — whose icon/label take the tenant colour (--navAcc). This gating
 * is presentation only; the backend enforces access independently.
 *
 * Nav keys line up with HarvestAgent's `activePage` values
 * ("rules" | "jobs" | "history" | "sources" | "leads" | "outreach"); pass the
 * current one as `activePage` to highlight it. "Analytics" has no target yet,
 * so it renders as an inert item (no onClick).
 */
function NavItem({ glyph: Glyph, children, active, badge, onClick }) {
  return (
    <button className={"ha-nav" + (active ? " ha-nav-active" : "")} onClick={onClick} title={typeof children === "string" ? children : undefined}>
      <Glyph size={18} />
      <span className="ha-nav-label">{children}</span>
      {badge != null && <span className="ha-badge">{badge}</span>}
    </button>
  );
}

// Convert #RRGGBB to [r, g, b]; null when the hex doesn't parse.
function hexToRgbArr(hex) {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || "");
  return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : null;
}
const mix = (c, t, k) => Math.round(c * (1 - k) + t * k);

// Per-tenant sidebar tint, derived from the tenant slip's accent: a brighter
// accent tone up top fading to near-black, so the gradient visibly reads.
// Internal (or an unparseable accent) keeps the neutral slate glass.
function sidebarVars(accent, isClient) {
  const rgb = isClient ? hexToRgbArr(accent) : null;
  if (!rgb) return { "--sb1": "rgba(30,41,59,.88)", "--sb2": "rgba(15,23,42,.94)", "--navAcc": "#2563EB" };
  const [r, g, b] = rgb;
  return {
    "--sb1": `rgba(${mix(r, 11, 0.18)},${mix(g, 18, 0.18)},${mix(b, 32, 0.18)},.92)`,
    "--sb2": `rgba(${mix(r, 5, 0.78)},${mix(g, 7, 0.78)},${mix(b, 15, 0.78)},.96)`,
    "--navAcc": accent,
  };
}

const LS_PINNED = "ha.sidebar.pinned";

export default function Sidebar({ activePage, onNavigate = () => {}, jobsCount, runsCount, onLogout, railCollapsed = false }) {
  const { tenant, theme, isClient, hasFeature } = useTenant();

  // `committed` = the persisted "keep the rail open" choice. Turned ON when the
  // user clicks a nav option (or the open button), OFF by the close button — so
  // navigating keeps the rail open, while a plain hover does NOT latch it. Persisted
  // under the same localStorage key so the choice survives a reload.
  const [committed, setCommitted] = useState(() => {
    try { return localStorage.getItem(LS_PINNED) === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem(LS_PINNED, committed ? "1" : "0"); } catch { /* storage unavailable */ }
  }, [committed]);

  // `peeking` = transient hover/focus peek. The rail is open when either is set; a
  // peek collapses when the pointer/focus leaves, but a committed rail does not — so
  // a click anywhere in the main page never closes it.
  const [peeking, setPeeking] = useState(false);
  // On a full-bleed console page (railCollapsed, e.g. Recruiter Contacts) the rail
  // rests collapsed regardless of the persisted latch — it still PEEKS open on
  // hover/focus so the nav stays reachable, and the saved choice is untouched for
  // every other page.
  const open = railCollapsed ? peeking : (committed || peeking);

  // Clicking a nav option commits the rail open, then navigates.
  const go = (key) => { setCommitted(true); onNavigate(key); };

  const tintVars = sidebarVars(theme.accent, isClient);
  // Workspace label reads best as a lightened accent on the dark glass.
  const accRgb = hexToRgbArr(theme.accent);
  const workspaceColor = accRgb
    ? `rgb(${mix(accRgb[0], 255, 0.68)},${mix(accRgb[1], 255, 0.68)},${mix(accRgb[2], 255, 0.68)})`
    : "#93C5FD";

  const showConfiguration = hasFeature("rules");
  const showReports = hasFeature("analytics");

  return (
    <div
      className={"ha-sb-slot" + (open ? " is-open" : "")}
      style={tintVars}
      onMouseEnter={() => setPeeking(true)}
      onMouseLeave={() => setPeeking(false)}
      onFocus={() => setPeeking(true)}
      onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setPeeking(false); }}
    >
      <aside className="ha-sidebar">
        <style>{styles}</style>
        <button
          className="ha-sb-pin"
          onClick={(e) => {
            if (open) {
              // Open → the chevron points left ("«") and this closes the rail: blur so
              // a lingering focus can't re-open it, and drop both the latch and the peek.
              e.currentTarget.blur();
              setCommitted(false);
              setPeeking(false);
            } else {
              // Collapsed (keyboard fallback — the button is hidden in the rail): open + keep.
              setCommitted(true);
            }
          }}
          title={open ? "Close sidebar" : "Open sidebar"}
          aria-pressed={open}
        >
          {open ? <ChevronsLeft size={15} /> : <ChevronsRight size={15} />}
        </button>
        <div className="ha-sb-brand">
          <img className="ha-logo-img" src={`${process.env.PUBLIC_URL}/sight_spectrum_logo.jpg`} alt="SS jobharvesting Agent" width="150" height="150" />
          <div className="ha-tagline">Contract Sourcing Automation</div>
          {/* {isClient && (
            <div className="ha-workspace" style={{ color: workspaceColor }}>
              {tenant.name}
            </div>
          )} */}
        </div>
        <nav style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 10, flex: 1 }}>
          {showConfiguration && (
            <div>
              <div className="ha-navhead"><span>Configuration</span></div>
              <NavItem glyph={SlidersHorizontal} active={activePage === "rules"} onClick={() => go("rules")}>Rule Engine</NavItem>
            </div>
          )}
          <div>
            <div className="ha-navhead"><span>Operations</span></div>
            {hasFeature("jobs") && (
              <NavItem glyph={LayoutList} active={activePage === "jobs"} badge={jobsCount} onClick={() => go("jobs")}>Harvested Jobs</NavItem>
            )}
            {hasFeature("history") && (
              <NavItem glyph={History} active={activePage === "history"} badge={runsCount} onClick={() => go("history")}>Run History</NavItem>
            )}
            {hasFeature("sources") && (
              <NavItem glyph={Radar} active={activePage === "sources"} onClick={() => go("sources")}>Source Runs</NavItem>
            )}
            {hasFeature("leads") && (
              <NavItem glyph={UserSearch} active={activePage === "leads"} onClick={() => go("leads")}>Lead Intelligence</NavItem>
            )}
            {hasFeature("contacts") && (
              <NavItem glyph={Contact} active={activePage === "contacts"} onClick={() => go("contacts")}>Apollo Enrichment</NavItem>
            )}
            {hasFeature("outreach") && (
              <NavItem glyph={Send} active={activePage === "outreach"} onClick={() => go("outreach")}>Mail logs</NavItem>
            )}
          </div>
          {showReports && (
            <div>
              <div className="ha-navhead"><span>Reports</span></div>
              <NavItem glyph={BarChart3}>Analytics</NavItem>
            </div>
          )}
        </nav>
        <div style={{ padding: 0, display: "flex", flexDirection: "column", gap: 8 }}>
          {onLogout && (
            <button className="ha-nav ha-logout" onClick={onLogout} title="Sign out">
              <LogOut size={18} />
              <span className="ha-nav-label">Sign out</span>
            </button>
          )}
          <HealthBadge />
        </div>
      </aside>
    </div>
  );
}

/* Sidebar-only styles. Expand-state selectors key off the SLOT's `.is-open`
   class (JS-driven: hover/focus peek OR committed) since the sticky aside fills it. */
const styles = `
  /* In-flow slot: its width is the only thing the page layout sees, so both
     hover and pin push the main content (no overlay mode). */
  .ha-sb-slot{display:none;flex-shrink:0;width:72px;transition:width .25s ease;}
  @media(min-width:768px){.ha-sb-slot{display:block;}}
  /* Open state is JS-driven (.is-open = hover/focus peek OR committed-via-nav-click)
     so a committed rail stays open while the user clicks around the page instead of
     collapsing the moment the pointer leaves, the way a pure :hover rule would. */
  .ha-sb-slot.is-open{width:240px;}

  .ha-sidebar{position:sticky;top:0;height:100vh;width:100%;z-index:50;box-sizing:border-box;
    display:flex;flex-direction:column;padding:10px;overflow-x:hidden;overflow-y:auto;scrollbar-width:none;
    background:
      radial-gradient(360px 240px at 50% -60px, rgba(255,255,255,.16), transparent 70%),
      linear-gradient(175deg, var(--sb1), var(--sb2) 90%);
    -webkit-backdrop-filter:blur(16px) saturate(140%);backdrop-filter:blur(16px) saturate(140%);
    font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;}
  .ha-sidebar::-webkit-scrollbar{display:none;}
  .ha-sidebar *{box-sizing:border-box;}

  .ha-sb-pin{position:absolute;top:10px;right:10px;z-index:2;display:grid;place-items:center;
    width:26px;height:26px;border-radius:8px;border:1px solid rgba(255,255,255,.14);
    background:rgba(255,255,255,.08);color:#CBD5E1;cursor:pointer;opacity:0;transition:.18s;padding:0;}
  .ha-sb-slot.is-open .ha-sb-pin{opacity:1;}
  .ha-sb-pin:hover{background:rgba(255,255,255,.16);color:#fff;}

  .ha-sb-brand{display:flex;flex-direction:column;align-items:center;padding:6px 0 0;}
  .ha-logo-img{width:44px;height:44px;max-width:100%;object-fit:cover;display:block;border-radius:50%;
    transition:width .25s ease,height .25s ease;}
  .ha-sb-slot.is-open .ha-logo-img{width:96px;height:96px;}
  /* Near-white on the dark tenant glass — the old #94A3B8 was unreadable. */
  .ha-tagline{margin-top:8px;font-size:10.5px;text-transform:uppercase;letter-spacing:.14em;color:#E2E8F0;text-shadow:0 1px 2px rgba(2,6,23,.45);white-space:nowrap;}
  .ha-workspace{margin-top:5px;font-size:12px;font-weight:700;letter-spacing:.02em;text-shadow:0 1px 2px rgba(2,6,23,.35);white-space:nowrap;}
  .ha-tagline,.ha-workspace{opacity:0;height:0;overflow:hidden;transition:opacity .18s ease .05s;}
  .ha-sb-slot.is-open .ha-tagline,.ha-sb-slot.is-open .ha-workspace{opacity:1;height:auto;}

  /* Section headers read as hairline dividers in rail mode. */
  .ha-navhead{position:relative;height:18px;padding:0 5px 8px;font-size:10px;font-weight:600;
    text-transform:uppercase;letter-spacing:.14em;color:#94A3B8;white-space:nowrap;}
  .ha-navhead span{opacity:0;transition:opacity .18s ease .05s;}
  .ha-navhead::before{content:"";position:absolute;left:8px;right:8px;top:5px;height:1px;
    background:rgba(255,255,255,.12);transition:opacity .18s;}
  .ha-sb-slot.is-open .ha-navhead span{opacity:1;}
  .ha-sb-slot.is-open .ha-navhead::before{opacity:0;}

  /* Nav item — fixed icon column so glyphs don't shift while expanding.
     Hover: gently illuminates and drifts 2px toward the content. */
  .ha-nav{position:relative;display:flex;width:100%;align-items:center;gap:12px;border:0;background:transparent;
    cursor:pointer;border-radius:10px;padding:8px 12px 8px 17px;font-size:14px;color:#CBD5E1;
    transition:background .18s ease-out,color .18s ease-out,transform .18s ease-out;text-align:left;font-family:inherit;}
  .ha-nav svg{flex:none;}
  .ha-nav-label{flex:1;text-align:left;white-space:nowrap;opacity:0;transition:opacity .18s ease .05s;}
  .ha-sb-slot.is-open .ha-nav-label{opacity:1;}

  /* Active item AND hover: the "attached tab" — full-bleed to the sidebar's
     right edge with concave fillets, icon/label in the tenant colour. #F3F6FC
     matches the page gradient at the junction so the tab and the main page
     read as ONE surface (pure #fff looked whiter than the page). Hovering any
     item previews the same tab; the active one is additionally bold. Sign out
     keeps its red hover instead. */
  .ha-nav:hover:not(.ha-logout),.ha-nav-active,.ha-nav-active:hover{background:#F3F6FC;color:var(--navAcc);transform:none;}
  .ha-nav:hover:not(.ha-logout),.ha-nav-active{margin-right:-10px;width:calc(100% + 10px);border-radius:12px 0 0 12px;}
  .ha-nav-active{font-weight:700;}
  .ha-nav:hover:not(.ha-logout)::before,.ha-nav-active::before,
  .ha-nav:hover:not(.ha-logout)::after,.ha-nav-active::after{content:"";position:absolute;right:0;width:14px;height:14px;pointer-events:none;}
  .ha-nav:hover:not(.ha-logout)::before,.ha-nav-active::before{top:-14px;background:radial-gradient(circle at 0 0, rgba(0,0,0,0) 13.5px, #F3F6FC 14px);}
  .ha-nav:hover:not(.ha-logout)::after,.ha-nav-active::after{bottom:-14px;background:radial-gradient(circle at 0 100%, rgba(0,0,0,0) 13.5px, #F3F6FC 14px);}

  /* Count badge: docks to the icon's corner as a mini bubble in rail mode. */
  .ha-badge{background:#F59E0B;color:#1E293B;border-radius:999px;padding:2px 8px;font-size:12px;font-weight:700;
    font-variant-numeric:tabular-nums;transition:all .18s ease;}
  .ha-sb-slot:not(.is-open) .ha-badge{
    position:absolute;top:2px;left:28px;font-size:9px;padding:1px 5px;min-width:10px;text-align:center;}

  .ha-logout:hover{background:rgba(239,68,68,.14);color:#FCA5A5;}

  /* HealthBadge: dot always visible, text clipped in rail mode. */
  .ha-health{overflow:hidden;white-space:nowrap;}
  .ha-health-label{opacity:0;transition:opacity .18s ease .05s;}
  .ha-sb-slot.is-open .ha-health-label{opacity:1;}

  @media (prefers-reduced-motion: reduce){
    .ha-sb-slot,.ha-sidebar,.ha-nav,.ha-nav-label,.ha-logo-img,.ha-badge,.ha-tagline,.ha-workspace,
    .ha-navhead span,.ha-sb-pin,.ha-health-label{transition:none;}
  }
`;
