import { api } from "./api";
import {
  clearImpersonationTab,
  isImpersonatingTab,
  readStoredUser,
  setImpersonationTab,
} from "./session";

// Impersonation lives in THIS TAB only (sessionStorage, see lib/session.ts): the
// impersonation token is sent as a bearer header and disappears with the tab, while
// the operator's own httpOnly-cookie session continues underneath and in other tabs.

export function isImpersonating(): boolean {
  if (typeof window === "undefined") return false;
  return isImpersonatingTab();
}

/** The impersonated user's display name in this tab (null when not impersonating). */
export function impersonatedUsername(): string | null {
  return isImpersonating() ? readStoredUser()?.username ?? null : null;
}

/** Starts impersonating userId in this tab. Caller is responsible for navigating
 * afterward (e.g. router.push("/dashboard")). */
export async function startImpersonation(userId: string): Promise<void> {
  const result = await api.auth.impersonate(userId);
  setImpersonationTab(
    result.access_token,
    {
      id: result.user_id,
      username: result.username,
      role: result.role,
      tenant_id: result.tenant_id,
    },
    Math.floor(Date.now() / 1000) + result.expires_in_minutes * 60,
  );
  // Switch the viewer cookie to the impersonated user's tenant too.
  await api.auth.refreshViewerSession().catch(() => {});
}

/** Ends the impersonation (revokes the token server-side) and returns this tab to the
 * operator's own session. Caller is responsible for navigating afterward. */
export async function stopImpersonation(): Promise<void> {
  try {
    await api.auth.stopImpersonation();
  } finally {
    clearImpersonationTab();
    await api.auth.refreshViewerSession().catch(() => {});
  }
}
