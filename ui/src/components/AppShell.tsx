"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Sidebar } from "./Sidebar";
import { NotificationToast } from "./NotificationToast";
import { AuthProvider } from "@/lib/auth";

const PUBLIC_PATHS = ["/login", "/portal/"];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const isPublic = PUBLIC_PATHS.some((p) => pathname.startsWith(p));
  const [authed, setAuthed] = useState(false);

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
      <div className="flex h-screen overflow-hidden">
        <Sidebar />
        <main className="flex-1 bg-transparent overflow-y-auto">
          {/* Keyed by route so each navigation re-triggers the entrance animation,
              giving the app a consistent sense of "flow" between pages. */}
          <div key={pathname} className="max-w-[1760px] mx-auto px-6 py-6 animate-fade-up">
            {children}
          </div>
        </main>
      </div>
      <NotificationToast />
    </AuthProvider>
  );
}
