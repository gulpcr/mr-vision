"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Sidebar } from "./Sidebar";
import { NotificationToast } from "./NotificationToast";
import { AuthProvider } from "@/lib/auth";
import { isImpersonating, stopImpersonation } from "@/lib/impersonation";
import { LogOut } from "lucide-react";

const PUBLIC_PATHS = ["/login", "/portal/"];

function ImpersonationBanner() {
  const router = useRouter();
  const [username, setUsername] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);

  useEffect(() => {
    try {
      const raw = localStorage.getItem("user");
      setUsername(raw ? JSON.parse(raw)?.username ?? null : null);
    } catch {
      setUsername(null);
    }
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

  // Route guard: protected pages require an auth token. Unauthenticated users
  // are redirected to /login. In open auth mode the backend ignores the token,
  // but the guard is harmless because login still issues one.
  useEffect(() => {
    if (isPublic) {
      setAuthed(true);
      return;
    }
    const token = typeof window !== "undefined" && localStorage.getItem("auth_token");
    if (!token) {
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

  if (isPublic) {
    return (
      <>
        {children}
        <NotificationToast />
      </>
    );
  }

  return (
    <AuthProvider>
      <div className="flex flex-col h-screen overflow-hidden">
        {impersonating && <ImpersonationBanner />}
        <div className="flex flex-1 min-h-0">
          <Sidebar />
          <main className="flex-1 bg-transparent overflow-y-auto">
            {/* Keyed by route so each navigation re-triggers the entrance animation,
                giving the app a consistent sense of "flow" between pages. */}
            <div key={pathname} className="max-w-[1760px] mx-auto px-6 py-6 animate-fade-up">
              {children}
            </div>
          </main>
        </div>
      </div>
      <NotificationToast />
    </AuthProvider>
  );
}
