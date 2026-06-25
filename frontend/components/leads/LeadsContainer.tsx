"use client";

import { useState, useMemo } from "react";
import Link from "next/link";
import type { LeadListItem } from "@/lib/api";
import { EmptyState } from "@/components/ui/EmptyState";
import { ErrorState } from "@/components/ui/ErrorState";
import { StatusBadge } from "@/components/ui/StatusBadge";

// ── Design tokens ──────────────────────────────────────────────────────────────

const TIER_CFG: Record<string, { color: string; bg: string; label: string }> = {
  hot: { color: "#DC2626", bg: "rgba(220,38,38,0.10)", label: "Hot" },
  warm: { color: "#D97706", bg: "rgba(217,119,6,0.10)", label: "Warm" },
  cold: { color: "#2563EB", bg: "rgba(37,99,235,0.10)", label: "Cold" },
  archive: { color: "#71717A", bg: "rgba(113,113,122,0.10)", label: "Archive" },
};

const AVATAR_PALETTES = [
  { bg: "#EFF6FF", text: "#1D4ED8" },
  { bg: "#FEF3C7", text: "#92400E" },
  { bg: "#DCFCE7", text: "#166534" },
  { bg: "#F3E8FF", text: "#6B21A8" },
  { bg: "#FFEDD5", text: "#C2410C" },
  { bg: "#E0F2FE", text: "#0C4A6E" },
  { bg: "#FEE2E2", text: "#991B1B" },
  { bg: "#F0FDF4", text: "#15803D" },
];

function avatarPalette(name: string) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = ((hash << 5) - hash + name.charCodeAt(i)) | 0;
  }
  return AVATAR_PALETTES[Math.abs(hash) % AVATAR_PALETTES.length];
}

function getInitials(name: string): string {
  const words = name.replace(/[^a-zA-Z\s]/g, "").trim().split(/\s+/);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  return name.substring(0, 2).toUpperCase();
}

function fmtDate(s: string | null): string {
  if (!s) return "—";
  return new Date(s).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function fmtMoney(s: string | null): string {
  if (!s) return "—";
  const n = parseFloat(s);
  if (isNaN(n)) return s;
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `$${(n / 1_000).toFixed(0)}K`;
  return `$${n.toFixed(0)}`;
}

function fmtPipelineValue(leads: LeadListItem[]): string {
  const total = leads.reduce(
    (s, l) => s + (l.max_award_amount ? parseFloat(l.max_award_amount) : 0),
    0
  );
  if (total >= 1_000_000_000) return `$${(total / 1_000_000_000).toFixed(2)}B`;
  if (total >= 1_000_000) return `$${(total / 1_000_000).toFixed(2)}M`;
  return `$${(total / 1_000).toFixed(0)}K`;
}

// ── Chip component ─────────────────────────────────────────────────────────────

function Chip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: "4px 10px",
        borderRadius: 999,
        fontSize: 11,
        fontWeight: 500,
        cursor: "pointer",
        border: `0.5px solid ${active ? "#09090B" : "#E4E4E7"}`,
        background: active ? "#09090B" : "transparent",
        color: active ? "#FAFAFA" : "#71717A",
        transition: "all 0.1s",
        outline: "none",
      }}
    >
      {label}
    </button>
  );
}

// ── Toggle switch ──────────────────────────────────────────────────────────────

function Toggle({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      onClick={() => onChange(!on)}
      style={{
        width: 30,
        height: 17,
        borderRadius: 999,
        background: on ? "#2563EB" : "#E4E4E7",
        position: "relative",
        border: "none",
        cursor: "pointer",
        transition: "background 0.15s",
        flexShrink: 0,
        outline: "none",
      }}
    >
      <span
        style={{
          position: "absolute",
          top: 2,
          left: on ? 15 : 2,
          width: 13,
          height: 13,
          borderRadius: "50%",
          background: "#FFFFFF",
          transition: "left 0.15s",
        }}
      />
    </button>
  );
}

// ── Grid column template ───────────────────────────────────────────────────────

const GRID_COLS =
  "26px minmax(200px,1.7fr) 80px 88px 72px minmax(160px,1.2fr) 92px 130px 96px 100px";

// ── Main component ─────────────────────────────────────────────────────────────

interface Props {
  leads: LeadListItem[];
  total: number;
  fetchError?: string;
}

export default function LeadsContainer({ leads, total, fetchError }: Props) {
  const [tierFilters, setTierFilters] = useState<string[]>([]);
  const [sourceFilters, setSourceFilters] = useState<string[]>([]);
  const [statusFilters, setStatusFilters] = useState<string[]>([]);
  const [targetOnly, setTargetOnly] = useState(true);
  const [density, setDensity] = useState<"comfortable" | "compact">("comfortable");

  const excludedCount = useMemo(() => leads.filter((l) => l.sector_excluded).length, [leads]);

  const uniqueStatuses = useMemo(
    () => [...new Set(leads.map((l) => l.sales_status))].sort(),
    [leads]
  );

  function toggleFilter(arr: string[], val: string, set: (v: string[]) => void) {
    set(arr.includes(val) ? arr.filter((x) => x !== val) : [...arr, val]);
  }

  const filtered = useMemo(() => {
    let result = leads;
    if (targetOnly) result = result.filter((l) => !l.sector_excluded);
    if (tierFilters.length > 0)
      result = result.filter((l) => tierFilters.includes((l.tier ?? "").toLowerCase()));
    if (sourceFilters.length > 0)
      result = result.filter((l) => {
        const isPrime = l.signal_type === "CONTRACT_AWARD";
        const isSub = l.signal_type === "SUBCONTRACT_AWARD";
        return sourceFilters.some(
          (sf) => (sf === "Prime" && isPrime) || (sf === "Sub" && isSub)
        );
      });
    if (statusFilters.length > 0)
      result = result.filter((l) => statusFilters.includes(l.sales_status));
    return result;
  }, [leads, targetOnly, tierFilters, sourceFilters, statusFilters]);

  const compact = density === "compact";
  const rowH = compact ? 44 : 54;

  if (fetchError) {
    return (
      <div style={{ padding: "24px" }}>
        <ErrorState message={fetchError} />
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
      {/* Filter bar */}
      <div
        style={{
          padding: "0 24px 14px",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 16,
          flexWrap: "wrap",
          rowGap: 10,
          flexShrink: 0,
        }}
      >
        {/* Left: filter chips */}
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap", rowGap: 8 }}>
          <span
            style={{
              fontSize: 11,
              fontWeight: 600,
              textTransform: "uppercase",
              letterSpacing: "0.08em",
              color: "#71717A",
            }}
          >
            Filters
          </span>

          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontSize: 10, color: "#A1A1AA", marginRight: 1 }}>Tier</span>
            {["Hot", "Warm", "Cold", "Archive"].map((t) => (
              <Chip
                key={t}
                label={t}
                active={tierFilters.includes(t.toLowerCase())}
                onClick={() => toggleFilter(tierFilters, t.toLowerCase(), setTierFilters)}
              />
            ))}
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontSize: 10, color: "#A1A1AA", marginRight: 1 }}>Source</span>
            {["Prime", "Sub"].map((s) => (
              <Chip
                key={s}
                label={s}
                active={sourceFilters.includes(s)}
                onClick={() => toggleFilter(sourceFilters, s, setSourceFilters)}
              />
            ))}
          </div>

          {uniqueStatuses.length > 1 && (
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span style={{ fontSize: 10, color: "#A1A1AA", marginRight: 1 }}>Status</span>
              {uniqueStatuses.slice(0, 5).map((s) => (
                <Chip
                  key={s}
                  label={s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
                  active={statusFilters.includes(s)}
                  onClick={() => toggleFilter(statusFilters, s, setStatusFilters)}
                />
              ))}
            </div>
          )}
        </div>

        {/* Right: target toggle */}
        <div
          style={{ display: "flex", alignItems: "center", gap: 9, cursor: "pointer", userSelect: "none" }}
          onClick={() => setTargetOnly(!targetOnly)}
        >
          <span style={{ fontSize: 11, color: "#71717A" }}>
            Target industries only{" "}
            {excludedCount > 0 && (
              <span style={{ color: "#A1A1AA" }}>({excludedCount})</span>
            )}
          </span>
          <Toggle on={targetOnly} onChange={setTargetOnly} />
        </div>
      </div>

      {/* Count + density row */}
      <div
        style={{
          padding: "0 24px 14px",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
          flexShrink: 0,
        }}
      >
        <div
          style={{
            fontSize: 11,
            color: "#71717A",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {filtered.length.toLocaleString()} / {total.toLocaleString()} ·{" "}
          <span
            style={{
              color: "#09090B",
              fontWeight: 500,
              fontFamily: "var(--font-data, Inter, sans-serif)",
            }}
          >
            {fmtPipelineValue(filtered)}
          </span>{" "}
          in view
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <div
            style={{
              display: "flex",
              border: "0.5px solid #E4E4E7",
              borderRadius: 7,
              overflow: "hidden",
            }}
          >
            {(["comfortable", "compact"] as const).map((d) => (
              <button
                key={d}
                onClick={() => setDensity(d)}
                style={{
                  padding: "4px 11px",
                  fontSize: 11,
                  fontWeight: 500,
                  cursor: "pointer",
                  border: "none",
                  borderLeft: d === "compact" ? "0.5px solid #E4E4E7" : "none",
                  background: density === d ? "#09090B" : "#FFFFFF",
                  color: density === d ? "#FAFAFA" : "#71717A",
                  outline: "none",
                }}
              >
                {d === "comfortable" ? "Default" : "Compact"}
              </button>
            ))}
          </div>
          <div style={{ fontSize: 11, color: "#71717A" }}>
            Sort <span style={{ color: "#09090B", fontWeight: 500 }}>Score</span>{" "}
            <span style={{ color: "#A1A1AA" }}>▾</span>
          </div>
        </div>
      </div>

      {/* Table */}
      <div
        style={{
          background: "#FFFFFF",
          border: "0.5px solid #E4E4E7",
          borderRadius: 8,
          overflow: "hidden",
          margin: "0 24px",
          flexShrink: 0,
        }}
      >
        <div style={{ overflowX: "auto" }}>
          <div style={{ minWidth: 1140 }}>
            {/* Header */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: GRID_COLS,
                alignItems: "center",
                padding: "0 16px",
                height: 38,
                borderBottom: "0.5px solid #E4E4E7",
              }}
            >
              <div />
              {[
                "Company",
                "Score",
                "Tier",
                "Source",
                "Industry",
                "Award $",
                "Agency",
                "Signal",
                "Status",
              ].map((h, i) => (
                <div
                  key={h}
                  style={{
                    fontSize: 11,
                    fontWeight: 500,
                    textTransform: "uppercase",
                    letterSpacing: "0.08em",
                    color: "#71717A",
                    textAlign: h === "Award $" ? "right" : "left",
                    paddingRight: h === "Award $" ? 16 : 0,
                  }}
                >
                  {h}
                </div>
              ))}
            </div>

            {/* Rows */}
            {filtered.length === 0 ? (
              <EmptyState
                title="No leads match these filters"
                description="Try adjusting your filters or toggling the target industries switch."
              />
            ) : (
              filtered.map((lead, idx) => {
                const tierKey = (lead.tier ?? "").toLowerCase();
                const tierCfg = TIER_CFG[tierKey] ?? TIER_CFG.archive;
                const isPrime = lead.signal_type === "CONTRACT_AWARD";
                const isSub = lead.signal_type === "SUBCONTRACT_AWARD";
                const pal = avatarPalette(lead.company_name);
                const initials = getInitials(lead.company_name);
                const rowBg = idx % 2 === 0 ? "#FFFFFF" : "#FAFAFA";

                return (
                  <LeadRow
                    key={lead.lead_id}
                    lead={lead}
                    rowBg={rowBg}
                    rowH={rowH}
                    tierCfg={tierCfg}
                    isPrime={isPrime}
                    isSub={isSub}
                    pal={pal}
                    initials={initials}
                  />
                );
              })
            )}
          </div>
        </div>
      </div>

      {/* Keyboard hints */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
          padding: "12px 26px 20px",
          flexShrink: 0,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 14, fontSize: 11, color: "#A1A1AA" }}>
          {[
            { key: "↑↓", label: "navigate" },
            { key: "⏎", label: "open" },
            { key: "Space", label: "select" },
            { key: "⌘K", label: "command" },
          ].map(({ key, label }) => (
            <span key={key} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span
                style={{
                  fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
                  fontSize: 10,
                  padding: "1px 6px",
                  borderRadius: 4,
                  background: "#F4F4F5",
                  color: "#71717A",
                }}
              >
                {key}
              </span>
              {label}
            </span>
          ))}
        </div>
        <div style={{ fontSize: 11, color: "#A1A1AA", fontVariantNumeric: "tabular-nums" }}>
          Showing 1–{Math.min(filtered.length, 2000).toLocaleString()} of{" "}
          {total.toLocaleString()} leads
        </div>
      </div>
    </div>
  );
}

// ── Individual row (extracted to avoid inline closure issues) ──────────────────

interface RowProps {
  lead: LeadListItem;
  rowBg: string;
  rowH: number;
  tierCfg: { color: string; bg: string; label: string };
  isPrime: boolean;
  isSub: boolean;
  pal: { bg: string; text: string };
  initials: string;
}

function LeadRow({ lead, rowBg, rowH, tierCfg, isPrime, isSub, pal, initials }: RowProps) {
  const [hovered, setHovered] = useState(false);

  const naics = lead.company_naics ?? "—";
  const industry = lead.company_naics_description ?? lead.company_industry ?? "—";
  const agency = lead.awarding_agency ?? "—";
  const location = lead.company_state ?? "";

  return (
    <Link
      href={`/leads/${lead.lead_id}`}
      style={{ textDecoration: "none", display: "block" }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <div
        style={{
          display: "grid",
          gridTemplateColumns:
            "26px minmax(200px,1.7fr) 80px 88px 72px minmax(160px,1.2fr) 92px 130px 96px 100px",
          alignItems: "center",
          padding: "0 16px",
          minHeight: rowH,
          background: hovered ? "#EFF6FF" : rowBg,
          borderTop: "0.5px solid #E4E4E7",
          cursor: "pointer",
          transition: "background 0.1s",
        }}
      >
        {/* Checkbox */}
        <div>
          <span
            style={{
              display: "block",
              width: 14,
              height: 14,
              border: "1px solid #D4D4D8",
              borderRadius: 3,
              background: "#FFFFFF",
            }}
          />
        </div>

        {/* Company */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            minWidth: 0,
            paddingRight: 12,
          }}
        >
          <span
            style={{
              width: 28,
              height: 28,
              flexShrink: 0,
              borderRadius: 6,
              background: pal.bg,
              color: pal.text,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 10,
              fontWeight: 600,
              fontFamily: "var(--font-data, Inter, sans-serif)",
            }}
          >
            {initials}
          </span>
          <span style={{ minWidth: 0 }}>
            <span
              style={{
                display: "block",
                fontSize: 13,
                fontWeight: 500,
                color: "#2563EB",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {lead.company_name}
              {lead.sector_excluded && (
                <span
                  style={{
                    marginLeft: 6,
                    fontSize: 9,
                    fontWeight: 600,
                    letterSpacing: "0.03em",
                    padding: "1px 5px",
                    borderRadius: 3,
                    background: "rgba(217,119,6,0.10)",
                    color: "#D97706",
                    verticalAlign: "middle",
                  }}
                >
                  OFF-TARGET
                </span>
              )}
            </span>
            <span
              style={{
                display: "flex",
                alignItems: "center",
                gap: 7,
                marginTop: 2,
                minWidth: 0,
              }}
            >
              <span
                style={{
                  fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
                  fontSize: 10,
                  color: "#A1A1AA",
                  flexShrink: 0,
                }}
              >
                {lead.lead_id.substring(0, 8)}…
              </span>
              {location && (
                <span
                  style={{
                    fontSize: 11,
                    color: "#A1A1AA",
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {location}
                </span>
              )}
            </span>
          </span>
        </div>

        {/* Score */}
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span
            style={{
              fontFamily: "var(--font-data, Inter, sans-serif)",
              fontSize: 14,
              fontWeight: 500,
              fontVariantNumeric: "tabular-nums",
              color: tierCfg.color,
            }}
          >
            {lead.score ?? "—"}
          </span>
          {lead.score != null && (
            <div
              style={{
                width: 64,
                height: 3,
                background: "#E4E4E7",
                borderRadius: 2,
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  width: `${Math.min(100, ((lead.score ?? 0) / 73) * 100)}%`,
                  height: "100%",
                  background: tierCfg.color,
                }}
              />
            </div>
          )}
        </div>

        {/* Tier */}
        <div>
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              padding: "3px 9px",
              borderRadius: 999,
              fontSize: 10,
              fontWeight: 500,
              background: tierCfg.bg,
              color: tierCfg.color,
            }}
          >
            <span
              style={{
                width: 5,
                height: 5,
                borderRadius: "50%",
                background: tierCfg.color,
                flexShrink: 0,
              }}
            />
            {tierCfg.label}
          </span>
        </div>

        {/* Source */}
        <div>
          {isPrime || isSub ? (
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                padding: "3px 7px",
                borderRadius: 4,
                fontSize: 10,
                fontWeight: 500,
                background: isPrime ? "rgba(22,163,74,0.10)" : "rgba(37,99,235,0.10)",
                color: isPrime ? "#16A34A" : "#2563EB",
              }}
            >
              {isPrime ? "↑ Prime" : "↓ Sub"}
            </span>
          ) : (
            <span style={{ fontSize: 12, color: "#A1A1AA" }}>—</span>
          )}
        </div>

        {/* Industry */}
        <div style={{ minWidth: 0, paddingRight: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
            <span
              style={{
                fontSize: 12,
                color: "#52525B",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {industry}
            </span>
          </div>
          <div
            style={{
              fontFamily: "var(--font-mono, 'JetBrains Mono', monospace)",
              fontSize: 10,
              color: "#A1A1AA",
              marginTop: 2,
            }}
          >
            {naics !== "—" ? `NAICS ${naics}` : "—"}
          </div>
        </div>

        {/* Award $ */}
        <div
          style={{
            fontFamily: "var(--font-data, Inter, sans-serif)",
            fontSize: 12,
            fontWeight: 500,
            fontVariantNumeric: "tabular-nums",
            textAlign: "right",
            paddingRight: 16,
            color: "#09090B",
          }}
        >
          {fmtMoney(lead.max_award_amount)}
        </div>

        {/* Agency */}
        <div
          style={{
            fontSize: 12,
            color: "#52525B",
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
            paddingRight: 10,
          }}
        >
          {agency}
        </div>

        {/* Signal date */}
        <div
          style={{
            fontSize: 12,
            color: "#A1A1AA",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {fmtDate(lead.latest_signal_date)}
        </div>

        {/* Status */}
        <div>
          <StatusBadge status={lead.sales_status} />
        </div>
      </div>
    </Link>
  );
}
