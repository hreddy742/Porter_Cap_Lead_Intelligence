"use client";

import { usePathname } from "next/navigation";

export default function TopBar() {
  const pathname = usePathname();

  const isDetail = pathname.startsWith("/leads/") && pathname.length > 7;
  const lastCrumb = isDetail ? "Detail" : "Leads";

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        background: "#FFFFFF",
        borderBottom: "0.5px solid #E4E4E7",
        padding: "0 24px",
        height: "52px",
        flexShrink: 0,
        zIndex: 8,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12, color: "#A1A1AA" }}>
        <span>Workspace</span>
        <span style={{ color: "#D4D4D8" }}>/</span>
        <span>Sales Ops</span>
        <span style={{ color: "#D4D4D8" }}>/</span>
        <span style={{ color: "#09090B", fontWeight: 500 }}>{lastCrumb}</span>
      </div>
    </div>
  );
}
