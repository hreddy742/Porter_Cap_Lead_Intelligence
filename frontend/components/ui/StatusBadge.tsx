const STATUS_CFG: Record<string, string> = {
  pending_review:
    "bg-amber-50 text-amber-700 ring-1 ring-inset ring-amber-600/20",
  approved:
    "bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-600/20",
  rejected: "bg-red-50 text-red-600 ring-1 ring-inset ring-red-600/20",
  under_review:
    "bg-blue-50 text-blue-700 ring-1 ring-inset ring-blue-600/20",
  suppressed:
    "bg-slate-100 text-slate-500 ring-1 ring-inset ring-slate-400/20",
  new: "bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-600/20",
};

function formatStatus(s: string): string {
  return s
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function StatusBadge({ status }: { status: string }) {
  const cls =
    STATUS_CFG[status] ??
    "bg-slate-100 text-slate-600 ring-1 ring-inset ring-slate-300";
  return (
    <span
      className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${cls}`}
    >
      {formatStatus(status)}
    </span>
  );
}
