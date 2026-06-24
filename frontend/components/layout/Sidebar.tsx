"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LayoutDashboard, Users } from "lucide-react";
import type { LucideIcon } from "lucide-react";

const NAV: { href: string; label: string; Icon: LucideIcon; exact?: boolean }[] = [
  { href: "/", label: "Dashboard", Icon: LayoutDashboard, exact: true },
  { href: "/leads", label: "Lead Review", Icon: Users },
];

export default function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="w-[220px] shrink-0 flex flex-col bg-slate-950 border-r border-slate-800/60">
      {/* Brand */}
      <div className="px-4 pt-5 pb-4 border-b border-slate-800/60">
        <div className="flex items-center gap-2.5">
          <div className="w-7 h-7 rounded-md bg-blue-600 flex items-center justify-center shrink-0">
            <span className="text-[11px] font-bold text-white tracking-tight">PC</span>
          </div>
          <div className="min-w-0">
            <p className="text-[10px] font-semibold tracking-[0.14em] text-slate-500 uppercase leading-none">
              Porter Capital
            </p>
            <p className="text-[13px] font-semibold text-white leading-snug mt-0.5">
              Lead Intelligence
            </p>
          </div>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-2.5 py-3 space-y-0.5">
        {NAV.map(({ href, label, Icon, exact }) => {
          const active = exact ? pathname === href : pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              className={`flex items-center gap-2.5 px-3 py-[7px] rounded-md text-[13px] transition-colors ${
                active
                  ? "bg-blue-600/[0.13] text-blue-400 font-medium"
                  : "text-slate-400 hover:bg-slate-800/70 hover:text-slate-100"
              }`}
            >
              <Icon
                className={`w-[15px] h-[15px] shrink-0 ${
                  active ? "text-blue-400" : "text-slate-600"
                }`}
              />
              {label}
            </Link>
          );
        })}
      </nav>

      {/* Footer */}
      <div className="px-4 py-3.5 border-t border-slate-800/60">
        <p className="text-[10px] text-slate-600 leading-relaxed">
          Internal research tool.
          <br />
          Human review required before outreach.
        </p>
      </div>
    </aside>
  );
}
