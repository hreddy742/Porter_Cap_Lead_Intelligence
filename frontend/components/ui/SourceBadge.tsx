const SIGNAL_TYPE_CFG: Record<string, { label: string; cls: string }> = {
  CONTRACT_AWARD: {
    label: "Prime Contract",
    cls: "bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-600/20",
  },
  SUBCONTRACT_AWARD: {
    label: "Subcontract",
    cls: "bg-blue-50 text-blue-700 ring-1 ring-inset ring-blue-600/20",
  },
};

export function SignalTypeBadge({ signalType }: { signalType: string | null }) {
  const cfg = signalType ? SIGNAL_TYPE_CFG[signalType] : undefined;
  const label = cfg?.label ?? "Unknown";
  const cls = cfg?.cls ?? "bg-slate-100 text-slate-500 ring-1 ring-inset ring-slate-300";
  return (
    <span
      className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${cls}`}
    >
      {label}
    </span>
  );
}

const SOURCE_CFG: Record<string, { label: string; cls: string }> = {
  usaspending: {
    label: "USASpending",
    cls: "bg-violet-50 text-violet-700 ring-1 ring-inset ring-violet-600/20",
  },
  usa_spending: {
    label: "USASpending",
    cls: "bg-violet-50 text-violet-700 ring-1 ring-inset ring-violet-600/20",
  },
  sam_gov: {
    label: "SAM.gov",
    cls: "bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-600/20",
  },
  sam: {
    label: "SAM.gov",
    cls: "bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-600/20",
  },
  apollo: {
    label: "Apollo",
    cls: "bg-orange-50 text-orange-700 ring-1 ring-inset ring-orange-600/20",
  },
};

export function SourceBadge({ source }: { source: string | null }) {
  if (!source) return <span className="text-slate-300 text-xs">—</span>;
  const key = source.toLowerCase().replace(/[^a-z_]/g, "_");
  const cfg = SOURCE_CFG[key];
  const label = cfg?.label ?? source;
  const cls =
    cfg?.cls ??
    "bg-slate-100 text-slate-600 ring-1 ring-inset ring-slate-300";
  return (
    <span
      className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${cls}`}
    >
      {label}
    </span>
  );
}
