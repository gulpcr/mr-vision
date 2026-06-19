"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, getFusedSliceUrl, FusedMeta } from "@/lib/api";
import { Loader2, ChevronLeft, ChevronRight } from "lucide-react";

type View = "axial" | "coronal" | "sagittal";
const VIEWS: View[] = ["axial", "coronal", "sagittal"];

/**
 * One fused PET/CT plane: a scrollable viewport (CT anatomy + PET hot-colormap
 * overlay) with a slice slider, mouse-wheel scrolling, and prev/next buttons.
 * Adjacent slices are prefetched (and the backend serves them with a 1h cache),
 * so scrolling is smooth.
 */
function FusedPane({
  studyUid,
  usecase,
  view,
  count,
  defaultSlice,
  showLesions,
}: {
  studyUid: string;
  usecase: string;
  view: View;
  count: number;
  defaultSlice: number;
  showLesions: boolean;
}) {
  const [slice, setSlice] = useState(defaultSlice);
  const [imgLoading, setImgLoading] = useState(true);
  const frameRef = useRef<HTMLDivElement>(null);

  const clamp = useCallback((n: number) => Math.max(0, Math.min(count - 1, n)), [count]);

  // Mouse-wheel scrolls slices (DICOM-style); non-passive so we can stop the
  // page from scrolling under the cursor.
  useEffect(() => {
    const el = frameRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      setSlice((s) => Math.max(0, Math.min(count - 1, s + (e.deltaY > 0 ? 1 : -1))));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [count]);

  // Prefetch nearby slices so scrolling doesn't wait on the network.
  useEffect(() => {
    for (const d of [1, -1, 2, -2, 3, -3]) {
      const i = slice + d;
      if (i >= 0 && i < count) {
        const im = new Image();
        im.src = getFusedSliceUrl(studyUid, usecase, view, i, showLesions);
      }
    }
  }, [slice, count, studyUid, usecase, view, showLesions]);

  useEffect(() => {
    setImgLoading(true);
  }, [slice, showLesions]);

  const src = getFusedSliceUrl(studyUid, usecase, view, slice, showLesions);

  return (
    <div className="flex flex-col bg-black rounded-md overflow-hidden border border-gray-800">
      <div className="px-2 py-1.5 text-[11px] font-semibold uppercase tracking-wider text-gray-300 border-b border-gray-800 flex items-center justify-between">
        <span className="capitalize">{view}</span>
        <span className="text-gray-500 tabular-nums normal-case">
          {slice + 1} / {count}
        </span>
      </div>
      <div
        ref={frameRef}
        className="relative flex items-center justify-center h-[300px] select-none"
      >
        {imgLoading && (
          <div className="absolute top-1.5 right-1.5 z-10">
            <Loader2 className="w-3.5 h-3.5 animate-spin text-gray-500" />
          </div>
        )}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={src}
          alt={`${view} fused PET/CT slice ${slice + 1}`}
          className="max-h-full max-w-full object-contain"
          onLoad={() => setImgLoading(false)}
          onError={() => setImgLoading(false)}
          draggable={false}
        />
      </div>
      <div className="flex items-center gap-2 px-2 py-2 border-t border-gray-800">
        <button
          onClick={() => setSlice((s) => clamp(s - 1))}
          disabled={slice <= 0}
          className="text-gray-400 hover:text-white disabled:opacity-30"
          aria-label="Previous slice"
        >
          <ChevronLeft className="w-4 h-4" />
        </button>
        <input
          type="range"
          min={0}
          max={count - 1}
          value={slice}
          onChange={(e) => setSlice(clamp(Number(e.target.value)))}
          className="flex-1 accent-primary-500 cursor-pointer"
        />
        <button
          onClick={() => setSlice((s) => clamp(s + 1))}
          disabled={slice >= count - 1}
          className="text-gray-400 hover:text-white disabled:opacity-30"
          aria-label="Next slice"
        >
          <ChevronRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}

/**
 * Lean interactive fused PET/CT viewer showing all three planes (axial, coronal,
 * sagittal) side by side, each independently scrollable. Renders from the stored
 * SUV + CT artifacts via GET /api/fused/.../{view}/{slice}.
 */
export function FusedViewer({
  studyUid,
  usecase,
}: {
  studyUid: string;
  usecase: string;
}) {
  const [meta, setMeta] = useState<FusedMeta | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showLesions, setShowLesions] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    setMeta(null);
    api.fused
      .meta(studyUid, usecase)
      .then((m) => {
        if (!cancelled) setMeta(m);
      })
      .catch((e) => {
        if (!cancelled) setError(e?.message || "Failed to load fused viewer");
      });
    return () => {
      cancelled = true;
    };
  }, [studyUid, usecase]);

  if (error) {
    return (
      <div className="flex items-center justify-center h-[360px] bg-black text-gray-400 text-sm">
        Fused viewer unavailable — {error}
      </div>
    );
  }
  if (!meta) {
    return (
      <div className="flex items-center justify-center h-[360px] bg-black text-gray-400 text-sm gap-2">
        <Loader2 className="w-4 h-4 animate-spin" /> Loading fused viewer…
      </div>
    );
  }

  return (
    <div className="bg-black p-3">
      {meta.has_lesions && (
        <div className="flex items-center justify-end mb-2 px-1">
          <label className="inline-flex items-center gap-2 text-[12px] text-gray-300 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={showLesions}
              onChange={(e) => setShowLesions(e.target.checked)}
              className="accent-cyan-400 cursor-pointer"
            />
            <span className="inline-flex items-center gap-1">
              Outline detected lesions
              <span className="inline-block w-3 h-3 rounded-sm border-2 border-cyan-400" />
            </span>
          </label>
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {VIEWS.map((v) => (
          <FusedPane
            key={v}
            studyUid={studyUid}
            usecase={usecase}
            view={v}
            count={meta.views[v] ?? 1}
            defaultSlice={meta.defaults?.[v] ?? Math.floor((meta.views[v] ?? 1) / 2)}
            showLesions={showLesions}
          />
        ))}
      </div>
      <p className="text-[11px] text-gray-500 mt-2 px-1">
        CT anatomy with PET SUV hot-colormap overlay
        {meta.has_lesions ? "; detected lesions outlined in cyan" : ""}. Scroll (mouse
        wheel), drag the slider, or use ◀ ▶ to move through slices in each plane
        independently.
      </p>
    </div>
  );
}
