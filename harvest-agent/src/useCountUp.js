import { useEffect, useRef, useState } from "react";

/**
 * Animate a displayed integer toward `target` with requestAnimationFrame, so a
 * value that jumps (e.g. 40 → 52 when a new batch is saved) eases up instead of
 * snapping. Continues smoothly from the currently-shown value if `target`
 * changes mid-animation. Honors `prefers-reduced-motion` (snaps instantly).
 *
 * Returns the current integer to render.
 */
export default function useCountUp(target, durationMs = 600) {
  const to = Number(target) || 0;
  const [display, setDisplay] = useState(to);
  const displayRef = useRef(to);
  const rafRef = useRef(null);

  // Track the latest rendered value so a re-triggered animation can continue
  // from where the number currently is, not from a stale start.
  useEffect(() => {
    displayRef.current = display;
  });

  useEffect(() => {
    const reduce =
      typeof window !== "undefined" &&
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const from = displayRef.current;
    if (reduce || from === to) {
      setDisplay(to);
      return;
    }

    let start = 0;
    const step = (ts) => {
      if (!start) start = ts;
      const t = Math.min(1, (ts - start) / durationMs);
      const eased = 1 - Math.pow(1 - t, 3); // easeOutCubic
      setDisplay(Math.round(from + (to - from) * eased));
      if (t < 1) rafRef.current = requestAnimationFrame(step);
    };
    rafRef.current = requestAnimationFrame(step);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [to, durationMs]);

  return display;
}
