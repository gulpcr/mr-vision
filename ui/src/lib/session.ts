import type { SessionResponse } from "@/lib/api";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "/api";

/** Server-enforced idle timeout (SESSION_IDLE_MINUTES); the UI warns shortly before. */
export const IDLE_LOGOFF_MS = 15 * 60_000;
export const IDLE_WARNING_MS = 13 * 60_000;
const ACTIVITY_KEY = "last_activity";

// The access token is an httpOnly cookie (mrv_access) the browser sends by itself —
// never readable by script, never in localStorage. What the UI keeps is only the
// display profile and the expiry time it schedules refreshes from.
const USER_KEY = "user";
const EXPIRES_KEY = "session_expires_at";
// Impersonation is per tab (sessionStorage): its token is sent as a bearer header and
// dies with the tab; the operator's own cookie session is untouched underneath.
const IMP_TOKEN_KEY = "impersonation_token";
const IMP_USER_KEY = "impersonation_user";
const IMP_EXPIRES_KEY = "impersonation_expires_at";

/** Sent on every API call; the backend requires it for cookie-authenticated writes. */
export const CSRF_HEADER = { "X-Requested-With": "mrcv" } as const;

export interface StoredProfile {
  id: string | null;
  username: string | null;
  role: string | null;
  tenant_id: string | null;
}

function ss(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

function ls(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

/** Persist a freshly issued session (login, MFA verify, password change, refresh). */
export function storeSession(result: SessionResponse): void {
  const store = ls();
  if (!store) return;
  store.setItem(USER_KEY, JSON.stringify({
    id: result.user_id,
    username: result.username,
    role: result.role,
    tenant_id: result.tenant_id,
  }));
  if (result.expires_at) store.setItem(EXPIRES_KEY, String(result.expires_at));
  // Tokens from older sessions (pre-cookie builds) must not linger in storage.
  store.removeItem("auth_token");
}

/** First page after sign-in: resolve any sign-in restriction before the app. */
export function pathAfterSignIn(result: SessionResponse): string {
  if (result.must_change_password) return "/change-password";
  if (result.mfa_enrollment_required) return "/setup-mfa";
  return "/dashboard";
}

/** The signed-in (or, in this tab, impersonated) user's display profile. */
export function readStoredUser(): StoredProfile | null {
  try {
    const raw = ss()?.getItem(IMP_USER_KEY) ?? ls()?.getItem(USER_KEY) ?? null;
    return raw ? (JSON.parse(raw) as StoredProfile) : null;
  } catch {
    return null;
  }
}

/** True while this browser holds a session that has not yet expired. The cookie itself
 * is invisible to script; the server remains the authority (401 → sign-in). */
export function hasSession(): boolean {
  if (isImpersonatingTab()) return true;
  return !!ls()?.getItem(USER_KEY) && tokenSecondsLeft() > 0;
}

/** Remove patient data the browser may still hold after a session ends (TEC-03):
 * unload the embedded viewer (decoded image slices live in its memory), then delete
 * Cache Storage, IndexedDB databases (OHIF / cornerstone caches) and service workers.
 * The server also sends Clear-Site-Data on logout, and image responses are no-store. */
export async function flushBrowserCaches(): Promise<void> {
  if (typeof window === "undefined") return;
  try {
    document.querySelectorAll("iframe").forEach((frame) => {
      frame.src = "about:blank";
      frame.remove();
    });
  } catch {
    /* ignore */
  }
  const tasks: Promise<unknown>[] = [];
  try {
    if ("caches" in window) {
      tasks.push(caches.keys().then((keys) => Promise.all(keys.map((k) => caches.delete(k)))));
    }
  } catch {
    /* ignore */
  }
  try {
    const idb = indexedDB as IDBFactory & { databases?: () => Promise<{ name?: string }[]> };
    if (idb.databases) {
      tasks.push(idb.databases().then((dbs) =>
        dbs.forEach((db) => db.name && indexedDB.deleteDatabase(db.name))));
    }
  } catch {
    /* ignore */
  }
  try {
    if ("serviceWorker" in navigator) {
      tasks.push(navigator.serviceWorker.getRegistrations()
        .then((regs) => Promise.all(regs.map((r) => r.unregister()))));
    }
  } catch {
    /* ignore */
  }
  // Never let cache clean-up block a sign-out for long.
  await Promise.race([Promise.allSettled(tasks), new Promise((r) => setTimeout(r, 1500))]);
}

/** Drop everything this tab knows about the session (not the server side). */
export function clearLocalSession(): void {
  void flushBrowserCaches();
  const store = ls();
  store?.removeItem(USER_KEY);
  store?.removeItem(EXPIRES_KEY);
  store?.removeItem("auth_token");
  store?.removeItem("impersonation_origin");
  clearImpersonationTab();
}

// ── Impersonation (this tab only) ────────────────────────────────────────────

export function isImpersonatingTab(): boolean {
  return !!ss()?.getItem(IMP_TOKEN_KEY);
}

export function setImpersonationTab(token: string, profile: StoredProfile, expiresAt: number): void {
  const store = ss();
  if (!store) return;
  store.setItem(IMP_TOKEN_KEY, token);
  store.setItem(IMP_USER_KEY, JSON.stringify(profile));
  store.setItem(IMP_EXPIRES_KEY, String(expiresAt));
}

export function clearImpersonationTab(): void {
  const store = ss();
  store?.removeItem(IMP_TOKEN_KEY);
  store?.removeItem(IMP_USER_KEY);
  store?.removeItem(IMP_EXPIRES_KEY);
}

/** Seconds until the current access token expires (0 if unknown / expired). */
export function tokenSecondsLeft(): number {
  const raw = isImpersonatingTab() ? ss()?.getItem(IMP_EXPIRES_KEY) : ls()?.getItem(EXPIRES_KEY);
  const exp = Number(raw || 0);
  if (!Number.isFinite(exp) || exp <= 0) return 0;
  return Math.max(0, Math.floor(exp - Date.now() / 1000));
}

let inflight: Promise<boolean> | null = null;

/** Exchange the httpOnly refresh cookie for a new access cookie. Single-flight within a
 * tab and serialised across tabs (Web Locks), so tabs never race the same refresh token
 * (the server treats a replayed refresh token as theft and ends the session). Never
 * used while impersonating: the refresh cookie belongs to the operator. */
export function refreshAccessToken(): Promise<boolean> {
  if (typeof window === "undefined" || isImpersonatingTab()) return Promise.resolve(false);
  // Only a person at the keyboard keeps a session alive — never background polling
  // (the permissions poll, realtime reconnects) after the idle limit.
  if (Date.now() - lastActivity() >= IDLE_LOGOFF_MS) return Promise.resolve(false);
  if (inflight) return inflight;
  const before = ls()?.getItem(EXPIRES_KEY) ?? null;
  const doRefresh = async (): Promise<boolean> => {
    // Another tab refreshed while this one waited for the lock.
    if ((ls()?.getItem(EXPIRES_KEY) ?? null) !== before && tokenSecondsLeft() > 60) return true;
    const res = await fetch(`${API_BASE}/auth/refresh`, {
      method: "POST", credentials: "same-origin", headers: { ...CSRF_HEADER },
    });
    if (!res.ok) return false;
    storeSession((await res.json()) as SessionResponse);
    return true;
  };
  const locks = (navigator as Navigator & { locks?: LockManager }).locks;
  const run: Promise<boolean> = locks?.request
    ? (locks.request("mrcv-session-refresh", doRefresh) as unknown as Promise<boolean>)
    : doRefresh();
  inflight = run.catch(() => false).finally(() => {
    inflight = null;
  });
  return inflight;
}

const NO_REFRESH_PATHS = ["/auth/login", "/auth/refresh", "/auth/mfa/verify", "/auth/logout"];

/** fetch() for the API: the session cookie goes automatically (same origin), plus the
 * CSRF header and, while impersonating in this tab, the impersonation bearer token.
 * On 401 refreshes the session once and retries. */
export async function authFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const prepared = (): RequestInit => {
    const headers = new Headers(init.headers || {});
    headers.set("X-Requested-With", CSRF_HEADER["X-Requested-With"]);
    const impToken = ss()?.getItem(IMP_TOKEN_KEY);
    if (impToken) headers.set("Authorization", `Bearer ${impToken}`);
    return { credentials: "same-origin", ...init, headers };
  };
  const res = await fetch(url, prepared());
  if (res.status !== 401 || NO_REFRESH_PATHS.some((p) => url.includes(p))) return res;
  if (!(await refreshAccessToken())) return res;
  return fetch(url, prepared());
}

/** Record user activity (shared across tabs) for the idle logoff. */
export function markActivity(): void {
  try {
    localStorage.setItem(ACTIVITY_KEY, String(Date.now()));
  } catch {
    /* storage unavailable: per-tab tracking still works */
  }
}

export function lastActivity(): number {
  const v = Number(ls()?.getItem(ACTIVITY_KEY) || 0);
  return Number.isFinite(v) ? v : 0;
}

/** Sign out locally and on the server, then go to the login page. */
export async function signOut(reason?: "idle" | "expired"): Promise<void> {
  try {
    await fetch(`${API_BASE}/auth/logout`, {
      method: "POST",
      credentials: "same-origin",
      headers: { ...CSRF_HEADER },
    });
  } catch {
    /* best effort — the session also dies server-side when idle */
  }
  clearLocalSession();
  await flushBrowserCaches();
  window.location.href = reason ? `/login?reason=${reason}` : "/login";
}
