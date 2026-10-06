"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, type SessionResponse } from "@/lib/api";
import { storeSession, pathAfterSignIn } from "@/lib/session";
import { CortexMark } from "@/components/ui/CortexMark";


export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [mfaCode, setMfaCode] = useState("");
  // Workspace (tenant). Usernames are unique per workspace; it may also come from the
  // subdomain, the invitation link (?workspace=) or the last successful login.
  const [workspace, setWorkspace] = useState("");
  const [branding, setBranding] = useState<{
    display_name: string | null; logo_data_url: string | null; primary_color: string | null;
  } | null>(null);

  useEffect(() => {
    const reason = new URLSearchParams(window.location.search).get("reason");
    if (reason === "idle") setError("You were signed out after 15 minutes of inactivity.");
    else if (reason === "expired") setError("Your session has ended — please sign in again.");
  }, []);

  useEffect(() => {
    const fromQuery = new URLSearchParams(window.location.search).get("workspace");
    let remembered: string | null = null;
    try { remembered = localStorage.getItem("workspace"); } catch { remembered = null; }
    setWorkspace(fromQuery || remembered || "");
  }, []);

  useEffect(() => {
    const handle = window.setTimeout(() => {
      api.tenant
        .publicBranding(workspace.trim() || undefined)
        .then((b) => setBranding(b.display_name || b.logo_data_url ? b : null))
        .catch(() => setBranding(null));
    }, 350);
    return () => window.clearTimeout(handle);
  }, [workspace]);

  const storeSessionAndGo = (result: SessionResponse) => {
    storeSession(result);
    if (workspace.trim()) localStorage.setItem("workspace", workspace.trim().toLowerCase());
    router.push(pathAfterSignIn(result));
  };

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const result = await api.auth.login(username, password, workspace.trim().toLowerCase() || undefined);
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
            {branding && (
              <div className="flex items-center gap-2 mt-2 px-3 py-1.5 rounded-xl bg-white/60 dark:bg-white/5 ring-1 ring-gray-200 dark:ring-white/10">
                {branding.logo_data_url && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={branding.logo_data_url} alt="" className="h-7 w-auto max-w-[8rem] object-contain" />
                )}
                {branding.display_name && (
                  <span className="text-sm font-medium" style={branding.primary_color ? { color: branding.primary_color } : undefined}>
                    {branding.display_name}
                  </span>
                )}
              </div>
            )}
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
                  Workspace <span className="text-gray-400 font-normal">(your hospital or clinic)</span>
                </label>
                <input
                  type="text"
                  value={workspace}
                  onChange={(e) => setWorkspace(e.target.value)}
                  className="w-full px-4 py-2.5 bg-white/70 dark:bg-white/5 border border-gray-300 dark:border-white/10 rounded-xl focus:outline-none focus:ring-2 focus:ring-accent focus:border-transparent transition text-sm"
                  placeholder="e.g. city-hospital"
                  autoCapitalize="none"
                  spellCheck={false}
                />
              </div>

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
