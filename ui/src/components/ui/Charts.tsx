"use client";

import { useState } from "react";

// ── Area trend ───────────────────────────────────────────────────────────────
// Single-series area+line over time. One accent hue (no legend needed — the card
// title names the series). HTML overlay drives a crosshair + tooltip so hit
// targets and positioning are robust to the SVG's responsive scaling.

export interface TrendPoint {
  label: string;
  value: number;
}

const TOP = 0.14; // fraction of height reserved above the peak
const BOT = 0.92; // baseline fraction

export function AreaTrend({
  data,
  className,
  height = 180,
}: {
  data: TrendPoint[];
  className?: string;
  height?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const n = data.length;
  const max = Math.max(1, ...data.map((d) => d.value));

  const xOf = (i: number) => (n <= 1 ? 0 : (i / (n - 1)) * 100);
  const yOf = (v: number) => (TOP + (1 - v / max) * (BOT - TOP)) * 100;

  const linePts = data.map((d, i) => `${xOf(i)},${yOf(d.value)}`).join(" ");
  const areaPts = `0,${BOT * 100} ${linePts} 100,${BOT * 100}`;

  const onMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const frac = (e.clientX - rect.left) / rect.width;
    setHover(Math.max(0, Math.min(n - 1, Math.round(frac * (n - 1)))));
  };

  return (
    <div className={className}>
      <div className="relative w-full" style={{ height }} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="absolute inset-0 w-full h-full">
          <defs>
            <linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="rgb(var(--accent))" stopOpacity="0.35" />
              <stop offset="100%" stopColor="rgb(var(--accent))" stopOpacity="0.02" />
            </linearGradient>
          </defs>
          {/* gridlines */}
          {[0.25, 0.5, 0.75].map((g) => (
            <line
              key={g}
              x1="0"
              x2="100"
              y1={(TOP + g * (BOT - TOP)) * 100}
              y2={(TOP + g * (BOT - TOP)) * 100}
              stroke="rgb(var(--border))"
              strokeWidth="0.5"
              vectorEffect="non-scaling-stroke"
              opacity="0.5"
            />
          ))}
          <polygon points={areaPts} fill="url(#areaFill)" />
          <polyline
            points={linePts}
            fill="none"
            stroke="rgb(var(--accent))"
            strokeWidth="2"
            strokeLinejoin="round"
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
          />
        </svg>

        {/* crosshair + hovered dot */}
        {hover !== null && (
          <>
            <div
              className="absolute top-0 bottom-0 w-px bg-accent/40 pointer-events-none"
              style={{ left: `${xOf(hover)}%` }}
            />
            <div
              className="absolute w-2.5 h-2.5 rounded-full bg-accent ring-2 ring-white dark:ring-[#0d1424] -translate-x-1/2 -translate-y-1/2 pointer-events-none shadow-glow-sm"
              style={{ left: `${xOf(hover)}%`, top: `${yOf(data[hover].value)}%` }}
            />
            <div
              className="absolute -translate-x-1/2 -translate-y-full mb-2 px-2.5 py-1.5 rounded-lg glass-raised shadow-glow text-center pointer-events-none whitespace-nowrap z-10"
              style={{ left: `${Math.min(90, Math.max(10, xOf(hover)))}%`, top: `${yOf(data[hover].value)}%` }}
            >
              <p className="text-sm font-bold text-gray-900 dark:text-gray-100 tabular-nums leading-none">{data[hover].value}</p>
              <p className="text-[10px] text-gray-500 dark:text-gray-400 mt-0.5">{data[hover].label}</p>
            </div>
          </>
        )}
      </div>

      {/* x-axis labels (first, middle, last) */}
      <div className="flex justify-between mt-2 text-[10px] text-gray-400 dark:text-gray-500 tabular-nums">
        {[0, Math.floor((n - 1) / 2), n - 1].map((i) => (
          <span key={i}>{data[i]?.label}</span>
        ))}
      </div>
    </div>
  );
}

// ── Donut ────────────────────────────────────────────────────────────────────
// Proportional ring with a center total and a labelled legend (identity never
// rests on color alone). Segments carry a 2px-equivalent gap between them.

export interface DonutSegment {
  label: string;
  value: number;
  color: string;
}

export function Donut({
  segments,
  size = 132,
  centerLabel,
  className,
}: {
  segments: DonutSegment[];
  size?: number;
  centerLabel?: string;
  className?: string;
}) {
  const total = segments.reduce((a, s) => a + s.value, 0);
  const [hover, setHover] = useState<number | null>(null);
  const R = 15.9155; // circumference ≈ 100
  const GAP = 1.2; // visual gap between segments (in circumference units)

  let offset = 25; // start at 12 o'clock
  const arcs = segments.map((s) => {
    const frac = total > 0 ? s.value / total : 0;
    const len = Math.max(0, frac * 100 - GAP);
    const arc = { ...s, len, dashoffset: offset };
    offset -= frac * 100;
    return arc;
  });

  return (
    <div className={`flex items-center gap-5 ${className ?? ""}`}>
      <div className="relative shrink-0" style={{ width: size, height: size }}>
        <svg viewBox="0 0 36 36" className="w-full h-full -rotate-0">
          <circle cx="18" cy="18" r={R} fill="none" stroke="rgb(var(--border))" strokeWidth="3.2" opacity="0.35" />
          {total > 0 &&
            arcs.map((a, i) =>
              // Skip zero-value segments entirely — a rounded line-cap would
              // otherwise render a stray dot for an empty slice.
              a.value <= 0 ? null : (
                <circle
                  key={a.label}
                  cx="18"
                  cy="18"
                  r={R}
                  fill="none"
                  stroke={a.color}
                  strokeWidth={hover === i ? 4 : 3.2}
                  strokeDasharray={`${a.len} ${100 - a.len}`}
                  strokeDashoffset={a.dashoffset}
                  strokeLinecap="round"
                  style={{ transition: "stroke-width 160ms ease" }}
                  onMouseEnter={() => setHover(i)}
                  onMouseLeave={() => setHover(null)}
                />
              )
            )}
        </svg>
        <div className="absolute inset-0 grid place-items-center text-center pointer-events-none">
          <div>
            <p className="text-2xl font-bold text-gray-900 dark:text-gray-100 tabular-nums leading-none">
              {hover !== null ? segments[hover].value : total}
            </p>
            <p className="text-[10px] uppercase tracking-wider text-gray-400 dark:text-gray-500 mt-1">
              {hover !== null ? segments[hover].label : centerLabel ?? "Total"}
            </p>
          </div>
        </div>
      </div>

      <ul className="space-y-2 min-w-0">
        {segments.map((s, i) => (
          <li
            key={s.label}
            className="flex items-center gap-2 text-sm cursor-default"
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(null)}
          >
            <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ background: s.color }} />
            <span className="text-gray-600 dark:text-gray-300 flex-1 truncate">{s.label}</span>
            <span className="font-semibold text-gray-900 dark:text-gray-100 tabular-nums">{s.value}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
