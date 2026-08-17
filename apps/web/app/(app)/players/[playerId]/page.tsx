import { backendClient } from "@/lib/api-client";
import { AvailabilityRiskNotes } from "@/components/availability-risk-notes";
import { RoleNotes } from "@/components/role-notes";
import { Card, ErrorState, PriceMomentumBadge, TableScroll } from "@/components/ui";
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

  const [
    { data: player, response: playerResponse },
    { data: projection },
    { data: priceHistory },
    { data: riskNotes },
    { data: roleNotes },
  ] = await Promise.all([
    client.GET("/players/{player_id}", { params: { path: { player_id: playerId } } }),
    client.GET("/players/{player_id}/projection", { params: { path: { player_id: playerId } } }),
    client.GET("/players/{player_id}/price-history", { params: { path: { player_id: playerId } } }),
    client.GET("/players/{player_id}/availability-risk-notes", { params: { path: { player_id: playerId } } }),
    client.GET("/players/{player_id}/role-notes", { params: { path: { player_id: playerId } } }),
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
          <div style={{ display: "flex", gap: "1.5rem", fontSize: "0.9rem", flexWrap: "wrap" }}>
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

          {projection.availability_risk_reason && (
            <p
              style={{
                margin: "0.6rem 0 0",
                padding: "0.5rem 0.6rem",
                borderRadius: "6px",
                border: "1px solid var(--danger)",
                color: "var(--danger)",
                fontSize: "0.85rem",
              }}
            >
              Flagged availability risk (severity {projection.availability_risk_severity}):{" "}
              {projection.availability_risk_reason}
            </p>
          )}

          {projection.role_boost_reason && (
            <p
              style={{
                margin: "0.6rem 0 0",
                padding: "0.5rem 0.6rem",
                borderRadius: "6px",
                border: "1px solid var(--success)",
                color: "var(--success)",
                fontSize: "0.85rem",
              }}
            >
              Boosted for a confirmed role (severity {projection.role_boost_severity}):{" "}
              {projection.role_boost_reason}
            </p>
          )}

          <p
            style={{
              margin: "0.6rem 0 0",
              padding: "0.5rem 0.6rem",
              borderRadius: "6px",
              background: "var(--surface-muted, rgba(127,127,127,0.08))",
              color: "var(--text-muted)",
              fontSize: "0.85rem",
            }}
          >
            {projection.explanation}
          </p>

          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(11rem, 1fr))",
              gap: "0.4rem 1.5rem",
              fontSize: "0.85rem",
              marginTop: "0.6rem",
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-muted)" }}>Cold-start season</span>
              <span>{projection.is_cold_start_season ? "yes" : "no"}</span>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ color: "var(--text-muted)" }}>Cold-start player</span>
              <span>{projection.is_cold_start_player ? "yes" : "no"}</span>
            </div>
            {projection.champion_model_family && (
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-muted)" }}>Model</span>
                <span>{projection.champion_model_family}</span>
              </div>
            )}
            {projection.ep_next !== null && (
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-muted)" }}>FPL ep_next</span>
                <span>{Number(projection.ep_next).toFixed(1)}</span>
              </div>
            )}
            {projection.career_avg_points_prior_seasons !== null && (
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-muted)" }}>Prior-season average</span>
                <span>{Number(projection.career_avg_points_prior_seasons).toFixed(1)}</span>
              </div>
            )}
            {projection.next_fixture_difficulty !== null && (
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-muted)" }}>Next fixture difficulty</span>
                <span>{projection.next_fixture_difficulty}/5</span>
              </div>
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

      {player.price_momentum && (
        <Card>
          <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Price momentum</h2>
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", flexWrap: "wrap" }}>
            <PriceMomentumBadge momentum={player.price_momentum.momentum} />
            {player.price_momentum.net_transfers_event !== null && (
              <span style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>
                {player.price_momentum.net_transfers_event >= 0 ? "+" : ""}
                {player.price_momentum.net_transfers_event.toLocaleString()} net transfers this gameweek
              </span>
            )}
            {player.price_momentum.estimated_owners !== null && (
              <span style={{ fontSize: "0.85rem", color: "var(--text-muted)" }}>
                ~{player.price_momentum.estimated_owners.toLocaleString()} estimated owners
                {player.selected_by_percent !== null ? ` (${player.selected_by_percent}%)` : ""}
              </span>
            )}
          </div>
          <p style={{ margin: "0.5rem 0 0", fontSize: "0.8rem", color: "var(--text-muted)" }}>
            An unofficial heuristic based on this gameweek&apos;s transfer activity relative to
            ownership -- not FPL&apos;s real price-change algorithm, which is undisclosed.
          </p>
        </Card>
      )}

      <Card>
        <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Price history</h2>
        {!priceHistory || priceHistory.length === 0 ? (
          <p style={{ margin: 0, color: "var(--text-muted)", fontSize: "0.9rem" }}>
            No price changes observed yet this season.
          </p>
        ) : (
          <TableScroll>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.9rem" }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                  <th style={{ padding: "0.3rem 0" }}>Date</th>
                  <th>Change</th>
                </tr>
              </thead>
              <tbody>
                {priceHistory.map((change) => (
                  <tr key={change.observed_at} style={{ borderTop: "1px solid var(--border)" }}>
                    <td style={{ padding: "0.4rem 0" }}>{new Date(change.observed_at).toLocaleDateString()}</td>
                    <td
                      style={{
                        color: change.new_cost > change.old_cost ? "var(--success)" : "var(--danger)",
                      }}
                    >
                      {(change.old_cost / 10).toFixed(1)} -&gt; {(change.new_cost / 10).toFixed(1)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        )}
      </Card>

      <AvailabilityRiskNotes playerId={playerId} notes={riskNotes ?? []} />
      <RoleNotes playerId={playerId} notes={roleNotes ?? []} />
    </div>
  );
}
