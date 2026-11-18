import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import { useLocation, useNavigate, useNavigationType } from "react-router-dom";

/* App-wide scroll restoration for the SPA (mounted once in HarvestAgent).
 *
 * The list pages (Harvested Jobs, Run History, …) unmount when a full-page detail
 * route opens, so on Back the browser has nothing to restore: the window AND each
 * table's own scroller (.ha-table-scroll — the jobs/runs tables scroll inside a
 * max-height box) came back at the top. This component:
 *   • saves window.scrollX/Y + scrollTop/scrollLeft of every SCROLLERS element
 *     (by document order) on every scroll, keyed by the history entry's
 *     location.key, plus a pathname+search fallback index — kept in a module Map
 *     mirrored to sessionStorage (so it also survives a reload of the tab);
 *   • on a POP (browser Back/Forward, or reload) — or a PUSH carrying
 *     state.restoreScroll — re-applies the saved position after render, retrying
 *     each animation frame for up to RESTORE_MS until the content is tall enough
 *     (rows can render a beat after the route mounts), and stops as soon as the
 *     user scrolls/clicks/types;
 *   • on any other PUSH scrolls the window to the top;
 *   • ignores REPLACE (the jobs table mirrors its filters/page into the URL with
 *     replace — same view, so the position stays and is re-saved under the new key).
 * history.scrollRestoration is set to "manual" so the browser doesn't fight it. */

const STORAGE_KEY = "ha-scroll-positions:v1";
const MAX_ENTRIES = 80;
const RESTORE_MS = 1500;
// Scroll containers to track, besides the window: the shared table scroller,
// the Recruiter Contact Finder page (its own 100vh scroller), and an opt-in hook.
const SCROLLERS = ".ha-table-scroll, .rcf, [data-scroll-restore]";

const byKey = new Map();  // history entry key → snapshot
const byPath = new Map(); // pathname+search → latest snapshot for that URL

(function hydrate() {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const data = JSON.parse(raw);
    (data.k || []).forEach(([k, v]) => byKey.set(k, v));
    (data.p || []).forEach(([k, v]) => byPath.set(k, v));
  } catch { /* storage blocked or corrupt — start empty */ }
}());

let persistTimer = 0;
/** Debounced write of both maps to sessionStorage (best-effort). */
function persistSoon() {
  if (persistTimer) return;
  persistTimer = window.setTimeout(persistNow, 250);
}
/** Writes both maps to sessionStorage immediately. */
function persistNow() {
  if (persistTimer) { window.clearTimeout(persistTimer); persistTimer = 0; }
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ k: [...byKey], p: [...byPath] }));
  } catch { /* quota / blocked — the in-memory maps still work */ }
}

/** Sets a map entry as the most recent one, evicting the oldest past MAX_ENTRIES. */
function remember(map, key, value) {
  map.delete(key);
  map.set(key, value);
  while (map.size > MAX_ENTRIES) map.delete(map.keys().next().value);
}

const pathKey = (loc) => loc.pathname + loc.search;
// The very first entry of a tab has key "default" until it is replaced — scope it
// by URL so two different "default" entries (across reloads) never collide.
const entryKey = (loc) => (loc.key && loc.key !== "default" ? loc.key : `default:${pathKey(loc)}`);

/** Current window + scroller positions. */
function snapshot() {
  return {
    x: window.scrollX,
    y: window.scrollY,
    els: Array.from(document.querySelectorAll(SCROLLERS), (el) => [el.scrollTop, el.scrollLeft]),
  };
}

/** Saves the current positions under the given location. */
function save(loc) {
  const snap = snapshot();
  remember(byKey, entryKey(loc), snap);
  remember(byPath, pathKey(loc), snap);
  persistSoon();
}

/**
 * Applies a snapshot. Each target is only set once the content can actually reach
 * it (otherwise the browser would clamp it); with `force` it is set regardless.
 * @returns {boolean} true when every target was reached.
 */
function apply(target, force) {
  let done = true;
  const els = document.querySelectorAll(SCROLLERS);
  (target.els || []).forEach(([top, left], i) => {
    if (!top && !left) return;
    const el = els[i];
    if (!el) { done = false; return; }
    const reachable = el.scrollHeight - el.clientHeight >= top - 1 && el.scrollWidth - el.clientWidth >= left - 1;
    if (reachable || force) {
      if (el.scrollTop !== top) el.scrollTop = top;
      if (el.scrollLeft !== left) el.scrollLeft = left;
    }
    if (!reachable) done = false;
  });
  // Window last: a restored inner scroller never changes the page height, but
  // rows landing inside it can.
  const doc = document.documentElement;
  const reachable = doc.scrollHeight - window.innerHeight >= target.y - 1
    && doc.scrollWidth - window.innerWidth >= target.x - 1;
  if (reachable || force) {
    if (window.scrollX !== target.x || window.scrollY !== target.y) window.scrollTo(target.x, target.y);
  }
  if (!reachable) done = false;
  return done;
}

const STOP_EVENTS = ["wheel", "touchstart", "keydown", "mousedown"];

/** Restores `target`, retrying per frame for up to RESTORE_MS. Returns a cancel fn. */
function restore(target) {
  const start = performance.now();
  let raf = 0;
  let stopped = false;
  const stop = () => {
    if (stopped) return;
    stopped = true;
    cancelAnimationFrame(raf);
    STOP_EVENTS.forEach((t) => window.removeEventListener(t, stop, true));
  };
  // The user taking over (scrolling, clicking, typing) ends the restore.
  STOP_EVENTS.forEach((t) => window.addEventListener(t, stop, { capture: true, passive: true }));
  const step = () => {
    if (stopped) return;
    const timedOut = performance.now() - start >= RESTORE_MS;
    if (apply(target, timedOut) || timedOut) { stop(); return; }
    raf = requestAnimationFrame(step);
  };
  step();
  return stop;
}

/** Mount once inside the router: saves and restores scroll positions per history entry. */
export default function ScrollRestoration() {
  const location = useLocation();
  const navType = useNavigationType();
  const locRef = useRef(location);
  const cancelRef = useRef(null);

  // Take scroll restoration away from the browser for the app's lifetime.
  useEffect(() => {
    const h = window.history;
    if (!("scrollRestoration" in h)) return undefined;
    const prev = h.scrollRestoration;
    h.scrollRestoration = "manual";
    return () => { h.scrollRestoration = prev; };
  }, []);

  // Save continuously. A capturing listener on document also sees element
  // scrolls (scroll events don't bubble); rAF-throttled to one save per frame.
  useEffect(() => {
    let raf = 0;
    const onScroll = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => { raf = 0; save(locRef.current); });
    };
    const onHide = () => persistNow();
    document.addEventListener("scroll", onScroll, { capture: true, passive: true });
    window.addEventListener("pagehide", onHide);
    return () => {
      document.removeEventListener("scroll", onScroll, { capture: true });
      window.removeEventListener("pagehide", onHide);
      cancelAnimationFrame(raf);
      persistNow();
    };
  }, []);

  // React to navigations right after the new route's DOM is committed (before paint).
  useLayoutEffect(() => {
    locRef.current = location;
    if (navType === "REPLACE") {
      // Same view, new URL/key (e.g. the jobs table syncing ?page=…): keep any
      // restore running and file the current position under the new entry too.
      save(location);
      return;
    }
    if (cancelRef.current) { cancelRef.current(); cancelRef.current = null; }
    const wantRestore = navType === "POP" || location.state?.restoreScroll === true;
    const target = wantRestore
      ? (navType === "POP" && byKey.get(entryKey(location))) || byPath.get(pathKey(location))
      : null;
    if (target) {
      cancelRef.current = restore(target);
    } else {
      window.scrollTo(0, 0);
    }
  }, [location, navType]);

  // Cancel a pending restore on unmount.
  useEffect(() => () => { if (cancelRef.current) cancelRef.current(); }, []);

  return null;
}

/**
 * In-app "Back" for full-page detail routes. When the page was opened in-app
 * (navigation state carries `from`, and there's an earlier entry in this tab's
 * history), it goes history-back — a POP, so ScrollRestoration puts the list back
 * exactly where it was, and the history stack isn't padded with a duplicate list
 * entry. Otherwise (deep link / new tab) it pushes `from` with restoreScroll (best
 * match by URL), or `fallback`.
 * @param {string} fallback Path to open when there is no `from`.
 * @returns {() => void}
 */
export function useBackNavigation(fallback) {
  const navigate = useNavigate();
  const location = useLocation();
  const from = location.state?.from;
  return useCallback(() => {
    const idx = window.history.state?.idx;
    if (from && typeof idx === "number" && idx > 0) { navigate(-1); return; }
    if (from) navigate(from, { state: { restoreScroll: true } });
    else navigate(fallback);
  }, [navigate, from, fallback]);
}
