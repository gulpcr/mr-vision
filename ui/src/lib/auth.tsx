"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { type Permission, roleHasPermission } from "@/lib/permissions";

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
  can: (permission: Permission) => boolean;
  isLoading: boolean;
}

const AuthContext = createContext<AuthContextValue>({
  user: null,
  role: null,
  isPlatformAdmin: false,
  can: () => false,
  isLoading: true,
});

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<StoredUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  // is_platform_admin isn't in the cached login response (see StoredUser above) —
  // it can change server-side without the user re-logging in, so it's fetched live
  // from /auth/me rather than cached, same rationale as useCurrentUser().
  const [isPlatformAdmin, setIsPlatformAdmin] = useState(false);

  useEffect(() => {
    try {
      const raw = localStorage.getItem("user");
      setUser(raw ? (JSON.parse(raw) as StoredUser) : null);
    } catch {
      setUser(null);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!user) return;
    api.auth
      .me()
      .then((me) => setIsPlatformAdmin(!!me.is_platform_admin))
      .catch(() => setIsPlatformAdmin(false));
  }, [user]);

  const value: AuthContextValue = {
    user,
    role: user?.role ?? null,
    isPlatformAdmin,
    // "tenant.manage" is a cross-tenant capability gated on is_platform_admin, not a
    // per-tenant role permission — every other permission still goes through the
    // role->permission table.
    can: (permission) =>
      permission === "tenant.manage" ? isPlatformAdmin : roleHasPermission(user?.role, permission),
    isLoading,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}
