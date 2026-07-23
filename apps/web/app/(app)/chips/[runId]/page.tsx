import { backendClient } from "@/lib/api-client";
import { Card, ErrorState } from "@/components/ui";

export const dynamic = "force-dynamic";

const VERDICT_LABELS: Record<string, string> = {
  use_now: "Use now",
  hold: "Hold",
};

export default async function ChipsPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  const client = backendClient();
  const { data, response } = await client.GET("/optimize/chips/{chip_optimization_run_id}", {
    params: { path: { chip_optimization_run_id: runId } },
  });

  if (response.status === 404) {
    return <ErrorState>No chip-optimization run found for id {runId}.</ErrorState>;
  }
  if (!data) {
    return <ErrorState>Could not load this chip-optimization run ({response.status}).</ErrorState>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <h1 style={{ margin: 0, fontSize: "1.1rem" }}>
          Chip evaluation for GW{data.target_fpl_event_id} -- {data.run_status}
        </h1>
      </Card>

      {data.recommendations.map((recommendation) => (
        <Card
          key={recommendation.chip_type}
          style={recommendation.verdict === "use_now" ? { borderColor: "var(--accent)" } : undefined}
        >
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <h2 style={{ margin: 0, fontSize: "0.95rem", textTransform: "uppercase" }}>
              {recommendation.chip_type}
            </h2>
            <span style={{ color: "var(--text-muted)", fontSize: "0.9rem" }}>
              {VERDICT_LABELS[recommendation.verdict] ?? recommendation.verdict}
            </span>
          </div>
          <p style={{ margin: "0.5rem 0", color: "var(--text-muted)", fontSize: "0.9rem" }}>
            {recommendation.rationale}
          </p>
          {recommendation.candidates.length > 0 && (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem", marginTop: "0.5rem" }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                  <th style={{ padding: "0.25rem 0" }}>Gameweek</th>
                  <th>Expected gain</th>
                  <th>Range</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {recommendation.candidates.map((candidate) => (
                  <tr key={candidate.candidate_fpl_event_id} style={{ borderTop: "1px solid var(--border)" }}>
                    <td style={{ padding: "0.35rem 0" }}>
                      GW{candidate.candidate_fpl_event_id}
                      {candidate.is_double_gameweek && " (DGW)"}
                      {candidate.is_blank_gameweek && " (BGW)"}
                    </td>
                    <td>{Number(candidate.gain_mean).toFixed(1)}</td>
                    <td>
                      {Number(candidate.gain_floor).toFixed(1)} - {Number(candidate.gain_ceiling).toFixed(1)}
                    </td>
                    <td>
                      {recommendation.recommended_candidate?.candidate_fpl_event_id ===
                        candidate.candidate_fpl_event_id && (
                        <span style={{ color: "var(--accent)" }}>recommended</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      ))}
    </div>
  );
}
