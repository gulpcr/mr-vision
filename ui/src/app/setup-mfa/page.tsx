"use client";

import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { MfaSettingsCard } from "@/components/MfaSettingsCard";
import { CortexMark } from "@/components/ui/CortexMark";
import { clearLocalSession } from "@/lib/session";

/** Forced two-factor enrolment: accounts whose role requires MFA land here until they
 * have set it up (the backend blocks everything else meanwhile). */
export default function SetupMfaPage() {
  const router = useRouter();

  const signOut = async () => {
    await api.auth.logout().catch(() => {});
    clearLocalSession();
    router.replace("/login");
  };

  return (
    <div className="relative min-h-screen flex items-center justify-center px-4 py-10">
      <div className="w-full max-w-xl">
        <div className="flex flex-col items-center gap-3 mb-6 text-center">
          <CortexMark className="w-9 h-9" />
          <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-100">Set up two-factor authentication</h1>
          <p className="text-sm text-gray-600 dark:text-gray-400 max-w-md">
            Your role has access to patient data, so your workspace requires a code from an
            authenticator app in addition to your password. This takes about a minute.
          </p>
        </div>
        {/* Full navigation once done so the auth context re-reads the unrestricted session. */}
        <MfaSettingsCard onEnrolled={() => { window.location.href = "/dashboard"; }} />
        <button type="button" onClick={signOut}
          className="w-full py-2 text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition">
          Sign out
        </button>
      </div>
    </div>
  );
}
