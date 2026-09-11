// Client-side mirror of backend/app/domain/permissions.py's SYSTEM_ROLE_PERMISSIONS.
// There is no /auth/me permissions endpoint yet — only a role string is issued at
// login (see app/login/page.tsx) — so this hand-synced copy is what nav/route
// gating checks against. Keep it in sync by hand with the backend catalog until a
// real permissions endpoint exists and this can be deleted in favor of a fetch.

export const ALL_PERMISSIONS = [
  "study.view",
  "study.upload",
  "study.claim",
  "study.escalate",
  "study.delete",
  "job.run",
  "job.manage",
  "result.approve",
  "result.export",
  "alert.view",
  "alert.acknowledge",
  "patient.onboard",
  "user.manage",
  "config.manage",
  "audit.view",
  "data.purge",
  // Cross-tenant capability — NOT granted through any role in
  // SYSTEM_ROLE_PERMISSIONS below. Gated on is_platform_admin instead (see
  // lib/auth.tsx's can()), since platform-admin is orthogonal to a user's
  // per-tenant role.
  "tenant.manage",
] as const;

export type Permission = (typeof ALL_PERMISSIONS)[number];

// Excludes "tenant.manage" — role="admin" is a per-tenant role and must NOT imply
// platform-admin; can() special-cases that one permission against is_platform_admin
// instead of this table (see lib/auth.tsx).
const TENANT_ROLE_PERMISSIONS = ALL_PERMISSIONS.filter((p) => p !== "tenant.manage");

export const SYSTEM_ROLE_PERMISSIONS: Record<string, Permission[]> = {
  admin: [...TENANT_ROLE_PERMISSIONS],
  receptionist: ["patient.onboard", "study.view"],
  technician: ["job.manage", "job.run", "study.escalate", "study.upload", "study.view"],
  radiologist: [
    "alert.acknowledge",
    "alert.view",
    "result.approve",
    "result.export",
    "study.claim",
    "study.escalate",
    "study.view",
  ],
  viewer: ["study.view"],
};

export function roleHasPermission(role: string | null | undefined, permission: Permission): boolean {
  if (!role) return false;
  return SYSTEM_ROLE_PERMISSIONS[role]?.includes(permission) ?? false;
}
