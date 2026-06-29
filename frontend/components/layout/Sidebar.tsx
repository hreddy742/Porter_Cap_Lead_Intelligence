"use client";

import { useState, useEffect } from "react";
import { usePathname } from "next/navigation";
import Link from "next/link";

export default function Sidebar() {
  const pathname = usePathname();
  const active = pathname.startsWith("/leads");
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    const stored = localStorage.getItem("sidebar_collapsed");
    if (stored === "true") setCollapsed(true);
  }, []);

  function toggle() {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem("sidebar_collapsed", String(next));
  }

  return (
    <aside
      style={{
        width: collapsed ? 48 : 224,
        flexShrink: 0,
        background: "#09090B",
        color: "#FAFAFA",
        display: "flex",
        flexDirection: "column",
        height: "100vh",
        position: "sticky",
        top: 0,
        overflow: "hidden",
        transition: "width 0.2s ease",
      }}
    >
      {/* Brand */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: collapsed ? "18px 9px 16px" : "18px 18px 16px",
          justifyContent: collapsed ? "center" : "flex-start",
          flexShrink: 0,
        }}
      >
        <div
          style={{
            width: 30,
            height: 30,
            flexShrink: 0,
            borderRadius: 7,
            background: "#FAFAFA",
            color: "#09090B",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 12,
            fontWeight: 700,
            letterSpacing: "0.02em",
          }}
        >
          PC
        </div>
        {!collapsed && (
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.1, whiteSpace: "nowrap" }}>
              Porter Capital
            </div>
            <div style={{ fontSize: 10, color: "rgba(250,250,250,0.42)", marginTop: 2 }}>
              Birmingham · AL
            </div>
          </div>
        )}
      </div>

      <div style={{ height: "0.5px", background: "rgba(250,250,250,0.10)", margin: "0 10px", flexShrink: 0 }} />

      {/* Nav */}
      <div style={{ padding: collapsed ? "14px 8px 4px" : "14px 12px 4px", flexShrink: 0 }}>
        <nav style={{ display: "flex", flexDirection: "column", gap: 1 }}>
          <Link
            href="/leads"
            title={collapsed ? "Leads" : undefined}
            style={{
              display: "flex",
              alignItems: "center",
              gap: collapsed ? 0 : 9,
              padding: collapsed ? "7px 0" : "7px 8px",
              justifyContent: collapsed ? "center" : "flex-start",
              borderRadius: 6,
              fontSize: 12,
              fontWeight: 500,
              cursor: "pointer",
              textDecoration: "none",
              background: active ? "#FAFAFA" : "transparent",
              color: active ? "#09090B" : "rgba(250,250,250,0.55)",
              transition: "background 0.1s",
            }}
          >
            <span
              style={{
                width: 5,
                height: 5,
                borderRadius: "50%",
                background: active ? "#09090B" : "rgba(250,250,250,0.30)",
                flexShrink: 0,
              }}
            />
            {!collapsed && <span style={{ flex: 1 }}>Leads</span>}
          </Link>
        </nav>
      </div>

      {/* Spacer */}
      <div style={{ flex: 1 }} />

      {/* Collapse toggle */}
      <div
        style={{
          padding: collapsed ? "8px 0" : "8px 12px",
          display: "flex",
          justifyContent: collapsed ? "center" : "flex-end",
          flexShrink: 0,
        }}
      >
        <button
          onClick={toggle}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          style={{
            width: 26,
            height: 26,
            borderRadius: 6,
            border: "0.5px solid rgba(250,250,250,0.15)",
            background: "rgba(250,250,250,0.07)",
            color: "rgba(250,250,250,0.45)",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 11,
            outline: "none",
            transition: "background 0.1s",
            flexShrink: 0,
          }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLButtonElement).style.background = "rgba(250,250,250,0.14)";
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLButtonElement).style.background = "rgba(250,250,250,0.07)";
          }}
        >
          {collapsed ? "→" : "←"}
        </button>
      </div>

      {/* Bottom */}
      {!collapsed && (
        <div style={{ padding: "4px 18px 16px", flexShrink: 0 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 7,
              fontSize: 10,
              color: "rgba(250,250,250,0.42)",
              marginBottom: 12,
            }}
          >
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: "50%",
                background: "#16A34A",
                flexShrink: 0,
              }}
            />
            Feed synced daily ·{" "}
            <span style={{ fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)" }}>
              SAM.gov
            </span>
          </div>
          <div style={{ height: "0.5px", background: "rgba(250,250,250,0.10)", marginBottom: 12 }} />
          <div style={{ display: "flex", alignItems: "center", gap: 10, paddingTop: 2 }}>
            <div
              style={{
                width: 28,
                height: 28,
                flexShrink: 0,
                borderRadius: "50%",
                background: "rgba(250,250,250,0.10)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 11,
                fontWeight: 600,
                color: "#FAFAFA",
              }}
            >
              HR
            </div>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ fontSize: 12, fontWeight: 500, lineHeight: 1.1 }}>Harsha Reddy</div>
              <div style={{ fontSize: 10, color: "rgba(250,250,250,0.42)", marginTop: 2 }}>
                Lead Engineer
              </div>
            </div>
            <div
              style={{
                fontSize: 9,
                color: "rgba(250,250,250,0.30)",
                marginTop: 8,
                alignSelf: "flex-start",
              }}
            >
              Research only
            </div>
          </div>
        </div>
      )}

      {/* Collapsed: show avatar only */}
      {collapsed && (
        <div
          style={{
            display: "flex",
            justifyContent: "center",
            padding: "0 0 16px",
            flexShrink: 0,
          }}
        >
          <div
            title="Harsha Reddy"
            style={{
              width: 28,
              height: 28,
              borderRadius: "50%",
              background: "rgba(250,250,250,0.10)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 11,
              fontWeight: 600,
              color: "#FAFAFA",
            }}
          >
            HR
          </div>
        </div>
      )}
    </aside>
  );
}
