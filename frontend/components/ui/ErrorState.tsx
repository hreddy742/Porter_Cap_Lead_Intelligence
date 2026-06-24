import { AlertCircle } from "lucide-react";

interface ErrorStateProps {
  title?: string;
  message: string;
}

export function ErrorState({
  title = "API Unavailable",
  message,
}: ErrorStateProps) {
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 p-4 flex items-start gap-3">
      <AlertCircle className="w-4 h-4 text-red-500 shrink-0 mt-0.5" />
      <div className="min-w-0">
        <p className="text-sm font-semibold text-red-700">{title}</p>
        <p className="text-xs text-red-600 mt-0.5 break-all">{message}</p>
        <p className="text-xs text-slate-500 mt-2">
          Start the backend:{" "}
          <code className="font-mono bg-red-100 px-1 py-0.5 rounded text-red-700">
            uvicorn app.api.main:app --reload
          </code>
        </p>
      </div>
    </div>
  );
}
