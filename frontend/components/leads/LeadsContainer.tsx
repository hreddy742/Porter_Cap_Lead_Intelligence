"use client";

import { useState, useMemo } from "react";
import Link from "next/link";
import type { LeadListItem } from "@/lib/api";
import { TierBadge } from "@/components/ui/TierBadge";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { SourceBadge, SignalTypeBadge } from "@/components/ui/SourceBadge";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorState } from "@/components/ui/ErrorState";
import { Search, X } from "lucide-react";

type Tab = "all" | "latest_run" | "pending" | "recent";

const TABS: { key: Tab; label: string }[] = [
  { key: "all", label: "All Active" },
  { key: "latest_run", label: "Latest Run" },
  { key: "pending", label: "Pending Review" },
  { key: "recent", label: "Recently Updated" },
];

const TIERS = ["hot", "warm", "cold", "archive"] as const;

const SCORE_COLOR: Record<string, string> = {
  hot: "text-red-600",
  warm: "text-amber-600",
  cold: "text-sky-600",
  archive: "text-slate-400",
};

function fmtDate(s: string | null): string {
  if (!s) return "—";
  return new Date(s).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function fmtScore(n: number | null): string {
  if (n == null) return "—";
  return n.toString();
}

function fmtMoney(s: string | null): string {
  if (!s) return "—";
  const n = parseFloat(s);
  if (isNaN(n)) return s;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
}

function getTabLeads(leads: LeadListItem[], tab: Tab): LeadListItem[] {
  switch (tab) {
    case "latest_run":
      return leads.filter((l) => l.is_new_in_run);
    case "pending":
      return leads.filter((l) => l.sales_status === "pending_review");
    case "recent":
      return [...leads].sort(
        (a, b) =>
          new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
      );
    default:
      return leads;
  }
}

function getUniqueSources(leads: LeadListItem[]): string[] {
  const sources = leads
    .map((l) => l.primary_source)
    .filter((s): s is string => Boolean(s));
  return [...new Set(sources)].sort();
}

function getUniqueStatuses(leads: LeadListItem[]): string[] {
  return [...new Set(leads.map((l) => l.sales_status))].sort();
}

interface FilterChipProps {
  label: string;
  onRemove: () => void;
}

function FilterChip({ label, onRemove }: FilterChipProps) {
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-blue-50 text-blue-700 text-xs font-medium ring-1 ring-inset ring-blue-600/20">
      {label}
      <button
        onClick={onRemove}
        className="hover:text-blue-900 transition-colors ml-0.5"
        aria-label={`Remove filter: ${label}`}
      >
        <X className="w-2.5 h-2.5" />
      </button>
    </span>
  );
}

interface Props {
  leads: LeadListItem[];
  total: number;
  fetchError?: string;
}

export default function LeadsContainer({ leads, total, fetchError }: Props) {
  const [activeTab, setActiveTab] = useState<Tab>("all");
  const [search, setSearch] = useState("");
  const [tierFilter, setTierFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [includeExcluded, setIncludeExcluded] = useState(false);

  const statuses = useMemo(() => getUniqueStatuses(leads), [leads]);
  const sources = useMemo(() => getUniqueSources(leads), [leads]);

  const tabCounts = useMemo(
    () => ({
      all: leads.length,
      latest_run: leads.filter((l) => l.is_new_in_run).length,
      pending: leads.filter((l) => l.sales_status === "pending_review").length,
      recent: leads.length,
    }),
    [leads]
  );

  const tabLeads = useMemo(
    () => getTabLeads(leads, activeTab),
    [leads, activeTab]
  );

  const filtered = useMemo(() => {
    let result = tabLeads;
    if (!includeExcluded) result = result.filter((l) => !l.sector_excluded);
    if (search.trim()) {
      const q = search.toLowerCase();
      result = result.filter((l) =>
        l.company_name.toLowerCase().includes(q)
      );
    }
    if (tierFilter) result = result.filter((l) => l.tier === tierFilter);
    if (statusFilter)
      result = result.filter((l) => l.sales_status === statusFilter);
    if (sourceFilter)
      result = result.filter((l) => l.primary_source === sourceFilter);
    return result;
  }, [tabLeads, search, tierFilter, statusFilter, sourceFilter, includeExcluded]);

  const hasActiveFilters = search || tierFilter || statusFilter || sourceFilter;

  function clearFilters() {
    setSearch("");
    setTierFilter("");
    setStatusFilter("");
    setSourceFilter("");
  }

  if (fetchError) {
    return (
      <div className="px-6 py-6 max-w-2xl">
        <ErrorState message={fetchError} />
      </div>
    );
  }

  return (
    <div className="flex flex-col" style={{ minHeight: 0 }}>
      {/* View tabs */}
      <div className="border-b border-slate-200 bg-white px-6 flex items-center gap-0">
        {TABS.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => setActiveTab(key)}
            className={`relative px-4 py-3 text-[13px] font-medium transition-colors whitespace-nowrap ${
              activeTab === key
                ? "text-blue-600"
                : "text-slate-500 hover:text-slate-700"
            }`}
          >
            {activeTab === key && (
              <span className="absolute bottom-0 left-0 right-0 h-0.5 bg-blue-600" />
            )}
            {label}
            {tabCounts[key] > 0 && (
              <span
                className={`ml-1.5 text-[10px] rounded px-1.5 py-0.5 tabular-nums ${
                  activeTab === key
                    ? "bg-blue-50 text-blue-600"
                    : "bg-slate-100 text-slate-500"
                }`}
              >
                {tabCounts[key].toLocaleString()}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Filter bar */}
      <div className="border-b border-slate-200 bg-white px-6 py-3 flex items-center gap-3 flex-wrap">
        <div className="relative flex-1 min-w-[180px] max-w-xs">
          <Search className="absolute left-2.5 top-[7px] w-3.5 h-3.5 text-slate-400 pointer-events-none" />
          <input
            type="text"
            placeholder="Search companies..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full pl-8 pr-3 py-[6px] text-[13px] bg-slate-50 border border-slate-200 rounded-md text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500 transition-colors"
          />
        </div>

        <select
          value={tierFilter}
          onChange={(e) => setTierFilter(e.target.value)}
          className="text-[13px] bg-slate-50 border border-slate-200 rounded-md px-2.5 py-[6px] text-slate-700 focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500 transition-colors"
        >
          <option value="">All tiers</option>
          {TIERS.map((t) => (
            <option key={t} value={t}>
              {t.charAt(0).toUpperCase() + t.slice(1)}
            </option>
          ))}
        </select>

        {statuses.length > 1 && (
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="text-[13px] bg-slate-50 border border-slate-200 rounded-md px-2.5 py-[6px] text-slate-700 focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500 transition-colors"
          >
            <option value="">All statuses</option>
            {statuses.map((s) => (
              <option key={s} value={s}>
                {s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
              </option>
            ))}
          </select>
        )}

        {sources.length > 1 && (
          <select
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
            className="text-[13px] bg-slate-50 border border-slate-200 rounded-md px-2.5 py-[6px] text-slate-700 focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500 transition-colors"
          >
            <option value="">All sources</option>
            {sources.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        )}

        {hasActiveFilters && (
          <button
            onClick={clearFilters}
            className="flex items-center gap-1 text-[12px] text-slate-500 hover:text-slate-700 transition-colors"
          >
            <X className="w-3 h-3" />
            Clear filters
          </button>
        )}

        <label className="flex items-center gap-1.5 cursor-pointer ml-2 select-none">
          <input
            type="checkbox"
            checked={includeExcluded}
            onChange={(e) => setIncludeExcluded(e.target.checked)}
            className="w-3.5 h-3.5 rounded border-slate-300 text-amber-500 focus:ring-amber-400"
          />
          <span className="text-[12px] text-slate-400 whitespace-nowrap">
            Include leads outside Porter ICP sectors
          </span>
        </label>

        <p className="ml-auto text-[11px] text-slate-400 tabular-nums whitespace-nowrap">
          {filtered.length.toLocaleString()} of {total.toLocaleString()} leads
        </p>
      </div>

      {/* Active filter chips */}
      {hasActiveFilters && (
        <div className="bg-white border-b border-slate-100 px-6 py-2 flex items-center gap-2 flex-wrap">
          <span className="text-[10px] text-slate-400 uppercase tracking-wider font-semibold mr-1">
            Active:
          </span>
          {search && (
            <FilterChip
              label={`Search: "${search}"`}
              onRemove={() => setSearch("")}
            />
          )}
          {tierFilter && (
            <FilterChip
              label={`Tier: ${tierFilter}`}
              onRemove={() => setTierFilter("")}
            />
          )}
          {statusFilter && (
            <FilterChip
              label={`Status: ${statusFilter.replace(/_/g, " ")}`}
              onRemove={() => setStatusFilter("")}
            />
          )}
          {sourceFilter && (
            <FilterChip
              label={`Source: ${sourceFilter}`}
              onRemove={() => setSourceFilter("")}
            />
          )}
        </div>
      )}

      {/* Table */}
      <div className="overflow-x-auto">
        {filtered.length === 0 ? (
          <EmptyState
            title={
              hasActiveFilters
                ? "No leads match these filters"
                : "No leads found"
            }
            description={
              hasActiveFilters
                ? "Try adjusting your search or filters."
                : "Run the pipeline to populate leads."
            }
          />
        ) : (
          <table className="min-w-full">
            <thead className="bg-white border-b border-slate-200">
              <tr>
                <th className="px-5 py-3 text-left text-[10px] font-semibold uppercase tracking-widest text-slate-400 whitespace-nowrap">
                  Company
                </th>
                <th className="px-4 py-3 text-left text-[10px] font-semibold uppercase tracking-widest text-slate-400 whitespace-nowrap">
                  Tier
                </th>
                <th className="px-4 py-3 text-right text-[10px] font-semibold uppercase tracking-widest text-slate-400 whitespace-nowrap">
                  Score
                </th>
                <th className="px-4 py-3 text-left text-[10px] font-semibold uppercase tracking-widest text-slate-400 whitespace-nowrap">
                  Source
                </th>
                <th className="px-4 py-3 text-left text-[10px] font-semibold uppercase tracking-widest text-slate-400 whitespace-nowrap">
                  Signal Date
                </th>
                <th className="px-4 py-3 text-right text-[10px] font-semibold uppercase tracking-widest text-slate-400 whitespace-nowrap">
                  Max Award
                </th>
                <th className="px-4 py-3 text-left text-[10px] font-semibold uppercase tracking-widest text-slate-400 whitespace-nowrap">
                  Status
                </th>
                <th className="px-4 py-3 text-center text-[10px] font-semibold uppercase tracking-widest text-slate-400 whitespace-nowrap">
                  In Run
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 bg-white">
              {filtered.map((lead) => (
                <tr
                  key={lead.lead_id}
                  className="hover:bg-slate-50 transition-colors group"
                >
                  <td className="px-5 py-3">
                    <Link
                      href={`/leads/${lead.lead_id}`}
                      className="block min-w-0"
                    >
                      <span className="inline-flex items-center gap-1.5 leading-snug">
                        <span className="text-[13px] font-medium text-slate-900 group-hover:text-blue-600 transition-colors">
                          {lead.company_name}
                        </span>
                        {includeExcluded && lead.sector_excluded && (
                          <span
                            title={lead.sector_excluded_reason ?? "Outside Porter ICP sector"}
                            className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-50 text-amber-700 ring-1 ring-inset ring-amber-600/20 whitespace-nowrap"
                          >
                            ⚠ Outside ICP
                          </span>
                        )}
                      </span>
                      <span className="block text-[10px] font-mono text-slate-400 mt-0.5">
                        {lead.lead_id.substring(0, 8)}&hellip;
                      </span>
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    <TierBadge tier={lead.tier} />
                  </td>
                  <td className="px-4 py-3 text-right">
                    <span
                      className={`text-[13px] font-bold tabular-nums font-mono ${
                        SCORE_COLOR[lead.tier ?? ""] ?? "text-slate-600"
                      }`}
                    >
                      {fmtScore(lead.score)}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-col gap-1">
                      <SourceBadge source={lead.primary_source} />
                      <SignalTypeBadge signalType={lead.signal_type} />
                    </div>
                  </td>
                  <td className="px-4 py-3 text-[12px] text-slate-500 whitespace-nowrap">
                    {fmtDate(lead.latest_signal_date)}
                  </td>
                  <td className="px-4 py-3 text-right text-[12px] font-medium text-slate-700 tabular-nums">
                    {fmtMoney(lead.max_award_amount)}
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge status={lead.sales_status} />
                  </td>
                  <td className="px-4 py-3 text-center">
                    {lead.is_new_in_run ? (
                      <span
                        title="New in latest run"
                        className="inline-block w-2 h-2 rounded-full bg-emerald-500"
                      />
                    ) : (
                      <span className="text-slate-200 text-xs">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Table footer */}
      {filtered.length > 0 && total > leads.length && (
        <div className="bg-white border-t border-slate-100 px-5 py-2.5">
          <p className="text-[11px] text-slate-400">
            Showing {leads.length.toLocaleString()} of{" "}
            {total.toLocaleString()} total leads — expand limit to see more.
          </p>
        </div>
      )}
    </div>
  );
}
