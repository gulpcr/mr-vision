"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api, type MyPermissions, type WorkspaceInfo } from "@/lib/api";
import { hasPermission, type Permission } from "@/lib/permissions";
import { readStoredUser } from "@/lib/session";

export interface StoredUser {
  id: string;
  username: string;
  role: string;
  tenant_id: string;
}

interface AuthContextValue {
  user: StoredUser | null;
  role: string | null;
  isPlatformAdmin: boolean;
  /** Live effective permissions of the caller's role in their workspace. */
  permissions: string[];
  /** Referring Doctor: sees only studies of patients they referred. */
  referralScoped: boolean;
  workspace: WorkspaceInfo | null;
  can: (permission: Permission) => boolean;
  /** Re-fetch permissions (e.g. after a role edit) without reloading the page. */
  refresh: () => void;
  isLoading: boolean;
  /** Sign-in restrictions the account must resolve before using the app. */
  mustChangePassword: boolean;
  mfaEnrollmentRequired: boolean;
}

const AuthContext = createContext<AuthContextValue>({
  user: null,
  role: null,
  isPlatformAdmin: false,
  permissions: [],
  referralScoped: false,
  workspace: null,
  can: () => false,
  refresh: () => {},
  isLoading: true,
  mustChangePassword: false,
  mfaEnrollmentRequired: false,
});

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<StoredUser | null>(null);
  const [me, setMe] = useState<MyPermissions | null>(null);
  const [workspace, setWorkspace] = useState<WorkspaceInfo | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    setUser(readStoredUser() as StoredUser | null);
  }, []);

  const load = useCallback(() => {
    // Permissions and platform flags are read live from the server (never from the
    // cached login response): an admin can change a role, or revoke a session, at any time.
    api.auth
      .myPermissions()
      .then(setMe)
      .catch(() => setMe(null))
      .finally(() => setIsLoading(false));
    api.tenant
      .current()
      .then(setWorkspace)
      .catch(() => setWorkspace(null));
  }, []);

  useEffect(() => {
    if (!user) return;
    load();
    // A session restored from localStorage predates (or outlived) the viewer cookie set
    // at login — reissue it so OHIF and the WebSocket are authorised for this tenant.
    api.auth.refreshViewerSession().catch(() => {});
  }, [user, load]);

  // Pick up role / permission changes made while the tab stays open.
  useEffect(() => {
    if (!user) return;
    const onFocus = () => load();
    window.addEventListener("focus", onFocus);
    const timer = window.setInterval(load, 60_000);
    return () => {
      window.removeEventListener("focus", onFocus);
      window.clearInterval(timer);
    };
  }, [user, load]);

  const permissions = me?.permissions ?? [];
  const isPlatformAdmin = !!me?.is_platform_admin;

  const value: AuthContextValue = {
    user,
    role: me?.role ?? user?.role ?? null,
    isPlatformAdmin,
    permissions,
    referralScoped: !!me?.referral_scoped,
    workspace,
    // "tenant.manage" is the cross-tenant platform-admin capability, not a workspace
    // role permission.
    can: (permission) =>
      permission === "tenant.manage" ? isPlatformAdmin : hasPermission(permissions, permission),
    refresh: load,
    isLoading: isLoading && !!user,
    mustChangePassword: !!me?.must_change_password,
    mfaEnrollmentRequired: !!me?.mfa_enrollment_required,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}
