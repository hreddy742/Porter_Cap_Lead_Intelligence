"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

interface Props {
  leadId: string;
}

const DECISIONS = [
  { value: "approved", label: "Approve", color: "#16A34A", bg: "rgba(22,163,74,0.10)" },
  { value: "contacted", label: "Contacted", color: "#2563EB", bg: "rgba(37,99,235,0.10)" },
  { value: "rejected", label: "Reject", color: "#DC2626", bg: "rgba(220,38,38,0.10)" },
];

export default function ReviewPanel({ leadId }: Props) {
  const router = useRouter();
  const [decision, setDecision] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit() {
    if (!decision || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(`/api/leads/${leadId}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          decision,
          note: note.trim() || null,
          reviewer: "John Cox Miller",
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        throw new Error(typeof err.detail === "string" ? err.detail : `HTTP ${res.status}`);
      }
      setSuccess(`Marked as "${decision}"`);
      setDecision(null);
      setNote("");
      router.refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Submit failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="rounded-lg border border-slate-200 bg-white shadow-sm overflow-hidden">
      <div className="px-4 py-3 border-b border-slate-100">
        <p className="text-[11px] font-semibold uppercase tracking-widest text-slate-500">
          Review Decision
        </p>
      </div>
      <div className="p-4 space-y-3">
        {/* Decision buttons */}
        <div style={{ display: "flex", gap: 6 }}>
          {DECISIONS.map((d) => (
            <button
              key={d.value}
              onClick={() => setDecision(decision === d.value ? null : d.value)}
              style={{
                flex: 1,
                padding: "6px 0",
                borderRadius: 6,
                border: `1.5px solid ${decision === d.value ? d.color : "#E4E4E7"}`,
                background: decision === d.value ? d.bg : "transparent",
                color: decision === d.value ? d.color : "#71717A",
                fontSize: 12,
                fontWeight: 600,
                cursor: "pointer",
                transition: "all 0.1s",
                outline: "none",
              }}
            >
              {d.label}
            </button>
          ))}
        </div>

        {/* Note */}
        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Note (optional)"
          rows={2}
          style={{
            width: "100%",
            padding: "6px 8px",
            borderRadius: 6,
            border: "0.5px solid #E4E4E7",
            fontSize: 12,
            color: "#09090B",
            resize: "none",
            outline: "none",
            boxSizing: "border-box",
            fontFamily: "inherit",
          }}
        />

        {/* Submit */}
        <button
          onClick={handleSubmit}
          disabled={!decision || submitting}
          style={{
            width: "100%",
            padding: "7px 0",
            borderRadius: 6,
            border: "none",
            background: decision && !submitting ? "#09090B" : "#E4E4E7",
            color: decision && !submitting ? "#FAFAFA" : "#A1A1AA",
            fontSize: 12,
            fontWeight: 600,
            cursor: decision && !submitting ? "pointer" : "default",
            transition: "all 0.1s",
            outline: "none",
          }}
        >
          {submitting ? "Submitting…" : "Submit Decision"}
        </button>

        {/* Feedback */}
        {success && (
          <p style={{ fontSize: 11, color: "#16A34A", textAlign: "center", margin: 0 }}>
            {success}
          </p>
        )}
        {error && (
          <p style={{ fontSize: 11, color: "#DC2626", textAlign: "center", margin: 0 }}>
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
