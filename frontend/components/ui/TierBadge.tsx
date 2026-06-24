const TIER_CFG: Record<string, { label: string; cls: string }> = {
  hot: {
    label: "Hot",
    cls: "bg-red-50 text-red-700 ring-1 ring-inset ring-red-600/20",
  },
  warm: {
    label: "Warm",
    cls: "bg-amber-50 text-amber-700 ring-1 ring-inset ring-amber-600/20",
  },
  cold: {
    label: "Cold",
    cls: "bg-sky-50 text-sky-700 ring-1 ring-inset ring-sky-600/20",
  },
  archive: {
    label: "Archive",
    cls: "bg-slate-100 text-slate-500 ring-1 ring-inset ring-slate-400/20",
  },
};

export function TierBadge({ tier }: { tier: string | null }) {
  if (!tier) return <span className="text-slate-300 text-xs">—</span>;
  const cfg = TIER_CFG[tier];
  if (!cfg) {
    return (
      <span className="inline-flex items-center rounded px-2 py-0.5 text-xs font-medium bg-slate-100 text-slate-600">
        {tier}
      </span>
    );
  }
  return (
    <span
      className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${cfg.cls}`}
    >
      {cfg.label}
    </span>
  );
}
