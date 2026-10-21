// Clipboard copy that also works over plain HTTP (the EC2 deploy on :8080 is a
// non-secure context, where navigator.clipboard is undefined).

/** Copies `text`; resolves true on success. Uses the async Clipboard API when the
 * page is a secure context, else (or if it rejects) the legacy execCommand path
 * through an off-screen textarea. Never throws. */
export async function copyText(text) {
  const value = String(text ?? "");
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch {
      // Permission denied / document not focused — fall through to execCommand.
    }
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = value;
    ta.setAttribute("readonly", "");
    Object.assign(ta.style, { position: "fixed", top: "-9999px", left: "-9999px", opacity: "0" });
    document.body.appendChild(ta);
    // Remember the user's selection so the copy doesn't clobber it.
    const sel = window.getSelection();
    const saved = [];
    if (sel) for (let i = 0; i < sel.rangeCount; i += 1) saved.push(sel.getRangeAt(i));
    ta.select();
    ta.setSelectionRange(0, value.length);
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } finally {
      document.body.removeChild(ta);
      if (sel) {
        sel.removeAllRanges();
        saved.forEach((r) => sel.addRange(r));
      }
    }
    return Boolean(ok);
  } catch {
    return false;
  }
}
