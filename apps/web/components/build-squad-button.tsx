"use client";

import { useRouter } from "next/navigation";
import { ActionButton } from "@/components/action-button";

const GATE_ERROR_CODES = new Set(["OPTIMIZATION_NOT_PERMITTED", "NO_INGESTION_DATA", "RULES_BLOCKED"]);

/** POST /optimize/initial-squad needs no existing snapshot at all -- builds
 * a fresh 15-man squad from scratch via the solver. This is the *official*,
 * persisted path, so it goes through check_optimization_gate and is
 * refused pre-season (no gameweek is_current yet) -- see
 * app/optimization/gate.py. The 409 body's error_code is one of
 * OPTIMIZATION_NOT_PERMITTED / NO_INGESTION_DATA / RULES_BLOCKED in that
 * case; mapError below turns that into a pointer at the unofficial preview
 * escape hatch instead of surfacing the raw code. */
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
      mapError={(data) => {
        const record = data as { error_code?: string } | null;
        if (record?.error_code && GATE_ERROR_CODES.has(record.error_code)) {
          return "Official squad building is blocked until FPL marks a gameweek live (a pre-season safeguard, not a bug). Use \"Preview a squad (unofficial)\" below instead -- it runs the exact same solver against real players, it's just not saved as an official run yet.";
        }
        return null;
      }}
    />
  );
}
