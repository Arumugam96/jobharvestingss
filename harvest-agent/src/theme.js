import { CheckCircle2, XCircle, Loader2, HelpCircle, Square } from "lucide-react";

/* Palette: Primary #2563EB · Secondary #1E40AF · Accent #F59E0B · BG #F8FAFC · Text #1E293B */
export const C = {
  primary: "#2563EB", secondary: "#1E40AF", accent: "#F59E0B",
  bg: "#F8FAFC", text: "#1E293B", textSoft: "#64748B",
  border: "#E2E8F0", sidebar: "#1E293B", pale: "#EFF6FF",
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
