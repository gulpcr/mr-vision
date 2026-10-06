"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { clearLocalSession, pathAfterSignIn, storeSession } from "@/lib/session";
import { CortexMark } from "@/components/ui/CortexMark";

const inputCls =
  "w-full px-4 py-2.5 bg-white/70 dark:bg-white/5 border border-gray-300 dark:border-white/10 rounded-xl focus:outline-none focus:ring-2 focus:ring-accent focus:border-transparent transition text-sm";

/** Change your password — forced after an admin reset / for seeded accounts, and
 * reachable from Settings. Every other session of the account is signed out. */
export default function ChangePasswordPage() {
  const router = useRouter();
  const { mustChangePassword, user } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (next.length < 12) {
      setError("Use at least 12 characters.");
      return;
    }
    if (next !== confirm) {
      setError("The new passwords don't match.");
      return;
    }
    setBusy(true);
    try {
      const result = await api.auth.changePassword(current, next);
      storeSession(result);
      // Full navigation so the auth context re-reads the (now unrestricted) session.
      window.location.href = pathAfterSignIn(result);
    } catch (err: any) {
      setError(err.message || "Could not change the password");
    } finally {
      setBusy(false);
    }
  };

  const signOut = async () => {
    await api.auth.logout().catch(() => {});
    clearLocalSession();
    router.replace("/login");
  };

  return (
    <div className="relative min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-md glass-raised rounded-3xl shadow-glow-lg p-8 sm:p-10">
        <div className="flex flex-col items-center gap-3 mb-6">
          <CortexMark className="w-9 h-9" />
          <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-100">Change your password</h1>
          {mustChangePassword ? (
            <p className="text-sm text-center text-amber-700 dark:text-amber-300">
              Your account needs a new password before you can continue.
            </p>
          ) : (
            user && (
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Signed in as <span className="font-mono">{user.username}</span>
              </p>
            )
          )}
        </div>

        <form onSubmit={submit} className="space-y-4">
          {error && (
            <div className="bg-red-50/80 dark:bg-red-950/60 border border-red-200 dark:border-red-900 text-red-700 dark:text-red-300 px-4 py-3 rounded-xl text-sm">
              {error}
            </div>
          )}
          <label className="block">
            <span className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">Current password</span>
            <input type="password" autoComplete="current-password" value={current}
              onChange={(e) => setCurrent(e.target.value)} className={inputCls} required />
          </label>
          <label className="block">
            <span className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">New password</span>
            <input type="password" autoComplete="new-password" value={next}
              onChange={(e) => setNext(e.target.value)} className={inputCls} required minLength={12} />
            <span className="block text-xs text-gray-500 dark:text-gray-400 mt-1.5">
              At least 12 characters. A short phrase is easier to remember than symbols; common
              passwords and your username are not allowed.
            </span>
          </label>
          <label className="block">
            <span className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">Confirm new password</span>
            <input type="password" autoComplete="new-password" value={confirm}
              onChange={(e) => setConfirm(e.target.value)} className={inputCls} required />
          </label>
          <button type="submit" disabled={busy} className="btn-gradient w-full py-2.5 px-4 font-semibold rounded-xl text-sm">
            {busy ? "Saving…" : "Change password"}
          </button>
          <p className="text-xs text-center text-gray-500 dark:text-gray-400">
            Your other signed-in sessions will be signed out.
          </p>
          {mustChangePassword ? (
            <button type="button" onClick={signOut}
              className="w-full py-2 text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition">
              Sign out
            </button>
          ) : (
            <button type="button" onClick={() => router.back()}
              className="w-full py-2 text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition">
              Cancel
            </button>
          )}
        </form>
      </div>
    </div>
  );
}
