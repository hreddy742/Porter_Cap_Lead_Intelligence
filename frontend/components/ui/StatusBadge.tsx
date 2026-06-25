const STATUS_CFG: Record<string, { color: string; bg: string }> = {
  new: { color: "#2563EB", bg: "rgba(37,99,235,0.10)" },
  research: { color: "#2563EB", bg: "rgba(37,99,235,0.10)" },
  pending_review: { color: "#D97706", bg: "rgba(217,119,6,0.10)" },
  under_review: { color: "#D97706", bg: "rgba(217,119,6,0.10)" },
  approved: { color: "#16A34A", bg: "rgba(22,163,74,0.10)" },
  rejected: { color: "#A1A1AA", bg: "rgba(161,161,170,0.10)" },
  suppressed: { color: "#A1A1AA", bg: "rgba(161,161,170,0.10)" },
  contacted: { color: "#71717A", bg: "rgba(113,113,122,0.10)" },
  qualified: { color: "#16A34A", bg: "rgba(22,163,74,0.10)" },
};

function formatStatus(s: string): string {
  if (s === "pending_review") return "Reviewing";
  if (s === "under_review") return "Reviewing";
  if (s === "research") return "New";
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function StatusBadge({ status }: { status: string }) {
  const cfg = STATUS_CFG[status.toLowerCase()] ?? {
    color: "#71717A",
    bg: "rgba(113,113,122,0.10)",
  };
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        padding: "3px 9px",
        borderRadius: 999,
        fontSize: 10,
        fontWeight: 500,
        background: cfg.bg,
        color: cfg.color,
      }}
    >
      {formatStatus(status)}
    </span>
  );
}
