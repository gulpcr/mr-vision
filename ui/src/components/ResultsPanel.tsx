"use client";

import { useEffect, useState } from "react";
import { api, Result } from "@/lib/api";
import { QAPanel } from "./QAPanel";

interface ResultsPanelProps {
  studyUid: string;
  usecaseName: string;
}

export function ResultsPanel({ studyUid, usecaseName }: ResultsPanelProps) {
  const [result, setResult] = useState<Result | null>(null);
  const [uiSchema, setUiSchema] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      try {
        const [res, schema] = await Promise.all([
          api.results.get(studyUid, usecaseName),
          api.usecases.getUiSchema(usecaseName),
        ]);
        setResult(res);
        setUiSchema(schema);
      } catch (e: any) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [studyUid, usecaseName]);

  if (loading) return <div className="p-4 text-gray-500">Loading results...</div>;
  if (error) return <div className="p-4 text-red-600">Error: {error}</div>;
  if (!result) return <div className="p-4 text-gray-500">No results available</div>;

  return (
    <div className="space-y-6">
      <h2 className="text-lg font-semibold">
        {uiSchema?.title || usecaseName}
      </h2>
      {uiSchema?.description && (
        <p className="text-sm text-gray-500">{uiSchema.description}</p>
      )}

      {uiSchema?.sections?.map((section: any) => (
        <div key={section.id} className="bg-white rounded-lg shadow p-4">
          <h3 className="font-medium mb-3">{section.title}</h3>
          {renderSection(section, result)}
        </div>
      ))}
    </div>
  );
}

function renderSection(section: any, result: Result) {
  switch (section.type) {
    case "key_value":
      return renderKeyValue(section, result);
    case "table":
      return renderTable(section, result);
    case "qa_panel":
      return (
        <QAPanel
          flags={result.qa_flags}
          details={result.qa_details}
        />
      );
    case "overlay":
      return renderOverlayInfo(section, result);
    default:
      return <pre className="text-xs">{JSON.stringify(section, null, 2)}</pre>;
  }
}

function getNestedValue(obj: any, path: string): any {
  return path.split(".").reduce((acc, key) => acc?.[key], obj);
}

function renderKeyValue(section: any, result: Result) {
  const data = section.data_path ? getNestedValue(result, section.data_path) : result;
  return (
    <dl className="grid grid-cols-2 gap-2 text-sm">
      {section.fields?.map((field: any) => {
        const value = field.data_path
          ? getNestedValue(result, field.data_path)
          : data?.[field.key];
        return (
          <div key={field.key}>
            <dt className="text-gray-500">{field.label}</dt>
            <dd className="font-medium">
              {formatValue(value, field.format, field.precision, field.unit)}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

function renderTable(section: any, result: Result) {
  const data = getNestedValue(result, section.data_path);
  if (!data) return <p className="text-sm text-gray-400">No data</p>;

  // data_path may point to an array of row objects (e.g. measurements.lesions)
  // or a plain key→value object (legacy). Normalise to an array of row objects.
  let rows: any[];
  if (Array.isArray(data)) {
    rows = data;
  } else if (typeof data === "object") {
    rows = Object.entries(data).map(([k, v]) => ({ _key: k, _value: v }));
  } else {
    return <p className="text-sm text-gray-400">No data</p>;
  }

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b">
          {section.columns?.map((col: any) => (
            <th key={col.key} className="text-left py-2 px-2 font-medium text-gray-600">
              {col.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row: any, idx: number) => (
          <tr key={row.id ?? idx} className="border-b border-gray-100">
            {section.columns?.map((col: any) => (
              <td key={col.key} className="py-2 px-2">
                {col.key === "_key"
                  ? String(row._key ?? "").replace(/_/g, " ")
                  : formatValue(row[col.key], col.format, col.precision)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function renderOverlayInfo(section: any, result: Result) {
  const segArtifacts = result.artifacts.filter(
    (a) => a.artifact_type === section.artifact_filter
  );
  return (
    <div className="text-sm">
      {segArtifacts.length > 0 ? (
        <div>
          <p className="mb-2">Segmentation overlays available:</p>
          {segArtifacts.map((a) => (
            <div key={a.name} className="flex items-center gap-2 mb-1">
              <span className="font-mono text-xs bg-gray-100 px-2 py-1 rounded">
                {a.name}
              </span>
              <span className="text-gray-400 text-xs">
                ({(a.size_bytes / 1024).toFixed(0)} KB)
              </span>
            </div>
          ))}
          {section.colormap && (
            <div className="mt-3 flex flex-wrap gap-3">
              {Object.entries(section.colormap).map(([id, meta]: [string, any]) => (
                <div key={id} className="flex items-center gap-1.5">
                  <div
                    className="w-3 h-3 rounded-sm"
                    style={{ backgroundColor: meta.color }}
                  />
                  <span className="text-xs">{meta.label}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : (
        <p className="text-gray-400">No overlays generated</p>
      )}
    </div>
  );
}

function formatValue(
  value: any,
  format?: string,
  precision?: number,
  unit?: string
): string {
  if (value === null || value === undefined) return "-";
  let formatted: string;
  switch (format) {
    case "number":
      formatted = typeof value === "number" ? value.toFixed(precision ?? 2) : String(value);
      break;
    case "percent":
      formatted = typeof value === "number" ? `${value.toFixed(precision ?? 1)}%` : String(value);
      break;
    case "boolean":
      formatted = value ? "Yes" : "No";
      break;
    case "array":
      formatted = Array.isArray(value) ? value.join(", ") : String(value);
      break;
    default:
      formatted = String(value);
  }
  return unit ? `${formatted} ${unit}` : formatted;
}
