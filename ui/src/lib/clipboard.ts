"use client";

/**
 * Copy text to the clipboard, reporting whether it actually worked.
 *
 * `navigator.clipboard` only exists in secure contexts (HTTPS or localhost). On a plain
 * HTTP deployment it is undefined, and a naive `navigator.clipboard.writeText(...)` throws
 * — silently leaving the clipboard holding whatever was copied before (e.g. an older
 * invitation link). This falls back to a temporary textarea + execCommand("copy"), which
 * browsers still allow from a click handler, and returns false if both fail so the UI
 * can tell the user to copy manually.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to the legacy path
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    area.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}
