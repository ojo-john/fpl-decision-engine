"use client";

import { useState } from "react";
import { ActionButton } from "@/components/action-button";
import { TableScroll } from "@/components/ui";
import type { components } from "@/lib/api-types";

type ChipScheduleOptimizationRunOut = components["schemas"]["ChipScheduleOptimizationRunOut"];

/**
 * POST /optimize/chips/schedule -- the joint chip scheduler (see
 * app.jobs.handlers.chip_schedule_optimization's own module docstring). Real gap this closes: the
 * 4 chip cards on this page are each evaluated completely independently and can (did, live)
 * recommend "use now" for several chips in the SAME gameweek -- impossible to act on, since a real
 * FPL manager can only play one chip per gameweek. This button asks for the best non-colliding
 * combination across all 4, across BOTH windows each, with real path-dependent re-evaluation for
 * anything after a Wildcard (a Wildcard permanently reshapes the squad, changing what Bench
 * Boost/Triple Captain/a later chip window is actually worth -- see that module's own stated scope
 * boundary for exactly what is and isn't modeled). A fresh, full evaluation, not a lookup -- takes
 * meaningfully longer than the 4 cards above (real, uncapped Monte Carlo evaluation across two
 * windows, plus one extra re-evaluation pass per Wildcard candidate).
 */
export function ChipScheduleButton({ managerSquadSnapshotId }: { managerSquadSnapshotId: string }) {
  const [result, setResult] = useState<ChipScheduleOptimizationRunOut | null>(null);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
      <ActionButton
        path="/optimize/chips/schedule"
        body={{ manager_squad_snapshot_id: managerSquadSnapshotId }}
        label="Build joint schedule (no chip collisions)"
        pendingLabel="Scheduling..."
        stillWorkingCopy="Still working -- a full-season joint schedule genuinely takes several real minutes (much longer than the 4 cards above), not seconds."
        onSuccess={(data) => setResult(data as ChipScheduleOptimizationRunOut)}
      />
      {result && (
        <div>
          <p style={{ margin: "0 0 0.5rem", fontSize: "0.85rem", color: "var(--text-muted)" }}>
            Total expected gain across the chips below:{" "}
            {result.total_expected_gain !== null ? Number(result.total_expected_gain).toFixed(1) : "0.0"} pts
          </p>
          <TableScroll>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                  <th style={{ padding: "0.25rem 0" }}>Chip</th>
                  <th>Gameweek</th>
                  <th>Expected gain</th>
                </tr>
              </thead>
              <tbody>
                {result.assignments.map((assignment) => (
                  <tr
                    key={`${assignment.chip_type}-${assignment.window_number}`}
                    style={{ borderTop: "1px solid var(--border)" }}
                  >
                    <td style={{ padding: "0.35rem 0", textTransform: "uppercase" }}>{assignment.chip_type}</td>
                    <td>
                      {assignment.assigned_fpl_event_id !== null
                        ? `GW${assignment.assigned_fpl_event_id}`
                        : "not worth it this window"}
                    </td>
                    <td>{assignment.gain_mean !== null ? Number(assignment.gain_mean).toFixed(1) : "--"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        </div>
      )}
    </div>
  );
}
