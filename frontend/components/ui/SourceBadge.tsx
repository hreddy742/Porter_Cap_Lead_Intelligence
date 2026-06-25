export function SignalTypeBadge({ signalType }: { signalType: string | null }) {
  const isPrime = signalType === "CONTRACT_AWARD";
  const isSub = signalType === "SUBCONTRACT_AWARD";

  if (!isPrime && !isSub) {
    return (
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          padding: "3px 7px",
          borderRadius: 4,
          fontSize: 10,
          fontWeight: 500,
          background: "rgba(113,113,122,0.10)",
          color: "#71717A",
        }}
      >
        Unknown
      </span>
    );
  }

  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        padding: "3px 7px",
        borderRadius: 4,
        fontSize: 10,
        fontWeight: 500,
        background: isPrime ? "rgba(22,163,74,0.10)" : "rgba(37,99,235,0.10)",
        color: isPrime ? "#16A34A" : "#2563EB",
      }}
    >
      {isPrime ? "↑ Prime" : "↓ Sub"}
    </span>
  );
}

export function SourceBadge({ source }: { source: string | null }) {
  if (!source) return <span style={{ color: "#A1A1AA", fontSize: 12 }}>—</span>;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        padding: "2px 6px",
        borderRadius: 4,
        fontSize: 10,
        fontWeight: 500,
        background: "rgba(113,113,122,0.10)",
        color: "#71717A",
      }}
    >
      {source}
    </span>
  );
}
