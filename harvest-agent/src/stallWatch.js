// Shared stall watchdog for harvest runs.
//
// A harvest run can sit in status "running" for a long time while the backend
// is actually stuck (e.g. the extraction LLM is down and each call waits out
// its timeout). The status endpoint keeps returning the same snapshot, so the
// UI would otherwise show an unchanging "Running…" forever. This tracks the
// last-changed "progress signature" and reports when it has been frozen past a
// threshold, so each run surface can surface a non-fatal "no progress" warning.
//
// The backend remains the source of truth for terminal state — this only warns.

export const STALL_WARN_MS = 2 * 60 * 1000; // 2 minutes with no forward progress

export const STALL_WARN_MSG =
  "No progress for 2 min — the LLM server may be slow or down.";

// Create a fresh tracker. Call note(signature) on each poll tick (or start()
// then tick for the pollless synchronous path); it returns
// { stalled, elapsedMs }. `stalled` flips true once `signature` has been
// unchanged for at least warnMs. Call reset() when a new run starts.
export function makeStallWatch(warnMs = STALL_WARN_MS) {
  let lastSig = null;
  let lastChange = Date.now();
  return {
    note(signature) {
      const now = Date.now();
      if (signature !== lastSig) {
        lastSig = signature;
        lastChange = now;
      }
      const elapsedMs = now - lastChange;
      return { stalled: elapsedMs >= warnMs, elapsedMs };
    },
    reset() {
      lastSig = null;
      lastChange = Date.now();
    },
  };
}
