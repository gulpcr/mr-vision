import {
  LayoutDashboard,
  ClipboardList,
  Upload,
  Brain,
  GitBranch,
  FileText,
  ArrowLeftRight,
  Settings,
  Users,
  Shield,
  Bell,
  Database,
  FlaskConical,
  Eye,
  BarChart3,
  BarChart2,
  Wrench,
  UserPlus,
  Radio,
  Building2,
  type LucideIcon,
} from "lucide-react";
import type { Permission } from "@/lib/permissions";
import type { Strings } from "@/lib/locales/en";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  badgeKey?: "alerts";
  /** Omitted = visible to any authenticated role. */
  requiredPermission?: Permission;
  /** Present only for items covered by the EN/Urdu catalog (primary clinical flow) — Sidebar prefers this over `label` when set. */
  labelKey?: keyof Strings["nav"];
}

// admin/metrics and admin/capacity map to "audit.view" as a reasonable default —
// there is no dedicated "metrics.view" permission key in the backend catalog.
export const NAV_ITEMS: NavItem[] = [
  { href: "/dashboard", label: "Dashboard", labelKey: "dashboard", icon: LayoutDashboard, requiredPermission: "study.view" },
  { href: "/worklist", label: "Worklist", labelKey: "worklist", icon: ClipboardList, requiredPermission: "study.view" },
  { href: "/remote", label: "Remote Reading", labelKey: "remoteReading", icon: Radio, requiredPermission: "study.view" },
  { href: "/onboarding", label: "Patient Intake", icon: UserPlus, requiredPermission: "patient.onboard" },
  { href: "/upload", label: "Upload DICOM", labelKey: "uploadDicom", icon: Upload, requiredPermission: "study.upload" },
  { href: "/admin/usecases", label: "AI Models", icon: Brain, requiredPermission: "config.manage" },
  { href: "/admin/routing", label: "Routing Rules", icon: GitBranch, requiredPermission: "config.manage" },
  { href: "/reports", label: "Reports", labelKey: "reports", icon: FileText, requiredPermission: "result.export" },
  { href: "/compare", label: "Compare", icon: ArrowLeftRight, requiredPermission: "study.view" },
  { href: "/review", label: "Review Queue", icon: Eye, requiredPermission: "result.approve" },
  { href: "/admin/metrics", label: "QA Dashboard", icon: BarChart3, requiredPermission: "audit.view" },
  { href: "/admin/capacity", label: "Capacity", icon: BarChart2, requiredPermission: "audit.view" },
  { href: "/admin/audit", label: "Audit Log", icon: Shield, requiredPermission: "audit.view" },
  { href: "/admin/experiments", label: "A/B Testing", icon: FlaskConical, requiredPermission: "config.manage" },
  { href: "/admin/alerts", label: "Alerts", icon: Bell, badgeKey: "alerts", requiredPermission: "alert.view" },
  { href: "/admin/retention", label: "Retention", icon: Database, requiredPermission: "data.purge" },
  { href: "/settings", label: "Settings", icon: Settings },
  { href: "/admin/users", label: "Users", icon: Users, requiredPermission: "user.manage" },
  { href: "/admin/tools", label: "Admin Tools", icon: Wrench, requiredPermission: "data.purge" },
  { href: "/admin/tenants", label: "Tenants", icon: Building2, requiredPermission: "tenant.manage" },
];
