import { notFound } from "next/navigation";
import Link from "next/link";
import {
  fetchLead,
  type LeadDetail,
  type EvidenceItem,
  type Signal,
  type ReviewDecision,
  type ScoreDetail,
} from "@/lib/api";
import { TierBadge } from "@/components/ui/TierBadge";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { EmptyState } from "@/components/ui/EmptyState";
import {
  ChevronLeft,
  AlertTriangle,
  ExternalLink,
  Building2,
  ShieldCheck,
  FileText,
  Activity,
  ClipboardList,
} from "lucide-react";

// ── Formatters ─────────────────────────────────────────────────────────────────

function fmt(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString();
}

function fmtDate(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtDateShort(s: string | null | undefined): string {
  if (!s) return "—";
  return new Date(s).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function fmtMoney(s: string | null | undefined): string {
  if (!s) return "—";
  const n = parseFloat(s);
  if (isNaN(n)) return s;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
}

// ── Shared sub-layout components ────────────────────────────────────────────────

function SectionHeading({
  icon: Icon,
  title,
  count,
}: {
  icon: React.FC<{ className?: string }>;
  title: string;
  count?: number;
}) {
  return (
    <div className="flex items-center gap-2 mb-3">
      <Icon className="w-3.5 h-3.5 text-slate-400" />
      <h2 className="text-[10px] font-semibold uppercase tracking-widest text-slate-500">
        {title}
        {count != null && (
          <span className="ml-1.5 text-slate-400">({count})</span>
        )}
      </h2>
    </div>
  );
}

function InfoGrid({
  rows,
}: {
  rows: { label: string; value: string; mono?: boolean }[];
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white shadow-sm overflow-hidden divide-y divide-slate-100">
      {rows.map(({ label, value, mono }) => (
        <div
          key={label}
          className="flex items-baseline justify-between px-4 py-2.5 gap-4"
        >
          <span className="text-[12px] text-slate-500 shrink-0">{label}</span>
          <span
            className={`text-right break-all ${
              mono
                ? "text-[11px] font-mono text-slate-600"
                : "text-[13px] text-slate-900"
            }`}
          >
            {value}
          </span>
        </div>
      ))}
    </div>
  );
}

// ── Evidence timeline ────────────────────────────────────────────────────────────

function EvidenceTimeline({ evidence }: { evidence: EvidenceItem[] }) {
  if (evidence.length === 0) {
    return (
      <EmptyState
        title="No evidence items"
        description="No evidence has been extracted for this lead yet."
      />
    );
  }
  return (
    <div className="relative border-l-2 border-slate-200 ml-3 space-y-0">
      {evidence.map((ev) => (
        <div key={ev.id} className="relative pl-6 pb-5">
          {/* Timeline dot */}
          <div className="absolute -left-[5px] top-[5px] w-2.5 h-2.5 rounded-full bg-white border-2 border-slate-300" />

          <div className="rounded-lg border border-slate-200 bg-white shadow-sm p-4">
            <p className="text-[13px] font-medium text-slate-900 leading-snug">
              {ev.claim_supported}
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
              <span className="text-[11px] text-slate-500">
                Confidence:{" "}
                <span className="font-semibold text-slate-700">
                  {typeof ev.confidence_score === "number"
                    ? (ev.confidence_score * 100).toFixed(0) + "%"
                    : ev.confidence_score}
                </span>
              </span>
              <span className="text-[11px] text-slate-400">
                Captured: {fmtDate(ev.captured_at)}
              </span>
            </div>
            {ev.source_url && (
              <a
                href={ev.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-2 flex items-center gap-1 text-[11px] font-mono text-blue-600 hover:text-blue-700 break-all transition-colors"
              >
                <ExternalLink className="w-3 h-3 shrink-0" />
                {ev.source_url}
              </a>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Signal cards ───────────────────────────────────────────────────────────────

function SignalCard({ sig }: { sig: Signal }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white shadow-sm p-4">
      <div className="flex items-start justify-between gap-3">
        <p className="text-[13px] font-medium text-slate-900">
          {sig.signal_type}
        </p>
        <span className="shrink-0 inline-flex items-center rounded px-2 py-0.5 text-[10px] font-semibold bg-slate-100 text-slate-600 uppercase tracking-wide">
          {sig.signal_strength}
        </span>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5">
        <span className="text-[11px] text-slate-500">
          {fmtDateShort(sig.signal_date)}
        </span>
        {sig.award_amount && (
          <span className="text-[11px] text-slate-500">
            Award:{" "}
            <span className="font-semibold text-slate-700">
              {fmtMoney(sig.award_amount)}
            </span>
          </span>
        )}
      </div>
    </div>
  );
}

// ── Review history ─────────────────────────────────────────────────────────────

function ReviewCard({ rd }: { rd: ReviewDecision }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white shadow-sm p-4">
      <div className="flex items-center justify-between gap-3">
        <StatusBadge status={rd.action} />
        <span className="text-[11px] text-slate-400">
          {fmtDate(rd.decided_at)}
        </span>
      </div>
      <p className="mt-1.5 text-[11px] text-slate-500">
        Reviewer: <span className="text-slate-700">{rd.reviewer_id}</span>
      </p>
      {rd.note && (
        <p className="mt-1.5 text-[12px] text-slate-600 italic leading-snug">
          &ldquo;{rd.note}&rdquo;
        </p>
      )}
    </div>
  );
}

// ── Score breakdown ─────────────────────────────────────────────────────────────

function ScoreBreakdown({ score }: { score: ScoreDetail }) {
  const breakdown = score.component_breakdown as Record<string, number>;
  const entries = Object.entries(breakdown).filter(
    ([, v]) => typeof v === "number"
  );

  return (
    <div className="rounded-lg border border-slate-200 bg-white shadow-sm overflow-hidden">
      <div className="px-4 py-3 border-b border-slate-100 flex items-center justify-between">
        <p className="text-[11px] font-semibold uppercase tracking-widest text-slate-500">
          Score Breakdown
        </p>
        <span className="text-[22px] font-bold tabular-nums text-slate-900">
          {score.total_score}
        </span>
      </div>
      {entries.length > 0 && (
        <div className="divide-y divide-slate-100">
          {entries.map(([key, value]) => (
            <div
              key={key}
              className="flex items-center justify-between px-4 py-2"
            >
              <span className="text-[11px] text-slate-500 capitalize">
                {key.replace(/_/g, " ")}
              </span>
              <span className="text-[12px] font-semibold tabular-nums text-slate-800">
                {value}
              </span>
            </div>
          ))}
        </div>
      )}
      <div className="px-4 py-2 border-t border-slate-100 flex items-center justify-between">
        <span className="text-[10px] text-slate-400">Gate</span>
        <span
          className={`text-[11px] font-medium ${
            score.gate_result === "passed"
              ? "text-emerald-600"
              : "text-red-600"
          }`}
        >
          {score.gate_result}
        </span>
      </div>
      <div className="px-4 py-2 border-t border-slate-100">
        <span className="text-[10px] font-mono text-slate-400 break-all">
          config: {score.config_hash}
        </span>
      </div>
    </div>
  );
}

// ── Page ───────────────────────────────────────────────────────────────────────

export default async function LeadDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let lead: LeadDetail | null;
  try {
    lead = await fetchLead(id);
  } catch (err) {
    const msg = err instanceof Error ? err.message : "unknown error";
    return (
      <div className="px-6 py-6">
        <p className="text-sm text-red-600">API error: {msg}</p>
      </div>
    );
  }

  if (!lead) {
    notFound();
  }

  return (
    <>
      {/* Breadcrumb + company header */}
      <div className="px-6 py-5 border-b border-slate-200 bg-white shrink-0">
        {/* Breadcrumb */}
        <div className="flex items-center gap-1.5 mb-3">
          <Link
            href="/leads"
            className="flex items-center gap-1 text-[12px] text-slate-500 hover:text-slate-700 transition-colors"
          >
            <ChevronLeft className="w-3.5 h-3.5" />
            Lead Review
          </Link>
          <span className="text-slate-300 text-xs">/</span>
          <span className="text-[12px] text-slate-400 truncate max-w-xs">
            {lead.company_name}
          </span>
        </div>

        {/* Company header */}
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold text-slate-900 leading-tight">
              {lead.company_name}
            </h1>
            <p className="text-[11px] font-mono text-slate-400 mt-0.5">
              {lead.lead_id}
            </p>
          </div>
          <div className="flex items-center gap-3 shrink-0">
            {lead.tier && <TierBadge tier={lead.tier} />}
            {lead.score != null && (
              <div className="text-right">
                <p className="text-[28px] font-bold text-slate-900 leading-none tabular-nums">
                  {lead.score}
                </p>
                <p className="text-[10px] text-slate-400 uppercase tracking-widest mt-0.5">
                  score
                </p>
              </div>
            )}
            <StatusBadge status={lead.sales_status} />
          </div>
        </div>
      </div>

      {/* Research disclaimer */}
      <div className="px-6 py-3 border-b border-slate-100 bg-slate-50 flex items-start gap-2">
        <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0 mt-0.5" />
        <p className="text-[11px] text-slate-600 leading-snug">
          <span className="font-semibold text-slate-700">
            Research-ready. Not sales-ready.
          </span>{" "}
          {lead.contactability_note}
        </p>
      </div>

      {/* Main content */}
      <div className="px-6 py-6 space-y-8 max-w-5xl">
        {/* Company + Score two-column layout */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
          {/* Company info — wider column */}
          <div className="lg:col-span-2 space-y-3">
            <SectionHeading icon={Building2} title="Company" />
            <InfoGrid
              rows={[
                { label: "State", value: lead.company_state ?? "—" },
                { label: "NAICS", value: lead.company_naics ?? "—" },
                {
                  label: "Industry",
                  value:
                    lead.company_naics_description ??
                    lead.company_industry ??
                    "—",
                },
                {
                  label: "Business type",
                  value: lead.company_business_type ?? "—",
                },
                {
                  label: "Domain",
                  value: lead.company_domain ?? "—",
                  mono: true,
                },
                {
                  label: "A/R fit confidence",
                  value: lead.ar_fit_confidence ?? "—",
                },
                { label: "Gate result", value: lead.gate_result ?? "—" },
                ...(lead.gate_reason
                  ? [{ label: "Gate reason", value: lead.gate_reason }]
                  : []),
              ]}
            />
          </div>

          {/* Score + review — narrower column */}
          <div className="space-y-4">
            {lead.latest_score ? (
              <ScoreBreakdown score={lead.latest_score} />
            ) : (
              <div className="rounded-lg border border-slate-200 bg-white px-4 py-4">
                <p className="text-[11px] font-semibold uppercase tracking-widest text-slate-500 mb-2">
                  Score
                </p>
                <p className="text-sm text-slate-400">
                  No score detail available.
                </p>
              </div>
            )}
          </div>
        </div>

        {/* Why now */}
        {lead.why_now_summary && (
          <section>
            <SectionHeading icon={Activity} title="Why This Lead" />
            <div className="rounded-lg border border-blue-100 bg-blue-50 px-5 py-4">
              <p className="text-[13px] text-blue-900 leading-relaxed">
                {lead.why_now_summary}
              </p>
            </div>
          </section>
        )}

        {/* Contactability / SAM validation */}
        {lead.contactability && (
          <section>
            <SectionHeading
              icon={ShieldCheck}
              title="SAM Entity Validation"
            />
            <InfoGrid
              rows={[
                {
                  label: "Contactability status",
                  value: lead.contactability.contactability_status,
                },
                {
                  label: "Contactability score",
                  value: fmt(lead.contactability.contactability_score),
                },
                {
                  label: "SAM UEI",
                  value: lead.contactability.sam_uei ?? "—",
                  mono: true,
                },
                {
                  label: "SAM registration",
                  value:
                    lead.contactability.sam_registration_status ?? "—",
                },
                {
                  label: "Official website",
                  value: lead.contactability.official_website ?? "—",
                },
                {
                  label: "Phone (unverified)",
                  value: lead.contactability.phone ?? "—",
                },
                {
                  label: "Generic email (unverified)",
                  value: lead.contactability.generic_email ?? "—",
                },
                {
                  label: "Last checked",
                  value: fmtDate(lead.contactability.last_checked_at),
                },
              ]}
            />
            {lead.contactability.sam_note && (
              <p className="mt-2 text-[11px] text-slate-400 leading-snug">
                {lead.contactability.sam_note}
              </p>
            )}
          </section>
        )}

        {/* Evidence timeline */}
        <section>
          <SectionHeading
            icon={FileText}
            title="Evidence"
            count={lead.evidence.length}
          />
          <EvidenceTimeline evidence={lead.evidence} />
        </section>

        {/* Signals */}
        {lead.signals.length > 0 && (
          <section>
            <SectionHeading
              icon={Activity}
              title="Signals"
              count={lead.signals.length}
            />
            <div className="space-y-2">
              {lead.signals.map((sig) => (
                <SignalCard key={sig.id} sig={sig} />
              ))}
            </div>
          </section>
        )}

        {/* Review history */}
        <section>
          <SectionHeading
            icon={ClipboardList}
            title="Review History"
            count={lead.review_history.length}
          />
          {lead.review_history.length === 0 ? (
            <EmptyState
              title="No review decisions yet"
              description="This lead has not been reviewed. Human review is required before any sales action."
            />
          ) : (
            <div className="space-y-2">
              {lead.review_history.map((rd) => (
                <ReviewCard key={rd.id} rd={rd} />
              ))}
            </div>
          )}
        </section>
      </div>
    </>
  );
}
