"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Animated number that counts up to its target when it mounts / when the value
 * changes. Non-numeric values (e.g. "Online", "—") are rendered verbatim with no
 * animation. Respects prefers-reduced-motion by snapping straight to the value.
 */
export function CountUp({
  value,
  durationMs = 900,
  className,
}: {
  value: number | string;
  durationMs?: number;
  className?: string;
}) {
  const isNumeric = typeof value === "number" && isFinite(value);
  const [display, setDisplay] = useState<number>(isNumeric ? 0 : 0);
  const frame = useRef<number | null>(null);

  useEffect(() => {
    if (!isNumeric) return;
    const target = value as number;
    const reduce =
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce || target === 0) {
      setDisplay(target);
      return;
    }
    const start = performance.now();
    const from = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs);
      // ease-out-expo
      const eased = t === 1 ? 1 : 1 - Math.pow(2, -10 * t);
      setDisplay(from + (target - from) * eased);
      if (t < 1) frame.current = requestAnimationFrame(tick);
    };
    frame.current = requestAnimationFrame(tick);
    return () => {
      if (frame.current) cancelAnimationFrame(frame.current);
    };
  }, [value, isNumeric, durationMs]);

  if (!isNumeric) return <span className={className}>{value}</span>;

  const isInt = Number.isInteger(value as number);
  return (
    <span className={className}>
      {isInt ? Math.round(display).toLocaleString() : display.toFixed(1)}
    </span>
  );
}
