import { fetchSummary } from "@/lib/api";
import { PageHeader } from "@/components/ui/PageHeader";
import { KpiCard } from "@/components/ui/KpiCard";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { ErrorState } from "@/components/ui/ErrorState";
import { Activity, Info } from "lucide-react";
import Link from "next/link";

function fmt(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString();
}

function fmtDate(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default async function DashboardPage() {
  let summary;
  let fetchError: string | null = null;

  try {
    summary = await fetchSummary();
  } catch (err) {
    fetchError =
      err instanceof Error ? err.message : "Could not reach the API.";
  }

  return (
    <>
      <PageHeader
        title="Dashboard"
        description="Lead pipeline overview — research-ready view"
        actions={
          <Link
            href="/leads"
            className="text-[12px] font-medium text-blue-600 hover:text-blue-700 transition-colors"
          >
            View all leads →
          </Link>
        }
      />

      <div className="px-6 py-6 space-y-8 max-w-5xl">
        {fetchError || !summary ? (
          <ErrorState message={fetchError ?? "Unknown error."} />
        ) : (
          <>
            {/* Tier KPI cards */}
            <section>
              <p className="text-[10px] font-semibold uppercase tracking-widest text-slate-400 mb-3">
                Lead Tiers
              </p>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <KpiCard
                  label="Hot"
                  value={fmt(summary.tier_counts.hot)}
                  borderClass="border-l-red-500"
                  valueClass="text-red-600"
                  description="High-confidence A/R fit"
                />
                <KpiCard
                  label="Warm"
                  value={fmt(summary.tier_counts.warm)}
                  borderClass="border-l-amber-500"
                  valueClass="text-amber-600"
                  description="Moderate signal strength"
                />
                <KpiCard
                  label="Cold"
                  value={fmt(summary.tier_counts.cold)}
                  borderClass="border-l-sky-400"
                  valueClass="text-sky-600"
                  description="Early-stage activity"
                />
                <KpiCard
                  label="Archive"
                  value={fmt(summary.tier_counts.archive)}
                  borderClass="border-l-slate-300"
                  valueClass="text-slate-400"
                  description="Low signal or gated"
                />
              </div>

              <div className="mt-3 flex items-center gap-6">
                <p className="text-[12px] text-slate-500">
                  Total:{" "}
                  <span className="font-semibold text-slate-900 tabular-nums">
                    {fmt(summary.tier_counts.total)}
                  </span>
                </p>
                <p className="text-[12px] text-slate-500">
                  Pending review:{" "}
                  <span className="font-semibold text-slate-900 tabular-nums">
                    {fmt(summary.tier_counts.pending_review)}
                  </span>
                </p>
              </div>
            </section>

            {/* Latest pipeline run */}
            <section>
              <p className="text-[10px] font-semibold uppercase tracking-widest text-slate-400 mb-3">
                Latest Pipeline Run
              </p>

              {!summary.run_context.has_runs ? (
                <div className="rounded-lg border border-slate-200 bg-white px-5 py-8 text-center">
                  <p className="text-sm text-slate-500">
                    No pipeline runs recorded yet.
                  </p>
                  <p className="text-xs text-slate-400 mt-1">
                    Run the pipeline to see run context here.
                  </p>
                </div>
              ) : (
                <div className="rounded-lg border border-slate-200 bg-white shadow-sm overflow-hidden">
                  {/* Run header row */}
                  <div className="px-5 py-3 border-b border-slate-100 flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2 min-w-0">
                      <Activity className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                      <span className="text-[11px] font-mono text-slate-500 truncate">
                        {summary.run_context.pipeline_run_id ?? "—"}
                      </span>
                    </div>
                    {summary.run_context.status && (
                      <StatusBadge status={summary.run_context.status} />
                    )}
                  </div>

                  {/* Stats grid */}
                  <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 divide-x divide-y divide-slate-100">
                    {[
                      {
                        label: "Started",
                        value: fmtDate(summary.run_context.started_at),
                      },
                      {
                        label: "Finished",
                        value: fmtDate(summary.run_context.finished_at),
                      },
                      {
                        label: "Leads scored",
                        value: fmt(summary.run_context.leads_scored),
                      },
                      {
                        label: "Records fetched",
                        value: fmt(summary.run_context.records_fetched),
                      },
                      {
                        label: "Raw events",
                        value: fmt(summary.run_context.raw_events_stored),
                      },
                    ].map(({ label, value }) => (
                      <div key={label} className="px-5 py-3.5">
                        <p className="text-[10px] text-slate-400 uppercase tracking-widest font-semibold mb-1">
                          {label}
                        </p>
                        <p className="text-[13px] font-semibold text-slate-900 tabular-nums">
                          {value}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {summary.run_context.errors.length > 0 && (
                <div className="mt-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3">
                  <p className="text-[11px] font-semibold text-red-700 mb-1">
                    {summary.run_context.errors.length} error
                    {summary.run_context.errors.length !== 1 ? "s" : ""} in
                    this run
                  </p>
                  {summary.run_context.errors.map((e, i) => (
                    <p key={i} className="text-[11px] font-mono text-red-600">
                      {e.error_text ?? "unknown error"}
                    </p>
                  ))}
                </div>
              )}
            </section>

            {/* What this system does */}
            <section>
              <div className="rounded-lg border border-slate-200 bg-white px-5 py-4 flex gap-3">
                <Info className="w-4 h-4 text-slate-300 mt-0.5 shrink-0" />
                <div className="space-y-1.5">
                  <p className="text-[12px] font-semibold text-slate-700">
                    What this pipeline does
                  </p>
                  <p className="text-[12px] text-slate-500 leading-relaxed">
                    Discovers companies with active government contract activity
                    that may need accounts-receivable financing. Scores and
                    classifies them as leads. Routes the best ones to human
                    review before any Salesforce push or outreach.
                  </p>
                  <p className="text-[11px] text-slate-400 leading-relaxed">
                    SAM entity validation ≠ verified phone or email.
                    Government evidence ≠ proven factoring need.
                    Contactability ≠ sales-ready. Human review is required.
                  </p>
                </div>
              </div>
            </section>
          </>
        )}
      </div>
    </>
  );
}
