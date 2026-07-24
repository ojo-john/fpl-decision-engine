import { backendClient } from "@/lib/api-client";
import { Card, ConfidenceBadge, ErrorState, TableScroll } from "@/components/ui";

export const dynamic = "force-dynamic";

function pct(value: string | null): string {
  return value !== null ? `${(Number(value) * 100).toFixed(0)}%` : "unknown";
}

export default async function H2HPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  const client = backendClient();
  const { data, response } = await client.GET("/h2h/simulate/{h2h_simulation_run_id}", {
    params: { path: { h2h_simulation_run_id: runId } },
  });

  if (response.status === 404) {
    return <ErrorState>No H2H simulation run found for id {runId}.</ErrorState>;
  }
  if (!data) {
    return <ErrorState>Could not load this H2H simulation run ({response.status}).</ErrorState>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "start" }}>
          <h1 style={{ margin: 0, fontSize: "1.1rem" }}>
            H2H matchup, GW{data.target_fpl_event_id} -- {data.run_status}
          </h1>
          {data.confidence && <ConfidenceBadge confidence={data.confidence} />}
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
            gap: "0.5rem",
            marginTop: "0.75rem",
            fontSize: "0.9rem",
            color: "var(--text-muted)",
          }}
        >
          <span>Win: {pct(data.win_probability)}</span>
          <span>Draw: {pct(data.draw_probability)}</span>
          <span>Loss: {pct(data.loss_probability)}</span>
          <span>
            Expected margin: {data.expected_margin !== null ? Number(data.expected_margin).toFixed(1) : "unknown"}
          </span>
        </div>
        <div style={{ display: "flex", gap: "1.5rem", marginTop: "0.5rem", fontSize: "0.85rem", color: "var(--text-muted)" }}>
          <span>
            Your score: {data.user_score_mean !== null ? Number(data.user_score_mean).toFixed(1) : "unknown"}
          </span>
          <span>
            Rival score:{" "}
            {data.rival_score_mean !== null ? Number(data.rival_score_mean).toFixed(1) : "unknown"}
          </span>
        </div>
      </Card>

      {data.strategy_candidates.length > 0 && (
        <Card>
          <h2 style={{ margin: "0 0 0.75rem", fontSize: "0.95rem" }}>Strategy candidates</h2>
          <TableScroll>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                  <th style={{ padding: "0.25rem 0" }}>Strategy</th>
                  <th>Win</th>
                  <th>Draw</th>
                  <th>Loss</th>
                  <th>Margin</th>
                </tr>
              </thead>
              <tbody>
                {data.strategy_candidates.map((candidate) => (
                  <tr
                    key={candidate.strategy_label}
                    style={{
                      borderTop: "1px solid var(--border)",
                      color: candidate.is_recommended ? "var(--accent)" : undefined,
                    }}
                  >
                    <td style={{ padding: "0.35rem 0" }}>
                      {candidate.strategy_label}
                      {candidate.is_recommended && " (recommended)"}
                    </td>
                    <td>{(Number(candidate.win_probability) * 100).toFixed(0)}%</td>
                    <td>{(Number(candidate.draw_probability) * 100).toFixed(0)}%</td>
                    <td>{(Number(candidate.loss_probability) * 100).toFixed(0)}%</td>
                    <td>{Number(candidate.expected_margin).toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        </Card>
      )}
    </div>
  );
}
