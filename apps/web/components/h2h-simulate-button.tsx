"use client";

import { useRouter } from "next/navigation";
import { ActionButton } from "@/components/action-button";

export function H2HSimulateButton({
  managerSquadSnapshotId,
  rivalProfileId,
}: {
  managerSquadSnapshotId: string;
  rivalProfileId: string;
}) {
  const router = useRouter();

  return (
    <ActionButton
      path="/h2h/simulate"
      body={{ manager_squad_snapshot_id: managerSquadSnapshotId, rival_profile_id: rivalProfileId }}
      label="Simulate H2H"
      pendingLabel="Simulating..."
      onSuccess={(data) => {
        const body = data as { h2h_simulation_run_id: string };
        router.push(`/h2h/${body.h2h_simulation_run_id}`);
      }}
    />
  );
}
