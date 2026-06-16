"use client";

import { Users, Shield, Eye, Stethoscope, UserCog } from "lucide-react";

const ROLES = [
  {
    name: "Administrator",
    icon: UserCog,
    description: "Full platform access including user management and system configuration",
    color: "bg-red-50 text-red-700",
  },
  {
    name: "Radiologist",
    icon: Stethoscope,
    description: "View studies, run AI analysis, review and approve reports",
    color: "bg-blue-50 text-blue-700",
  },
  {
    name: "Technician",
    icon: Users,
    description: "Upload DICOM studies, view worklist, and trigger AI analysis",
    color: "bg-amber-50 text-amber-700",
  },
  {
    name: "Viewer",
    icon: Eye,
    description: "Read-only access to studies and completed reports",
    color: "bg-gray-100 text-gray-600",
  },
];

export default function UsersPage() {
  return (
    <div>
      <h1 className="text-2xl font-bold text-gray-900 mb-6">User Management</h1>

      {/* Coming soon banner */}
      <div className="bg-primary-50 border border-primary-200 rounded-lg p-5 mb-8">
        <div className="flex items-start gap-3">
          <Shield className="w-6 h-6 text-primary-600 mt-0.5" />
          <div>
            <h2 className="font-semibold text-primary-900">
              Authentication & Access Control
            </h2>
            <p className="text-sm text-primary-700 mt-1">
              User authentication and role-based access control (RBAC) will be
              configured here. The backend already includes RBAC middleware
              for authorization enforcement.
            </p>
          </div>
        </div>
      </div>

      {/* Roles Overview */}
      <div className="bg-white rounded-lg shadow-sm border border-gray-200">
        <div className="px-5 py-4 border-b border-gray-100">
          <h2 className="font-semibold text-gray-900">Role Definitions</h2>
          <p className="text-xs text-gray-500 mt-0.5">
            Available roles for platform access control
          </p>
        </div>
        <div className="divide-y divide-gray-50">
          {ROLES.map((role) => {
            const Icon = role.icon;
            return (
              <div key={role.name} className="flex items-center gap-4 px-5 py-4">
                <div className={`p-2.5 rounded-lg ${role.color}`}>
                  <Icon className="w-5 h-5" />
                </div>
                <div>
                  <p className="font-medium text-gray-900">{role.name}</p>
                  <p className="text-sm text-gray-500">{role.description}</p>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
