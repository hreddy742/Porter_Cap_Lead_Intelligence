interface KpiCardProps {
  label: string;
  value: string | number;
  borderClass: string;
  valueClass?: string;
  description?: string;
}

export function KpiCard({
  label,
  value,
  borderClass,
  valueClass = "text-slate-900",
  description,
}: KpiCardProps) {
  return (
    <div
      className={`bg-white rounded-lg border border-slate-200 shadow-sm p-4 border-l-[3px] ${borderClass}`}
    >
      <p className={`text-[26px] font-bold tabular-nums leading-none ${valueClass}`}>
        {value}
      </p>
      <p className="text-[10px] text-slate-500 font-semibold mt-2 uppercase tracking-widest">
        {label}
      </p>
      {description && (
        <p className="text-[11px] text-slate-400 mt-0.5">{description}</p>
      )}
    </div>
  );
}
