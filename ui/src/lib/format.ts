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

export function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "-";
  try {
    return new Date(dateStr).toLocaleDateString("en-US", {
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
    return new Date(dateStr).toLocaleString("en-US", {
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
