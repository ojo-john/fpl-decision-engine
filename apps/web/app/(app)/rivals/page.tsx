import Link from "next/link";
import { backendClient } from "@/lib/api-client";
import { ActionButton } from "@/components/action-button";
import { H2HSimulateButton } from "@/components/h2h-simulate-button";
import { SingleFieldActionForm } from "@/components/single-field-action-form";
import { SquadDifferential } from "@/components/squad-differential";
import { Card, ConfidenceBadge, EmptyState, ErrorState } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function RivalsPage({
  searchParams,
}: {
  searchParams: Promise<{ league?: string }>;
}) {
  const { league: leagueIdParam } = await searchParams;
  const client = backendClient();
  const { data: leagues, response: leaguesResponse } = await client.GET("/leagues");

  if (!leagues) {
    return <ErrorState>Could not load leagues ({leaguesResponse.status}).</ErrorState>;
  }
  if (leagues.length === 0) {
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

  // Which league to show: an explicit ?league=<id> tab selection, else whichever is flagged
  // priority, else just the first one (covers the edge case of leagues synced before the
  // priority column existed, where none are flagged yet).
  const league =
    leagues.find((row) => row.league_id === leagueIdParam) ??
    leagues.find((row) => row.is_priority) ??
    leagues[0];
  if (!league) {
    // Unreachable in practice -- leagues.length === 0 already returned above -- but needed so
    // TypeScript can narrow `league` to non-undefined for everything below.
    return <ErrorState>Could not resolve a league to show.</ErrorState>;
  }

  const [{ data: rivals, response: rivalsResponse }, { data: standings }] = await Promise.all([
    client.GET("/leagues/{league_id}/rivals", { params: { path: { league_id: league.league_id } } }),
    client.GET("/leagues/{league_id}/standings", { params: { path: { league_id: league.league_id } } }),
  ]);

  const syncButton = (
    <ActionButton
      path="/leagues/sync"
      body={{ fpl_league_id: league.fpl_league_id }}
      label="Sync now"
      pendingLabel="Syncing..."
    />
  );

  // Real league names as tab labels (LeagueOut.name -- the actual FPL league name), never the
  // id or a placeholder -- the whole point of a switcher is knowing which real league you're on.
  const leagueTabs = leagues.length > 1 && (
    <div
      style={{
        display: "flex",
        flexWrap: "wrap",
        gap: "1.25rem",
        alignItems: "center",
        borderBottom: "1px solid var(--border)",
        paddingBottom: "0.75rem",
      }}
    >
      {leagues.map((row) => (
        <Link
          key={row.league_id}
          href={`/rivals?league=${row.league_id}`}
          style={{
            color: row.league_id === league.league_id ? "var(--text)" : "var(--text-muted)",
            fontWeight: row.league_id === league.league_id ? 600 : 400,
            textDecoration: "none",
            fontSize: "0.9rem",
            paddingBottom: "0.4rem",
            borderBottom:
              row.league_id === league.league_id ? "2px solid var(--accent)" : "2px solid transparent",
          }}
        >
          {row.name}
          {row.is_priority && (
            <span style={{ color: "var(--text-muted)", fontSize: "0.75rem", marginLeft: "0.35rem" }}>
              (priority)
            </span>
          )}
        </Link>
      ))}
    </div>
  );

  const header = (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
      {leagueTabs}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1 style={{ margin: 0, fontSize: "1.1rem" }}>{league.name}</h1>
        <div style={{ display: "flex", gap: "0.75rem", alignItems: "center" }}>
          {!league.is_priority && (
            <ActionButton
              path={`/leagues/${league.league_id}/priority`}
              label="Set as priority"
              pendingLabel="Setting..."
            />
          )}
          {syncButton}
        </div>
      </div>
      <SingleFieldActionForm
        path="/leagues/sync"
        fieldName="fpl_league_id"
        fieldLabel="Sync another league"
        fieldType="number"
        submitLabel="Sync"
      />
    </div>
  );

  if (!rivals) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
        {header}
        <ErrorState>
          Could not load rivals for {league.name} ({rivalsResponse.status}).
        </ErrorState>
      </div>
    );
  }

  if (rivals.length === 0) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
        {header}
        <EmptyState>No rival profiles computed yet for {league.name} this season.</EmptyState>
      </div>
    );
  }

  const standingByManager = new Map((standings ?? []).map((entry) => [entry.fpl_manager_id, entry]));

  const { data: currentSquads } = await client.GET("/squads", { params: { query: { is_current: true } } });
  const currentSquadId = currentSquads?.[0]?.manager_squad_snapshot_id;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      {header}
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
            <SquadDifferential
              currentSquadId={currentSquadId}
              onlyYouHave={rival.players_only_you_have}
              onlyRivalHas={rival.players_only_rival_has}
            />
            {currentSquadId && (
              <div style={{ marginTop: "0.75rem" }}>
                <H2HSimulateButton
                  managerSquadSnapshotId={currentSquadId}
                  rivalProfileId={rival.rival_profile_id}
                />
              </div>
            )}
          </Card>
        );
      })}
    </div>
  );
}
