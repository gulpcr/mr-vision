"use client";

import clsx from "clsx";

const STATUS_STYLES: Record<string, string> = {
  pending: "bg-gray-100 text-gray-800",
  routing: "bg-blue-100 text-blue-800",
  preprocessing: "bg-blue-100 text-blue-800",
  inferring: "bg-yellow-100 text-yellow-800",
  postprocessing: "bg-indigo-100 text-indigo-800",
  completed: "bg-green-100 text-green-800",
  failed: "bg-red-100 text-red-800",
  cancelled: "bg-gray-100 text-gray-500",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium",
        STATUS_STYLES[status] || "bg-gray-100 text-gray-800"
      )}
    >
      {status}
    </span>
  );
}
