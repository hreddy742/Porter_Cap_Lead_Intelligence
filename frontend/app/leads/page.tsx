import { fetchLeads, fetchSummary } from "@/lib/api";
import LeadsContainer from "@/components/leads/LeadsContainer";

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
}

function StatCard({ label, value, sub, borderColor }: StatCardProps) {
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

const VALID_PER_PAGE = [25, 50, 100] as const;

export default async function LeadsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string>>;
}) {
  const sp = await searchParams;

  // Parse pagination params
  const rawPage = parseInt(sp.page ?? "1", 10);
  const page = isNaN(rawPage) || rawPage < 1 ? 1 : rawPage;
  const rawPerPage = parseInt(sp.per_page ?? "50", 10);
  const perPage: 25 | 50 | 100 = VALID_PER_PAGE.includes(rawPerPage as 25 | 50 | 100)
    ? (rawPerPage as 25 | 50 | 100)
    : 50;
  const offset = (page - 1) * perPage;

  // Parse filter params
  const activeTier = sp.tier ?? "";
  const activeSignal = sp.signal ?? "";
  const activeStatus = sp.status ?? "";
  const includeExcluded = sp.include_excluded === "true";
  const sortBy = sp.sort_by ?? "score_desc";

  const apiParams: Record<string, string> = {
    limit: String(perPage),
    offset: String(offset),
    sort_by: sortBy,
    include_excluded: String(includeExcluded),
  };
  if (activeTier) apiParams.tier = activeTier;
  if (activeSignal) apiParams.signal_type = activeSignal;
  if (activeStatus) apiParams.sales_status = activeStatus;

  const [summaryResult, dataResult] = await Promise.allSettled([
    fetchSummary(),
    fetchLeads(apiParams),
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
  const finishedAt = summary?.run_context?.finished_at ?? null;

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      {/* Page header */}
      <div style={{ padding: "24px 24px 0", flexShrink: 0 }}>
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
            {total.toLocaleString()} leads · last run {lastRunAgo(finishedAt)}
          </span>
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
            label="Total Leads"
            value={total.toLocaleString()}
            sub={includeExcluded ? "all sectors" : "ICP sectors only"}
            borderColor="#2563EB"
          />
        </div>
      )}

      {/* Filter bar + table */}
      <div style={{ flex: 1, minHeight: 0, marginTop: 18 }}>
        <LeadsContainer
          leads={leads}
          total={total}
          limit={perPage}
          offset={offset}
          fetchError={fetchError}
          activeTier={activeTier}
          activeSignal={activeSignal}
          activeStatus={activeStatus}
          includeExcluded={includeExcluded}
          sortBy={sortBy}
        />
      </div>
    </div>
  );
}
