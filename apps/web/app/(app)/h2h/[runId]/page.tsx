import { backendClient } from "@/lib/api-client";
import { Card, ConfidenceBadge, ErrorState, TableScroll } from "@/components/ui";

export const dynamic = "force-dynamic";

function pct(value: string | null): string {
  return value !== null ? `${(Number(value) * 100).toFixed(0)}%` : "unknown";
}

/** Each row is the SAME squad you already have, just with a different captain-choice policy
 * applied and re-simulated against this specific rival -- see app/h2h/strategy.py's own
 * docstring for the exact selection rule behind each one. */
const STRATEGY_LABELS: Record<string, string> = {
  ep_optimal: "Highest expected points",
  low_variance: "Safest floor",
  high_upside: "Highest ceiling",
  captain_cover: "Hedge against rotation risk",
  differential_captain: "Differential (lower-owned)",
  no_hit: "No points hit",
  calculated_hit: "Take a hit, if worth it",
};

const STRATEGY_DESCRIPTIONS: Record<string, string> = {
  ep_optimal: "Captain whoever is projected to score the most -- no hedging.",
  low_variance: "Captain your most reliable starter, even if it means a lower ceiling.",
  high_upside: "Captain your highest-ceiling starter, accepting more boom-or-bust risk.",
  captain_cover: "Of your 2 safest (least rotation-risk) starters, captain whichever projects higher.",
  differential_captain: "Captain a lower-owned player of yours for extra rank swing if it pays off.",
  no_hit: "Use only free transfers -- never take a points deduction for this matchup.",
  calculated_hit: "Take a points hit for an extra transfer only if it's worth it against this rival.",
};

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
          <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Strategy candidates</h2>
          <p style={{ margin: "0 0 0.75rem", color: "var(--text-muted)", fontSize: "0.85rem" }}>
            Same squad, 7 different captain/transfer policies, each re-simulated against this
            specific rival.
          </p>
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
                      <div>
                        {STRATEGY_LABELS[candidate.strategy_label] ?? candidate.strategy_label}
                        {candidate.is_recommended && " (recommended)"}
                      </div>
                      {STRATEGY_DESCRIPTIONS[candidate.strategy_label] && (
                        <div style={{ color: "var(--text-muted)", fontSize: "0.76rem", marginTop: "0.1rem" }}>
                          {STRATEGY_DESCRIPTIONS[candidate.strategy_label]}
                        </div>
                      )}
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
