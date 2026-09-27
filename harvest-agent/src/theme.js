import { CheckCircle2, XCircle, Loader2, HelpCircle, Square } from "lucide-react";

/* Palette: Primary #2563EB · Secondary #1E40AF · Accent #F59E0B · BG #F8FAFC · Text #1E293B */
export const C = {
  primary: "#2563EB", secondary: "#1E40AF", accent: "#F59E0B",
  bg: "#F8FAFC", text: "#1E293B", textSoft: "#64748B",
  border: "#E2E8F0", sidebar: "#1E293B", pale: "#EFF6FF",
};

/* ── Glassmorphism recipe ─────────────────────────────────────────────────
   Shared by every card system (ha-/rec-/rr-/detail views), modeled on the
   Mail-logs .ha-stat cards. Each constant is a fragment of CSS declarations
   meant to be interpolated into a rule body. Glass only reads against a
   tinted backdrop, so any root that hosts glass cards must carry PAGE_BG. */
export const GLASS = `
  background:
    linear-gradient(150deg, rgba(59,130,246,.08), rgba(139,92,246,.04) 48%, rgba(255,255,255,0) 72%),
    linear-gradient(160deg, rgba(255,255,255,.9), rgba(255,255,255,.55) 75%);
  -webkit-backdrop-filter:blur(12px) saturate(150%);
  backdrop-filter:blur(12px) saturate(150%);
  border:1px solid rgba(255,255,255,.75);
  box-shadow:0 10px 30px -14px rgba(15,23,42,.28), inset 0 1px 0 rgba(255,255,255,.75);
`;
// Near-opaque variant for cards that hold dense tables/forms — glassy at the
// edges but effectively solid where text sits.
export const GLASS_SOLID = `
  background:linear-gradient(160deg, rgba(255,255,255,.97), rgba(255,255,255,.88));
  -webkit-backdrop-filter:blur(8px) saturate(140%);
  backdrop-filter:blur(8px) saturate(140%);
  border:1px solid rgba(255,255,255,.8);
  box-shadow:0 6px 18px -12px rgba(15,23,42,.18), inset 0 1px 0 rgba(255,255,255,.75);
`;
// Fixed page backdrop: soft blue/violet/mint tints over near-white.
export const PAGE_BG = `
  background:
    radial-gradient(1100px 620px at 8% -10%, rgba(59,130,246,.10), transparent 60%),
    radial-gradient(900px 560px at 100% 0%, rgba(139,92,246,.08), transparent 55%),
    radial-gradient(1000px 700px at 50% 112%, rgba(16,185,129,.07), transparent 60%),
    linear-gradient(180deg,#F6F8FC,#EEF2F9);
  background-attachment:fixed;
`;
// Browsers without backdrop-filter get a plain near-opaque card instead.
export const GLASS_FALLBACK = (selectors) => `
  @supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
    ${selectors}{background:rgba(255,255,255,.94);}
  }
`;

/* ── Level-2 + floating-layer fragments ───────────────────────────────────
   Layering doctrine: page root (PAGE_BG, no filter) → cards/bands/sidebar
   (GLASS*, blur) → anything INSIDE a glass card gets translucency only, NO
   backdrop-filter (nesting blur is muddy and spawns a compositor layer per
   element). Floating layers (dropdown panels, popovers, modal cards) may blur
   — their backdrop is real page content or the modal scrim. Semantic-color
   pills and solid primary/colored action buttons stay as they are. */

// Neutral/secondary button. Slate-tinted border so it keeps its edge on a
// near-white glass card.
export const GLASS_BTN = `
  background:linear-gradient(160deg, rgba(255,255,255,.9), rgba(255,255,255,.65));
  border:1px solid rgba(148,163,184,.45);
  box-shadow:0 2px 8px -4px rgba(15,23,42,.16), inset 0 1px 0 rgba(255,255,255,.75);
`;
export const GLASS_BTN_HOVER = `
  background:linear-gradient(160deg, rgba(255,255,255,1), rgba(255,255,255,.85));
  border-color:rgba(148,163,184,.65);
  box-shadow:0 4px 12px -6px rgba(15,23,42,.22), inset 0 1px 0 rgba(255,255,255,.9);
`;

// Readable translucent input; solidify on focus so typed text is
// max-contrast. Always keep the host system's focus ring alongside.
export const GLASS_INPUT = `
  background:rgba(255,255,255,.72);
  border:1px solid rgba(148,163,184,.55);
  box-shadow:inset 0 1px 2px rgba(15,23,42,.04);
`;
export const GLASS_INPUT_FOCUS = `
  background:rgba(255,255,255,.96);
`;

// Floating dropdown/popover: near-opaque so list text never fights the page,
// stronger blur (its backdrop is page content), deep detached shadow.
export const GLASS_PANEL = `
  background:linear-gradient(160deg, rgba(255,255,255,.97), rgba(255,255,255,.92));
  -webkit-backdrop-filter:blur(16px) saturate(160%);
  backdrop-filter:blur(16px) saturate(160%);
  border:1px solid rgba(255,255,255,.85);
  box-shadow:0 18px 40px -12px rgba(15,23,42,.28), 0 4px 12px -6px rgba(15,23,42,.12), inset 0 1px 0 rgba(255,255,255,.9);
`;

// Modal card over the dark scrim: forms sit on effectively solid ground, the
// glass reads at the edges.
export const GLASS_MODAL = `
  background:linear-gradient(165deg, rgba(255,255,255,.97), rgba(255,255,255,.9));
  -webkit-backdrop-filter:blur(20px) saturate(150%);
  backdrop-filter:blur(20px) saturate(150%);
  border:1px solid rgba(255,255,255,.85);
  box-shadow:0 24px 60px -12px rgba(15,23,42,.35), inset 0 1px 0 rgba(255,255,255,.9);
`;

// Stat-tile tint treatment, modeled on Mail Logs' .ha-stat. No blur of its
// own — stat tiles nest inside glass cards. Tint rides on --tint/--tint2
// custom props (blue defaults).
export const GLASS_STAT = `
  background:
    linear-gradient(150deg, var(--tint, rgba(37,99,235,.13)), var(--tint2, rgba(37,99,235,.04)) 52%, rgba(255,255,255,0) 78%),
    linear-gradient(160deg, rgba(255,255,255,.92), rgba(255,255,255,.6));
  border:1px solid rgba(255,255,255,.75);
  box-shadow:0 6px 20px -12px rgba(15,23,42,.22), inset 0 1px 0 rgba(255,255,255,.7);
  transition:transform .18s ease, box-shadow .18s ease;
`;
export const GLASS_STAT_HOVER = `
  transform:translateY(-4px);
  box-shadow:0 18px 34px -14px rgba(15,23,42,.34), inset 0 1px 0 rgba(255,255,255,.85);
`;

// React style-object twin of PAGE_BG for the few inline-styled screens
// (App.js "Restoring your session…"). Keep in sync with PAGE_BG.
export const PAGE_BG_STYLE = {
  background: `radial-gradient(1100px 620px at 8% -10%, rgba(59,130,246,.10), transparent 60%),
    radial-gradient(900px 560px at 100% 0%, rgba(139,92,246,.08), transparent 55%),
    radial-gradient(1000px 700px at 50% 112%, rgba(16,185,129,.07), transparent 60%),
    linear-gradient(180deg,#F6F8FC,#EEF2F9)`,
  backgroundAttachment: "fixed",
};

export const SRC_TONES = {
  Naukri: { background: "#EFF6FF", color: "#1E40AF" },
  Dice: { background: "#FEF3C7", color: "#92400E" },
  LinkedIn: { background: "#E0E7FF", color: "#3730A3" },
  // Home Feed leads — a distinct purple tone so they read differently from the
  // LinkedIn Jobs source everywhere a SourceChip appears (Jobs table, Run History).
  "LinkedIn Feed": { background: "#F5F3FF", color: "#6D28D9" },
};

export const STATUS_TONES = {
  success:    { background: "#ECFDF5", color: "#047857", Icon: CheckCircle2 },
  no_results: { background: "#F1F5F9", color: "#475569", Icon: HelpCircle },
  failed:     { background: "#FEF2F2", color: "#B91C1C", Icon: XCircle },
  running:    { background: "#FFFBEB", color: "#B45309", Icon: Loader2 },
  stopped:    { background: "#FFF7ED", color: "#9A3412", Icon: Square },
};

// Company-size tier → badge colours (bg tint + text). The tier is the headline
// value; the raw employee band renders small underneath. Unknown → neutral.
export const SIZE_TIER_STYLE = {
  Small:      { bg: "#ECFDF5", fg: "#047857" }, // emerald
  Medium:     { bg: "#EFF6FF", fg: "#1D4ED8" }, // blue
  Large:      { bg: "#FFFBEB", fg: "#B45309" }, // amber
  Enterprise: { bg: "#F5F3FF", fg: "#6D28D9" }, // violet
};
