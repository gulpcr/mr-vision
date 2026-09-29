"use client";

import type { SlicePanel } from "@/lib/api";

interface MontagePanelLinksProps {
  panels: SlicePanel[];
  /** Natural width / height of the tile image (null until it has loaded). */
  aspect: number | null;
  onPanelClick: (panel: SlicePanel) => void;
}

export function panelLabel(p: SlicePanel): string {
  const pos = p.stack_position ?? p.instance_number;
  return `${p.sequence}${pos != null ? ` · image ${pos}${p.n_images ? ` of ${p.n_images}` : ""}` : ""}`;
}

// Click targets over the sequence panels of an MRI montage tile. The tile shows the
// montage with object-contain inside a square box, so the hit areas live in a box with
// the image's own aspect ratio, centred the same way — each panel's rect (fractions of
// the image) then lines up with what is drawn.
export function MontagePanelLinks({ panels, aspect, onPanelClick }: MontagePanelLinksProps) {
  if (!aspect || panels.length === 0) return null;
  return (
    <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
      <div
        className="relative pointer-events-auto"
        style={{
          aspectRatio: String(aspect),
          maxWidth: "100%",
          maxHeight: "100%",
          width: aspect >= 1 ? "100%" : undefined,
          height: aspect < 1 ? "100%" : undefined,
        }}
      >
        {panels.map((p, i) => {
          const [x0, y0, x1, y1] = p.rect;
          const style = {
            left: `${x0 * 100}%`,
            top: `${y0 * 100}%`,
            width: `${(x1 - x0) * 100}%`,
            height: `${(y1 - y0) * 100}%`,
          };
          if (!p.clickable) {
            return (
              <div
                key={`${p.sequence}-${i}`}
                style={style}
                title={`${p.sequence}: ${p.reason ?? "not linked to a DICOM image"}`}
                className="absolute cursor-not-allowed"
              />
            );
          }
          const label = panelLabel(p);
          const approx = p.slice_offset != null && p.slice_offset > 0.25 ? " (nearest image)" : "";
          return (
            <button
              key={`${p.sequence}-${i}`}
              type="button"
              style={style}
              onClick={(e) => {
                e.stopPropagation();
                onPanelClick(p);
              }}
              title={`Open ${label} in the viewer${approx}`}
              aria-label={`Open ${label} in the viewer`}
              className="group/panel absolute cursor-pointer rounded-sm ring-inset transition-shadow hover:ring-2 hover:ring-primary-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400"
            >
              <span className="pointer-events-none absolute bottom-1 left-1 max-w-[calc(100%-0.5rem)] truncate rounded bg-black/75 px-1.5 py-0.5 text-[10px] font-medium text-white opacity-0 transition-opacity group-hover/panel:opacity-100 group-focus-visible/panel:opacity-100">
                {label}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
