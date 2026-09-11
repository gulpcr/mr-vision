"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { CortexMark } from "@/components/ui/CortexMark";

interface SessionResult {
  access_token: string | null;
  user_id: string | null;
  username: string | null;
  role: string | null;
  tenant_id: string | null;
}

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [mfaCode, setMfaCode] = useState("");

  const storeSessionAndGo = (result: SessionResult) => {
    localStorage.setItem("auth_token", result.access_token || "");
    localStorage.setItem("user", JSON.stringify({
      id: result.user_id,
      username: result.username,
      role: result.role,
      tenant_id: result.tenant_id,
    }));
    router.push("/dashboard");
  };

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const result = await api.auth.login(username, password);
      if (result.mfa_required) {
        setMfaToken(result.mfa_token);
      } else {
        storeSessionAndGo(result);
      }
    } catch (e: any) {
      setError(e.message || "Login failed");
    } finally {
      setLoading(false);
    }
  };

  const handleMfaVerify = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const result = await api.auth.mfaVerify(mfaToken!, mfaCode);
      storeSessionAndGo(result);
    } catch (e: any) {
      setError(e.message || "Verification failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative min-h-screen flex items-center justify-center overflow-hidden px-4">
      {/* Ambient glow orbs — slowly drifting for a living backdrop */}
      <div className="pointer-events-none absolute -top-32 -left-24 w-[32rem] h-[32rem] rounded-full bg-cyan-500/20 blur-3xl float" />
      <div className="pointer-events-none absolute -bottom-40 -right-24 w-[34rem] h-[34rem] rounded-full bg-violet-500/20 blur-3xl animate-pulse" style={{ animationDuration: "6s" }} />
      <div className="pointer-events-none absolute top-1/3 left-1/2 -translate-x-1/2 w-[28rem] h-[28rem] rounded-full bg-sky-500/10 blur-3xl float" style={{ animationDuration: "7s" }} />

      <div className="relative w-full max-w-md">
        <div className="glass-raised rounded-3xl shadow-glow-lg p-8 sm:p-10 accent-top animate-scale-in">
          <div className="flex flex-col items-center gap-3 mb-8">
            <div className="group grid place-items-center w-16 h-16 rounded-2xl bg-accent/10 ring-1 ring-accent/30 shadow-glow transition-transform duration-300 hover:scale-105">
              <CortexMark className="w-9 h-9 transition-transform duration-700 ease-out group-hover:rotate-90" />
            </div>
            <h1 className="text-2xl tracking-tight">
              <span className="font-extrabold text-gradient-anim">CORTEX</span>
              <span className="font-medium text-gray-700 dark:text-gray-200"> Radiology</span>
            </h1>
            <p className="text-xs font-mono uppercase tracking-[0.2em] text-accent">AI-Assisted Imaging Platform</p>
          </div>

          {mfaToken ? (
            <form onSubmit={handleMfaVerify} className="space-y-4">
              {error && (
                <div className="bg-red-50/80 dark:bg-red-950/60 border border-red-200 dark:border-red-900 text-red-700 dark:text-red-300 px-4 py-3 rounded-xl text-sm">
                  {error}
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">
                  Verification code
                </label>
                <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">
                  Enter the 6-digit code from your authenticator app, or one of your recovery codes.
                </p>
                <input
                  type="text"
                  inputMode="numeric"
                  autoFocus
                  value={mfaCode}
                  onChange={(e) => setMfaCode(e.target.value)}
                  className="w-full px-4 py-2.5 bg-white/70 dark:bg-white/5 border border-gray-300 dark:border-white/10 rounded-xl focus:outline-none focus:ring-2 focus:ring-accent focus:border-transparent transition text-sm tracking-widest"
                  placeholder="000000"
                  required
                />
              </div>

              <button
                type="submit"
                disabled={loading}
                className="btn-gradient w-full py-2.5 px-4 font-semibold rounded-xl text-sm"
              >
                {loading ? "Verifying..." : "Verify"}
              </button>

              <button
                type="button"
                onClick={() => {
                  setMfaToken(null);
                  setMfaCode("");
                  setError("");
                }}
                className="w-full py-2 text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition"
              >
                Back to sign in
              </button>
            </form>
          ) : (
            <form onSubmit={handleLogin} className="space-y-4">
              {error && (
                <div className="bg-red-50/80 dark:bg-red-950/60 border border-red-200 dark:border-red-900 text-red-700 dark:text-red-300 px-4 py-3 rounded-xl text-sm">
                  {error}
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">
                  Username
                </label>
                <input
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className="w-full px-4 py-2.5 bg-white/70 dark:bg-white/5 border border-gray-300 dark:border-white/10 rounded-xl focus:outline-none focus:ring-2 focus:ring-accent focus:border-transparent transition text-sm"
                  placeholder="Enter your username"
                  required
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">
                  Password
                </label>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full px-4 py-2.5 bg-white/70 dark:bg-white/5 border border-gray-300 dark:border-white/10 rounded-xl focus:outline-none focus:ring-2 focus:ring-accent focus:border-transparent transition text-sm"
                  placeholder="Enter your password"
                  required
                />
              </div>

              <button
                type="submit"
                disabled={loading}
                className="btn-gradient w-full py-2.5 px-4 font-semibold rounded-xl text-sm"
              >
                {loading ? "Signing in..." : "Sign In"}
              </button>
            </form>
          )}

          <p className="text-center text-xs text-gray-400 dark:text-gray-500 mt-8">
            Cortex Radiology v1.0 — AI-assisted diagnostic imaging
          </p>
        </div>
      </div>
    </div>
  );
}
