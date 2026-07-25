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
import { Card, PriceMomentumBadge, buttonStyle } from "@/components/ui";
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
  const [baselineXiPoints, setBaselineXiPoints] = useState<number | null>(null);
  const [excludedPlayerName, setExcludedPlayerName] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { elapsedSeconds, start, stop } = useElapsedTimer();

  async function runPreview(excludePlayerIds: string[]) {
    setPending(true);
    setError(null);
    start();
    try {
      const { data, error: apiError, response } = await browserClient().POST("/optimize/preview", {
        body: { exclude_player_ids: excludePlayerIds },
      });
      if (!data) {
        setError(extractErrorMessage(apiError) ?? `Request failed (${response.status}).`);
        return;
      }
      // Remember the very first (unrestricted) solve's XI points so every "try without this
      // player" comparison always shows the delta against the real optimum, not against
      // whatever the previous exclusion happened to be.
      if (excludePlayerIds.length === 0 && data.xi_projected_points !== null) {
        setBaselineXiPoints(Number(data.xi_projected_points));
      }
      setPreview(data);
    } catch {
      setError("Could not reach the server.");
    } finally {
      setPending(false);
      stop();
    }
  }

  function handleClick() {
    setExcludedPlayerName(null);
    void runPreview([]);
  }

  function handleExclude(playerId: string, webName: string | null) {
    setExcludedPlayerName(webName ?? "this player");
    void runPreview([playerId]);
  }

  function handleReset() {
    setExcludedPlayerName(null);
    void runPreview([]);
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

          {(preview.excluded_player_ids ?? []).length > 0 && (
            <div
              style={{
                margin: "0 0 0.75rem",
                padding: "0.6rem 0.75rem",
                borderRadius: "6px",
                border: "1px solid var(--accent)",
                fontSize: "0.85rem",
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "0.75rem",
                flexWrap: "wrap",
              }}
            >
              <span>
                Excluding <strong>{excludedPlayerName}</strong>:{" "}
                {preview.xi_projected_points !== null ? Number(preview.xi_projected_points).toFixed(2) : "-"} pts
                {baselineXiPoints !== null && preview.xi_projected_points !== null && (
                  <>
                    {" "}
                    vs {baselineXiPoints.toFixed(2)} pts with them -- a difference of{" "}
                    {Math.abs(baselineXiPoints - Number(preview.xi_projected_points)).toFixed(2)} pts over{" "}
                    the horizon.
                  </>
                )}
              </span>
              <button onClick={handleReset} disabled={pending} style={{ ...buttonStyle, opacity: pending ? 0.7 : 1 }}>
                Reset to optimal
              </button>
            </div>
          )}

          {preview.composition_risk && (
            <div
              style={{
                margin: "0 0 0.75rem",
                padding: "0.6rem 0.75rem",
                borderRadius: "6px",
                border: "1px solid var(--border)",
                fontSize: "0.8rem",
                color: "var(--text-muted)",
              }}
            >
              {preview.composition_risk.budget_share_pct !== null && (
                <p style={{ margin: "0 0 0.3rem" }}>
                  {preview.composition_risk.most_expensive_player_web_name} is{" "}
                  {preview.composition_risk.budget_share_pct}% of your total budget
                  {preview.composition_risk.is_budget_notably_concentrated ? " -- notably concentrated" : ""}.
                </p>
              )}
              {preview.composition_risk.bench_depth_warnings.map((warning) => (
                <p key={warning} style={{ margin: "0.2rem 0 0", color: "var(--danger)" }}>
                  {warning}
                </p>
              ))}
            </div>
          )}

          <ul style={{ listStyle: "none", margin: "0 0 0.75rem", padding: 0, fontSize: "0.85rem" }}>
            {starters.map((player) => (
              <li
                key={player.player_id}
                style={{ padding: "0.2rem 0", display: "flex", alignItems: "center", gap: "0.4rem" }}
              >
                <span style={{ color: "var(--text-muted)" }}>{player.position}</span>
                <span>{playerLine(player)}</span>
                {player.price_momentum && player.price_momentum.momentum !== "unknown" && (
                  <PriceMomentumBadge momentum={player.price_momentum.momentum} />
                )}
                <button
                  onClick={() => handleExclude(player.player_id, player.web_name)}
                  disabled={pending}
                  style={{
                    background: "transparent",
                    border: "1px solid var(--border)",
                    borderRadius: "6px",
                    color: "var(--text-muted)",
                    padding: "0.1rem 0.5rem",
                    fontSize: "0.75rem",
                    cursor: "pointer",
                  }}
                >
                  Try without
                </button>
              </li>
            ))}
          </ul>
          <p style={{ margin: "0 0 0.3rem", fontSize: "0.8rem", color: "var(--text-muted)" }}>Bench</p>
          <ul style={{ listStyle: "none", margin: 0, padding: 0, fontSize: "0.85rem" }}>
            {bench.map((player) => (
              <li
                key={player.player_id}
                style={{ padding: "0.2rem 0", display: "flex", alignItems: "center", gap: "0.4rem" }}
              >
                <span style={{ color: "var(--text-muted)" }}>{player.position}</span>
                <span>{playerLine(player)}</span>
                {player.price_momentum && player.price_momentum.momentum !== "unknown" && (
                  <PriceMomentumBadge momentum={player.price_momentum.momentum} />
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
