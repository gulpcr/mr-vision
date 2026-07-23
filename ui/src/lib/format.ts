export function getNestedValue(obj: any, path: string): any {
  return path.split(".").reduce((acc, key) => acc?.[key], obj);
}

export function formatValue(
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

// The backend returns UTC datetimes without a timezone suffix (e.g.
// "2026-06-08T17:06:39"). JavaScript's Date() parses a timezone-less string as
// LOCAL time, so every such timestamp would otherwise render offset by the
// viewer's own UTC offset. Appending 'Z' forces correct UTC interpretation.
// Every date-parsing helper in this file (and any other caller that needs to
// parse a raw backend timestamp) should go through this — do not call
// `new Date(rawBackendString)` directly elsewhere.
export function parseUtcDate(dateStr: string): Date {
  const hasOffset = /[Zz]$|[+-]\d{2}:?\d{2}$/.test(dateStr);
  return new Date(hasOffset ? dateStr : `${dateStr}Z`);
}

// Locale-aware formatting driven by a single site-locale setting (see
// lib/i18n.tsx) rather than a hardcoded "en-US" — falls back to "en-US" when
// no locale has been resolved yet (e.g. during SSR before the client mounts).
const DEFAULT_LOCALE = "en-US";
let activeLocale: string = DEFAULT_LOCALE;
export function setFormatLocale(locale: string): void {
  activeLocale = locale;
}

export function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "-";
  try {
    return parseUtcDate(dateStr).toLocaleDateString(activeLocale, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return dateStr;
  }
}

export function formatDateTime(dateStr: string | null | undefined): string {
  if (!dateStr) return "-";
  try {
    return parseUtcDate(dateStr).toLocaleString(activeLocale, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return dateStr;
  }
}

export function formatPatientName(name: string | null | undefined): string {
  if (!name) return "Unknown";
  return name.replace(/\^/g, " ").replace(/\s+/g, " ").trim();
}
