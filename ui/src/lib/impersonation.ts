import { api } from "./api";

const ORIGIN_KEY = "impersonation_origin";

interface StoredSession {
  auth_token: string;
  user: string; // the raw localStorage "user" JSON string
}

export function isImpersonating(): boolean {
  if (typeof window === "undefined") return false;
  return localStorage.getItem(ORIGIN_KEY) !== null;
}

export function getImpersonationOrigin(): StoredSession | null {
  if (typeof window === "undefined") return null;
  const raw = localStorage.getItem(ORIGIN_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

/** Starts impersonating userId: stashes the CURRENT (operator's) session,
 * then overwrites auth_token/user with the impersonation token. Caller is
 * responsible for navigating afterward (e.g. router.push("/dashboard")). */
export async function startImpersonation(userId: string): Promise<void> {
  const result = await api.auth.impersonate(userId);

  const origin: StoredSession = {
    auth_token: localStorage.getItem("auth_token") || "",
    user: localStorage.getItem("user") || "null",
  };
  localStorage.setItem(ORIGIN_KEY, JSON.stringify(origin));

  localStorage.setItem("auth_token", result.access_token);
  localStorage.setItem("user", JSON.stringify({
    id: result.user_id,
    username: result.username,
    role: result.role,
    tenant_id: result.tenant_id,
  }));
}

/** Ends the current impersonation session (revokes the token server-side)
 * and restores the operator's original session. Caller is responsible for
 * navigating afterward. */
export async function stopImpersonation(): Promise<void> {
  try {
    await api.auth.stopImpersonation();
  } finally {
    const origin = getImpersonationOrigin();
    if (origin) {
      localStorage.setItem("auth_token", origin.auth_token);
      localStorage.setItem("user", origin.user);
    }
    localStorage.removeItem(ORIGIN_KEY);
  }
}
