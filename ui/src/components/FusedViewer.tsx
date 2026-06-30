"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, getFusedSliceUrl, FusedMeta, FusedMode } from "@/lib/api";
import { Loader2, ChevronLeft, ChevronRight } from "lucide-react";

type View = "axial" | "coronal" | "sagittal";
const VIEWS: View[] = ["axial", "coronal", "sagittal"];

/**
 * One PET/CT plane: a scrollable viewport with a slice slider, mouse-wheel
 * scrolling, and prev/next buttons. `mode` selects what is rendered — "ct"
 * (CT grayscale), "pet" (PET hot-colormap), or "fused". Adjacent slices are
 * prefetched (and the backend serves them with a 1h cache), so scrolling is
 * smooth.
 */
function FusedPane({
  studyUid,
  usecase,
  view,
  count,
  defaultSlice,
  showLesions,
  mode,
}: {
  studyUid: string;
  usecase: string;
  view: View;
  count: number;
  defaultSlice: number;
  showLesions: boolean;
  mode: FusedMode;
}) {
  const [slice, setSlice] = useState(defaultSlice);
  const [imgLoading, setImgLoading] = useState(true);
  const [src, setSrc] = useState<string | null>(null);
  const frameRef = useRef<HTMLDivElement>(null);
  // Cache of fetched-slice object URLs (url → objectURL). Browser <img> can't
  // send the JWT, so each slice is fetched WITH the Authorization header and
  // rendered from an object URL (mirrors ReportView's AuthImage).
  const cacheRef = useRef<Map<string, string>>(new Map());

  const clamp = useCallback((n: number) => Math.max(0, Math.min(count - 1, n)), [count]);

  const loadBlob = useCallback(async (url: string): Promise<string | null> => {
    const cache = cacheRef.current;
    const cached = cache.get(url);
    if (cached) return cached;
    try {
      const token = typeof window !== "undefined" ? localStorage.getItem("auth_token") : null;
      const r = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
      if (!r.ok) return null;
      const obj = URL.createObjectURL(await r.blob());
      cache.set(url, obj);
      // Bound memory: evict the oldest entries (current + neighbours are newest).
      while (cache.size > 30) {
        const oldest = cache.keys().next().value as string;
        const old = cache.get(oldest);
        if (old) URL.revokeObjectURL(old);
        cache.delete(oldest);
      }
      return obj;
    } catch {
      return null;
    }
  }, []);

  // Revoke all cached object URLs when the pane unmounts.
  useEffect(() => {
    const cache = cacheRef.current;
    return () => {
      Array.from(cache.values()).forEach((u) => URL.revokeObjectURL(u));
      cache.clear();
    };
  }, []);

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

  // Load the current slice (authenticated) and show it from an object URL.
  useEffect(() => {
    let active = true;
    setImgLoading(true);
    const url = getFusedSliceUrl(studyUid, usecase, view, slice, showLesions, mode);
    loadBlob(url).then((obj) => {
      if (active) {
        setSrc(obj);
        setImgLoading(false);
      }
    });
    return () => { active = false; };
  }, [slice, showLesions, studyUid, usecase, view, mode, loadBlob]);

  // Prefetch nearby slices (authenticated) so scrolling doesn't wait on the network.
  useEffect(() => {
    for (const d of [1, -1, 2, -2, 3, -3]) {
      const i = slice + d;
      if (i >= 0 && i < count) {
        loadBlob(getFusedSliceUrl(studyUid, usecase, view, i, showLesions, mode));
      }
    }
  }, [slice, count, studyUid, usecase, view, showLesions, mode, loadBlob]);

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
        {src && (
          <img
            src={src}
            alt={`${view} ${mode === "fused" ? "fused PET/CT" : mode.toUpperCase()} slice ${slice + 1}`}
            className="max-h-full max-w-full object-contain"
            onLoad={() => setImgLoading(false)}
            onError={() => setImgLoading(false)}
            draggable={false}
          />
        )}
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

/** One optionally-labeled row of three independently-scrollable planes for a single mode. */
function ModeRow({
  label,
  mode,
  studyUid,
  usecase,
  meta,
  showLesions,
}: {
  label: string | null;
  mode: FusedMode;
  studyUid: string;
  usecase: string;
  meta: FusedMeta;
  showLesions: boolean;
}) {
  return (
    <div>
      {label && (
        <div className="text-[11px] font-bold uppercase tracking-wider text-gray-400 mb-1.5 px-1">
          {label}
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {VIEWS.map((v) => (
          <FusedPane
            key={`${mode}-${v}`}
            studyUid={studyUid}
            usecase={usecase}
            view={v}
            mode={mode}
            count={meta.views[v] ?? 1}
            defaultSlice={meta.defaults?.[v] ?? Math.floor((meta.views[v] ?? 1) / 2)}
            showLesions={showLesions}
          />
        ))}
      </div>
    </div>
  );
}

const MODE_LABEL: Record<FusedMode, string> = {
  ct: "CT",
  pet: "PET",
  fused: "Fused PET/CT",
};

/**
 * Lean interactive PET/CT viewer. Each requested `mode` becomes its own row of
 * three independently-scrollable planes (axial, coronal, sagittal). Defaults to
 * showing CT (grayscale) then PET (hot-colormap); pass `modes={["fused"]}` for a
 * single scrollable fused PET-on-CT row. Renders from the stored SUV + CT
 * artifacts via GET /api/fused/.../{view}/{slice}?mode=ct|pet|fused.
 */
export function FusedViewer({
  studyUid,
  usecase,
  modes = ["ct", "pet"],
}: {
  studyUid: string;
  usecase: string;
  modes?: FusedMode[];
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
      {(() => {
        // Drop the CT row when the study has no CT volume (it would be all-black).
        const visibleModes = modes.filter((m) => (m === "ct" ? meta.has_ct : true));
        // Only label rows when more than one is shown; a single row is self-evident.
        const showLabels = visibleModes.length > 1;
        return (
          <div className="space-y-4">
            {visibleModes.map((m) => (
              <ModeRow
                key={m}
                label={showLabels ? MODE_LABEL[m] : null}
                mode={m}
                studyUid={studyUid}
                usecase={usecase}
                meta={meta}
                showLesions={showLesions}
              />
            ))}
          </div>
        );
      })()}
      <p className="text-[11px] text-gray-500 mt-2 px-1">
        {modes.length === 1 && modes[0] === "fused"
          ? `CT anatomy with PET SUV hot-colormap overlay${meta.has_lesions ? "; detected lesions outlined in cyan" : ""}`
          : `${meta.has_ct ? "CT anatomy (grayscale) above, PET SUV (hot colormap) below" : "PET SUV (hot colormap)"}${meta.has_lesions ? "; detected lesions outlined in cyan" : ""}`}
        . Scroll (mouse wheel), drag the slider, or use ◀ ▶ to move through slices
        in each plane independently.
      </p>
    </div>
  );
}
