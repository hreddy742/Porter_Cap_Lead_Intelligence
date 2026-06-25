"use client";

import { usePathname } from "next/navigation";
import Link from "next/link";

export default function Sidebar() {
  const pathname = usePathname();
  const active = pathname.startsWith("/leads");

  return (
    <aside
      style={{
        width: 224,
        flexShrink: 0,
        background: "#09090B",
        color: "#FAFAFA",
        display: "flex",
        flexDirection: "column",
        height: "100vh",
        position: "sticky",
        top: 0,
        overflowY: "auto",
      }}
    >
      {/* Brand */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "18px 18px 16px" }}>
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
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.1 }}>Porter Capital</div>
          <div style={{ fontSize: 10, color: "rgba(250,250,250,0.42)", marginTop: 2 }}>Birmingham · AL</div>
        </div>
      </div>

      <div style={{ height: "0.5px", background: "rgba(250,250,250,0.10)", margin: "0 18px" }} />

      {/* Nav */}
      <div style={{ padding: "14px 12px 4px" }}>
        <nav style={{ display: "flex", flexDirection: "column", gap: 1 }}>
          <Link
            href="/leads"
            style={{
              display: "flex",
              alignItems: "center",
              gap: 9,
              padding: "7px 8px",
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
            <span style={{ flex: 1 }}>Leads</span>
          </Link>
        </nav>
      </div>

      {/* Bottom */}
      <div style={{ marginTop: "auto", padding: "12px 18px 16px" }}>
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
    </aside>
  );
}
