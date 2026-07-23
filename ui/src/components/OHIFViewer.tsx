"use client";

import { useEffect, useState } from "react";
import { ExternalLink, Maximize2 } from "lucide-react";

interface OHIFViewerProps {
  studyInstanceUID: string;
}

export function OHIFViewer({ studyInstanceUID }: OHIFViewerProps) {
  const [expanded, setExpanded] = useState(false);
  // TMTV mode opens PET/CT already fused (CT + PET + fused PET-on-CT + MIP).
  const ohifUrl = `/ohif/tmtv?StudyInstanceUIDs=${studyInstanceUID}`;

  useEffect(() => {
    if (!expanded) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setExpanded(false);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [expanded]);

  return (
    <div className={`relative ${expanded ? "fixed inset-0 z-50 bg-black" : ""}`}>
      <div className="flex items-center justify-between bg-gray-800 px-3 py-2 rounded-t-lg">
        <span className="text-white text-sm font-medium">OHIF DICOM Viewer</span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setExpanded(!expanded)}
            aria-label={expanded ? "Exit fullscreen" : "Fullscreen"}
            title={expanded ? "Exit fullscreen" : "Fullscreen"}
            className="text-gray-300 hover:text-white"
          >
            <Maximize2 className="w-4 h-4" />
          </button>
          <a
            href={ohifUrl}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Open OHIF viewer in a new tab"
            title="Open in new tab"
            className="text-gray-300 hover:text-white"
          >
            <ExternalLink className="w-4 h-4" />
          </a>
        </div>
      </div>
      <iframe
        src={ohifUrl}
        className={`w-full border-0 ${expanded ? "h-[calc(100vh-40px)]" : "h-[600px]"} rounded-b-lg`}
        title="OHIF Viewer"
        allow="fullscreen"
      />
    </div>
  );
}
