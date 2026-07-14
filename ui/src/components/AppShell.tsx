"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { Sidebar } from "./Sidebar";
import { NotificationToast } from "./NotificationToast";
import { isImpersonating, stopImpersonation } from "@/lib/impersonation";
import { LogOut } from "lucide-react";

const PUBLIC_PATHS = ["/login", "/portal/"];

function ImpersonationBanner() {
  const router = useRouter();
  const [stopping, setStopping] = useState(false);
  const [username, setUsername] = useState("");

  useEffect(() => {
    try {
      const user = JSON.parse(localStorage.getItem("user") || "null");
      setUsername(user?.username || "");
    } catch {
      setUsername("");
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
    <div className="bg-amber-500 text-white px-4 py-2 text-sm flex items-center justify-between shrink-0">
      <span>
        Impersonating <strong>{username}</strong> — this session expires automatically in 30 minutes.
      </span>
      <button
        onClick={handleStop}
        disabled={stopping}
        className="flex items-center gap-1.5 px-3 py-1 bg-amber-600 hover:bg-amber-700 rounded-lg font-medium disabled:opacity-50"
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

  useEffect(() => {
    setImpersonating(isImpersonating());
  }, [pathname]);

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
      <div className="flex flex-col min-h-screen">
        {impersonating && <ImpersonationBanner />}
        <div className="flex flex-1 min-h-0">
          <Sidebar />
          <main className="flex-1 bg-gray-50 overflow-auto">
            <div className="max-w-7xl mx-auto px-6 py-6">{children}</div>
          </main>
        </div>
      </div>
      <NotificationToast />
    </>
  );
}
