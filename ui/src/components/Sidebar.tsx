"use client";

import { useState, useEffect } from "react";
import { usePathname } from "next/navigation";
import Link from "next/link";
import clsx from "clsx";
import {
  PanelLeftClose,
  PanelLeftOpen,
  LogOut,
  Sun,
  Moon,
  Languages,
} from "lucide-react";
import { CortexMark } from "@/components/ui/CortexMark";
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
        "sidebar sidebar-transition flex flex-col h-full min-h-0 text-white shrink-0 no-print relative",
        "bg-[#0a0f1e]/80 dark:bg-[#070b16]/70 backdrop-blur-xl border-r border-white/10",
        collapsed ? "w-16" : "w-60"
      )}
    >
      {/* Brand */}
      <div className="group flex items-center gap-3 px-4 py-5 border-b border-white/10">
        <div className="grid place-items-center w-9 h-9 rounded-xl bg-accent/10 ring-1 ring-accent/30 shadow-glow-sm shrink-0 transition-transform duration-200 group-hover:scale-105">
          <CortexMark className="w-6 h-6 transition-transform duration-700 ease-out group-hover:rotate-90" />
        </div>
        {!collapsed && (
          <span className="text-lg tracking-tight whitespace-nowrap">
            <span className="font-extrabold text-gradient">CORTEX</span>
            <span className="font-medium text-white/85"> Radiology</span>
          </span>
        )}
      </div>

      {/* Navigation */}
      <nav className="stagger flex-1 min-h-0 overflow-y-auto py-3 space-y-1 px-2">
        {visibleItems.map((item) => {
          const Icon = item.icon;
          const active = isActive(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              title={collapsed ? item.label : undefined}
              className={clsx(
                "group relative flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all duration-200",
                active
                  ? "bg-white/10 text-white shadow-glow ring-1 ring-white/15 before:absolute before:inset-y-1.5 before:-left-2 before:w-1 before:rounded-full before:bg-accent-gradient before:animate-[scale-in_320ms_cubic-bezier(0.22,1,0.36,1)]"
                  : "text-gray-400 hover:bg-white/5 hover:text-white hover:translate-x-0.5"
              )}
            >
              <div className="relative shrink-0 transition-transform duration-200 group-hover:scale-110 group-active:scale-95">
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
      <div className="px-3 py-2 border-t border-white/10 space-y-1">
        <button
          onClick={() => setLocale(locale === "ur" ? "en" : "ur")}
          aria-label={locale === "ur" ? "Switch to English" : "اردو میں تبدیل کریں"}
          title={collapsed ? (locale === "ur" ? "Switch to English" : "اردو میں تبدیل کریں") : undefined}
          className="press flex items-center gap-2 w-full px-3 py-2 text-sm text-gray-400 hover:text-white hover:bg-white/5 rounded-lg transition-colors"
        >
          <Languages className="w-4 h-4 shrink-0" />
          {!collapsed && <span>{locale === "ur" ? "English" : "اردو"}</span>}
        </button>
        <button
          onClick={toggleTheme}
          aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          title={collapsed ? (theme === "dark" ? "Switch to light mode" : "Switch to dark mode") : undefined}
          className="press flex items-center gap-2 w-full px-3 py-2 text-sm text-gray-400 hover:text-white hover:bg-white/5 rounded-lg transition-colors"
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
            className="press flex items-center gap-2 w-full px-3 py-2 text-sm text-gray-400 hover:text-white hover:bg-white/5 rounded-lg transition-colors"
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
        className="flex items-center gap-3 px-5 py-4 border-t border-white/10 text-gray-400 hover:text-white transition-colors"
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
