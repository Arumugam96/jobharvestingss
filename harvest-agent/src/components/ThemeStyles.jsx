import {
  C, GLASS, GLASS_SOLID, PAGE_BG, GLASS_FALLBACK,
  GLASS_BTN, GLASS_INPUT, GLASS_INPUT_FOCUS, GLASS_PANEL, GLASS_STAT, GLASS_STAT_HOVER,
} from "../theme";

const ThemeStyles = () => (
  <style>{`
    .ha-root{display:flex;min-height:100vh;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;${PAGE_BG}color:${C.text};}
    .ha-main{flex:1;min-width:0;overflow-x:hidden;}
    /* Sidebar styles now live in Sidebar.jsx (the shared component). */
    /* Cards are frosted glass over the ha-root gradient; the near-opaque
       variant keeps table/form text fully legible. */
    .ha-card{${GLASS_SOLID}border-radius:12px;}
    .ha-input{box-sizing:border-box;height:38px;padding:0 12px;${GLASS_INPUT}color:${C.text};border-radius:8px;font-size:14px;}
    .ha-input:focus{outline:none;border-color:${C.primary};box-shadow:0 0 0 2px rgba(37,99,235,.25);${GLASS_INPUT_FOCUS}}
    .ha-btn{display:inline-flex;align-items:center;gap:8px;border-radius:8px;padding:8px 16px;font-size:14px;cursor:pointer;transition:.15s;}
    .ha-btn-primary{border:0;font-weight:600;background:${C.primary};color:#fff;box-shadow:0 2px 6px rgba(37,99,235,.35);}
    .ha-btn-primary:hover{background:${C.secondary};}
    .ha-btn-secondary{font-weight:500;${GLASS_BTN}color:${C.primary};}
    .ha-btn-secondary:hover{background:rgba(239,246,255,.92);border-color:#BFDBFE;}
    .ha-btn-secondary:disabled{opacity:.6;cursor:default;}
    .ha-table{width:100%;min-width:1080px;border-collapse:collapse;font-size:14px;table-layout:fixed;}
    .ha-table-scroll{overflow-x:auto;overflow-y:auto;max-height:400px;scrollbar-width:thin;scrollbar-color:#CBD5E1 transparent;}
    .ha-table-scroll::-webkit-scrollbar{height:9px;width:9px;}
    .ha-table-scroll::-webkit-scrollbar-track{background:transparent;}
    .ha-table-scroll::-webkit-scrollbar-thumb{background:#CBD5E1;border-radius:99px;}
    .ha-table-scroll::-webkit-scrollbar-thumb:hover{background:#94A3B8;}
    /* Sticky headers stay fully OPAQUE (no backdrop-filter): they must mask
       rows scrolling beneath, and sticky+blur is janky in Chromium/Safari.
       The vertical gradient tiles identically per cell, so it reads as one band. */
    .ha-thead{background:linear-gradient(180deg,#F8FAFF,#EEF3FB);text-align:left;position:sticky;top:0;z-index:1;}
    .ha-th{padding:12px 16px;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:${C.textSoft};text-align:left;position:sticky;top:0;background:linear-gradient(180deg,#F8FAFF,#EEF3FB);z-index:1;box-shadow:0 1px 0 rgba(148,163,184,.28);}
    .ha-sortbtn{display:inline-flex;align-items:center;gap:4px;border:0;background:transparent;cursor:pointer;font:inherit;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;}
    .ha-td{padding:14px 16px;vertical-align:middle;}
    .ha-row{border-top:1px solid #EEF2F7;}
    .ha-row:hover{background:rgba(226,236,254,.5);}
    .ha-link{color:${C.primary};font-weight:600;background:none;border:0;padding:0;cursor:pointer;font-size:inherit;font-family:inherit;text-align:left;}
    .ha-statnum{font-size:30px;font-weight:700;line-height:1;}
    .ha-statlbl{margin-top:8px;font-size:14px;color:${C.textSoft};}
    .ha-select{cursor:pointer;flex:1 1 auto;min-width:0;box-sizing:border-box;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
    .ha-multiselect-btn{display:flex;align-items:center;justify-content:space-between;gap:8px;font:inherit;color:inherit;text-align:left;}
    .ha-multiselect-btn svg{flex-shrink:0;color:#94A3B8;}
    .ha-multiselect-summary{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
    .ha-multiselect-panel{position:absolute;top:calc(100% + 6px);left:0;z-index:10;min-width:100%;width:max-content;max-width:260px;max-height:240px;overflow-y:auto;${GLASS_PANEL}border-radius:8px;padding:6px;}
    .ha-multiselect-opt{display:flex;align-items:center;gap:8px;padding:7px 8px;border-radius:6px;font-size:14px;color:${C.text};cursor:pointer;white-space:nowrap;}
    .ha-multiselect-opt:hover{background:rgba(239,246,255,.8);}
    .ha-multiselect-opt input{cursor:pointer;}
    .ha-filterbar{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px 16px;align-items:end;width:100%;max-width:100%;box-sizing:border-box;}
    .ha-daterow{display:flex;flex-wrap:wrap;align-items:center;gap:12px;max-width:100%;box-sizing:border-box;}
    .ha-filter-field{display:flex;flex-direction:column;align-items:stretch;gap:6px;width:100%;min-width:0;box-sizing:border-box;}
    .ha-filter-field>span{font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:${C.textSoft};white-space:nowrap;display:flex;align-items:center;gap:5px;}
    .ha-filter-loc>span{color:${C.primary};}
    .ha-filter-loc .ha-select{background:#F4F8FF;border-color:#BFDBFE;}
    .ha-filter-search{position:relative;min-width:0;box-sizing:border-box;grid-column:1/-1;}
    .ha-filter-search .ha-input{width:100%;min-width:0;max-width:100%;padding-left:34px;box-sizing:border-box;}
    .ha-filter-search svg{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:#94A3B8;pointer-events:none;}
    .ha-pill{display:inline-block;border-radius:6px;padding:2px 10px;font-size:12px;font-weight:600;}
    .ha-act{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:8px;${GLASS_BTN}color:${C.textSoft};cursor:pointer;transition:.15s;}
    .ha-act:hover{border-color:${C.primary};color:${C.primary};background:rgba(239,246,255,.9);}
    .ha-mail{color:${C.primary};text-decoration:none;}
    .ha-mail:hover{text-decoration:underline;}
    .ha-tel{color:${C.text};text-decoration:none;}
    .ha-tel:hover{text-decoration:underline;}
    .ha-cbtn{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:8px;border:1px solid;transition:.15s;text-decoration:none;}
    .ha-cbtn-on{border-color:${C.primary};color:${C.primary};background:rgba(255,255,255,.85);cursor:pointer;}
    .ha-cbtn-on:hover{background:${C.primary};color:#fff;}
    .ha-cbtn-off{border-color:${C.border};color:#CBD5E1;background:${C.bg};cursor:not-allowed;}
    /* Outreach already sent — a filled green state so a contacted recruiter is
       obvious at a glance (backed by the DB, so it survives a refresh). */
    .ha-cbtn-sent{border-color:#86EFAC;color:#047857;background:#ECFDF5;cursor:pointer;}
    .ha-cbtn-sent:hover{background:#047857;color:#fff;}
    .ha-spin{animation:ha-rot .9s linear infinite;}
    @keyframes ha-rot{to{transform:rotate(360deg);}}
    .ha-errbanner{background:#FEF2F2;border:1px solid #FCA5A5;color:#B91C1C;border-radius:10px;padding:10px 16px;font-size:13px;font-weight:500;}
    .ha-breakdown{display:flex;flex-wrap:wrap;gap:6px;font-size:12px;color:${C.textSoft};}
    .ha-breakdown b{color:${C.text};}
    .ha-detail-page{padding:24px;width:100%;box-sizing:border-box;}
    .ha-detail-back{display:inline-flex;align-items:center;gap:7px;cursor:pointer;${GLASS_BTN}color:#334155;font-size:13px;font-weight:600;padding:8px 14px;border-radius:8px;margin-bottom:18px;}
    .ha-detail-back:hover{background:rgba(241,245,249,.92);}
    .ha-detail-card{${GLASS}border-radius:14px;padding:24px;margin-bottom:16px;}
    .ha-detail-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px;margin-top:16px;}
    .ha-detail-stat{background:${C.pale};border-radius:10px;padding:14px 16px;}
    .ha-detail-stat b{display:block;font-size:22px;color:${C.text};}
    .ha-detail-stat span{font-size:12px;color:${C.textSoft};}
    .ha-runhead{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;flex-wrap:wrap;}
    .ha-runhead-meta{flex:0 1 auto;min-width:220px;}
    .ha-runhead-right{flex:1 1 440px;min-width:280px;display:flex;flex-direction:column;align-items:flex-end;gap:12px;}
    .ha-runstats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;width:100%;}
    .ha-runstat{${GLASS_STAT}border-radius:10px;padding:11px 14px;}
    .ha-runstat b{display:block;font-size:22px;font-weight:700;line-height:1.1;color:${C.text};}
    .ha-runstat span{display:block;margin-top:3px;font-size:11.5px;font-weight:500;color:${C.textSoft};}
    .ha-runstat-btn{font:inherit;text-align:left;width:100%;box-sizing:border-box;cursor:pointer;transition:transform .18s ease, box-shadow .18s ease;}
    .ha-runstat-btn:hover{transform:translateY(-2px);box-shadow:0 10px 22px -12px rgba(15,23,42,.3), inset 0 1px 0 rgba(255,255,255,.85);}
    .ha-runstat-btn:active{transform:translateY(1px);}
    .ha-runstat-active{box-shadow:0 0 0 2px rgba(37,99,235,.55), 0 6px 20px -12px rgba(15,23,42,.22), inset 0 1px 0 rgba(255,255,255,.85);}
    @media(max-width:720px){.ha-runhead-right{align-items:stretch;flex-basis:100%;}.ha-runhead-right .ha-pill{align-self:flex-start;}}
    @media(max-width:520px){.ha-runstats{grid-template-columns:repeat(2,minmax(0,1fr));}}
    /* Stat tiles (JobsPage/RunHistory StatCard) — Mail-logs .ha-stat treatment
       layered over the .ha-card base; tint comes from --tint/--tint2 vars. */
    .ha-statcard{${GLASS_STAT}}
    .ha-statcard:hover{${GLASS_STAT_HOVER}}
    @media (prefers-reduced-motion: reduce){
      .ha-statcard,.ha-runstat,.ha-runstat-btn{transition:none;}
      .ha-statcard:hover,.ha-runstat-btn:hover{transform:none;}
    }
    ${GLASS_FALLBACK(".ha-card,.ha-detail-card,.ha-multiselect-panel")}
  `}</style>
);

export default ThemeStyles;
