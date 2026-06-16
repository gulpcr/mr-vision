"use client";

import { useUsecases } from "@/lib/hooks";
import { Brain } from "lucide-react";

export default function UseCasesAdminPage() {
  const { data: usecases, isLoading } = useUsecases();

  if (isLoading) return <p className="text-gray-500 p-4">Loading...</p>;

  return (
    <div>
      <div className="flex items-center gap-3 mb-6">
        <Brain className="w-7 h-7 text-primary-600" />
        <h1 className="text-2xl font-bold text-gray-900">AI Models</h1>
      </div>
      <div className="grid gap-4">
        {usecases?.map((uc) => (
          <div key={uc.name} className="bg-white rounded-lg shadow-sm border border-gray-200 p-5">
            <div className="flex items-center justify-between mb-3">
              <div>
                <h2 className="text-lg font-semibold text-gray-900">
                  {uc.name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
                </h2>
                <span className="text-xs text-gray-400">v{uc.version}</span>
              </div>
              <span
                className={`px-3 py-1 rounded-full text-xs font-medium ${
                  uc.enabled
                    ? "bg-green-50 text-green-700"
                    : "bg-gray-100 text-gray-500"
                }`}
              >
                {uc.enabled ? "Enabled" : "Disabled"}
              </span>
            </div>
            <p className="text-sm text-gray-600 mb-4">{uc.description}</p>
            <div className="grid grid-cols-3 gap-4 text-sm">
              <div>
                <span className="text-xs text-gray-500 uppercase tracking-wider">Body Parts</span>
                <div className="flex flex-wrap gap-1 mt-1.5">
                  {uc.supported_body_parts.map((bp) => (
                    <span key={bp} className="bg-blue-50 text-blue-700 px-2 py-0.5 rounded-full text-xs font-medium">
                      {bp}
                    </span>
                  ))}
                </div>
              </div>
              <div>
                <span className="text-xs text-gray-500 uppercase tracking-wider">Required Sequences</span>
                <div className="flex flex-wrap gap-1 mt-1.5">
                  {uc.required_sequences.map((seq) => (
                    <span key={seq} className="bg-purple-50 text-purple-700 px-2 py-0.5 rounded-full text-xs font-medium">
                      {seq}
                    </span>
                  ))}
                </div>
              </div>
              <div>
                <span className="text-xs text-gray-500 uppercase tracking-wider">Model Type</span>
                <p className="mt-1.5 font-mono text-xs text-gray-700">{uc.model_type}</p>
              </div>
            </div>
          </div>
        ))}
        {(!usecases || usecases.length === 0) && (
          <div className="text-center py-12 text-gray-400">
            <Brain className="w-10 h-10 mx-auto mb-2 text-gray-300" />
            No AI models registered
          </div>
        )}
      </div>
    </div>
  );
}
