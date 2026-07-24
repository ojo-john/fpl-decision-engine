"use client";

import { useState } from "react";
import { browserClient } from "@/lib/browser-client";
import {
  extractErrorMessage,
  pendingButtonLabel,
  STILL_WORKING_COPY,
  STILL_WORKING_THRESHOLD_SECONDS,
  useElapsedTimer,
} from "@/components/action-button";
import { Card, buttonStyle } from "@/components/ui";
import type { components } from "@/lib/api-types";

type Preview = components["schemas"]["SquadPreviewOut"];
type PreviewPlayer = components["schemas"]["OptimizationRunPlayerOut"];

const POSITION_ORDER = ["GKP", "DEF", "MID", "FWD"];

function playerLine(player: PreviewPlayer): string {
  const tag = player.is_captain ? " (C)" : player.is_vice_captain ? " (VC)" : "";
  return `${player.web_name ?? "Unknown"}${tag} -- ${(player.now_cost_snapshot / 10).toFixed(1)} -- ${Number(player.projected_points).toFixed(2)} pts`;
}

/**
 * Deliberately separate from every other squad-building action on this page: this calls
 * POST /optimize/preview, which never touches the optimization gate, never persists anything
 * (no Job, no optimization_runs row) -- see that endpoint's own docstring. Exists specifically
 * for the pre-season window where the real gate refuses (no gameweek marked current yet), so a
 * manager can still see "what would this recommend" without it pretending to be an official run.
 */
export function SquadPreview() {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { elapsedSeconds, start, stop } = useElapsedTimer();

  async function handleClick() {
    setPending(true);
    setError(null);
    start();
    try {
      const { data, error: apiError, response } = await browserClient().POST("/optimize/preview", {});
      if (!data) {
        setError(extractErrorMessage(apiError) ?? `Request failed (${response.status}).`);
        return;
      }
      setPreview(data);
    } catch {
      setError("Could not reach the server.");
    } finally {
      setPending(false);
      stop();
    }
  }

  const starters = preview
    ? [...preview.players]
        .filter((p) => p.is_starting)
        .sort((a, b) => POSITION_ORDER.indexOf(a.position ?? "") - POSITION_ORDER.indexOf(b.position ?? ""))
    : [];
  const bench = preview
    ? [...preview.players].filter((p) => !p.is_starting).sort((a, b) => (a.bench_order ?? 0) - (b.bench_order ?? 0))
    : [];

  return (
    <Card>
      <h2 style={{ margin: "0 0 0.75rem", fontSize: "0.95rem" }}>Preview a squad (unofficial)</h2>
      <p style={{ margin: "0 0 0.75rem", color: "var(--text-muted)", fontSize: "0.9rem" }}>
        Runs the real solver against real players, but skips the readiness gate that blocks
        official optimization pre-season. Nothing here is saved -- it&rsquo;s a one-off look, not
        a recommendation you can revisit.
      </p>
      <button onClick={handleClick} disabled={pending} style={{ ...buttonStyle, opacity: pending ? 0.7 : 1 }}>
        {pending ? pendingButtonLabel("Solving...", elapsedSeconds) : "Preview squad"}
      </button>
      {pending && elapsedSeconds >= STILL_WORKING_THRESHOLD_SECONDS && (
        <p style={{ margin: "0.5rem 0 0", color: "var(--text-muted)", fontSize: "0.8rem" }}>
          {STILL_WORKING_COPY}
        </p>
      )}
      {error && (
        <p style={{ margin: "0.75rem 0 0", color: "var(--danger)", fontSize: "0.85rem" }}>{error}</p>
      )}

      {preview && (
        <div style={{ marginTop: "1rem" }}>
          {preview.is_cold_start_gameweek && (
            <p
              style={{
                margin: "0 0 0.75rem",
                padding: "0.6rem 0.75rem",
                borderRadius: "6px",
                border: "1px solid var(--accent)",
                color: "var(--accent)",
                fontSize: "0.85rem",
              }}
            >
              This is the season&rsquo;s first gameweek, so there&rsquo;s no current-season form
              yet -- projections below blend FPL&rsquo;s own pre-season estimate with each
              player&rsquo;s real prior-season average instead. A solid starting point; it&rsquo;ll
              sharpen further once real gameweeks are played.
            </p>
          )}
          <p style={{ margin: "0 0 0.5rem", color: "var(--text-muted)", fontSize: "0.85rem" }}>
            GW{preview.target_fpl_event_id} -- solver: {preview.solver_status} -- model:{" "}
            {preview.champion_model_family ?? "heuristic fallback"} -- squad cost{" "}
            {preview.squad_total_cost !== null ? (preview.squad_total_cost / 10).toFixed(1) : "-"}
          </p>
          <ul style={{ listStyle: "none", margin: "0 0 0.75rem", padding: 0, fontSize: "0.85rem" }}>
            {starters.map((player) => (
              <li key={player.player_id} style={{ padding: "0.2rem 0" }}>
                <span style={{ color: "var(--text-muted)", marginRight: "0.4rem" }}>{player.position}</span>
                {playerLine(player)}
              </li>
            ))}
          </ul>
          <p style={{ margin: "0 0 0.3rem", fontSize: "0.8rem", color: "var(--text-muted)" }}>Bench</p>
          <ul style={{ listStyle: "none", margin: 0, padding: 0, fontSize: "0.85rem" }}>
            {bench.map((player) => (
              <li key={player.player_id} style={{ padding: "0.2rem 0" }}>
                <span style={{ color: "var(--text-muted)", marginRight: "0.4rem" }}>{player.position}</span>
                {playerLine(player)}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
