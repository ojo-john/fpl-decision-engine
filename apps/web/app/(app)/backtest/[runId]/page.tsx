import { backendClient } from "@/lib/api-client";
import { Card, ErrorState, TableScroll } from "@/components/ui";

export const dynamic = "force-dynamic";

function fmt(value: string | null): string {
  return value !== null ? Number(value).toFixed(3) : "unknown";
}

type FoldMetric = {
  fold_index?: number;
  validation_season_label?: string;
  mae?: number;
  rmse?: number;
  spearman?: number;
  top_20_capture_rate?: number;
  n_observations?: number;
};

export default async function BacktestPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  const client = backendClient();
  const { data, response } = await client.GET("/learning/backtests/{backtest_run_id}", {
    params: { path: { backtest_run_id: runId } },
  });

  if (response.status === 404) {
    return <ErrorState>No backtest run found for id {runId}.</ErrorState>;
  }
  if (!data) {
    return <ErrorState>Could not load this backtest run ({response.status}).</ErrorState>;
  }

  const folds = data.fold_metrics as FoldMetric[];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <Card>
        <h1 style={{ margin: "0 0 0.5rem", fontSize: "1.1rem" }}>
          Backtest -- {data.model_family} (feature v{data.feature_version}) -- {data.run_status}
        </h1>
        <p style={{ margin: "0 0 0.75rem", color: "var(--text-muted)", fontSize: "0.85rem" }}>
          Seasons: {data.usable_seasons.join(", ")}
          {data.degenerate_single_season && " (single season -- no true out-of-sample fold)"}
        </p>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
            gap: "0.5rem",
            fontSize: "0.9rem",
            color: "var(--text-muted)",
          }}
        >
          <span>MAE: {fmt(data.aggregate_mae)}</span>
          <span>RMSE: {fmt(data.aggregate_rmse)}</span>
          <span>Mean bias: {fmt(data.aggregate_mean_bias)}</span>
          <span>Spearman: {fmt(data.aggregate_spearman)}</span>
          <span>Top-20 capture: {fmt(data.aggregate_top_20_capture_rate)}</span>
          <span>Observations: {data.n_observations}</span>
        </div>
      </Card>

      {folds.length > 0 && (
        <Card>
          <h2 style={{ margin: "0 0 0.75rem", fontSize: "0.95rem" }}>Folds ({folds.length})</h2>
          <TableScroll>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                  <th style={{ padding: "0.25rem 0" }}>Season</th>
                  <th>MAE</th>
                  <th>RMSE</th>
                  <th>Spearman</th>
                  <th>Top-20 capture</th>
                  <th>N</th>
                </tr>
              </thead>
              <tbody>
                {folds.map((fold, index) => (
                  <tr key={fold.fold_index ?? index} style={{ borderTop: "1px solid var(--border)" }}>
                    <td style={{ padding: "0.35rem 0" }}>{fold.validation_season_label ?? "-"}</td>
                    <td>{fold.mae?.toFixed(3) ?? "-"}</td>
                    <td>{fold.rmse?.toFixed(3) ?? "-"}</td>
                    <td>{fold.spearman?.toFixed(3) ?? "-"}</td>
                    <td>{fold.top_20_capture_rate?.toFixed(3) ?? "-"}</td>
                    <td>{fold.n_observations ?? "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        </Card>
      )}
    </div>
  );
}
