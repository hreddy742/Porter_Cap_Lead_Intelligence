import { Inbox } from "lucide-react";

interface EmptyStateProps {
  title: string;
  description?: string;
}

export function EmptyState({ title, description }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-20 px-6 text-center">
      <div className="w-10 h-10 rounded-full bg-slate-100 flex items-center justify-center mb-3">
        <Inbox className="w-5 h-5 text-slate-400" />
      </div>
      <p className="text-sm font-medium text-slate-700">{title}</p>
      {description && (
        <p className="mt-1 text-xs text-slate-500 max-w-xs leading-relaxed">
          {description}
        </p>
      )}
    </div>
  );
}
