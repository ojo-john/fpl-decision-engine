import { backendClient } from "@/lib/api-client";
import { Card, ErrorState } from "@/components/ui";
import {
  RUN_STATUS_LABELS,
  TRANSFER_ACTION_LABELS as ACTION_LABELS,
  transfersCountActionKey,
} from "@/lib/ui-labels";

export const dynamic = "force-dynamic";

export default async function TransfersPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  const client = backendClient();
  const { data, response } = await client.GET("/optimize/transfers/{transfer_optimization_run_id}", {
    params: { path: { transfer_optimization_run_id: runId } },
  });

  if (response.status === 404) {
    return <ErrorState>No transfer-optimization run found for id {runId}.</ErrorState>;
  }
  if (!data) {
    return <ErrorState>Could not load this transfer-optimization run ({response.status}).</ErrorState>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <h1 style={{ margin: "0 0 0.5rem", fontSize: "1.1rem" }}>
          Transfer scenarios -- {RUN_STATUS_LABELS[data.run_status] ?? data.run_status}
        </h1>
        <div style={{ display: "flex", gap: "1.5rem", color: "var(--text-muted)", fontSize: "0.9rem" }}>
          <span>Recommended: {ACTION_LABELS[data.recommended_action] ?? data.recommended_action}</span>
          <span>Free transfers: {data.free_transfers_available}</span>
          <span>Bank: {(data.bank / 10).toFixed(1)}</span>
        </div>
      </Card>

      <Card>
        <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>How this is calculated</h2>
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: "0.85rem" }}>
          Each scenario projects your squad&apos;s total points over the next{" "}
          <strong>{data.horizon_gameweek_count} gameweeks</strong>, not just the next one. Every
          player&apos;s projection for each of those gameweeks is already adjusted for that
          gameweek&apos;s own fixture difficulty, so a run of easy or hard fixtures is priced in.
          &quot;Net gain&quot; is the extra points a scenario is expected to score versus doing
          nothing, with any points-hit cost already subtracted. This does <strong>not</strong>{" "}
          know who you face in any specific H2H matchup -- it only optimizes your squad&apos;s own
          points, independent of any rival.
        </p>
      </Card>

      {[...data.scenarios]
        .sort((a, b) => a.transfers_count - b.transfers_count)
        .map((scenario) => (
          <Card
            key={scenario.transfers_count}
            style={scenario.is_recommended ? { borderColor: "var(--accent)" } : undefined}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <h2 style={{ margin: 0, fontSize: "0.95rem" }}>
                {ACTION_LABELS[transfersCountActionKey(scenario.transfers_count)] ??
                  `${scenario.transfers_count} transfer(s)`}
                {scenario.is_recommended && " (recommended)"}
              </h2>
              <span style={{ color: "var(--text-muted)", fontSize: "0.9rem" }}>
                net gain {Number(scenario.net_horizon_points_gain).toFixed(1)} pts
                {scenario.hit_cost > 0 && ` (-${scenario.hit_cost} hit)`}
              </span>
            </div>
            {scenario.moves.length > 0 && (
              <ul style={{ margin: "0.75rem 0 0", paddingLeft: "1.1rem", fontSize: "0.9rem" }}>
                {scenario.moves.map((move) => (
                  <li key={`${move.player_out_id}-${move.player_in_id}`}>
                    {move.player_out_web_name ?? "Unknown"} → {move.player_in_web_name ?? "Unknown"} (
                    {Number(move.projected_points_gain_over_horizon).toFixed(1)} pts over horizon)
                  </li>
                ))}
              </ul>
            )}
          </Card>
        ))}
    </div>
  );
}
