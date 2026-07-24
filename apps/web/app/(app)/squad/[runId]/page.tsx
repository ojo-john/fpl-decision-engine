import { backendClient } from "@/lib/api-client";
import { Card, ErrorState, TableScroll } from "@/components/ui";

export const dynamic = "force-dynamic";

const POSITION_ORDER = ["GKP", "DEF", "MID", "FWD"];

export default async function SquadPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  const client = backendClient();
  const { data, response } = await client.GET("/optimize/{optimization_run_id}", {
    params: { path: { optimization_run_id: runId } },
  });

  if (response.status === 404) {
    return <ErrorState>No optimization run found for id {runId}.</ErrorState>;
  }
  if (!data) {
    return <ErrorState>Could not load this optimization run ({response.status}).</ErrorState>;
  }

  const starters = [...data.players]
    .filter((p) => p.is_starting)
    .sort((a, b) => POSITION_ORDER.indexOf(a.position ?? "") - POSITION_ORDER.indexOf(b.position ?? ""));
  const bench = [...data.players]
    .filter((p) => !p.is_starting)
    .sort((a, b) => (a.bench_order ?? 0) - (b.bench_order ?? 0));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <h1 style={{ margin: "0 0 0.5rem", fontSize: "1.1rem" }}>
          {data.recommendation_type === "lineup" ? "Lineup" : "Squad"} -- {data.run_status}
          {data.solver_status ? ` (${data.solver_status})` : ""}
        </h1>
        <div style={{ display: "flex", gap: "1.5rem", color: "var(--text-muted)", fontSize: "0.9rem" }}>
          {data.squad_total_cost !== null && <span>Squad cost: {(data.squad_total_cost / 10).toFixed(1)}</span>}
          {data.squad_projected_points !== null && (
            <span>Squad projected: {Number(data.squad_projected_points).toFixed(1)} pts</span>
          )}
          {data.xi_projected_points !== null && (
            <span>XI projected: {Number(data.xi_projected_points).toFixed(1)} pts</span>
          )}
        </div>
      </Card>

      <Card>
        <h2 style={{ margin: "0 0 0.75rem", fontSize: "0.95rem" }}>Starting XI</h2>
        <PlayerTable players={starters} captainId={data.captain_player_id} viceCaptainId={data.vice_captain_player_id} />
      </Card>

      {bench.length > 0 && (
        <Card>
          <h2 style={{ margin: "0 0 0.75rem", fontSize: "0.95rem" }}>Bench</h2>
          <PlayerTable players={bench} captainId={data.captain_player_id} viceCaptainId={data.vice_captain_player_id} />
        </Card>
      )}
    </div>
  );
}

type Player = {
  player_id: string;
  web_name: string | null;
  position: string | null;
  projected_points: string;
  now_cost_snapshot: number;
};

function PlayerTable({
  players,
  captainId,
  viceCaptainId,
}: {
  players: Player[];
  captainId: string | null;
  viceCaptainId: string | null;
}) {
  return (
    <TableScroll>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.9rem" }}>
        <thead>
          <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
            <th style={{ padding: "0.25rem 0" }}>Pos</th>
            <th>Player</th>
            <th>Price</th>
            <th>Projected</th>
          </tr>
        </thead>
        <tbody>
          {players.map((p) => (
            <tr key={p.player_id} style={{ borderTop: "1px solid var(--border)" }}>
              <td style={{ padding: "0.4rem 0", color: "var(--text-muted)" }}>{p.position ?? "-"}</td>
              <td>
                {p.web_name ?? "Unknown"}
                {p.player_id === captainId && " (C)"}
                {p.player_id === viceCaptainId && " (VC)"}
              </td>
              <td>{(p.now_cost_snapshot / 10).toFixed(1)}</td>
              <td>{Number(p.projected_points).toFixed(1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableScroll>
  );
}
