import Link from "next/link";
import { backendClient } from "@/lib/api-client";
import { Card, EmptyState, ErrorState } from "@/components/ui";
import { RUN_STATUS_LABELS, TRANSFER_ACTION_LABELS } from "@/lib/ui-labels";

export const dynamic = "force-dynamic";

const ROW_STYLE = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  padding: "0.5rem 0",
  borderTop: "1px solid var(--border)",
  fontSize: "0.9rem",
} as const;

const MUTED_STYLE = { color: "var(--text-muted)", marginLeft: "0.5rem" } as const;

function formatDate(value: string): string {
  return new Date(value).toLocaleString();
}

const RECOMMENDATION_TYPE_LABELS: Record<string, string> = {
  initial_squad: "Initial squad",
  starting_xi: "Lineup",
  joint_squad_and_xi: "Squad + lineup",
};

export default async function HistoryPage() {
  const client = backendClient();
  const [optimizationRuns, transferRuns, chipRuns, h2hRuns] = await Promise.all([
    client.GET("/optimize"),
    client.GET("/optimize/transfers"),
    client.GET("/optimize/chips"),
    client.GET("/h2h/simulate"),
  ]);

  if (!optimizationRuns.data || !transferRuns.data || !chipRuns.data || !h2hRuns.data) {
    const failed = [optimizationRuns, transferRuns, chipRuns, h2hRuns].find((r) => !r.data);
    return <ErrorState>Could not load run history ({failed?.response.status}).</ErrorState>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <h1 style={{ margin: 0, fontSize: "1.1rem" }}>History</h1>
        <p style={{ margin: "0.5rem 0 0", color: "var(--text-muted)", fontSize: "0.9rem" }}>
          Every squad, transfer, chip, and head-to-head run this engine has produced, most recent
          first.
        </p>
      </Card>

      <Card>
        <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Squad &amp; lineup runs</h2>
        {optimizationRuns.data.length === 0 ? (
          <EmptyState>No squad or lineup runs yet.</EmptyState>
        ) : (
          <div>
            {optimizationRuns.data.map((run) => (
              <div key={run.optimization_run_id} style={ROW_STYLE}>
                <span>
                  {RECOMMENDATION_TYPE_LABELS[run.recommendation_type] ?? run.recommendation_type}
                  <span style={MUTED_STYLE}>{RUN_STATUS_LABELS[run.run_status] ?? run.run_status}</span>
                  <span style={MUTED_STYLE}>{formatDate(run.started_at)}</span>
                  {run.xi_projected_points !== null && (
                    <span style={MUTED_STYLE}>{Number(run.xi_projected_points).toFixed(1)} pts</span>
                  )}
                </span>
                <Link href={`/squad/${run.optimization_run_id}`}>View</Link>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card>
        <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Transfer runs</h2>
        {transferRuns.data.length === 0 ? (
          <EmptyState>No transfer-optimization runs yet.</EmptyState>
        ) : (
          <div>
            {transferRuns.data.map((run) => (
              <div key={run.transfer_optimization_run_id} style={ROW_STYLE}>
                <span>
                  {run.recommended_action
                    ? (TRANSFER_ACTION_LABELS[run.recommended_action] ?? run.recommended_action)
                    : "no recommendation"}
                  <span style={MUTED_STYLE}>{RUN_STATUS_LABELS[run.run_status] ?? run.run_status}</span>
                  <span style={MUTED_STYLE}>{formatDate(run.started_at)}</span>
                </span>
                <Link href={`/transfers/${run.transfer_optimization_run_id}`}>View</Link>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card>
        <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>Chip runs</h2>
        {chipRuns.data.length === 0 ? (
          <EmptyState>No chip-simulation runs yet.</EmptyState>
        ) : (
          <div>
            {chipRuns.data.map((run) => (
              <div key={run.chip_optimization_run_id} style={ROW_STYLE}>
                <span>
                  Gameweek {run.target_fpl_event_id}
                  <span style={MUTED_STYLE}>{RUN_STATUS_LABELS[run.run_status] ?? run.run_status}</span>
                  <span style={MUTED_STYLE}>{formatDate(run.started_at)}</span>
                </span>
                <Link href={`/chips/${run.chip_optimization_run_id}`}>View</Link>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card>
        <h2 style={{ margin: "0 0 0.5rem", fontSize: "0.95rem" }}>H2H runs</h2>
        {h2hRuns.data.length === 0 ? (
          <EmptyState>No H2H simulation runs yet.</EmptyState>
        ) : (
          <div>
            {h2hRuns.data.map((run) => (
              <div key={run.h2h_simulation_run_id} style={ROW_STYLE}>
                <span>
                  Gameweek {run.target_fpl_event_id}
                  <span style={MUTED_STYLE}>{RUN_STATUS_LABELS[run.run_status] ?? run.run_status}</span>
                  <span style={MUTED_STYLE}>{formatDate(run.started_at)}</span>
                  {run.win_probability !== null && (
                    <span style={MUTED_STYLE}>{(Number(run.win_probability) * 100).toFixed(0)}% win</span>
                  )}
                </span>
                <Link href={`/h2h/${run.h2h_simulation_run_id}`}>View</Link>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
