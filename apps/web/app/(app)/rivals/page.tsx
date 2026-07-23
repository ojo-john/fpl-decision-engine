import { backendClient } from "@/lib/api-client";
import { ActionButton } from "@/components/action-button";
import { SingleFieldActionForm } from "@/components/single-field-action-form";
import { Card, ConfidenceBadge, EmptyState, ErrorState } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function RivalsPage() {
  const client = backendClient();
  const { data: leagues, response: leaguesResponse } = await client.GET("/leagues");

  if (!leagues) {
    return <ErrorState>Could not load leagues ({leaguesResponse.status}).</ErrorState>;
  }
  const league = leagues[0];
  if (!league) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
        <EmptyState>No league has been synced yet.</EmptyState>
        <SingleFieldActionForm
          path="/leagues/sync"
          fieldName="fpl_league_id"
          fieldLabel="FPL H2H league ID"
          fieldType="number"
          submitLabel="Sync league"
        />
      </div>
    );
  }

  const [{ data: rivals, response: rivalsResponse }, { data: standings }] = await Promise.all([
    client.GET("/leagues/{league_id}/rivals", { params: { path: { league_id: league.league_id } } }),
    client.GET("/leagues/{league_id}/standings", { params: { path: { league_id: league.league_id } } }),
  ]);

  if (!rivals) {
    return <ErrorState>Could not load rivals for {league.name} ({rivalsResponse.status}).</ErrorState>;
  }

  const syncButton = (
    <ActionButton
      path="/leagues/sync"
      body={{ fpl_league_id: league.fpl_league_id }}
      label="Sync now"
      pendingLabel="Syncing..."
    />
  );

  if (rivals.length === 0) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
        <EmptyState>No rival profiles computed yet for {league.name} this season.</EmptyState>
        {syncButton}
      </div>
    );
  }

  const standingByManager = new Map((standings ?? []).map((entry) => [entry.fpl_manager_id, entry]));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1 style={{ margin: 0, fontSize: "1.1rem" }}>{league.name}</h1>
        {syncButton}
      </div>
      {rivals.map((rival) => {
        const standing = standingByManager.get(rival.fpl_manager_id);
        return (
          <Card key={rival.rival_profile_id}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "start" }}>
              <div>
                <h2 style={{ margin: "0 0 0.25rem", fontSize: "0.95rem" }}>
                  {standing?.entry_name ?? standing?.player_name ?? `Manager ${rival.fpl_manager_id}`}
                  {standing?.rank !== null && standing?.rank !== undefined && ` -- rank ${standing.rank}`}
                </h2>
              </div>
              <ConfidenceBadge confidence={rival.overall_confidence} />
            </div>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
                gap: "0.5rem",
                marginTop: "0.75rem",
                fontSize: "0.9rem",
                color: "var(--text-muted)",
              }}
            >
              <span>
                Free transfers:{" "}
                {rival.derived_free_transfers_min !== null
                  ? rival.derived_free_transfers_min === rival.derived_free_transfers_max
                    ? rival.derived_free_transfers_min
                    : `${rival.derived_free_transfers_min}-${rival.derived_free_transfers_max}`
                  : "unknown"}
              </span>
              <span>
                Hit probability:{" "}
                {rival.predicted_hit_probability !== null
                  ? `${(Number(rival.predicted_hit_probability) * 100).toFixed(0)}%`
                  : "unknown"}
              </span>
              <span>
                Remaining chips:{" "}
                {rival.derived_likely_remaining_chips.length > 0
                  ? rival.derived_likely_remaining_chips.join(", ")
                  : "none"}
              </span>
            </div>
            {rival.unknowns.length > 0 && (
              <p style={{ margin: "0.75rem 0 0", fontSize: "0.8rem", color: "var(--text-muted)" }}>
                {rival.unknowns.join(" ")}
              </p>
            )}
          </Card>
        );
      })}
    </div>
  );
}
