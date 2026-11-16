import { useEffect, useRef, useState } from "react";
import { Download, ChevronDown } from "lucide-react";
import { C } from "../theme";

/* Split "Export | <format> ▾" button shared by Harvested Jobs and Phone logs: the left half
 * runs the export, the right half picks the format. The page owns the format state and
 * decides what each format does. */

const FORMAT_LABELS = { csv: "CSV", excel: "Excel", json: "JSON" };

/**
 * @param {object} props
 * @param {string[]} [props.formats] format keys offered (csv | excel | json)
 * @param {string} props.format the selected format
 * @param {Function} props.onFormatChange (format) => void
 * @param {Function} props.onExport runs the export in the selected format
 * @param {boolean} [props.exporting] shows "Exporting…" and disables the button
 * @param {string} [props.title] tooltip for the Export half
 */
export default function ExportButton({ formats = ["csv", "excel", "json"], format, onFormatChange, onExport, exporting = false, title }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  // Close the format menu on an outside click.
  useEffect(() => {
    if (!open) return undefined;
    /** Close the format menu when a mousedown lands outside it. */
    const close = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  return (
    <div ref={ref} style={{ position: "relative", display: "inline-flex", borderRadius: 8, border: `1px solid ${C.primary}` }}>
      <button
        className="ha-btn ha-btn-primary"
        style={{ border: "none", borderRadius: "7px 0 0 7px", borderRight: "1px solid rgba(255,255,255,.25)" }}
        onClick={onExport}
        disabled={exporting}
        title={title}
      >
        <Download size={16} /> {exporting ? "Exporting…" : "Export"}
      </button>
      <button
        style={{
          display: "inline-flex", alignItems: "center", gap: 4,
          background: C.primary, color: "#fff", border: "none",
          borderRadius: "0 7px 7px 0",
          padding: "7px 12px", fontSize: 13, fontWeight: 500,
          cursor: "pointer", fontFamily: "inherit", whiteSpace: "nowrap",
        }}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label="Export format"
      >
        {FORMAT_LABELS[format] || format}
        <ChevronDown size={14} style={{ transition: "transform .2s", transform: open ? "rotate(180deg)" : "none" }} />
      </button>
      {open && (
        <div style={{
          position: "absolute", top: "100%", right: 0, marginTop: 4,
          minWidth: 140, background: "#fff", border: `1px solid ${C.border}`,
          borderRadius: 8, boxShadow: "0 8px 24px -6px rgba(15,23,42,.18)",
          zIndex: 10, overflow: "hidden",
        }}>
          {formats.map((fmt) => (
            <button
              key={fmt}
              onClick={() => { onFormatChange(fmt); setOpen(false); }}
              style={{
                display: "flex", alignItems: "center", gap: 8, width: "100%",
                padding: "9px 14px", border: "none", background: format === fmt ? `color-mix(in srgb, ${C.primary} 8%, transparent)` : "transparent",
                color: C.text, fontSize: 13, fontWeight: 500, cursor: "pointer",
                fontFamily: "inherit", textAlign: "left",
              }}
            >
              {FORMAT_LABELS[fmt] || fmt}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
