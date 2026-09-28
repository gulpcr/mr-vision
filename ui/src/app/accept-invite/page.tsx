"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { CortexMark } from "@/components/ui/CortexMark";

const inputCls =
  "w-full px-4 py-2.5 bg-white/70 dark:bg-white/5 border border-gray-300 dark:border-white/10 rounded-xl focus:outline-none focus:ring-2 focus:ring-accent focus:border-transparent transition text-sm";

/** Public page: redeem an invitation / password-reset link and choose a password. */
export default function AcceptInvitePage() {
  const router = useRouter();
  const [token, setToken] = useState<string | null>(null);
  const [workspace, setWorkspace] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setToken(params.get("token"));
    setWorkspace(params.get("workspace"));
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (password.length < 10) {
      setError("Use at least 10 characters.");
      return;
    }
    if (password !== confirm) {
      setError("The passwords don't match.");
      return;
    }
    setBusy(true);
    try {
      const res = await api.auth.acceptInvitation(token!, password);
      if (workspace) localStorage.setItem("workspace", workspace);
      setDone(res.username);
      window.setTimeout(() => router.push(`/login${workspace ? `?workspace=${encodeURIComponent(workspace)}` : ""}`), 2000);
    } catch (err: any) {
      setError(err.message || "This link could not be used");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-md glass-raised rounded-3xl shadow-glow-lg p-8 sm:p-10">
        <div className="flex flex-col items-center gap-3 mb-6">
          <CortexMark className="w-9 h-9" />
          <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-100">Set your password</h1>
          {workspace && (
            <p className="text-sm text-gray-500 dark:text-gray-400">
              Workspace <span className="font-mono">{workspace}</span>
            </p>
          )}
        </div>

        {!token ? (
          <p className="text-sm text-red-600 dark:text-red-400 text-center">
            This link is missing its token. Ask your administrator for a new invitation.
          </p>
        ) : done ? (
          <p className="text-sm text-green-700 dark:text-green-300 text-center">
            Password set for <strong>{done}</strong>. Taking you to sign in…
          </p>
        ) : (
          <form onSubmit={submit} className="space-y-4">
            {error && (
              <div className="bg-red-50/80 dark:bg-red-950/60 border border-red-200 dark:border-red-900 text-red-700 dark:text-red-300 px-4 py-3 rounded-xl text-sm">
                {error}
              </div>
            )}
            <label className="block">
              <span className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">New password</span>
              <input type="password" autoComplete="new-password" value={password}
                onChange={(e) => setPassword(e.target.value)} className={inputCls} required minLength={10} />
            </label>
            <label className="block">
              <span className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">Confirm password</span>
              <input type="password" autoComplete="new-password" value={confirm}
                onChange={(e) => setConfirm(e.target.value)} className={inputCls} required />
            </label>
            <button type="submit" disabled={busy} className="btn-gradient w-full py-2.5 px-4 font-semibold rounded-xl text-sm">
              {busy ? "Saving…" : "Set password"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
