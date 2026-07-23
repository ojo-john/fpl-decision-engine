"use client";

import { useRouter } from "next/navigation";
import { ActionButton } from "@/components/action-button";

/**
 * The three optimization/simulation triggers that make sense against any
 * squad snapshot (synced or manual) -- each navigates straight to the new
 * run's own detail page on success, rather than just refreshing in place,
 * since the interesting result lives on that page, not this one.
 */
export function SquadActions({
  snapshotId,
  squadPlayerIds,
  hasRawPicks,
}: {
  snapshotId: string;
  squadPlayerIds: string[];
  hasRawPicks: boolean;
}) {
  const router = useRouter();
  // LineupRequest requires exactly 15 player_ids -- a synced squad with an
  // unresolved element (see has_unresolved_players) has fewer than 15 here
  // and can never satisfy that, so don't offer an action that can only fail.
  const hasFullSquad = squadPlayerIds.length === 15;

  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "0.75rem", alignItems: "center" }}>
      {hasFullSquad ? (
        <ActionButton
          path="/optimize/lineup"
          body={{ player_ids: squadPlayerIds }}
          label="Optimize lineup"
          pendingLabel="Optimizing..."
          onSuccess={(data) => {
            const body = data as { optimization_run_id: string };
            router.push(`/squad/${body.optimization_run_id}`);
          }}
        />
      ) : (
        <span style={{ color: "var(--text-muted)", fontSize: "0.85rem" }}>
          Lineup optimization needs all 15 squad players resolved -- unavailable for this snapshot.
        </span>
      )}
      <ActionButton
        path="/optimize/transfers"
        body={{ manager_squad_snapshot_id: snapshotId }}
        label="Optimize transfers"
        pendingLabel="Optimizing..."
        onSuccess={(data) => {
          const body = data as { transfer_optimization_run_id: string };
          router.push(`/transfers/${body.transfer_optimization_run_id}`);
        }}
      />
      {hasRawPicks ? (
        <ActionButton
          path="/optimize/chips"
          body={{ manager_squad_snapshot_id: snapshotId }}
          label="Simulate chips"
          pendingLabel="Simulating..."
          onSuccess={(data) => {
            const body = data as { chip_optimization_run_id: string };
            router.push(`/chips/${body.chip_optimization_run_id}`);
          }}
        />
      ) : (
        <span style={{ color: "var(--text-muted)", fontSize: "0.85rem" }}>
          Chip simulation needs a squad with a known starting XI/bench split -- unavailable for this
          snapshot.
        </span>
      )}
    </div>
  );
}
