import {
  LayoutDashboard,
  ClipboardList,
  ClipboardCheck,
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
  KeyRound,
  Palette,
  Globe,
  Layers,
  PenLine,
  type LucideIcon,
  ShieldAlert,
} from "lucide-react";
import { STUDY_READ, type Permission } from "@/lib/permissions";
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
  { href: "/dashboard", label: "Dashboard", labelKey: "dashboard", icon: LayoutDashboard, requiredPermission: "dashboard.view" },
  { href: "/worklist", label: "Worklist", labelKey: "worklist", icon: ClipboardList, requiredPermission: STUDY_READ },
  { href: "/remote", label: "Remote Reading", labelKey: "remoteReading", icon: Radio, requiredPermission: "study.view" },
  { href: "/onboarding", label: "Patient Intake", icon: UserPlus, requiredPermission: "patient.onboard" },
  { href: "/emergency-access", label: "Emergency Access", icon: ShieldAlert, requiredPermission: "break_glass.invoke" },
  { href: "/upload", label: "Upload DICOM", labelKey: "uploadDicom", icon: Upload, requiredPermission: "study.upload" },
  // The AI model registry is platform-wide — superadmin only, hidden from tenants.
  { href: "/admin/usecases", label: "AI Models", icon: Brain, requiredPermission: "tenant.manage" },
  { href: "/admin/routing", label: "Routing Rules", icon: GitBranch, requiredPermission: "config.manage" },
  { href: "/reports", label: "Reports", labelKey: "reports", icon: FileText, requiredPermission: "result.export" },
  { href: "/compare", label: "Compare", icon: ArrowLeftRight, requiredPermission: STUDY_READ },
  // Priority queue of unsigned reports (radiologists sign, referring doctors comment).
  { href: "/review", label: "Review Queue", icon: PenLine, requiredPermission: "result.approve||report.comment" },
  { href: "/review/ai", label: "AI Confidence Review", icon: Eye, requiredPermission: "result.approve" },
  { href: "/admin/metrics", label: "QA Dashboard", icon: BarChart3, requiredPermission: "audit.view" },
  { href: "/admin/capacity", label: "Capacity", icon: BarChart2, requiredPermission: "audit.view" },
  { href: "/admin/audit", label: "Audit Log", icon: Shield, requiredPermission: "audit.view" },
  { href: "/admin/audit-review", label: "Audit Review", icon: ClipboardCheck, requiredPermission: "audit.view" },
  { href: "/admin/emergency-access", label: "Emergency Access Review", icon: ShieldAlert, requiredPermission: "break_glass.review" },
  { href: "/admin/patient-rights", label: "Patient Rights", icon: FileText, requiredPermission: "patient.rights" },
  { href: "/admin/experiments", label: "A/B Testing", icon: FlaskConical, requiredPermission: "config.manage" },
  { href: "/admin/alerts", label: "Alerts", icon: Bell, badgeKey: "alerts", requiredPermission: "alert.view" },
  { href: "/admin/retention", label: "Retention", icon: Database, requiredPermission: "data.purge" },
  { href: "/settings", label: "Settings", icon: Settings },
  { href: "/admin/users", label: "Users", icon: Users, requiredPermission: "user.manage" },
  { href: "/admin/roles", label: "Roles", icon: KeyRound, requiredPermission: "role.manage" },
  { href: "/settings/workspace", label: "Workspace", icon: Palette, requiredPermission: "settings.manage" },
  { href: "/admin/tools", label: "Admin Tools", icon: Wrench, requiredPermission: "data.purge" },
  { href: "/admin/platform", label: "Platform", icon: Globe, requiredPermission: "tenant.manage" },
  { href: "/admin/tenants", label: "Tenants", icon: Building2, requiredPermission: "tenant.manage" },
  { href: "/admin/plans", label: "Plans", icon: Layers, requiredPermission: "tenant.manage" },
];
