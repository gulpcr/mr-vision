// Permission matching — mirrors backend/app/domain/permissions.py.
//
// The caller's effective permissions come live from GET /api/auth/me/permissions (the
// role's current definition in their workspace), so custom roles and edited system
// roles take effect without a code change or re-login. This module only implements the
// matching grammar:
//   * a grant of "*" satisfies everything; "resource.*" satisfies "resource.<anything>";
//   * a broader grant implies its scoped variants ("study.view" ⊇ "study.view.referred");
//   * a required key may list alternatives with "||".
//
// "tenant.manage" is not a tenant permission at all: it stands for the cross-tenant
// platform-admin flag and is special-cased in lib/auth.tsx's can().

export type Permission = string;

export const STUDY_READ: Permission = "study.view||study.view.referred";

function grantSatisfies(grant: string, required: string): boolean {
  if (grant === "*" || grant === required) return true;
  if (grant.endsWith(".*")) return required.startsWith(grant.slice(0, -1));
  return required.startsWith(grant + ".");
}

export function hasPermission(granted: readonly string[] | null | undefined, required: Permission): boolean {
  if (!granted || granted.length === 0) return false;
  return required
    .split("||")
    .map((r) => r.trim())
    .filter(Boolean)
    .some((alt) => granted.some((g) => grantSatisfies(g, alt)));
}
