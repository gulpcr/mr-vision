import {
  AlertOctagon,
  AlertTriangle,
  AlertCircle,
  Info,
  CheckCircle2,
  type LucideIcon,
} from "lucide-react";

// Single source of truth for the platform's 5-tier clinical severity scale.
// Every tier pairs a color with an icon + text label — never rely on color alone
// (see CLAUDE.md / redesign brief: color must never carry meaning by itself).
// "good" is a first-class tier, not just the lightest shade of a caution color.
export type SeverityTier = "critical" | "high" | "moderate" | "informational" | "good";

export interface SeverityConfigEntry {
  label: string;
  icon: LucideIcon;
  /** Background + text classes for a pill/badge. Includes dark-mode pairs. */
  badgeClass: string;
  /** Text-only color class, for inline icons/labels without a pill background. */
  textClass: string;
}

export const SEVERITY_CONFIG: Record<SeverityTier, SeverityConfigEntry> = {
  critical: {
    label: "Critical",
    icon: AlertOctagon,
    badgeClass: "bg-severity-critical-50 text-severity-critical-700 dark:bg-red-950 dark:text-red-300",
    textClass: "text-severity-critical-500",
  },
  high: {
    label: "High",
    icon: AlertTriangle,
    badgeClass: "bg-severity-high-50 text-severity-high-700 dark:bg-orange-950 dark:text-orange-300",
    textClass: "text-severity-high-500",
  },
  moderate: {
    label: "Moderate",
    icon: AlertCircle,
    badgeClass: "bg-severity-moderate-50 text-severity-moderate-700 dark:bg-amber-950 dark:text-amber-300",
    textClass: "text-severity-moderate-500",
  },
  informational: {
    label: "Informational",
    icon: Info,
    badgeClass: "bg-severity-informational-50 text-severity-informational-700 dark:bg-blue-950 dark:text-blue-300",
    textClass: "text-severity-informational-500",
  },
  good: {
    label: "Confirmed Good",
    icon: CheckCircle2,
    badgeClass: "bg-severity-good-50 text-severity-good-700 dark:bg-green-950 dark:text-green-300",
    textClass: "text-severity-good-500",
  },
};
