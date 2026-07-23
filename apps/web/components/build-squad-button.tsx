"use client";

import { useRouter } from "next/navigation";
import { ActionButton } from "@/components/action-button";

/** POST /optimize/initial-squad needs no existing snapshot at all -- builds
 * a fresh 15-man squad from scratch via the solver. */
export function BuildSquadButton() {
  const router = useRouter();

  return (
    <ActionButton
      path="/optimize/initial-squad"
      label="Build optimal squad from scratch"
      pendingLabel="Optimizing..."
      onSuccess={(data) => {
        const body = data as { optimization_run_id: string };
        router.push(`/squad/${body.optimization_run_id}`);
      }}
    />
  );
}
