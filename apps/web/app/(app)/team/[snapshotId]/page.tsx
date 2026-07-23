import { backendClient } from "@/lib/api-client";
import { SquadActions } from "@/components/squad-actions";
import { Card, ErrorState } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function TeamSnapshotPage({
  params,
}: {
  params: Promise<{ snapshotId: string }>;
}) {
  const { snapshotId } = await params;
  const client = backendClient();
  const { data, response } = await client.GET("/squads/{manager_squad_snapshot_id}", {
    params: { path: { manager_squad_snapshot_id: snapshotId } },
  });

  if (response.status === 404) {
    return <ErrorState>No squad snapshot found for id {snapshotId}.</ErrorState>;
  }
  if (!data) {
    return <ErrorState>Could not load this squad snapshot ({response.status}).</ErrorState>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "start" }}>
          <div>
            <h1 style={{ margin: "0 0 0.25rem", fontSize: "1.1rem" }}>
              {data.source === "manager_sync" ? "Synced squad" : "Manual squad"}
              {data.is_current && <span style={{ color: "var(--success)", marginLeft: "0.5rem" }}>current</span>}
            </h1>
            <p style={{ margin: 0, color: "var(--text-muted)", fontSize: "0.85rem" }}>
              Created {new Date(data.created_at).toLocaleString()}
            </p>
          </div>
        </div>
        <div style={{ display: "flex", gap: "1.5rem", marginTop: "0.75rem", fontSize: "0.9rem", color: "var(--text-muted)" }}>
          <span>Bank: {(data.bank / 10).toFixed(1)}</span>
          <span>Free transfers: {data.free_transfers_available}</span>
        </div>
        {data.has_unresolved_players && (
          <p style={{ margin: "0.75rem 0 0", color: "var(--danger)", fontSize: "0.85rem" }}>
            {data.unresolved_fpl_element_ids.length} player(s) in this squad could not be resolved
            against the current season&rsquo;s player list (element ids:{" "}
            {data.unresolved_fpl_element_ids.join(", ")}).
          </p>
        )}
      </Card>

      <SquadActions
        snapshotId={data.manager_squad_snapshot_id}
        squadPlayerIds={data.squad_player_ids}
        hasRawPicks={data.has_raw_picks}
      />

      <Card>
        <h2 style={{ margin: "0 0 0.75rem", fontSize: "0.95rem" }}>Squad ({data.players.length})</h2>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.9rem" }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
              <th style={{ padding: "0.25rem 0" }}>Pos</th>
              <th>Player</th>
              <th>Price</th>
            </tr>
          </thead>
          <tbody>
            {data.players.map((player) => (
              <tr key={player.player_id} style={{ borderTop: "1px solid var(--border)" }}>
                <td style={{ padding: "0.4rem 0", color: "var(--text-muted)" }}>{player.position ?? "-"}</td>
                <td>{player.web_name ?? "Unknown"}</td>
                <td>{(player.now_cost / 10).toFixed(1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
