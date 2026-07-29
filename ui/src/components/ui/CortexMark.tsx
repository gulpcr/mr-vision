import clsx from "clsx";

/**
 * Cortex Radiology brand mark — a diagnostic "reticle" (crosshair over a target
 * ring), mirrored from the brand site (cortexradiology.ai). Stroke follows the
 * theme accent (cyan in dark, teal in light) via `currentColor`, so wrap it in a
 * `text-accent` element (or pass `className` with a text-color utility).
 */
export function CortexMark({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
      className={clsx("text-accent", className)}
    >
      <circle cx="16" cy="16" r="13" stroke="currentColor" strokeWidth="1.6" opacity="0.5" />
      <circle cx="16" cy="16" r="4.4" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M16 1.5V9M16 23v7.5M1.5 16H9M23 16h7.5"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}
