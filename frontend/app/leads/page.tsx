import { fetchLeads } from "@/lib/api";
import { PageHeader } from "@/components/ui/PageHeader";
import LeadsContainer from "@/components/leads/LeadsContainer";

export default async function LeadsPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const sp = await searchParams;

  const tier =
    typeof sp.tier === "string" ? sp.tier : undefined;
  const status =
    typeof sp.status === "string" ? sp.status : undefined;

  const params: Record<string, string> = {
    limit: "200",
    sort_by: "score_desc",
  };
  if (tier) params.tier = tier;
  if (status) params.sales_status = status;

  let data;
  let fetchError: string | undefined;

  try {
    data = await fetchLeads(params);
  } catch (err) {
    fetchError =
      err instanceof Error ? err.message : "Could not reach the API.";
  }

  return (
    <>
      <PageHeader
        title="Lead Review"
        description="Research-ready leads — not sales-ready. Government contract activity signals only. Human review required before any outreach."
      />
      <LeadsContainer
        leads={data?.items ?? []}
        total={data?.total ?? 0}
        fetchError={fetchError}
      />
    </>
  );
}
