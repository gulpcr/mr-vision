"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Sidebar } from "./Sidebar";
import { NotificationToast } from "./NotificationToast";
import { SessionKeeper } from "./SessionKeeper";
import { AuthProvider, useAuth } from "@/lib/auth";
import { NAV_ITEMS } from "@/lib/nav";
import { EmptyState } from "@/components/ui/EmptyState";
import { impersonatedUsername, isImpersonating, stopImpersonation } from "@/lib/impersonation";
import { hasSession } from "@/lib/session";
import { LogOut, ShieldAlert } from "lucide-react";

const PUBLIC_PATHS = ["/login", "/portal/", "/accept-invite"];
// Signed-in pages that resolve a sign-in restriction: rendered without the sidebar
// (whose data calls are all blocked until the restriction is resolved).
const SIGN_IN_FIX_PATHS = ["/change-password", "/setup-mfa"];

/** Sends an account with a pending forced password change / MFA enrolment to the page
 * that resolves it (the backend blocks everything else with 403 meanwhile). */
function SignInRestrictionGuard({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { mustChangePassword, mfaEnrollmentRequired, isLoading } = useAuth();
  const target = mustChangePassword ? "/change-password" : mfaEnrollmentRequired ? "/setup-mfa" : null;
  useEffect(() => {
    if (!isLoading && target && pathname !== target) router.replace(target);
  }, [isLoading, target, pathname, router]);
  if (target && pathname !== target) return null;
  return <>{children}</>;
}

/** Route-level permission gate: the page's nav entry (longest matching prefix) names
 * the permission it needs, checked against the caller's live server permissions. The
 * backend enforces the same permissions on every API route; this only spares users a
 * page full of 403s. */
function RouteGuard({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { can, isLoading, refresh } = useAuth();
  const entry = NAV_ITEMS.filter(
    (item) => item.requiredPermission && (pathname === item.href || pathname.startsWith(item.href + "/")),
  ).sort((a, b) => b.href.length - a.href.length)[0];

  if (!entry) return <>{children}</>;
  if (isLoading) return null;
  if (!can(entry.requiredPermission!)) {
    return (
      <div className="pt-16">
        <EmptyState
          icon={ShieldAlert}
          title="You don't have access to this page"
          description={`Your role in this workspace doesn't include "${entry.requiredPermission}". Ask a workspace administrator if you need it.`}
        />
        <div className="flex justify-center mt-4">
          <button
            onClick={refresh}
            className="px-4 py-2 text-sm font-medium rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700"
          >
            Refresh permissions
          </button>
        </div>
      </div>
    );
  }
  return <>{children}</>;
}

function ImpersonationBanner() {
  const router = useRouter();
  const [username, setUsername] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);

  useEffect(() => {
    setUsername(impersonatedUsername());
  }, []);

  const handleStop = async () => {
    setStopping(true);
    try {
      await stopImpersonation();
    } finally {
      router.push("/dashboard");
      router.refresh();
    }
  };

  return (
    <div className="flex items-center justify-between gap-3 px-4 py-2 bg-amber-500/15 border-b border-amber-500/30 text-amber-800 dark:text-amber-300 text-sm">
      <span>
        Impersonating <strong>{username ?? "user"}</strong> — this session expires automatically in 30 minutes.
      </span>
      <button
        onClick={handleStop}
        disabled={stopping}
        className="inline-flex items-center gap-1.5 px-3 py-1 rounded-md bg-amber-500/20 hover:bg-amber-500/30 transition text-xs font-medium"
      >
        <LogOut className="w-3.5 h-3.5" />
        {stopping ? "Stopping..." : "Stop & Return"}
      </button>
    </div>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const isPublic = PUBLIC_PATHS.some((p) => pathname.startsWith(p));
  const [authed, setAuthed] = useState(false);
  const [impersonating, setImpersonating] = useState(false);

  // Route guard: protected pages require a (not yet expired) session. The token is an
  // httpOnly cookie the page cannot see; the server stays the authority (401 → login).
  useEffect(() => {
    if (isPublic) {
      setAuthed(true);
      return;
    }
    if (!hasSession()) {
      router.replace("/login");
      setAuthed(false);
    } else {
      setAuthed(true);
    }
  }, [pathname, isPublic, router]);

  useEffect(() => {
    setImpersonating(isImpersonating());
  }, [pathname]);

  if (!isPublic && !authed) {
    return null;
  }

  // Public pages (login, accept-invite…) have no session, so no realtime socket: the
  // toast there opened /ws without a viewer cookie and retried it forever.
  if (isPublic) {
    return <>{children}</>;
  }

  if (SIGN_IN_FIX_PATHS.some((p) => pathname.startsWith(p))) {
    return (
      <AuthProvider>
        <SessionKeeper />
        <SignInRestrictionGuard>{children}</SignInRestrictionGuard>
      </AuthProvider>
    );
  }

  return (
    <AuthProvider>
      <SignInRestrictionGuard>
        <div className="flex flex-col h-screen overflow-hidden">
          {impersonating && <ImpersonationBanner />}
          <div className="flex flex-1 min-h-0">
            <Sidebar />
            <main className="flex-1 bg-transparent overflow-y-auto">
              {/* Keyed by route so each navigation re-triggers the entrance animation,
                  giving the app a consistent sense of "flow" between pages. */}
              <div key={pathname} className="max-w-[1760px] mx-auto px-6 py-6 animate-fade-up">
                <RouteGuard>{children}</RouteGuard>
              </div>
            </main>
          </div>
        </div>
        <NotificationToast />
        <SessionKeeper />
      </SignInRestrictionGuard>
    </AuthProvider>
  );
}
