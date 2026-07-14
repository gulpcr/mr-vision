"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { Activity } from "lucide-react";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  // Set once /login responds with mfa_required=true — switches the form to
  // the second step (TOTP/recovery code) instead of a real navigation.
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [mfaCode, setMfaCode] = useState("");

  const storeSessionAndGo = (result: {
    access_token: string | null;
    user_id: string | null;
    username: string | null;
    role: string | null;
    tenant_id: string | null;
  }) => {
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
    if (!mfaToken) return;
    setError("");
    setLoading(true);

    try {
      const result = await api.auth.mfaVerify(mfaToken, mfaCode);
      storeSessionAndGo(result);
    } catch (e: any) {
      setError(e.message || "Verification failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-100">
      <div className="w-full max-w-md">
        <div className="bg-white rounded-xl shadow-lg p-8">
          <div className="flex items-center justify-center gap-3 mb-8">
            <Activity className="w-8 h-8 text-blue-600" />
            <h1 className="text-2xl font-bold text-gray-900">MRI AI Platform</h1>
          </div>

          {mfaToken ? (
            <form onSubmit={handleMfaVerify} className="space-y-4">
              {error && (
                <div className="bg-red-50 text-red-700 px-4 py-3 rounded-lg text-sm">
                  {error}
                </div>
              )}

              <p className="text-sm text-gray-600">
                Enter the 6-digit code from your authenticator app, or one of your
                recovery codes.
              </p>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Verification code
                </label>
                <input
                  type="text"
                  inputMode="numeric"
                  autoFocus
                  value={mfaCode}
                  onChange={(e) => setMfaCode(e.target.value)}
                  className="w-full px-4 py-2.5 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm tracking-widest"
                  placeholder="123456"
                  required
                />
              </div>

              <button
                type="submit"
                disabled={loading}
                className="w-full py-2.5 px-4 bg-blue-600 text-white font-medium rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors text-sm"
              >
                {loading ? "Verifying..." : "Verify"}
              </button>

              <button
                type="button"
                onClick={() => { setMfaToken(null); setMfaCode(""); setError(""); }}
                className="w-full text-center text-xs text-gray-400 hover:text-gray-600"
              >
                Back to sign in
              </button>
            </form>
          ) : (
            <form onSubmit={handleLogin} className="space-y-4">
              {error && (
                <div className="bg-red-50 text-red-700 px-4 py-3 rounded-lg text-sm">
                  {error}
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Username
                </label>
                <input
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  className="w-full px-4 py-2.5 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                  placeholder="Enter your username"
                  required
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Password
                </label>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full px-4 py-2.5 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 text-sm"
                  placeholder="Enter your password"
                  required
                />
              </div>

              <button
                type="submit"
                disabled={loading}
                className="w-full py-2.5 px-4 bg-blue-600 text-white font-medium rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors text-sm"
              >
                {loading ? "Signing in..." : "Sign In"}
              </button>
            </form>
          )}

          <p className="text-center text-xs text-gray-400 mt-6">
            MRI AI Platform v1.0 - Production-grade AI-based MRI analysis
          </p>
        </div>
      </div>
    </div>
  );
}
