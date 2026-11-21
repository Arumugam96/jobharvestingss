// In-app link behaviour shared across pages: a plain click navigates in the SAME
// tab (client-side, keeping router state), while Ctrl/Cmd-click, Shift-click,
// middle-click and the browser's "Open link in new tab" still open a new tab.
// External links (LinkedIn, job posts, Resdex, mailto, …) don't use these — they
// keep target="_blank".
import { createElement } from "react";
import { ExternalLink } from "lucide-react";

/** True for an unmodified primary-button click — the only click an in-app link
 *  should intercept (modifier/middle clicks keep the browser's new-tab behaviour). */
export function isPlainLeftClick(e) {
  return !e.defaultPrevented && e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;
}

/** Open an app-relative URL in a new browser tab. */
export function openInNewTab(href) {
  window.open(href, "_blank", "noopener");
}

/**
 * Props for an in-app <a>: keeps the real `href` (so modifier/middle-click and the
 * context menu open a new tab) and runs `onNavigate` on a plain left click.
 * Usage: <a {...linkProps("/jobs/42", () => navigate("/jobs/42", { state }))}>
 */
export function linkProps(href, onNavigate) {
  return {
    href,
    onClick(e) {
      if (!isPlainLeftClick(e)) return;
      e.preventDefault();
      onNavigate(e);
    },
  };
}

/**
 * Props for a clickable non-anchor element (e.g. a table row) that navigates in-app:
 * plain click → `onNavigate()` (defaults to navigate(href)); Ctrl/Cmd/Shift-click or
 * middle-click → new tab; Enter/Space → in-app. Clicks that land on a nested
 * link/button are left to that control.
 * @param {string} href - app-relative URL the row stands for.
 * @param {Function} navigate - react-router navigate().
 * @param {Function} [onNavigate] - custom in-app navigation (e.g. to pass state).
 * @param {Function} [beforeOpen] - side effect run before either kind of navigation.
 */
export function rowNav(href, navigate, onNavigate, beforeOpen) {
  const go = () => { beforeOpen?.(); (onNavigate || (() => navigate(href)))(); };
  const newTab = () => { beforeOpen?.(); openInNewTab(href); };
  const fromNestedControl = (e) =>
    e.target !== e.currentTarget && e.target.closest?.("a, button, input, select, textarea, label");
  return {
    tabIndex: 0,
    onClick(e) {
      if (e.defaultPrevented || fromNestedControl(e)) return;
      if (e.ctrlKey || e.metaKey || e.shiftKey) { e.preventDefault(); newTab(); return; }
      go();
    },
    onAuxClick(e) {
      if (e.button !== 1 || e.defaultPrevented || fromNestedControl(e)) return;
      e.preventDefault();
      newTab();
    },
    // Suppress the middle-button autoscroll cursor so the aux click lands.
    onMouseDown(e) { if (e.button === 1) e.preventDefault(); },
    onKeyDown(e) {
      if (e.target !== e.currentTarget) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (e.ctrlKey || e.metaKey) newTab(); else go();
      }
    },
  };
}

/** Small ↗ icon link that opens an in-app `href` in a new tab without triggering
 *  the surrounding row's click. */
export function OpenInNewTab({ href, size = 13, style, onOpen, label = "Open in new tab" }) {
  return createElement(
    "a",
    {
      href,
      target: "_blank",
      rel: "noopener",
      title: label,
      "aria-label": label,
      className: "ha-open-newtab",
      onClick: (e) => { e.stopPropagation(); onOpen?.(); },
      onAuxClick: (e) => { e.stopPropagation(); if (e.button === 1) onOpen?.(); },
      onKeyDown: (e) => e.stopPropagation(),
      style: {
        display: "inline-flex", alignItems: "center", justifyContent: "center",
        color: "inherit", opacity: 0.6, padding: 2, borderRadius: 4,
        verticalAlign: "middle", flex: "none", ...style,
      },
    },
    createElement(ExternalLink, { size, "aria-hidden": true }),
  );
}
