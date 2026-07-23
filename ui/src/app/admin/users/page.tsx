"use client";

import { useState } from "react";
import { useUsers } from "@/lib/hooks";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { SYSTEM_ROLE_PERMISSIONS } from "@/lib/permissions";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { Table, Caption, Th } from "@/components/ui/Table";
import { TableSkeleton } from "@/components/ui/TableSkeleton";
import { Users, Plus, UserX, ShieldAlert } from "lucide-react";

const ROLE_NAMES = Object.keys(SYSTEM_ROLE_PERMISSIONS);

function roleBadgeClass(role: string): string {
  switch (role) {
    case "admin":
      return "bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300";
    case "radiologist":
      return "bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300";
    case "technician":
      return "bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300";
    case "receptionist":
      return "bg-purple-50 dark:bg-purple-950 text-purple-700 dark:text-purple-300";
    default:
      return "bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300";
  }
}

export default function UsersPage() {
  const { can, user: currentUser } = useAuth();
  const { data: users, isLoading, mutate } = useUsers();
  const [showInvite, setShowInvite] = useState(false);
  const [form, setForm] = useState({ username: "", email: "", password: "", full_name: "" });
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [inviteBusy, setInviteBusy] = useState(false);
  const [pendingDeactivate, setPendingDeactivate] = useState<{ id: string; username: string } | null>(null);
  const [pendingRoleChange, setPendingRoleChange] = useState<{ id: string; username: string; role: string } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  if (!can("user.manage")) {
    return (
      <EmptyState
        icon={ShieldAlert}
        title="You don't have permission to manage users"
        description="This screen requires the user.manage permission."
      />
    );
  }

  const handleInvite = async (e: React.FormEvent) => {
    e.preventDefault();
    setInviteBusy(true);
    setInviteError(null);
    try {
      await api.auth.register(form);
      setShowInvite(false);
      setForm({ username: "", email: "", password: "", full_name: "" });
      mutate();
    } catch (e: any) {
      setInviteError(e.message || "Failed to create user");
    } finally {
      setInviteBusy(false);
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Users className="w-7 h-7 text-primary-600" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Users</h1>
        </div>
        <button
          onClick={() => setShowInvite(true)}
          className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
        >
          <Plus className="w-4 h-4" /> Invite User
        </button>
      </div>

      {actionError && <ErrorBanner message={actionError} onDismiss={() => setActionError(null)} className="mb-4" />}

      <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
        <Table>
          <Caption>Platform users, their roles, and account status</Caption>
          <thead>
            <tr className="border-b border-gray-100 dark:border-gray-800">
              <Th>Username</Th>
              <Th>Full Name</Th>
              <Th>Email</Th>
              <Th>Role</Th>
              <Th>Status</Th>
              <Th className="w-16" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
            {isLoading ? (
              <TableSkeleton rows={5} columnWidths={[100, 120, 160, 90, 70, 32]} />
            ) : !users || users.length === 0 ? (
              <tr>
                <td colSpan={6}>
                  <EmptyState icon={Users} title="No users found" />
                </td>
              </tr>
            ) : (
              users.map((u) => (
                <tr key={u.id} className="hover:bg-gray-50 dark:hover:bg-gray-800/50">
                  <td className="py-2.5 px-4 font-medium text-gray-900 dark:text-gray-100">{u.username}</td>
                  <td className="py-2.5 px-4 text-gray-600 dark:text-gray-400">{u.full_name || "—"}</td>
                  <td className="py-2.5 px-4 text-gray-600 dark:text-gray-400">{u.email}</td>
                  <td className="py-2.5 px-4">
                    <select
                      value={u.role}
                      disabled={u.id === currentUser?.id}
                      onChange={(e) => setPendingRoleChange({ id: u.id, username: u.username, role: e.target.value })}
                      className={`text-xs font-medium rounded-full px-2 py-1 border-0 disabled:opacity-50 ${roleBadgeClass(u.role)}`}
                    >
                      {ROLE_NAMES.map((r) => (
                        <option key={r} value={r}>{r}</option>
                      ))}
                    </select>
                  </td>
                  <td className="py-2.5 px-4">
                    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${
                      u.is_active
                        ? "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300"
                        : "bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400"
                    }`}>
                      {u.is_active ? "Active" : "Deactivated"}
                    </span>
                  </td>
                  <td className="py-2.5 px-4">
                    {u.is_active && u.id !== currentUser?.id && (
                      <button
                        onClick={() => setPendingDeactivate({ id: u.id, username: u.username })}
                        aria-label={`Deactivate ${u.username}`}
                        className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-950 rounded-lg transition-colors"
                      >
                        <UserX className="w-4 h-4" />
                      </button>
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </Table>
      </div>

      <Modal open={showInvite} onClose={() => setShowInvite(false)} title="Invite User" size="sm">
        <form onSubmit={handleInvite} className="space-y-3">
          {inviteError && <ErrorBanner message={inviteError} className="mb-1" />}
          <label className="block text-xs">
            <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Username</span>
            <input
              required
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
              className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2"
            />
          </label>
          <label className="block text-xs">
            <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Email</span>
            <input
              required
              type="email"
              value={form.email}
              onChange={(e) => setForm({ ...form, email: e.target.value })}
              className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2"
            />
          </label>
          <label className="block text-xs">
            <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Full Name</span>
            <input
              value={form.full_name}
              onChange={(e) => setForm({ ...form, full_name: e.target.value })}
              className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2"
            />
          </label>
          <label className="block text-xs">
            <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Temporary Password</span>
            <input
              required
              type="password"
              minLength={8}
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              className="w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2"
            />
          </label>
          <p className="text-xs text-gray-400 dark:text-gray-500">
            New users are created with the "viewer" role — change it from the table after inviting.
          </p>
          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={() => setShowInvite(false)}
              className="px-4 py-2 text-sm font-medium text-gray-600 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={inviteBusy}
              className="px-4 py-2 text-sm font-medium text-white bg-primary-600 hover:bg-primary-700 disabled:opacity-50 rounded-lg transition-colors"
            >
              {inviteBusy ? "Creating…" : "Create User"}
            </button>
          </div>
        </form>
      </Modal>

      <ConfirmDialog
        tier="modal"
        open={pendingRoleChange !== null}
        title="Change Role"
        consequence={
          pendingRoleChange
            ? `Change ${pendingRoleChange.username}'s role to "${pendingRoleChange.role}"? This changes what they can access immediately.`
            : ""
        }
        confirmLabel="Change Role"
        onConfirm={async () => {
          if (!pendingRoleChange) return;
          try {
            await api.auth.updateUserRole(pendingRoleChange.id, pendingRoleChange.role);
            mutate();
          } catch (e: any) {
            setActionError(e.message || "Failed to update role");
          }
          setPendingRoleChange(null);
        }}
        onCancel={() => setPendingRoleChange(null)}
      />

      <ConfirmDialog
        tier="modal"
        danger
        open={pendingDeactivate !== null}
        title="Deactivate User"
        consequence={
          pendingDeactivate
            ? `${pendingDeactivate.username} will immediately lose access to the platform. This can be reversed later by a database administrator, not from this screen.`
            : ""
        }
        confirmLabel="Deactivate"
        onConfirm={async () => {
          if (!pendingDeactivate) return;
          try {
            await api.auth.deactivateUser(pendingDeactivate.id);
            mutate();
          } catch (e: any) {
            setActionError(e.message || "Failed to deactivate user");
          }
          setPendingDeactivate(null);
        }}
        onCancel={() => setPendingDeactivate(null)}
      />
    </div>
  );
}
