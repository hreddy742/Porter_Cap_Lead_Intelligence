const TIER_CFG: Record<string, { label: string; color: string; bg: string }> = {
  hot: { label: "Hot", color: "#DC2626", bg: "rgba(220,38,38,0.10)" },
  warm: { label: "Warm", color: "#D97706", bg: "rgba(217,119,6,0.10)" },
  cold: { label: "Cold", color: "#2563EB", bg: "rgba(37,99,235,0.10)" },
  archive: { label: "Archive", color: "#71717A", bg: "rgba(113,113,122,0.10)" },
};

export function TierBadge({ tier }: { tier: string | null }) {
  if (!tier) return <span style={{ color: "#A1A1AA", fontSize: 12 }}>—</span>;
  const cfg = TIER_CFG[tier.toLowerCase()] ?? {
    label: tier,
    color: "#71717A",
    bg: "rgba(113,113,122,0.10)",
  };
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "3px 9px",
        borderRadius: 999,
        fontSize: 10,
        fontWeight: 500,
        background: cfg.bg,
        color: cfg.color,
      }}
    >
      <span
        style={{
          width: 5,
          height: 5,
          borderRadius: "50%",
          background: cfg.color,
          flexShrink: 0,
        }}
      />
      {cfg.label}
    </span>
  );
}

export function tierColor(tier: string | null): string {
  const cfg = tier ? TIER_CFG[tier.toLowerCase()] : null;
  return cfg?.color ?? "#71717A";
}
