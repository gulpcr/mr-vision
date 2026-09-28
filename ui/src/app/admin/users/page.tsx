"use client";

import { useEffect, useState } from "react";
import { useUsers } from "@/lib/hooks";
import { api, type RoleDef } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorBanner } from "@/components/ui/ErrorBanner";
import { Table, Caption, Th } from "@/components/ui/Table";
import { TableSkeleton } from "@/components/ui/TableSkeleton";
import { Users, Plus, UserX, ShieldAlert, Link2, LogOut, Copy, Check } from "lucide-react";

function roleBadgeClass(role: string): string {
  switch (role) {
    case "admin":
      return "bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300";
    case "radiologist":
      return "bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300";
    case "doctor":
      return "bg-teal-50 dark:bg-teal-950 text-teal-700 dark:text-teal-300";
    case "technician":
      return "bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300";
    case "receptionist":
      return "bg-purple-50 dark:bg-purple-950 text-purple-700 dark:text-purple-300";
    default:
      return "bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300";
  }
}

function statusBadge(u: { is_active: boolean; status?: string }) {
  if (u.status === "invited") {
    return { label: "Invited", cls: "bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-300" };
  }
  return u.is_active
    ? { label: "Active", cls: "bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300" }
    : { label: "Deactivated", cls: "bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400" };
}

function CopyLink({ link }: { link: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="space-y-2">
      <p className="text-xs text-gray-500 dark:text-gray-400">
        Send this one-time link to the user (valid for 72 hours). It lets them set their own password.
      </p>
      <div className="flex gap-2">
        <input
          readOnly
          value={link}
          className="flex-1 text-xs font-mono border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2"
          onFocus={(e) => e.currentTarget.select()}
        />
        <button
          type="button"
          onClick={async () => {
            await navigator.clipboard.writeText(link).catch(() => {});
            setCopied(true);
          }}
          className="px-3 py-2 text-xs font-medium rounded-lg bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700"
          aria-label="Copy link"
        >
          {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
        </button>
      </div>
    </div>
  );
}

const inputCls =
  "w-full mt-1 text-sm border border-gray-200 dark:border-gray-700 dark:bg-surface-raised rounded-lg px-3 py-2";

export default function UsersPage() {
  const { can, user: currentUser, role: myRole } = useAuth();
  const { data: users, isLoading, mutate } = useUsers();
  const [roles, setRoles] = useState<RoleDef[]>([]);
  const [showInvite, setShowInvite] = useState(false);
  const [form, setForm] = useState({ username: "", email: "", full_name: "", role: "radiologist" });
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [inviteBusy, setInviteBusy] = useState(false);
  const [issuedLink, setIssuedLink] = useState<{ username: string; link: string } | null>(null);
  const [pendingDeactivate, setPendingDeactivate] = useState<{ id: string; username: string } | null>(null);
  const [pendingRoleChange, setPendingRoleChange] = useState<{ id: string; username: string; role: string } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const allowed = can("user.manage");
  useEffect(() => {
    if (!allowed) return;
    api.roles.list().then(setRoles).catch(() => setRoles([]));
  }, [allowed]);

  if (!allowed) {
    return (
      <EmptyState
        icon={ShieldAlert}
        title="You don't have permission to manage users"
        description="This screen requires the user.manage permission."
      />
    );
  }

  // Only an admin may hand out the admin role.
  const assignableRoles = roles.filter((r) => r.name !== "admin" || myRole === "admin");

  const handleInvite = async (e: React.FormEvent) => {
    e.preventDefault();
    setInviteBusy(true);
    setInviteError(null);
    try {
      const res = await api.auth.inviteUser(form);
      setShowInvite(false);
      setIssuedLink({ username: res.username, link: res.invite_link });
      setForm({ username: "", email: "", full_name: "", role: form.role });
      mutate();
    } catch (err: any) {
      setInviteError(err.message || "Failed to invite user");
    } finally {
      setInviteBusy(false);
    }
  };

  const reinvite = async (id: string, username: string) => {
    try {
      const res = await api.auth.reinviteUser(id);
      setIssuedLink({ username, link: res.invite_link });
    } catch (err: any) {
      setActionError(err.message || "Failed to create a new link");
    }
  };

  const revoke = async (id: string, username: string) => {
    try {
      await api.auth.revokeUserSessions(id);
      setNotice(`${username} has been signed out on every device.`);
    } catch (err: any) {
      setActionError(err.message || "Failed to revoke sessions");
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Users className="w-7 h-7 text-primary-600" />
          <div>
            <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Users</h1>
            <p className="text-sm text-gray-500 dark:text-gray-400">People with access to this workspace.</p>
          </div>
        </div>
        <button
          onClick={() => setShowInvite(true)}
          className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-primary-600 rounded-lg hover:bg-primary-700 transition-colors"
        >
          <Plus className="w-4 h-4" /> Invite user
        </button>
      </div>

      {actionError && <ErrorBanner message={actionError} onDismiss={() => setActionError(null)} className="mb-4" />}
      {notice && (
        <div className="mb-4 px-4 py-2 rounded-lg text-sm bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300">
          {notice}
        </div>
      )}

      <div className="bg-white dark:bg-surface rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
        <Table>
          <Caption>Workspace users, their roles, and account status</Caption>
          <thead>
            <tr className="border-b border-gray-100 dark:border-gray-800">
              <Th>Username</Th>
              <Th>Full name</Th>
              <Th>Email</Th>
              <Th>Role</Th>
              <Th>Status</Th>
              <Th className="w-28" />
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
            {isLoading ? (
              <TableSkeleton rows={5} columnWidths={[100, 120, 160, 90, 70, 60]} />
            ) : !users || users.length === 0 ? (
              <tr>
                <td colSpan={6}>
                  <EmptyState icon={Users} title="No users yet" description="Invite your radiologists, doctors and staff." />
                </td>
              </tr>
            ) : (
              users.map((u: any) => {
                const badge = statusBadge(u);
                const isSelf = u.id === currentUser?.id;
                return (
                  <tr key={u.id} className="hover:bg-gray-50 dark:hover:bg-gray-800/50">
                    <td className="py-2.5 px-4 font-medium text-gray-900 dark:text-gray-100">{u.username}</td>
                    <td className="py-2.5 px-4 text-gray-600 dark:text-gray-400">{u.full_name || "—"}</td>
                    <td className="py-2.5 px-4 text-gray-600 dark:text-gray-400">{u.email}</td>
                    <td className="py-2.5 px-4">
                      <select
                        value={u.role}
                        disabled={isSelf || (u.role === "admin" && myRole !== "admin")}
                        onChange={(e) => setPendingRoleChange({ id: u.id, username: u.username, role: e.target.value })}
                        className={`text-xs font-medium rounded-full px-2 py-1 border-0 disabled:opacity-50 ${roleBadgeClass(u.role)}`}
                        aria-label={`Role of ${u.username}`}
                      >
                        {(assignableRoles.some((r) => r.name === u.role) ? assignableRoles : [...assignableRoles, { id: u.role, name: u.role } as RoleDef]).map((r) => (
                          <option key={r.id} value={r.name}>{r.name}</option>
                        ))}
                      </select>
                    </td>
                    <td className="py-2.5 px-4">
                      <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${badge.cls}`}>{badge.label}</span>
                    </td>
                    <td className="py-2.5 px-4">
                      <div className="flex items-center gap-1 justify-end">
                        {u.status === "invited" && (
                          <button
                            onClick={() => reinvite(u.id, u.username)}
                            title="New invitation link"
                            aria-label={`New invitation link for ${u.username}`}
                            className="p-1.5 text-gray-400 hover:text-primary-600 hover:bg-primary-50 dark:hover:bg-primary-950 rounded-lg"
                          >
                            <Link2 className="w-4 h-4" />
                          </button>
                        )}
                        {u.is_active && !isSelf && (
                          <>
                            <button
                              onClick={() => revoke(u.id, u.username)}
                              title="Sign out everywhere"
                              aria-label={`Sign ${u.username} out everywhere`}
                              className="p-1.5 text-gray-400 hover:text-amber-600 hover:bg-amber-50 dark:hover:bg-amber-950 rounded-lg"
                            >
                              <LogOut className="w-4 h-4" />
                            </button>
                            <button
                              onClick={() => setPendingDeactivate({ id: u.id, username: u.username })}
                              title="Deactivate"
                              aria-label={`Deactivate ${u.username}`}
                              className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-950 rounded-lg"
                            >
                              <UserX className="w-4 h-4" />
                            </button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </Table>
      </div>

      <Modal open={showInvite} onClose={() => setShowInvite(false)} title="Invite user" size="sm">
        <form onSubmit={handleInvite} className="space-y-3">
          {inviteError && <ErrorBanner message={inviteError} className="mb-1" />}
          <label className="block text-xs">
            <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Username</span>
            <input required minLength={3} value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })} className={inputCls} />
          </label>
          <label className="block text-xs">
            <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Email</span>
            <input required type="email" value={form.email}
              onChange={(e) => setForm({ ...form, email: e.target.value })} className={inputCls} />
          </label>
          <label className="block text-xs">
            <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Full name</span>
            <input value={form.full_name}
              onChange={(e) => setForm({ ...form, full_name: e.target.value })} className={inputCls} />
          </label>
          <label className="block text-xs">
            <span className="text-gray-500 dark:text-gray-400 uppercase tracking-wider">Role</span>
            <select required value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} className={inputCls}>
              {assignableRoles.map((r) => (
                <option key={r.id} value={r.name}>{r.name}</option>
              ))}
            </select>
          </label>
          <p className="text-xs text-gray-400 dark:text-gray-500">
            You&apos;ll get a one-time link to send them. They set their own password; nobody else ever sees it.
          </p>
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" onClick={() => setShowInvite(false)}
              className="px-4 py-2 text-sm font-medium text-gray-600 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 rounded-lg transition-colors">
              Cancel
            </button>
            <button type="submit" disabled={inviteBusy}
              className="px-4 py-2 text-sm font-medium text-white bg-primary-600 hover:bg-primary-700 disabled:opacity-50 rounded-lg transition-colors">
              {inviteBusy ? "Inviting…" : "Create invitation"}
            </button>
          </div>
        </form>
      </Modal>

      <Modal open={issuedLink !== null} onClose={() => setIssuedLink(null)} title={`Invitation for ${issuedLink?.username ?? ""}`} size="sm">
        {issuedLink && <CopyLink link={issuedLink.link} />}
      </Modal>

      <ConfirmDialog
        tier="modal"
        open={pendingRoleChange !== null}
        title="Change role"
        consequence={
          pendingRoleChange
            ? `Change ${pendingRoleChange.username}'s role to "${pendingRoleChange.role}"? This changes what they can access immediately.`
            : ""
        }
        confirmLabel="Change role"
        onConfirm={async () => {
          if (!pendingRoleChange) return;
          try {
            await api.auth.updateUserRole(pendingRoleChange.id, pendingRoleChange.role);
            mutate();
          } catch (err: any) {
            setActionError(err.message || "Failed to update role");
          }
          setPendingRoleChange(null);
        }}
        onCancel={() => setPendingRoleChange(null)}
      />

      <ConfirmDialog
        tier="modal"
        danger
        open={pendingDeactivate !== null}
        title="Deactivate user"
        consequence={
          pendingDeactivate
            ? `${pendingDeactivate.username} will immediately lose access to the workspace.`
            : ""
        }
        confirmLabel="Deactivate"
        onConfirm={async () => {
          if (!pendingDeactivate) return;
          try {
            await api.auth.deactivateUser(pendingDeactivate.id);
            mutate();
          } catch (err: any) {
            setActionError(err.message || "Failed to deactivate user");
          }
          setPendingDeactivate(null);
        }}
        onCancel={() => setPendingDeactivate(null)}
      />
    </div>
  );
}
