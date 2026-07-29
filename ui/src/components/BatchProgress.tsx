"use client";

import { useBatch } from "@/lib/hooks";
import { CheckCircle, XCircle, Loader2, Package } from "lucide-react";

interface BatchProgressProps {
  batchId: string;
}

export function BatchProgress({ batchId }: BatchProgressProps) {
  const { data: batch } = useBatch(batchId);

  if (!batch) return <div className="text-sm text-gray-400 dark:text-gray-500">Loading batch...</div>;

  const progress = batch.total_items > 0
    ? ((batch.completed_items + batch.failed_items) / batch.total_items) * 100
    : 0;

  return (
    <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Package className="w-5 h-5 text-blue-600 dark:text-blue-400" />
          <span className="font-medium text-gray-900 dark:text-gray-100">{batch.name}</span>
        </div>
        <span className={`px-2 py-0.5 text-xs rounded-full font-medium ${
          batch.status === "completed" ? "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300" :
          batch.status === "failed" ? "bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300" :
          batch.status === "partial" ? "bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300" :
          "bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300"
        }`}>{batch.status}</span>
      </div>

      <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2 mb-2">
        <div
          className={`h-2 rounded-full transition-all ${
            batch.failed_items > 0 ? "bg-amber-500" : "bg-green-500"
          }`}
          style={{ width: `${progress}%` }}
        />
      </div>

      <div className="flex justify-between text-xs text-gray-500 dark:text-gray-400">
        <div className="flex items-center gap-3">
          <span className="flex items-center gap-1">
            <CheckCircle className="w-3 h-3 text-green-500 dark:text-green-400" /> {batch.completed_items}
          </span>
          <span className="flex items-center gap-1">
            <XCircle className="w-3 h-3 text-red-500 dark:text-red-400" /> {batch.failed_items}
          </span>
          {batch.status === "in_progress" && (
            <span className="flex items-center gap-1">
              <Loader2 className="w-3 h-3 animate-spin motion-reduce:animate-none text-blue-500 dark:text-blue-400" /> Processing
            </span>
          )}
        </div>
        <span>{batch.completed_items + batch.failed_items} / {batch.total_items}</span>
      </div>

      {batch.items && batch.items.length > 0 && (
        <div className="mt-3 max-h-40 overflow-auto">
          <table className="w-full text-xs">
            <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
              {batch.items.map((item: any) => (
                <tr key={item.id}>
                  <td className="py-1 text-gray-600 dark:text-gray-400 font-mono">{item.study_instance_uid.slice(0, 25)}...</td>
                  <td className="py-1 text-right">
                    <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                      item.status === "completed" ? "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300" :
                      item.status === "failed" ? "bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300" :
                      "bg-gray-50 dark:bg-gray-800 text-gray-500 dark:text-gray-400"
                    }`}>{item.status}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
