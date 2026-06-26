import { fetchLeads, fetchSummary } from "@/lib/api";
import LeadsContainer from "@/components/leads/LeadsContainer";

function fmtMoney(total: number): string {
  if (total >= 1_000_000_000) return `$${(total / 1_000_000_000).toFixed(2)}B`;
  if (total >= 1_000_000) return `$${(total / 1_000_000).toFixed(2)}M`;
  if (total >= 1_000) return `$${(total / 1_000).toFixed(0)}K`;
  return `$${total.toFixed(0)}`;
}

function lastRunAgo(finishedAt: string | null | undefined): string {
  if (!finishedAt) return "—";
  const ms = Date.now() - new Date(finishedAt).getTime();
  const min = Math.floor(ms / 60000);
  if (min < 1) return "< 1 min ago";
  if (min < 60) return `${min} min ago`;
  const hr = Math.floor(min / 60);
  return `${hr}h ago`;
}

function currentQuarter(): string {
  const now = new Date();
  const q = Math.ceil((now.getMonth() + 1) / 3);
  return `Q${q}`;
}

function fmtDate(): string {
  return new Date().toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

interface StatCardProps {
  label: string;
  value: string;
  sub: string;
  borderColor: string;
  deltaLabel?: string;
  deltaUp?: boolean;
}

function StatCard({ label, value, sub, borderColor, deltaLabel, deltaUp }: StatCardProps) {
  return (
    <div
      style={{
        background: "#FFFFFF",
        border: "0.5px solid #E4E4E7",
        borderLeft: `3px solid ${borderColor}`,
        borderRadius: 8,
        padding: "16px 18px",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div
          style={{
            fontSize: 10,
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            color: "#71717A",
          }}
        >
          {label}
        </div>
        {deltaLabel && (
          <div
            style={{
              fontFamily: "var(--font-data, Inter, sans-serif)",
              fontSize: 10,
              fontWeight: 600,
              fontVariantNumeric: "tabular-nums",
              padding: "1px 6px",
              borderRadius: 999,
              background: deltaUp ? "rgba(22,163,74,0.10)" : "rgba(220,38,38,0.10)",
              color: deltaUp ? "#16A34A" : "#DC2626",
            }}
          >
            {deltaLabel}
          </div>
        )}
      </div>
      <div
        style={{
          fontFamily: "var(--font-data, Inter, sans-serif)",
          fontSize: 24,
          fontWeight: 500,
          fontVariantNumeric: "tabular-nums",
          lineHeight: 1,
          marginTop: 12,
          color: "#09090B",
        }}
      >
        {value}
      </div>
      <div style={{ fontSize: 10, color: "#A1A1AA", marginTop: 7 }}>{sub}</div>
    </div>
  );
}

export default async function LeadsPage() {
  const [summaryResult, dataResult] = await Promise.allSettled([
    fetchSummary(),
    fetchLeads({ limit: "50000", sort_by: "score_desc", include_excluded: "true" }),
  ]);

  const summary = summaryResult.status === "fulfilled" ? summaryResult.value : null;
  const data = dataResult.status === "fulfilled" ? dataResult.value : null;
  const fetchError =
    dataResult.status === "rejected"
      ? dataResult.reason instanceof Error
        ? dataResult.reason.message
        : "Could not reach the API."
      : undefined;

  const leads = data?.items ?? [];
  const total = data?.total ?? 0;

  const pipelineTotal = leads
    .filter((l) => !l.sector_excluded)
    .reduce((sum, l) => sum + (l.max_award_amount ? parseFloat(l.max_award_amount) : 0), 0);

  const pipelineCount = leads.filter((l) => !l.sector_excluded).length;
  const finishedAt = summary?.run_context?.finished_at ?? null;

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      {/* Page header */}
      <div
        style={{
          padding: "24px 24px 0",
          flexShrink: 0,
        }}
      >
        <div>
          <h1
            style={{
              margin: 0,
              fontSize: 16,
              fontWeight: 500,
              letterSpacing: "-0.02em",
              color: "#09090B",
            }}
          >
            Active Leads
          </h1>
          <div
            style={{
              fontSize: 11,
              color: "#71717A",
              marginTop: 5,
              fontVariantNumeric: "tabular-nums",
            }}
          >
            {currentQuarter()} · {fmtDate()} ·{" "}
            <span style={{ color: "#A1A1AA" }}>
              {pipelineCount.toLocaleString()} in pipeline · last run {lastRunAgo(finishedAt)}
            </span>
          </div>
        </div>
      </div>

      {/* Stats row */}
      {summary && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(4, 1fr)",
            gap: 12,
            padding: "18px 24px 0",
            flexShrink: 0,
          }}
        >
          <StatCard
            label="Hot Leads"
            value={summary.tier_counts.hot.toLocaleString()}
            sub="immediate action"
            borderColor="#DC2626"
          />
          <StatCard
            label="Warm"
            value={summary.tier_counts.warm.toLocaleString()}
            sub="qualify this week"
            borderColor="#D97706"
          />
          <StatCard
            label="Cold"
            value={summary.tier_counts.cold.toLocaleString()}
            sub="monitor"
            borderColor="#2563EB"
          />
          <StatCard
            label="Pipeline Value"
            value={fmtMoney(pipelineTotal)}
            sub="ICP leads only"
            borderColor="#2563EB"
          />
        </div>
      )}

      {/* Filter bar + table */}
      <div style={{ flex: 1, minHeight: 0, marginTop: 18 }}>
        <LeadsContainer leads={leads} total={total} fetchError={fetchError} />
      </div>
    </div>
  );
}
