"use client";

import { useState, useEffect } from "react";
import { usePathname } from "next/navigation";
import Link from "next/link";
import clsx from "clsx";
import {
  PanelLeftClose,
  PanelLeftOpen,
  Activity,
  LogOut,
  Sun,
  Moon,
  Languages,
} from "lucide-react";
import { useCriticalAlertStats } from "@/lib/hooks";
import { useAuth } from "@/lib/auth";
import { useTheme } from "@/lib/theme";
import { useLocale } from "@/lib/i18n";
import { NAV_ITEMS } from "@/lib/nav";

export function Sidebar() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const { data: alertStats } = useCriticalAlertStats();
  const unackedCount = alertStats?.total_unacknowledged ?? 0;
  const { can } = useAuth();
  const { theme, toggle: toggleTheme } = useTheme();
  const { locale, setLocale, strings } = useLocale();
  const visibleItems = NAV_ITEMS.filter((item) => !item.requiredPermission || can(item.requiredPermission));

  useEffect(() => {
    const saved = localStorage.getItem("sidebar-collapsed");
    if (saved === "true") setCollapsed(true);
  }, []);

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem("sidebar-collapsed", String(next));
  };

  const isActive = (href: string) => {
    if (href === "/dashboard") return pathname === "/dashboard";
    if (href === "/worklist") return pathname === "/worklist" || pathname.startsWith("/study");
    if (href === "/review") return pathname === "/review" || pathname.startsWith("/review/");
    return pathname.startsWith(href);
  };

  return (
    <aside
      className={clsx(
        "sidebar sidebar-transition flex flex-col bg-primary-950 text-white shrink-0 no-print",
        collapsed ? "w-16" : "w-60"
      )}
      style={{ minHeight: "100vh" }}
    >
      {/* Brand */}
      <div className="flex items-center gap-3 px-4 py-5 border-b border-primary-800">
        <Activity className="w-7 h-7 text-blue-400 shrink-0" />
        {!collapsed && (
          <span className="text-lg font-bold tracking-tight whitespace-nowrap">
            MRI AI Platform
          </span>
        )}
      </div>

      {/* Navigation */}
      <nav className="flex-1 py-3 space-y-1 px-2">
        {visibleItems.map((item) => {
          const Icon = item.icon;
          const active = isActive(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              title={collapsed ? item.label : undefined}
              className={clsx(
                "flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors",
                active
                  ? "bg-primary-700 text-white"
                  : "text-gray-300 hover:bg-primary-800 hover:text-white"
              )}
            >
              <div className="relative shrink-0">
                <Icon className="w-5 h-5" />
                {item.badgeKey === "alerts" && unackedCount > 0 && (
                  <span className="absolute -top-1.5 -right-1.5 min-w-[16px] h-4 px-0.5 flex items-center justify-center rounded-full text-[10px] font-bold bg-red-500 text-white leading-none">
                    {unackedCount > 99 ? "99+" : unackedCount}
                  </span>
                )}
              </div>
              {!collapsed && (
                <span className="flex-1">{item.labelKey ? strings.nav[item.labelKey] : item.label}</span>
              )}
              {!collapsed && item.badgeKey === "alerts" && unackedCount > 0 && (
                <span className="ml-auto min-w-[20px] px-1.5 py-0.5 text-[10px] font-bold bg-red-500 text-white rounded-full text-center leading-none">
                  {unackedCount > 99 ? "99+" : unackedCount}
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      {/* Theme + locale toggles + user info / logout */}
      <div className="px-3 py-2 border-t border-primary-800 space-y-1">
        <button
          onClick={() => setLocale(locale === "ur" ? "en" : "ur")}
          aria-label={locale === "ur" ? "Switch to English" : "اردو میں تبدیل کریں"}
          title={collapsed ? (locale === "ur" ? "Switch to English" : "اردو میں تبدیل کریں") : undefined}
          className="flex items-center gap-2 w-full px-3 py-2 text-sm text-gray-400 hover:text-white hover:bg-primary-800 rounded-lg transition-colors"
        >
          <Languages className="w-4 h-4 shrink-0" />
          {!collapsed && <span>{locale === "ur" ? "English" : "اردو"}</span>}
        </button>
        <button
          onClick={toggleTheme}
          aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          title={collapsed ? (theme === "dark" ? "Switch to light mode" : "Switch to dark mode") : undefined}
          className="flex items-center gap-2 w-full px-3 py-2 text-sm text-gray-400 hover:text-white hover:bg-primary-800 rounded-lg transition-colors"
        >
          {theme === "dark" ? <Sun className="w-4 h-4 shrink-0" /> : <Moon className="w-4 h-4 shrink-0" />}
          {!collapsed && <span>{theme === "dark" ? "Light mode" : "Dark mode"}</span>}
        </button>
        {!collapsed && (
          <button
            onClick={() => {
              localStorage.removeItem("auth_token");
              localStorage.removeItem("user");
              window.location.href = "/login";
            }}
            className="flex items-center gap-2 w-full px-3 py-2 text-sm text-gray-400 hover:text-white hover:bg-primary-800 rounded-lg transition-colors"
          >
            <LogOut className="w-4 h-4" />
            <span>Sign Out</span>
          </button>
        )}
      </div>

      {/* Collapse toggle */}
      <button
        onClick={toggle}
        aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        className="flex items-center gap-3 px-5 py-4 border-t border-primary-800 text-gray-400 hover:text-white transition-colors"
      >
        {collapsed ? (
          <PanelLeftOpen className="w-5 h-5 rtl:scale-x-[-1]" />
        ) : (
          <>
            <PanelLeftClose className="w-5 h-5 rtl:scale-x-[-1]" />
            <span className="text-sm">Collapse</span>
          </>
        )}
      </button>
    </aside>
  );
}
