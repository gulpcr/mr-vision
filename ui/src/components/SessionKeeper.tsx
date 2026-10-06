"use client";

import { useEffect, useRef, useState } from "react";
import {
  IDLE_LOGOFF_MS,
  IDLE_WARNING_MS,
  lastActivity,
  markActivity,
  refreshAccessToken,
  signOut,
  tokenSecondsLeft,
} from "@/lib/session";

const ACTIVITY_EVENTS = ["mousedown", "mousemove", "keydown", "wheel", "touchstart", "scroll"] as const;

/** Keeps an active session alive (refreshes the 15-minute access token shortly before
 * it expires) and signs out after 15 minutes without activity — HIPAA automatic logoff.
 * Activity inside same-origin iframes (the OHIF viewer) counts too. The server enforces
 * the same idle limit, so a closed or sleeping tab cannot keep a session alive. */
export function SessionKeeper() {
  const [remainingMs, setRemainingMs] = useState<number | null>(null);
  const lastWrite = useRef(0);

  useEffect(() => {
    const onActivity = () => {
      const now = Date.now();
      if (now - lastWrite.current > 5_000) {
        lastWrite.current = now;
        markActivity();
      }
    };
    markActivity();

    const opts = { capture: true, passive: true } as AddEventListenerOptions;
    ACTIVITY_EVENTS.forEach((e) => window.addEventListener(e, onActivity, opts));

    // The embedded viewer is a same-origin iframe whose events don't reach this window.
    const watched = new WeakSet<Window>();
    const attachFrames = () => {
      document.querySelectorAll("iframe").forEach((frame) => {
        try {
          const w = frame.contentWindow;
          if (w && !watched.has(w) && w.document) {
            ACTIVITY_EVENTS.forEach((e) => w.addEventListener(e, onActivity, opts));
            watched.add(w);
          }
        } catch {
          /* cross-origin frame: ignore */
        }
      });
    };
    attachFrames();

    const tick = window.setInterval(() => {
      attachFrames();
      const idle = Date.now() - lastActivity();
      if (idle >= IDLE_LOGOFF_MS) {
        window.clearInterval(tick);
        void signOut("idle");
        return;
      }
      if (idle >= IDLE_WARNING_MS) {
        setRemainingMs(IDLE_LOGOFF_MS - idle);
        return;
      }
      setRemainingMs(null);
      // Active: renew the access token a couple of minutes before it expires.
      if (tokenSecondsLeft() < 150) void refreshAccessToken();
    }, 5_000);

    return () => {
      window.clearInterval(tick);
      ACTIVITY_EVENTS.forEach((e) => window.removeEventListener(e, onActivity, opts));
    };
  }, []);

  if (remainingMs === null) return null;
  const secs = Math.max(0, Math.ceil(remainingMs / 1000));
  const mmss = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 px-4" role="alertdialog"
      aria-modal="true" aria-labelledby="idle-title">
      <div className="w-full max-w-sm glass-raised rounded-2xl shadow-glow-lg p-6 text-center">
        <h2 id="idle-title" className="text-lg font-semibold text-gray-900 dark:text-gray-100">Are you still there?</h2>
        <p className="text-sm text-gray-600 dark:text-gray-400 mt-2">
          To protect patient information you&apos;ll be signed out in{" "}
          <span className="font-mono font-semibold">{mmss}</span> because of inactivity.
        </p>
        <div className="flex gap-3 mt-5">
          <button
            type="button"
            onClick={() => void signOut()}
            className="flex-1 px-4 py-2 rounded-lg text-sm font-medium bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700"
          >
            Sign out
          </button>
          <button
            type="button"
            autoFocus
            onClick={() => {
              markActivity();
              setRemainingMs(null);
              void refreshAccessToken();
            }}
            className="flex-1 btn-gradient px-4 py-2 rounded-lg text-sm font-semibold"
          >
            Stay signed in
          </button>
        </div>
      </div>
    </div>
  );
}
