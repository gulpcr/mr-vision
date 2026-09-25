/**
 * Bridge to the embedded OHIF viewer (same-origin iframe under /ohif/).
 *
 * The ONLY module that touches OHIF internals. It relies on globals the OHIF 3.12.x
 * bundle sets on its window (`services`, `commandsManager`, `cornerstone`), so the
 * OHIF image is pinned in docker-compose.yml — re-check this file when upgrading it.
 * Every entry point degrades gracefully: if the globals are missing, `jumpToImage`
 * falls back to reloading the viewer at the image via OHIF's documented
 * `initialSeriesInstanceUID` / `initialSOPInstanceUID` URL params.
 */

export interface JumpTarget {
  seriesInstanceUID: string;
  sopInstanceUID: string;
  /** HU window to apply; omitted → keep the viewer's current W/L. */
  window?: { level: number; width: number } | null;
}

export interface FlagInfo {
  finding: string | null;
  instanceNumber: number | null;
  /** true → reviewed in detail and reported (strong red); false → screening-only flag
      (faint amber) — the fast first pass flags liberally, so these are unconfirmed. */
  reported: boolean;
}

export type JumpOutcome = "jumped" | "reloaded";

type OhifWin = Window & { services?: any; commandsManager?: any; cornerstone?: any };

const STACK_NEW_IMAGE = "CORNERSTONE_STACK_NEW_IMAGE";
const IMAGE_RENDERED = "CORNERSTONE_IMAGE_RENDERED";
const ELEMENT_ENABLED = "CORNERSTONE_ELEMENT_ENABLED";
const OVERLAY_CLASS = "mrcv-flag-overlay";

function ohifWindow(iframe: HTMLIFrameElement | null): OhifWin | null {
  try {
    return (iframe?.contentWindow as OhifWin | null) ?? null;
  } catch {
    return null; // cross-origin (should not happen: same host via nginx)
  }
}

function isReady(w: OhifWin | null): boolean {
  return !!(
    w?.services?.cornerstoneViewportService &&
    w.services.displaySetService &&
    w.services.viewportGridService &&
    w.cornerstone?.getRenderingEngines?.()?.length
  );
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Resolve once OHIF has booted and has a rendering engine; false on timeout. */
export async function waitForOhif(iframe: HTMLIFrameElement | null, timeoutMs = 20000): Promise<boolean> {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (isReady(ohifWindow(iframe))) return true;
    await sleep(250);
  }
  return false;
}

/** SOP Instance UID of a Cornerstone imageId (metadata first, wadors URL fallback). */
function sopOf(w: OhifWin, imageId: string | undefined): string | null {
  if (!imageId) return null;
  try {
    const inst = w.cornerstone?.metaData?.get?.("instance", imageId);
    if (inst?.SOPInstanceUID) return String(inst.SOPInstanceUID);
  } catch {
    /* fall through */
  }
  const m = /\/instances\/([^/]+)/.exec(imageId);
  return m ? m[1] : null;
}

function stackViewport(w: OhifWin, viewportId: string): any | null {
  const vp = w.services.cornerstoneViewportService.getCornerstoneViewport(viewportId);
  return vp && typeof vp.getImageIds === "function" && typeof vp.setImageIdIndex === "function"
    ? vp
    : null;
}

function indexOfSop(w: OhifWin, vp: any, sop: string): number {
  const ids: string[] = vp?.getImageIds?.() ?? [];
  return ids.findIndex((id) => sopOf(w, id) === sop);
}

/**
 * Show the image in the active viewport: switch it to the right series if needed,
 * scroll to the image, apply the tile's W/L. Falls back to reloading the viewer
 * at the image (W/L not applied) when OHIF can't be driven directly.
 */
export async function jumpToImage(
  iframe: HTMLIFrameElement | null,
  target: JumpTarget,
  fallbackBaseUrl: string,
): Promise<JumpOutcome> {
  try {
    const w = ohifWindow(iframe);
    if (w && isReady(w)) {
      const { displaySetService, viewportGridService } = w.services;
      const ds = (displaySetService.getActiveDisplaySets() || []).find(
        (d: any) => d.SeriesInstanceUID === target.seriesInstanceUID && !d.unsupported,
      );
      const viewportId: string | undefined = viewportGridService.getActiveViewportId?.();
      if (ds && viewportId) {
        const shown: string[] = viewportGridService.getDisplaySetsUIDsForViewport(viewportId) || [];
        if (!shown.includes(ds.displaySetInstanceUID)) {
          w.commandsManager.runCommand("setDisplaySetsForViewports", {
            viewportsToUpdate: [{ viewportId, displaySetInstanceUIDs: [ds.displaySetInstanceUID] }],
          });
        }
        // The viewport is (re)built asynchronously after a series switch.
        const t0 = Date.now();
        while (Date.now() - t0 < 10000) {
          const vp = stackViewport(w, viewportId);
          const idx = vp ? indexOfSop(w, vp, target.sopInstanceUID) : -1;
          if (vp && idx >= 0) {
            await vp.setImageIdIndex(idx);
            if (target.window) {
              const { level, width } = target.window;
              vp.setProperties({ voiRange: { lower: level - width / 2, upper: level + width / 2 } });
            }
            vp.render();
            return "jumped";
          }
          await sleep(200);
        }
      }
    }
  } catch (e) {
    console.warn("OHIF jump failed; reloading viewer at image", e);
  }

  if (iframe) {
    const sep = fallbackBaseUrl.includes("?") ? "&" : "?";
    iframe.src =
      `${fallbackBaseUrl}${sep}initialSeriesInstanceUID=${encodeURIComponent(target.seriesInstanceUID)}` +
      `&initialSOPInstanceUID=${encodeURIComponent(target.sopInstanceUID)}`;
  }
  return "reloaded";
}

function ensureOverlay(doc: Document, element: HTMLElement): HTMLElement {
  let el = element.querySelector<HTMLElement>(`:scope > .${OVERLAY_CLASS}`);
  if (el) return el;
  if (doc.defaultView?.getComputedStyle(element).position === "static") {
    element.style.position = "relative";
  }
  el = doc.createElement("div");
  el.className = OVERLAY_CLASS;
  el.setAttribute("aria-live", "polite");
  Object.assign(el.style, {
    position: "absolute",
    inset: "0",
    pointerEvents: "none",
    zIndex: "20",
    display: "none",
  } as Partial<CSSStyleDeclaration>);
  const banner = doc.createElement("div");
  Object.assign(banner.style, {
    position: "absolute",
    top: "8px",
    left: "50%",
    transform: "translateX(-50%)",
    maxWidth: "85%",
    padding: "4px 10px",
    borderRadius: "6px",
    color: "#fff",
    font: "600 12px/1.35 system-ui, sans-serif",
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
  } as Partial<CSSStyleDeclaration>);
  el.appendChild(banner);
  element.appendChild(el);
  return el;
}

/**
 * Red border + "AI FLAGGED" banner on any stack viewport while it displays a reported
 * flagged image (faint dashed amber + "Screening flag" for screening-only flags).
 * Follows viewports OHIF creates later (layout / series changes). Returns a detach
 * function. MPR/volume viewports are not highlighted.
 */
export function attachFlagHighlight(
  iframe: HTMLIFrameElement | null,
  flagged: Map<string, FlagInfo>,
): () => void {
  const w = ohifWindow(iframe);
  if (!w || !isReady(w)) return () => {};
  const doc = w.document;
  const cs = w.cornerstone;
  const listeners: Array<[EventTarget, string, EventListener]> = [];
  const attached = new WeakSet<HTMLElement>();

  const update = (element: HTMLElement) => {
    let imageId: string | undefined;
    try {
      imageId = cs.getEnabledElement(element)?.viewport?.getCurrentImageId?.();
    } catch {
      imageId = undefined;
    }
    const sop = sopOf(w, imageId);
    const info = sop ? flagged.get(sop) : undefined;
    const existing = element.querySelector<HTMLElement>(`:scope > .${OVERLAY_CLASS}`);
    if (!info) {
      if (existing) existing.style.display = "none";
      return;
    }
    const overlay = existing ?? ensureOverlay(doc, element);
    const banner = overlay.firstElementChild as HTMLElement;
    if (info.reported) {
      overlay.style.border = "4px solid #ef4444";
      overlay.style.boxShadow = "inset 0 0 24px rgba(239,68,68,0.45)";
      banner.style.background = "rgba(220,38,38,0.92)";
    } else {
      overlay.style.border = "2px dashed rgba(245,158,11,0.75)";
      overlay.style.boxShadow = "none";
      banner.style.background = "rgba(180,83,9,0.8)";
    }
    const parts = [info.reported ? "AI FLAGGED · reported" : "Screening flag · not reviewed"];
    if (info.instanceNumber != null) parts.push(`image ${info.instanceNumber}`);
    if (info.finding) parts.push(info.finding);
    banner.textContent = parts.join(" · ");
    banner.title = info.finding ?? "";
    overlay.style.display = "block";
  };

  const attach = (element: HTMLElement | null | undefined) => {
    if (!element || attached.has(element)) return;
    attached.add(element);
    const onImage: EventListener = () => update(element);
    for (const evt of [STACK_NEW_IMAGE, IMAGE_RENDERED]) {
      element.addEventListener(evt, onImage);
      listeners.push([element, evt, onImage]);
    }
    update(element);
  };

  try {
    for (const engine of cs.getRenderingEngines() || []) {
      for (const vp of engine.getViewports?.() || []) attach(vp.element);
    }
  } catch (e) {
    console.warn("Could not attach flag highlight to OHIF viewports", e);
  }
  const onEnabled: EventListener = (evt: any) => attach(evt?.detail?.element);
  cs.eventTarget?.addEventListener(ELEMENT_ENABLED, onEnabled);
  if (cs.eventTarget) listeners.push([cs.eventTarget, ELEMENT_ENABLED, onEnabled]);

  return () => {
    for (const [target, evt, fn] of listeners) target.removeEventListener(evt, fn);
    doc.querySelectorAll(`.${OVERLAY_CLASS}`).forEach((n) => n.remove());
  };
}
