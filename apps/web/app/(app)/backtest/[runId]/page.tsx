import { backendClient } from "@/lib/api-client";
import { Card, ErrorState, TableScroll } from "@/components/ui";
import { RUN_STATUS_LABELS } from "@/lib/ui-labels";

export const dynamic = "force-dynamic";

function fmt(value: string | null): string {
  return value !== null ? Number(value).toFixed(3) : "unknown";
}

/** Plain-English one-liners for this page's statistics jargon -- these are standard model-
 * accuracy metrics computed by app/modeling/backtest.py, not this app's own invention. */
const METRIC_HELP: Record<string, string> = {
  MAE: "Average points off per prediction. Lower is better; 0 would be perfect.",
  RMSE: "Like MAE, but penalizes big misses more heavily than small ones. Lower is better.",
  "Mean bias": "Positive = the model tends to over-predict; negative = it tends to under-predict.",
  Spearman: "How well the model ranks players relative to each other. 1.0 = perfect ranking, 0 = random.",
  "Top-20 capture": "Share of the real top-20 scorers that the model also ranked in its own top 20.",
};

function MetricStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div>
        {label}: {value}
      </div>
      {METRIC_HELP[label] && (
        <div style={{ fontSize: "0.76rem", opacity: 0.85, marginTop: "0.1rem" }}>{METRIC_HELP[label]}</div>
      )}
    </div>
  );
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
          Backtest -- {data.model_family} (feature v{data.feature_version}) --{" "}
          {RUN_STATUS_LABELS[data.run_status] ?? data.run_status}
        </h1>
        <p style={{ margin: "0 0 0.75rem", color: "var(--text-muted)", fontSize: "0.85rem" }}>
          How accurate this model&apos;s predictions were on real, historical seasons it wasn&apos;t
          trained on. Seasons: {data.usable_seasons.join(", ")}
          {data.degenerate_single_season && " (single season -- no true out-of-sample fold)"}
        </p>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
            gap: "0.75rem",
            fontSize: "0.9rem",
            color: "var(--text-muted)",
          }}
        >
          <MetricStat label="MAE" value={fmt(data.aggregate_mae)} />
          <MetricStat label="RMSE" value={fmt(data.aggregate_rmse)} />
          <MetricStat label="Mean bias" value={fmt(data.aggregate_mean_bias)} />
          <MetricStat label="Spearman" value={fmt(data.aggregate_spearman)} />
          <MetricStat label="Top-20 capture" value={fmt(data.aggregate_top_20_capture_rate)} />
          <MetricStat label="Observations" value={String(data.n_observations)} />
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
