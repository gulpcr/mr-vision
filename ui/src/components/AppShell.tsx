"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Sidebar } from "./Sidebar";
import { NotificationToast } from "./NotificationToast";

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
    <>
      <div className="flex min-h-screen">
        <Sidebar />
        <main className="flex-1 bg-gray-50 overflow-auto">
          <div className="max-w-7xl mx-auto px-6 py-6">{children}</div>
        </main>
      </div>
      <NotificationToast />
    </>
  );
}
