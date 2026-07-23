"use client";

import { createContext, useContext, useEffect, useState } from "react";
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
  can: (permission: Permission) => boolean;
  isLoading: boolean;
}

const AuthContext = createContext<AuthContextValue>({
  user: null,
  role: null,
  can: () => false,
  isLoading: true,
});

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<StoredUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

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

  const value: AuthContextValue = {
    user,
    role: user?.role ?? null,
    can: (permission) => roleHasPermission(user?.role, permission),
    isLoading,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  return useContext(AuthContext);
}
