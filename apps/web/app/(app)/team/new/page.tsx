import { backendClient } from "@/lib/api-client";
import { Card } from "@/components/ui";
import { ManualSquadForm } from "./manual-squad-form";
import type { components } from "@/lib/api-types";

export const dynamic = "force-dynamic";

type PickedPlayer = components["schemas"]["PlayerSummary"];

async function loadInitialSquad(fromSnapshotId: string) {
  const client = backendClient();
  const { data: snapshot } = await client.GET("/squads/{manager_squad_snapshot_id}", {
    params: { path: { manager_squad_snapshot_id: fromSnapshotId } },
  });
  if (!snapshot) return null;

  const playerResponses = await Promise.all(
    snapshot.squad_player_ids.map((playerId) =>
      client.GET("/players/{player_id}", { params: { path: { player_id: playerId } } }),
    ),
  );
  const players: PickedPlayer[] = playerResponses
    .map(({ data }) => data)
    .filter((player) => player !== undefined)
    .map((player) => ({
      id: player.id,
      fpl_element_id: player.fpl_element_id,
      web_name: player.web_name,
      team_id: player.team_id,
      team_short_name: player.team_short_name ?? null,
      position: player.position ?? null,
      now_cost: player.now_cost,
      status: player.status,
      total_points: player.total_points,
      form: player.form,
      points_per_game: player.points_per_game,
      ep_next: player.ep_next,
      is_quarantined: player.is_quarantined,
    }));

  return { players, bank: snapshot.bank, freeTransfersAvailable: snapshot.free_transfers_available };
}

export default async function ManualSquadPage({
  searchParams,
}: {
  searchParams: Promise<{ from?: string }>;
}) {
  const { from } = await searchParams;
  const initialSquad = from ? await loadInitialSquad(from) : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <h1 style={{ margin: 0, fontSize: "1.1rem" }}>
        {initialSquad ? "Duplicate and edit squad" : "Enter a squad manually"}
      </h1>
      {from && !initialSquad && (
        <p style={{ margin: 0, color: "var(--danger)", fontSize: "0.85rem" }}>
          Could not load squad {from} to duplicate -- starting from an empty squad instead.
        </p>
      )}
      <Card>
        <ManualSquadForm initialSquad={initialSquad ?? undefined} />
      </Card>
    </div>
  );
}
