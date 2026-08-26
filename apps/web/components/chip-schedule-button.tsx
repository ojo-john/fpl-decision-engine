"use client";

import { useState } from "react";
import { ActionButton } from "@/components/action-button";
import { TableScroll } from "@/components/ui";
import type { components } from "@/lib/api-types";

type ChipScheduleOptimizationRunOut = components["schemas"]["ChipScheduleOptimizationRunOut"];

/**
 * POST /optimize/chips/schedule -- Phase 1 of the joint chip scheduler (see
 * app.jobs.handlers.chip_schedule_optimization's own module docstring). Real gap this closes: the
 * 4 chip cards on this page are each evaluated completely independently and can (did, live)
 * recommend "use now" for several chips in the SAME gameweek -- impossible to act on, since a real
 * FPL manager can only play one chip per gameweek. This button asks for the best non-colliding
 * combination across all 4, computed from the SAME candidate gains already shown above (no
 * re-evaluation -- see that module's own stated Phase 1 scope boundary: compounding effects
 * between chips, e.g. a Wildcard rebuild changing what Bench Boost is worth afterward, are not
 * modeled yet).
 */
export function ChipScheduleButton({ chipOptimizationRunId }: { chipOptimizationRunId: string }) {
  const [result, setResult] = useState<ChipScheduleOptimizationRunOut | null>(null);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
      <ActionButton
        path="/optimize/chips/schedule"
        body={{ chip_optimization_run_id: chipOptimizationRunId }}
        label="Build joint schedule (no chip collisions)"
        pendingLabel="Scheduling..."
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
