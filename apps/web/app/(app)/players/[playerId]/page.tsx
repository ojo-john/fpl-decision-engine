import { backendClient } from "@/lib/api-client";
import { Card, ErrorState } from "@/components/ui";
import type { components } from "@/lib/api-types";

export const dynamic = "force-dynamic";

type PlayerDetail = components["schemas"]["PlayerDetail"];

const STAT_ROWS: Array<{ label: string; key: keyof PlayerDetail }> = [
  { label: "Minutes", key: "minutes" },
  { label: "Goals", key: "goals_scored" },
  { label: "Assists", key: "assists" },
  { label: "Clean sheets", key: "clean_sheets" },
  { label: "Goals conceded", key: "goals_conceded" },
  { label: "Bonus", key: "bonus" },
  { label: "BPS", key: "bps" },
  { label: "Yellow cards", key: "yellow_cards" },
  { label: "Red cards", key: "red_cards" },
  { label: "Starts", key: "starts" },
];

export default async function PlayerDetailPage({
  params,
}: {
  params: Promise<{ playerId: string }>;
}) {
  const { playerId } = await params;
  const client = backendClient();

  const [{ data: player, response: playerResponse }, { data: projection }] = await Promise.all([
    client.GET("/players/{player_id}", { params: { path: { player_id: playerId } } }),
    client.GET("/players/{player_id}/projection", { params: { path: { player_id: playerId } } }),
  ]);

  if (playerResponse.status === 404) {
    return <ErrorState>No player found for id {playerId}.</ErrorState>;
  }
  if (!player) {
    return <ErrorState>Could not load this player ({playerResponse.status}).</ErrorState>;
  }

  const name = player.web_name ?? `${player.first_name ?? ""} ${player.second_name ?? ""}`.trim();

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <h1 style={{ margin: "0 0 0.25rem", fontSize: "1.1rem" }}>{name}</h1>
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: "0.9rem" }}>
          {player.position ?? "-"} -- {player.team_short_name ?? "-"} -- {(player.now_cost / 10).toFixed(1)}
        </p>
        {player.is_quarantined && (
          <p style={{ margin: "0.5rem 0 0", color: "var(--danger)", fontSize: "0.85rem" }}>
            Quarantined{player.quarantine_reason ? `: ${player.quarantine_reason}` : ""}
          </p>
        )}
        {player.news && (
          <p style={{ margin: "0.5rem 0 0", color: "var(--text-muted)", fontSize: "0.85rem" }}>{player.news}</p>
        )}
      </Card>

      {projection && (
        <Card>
          <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Live projection</h2>
          <div style={{ display: "flex", gap: "1.5rem", fontSize: "0.9rem" }}>
            <span>{Number(projection.projected_points).toFixed(1)} pts</span>
            <span style={{ color: "var(--text-muted)" }}>source: {projection.source}</span>
            <span style={{ color: projection.is_eligible ? "var(--success)" : "var(--danger)" }}>
              {projection.is_eligible ? "eligible" : "not eligible"}
            </span>
            {projection.chance_of_playing_next_round !== null && (
              <span style={{ color: "var(--text-muted)" }}>
                {projection.chance_of_playing_next_round}% chance of playing next round
              </span>
            )}
          </div>
        </Card>
      )}

      <Card>
        <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Season stats</h2>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: "0.4rem 1.5rem", fontSize: "0.9rem" }}>
          <div style={{ display: "flex", justifyContent: "space-between" }}>
            <span style={{ color: "var(--text-muted)" }}>Total points</span>
            <span>{player.total_points}</span>
          </div>
          <div style={{ display: "flex", justifyContent: "space-between" }}>
            <span style={{ color: "var(--text-muted)" }}>Points per game</span>
            <span>{player.points_per_game ?? "-"}</span>
          </div>
          <div style={{ display: "flex", justifyContent: "space-between" }}>
            <span style={{ color: "var(--text-muted)" }}>Form</span>
            <span>{player.form ?? "-"}</span>
          </div>
          <div style={{ display: "flex", justifyContent: "space-between" }}>
            <span style={{ color: "var(--text-muted)" }}>Selected by</span>
            <span>{player.selected_by_percent !== null ? `${player.selected_by_percent}%` : "-"}</span>
          </div>
          {STAT_ROWS.map(({ label, key }) => (
            <div key={key} style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-muted)" }}>{label}</span>
              <span>{String(player[key])}</span>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
